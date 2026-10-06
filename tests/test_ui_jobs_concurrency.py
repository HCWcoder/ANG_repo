"""Concurrent QA jobs use synthetic thread-owned vaults and make no network calls."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from anghami_session import play_record, proxy, proxy_pool, ui_jobs, ui_server
from anghami_session.errors import SessionError
from anghami_session.play_record import TEST_SONG_ID
from test_ui_server import console, real_console, request


PRIVATE_EMAIL = "synthetic-concurrency@example.invalid"
PRIVATE_PASSWORD = "synthetic-concurrency-password"
PRIVATE_SESSION = "synthetic-concurrency-session"
PRIVATE_PROXY = "synthetic-concurrency-proxy-auth"


def success(action):
    return {
        "passed": True, "event_attempted": action == "play", "event_result": "accepted" if action == "play" else "not_attempted",
        "mutation_attempted": action == "like", "mutation_result": "accepted" if action == "like" else "not_attempted",
        "password": PRIVATE_PASSWORD, "sid": PRIVATE_SESSION, "proxy_auth": PRIVATE_PROXY,
    }


def operational_failure(action):
    return {
        "passed": False, "phase": "failed", "failed_phase": "metadata" if action == "play" else "state_before",
        "error_code": "metadata_region_unavailable" if action == "play" else "state_http_failed",
        "event_attempted": False, "event_result": "not_attempted",
        "mutation_attempted": False, "mutation_result": "not_attempted", "password": PRIVATE_PASSWORD,
    }


class ConcurrentVaultFactory:
    """SQLite's native affinity check detects any vault crossing worker threads."""

    def __init__(self, path, rows, handler=None):
        self.path, self.rows = Path(path), tuple(rows)
        self.handler = handler or (lambda action, row, number, _options: success(action))
        self.identities = {row: f"synthetic-concurrency-{row}@example.invalid" for row in self.rows}
        self.cohort, self.ready_rows = self.rows, self.rows
        self.events, self.instances, self.attempts = [], [], {}
        self.active_rows, self.active_identities, self.peak = set(), set(), 0
        self.lock = threading.RLock()
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE accounts(source_row INTEGER PRIMARY KEY)")
            db.executemany("INSERT INTO accounts VALUES(?)", ((row,) for row in self.rows))

    def log(self, *event):
        with self.lock:
            self.events.append((*event, threading.get_ident()))

    def __call__(self, path):
        assert Path(path) == self.path
        vault = ConcurrentVault(self)
        with self.lock:
            self.instances.append(vault)
        return vault


class ConcurrentVault:
    def __init__(self, factory):
        self.factory, self.path = factory, factory.path
        self.owner, self.closed = threading.get_ident(), False
        self._db = sqlite3.connect(self.path)
        self.factory.log("open", id(self))

    def __enter__(self):
        assert threading.get_ident() == self.owner
        return self

    def __exit__(self, *_):
        assert threading.get_ident() == self.owner
        self._db.close()
        self.closed = True
        self.factory.log("close", id(self))

    def record(self, row):
        assert self._db.execute("SELECT source_row FROM accounts WHERE source_row=?", (row,)).fetchone() == (row,)
        self.factory.log("record", row, id(self))
        return {"email": self.factory.identities[row], "password": PRIVATE_PASSWORD}

    def enrolled_test_rows(self):
        self._db.execute("SELECT COUNT(*) FROM accounts").fetchone()
        return frozenset(self.factory.cohort)

    def test_accounts(self):
        self._db.execute("SELECT COUNT(*) FROM accounts").fetchone()
        return {"ready_rows": list(self.factory.ready_rows)}

    def test_play_record(self, row, song_id, **options):
        return self._test("play", row, song_id, options)

    def test_like(self, row, song_id, **options):
        return self._test("like", row, song_id, options)

    def _test(self, action, row, song_id, options):
        assert threading.get_ident() == self.owner
        assert self._db.execute("SELECT source_row FROM accounts WHERE source_row=?", (row,)).fetchone() == (row,)
        assert song_id == TEST_SONG_ID
        identity = self.factory.identities[row].strip().casefold()
        with self.factory.lock:
            assert row not in self.factory.active_rows, "The same account was dispatched concurrently"
            assert identity not in self.factory.active_identities, "Aliased account rows were dispatched concurrently"
            self.factory.active_rows.add(row)
            self.factory.active_identities.add(identity)
            self.factory.peak = max(self.factory.peak, len(self.factory.active_rows))
            number = self.factory.attempts.get(row, 0) + 1
            self.factory.attempts[row] = number
            self.factory.log("attempt", action, row, number, id(self), dict(options))
        try:
            return self.factory.handler(action, row, number, options)
        finally:
            with self.factory.lock:
                self.factory.active_rows.remove(row)
                self.factory.active_identities.remove(identity)
                self.factory.log("finished", action, row, number, id(self))


