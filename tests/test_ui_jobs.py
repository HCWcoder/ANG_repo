"""Offline UI jobs: scoped writes, concurrency, progress, and secret redaction."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from anghami_session import preparation, test_settings, ui_jobs
from anghami_session.errors import SessionError
from anghami_session.play_record import TEST_SONG_ID


EMAIL = "synthetic-ui@example.invalid"
PASSWORD = "synthetic-ui-password-secret"
SESSION_SECRET = "synthetic-ui-session-secret"
PROXY_SECRET = "synthetic-ui-proxy-key-secret"
ALTERNATE_SONG_ID = "1280677978"


class FakeProxy:
    def __init__(self):
        self.calls = []

    def summary(self):
        return {"provider": "PacketStream", "country": "EG", "sticky": True, "auth_key": PROXY_SECRET}

    def verify_country(self):
        self.calls.append("verify")
        return {"country_verified": True, "proxy_used": True, "http_status": 200, "auth_key": PROXY_SECRET}


class FakeSession:
    def __init__(self, vault, row, proxy):
        self.vault, self.row, self.proxy = vault, row, proxy
        self.proxy_summary = None if proxy is None else proxy.summary()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.vault.calls.append(("close_session", self.row))

    def check(self, *, negative_control=False):
        self.vault.calls.append(("check", self.row, negative_control, self.proxy))
        return {
            "authenticated": True, "http_status": 200, "operations": {"relations": "ok", "secret": SESSION_SECRET},
            "without_session": {"authentication_rejected": True, "password": PASSWORD},
            "proxy": self.proxy_summary, "cookies": SESSION_SECRET,
        }

    def song(self, song_id):
        self.vault.calls.append(("song", self.row, song_id, self.proxy))
        return {"id": song_id, "duration": 167, "title": "Synthetic test title", "url": SESSION_SECRET}


class FakeVault:
    def __init__(self, path, *, rows=(7, 8), cohort=(7, 8), ready_rows=None, failure=None, gate=None):
        self.path = Path(path)
        self.rows, self.cohort = set(rows), set(cohort)
        self.ready_rows = self.rows & self.cohort if ready_rows is None else ready_rows
        self.failure, self.gate = failure, gate
        self.calls = []

    def __enter__(self):
        self.calls.append(("enter", threading.get_ident()))
        return self

    def __exit__(self, *_):
        self.calls.append(("exit", threading.get_ident()))

    def record(self, row):
        self.calls.append(("record", row))
        if row not in self.rows:
            raise SessionError(SESSION_SECRET)
        return {"email": EMAIL, "password": PASSWORD, "legacy_cookies": SESSION_SECRET}

    def enrolled_test_rows(self):
        self.calls.append(("cohort",))
        return frozenset(self.cohort)

    def test_accounts(self):
        self.calls.append(("readiness",))
        return {"ready_rows": list(self.ready_rows)}

    def test_play_record(self, row, song_id, **options):
        return self._test("play", row, song_id, options)

    def test_like(self, row, song_id, **options):
        return self._test("like", row, song_id, options)

    def _test(self, action, row, song_id, options):
        self.calls.append((action, row, song_id, options))
        if self.gate is not None:
            self.gate[0].set()
            assert self.gate[1].wait(3)
        if self.failure is not None and len([call for call in self.calls if call[0] in {"play", "like"}]) == self.failure:
            raise SessionError(f"Request failed at https://example.invalid/?sid={SESSION_SECRET} password={PASSWORD}")
        return {
            "passed": True, "event_accepted": action == "play", "event_result": "accepted" if action == "play" else "not_attempted",
            "mutation_result": "skipped_already_liked" if action == "like" else "not_attempted",
            "bandwidth": {"total": {"request_count": 2, "request_bytes": 321, "url": SESSION_SECRET}},
            "password": PASSWORD, "sid": SESSION_SECRET, "proxy_auth": PROXY_SECRET,
        }

    def _http_session(self, row, proxy=None):
        self.calls.append(("http_session", row, proxy))
        return FakeSession(self, row, proxy)

    def attach(self, row, saved, **options):
        self.calls.append(("attach", row, saved, options))
        return {"authenticated": True, "source_row": row, "password": PASSWORD, "session": saved}


def make_manager(tmp_path, **vault_options):
    factories = []
    proxy = FakeProxy()
    loads = []

    def vault_factory(path):
        vault = FakeVault(path, **vault_options)
        factories.append(vault)
        return vault

    def proxy_loader(path):
        loads.append((path, threading.get_ident()))
        return proxy

    manager = ui_jobs.JobManager(tmp_path / "fake.sqlite3", vault_factory=vault_factory, proxy_loader=proxy_loader)
    return manager, factories, proxy, loads


def finish(manager):
    manager._thread.join(3)
    assert not manager._thread.is_alive()
    result = manager.snapshot()
    encoded = json.dumps(result)
    for secret in (EMAIL, PASSWORD, SESSION_SECRET, PROXY_SECRET):
        assert secret not in encoded
    journal = manager._report_path.read_text(encoding="utf-8")
    for secret in (EMAIL, PASSWORD, SESSION_SECRET, PROXY_SECRET):
        assert secret not in journal
    return result


@pytest.mark.parametrize("payload", [
    None, [], {}, {"action": "arbitrary"}, {"action": True},
    {"action": "play", "rows": [7], "song_id": "1"}, {"action": "proxy-check", "password": PASSWORD},
    *[{"action": "preview", "count": value} for value in (0, -1, True, "1", 1.0, None)],
    *[{"action": "preview", "start_row": value} for value in (0, -1, True, "1", 1.0, None, 2**31)],
    *[{"action": "play", "rows": value} for value in (None, [], [7, 7], [0], [-1], [True], ["7"], [7.0], "7")],
    {"action": "login", "rows": [1, 2, 3, 4, 5, 6]},
    {"action": "play"}, {"action": "preview", "rows": None},
    {"action": "preview", "browser": []}, {"action": "preview", "browser": "unsupported"},
    *[{"action": "preview", field: value} for field in ("headless", "proxy_egypt", "reduce_browser_data") for value in ("false", 1, 0, None)],
])
def test_invalid_request_never_creates_worker_or_reads_configuration(tmp_path, payload):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit(payload)
    assert factories == [] and loads == []
    assert manager.snapshot() is None and manager._thread is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("action", ["play", "like"])
def test_tests_repeat_sequentially_for_each_row_and_hide_secret_results(tmp_path, action):
    manager, factories, proxy, loads = make_manager(tmp_path)
    main_thread = threading.get_ident()
    queued = manager.submit({"action": action, "rows": [7, 8], "count": 3, "proxy_egypt": True})
    assert queued["status"] == "queued" and queued["progress"] == {"completed": 0, "total": 6}
    result = finish(manager)
    assert result["status"] == "succeeded" and result["progress"] == {"completed": 6, "total": 6}
    assert len(result["results"]) == 6
    assert [item["test_number"] for item in result["results"]] == [1, 2, 3, 1, 2, 3]
    assert [item["source_row"] for item in result["results"]] == [7, 7, 7, 8, 8, 8]
    calls = [call for call in factories[0].calls if call[0] == action]
    assert [call[1] for call in calls] == [7, 7, 7, 8, 8, 8]
    assert all(call[2] == TEST_SONG_ID and call[3] == {"proxy": proxy} for call in calls)
    assert loads[0][1] != main_thread and factories[0].calls[0][1] != main_thread
    assert factories[0].calls[:4] == [("enter", factories[0].calls[0][1]), ("record", 7), ("record", 8), ("cohort",)]
    assert result["results"][0]["bandwidth"] == {"total": {"request_count": 2, "request_bytes": 321}}


@pytest.mark.parametrize("action", ["play", "like", "check", "song"])
@pytest.mark.parametrize("account_count", [6, 7])
def test_all_ready_six_or_seven_rows_execute_in_selected_order(tmp_path, action, account_count):
    rows = [1, 2, 3, 4, 5, 7, 8][:account_count]
    count = 2 if action in {"play", "like"} else 1
    manager, factories, proxy, loads = make_manager(tmp_path, rows=rows, cohort=rows)
    queued = manager.submit({"action": action, "rows": rows, "count": count, "proxy_egypt": True})
    assert queued["progress"] == {"completed": 0, "total": account_count * count}
    result = finish(manager)
    assert result["status"] == "succeeded"
    assert result["progress"] == {"completed": account_count * count, "total": account_count * count}
    expected_rows = [row for row in rows for _ in range(count)]
    assert [item["source_row"] for item in result["results"]] == expected_rows
    calls = [call for call in factories[0].calls if call[0] == action]
    assert [call[1] for call in calls] == expected_rows
    assert len(loads) == 1
    record_calls = [call[1] for call in factories[0].calls if call[0] == "record"]
    assert record_calls[:len(rows)] == rows and set(record_calls) == set(rows)
    if action in {"play", "like"}:
        assert [item["test_number"] for item in result["results"]] == [1, 2] * account_count
        assert all(call[3] == {"proxy": proxy} for call in calls)


@pytest.mark.parametrize("action", ["play", "like", "check", "song"])
@pytest.mark.parametrize("failure", ["missing", "not_prepared", "not_ready"])
def test_late_ineligible_row_blocks_all_six_ready_workbench_rows_before_transport(tmp_path, action, failure):
    selected = [1, 2, 3, 4, 5, 7, 8]
    options = {"rows": selected, "cohort": selected}
    if failure == "missing":
        options["rows"] = selected[:-1]
    elif failure == "not_prepared":
        options["cohort"] = selected[:-1]
    else:
        options["ready_rows"] = selected[:-1]
    manager, factories, _proxy, loads = make_manager(tmp_path, **options)
    manager.submit({"action": action, "rows": selected, "proxy_egypt": True})
    result = finish(manager)
    assert result["status"] == "failed" and result["progress"]["completed"] == 0
    assert result["results"] == [] and loads == []
    assert not any(call[0] in {"play", "like", "check", "song", "http_session"} for call in factories[0].calls)
    assert [call[1] for call in factories[0].calls if call[0] == "record"] == selected
    if failure == "not_prepared":
        assert result["error"]["code"] == "account_not_prepared"
    elif failure == "not_ready":
        assert result["error"]["code"] == "account_not_ready"


@pytest.mark.parametrize("action", ["play", "like"])
def test_all_ready_batch_stops_on_first_failure_without_later_rows_or_retries(tmp_path, action):
    rows = [1, 2, 3, 4, 5, 7, 8]
    manager, factories, _proxy, loads = make_manager(tmp_path, rows=rows, cohort=rows, failure=4)
    manager.submit({"action": action, "rows": rows, "count": 2})
    result = finish(manager)
    assert result["status"] == "failed" and result["progress"] == {"completed": 3, "total": 14}
    assert [item["source_row"] for item in result["results"]] == [1, 1, 2, 2]
    assert [item["test_number"] for item in result["results"]] == [1, 2, 1, 2]
    assert [(call[1], call[2]) for call in factories[0].calls if call[0] == action] == [(1, TEST_SONG_ID), (1, TEST_SONG_ID), (2, TEST_SONG_ID), (2, TEST_SONG_ID)]
    assert result["error"]["source_row"] == 2 and result["error"]["test_number"] == 2
    assert "No automatic retry" in result["error"]["message"] and loads == []


def test_newly_prepared_ready_account_joins_next_job_without_manager_restart(tmp_path):
    pool = {1, 2, 3, 4, 5, 7}
    factories = []

    def factory(path):
        vault = FakeVault(path, rows=pool, cohort=pool)
        factories.append(vault)
        return vault

    manager = ui_jobs.JobManager(tmp_path / "synthetic.sqlite3", vault_factory=factory)
    manager.submit({"action": "check", "rows": sorted(pool)})
    assert finish(manager)["progress"] == {"completed": 6, "total": 6}
    pool.add(8)
    manager.submit({"action": "check", "rows": sorted(pool)})
    result = finish(manager)
    assert result["status"] == "succeeded" and result["progress"] == {"completed": 7, "total": 7}
    assert [item["source_row"] for item in result["results"]] == sorted(pool)
    assert len(factories) == 2


@pytest.mark.parametrize("action", ["play", "like"])
def test_all_ready_selection_keeps_per_account_count_limit(tmp_path, action):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    with pytest.raises(ui_jobs.JobValidationError, match="count from 1 to 5"):
        manager.submit({"action": action, "rows": [1, 2, 3, 4, 5, 7, 8], "count": 6})
    assert factories == [] and loads == [] and manager._thread is None
    assert list(tmp_path.iterdir()) == []


def test_defaults_use_direct_connection_and_one_test(tmp_path):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    manager.submit({"action": "play", "rows": [7]})
    result = finish(manager)
    assert result["progress"] == {"completed": 1, "total": 1}
    assert [call for call in factories[0].calls if call[0] == "play"] == [("play", 7, TEST_SONG_ID, {})]
    assert loads == [] and result["proxy_egypt"] is False


@pytest.mark.parametrize("action", ["prepare", "login", "play", "like", "check", "song"])
def test_route_switches_are_scoped_to_each_job_and_direct_never_loads_proxy(tmp_path, monkeypatch, action):
    manager, factories, proxy, loads = make_manager(tmp_path)
    preparations, captures = [], []
    saved = {"sid": SESSION_SECRET}

    def prepare(vault, **options):
        preparations.append(dict(options))
        return {"passed": True, "prepared_rows": [9], "prepared_account_count": 1}

    def capture_login(**options):
        captures.append(dict(options))
        return saved, {}

    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    monkeypatch.setitem(sys.modules, "anghami_session.capture", SimpleNamespace(capture_login=capture_login))

    expected_loads = 0
    for egypt in (True, False, True):
        queued = manager.submit({"action": action, "rows": [7], "count": 1, "proxy_egypt": egypt})
        result = finish(manager)
        expected_proxy = proxy if egypt else None
        expected_loads += int(egypt)
        assert queued["proxy_egypt"] is egypt and result["proxy_egypt"] is egypt
        assert result["status"] == "succeeded" and result["progress"] == {"completed": 1, "total": 1}
        assert len(loads) == expected_loads
        assert all(path == tmp_path / "packetstream.dpapi" for path, _thread in loads)
        vault = factories[-1]
        if action == "prepare":
            assert preparations[-1]["proxy"] is expected_proxy
            assert preparations[-1]["dry_run"] is False
        elif action == "login":
            assert captures[-1].get("proxy") is expected_proxy
            attach = next(call for call in vault.calls if call[0] == "attach")
            assert attach[3] == ({"proxy": proxy} if egypt else {})
        elif action in {"play", "like"}:
            call = next(call for call in vault.calls if call[0] == action)
            assert call[3] == ({"proxy": proxy} if egypt else {})
        else:
            assert ("http_session", 7, expected_proxy) in vault.calls
            call = next(call for call in vault.calls if call[0] == action)
            assert call[3] is expected_proxy


@pytest.mark.parametrize("action", ["prepare", "login", "play", "like", "check", "song"])
def test_selected_egypt_proxy_load_failure_never_falls_back_to_direct(tmp_path, monkeypatch, action):
    manager, factories, _proxy, _loads = make_manager(tmp_path)
    failed_loads, preparation_calls, capture_calls = [], [], []

    def unavailable_proxy(path):
        failed_loads.append(path)
        raise SessionError(PROXY_SECRET)

    monkeypatch.setattr(manager, "_proxy_loader", unavailable_proxy)
    monkeypatch.setattr(preparation, "prepare_test_accounts", lambda *args, **options: preparation_calls.append(options))
    monkeypatch.setitem(sys.modules, "anghami_session.capture", SimpleNamespace(capture_login=lambda **options: capture_calls.append(options)))
    manager.submit({"action": action, "rows": [7], "proxy_egypt": True})
    result = finish(manager)
    assert result["proxy_egypt"] is True and result["status"] == "failed"
    assert result["progress"] == {"completed": 0, "total": 1} and result["results"] == []
    assert failed_loads == [tmp_path / "packetstream.dpapi"]
    assert preparation_calls == [] and capture_calls == []
    assert not any(call[0] in {"play", "like", "check", "song", "http_session", "attach"} for call in factories[0].calls)


@pytest.mark.parametrize("action", ["play", "like"])
def test_configured_song_is_captured_in_job_and_used_in_every_scoped_call(tmp_path, action):
    test_settings.write_test_song_id(ALTERNATE_SONG_ID, tmp_path / "test-settings.json")
    manager, factories, _proxy, _loads = make_manager(tmp_path)
    queued = manager.submit({"action": action, "rows": [7, 8], "count": 2})
    assert queued["song_id"] == ALTERNATE_SONG_ID
    result = finish(manager)
    assert result["status"] == "succeeded" and result["song_id"] == ALTERNATE_SONG_ID
    calls = [call for call in factories[0].calls if call[0] == action]
    assert len(calls) == 4
    assert all(call[2] == ALTERNATE_SONG_ID and call[3] == {"declared_song_id": ALTERNATE_SONG_ID} for call in calls)


@pytest.mark.parametrize("action", ["play", "like", "song"])
def test_stale_numeric_song_selection_is_rejected_before_any_worker_or_journal(tmp_path, action):
    settings = tmp_path / "test-settings.json"
    test_settings.write_test_song_id(ALTERNATE_SONG_ID, settings)
    before = settings.read_bytes()
    manager, factories, _proxy, loads = make_manager(tmp_path)
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit({"action": action, "rows": [7], "song_id": TEST_SONG_ID})
    assert factories == [] and loads == []
    assert manager.snapshot() is None and manager._thread is None
    assert not manager._report_path.exists()
    assert settings.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["test-settings.json"]


@pytest.mark.parametrize("action", ["play", "like", "song"])
def test_matching_numeric_song_selection_runs_with_declared_setting(tmp_path, action):
    test_settings.write_test_song_id(ALTERNATE_SONG_ID, tmp_path / "test-settings.json")
    manager, factories, _proxy, _loads = make_manager(tmp_path)
    queued = manager.submit({"action": action, "rows": [7], "song_id": ALTERNATE_SONG_ID})
    assert queued["song_id"] == ALTERNATE_SONG_ID
    result = finish(manager)
    assert result["status"] == "succeeded" and result["song_id"] == ALTERNATE_SONG_ID
    calls = [call for call in factories[0].calls if call[0] == action]
    assert len(calls) == 1 and calls[0][2] == ALTERNATE_SONG_ID
    if action in {"play", "like"}:
        assert calls[0][3] == {"declared_song_id": ALTERNATE_SONG_ID}


def test_setting_changes_after_submission_cannot_retarget_running_batch(tmp_path):
    settings = tmp_path / "test-settings.json"
    test_settings.write_test_song_id(ALTERNATE_SONG_ID, settings)
    entered, released = threading.Event(), threading.Event()
    manager, factories, _proxy, _loads = make_manager(tmp_path, gate=(entered, released))
    queued = manager.submit({"action": "play", "rows": [7, 8], "count": 2})
    assert entered.wait(3)
    try:
        test_settings.write_test_song_id(TEST_SONG_ID, settings)
        assert queued["song_id"] == ALTERNATE_SONG_ID
        assert manager.snapshot()["song_id"] == ALTERNATE_SONG_ID
    finally:
        released.set()
    result = finish(manager)
    assert result["status"] == "failed" and result["song_id"] == ALTERNATE_SONG_ID
    assert result["error"]["code"] == "song_scope_invalid"
    calls = [call for call in factories[0].calls if call[0] == "play"]
    assert len(calls) == 1
    assert all(call[2] == ALTERNATE_SONG_ID and call[3] == {"declared_song_id": ALTERNATE_SONG_ID} for call in calls)
    assert test_settings.read_test_song_id(settings) == TEST_SONG_ID


def test_song_metadata_check_uses_configured_song(tmp_path):
    test_settings.write_test_song_id(ALTERNATE_SONG_ID, tmp_path / "test-settings.json")
    manager, factories, _proxy, _loads = make_manager(tmp_path)
    manager.submit({"action": "song", "rows": [7]})
    assert finish(manager)["status"] == "succeeded"
    assert ("song", 7, ALTERNATE_SONG_ID, None) in factories[0].calls


def test_song_metadata_for_different_id_cannot_report_selected_song_success(tmp_path, monkeypatch):
    test_settings.write_test_song_id(ALTERNATE_SONG_ID, tmp_path / "test-settings.json")
    original = FakeSession.song

    def mismatched_reply(self, song_id):
        payload = original(self, song_id)
        payload["id"] = TEST_SONG_ID
        return payload

    monkeypatch.setattr(FakeSession, "song", mismatched_reply)
    manager, factories, _proxy, _loads = make_manager(tmp_path)
    manager.submit({"action": "song", "rows": [7]})
    result = finish(manager)
    assert result["status"] == "failed"
    assert result["progress"] == {"completed": 0, "total": 1}
    assert ("song", 7, ALTERNATE_SONG_ID, None) in factories[0].calls
    assert not any(report.get("passed") is True or report.get("metadata_verified") is True for report in result["results"])


def test_corrupt_settings_reject_job_before_opening_vault_or_proxy(tmp_path):
    (tmp_path / "test-settings.json").write_text('{"test_song_id":"01"}', encoding="utf-8")
    manager, factories, _proxy, loads = make_manager(tmp_path)
    with pytest.raises(SessionError):
        manager.submit({"action": "play", "rows": [7], "proxy_egypt": True})
    assert factories == [] and loads == []
    assert manager.snapshot() is None and manager._thread is None
    assert not manager._report_path.exists()


def test_public_report_keeps_valid_selected_song_and_rejects_untrusted_value():
    assert ui_jobs._public_report({"song_id": ALTERNATE_SONG_ID}) == {"song_id": ALTERNATE_SONG_ID}
    for invalid in ("01", "0", "9223372036854775808", SESSION_SECRET, True, 1.0):
        assert "song_id" not in ui_jobs._public_report({"song_id": invalid})


@pytest.mark.parametrize("action", ["play", "like"])
def test_first_failure_stops_every_later_test_and_row(tmp_path, action):
    manager, factories, _proxy, _loads = make_manager(tmp_path, failure=2)
    manager.submit({"action": action, "rows": [7, 8], "count": 3})
    result = finish(manager)
    assert result["status"] == "failed" and result["progress"] == {"completed": 1, "total": 6}
    assert len([call for call in factories[0].calls if call[0] == action]) == 2
    assert result["error"]["source_row"] == 7 and result["error"]["test_number"] == 2
    assert "No automatic retry" in result["error"]["message"]


@pytest.mark.parametrize("vault_options", [{"rows": (7,)}, {"cohort": (7,)}])
def test_validates_all_rows_and_cohort_before_any_proxy_or_test(tmp_path, vault_options):
    manager, factories, _proxy, loads = make_manager(tmp_path, **vault_options)
    manager.submit({"action": "play", "rows": [7, 8], "proxy_egypt": True})
    assert finish(manager)["status"] == "failed"
    assert loads == []
    assert not any(call[0] == "play" for call in factories[0].calls)


def test_only_one_active_job_and_snapshots_cannot_mutate_manager(tmp_path):
    entered, released = threading.Event(), threading.Event()
    manager, factories, _proxy, _loads = make_manager(tmp_path, gate=(entered, released))
    manager.submit({"action": "play", "rows": [7]})
    assert entered.wait(3)
    try:
        with pytest.raises(ui_jobs.JobBusyError):
            manager.submit({"action": "play", "rows": [8]})
        snapshot = manager.snapshot()
        snapshot["progress"]["completed"] = 900
        snapshot["results"].append({"password": PASSWORD})
        assert manager.snapshot()["progress"]["completed"] == 0
        assert manager.snapshot()["results"] == []
    finally:
        released.set()
    assert finish(manager)["status"] == "succeeded" and len(factories) == 1
    manager.submit({"action": "like", "rows": [8]})
    assert finish(manager)["status"] == "succeeded" and len(factories) == 2


def test_session_check_uses_same_proxy_and_negative_control_without_state_update(tmp_path):
    manager, factories, proxy, _loads = make_manager(tmp_path)
    manager.submit({"action": "check", "rows": [7], "proxy_egypt": True})
    result = finish(manager)
    assert result["status"] == "succeeded"
    assert result["results"][0]["passed"] is True
    assert ("check", 7, True, proxy) in factories[0].calls
    assert result["results"][0]["without_session"] == {"authentication_rejected": True}
    assert result["results"][0]["operations"] == {"relations": "ok"}


def test_song_reads_fixed_metadata_only_through_selected_proxy(tmp_path):
    manager, factories, proxy, _loads = make_manager(tmp_path)
    manager.submit({"action": "song", "rows": [7], "proxy_egypt": True})
    result = finish(manager)
    assert result["status"] == "succeeded"
    assert ("song", 7, TEST_SONG_ID, proxy) in factories[0].calls
    assert result["results"][0]["metadata_verified"] is True
    assert result["results"][0]["passed"] is True
    assert result["results"][0]["song_duration_seconds"] == 167
    assert "url" not in result["results"][0] and "title" not in result["results"][0]


def test_proxy_check_never_opens_vault_or_account_session(tmp_path):
    manager, factories, proxy, loads = make_manager(tmp_path)
    manager.submit({"action": "proxy-check"})
    result = finish(manager)
    assert result["status"] == "succeeded" and result["proxy_egypt"] is True
    assert factories == [] and len(loads) == 1 and proxy.calls == ["verify"]
    assert result["results"][0]["proxy"]["country_verified"] is True
    assert result["results"][0]["passed"] is True and result["results"][0]["country"] == "EG"


def test_preview_does_not_load_proxy_or_start_browser_and_reports_rows(tmp_path, monkeypatch):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    received = []

    def prepare(vault, **options):
        received.append((vault, options))
        return {"selected_rows": [9, 10], "prepared_rows": [], "prepared_account_count": 0, "dry_run": True, "phase": "preview", "password": PASSWORD}

    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    manager.submit({"action": "preview", "count": 2, "start_row": 9, "proxy_egypt": True})
    result = finish(manager)
    assert result["status"] == "succeeded" and result["progress"] == {"completed": 2, "total": 2}
    assert loads == [] and received[0][1]["proxy"] is None and received[0][1]["dry_run"] is True
    assert received[0][1]["headless"] is True and received[0][1]["browser_backend"] == "chrome"
    assert result["results"][0]["selected_rows"] == [9, 10]


@pytest.mark.parametrize("no_browser", [False, True])
@pytest.mark.parametrize("egypt", [False, True])
def test_offline_preview_reports_selected_route_without_loading_profile_for_either_method(tmp_path, monkeypatch, no_browser, egypt):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    received = []

    def prepare(vault, **options):
        received.append(options)
        assert options["proxy"] is None and options["dry_run"] is True
        return {
            "selected_rows": [25], "prepared_rows": [], "prepared_account_count": 0,
            "dry_run": True, "phase": "preview", "connection": "direct",
            "no_browser": no_browser, "preparation_method": "http" if no_browser else "browser",
            "browser": "none" if no_browser else "chrome",
        }

    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    manager.submit({"action": "preview", "count": 1, "start_row": 25, "proxy_egypt": egypt, "no_browser": no_browser})
    result = finish(manager)
    assert result["status"] == "succeeded" and result["proxy_egypt"] is egypt
    assert result["results"][0]["connection"] == ("proxy_egypt" if egypt else "direct")
    assert result["results"][0]["no_browser"] is no_browser
    assert received[0].get("no_browser", False) is no_browser
    assert loads == [] and len(factories) == 1
    assert not any(call[0] in {"record", "attach", "http_session"} for call in factories[0].calls)
    assert "proxy" not in result["results"][0]


def test_preparation_receives_proxy_and_progress_is_visible(tmp_path, monkeypatch):
    manager, factories, proxy, _loads = make_manager(tmp_path)
    observed = []

    def prepare(vault, **options):
        assert options["proxy"] is proxy and options["dry_run"] is False
        assert options["browser_backend"] == "chrome" and options["headless"] is True
        options["progress"]({"phase": "login", "active_row": 9, "prepared_account_count": 1, "password": PASSWORD})
        observed.append(manager.snapshot())
        return {"passed": True, "selected_rows": [9, 10], "prepared_rows": [9, 10], "prepared_account_count": 2}

    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    manager.submit({"action": "prepare", "count": 2, "proxy_egypt": True})
    result = finish(manager)
    assert observed[0]["phase"] == "login" and observed[0]["progress"] == {"completed": 1, "total": 2}
    assert result["status"] == "succeeded" and result["progress"] == {"completed": 2, "total": 2}


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("use_proxy", [False, True])
def test_browser_data_option_forwards_independently_of_preparation_route(tmp_path, monkeypatch, action, use_proxy):
    manager, _factories, proxy, loads = make_manager(tmp_path)
    received = []

    def prepare(vault, **options):
        received.append(options)
        return {
            "passed": action == "prepare", "selected_rows": [9], "prepared_rows": [9] if action == "prepare" else [],
            "prepared_account_count": 1 if action == "prepare" else 0,
            "reduce_browser_data": options["reduce_browser_data"], "password": PASSWORD,
        }

    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    queued = manager.submit({"action": action, "count": 1, "start_row": 9, "proxy_egypt": use_proxy, "reduce_browser_data": True})
    result = finish(manager)
    assert queued["reduce_browser_data"] is result["reduce_browser_data"] is True
    assert result["status"] == "succeeded"
    assert received[0]["reduce_browser_data"] is True
    assert received[0]["proxy"] is (proxy if use_proxy and action == "prepare" else None)
    assert bool(loads) is (use_proxy and action == "prepare")
    assert result["results"][0]["reduce_browser_data"] is True


def test_login_uses_selected_record_and_attaches_with_same_proxy_without_enrolling(tmp_path, monkeypatch):
    manager, factories, proxy, _loads = make_manager(tmp_path)
    captures = []
    saved = {"sid": SESSION_SECRET}

    def capture_login(**options):
        captures.append(deepcopy(options))
        return saved, {"password": PASSWORD, "url": SESSION_SECRET}

    monkeypatch.setitem(sys.modules, "anghami_session.capture", SimpleNamespace(capture_login=capture_login))
    manager.submit({"action": "login", "rows": [7], "browser": "chrome", "headless": True, "proxy_egypt": True})
    result = finish(manager)
    assert result["status"] == "succeeded"
    assert captures[0]["email"] == EMAIL and captures[0]["password"] == PASSWORD
    assert captures[0]["proxy"].summary()["country"] == "EG"
    assert ("attach", 7, saved, {"proxy": proxy}) in factories[0].calls
    assert not any(call[0] == "cohort" for call in factories[0].calls)
    assert result["results"] == [{"authenticated": True, "source_row": 7, "reduce_browser_data": False}]


@pytest.mark.parametrize("use_proxy", [False, True])
def test_refresh_browser_data_option_keeps_selected_row_and_validation_route(tmp_path, monkeypatch, use_proxy):
    manager, factories, proxy, loads = make_manager(tmp_path)
    captures = []
    saved = {"sid": SESSION_SECRET}

    def capture_login(**options):
        captures.append(dict(options))
        return saved, []

    monkeypatch.setitem(sys.modules, "anghami_session.capture", SimpleNamespace(capture_login=capture_login))
    manager.submit({"action": "login", "rows": [7], "proxy_egypt": use_proxy, "reduce_browser_data": True})
    result = finish(manager)
    assert captures == [{
        "email": EMAIL, "password": PASSWORD, "browser_backend": "chrome", "headless": True,
        "reduce_browser_data": True, **({"proxy": proxy} if use_proxy else {}),
    }]
    assert ("attach", 7, saved, {"proxy": proxy} if use_proxy else {}) in factories[0].calls
    assert bool(loads) is use_proxy
    assert result["status"] == "succeeded"
    assert result["results"] == [{"authenticated": True, "source_row": 7, "reduce_browser_data": True}]


@pytest.mark.parametrize("use_proxy", [False, True])
@pytest.mark.parametrize("value", ["true", 0, 1, None, [], {}])
def test_invalid_browser_data_flag_rejected_before_transport_on_each_route(tmp_path, use_proxy, value):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    with pytest.raises(ui_jobs.JobValidationError, match="true or false"):
        manager.submit({"action": "prepare", "proxy_egypt": use_proxy, "reduce_browser_data": value})
    assert factories == loads == []
    assert manager._thread is None and manager.snapshot() is None
    assert not manager._report_path.exists()


@pytest.mark.parametrize("value", [True, False, "synthetic-private-value", 1, None])
def test_public_report_browser_data_flag_keeps_only_boolean_facts(value):
    result = ui_jobs._public_report({"reduce_browser_data": value, "password": PASSWORD})
    assert result == ({"reduce_browser_data": value} if type(value) is bool or value is None else {})


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("use_proxy", [False, True])
def test_no_browser_preparation_forwards_explicit_method_and_ignores_browser_only_options(tmp_path, monkeypatch, action, use_proxy):
    manager, _factories, proxy, loads = make_manager(tmp_path)
    received, observed = [], []

    def prepare(vault, **options):
        received.append(dict(options))
        options["progress"]({"phase": "session_recovery", "active_row": 9, "prepared_account_count": 0, "password": PASSWORD})
        observed.append(manager.snapshot())
        return {
            "passed": action == "prepare", "selected_rows": [9], "prepared_rows": [9] if action == "prepare" else [],
            "prepared_account_count": 1 if action == "prepare" else 0,
            "no_browser": True, "browser": "none", "browser_required": False,
            "preparation_method": "http", "headless": False, "reduce_browser_data": False,
            "password": PASSWORD, "legacy_cookies": SESSION_SECRET,
        }

    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    queued = manager.submit({
        "action": action, "count": 1, "start_row": 9, "proxy_egypt": use_proxy,
        "no_browser": True, "headless": True, "reduce_browser_data": True,
    })
    result = finish(manager)
    assert queued["no_browser"] is result["no_browser"] is True
    assert queued["reduce_browser_data"] is result["reduce_browser_data"] is False
    assert result["status"] == "succeeded"
    assert received[0]["no_browser"] is True and received[0]["headless"] is False
    assert "reduce_browser_data" not in received[0]
    assert received[0]["proxy"] is (proxy if use_proxy and action == "prepare" else None)
    assert bool(loads) is (use_proxy and action == "prepare")
    assert observed[0]["phase"] == "session_recovery"
    assert result["results"][0]["browser"] == "none"
    assert result["results"][0]["preparation_method"] == "http"
    assert result["results"][0]["browser_required"] is False


@pytest.mark.parametrize("field", [{}, {"no_browser": False}])
def test_browser_preparation_default_does_not_forward_new_keyword_or_change_existing_choices(tmp_path, monkeypatch, field):
    manager, _factories, _proxy, loads = make_manager(tmp_path)
    received = []

    def prepare(vault, **options):
        received.append(options)
        return {"passed": True, "prepared_rows": [9], "prepared_account_count": 1}

    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    manager.submit({"action": "prepare", "browser": "cloakbrowser", "headless": True, "reduce_browser_data": True, **field})
    result = finish(manager)
    assert result["status"] == "succeeded" and result["no_browser"] is False
    assert "no_browser" not in received[0]
    assert received[0]["browser_backend"] == "cloakbrowser"
    assert received[0]["headless"] is True and received[0]["reduce_browser_data"] is True
    assert loads == []


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_no_browser_flag_rejects_non_boolean_before_vault_proxy_or_journal(tmp_path, action, value):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    with pytest.raises(ui_jobs.JobValidationError, match="true or false"):
        manager.submit({"action": action, "no_browser": value, "proxy_egypt": True})
    assert factories == loads == [] and manager._thread is None
    assert not manager._report_path.exists()


@pytest.mark.parametrize("action", ["play", "like", "check", "song", "login", "proxy-check"])
@pytest.mark.parametrize("value", [False, True])
def test_preparation_method_cannot_alter_another_action(tmp_path, action, value):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    with pytest.raises(ui_jobs.JobValidationError, match="preparation method"):
        manager.submit({"action": action, "rows": [7], "no_browser": value})
    assert factories == loads == [] and manager._thread is None
    assert not manager._report_path.exists()


def test_http_preparation_failure_stops_job_with_safe_message_and_visible_phase(tmp_path, monkeypatch):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    calls, observed = [], []

    def prepare(vault, **options):
        calls.append(options)
        options["progress"]({"phase": "session_recovery", "active_row": 9, "prepared_account_count": 0})
        observed.append(manager.snapshot())
        raise SessionError(f"{PASSWORD} {EMAIL} {SESSION_SECRET}")

    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    manager.submit({"action": "prepare", "count": 2, "no_browser": True})
    result = finish(manager)
    assert len(calls) == 1 and calls[0]["no_browser"] is True
    assert observed[0]["phase"] == "session_recovery"
    assert result["status"] == "failed" and result["results"] == []
    assert result["progress"] == {"completed": 0, "total": 2}
    assert "No browser login was attempted." in result["error"]["message"]
    assert loads == [] and not any(call[0] == "attach" for call in factories[0].calls)


def test_public_http_preparation_report_keeps_only_known_method_facts():
    result = ui_jobs._public_report({
        "no_browser": True, "browser": "none", "preparation_method": "http", "phase": "session_recovery",
        "browser_required": False, "password": PASSWORD, "session": SESSION_SECRET,
    })
    assert result == {"no_browser": True, "browser": "none", "preparation_method": "http", "phase": "session_recovery", "browser_required": False}
    assert ui_jobs._public_report({"preparation_method": PASSWORD, "browser": PASSWORD}) == {}


def test_failure_readback_uses_only_changed_redacted_report(tmp_path):
    manager, factories, _proxy, _loads = make_manager(tmp_path)
    path = tmp_path / "account-7.test-play-record-report.json"

    def factory(vault_path):
        vault = FakeVault(vault_path)

        def fail(*_args, **_kwargs):
            path.write_text(json.dumps({
                "passed": False, "phase": "failed", "failed_phase": "metadata",
                "error_code": "metadata_region_unavailable", "event_attempted": False,
                "event_result": "not_attempted", "password": PASSWORD,
            }), encoding="utf-8")
            raise SessionError(SESSION_SECRET)

        vault.test_play_record = fail
        return vault

    manager._vault_factory = factory
    manager.submit({"action": "play", "rows": [7]})
    result = finish(manager)
    assert result["status"] == "failed" and len(result["results"]) == 1
    assert result["error"]["failed_phase"] == "metadata"
    assert result["error"]["report_code"] == "metadata_region_unavailable"
    assert result["error"]["result_unknown"] is False
    assert result["results"][0]["test_number"] == 1


def test_failed_unknown_attempt_keeps_its_row_and_test_number(tmp_path):
    manager, _factories, _proxy, _loads = make_manager(tmp_path)
    path = tmp_path / "account-7.test-like-report.json"
    attempts = []

    def factory(vault_path):
        vault = FakeVault(vault_path)

        def like(row, song_id, **options):
            attempts.append((row, song_id))
            if len(attempts) == 1:
                return {"passed": True, "mutation_result": "skipped_already_liked"}
            path.write_text(json.dumps({
                "passed": False, "phase": "failed", "failed_phase": "mutation",
                "error_code": "mutation_transport_failed", "mutation_attempted": True,
                "mutation_result": "unknown", "password": PASSWORD,
            }), encoding="utf-8")
            raise SessionError(SESSION_SECRET)

        vault.test_like = like
        return vault

    manager._vault_factory = factory
    manager.submit({"action": "like", "rows": [7, 8], "count": 3})
    result = finish(manager)
    assert result["status"] == "failed" and result["progress"] == {"completed": 1, "total": 6}
    assert [(item["source_row"], item["test_number"]) for item in result["results"]] == [(7, 1), (7, 2)]
    assert result["results"][1]["mutation_result"] == "unknown"
    assert result["error"]["result_unknown"] is True and result["error"]["test_number"] == 2
    assert attempts == [(7, TEST_SONG_ID), (7, TEST_SONG_ID)]


@pytest.mark.parametrize("bad_report", [
    {"authenticated": False, "without_session": {"authentication_rejected": True}},
    {"authenticated": True, "without_session": {"authentication_rejected": False}},
])
def test_unconfirmed_session_check_does_not_get_success_badge(tmp_path, bad_report):
    manager, _factories, _proxy, _loads = make_manager(tmp_path)

    def factory(path):
        vault = FakeVault(path)
        session = FakeSession(vault, 7, None)
        session.check = lambda **options: bad_report
        vault._http_session = lambda *_args: session
        return vault

    manager._vault_factory = factory
    manager.submit({"action": "check", "rows": [7]})
    result = finish(manager)
    assert result["status"] == "failed" and result["results"] == []


def test_unconfirmed_proxy_check_does_not_get_success_badge(tmp_path):
    manager, _factories, proxy, _loads = make_manager(tmp_path)
    proxy.verify_country = lambda: {"country_verified": False, "proxy_used": True}
    manager.submit({"action": "proxy-check"})
    result = finish(manager)
    assert result["status"] == "failed" and result["results"] == []


def test_unchanged_previous_failure_report_is_not_claimed_for_new_attempt(tmp_path):
    manager, _factories, _proxy, _loads = make_manager(tmp_path, failure=1)
    (tmp_path / "account-7.test-play-record-report.json").write_text(json.dumps({
        "phase": "failed", "failed_phase": "mutation", "event_attempted": True, "event_result": "unknown",
    }), encoding="utf-8")
    manager.submit({"action": "play", "rows": [7]})
    result = finish(manager)
    assert len(result["results"]) == 1 and "failed_phase" not in result["error"]
    assert "failed_phase" not in result["results"][0] and "event_attempted" not in result["results"][0]


def test_unconfirmed_result_stops_without_counting_as_success(tmp_path):
    manager, _factories, _proxy, _loads = make_manager(tmp_path)

    def factory(path):
        vault = FakeVault(path)
        vault.test_like = lambda *_args, **_kwargs: {"passed": False, "mutation_result": "unknown", "password": PASSWORD}
        return vault

    manager._vault_factory = factory
    manager.submit({"action": "like", "rows": [7], "count": 3})
    result = finish(manager)
    assert result["status"] == "failed" and result["progress"]["completed"] == 0
    assert result["error"]["code"] == "test_not_confirmed" and len(result["results"]) == 1


def test_journal_failure_before_queue_does_not_create_worker(tmp_path, monkeypatch):
    manager, factories, _proxy, loads = make_manager(tmp_path)

    def fail_journal(*_args):
        raise OSError(PASSWORD)

    monkeypatch.setattr(ui_jobs, "_journal", fail_journal)
    with pytest.raises(ui_jobs.JobValidationError, match="report could not be saved"):
        manager.submit({"action": "play", "rows": [7]})
    assert factories == [] and loads == [] and manager._thread is None


def test_journal_failure_before_attempt_stops_without_test_call(tmp_path, monkeypatch):
    manager, factories, _proxy, _loads = make_manager(tmp_path)
    original = ui_jobs._journal

    def journal(report, path):
        if report["phase"] == "executing":
            raise OSError(PASSWORD)
        original(report, path)

    monkeypatch.setattr(ui_jobs, "_journal", journal)
    manager.submit({"action": "play", "rows": [7]})
    assert finish(manager)["status"] == "failed"
    assert not any(call[0] == "play" for call in factories[0].calls)