def manager_for(tmp_path, *, rows=range(1, 41), handler=None, proxy_loader=None):
    factory = ConcurrentVaultFactory(tmp_path / "synthetic.sqlite3", rows, handler)
    manager = ui_jobs.JobManager(
        factory.path, vault_factory=factory,
        proxy_loader=proxy_loader or (lambda _path: pytest.fail("Direct synthetic job loaded proxy credentials")),
    )
    return manager, factory


def finish(manager):
    manager._thread.join(10)
    assert not manager._thread.is_alive()
    job = manager.snapshot()
    for secret in (PRIVATE_EMAIL, PRIVATE_PASSWORD, PRIVATE_SESSION, PRIVATE_PROXY):
        assert secret not in json.dumps(job)
        assert secret not in manager._report_path.read_text(encoding="utf-8")
    assert "example.invalid" not in json.dumps(job)
    return job


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("field,bad", [
    ("workers", value) for value in (None, True, False, 0, -1, 2**53, 8.0, "8", [], {})
] + [
    ("max_consecutive_failures", value) for value in (None, True, False, 0, -1, 2**53, 20.0, "20", [], {})
])
def test_invalid_concurrency_controls_reject_before_worker_or_private_configuration(tmp_path, action, field, bad):
    factory_calls = []
    manager = ui_jobs.JobManager(tmp_path / "synthetic.sqlite3", vault_factory=lambda path: factory_calls.append(path))
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit({"action": action, "rows": [1], field: bad})
    assert manager._thread is None and manager.snapshot() is None and factory_calls == []
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("action,field", [
    (action, field)
    for action in ("prepare", "preview", "check", "song", "login", "proxy-check")
    for field in ("workers", "max_consecutive_failures")
    if (action, field) != ("prepare", "workers")
])
def test_concurrency_controls_are_rejected_for_unrelated_actions(tmp_path, action, field):
    manager = ui_jobs.JobManager(tmp_path / "synthetic.sqlite3", vault_factory=lambda _path: pytest.fail("Invalid action opened vault"))
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit({"action": action, "rows": [1], field: 1})
    assert manager._thread is None and not list(tmp_path.iterdir())


@pytest.mark.parametrize("action", ["play", "like"])
def test_default_controls_keep_one_worker_and_first_failure_policy(action):
    options = ui_jobs._validate({"action": action, "rows": [1]})
    assert options["workers"] == 1 and options["max_consecutive_failures"] == 1


@pytest.mark.parametrize("action", ["play", "like"])
def test_eight_workers_are_bounded_with_thread_owned_vaults_and_ordered_five_tests_per_row(tmp_path, action):
    ready, release = threading.Event(), threading.Event()
    guard, entered = threading.Lock(), []

    def handler(kind, row, number, _options):
        if row <= 8 and number == 1:
            with guard:
                entered.append(row)
                if len(entered) == 8:
                    ready.set()
            assert release.wait(5)
        return success(kind)

    manager, factory = manager_for(tmp_path, rows=range(1, 17), handler=handler)
    queued = manager.submit({"action": action, "rows": list(factory.rows), "count": 5, "workers": 8, "max_consecutive_failures": 20})
    try:
        assert ready.wait(5)
        assert queued["workers"] == 8 and queued["max_consecutive_failures"] == 20
        with pytest.raises(ui_jobs.JobBusyError):
            manager.submit({"action": action, "rows": [1], "workers": 8, "max_consecutive_failures": 20})
        snapshot = manager.snapshot()
        assert snapshot["status"] == "running" and snapshot["active_workers"] == 8
        assert snapshot["active_rows"] == list(range(1, 9))
        snapshot["active_rows"].clear()
        assert manager.snapshot()["active_rows"] == list(range(1, 9))
    finally:
        release.set()
    job = finish(manager)
    assert job["status"] == "succeeded" and job["progress"] == {"completed": 80, "total": 80}
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["completed_tests"]) == (80, 80, 0, 0, 80)
    assert job["active_workers"] == 0 and job["active_rows"] == [] and job["consecutive_failures"] == 0
    assert factory.peak == 8 and all(vault.closed for vault in factory.instances)
    assert len(factory.instances) > 1
    calls = [event for event in factory.events if event[0] == "attempt"]
    assert len(calls) == 80 and all(event[-1] != manager._thread.ident for event in calls)
    for row in factory.rows:
        assert [event[3] for event in calls if event[2] == row] == [1, 2, 3, 4, 5]
    assert sorted((report["source_row"], report["test_number"]) for report in job["results"]) == [
        (row, number) for row in factory.rows for number in range(1, 6)
    ]


def test_distinct_rows_with_same_normalized_account_identity_are_serialized(tmp_path):
    ready, release = threading.Event(), threading.Event()
    entered, guard = [], threading.Lock()

    def handler(action, row, number, _options):
        if row <= 8 and number == 1:
            with guard:
                entered.append(row)
                if len(entered) == 8:
                    ready.set()
            assert release.wait(5)
        return success(action)

    manager, factory = manager_for(tmp_path, rows=range(1, 10), handler=handler)
    factory.identities[9] = "  " + factory.identities[1].upper() + "  "
    manager.submit({"action": "play", "rows": list(factory.rows), "count": 3, "workers": 8, "max_consecutive_failures": 20})
    try:
        assert ready.wait(5)
        assert not any(event[0] == "attempt" and event[2] == 9 for event in factory.events)
    finally:
        release.set()
    job = finish(manager)
    assert job["status"] == "succeeded" and job["succeeded"] == 27
    assert factory.peak == 8 and not factory.active_identities
    assert factory.attempts[1] == factory.attempts[9] == 3


@pytest.mark.parametrize("action", ["play", "like"])
def test_confirmed_operational_failures_continue_and_success_resets_streak(tmp_path, action):
    def handler(kind, row, _number, _options):
        return operational_failure(kind) if row <= 2 or 4 <= row <= 22 else success(kind)

    manager, factory = manager_for(tmp_path, rows=range(1, 26), handler=handler)
    manager.submit({"action": action, "rows": list(factory.rows), "workers": 1, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "completed_with_failures" and job["phase"] == "complete"
    assert job["progress"] == {"completed": 4, "total": 25}
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["completed_tests"]) == (25, 4, 21, 0, 25)
    assert job["consecutive_failures"] == 0 and len(job["results"]) == 25
    assert factory.attempts == {row: 1 for row in factory.rows}


@pytest.mark.parametrize("action", ["play", "like"])
def test_twenty_failure_budget_reserves_inflight_slots_and_stops_at_exactly_twenty(tmp_path, action):
    barrier = threading.Barrier(8, timeout=5)

    def handler(kind, row, number, _options):
        if row <= 8 and number == 1:
            barrier.wait()
        return operational_failure(kind)

    manager, factory = manager_for(tmp_path, rows=range(1, 41), handler=handler)
    manager.submit({"action": action, "rows": list(factory.rows), "count": 2, "workers": 8, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "failed" and job["consecutive_failures"] == 20 and job["stop_reason"]
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["completed_tests"]) == (20, 0, 20, 60, 20)
    assert job["progress"] == {"completed": 0, "total": 80}
    assert sum(factory.attempts.values()) == 20 and factory.peak == 8
    assert all(number <= 2 for number in factory.attempts.values())
    assert len(job["results"]) == 20 and job["active_workers"] == 0


@pytest.mark.parametrize("action", ["play", "like"])
def test_fresh_explicit_rejection_report_continues_without_replaying_the_same_test(tmp_path, action):
    def handler(kind, row, number, _options):
        if row == 1:
            report = operational_failure(kind)
            report.update(failed_phase="mutation", error_code="event_rejected" if kind == "play" else "mutation_rejected",
                          event_attempted=kind == "play", event_result="rejected" if kind == "play" else "not_attempted",
                          mutation_attempted=kind == "like", mutation_result="rejected" if kind == "like" else "not_attempted",
                          test_number=number)
            report_path = tmp_path / f"account-{row}.{'test-play-record' if kind == 'play' else 'test-like'}-report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            raise SessionError("synthetic private rejected response " + PRIVATE_SESSION)
        return success(kind)

    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=handler)
    manager.submit({"action": action, "rows": [1, 2], "count": 3, "workers": 1, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "completed_with_failures" and job["consecutive_failures"] == 0
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"]) == (6, 3, 3, 0)
    assert factory.attempts == {1: 3, 2: 3}
    assert [(report["source_row"], report["test_number"]) for report in job["results"]] == [
        (1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3),
    ]


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("fault", ["unknown_write", "missing_report", "stale_report", "scope", "configuration", "journal", "interrupt"])
def test_unknown_scope_configuration_or_journal_failure_stops_immediately_without_replay(tmp_path, action, fault):
    report_path = tmp_path / f"account-1.{'test-play-record' if action == 'play' else 'test-like'}-report.json"
    if fault == "stale_report":
        report_path.write_text(json.dumps(operational_failure(action)), encoding="utf-8")

    def handler(kind, _row, _number, _options):
        if fault in {"missing_report", "stale_report"}:
            raise SessionError("synthetic private response " + PRIVATE_SESSION)
        if fault == "journal":
            raise play_record._Failure("journal_failed", "The synthetic action report could not be saved.")
        if fault == "interrupt":
            raise KeyboardInterrupt()
        report = operational_failure(kind)
        if fault == "unknown_write":
            report.update(failed_phase="mutation", error_code="transport_failed" if kind == "play" else "mutation_transport_failed",
                          event_attempted=kind == "play", event_result="unknown" if kind == "play" else "not_attempted",
                          mutation_attempted=kind == "like", mutation_result="unknown" if kind == "like" else "not_attempted")
        else:
            report["error_code"] = "song_scope_invalid" if fault == "scope" else "legacy_endpoint_invalid"
        return report

    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=handler)
    manager.submit({"action": action, "rows": [1, 2], "count": 3, "workers": 1, "max_consecutive_failures": 2**53 - 1})
    job = finish(manager)
    assert job["status"] == "failed" and job["stop_reason"]
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"]) == (1, 0, 1, 5)
    assert job["progress"] == {"completed": 0, "total": 6}
    assert factory.attempts == {1: 1} and job["active_workers"] == 0


def test_unknown_attempt_latches_stop_while_existing_peer_success_drains(tmp_path):
    both_started, unknown_returned = threading.Event(), threading.Event()
    guard, entered = threading.Lock(), []

    def handler(action, row, _number, _options):
        with guard:
            entered.append(row)
            if len(entered) == 2:
                both_started.set()
        assert both_started.wait(5)
        if row == 1:
            report = operational_failure(action)
            report.update(event_attempted=True, event_result="unknown", failed_phase="mutation", error_code="transport_failed")
            return report
        assert unknown_returned.wait(5)
        return success(action)

    manager, factory = manager_for(tmp_path, rows=(1, 2, 3), handler=handler)
    original_journal = ui_jobs._journal

    def journal(job, path):
        result = original_journal(job, path)
        if job.get("stop_reason") and job.get("active_workers") == 1:
            unknown_returned.set()
        return result

    # Keep the peer alive until the coordinator has durably latched the stop.
    from unittest.mock import patch
    with patch.object(ui_jobs, "_journal", journal):
        manager.submit({"action": "play", "rows": [1, 2, 3], "workers": 2, "max_consecutive_failures": 20})
        job = finish(manager)
    assert job["status"] == "failed" and job["stop_reason"]
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"]) == (2, 1, 1, 1)
    assert job["progress"] == {"completed": 1, "total": 3} and factory.attempts == {1: 1, 2: 1}


def test_duplicate_identity_grouping_stops_if_a_pending_row_changes_identity(tmp_path):
    def handler(action, row, _number, _options):
        if row == 1:
            factory.identities[2] = "synthetic-changed-after-validation@example.invalid"
        return success(action)

    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=handler)
    manager.submit({"action": "play", "rows": [1, 2], "workers": 1, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "failed" and job["stop_reason"]
    assert factory.attempts == {1: 1}
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"]) == (2, 1, 1, 0)


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("failure", ["not_prepared", "not_ready"])
def test_all_selected_accounts_validate_before_any_parallel_operation(tmp_path, action, failure):
    manager, factory = manager_for(tmp_path, rows=range(1, 17))
    if failure == "not_prepared":
        factory.cohort = factory.rows[:-1]
    else:
        factory.ready_rows = factory.rows[:-1]
    manager.submit({"action": action, "rows": list(factory.rows), "workers": 8, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "failed" and job["attempted"] == 0
    assert job["succeeded"] == job["failed"] == 0 and job["skipped"] == 16
    assert factory.attempts == {} and job["results"] == []
    assert job["error"]["code"] == ("account_not_prepared" if failure == "not_prepared" else "account_not_ready")


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("error", [
    proxy.ProxyCountryError("transport_error", curl_code=28, country_check_attempts=3),
    proxy.ProxyCountryError("http_failure", http_status=503, proxy_connect_http_status=200, country_check_attempts=3),
])
def test_typed_prewrite_proxy_transport_or_http_failure_continues_without_row_report(tmp_path, action, error):
    def handler(kind, row, _number, _options):
        if row == 1:
            raise error
        return success(kind)

    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=handler)
    manager.submit({"action": action, "rows": [1, 2], "count": 2, "workers": 1, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "completed_with_pending" and job["consecutive_failures"] == 0
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["connection_pending"]) == (3, 2, 0, 1, 1)
    assert factory.attempts == {1: 3, 2: 2}
    assert not list(tmp_path.glob("account-*.test-*-report.json"))


@pytest.mark.parametrize("error", [
    proxy.ProxyCountryError("authentication_rejected", http_status=407),
    proxy.ProxyCountryError("country_unverified"), proxy.ProxyCountryError("route_unverified"),
    proxy.ProxyCountryError("response_invalid"),
    *[proxy.ProxyCountryError("transport_error", curl_code=code) for code in (58, 60, 82, 91, 98)],
])
def test_typed_proxy_configuration_or_certificate_failure_is_deferred_without_account_failure(tmp_path, error):
    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=lambda *_: (_ for _ in ()).throw(error))
    manager.submit({"action": "play", "rows": [1, 2], "count": 2, "workers": 1, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "completed_with_pending" and job["stop_reason"] is None
    assert (job["attempted"], job["failed"], job["skipped"], job["connection_pending"]) == (2, 0, 2, 2)
    attempts = 3 if error.failure_kind in {"country_unverified", "route_unverified", "response_invalid"} else 1
    assert factory.attempts == {1: attempts, 2: attempts}


def test_typed_prewrite_proxy_failure_cannot_override_fresh_unknown_write_evidence(tmp_path):
    def handler(action, row, _number, _options):
        report = operational_failure(action)
        report.update(event_attempted=True, event_result="unknown", failed_phase="mutation", error_code="transport_failed")
        (tmp_path / f"account-{row}.test-play-record-report.json").write_text(json.dumps(report), encoding="utf-8")
        raise proxy.ProxyCountryError("transport_error", curl_code=28)

    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=handler)
    manager.submit({"action": "play", "rows": [1, 2], "count": 2, "workers": 1, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "failed" and job["stop_reason"]
    assert factory.attempts == {1: 1} and job["skipped"] == 3


@pytest.mark.parametrize("action", ["play", "like"])
def test_default_serial_failure_preserves_first_failure_behavior_and_accurate_counters(tmp_path, action):
    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=lambda *_: (_ for _ in ()).throw(SessionError(PRIVATE_SESSION)))
    queued = manager.submit({"action": action, "rows": [1, 2], "count": 3})
    job = finish(manager)
    assert queued["workers"] == queued["max_consecutive_failures"] == 1
    assert factory.attempts == {1: 1} and len(factory.instances) == 1
    assert job["status"] == "failed" and job["progress"] == {"completed": 0, "total": 6}
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["completed_tests"]) == (1, 0, 1, 5, 1)


def test_successful_already_liked_checks_are_successes_and_not_unattempted_skips(tmp_path):
    def handler(_action, _row, _number, _options):
        return {"passed": True, "mutation_attempted": False, "mutation_result": "skipped_already_liked"}

    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=handler)
    manager.submit({"action": "like", "rows": [1, 2], "count": 3, "workers": 2, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "succeeded"
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["completed_tests"]) == (6, 6, 0, 0, 6)
    assert all(report["mutation_result"] == "skipped_already_liked" for report in job["results"])
    assert factory.attempts == {1: 3, 2: 3}


def test_large_all_ready_selection_is_accepted_by_loopback_api_with_concurrency_controls(console):
    payload = {"action": "play", "rows": list(range(1, 6001)), "workers": 8, "max_consecutive_failures": 20}
    size = len(json.dumps(payload).encode("utf-8"))
    assert 16 * 1024 < size < ui_server.MAX_BODY
    status, _headers, body = request(console, "POST", "/api/jobs", body=payload)
    assert status == 202 and json.loads(body)["job"]["status"] == "queued"
    assert console.service.manager.calls == [payload]


@pytest.mark.parametrize("field,value", [("workers", 2**53), ("max_consecutive_failures", 2**53)])
def test_loopback_job_api_rejects_controls_above_current_concurrency_caps(console, field, value):
    status, _headers, _body = request(console, "POST", "/api/jobs", body={"action": "play", "rows": [1], field: value})
    assert status == 400 and console.service.manager.calls == []


def test_state_api_exposes_current_worker_and_failure_limits(real_console):
    status, _headers, body = request(real_console, "GET", "/api/state")
    assert status == 200
    limits = json.loads(body)["limits"]
    assert limits["workers"] is None and limits["worker_integer_max"] == 2**53 - 1
    assert limits["max_consecutive_failures"] is None and limits["failure_integer_max"] == 2**53 - 1


def test_retained_result_limit_preserves_aggregate_counts_and_unique_per_test_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_jobs, "MAX_RETAINED_RESULTS", 4)
    manager, factory = manager_for(tmp_path, rows=range(1, 7))
    manager.submit({"action": "play", "rows": list(factory.rows), "count": 2, "workers": 2, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "succeeded"
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["completed_tests"]) == (12, 12, 0, 0, 12)
    assert len(job["results"]) == 4 and job["results_total"] == 12 and job["results_truncated"] == 8
    reports = list((tmp_path / "ui-test-reports" / job["id"]).glob("*.redacted.json"))
    assert len(reports) == 12 and len({path.name for path in reports}) == 12
    saved = ui_server.safe_report(json.loads(manager._report_path.read_text()))
    for key in ("attempted", "succeeded", "failed", "skipped", "completed_tests", "results_total", "results_truncated"):
        assert saved[key] == job[key]


def test_canceled_worker_before_checks_or_action_is_skipped_and_not_claimed_as_attempted(tmp_path, monkeypatch):
    second_queued = threading.Event()

    def handler(action, row, _number, _options):
        assert row == 1 and second_queued.wait(5)
        report = operational_failure(action)
        report.update(event_attempted=True, event_result="unknown", failed_phase="mutation", error_code="transport_failed")
        return report

    manager, factory = manager_for(tmp_path, rows=(1, 2, 3), handler=handler)
    original_test = manager._test_once

    def test_once(options, profile, row, number, identity, stopping, start_gate=None):
        if start_gate is not None:
            assert start_gate.wait(5)
        if row == 2:
            second_queued.set()
            assert stopping.wait(5)
        return original_test(options, profile, row, number, identity, stopping, start_gate)

    monkeypatch.setattr(manager, "_test_once", test_once)
    manager.submit({"action": "play", "rows": [1, 2, 3], "workers": 2, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "failed" and job["stop_reason"]
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["completed_tests"]) == (1, 0, 1, 2, 1)
    assert factory.attempts == {1: 1} and len(job["results"]) == 1
    assert all(vault.closed for vault in factory.instances)


def test_dispatch_journal_failure_keeps_start_gate_closed_until_worker_is_canceled(tmp_path, monkeypatch):
    manager, factory = manager_for(tmp_path, rows=(1, 2))
    original_journal = ui_jobs._journal

    def journal(job, path):
        if Path(path) == manager._report_path and job.get("active_workers") == 1 and job.get("completed_tests") == 0:
            raise OSError(PRIVATE_PASSWORD)
        return original_journal(job, path)

    monkeypatch.setattr(ui_jobs, "_journal", journal)
    manager.submit({"action": "play", "rows": [1, 2], "count": 2, "workers": 2, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "failed" and job["error"]["code"] == "journal_failed"
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["completed_tests"]) == (0, 0, 0, 4, 0)
    assert factory.attempts == {} and job["active_workers"] == 0


def test_per_test_report_failure_preserves_completed_success_but_stops_next_invocation(tmp_path, monkeypatch):
    manager, factory = manager_for(tmp_path, rows=(1, 2))
    original_journal = ui_jobs._journal

    def journal(report, path):
        if "ui-test-reports" in Path(path).parts:
            raise OSError(PRIVATE_PASSWORD)
        return original_journal(report, path)

    monkeypatch.setattr(ui_jobs, "_journal", journal)
    manager.submit({"action": "like", "rows": [1, 2], "count": 2, "workers": 1, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "failed" and job["error"]["code"] == "journal_failed"
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["completed_tests"]) == (1, 1, 0, 3, 1)
    assert factory.attempts == {1: 1} and len(job["results"]) == 1
    assert job["results"][0]["passed"] is True


@pytest.mark.parametrize("fault", ["warning_transition", "terminal_warning"])
def test_final_warning_storage_fault_is_failed_with_exact_finished_counts_and_no_replay(tmp_path, monkeypatch, fault):
    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=lambda action, *_: operational_failure(action))
    original_journal, blocked = ui_jobs._journal, []

    def journal(job, path):
        warning = Path(path) == manager._report_path and job.get("stop_reason") == "completed_with_failures"
        target = warning and (job.get("phase") == "executing" if fault == "warning_transition" else job.get("status") == "completed_with_failures")
        if target:
            blocked.append(deepcopy(job))
            raise OSError(PRIVATE_PASSWORD)
        return original_journal(job, path)

    monkeypatch.setattr(ui_jobs, "_journal", journal)
    manager.submit({"action": "play", "rows": [1, 2], "count": 2, "workers": 1, "max_consecutive_failures": 20})
    job = finish(manager)
    assert len(blocked) == 1
    assert job["status"] == "failed" and job["phase"] == "failed"
    assert job["stop_reason"] == job["error"]["code"] == "journal_failed"
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["completed_tests"]) == (4, 0, 4, 0, 4)
    assert job["progress"] == {"completed": 0, "total": 4}
    assert factory.attempts == {1: 2, 2: 2} and len(job["results"]) == 4
    assert job["active_workers"] == 0 and job["active_rows"] == []
    assert len(list((tmp_path / "ui-test-reports" / job["id"]).glob("*.redacted.json"))) == 4


@pytest.mark.parametrize("workers,maximum", [(1, 1), (2, 20)])
def test_active_test_identity_and_timing_are_visible_for_serial_and_parallel_jobs(tmp_path, workers, maximum):
    ready, release = threading.Event(), threading.Event()
    guard, entered = threading.Lock(), []

    def handler(action, row, number, _options):
        if number == 1 and row <= workers:
            with guard:
                entered.append(row)
                if len(entered) == workers:
                    ready.set()
            assert release.wait(5)
        return success(action)

    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=handler)
    manager.submit({"action": "play", "rows": [1, 2], "count": 2, "workers": workers, "max_consecutive_failures": maximum})
    try:
        assert ready.wait(5)
        snapshot = manager.snapshot()
        assert snapshot["active_workers"] == workers
        active = snapshot["active_tests"]
        assert [(item["source_row"], item["test_number"]) for item in active] == [(row, 1) for row in range(1, workers + 1)]
        assert all(datetime.fromisoformat(item["started_at"]).tzinfo is not None for item in active)
        active[0]["source_row"] = 999
        assert manager.snapshot()["active_tests"][0]["source_row"] == 1
    finally:
        release.set()
    job = finish(manager)
    assert job["status"] == "succeeded" and job["active_tests"] == []
    assert job["elapsed_seconds"] >= 0
    for report in job["results"]:
        started = datetime.fromisoformat(report["started_at"])
        finished = datetime.fromisoformat(report["finished_at"])
        assert started.tzinfo is not None and finished.tzinfo is not None and finished >= started
        assert type(report["elapsed_seconds"]) in (float, int) and report["elapsed_seconds"] >= 0
        assert report["result_unknown"] is False
    archived = ui_server.safe_report(json.loads(manager._report_path.read_text()))
    assert archived["active_tests"] == [] and archived["elapsed_seconds"] == job["elapsed_seconds"]
    assert all("finished_at" in report and "elapsed_seconds" in report for report in archived["results"])


def test_result_visibility_retains_only_safe_typed_proxy_failure_evidence(tmp_path):
    error = proxy.ProxyCountryError("transport_error", curl_code=28, proxy_connect_http_status=200, country_check_attempts=3)
    error.args = (PRIVATE_PROXY,)
    error.auth_key = PRIVATE_PROXY
    error.request_url = "https://private.invalid/?session=" + PRIVATE_SESSION

    def handler(action, row, _number, _options):
        if row == 1:
            raise error
        return success(action)

    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=handler)
    manager.submit({"action": "play", "rows": [1, 2], "workers": 1, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "completed_with_pending" and factory.attempts == {1: 3, 2: 1}
    assert job["active_tests"] == [] and job["active_workers"] == 0
    failed = next(report for report in job["results"] if report["source_row"] == 1)
    assert failed["result_unknown"] is False
    assert failed["proxy_failure"] == {"failure_kind": "transport_error", "curl_code": 28,
                                        "proxy_connect_http_status": 200, "country_check_attempts": 3}
    archived = ui_server.safe_report(json.loads(manager._report_path.read_text()))
    assert archived["results"][0]["proxy_failure"] == failed["proxy_failure"]
    assert archived["active_tests"] == []
    assert PRIVATE_PROXY not in json.dumps(archived) and "private.invalid" not in json.dumps(archived)


@pytest.mark.parametrize("workers,maximum", [(1, 1), (2, 20)])
def test_local_result_timing_and_unknown_status_override_untrusted_action_fields(tmp_path, workers, maximum):
    def handler(action, _row, _number, _options):
        return {**success(action), "started_at": "2099-01-01T00:00:00+00:00",
                "finished_at": "2099-01-01T00:00:01+00:00", "elapsed_seconds": 123456789,
                "result_unknown": True}

    manager, factory = manager_for(tmp_path, rows=(1, 2), handler=handler)
    manager.submit({"action": "play", "rows": [1, 2], "workers": workers, "max_consecutive_failures": maximum})
    job = finish(manager)
    assert job["status"] == "succeeded" and factory.attempts == {1: 1, 2: 1}
    for report in job["results"]:
        assert not report["started_at"].startswith("2099-") and not report["finished_at"].startswith("2099-")
        assert 0 <= report["elapsed_seconds"] < 60 and report["result_unknown"] is False


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("pool_size,aliases", [(3, False), (13, False), (3, True)])
def test_eight_worker_pool_binding_cycles_in_selection_order_and_is_frozen_per_identity(tmp_path, action, pool_size, aliases):
    rows = [9, 2, 7, 1, 8, 3, 5, 11, 13]
    pool = proxy_pool.StickyProxyPool(tuple(proxy.PacketStreamProxy.from_route(
        "synthetic-pool-user", PRIVATE_PROXY, f"syntheticroute{number}", "http://proxy.packetstream.io:31112")
        for number in range(pool_size)))
    replacement = proxy_pool.StickyProxyPool((proxy.PacketStreamProxy.from_route(
        "synthetic-pool-user", PRIVATE_PROXY, "syntheticreplacement", "http://proxy.packetstream.io:31112"),))
    selected_pool, loads, entered = [pool], [], []
    ready, release, guard = threading.Event(), threading.Event(), threading.Lock()
    identities = {row: (9 if aliases and row == 2 else row) for row in rows}
    ordinal_by_identity, expected = {}, {}
    for row in rows:
        identity = identities[row]
        if identity not in ordinal_by_identity:
            ordinal_by_identity[identity] = len(ordinal_by_identity) % pool_size + 1
        expected[row] = ordinal_by_identity[identity]
    first_rows, seen = [], set()
    for row in rows:
        if identities[row] not in seen:
            first_rows.append(row)
            seen.add(identities[row])
        if len(first_rows) == 8:
            break

    def handler(kind, row, number, options):
        assert options["proxy"] is pool.proxy_for_index(expected[row] - 1)
        if row in first_rows and number == 1:
            with guard:
                entered.append(row)
                if len(entered) == 8:
                    ready.set()
            assert release.wait(5)
        return {**success(kind), "proxy_route_number": 10000, "proxy_pool_size": 10000}

    factory = ConcurrentVaultFactory(tmp_path / "synthetic.sqlite3", rows, handler)
    if aliases:
        factory.identities[2] = " " + factory.identities[9].upper() + " "

    def load(path):
        loads.append(Path(path))
        return selected_pool[0]

    manager = ui_jobs.JobManager(factory.path, vault_factory=factory, test_proxy_loader=load,
                                proxy_loader=lambda _path: pytest.fail("Frozen Workbench pool fell back to general proxy"))
    manager.submit({"action": action, "rows": rows, "count": 5, "workers": 8,
                    "max_consecutive_failures": 20, "proxy_test_session": True})
    try:
        assert ready.wait(5)
        selected_pool[0] = replacement
        snapshot = manager.snapshot()
        assert snapshot["active_workers"] == 8 and factory.peak == 8
        assert snapshot["proxy_pool_size"] == pool_size
        assert snapshot["proxy_route_assignments"] == [{"source_row": row, "proxy_route_number": expected[row]} for row in rows]
        assert {item["source_row"] for item in snapshot["active_tests"]} == set(first_rows)
        assert all(item["proxy_route_number"] == expected[item["source_row"]] and item["proxy_pool_size"] == pool_size
                   for item in snapshot["active_tests"])
        snapshot["proxy_route_assignments"][0]["proxy_route_number"] = 999
        assert manager.snapshot()["proxy_route_assignments"][0]["proxy_route_number"] == expected[rows[0]]
    finally:
        release.set()
    job = finish(manager)
    assert job["status"] == "succeeded" and job["attempted"] == job["succeeded"] == 45
    assert loads == [tmp_path / "packetstream-test-route.dpapi"]
    assert all(vault.closed for vault in factory.instances)
    assert factory.attempts == {row: 5 for row in rows}
    calls = [event for event in factory.events if event[0] == "attempt"]
    assert len(calls) == 45 and all(event[5]["proxy"] is pool.proxy_for_index(expected[event[2]] - 1) for event in calls)
    assert all(event[-1] != manager._thread.ident for event in calls)
    assert all(item["proxy_route_number"] == expected[item["source_row"]] and item["proxy_pool_size"] == pool_size
               for item in job["results"])
    archived = ui_server.safe_report(json.loads(manager._report_path.read_text()))
    assert archived["proxy_route_assignments"] == job["proxy_route_assignments"]
    assert archived["proxy_pool_size"] == pool_size and archived["active_tests"] == []
    assert all(item["proxy_route_number"] == expected[item["source_row"]] for item in archived["results"])


def test_failed_assignment_journal_stops_before_any_test_with_zero_attempts_and_no_fallback(tmp_path, monkeypatch):
    pool = proxy_pool.StickyProxyPool(tuple(proxy.PacketStreamProxy.from_route(
        "synthetic-pool-user", PRIVATE_PROXY, f"syntheticroute{number}", "http://proxy.packetstream.io:31112")
        for number in range(3)))
    factory = ConcurrentVaultFactory(tmp_path / "synthetic.sqlite3", range(1, 10),
                                     handler=lambda *_: pytest.fail("Assignment journal failure dispatched a test"))
    manager = ui_jobs.JobManager(factory.path, vault_factory=factory, test_proxy_loader=lambda _path: pool,
                                proxy_loader=lambda _path: pytest.fail("Assignment journal failure used another proxy"))
    original_journal, blocked = ui_jobs._journal, []

    def journal(job, path):
        if Path(path) == manager._report_path and job.get("proxy_route_assignments"):
            blocked.append(deepcopy(job))
            raise OSError(PRIVATE_PASSWORD)
        return original_journal(job, path)

    monkeypatch.setattr(ui_jobs, "_journal", journal)
    manager.submit({"action": "play", "rows": list(factory.rows), "count": 5, "workers": 8,
                    "max_consecutive_failures": 20, "proxy_test_session": True})
    job = finish(manager)
    assert len(blocked) == 1 and job["error"]["code"] == job["stop_reason"] == "journal_failed"
    assert job["status"] == job["phase"] == "failed"
    assert (job["attempted"], job["succeeded"], job["failed"], job["skipped"], job["completed_tests"]) == (0, 0, 0, 45, 0)
    assert job["progress"] == {"completed": 0, "total": 45} and job["results"] == []
    assert job["proxy_route_assignments"] == [] and job["active_tests"] == []
    assert factory.attempts == {} and len(factory.instances) == 1 and factory.instances[0].closed
    assert not list((tmp_path / "ui-test-reports").glob("**/*.redacted.json"))




