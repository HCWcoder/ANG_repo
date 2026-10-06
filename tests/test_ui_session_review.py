"""Offline durable no-engagement quarantine and explicit GET-review contracts."""

import json
import sqlite3

import pytest

from anghami_session import ui_jobs, ui_server
from anghami_session.errors import RequestFailure, SessionError, safe_request_failure
from anghami_session.play_record import TEST_SONG_ID
from test_ui_jobs_concurrency import ConcurrentVault, ConcurrentVaultFactory, finish, operational_failure, success
from test_ui_server import console, request


STAMP = "2026-10-04T10:00:00+00:00"


def unknown_renewal(action, **changes):
    prefix = "event" if action == "play" else "mutation"
    return {"passed": False, "phase": "failed", "failed_phase": "account_identity", "song_id": TEST_SONG_ID,
            "renewal_attempted": True, "renewal_completed": False, "error_code": "session_renewal_unknown",
            f"{prefix}_attempted": False, f"{prefix}_attempts": 0, f"{prefix}_accepted": False,
            f"{prefix}_result": "not_attempted", "session_failure": safe_request_failure(
                RequestFailure("request_transport_failed", stage="identity", curl_code=28, retry_safe=False)), **changes}


class ReviewFactory(ConcurrentVaultFactory):
    def __init__(self, path, rows, handler=None):
        super().__init__(path, rows, handler)
        self.store_error = False
        self.review_handler = None
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE holds(source_row INTEGER PRIMARY KEY, failure TEXT NOT NULL, job_id TEXT NOT NULL)")

    def __call__(self, path):
        assert path == self.path
        vault = ReviewVault(self)
        with self.lock:
            self.instances.append(vault)
        return vault


class ReviewVault(ConcurrentVault):
    def session_review(self):
        rows = self._db.execute("SELECT source_row,failure,job_id FROM holds ORDER BY source_row").fetchall()
        accounts = [{"source_row": row, "state": "session_review_pending", "failure_code": json.loads(failure)["code"],
                     "failed_stage": json.loads(failure)["stage"], "held_at": STAMP, "job_id": job_id,
                     "session_failure": json.loads(failure)} for row, failure, job_id in rows]
        identities = {self.factory.identities[row].strip().casefold() for row, *_ in rows}
        return {"accounts": accounts, "total": len(accounts), "session_review_rows": [row for row, *_ in rows],
                "held_rows": [row for row in self.factory.rows if self.factory.identities[row].strip().casefold() in identities]}

    def test_accounts(self):
        blocked = set(self.session_review()["held_rows"])
        return {"ready_rows": [row for row in self.factory.ready_rows if row not in blocked]}

    def record_session_review(self, row, failure, job_id):
        self.factory.log("hold", row, safe_request_failure(failure), job_id)
        if self.factory.store_error:
            raise SessionError("synthetic private storage exception")
        identity = self.factory.identities[row].strip().casefold()
        with self._db:
            self._db.executemany("INSERT OR REPLACE INTO holds VALUES(?,?,?)", [
                (alias, json.dumps(safe_request_failure(failure)), job_id) for alias in self.factory.rows
                if self.factory.identities[alias].strip().casefold() == identity])

    def review_saved_session(self, row, **options):
        self.factory.log("review", row, dict(options))
        if self.factory.review_handler:
            return self.factory.review_handler(self, row, options)
        with self._db:
            self._db.execute("DELETE FROM holds WHERE source_row=?", (row,))
        return {"source_row": row, "passed": True, "authenticated": True, "negative_control_passed": True,
                "server_account_identity_verified": True, "session_review_cleared": True,
                "cleared_rows": [row], "checked_at_utc": STAMP}


def make_manager(tmp_path, rows=(1, 2, 3), handler=None, **kwargs):
    factory = ReviewFactory(tmp_path / "synthetic.sqlite3", rows, handler)
    manager = ui_jobs.JobManager(factory.path, vault_factory=factory,
                                proxy_loader=kwargs.get("proxy_loader", lambda _: pytest.fail("Unexpected proxy configuration")))
    return manager, factory


def put_hold(factory, row):
    with factory(factory.path) as vault:
        vault.record_session_review(row, unknown_renewal("play")["session_failure"], job_id="a" * 32)


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("workers", [1, 3])
def test_fresh_typed_bootstrap_timeout_quarantines_once_and_continues_unrelated(tmp_path, monkeypatch, action, workers):
    def handler(kind, row, number, options):
        if row == 1:
            name = "test-play-record" if kind == "play" else "test-like"
            (tmp_path / f"account-{row}.{name}-report.json").write_text(json.dumps(unknown_renewal(kind)), encoding="utf-8")
            raise RequestFailure("request_transport_failed", stage="identity", curl_code=28, retry_safe=False)
        return success(kind)

    manager, factory = make_manager(tmp_path, rows=(1, 2, 3, 4), handler=handler)
    factory.identities[4] = " " + factory.identities[1].upper() + " "
    monkeypatch.setattr(manager, "_next_test_route", lambda *_args, **_kw: pytest.fail("Unknown bootstrap was rotated"))
    manager.submit({"action": action, "rows": [1, 2, 3, 4], "count": 3, "workers": workers,
                    "max_consecutive_failures": 1 if workers == 1 else 5})
    job = finish(manager)
    assert job["status"] == "completed_with_pending" and job["stop_reason"] is None
    assert factory.attempts == {1: 1, 2: 3, 3: 3}
    assert job["session_review_pending"] == 1 and job["failed"] == job["account_failed"] == 0
    assert job["succeeded"] == 6 and job["writes_attempted"] == 6 and job["consecutive_failures"] == 0
    held = [report for report in job["results"] if report["source_row"] == 1][0]
    assert held["outcome"] == "session_review_pending" and held["result_unknown"] is False
    assert held["renewal_unknown"] is True and held["provider_attempts"] == 1
    assert held["attempt_history"][0]["outcome"] == "session_review_pending"
    assert sum(event[0] == "hold" for event in factory.events) == 1
    with factory(factory.path) as vault:
        assert vault.session_review()["held_rows"] == [1, 4]
    assert "_no_write_proven" not in json.dumps(job) and "_fresh_scoped" not in json.dumps(job)


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("changes", [
    {"event_attempted": True, "event_attempts": 1, "event_accepted": None, "event_result": "unknown"},
    {"mutation_attempted": True, "mutation_attempts": 1, "mutation_accepted": None, "mutation_result": "unknown"},
    {"event_accepted": True}, {"mutation_accepted": True}, {"event_attempts": 0.0}, {"mutation_attempts": 0.0},
    {"event_attempted": "false"}, {"mutation_attempted": "false"}, {"event_result": "accepted"},
    {"mutation_result": "accepted"}, {"event_accepted": None}, {"mutation_accepted": None},
    {"writes_attempted": 1}, {"writes_accepted": 1}, {"writes_accepted": "0"}, {"event_unknown": True},
    {"api_status": "unknown"}, {"api_status": "ok"},
    {"renewal_completed": None}, {"renewal_completed": 0}, {"renewal_completed": "false"},
    {"mutation_unknown_count": 1}, {"song_id": str(int(TEST_SONG_ID) + 1)}, {"source_row": 2},
])
def test_contradictory_or_malformed_no_write_evidence_keeps_global_hold(tmp_path, action, changes):
    manager, factory = make_manager(tmp_path, handler=lambda kind, *_: unknown_renewal(kind, **changes))
    manager.submit({"action": action, "rows": [1, 2]})
    job = finish(manager)
    assert job["status"] == "failed" and job["session_review_pending"] == 0 and factory.attempts == {1: 1}
    assert not any(event[0] == "hold" for event in factory.events)


@pytest.mark.parametrize("failure", [
    {}, safe_request_failure(RequestFailure("session_response_invalid", stage="identity")),
    safe_request_failure(RequestFailure("request_transport_failed", stage="identity", curl_code=60)),
    safe_request_failure(RequestFailure("request_transport_failed", stage="identity", curl_code=28, http_status=403)),
    safe_request_failure(RequestFailure("request_transport_failed", stage="metadata", curl_code=28)),
    safe_request_failure(RequestFailure("request_transport_failed", stage="identity", curl_code=99)),
])
def test_untyped_semantic_certificate_authentication_and_other_stage_unknown_still_stop(tmp_path, failure):
    manager, factory = make_manager(tmp_path, handler=lambda kind, *_: unknown_renewal(kind, session_failure=failure))
    manager.submit({"action": "play", "rows": [1, 2]})
    job = finish(manager)
    assert job["status"] == "failed" and job["session_review_pending"] == 0
    assert factory.attempts == {1: 1} and not any(event[0] == "hold" for event in factory.events)


@pytest.mark.parametrize("missing", ["song_id", "event_attempts", "event_accepted", "renewal_completed"])
def test_incomplete_fresh_proof_keeps_global_hold(tmp_path, missing):
    report = unknown_renewal("play")
    report.pop(missing)
    manager, factory = make_manager(tmp_path, handler=lambda *_: report)
    manager.submit({"action": "play", "rows": [1, 2]})
    job = finish(manager)
    assert job["status"] == "failed" and job["session_review_pending"] == 0 and factory.attempts == {1: 1}


def test_stale_valid_report_is_not_no_write_proof(tmp_path):
    (tmp_path / "account-1.test-play-record-report.json").write_text(json.dumps(unknown_renewal("play")), encoding="utf-8")
    def handler(*_):
        raise RequestFailure("request_transport_failed", stage="identity", curl_code=28, retry_safe=False)
    manager, factory = make_manager(tmp_path, handler=handler)
    manager.submit({"action": "play", "rows": [1, 2]})
    job = finish(manager)
    assert job["status"] == "completed_with_pending" and job["session_review_pending"] == 0
    assert job["connection_pending"] == 2 and factory.attempts == {1: 1, 2: 1}
    assert not any(event[0] == "hold" for event in factory.events)


@pytest.mark.parametrize("workers", [1, 3])
def test_hold_storage_failure_is_immediate_global_journal_hold(tmp_path, workers):
    manager, factory = make_manager(tmp_path, handler=lambda kind, *_: unknown_renewal(kind))
    factory.store_error = True
    manager.submit({"action": "play", "rows": [1, 2, 3], "workers": workers, "max_consecutive_failures": 10})
    job = finish(manager)
    assert job["status"] == "failed" and job["error"]["code"] == "journal_failed"
    assert job["session_review_pending"] == 0 and max(factory.attempts.values()) == 1
    with factory(factory.path) as vault:
        assert vault.session_review()["total"] == 0


def test_frozen_identity_changed_before_hold_persistence_remains_global_scope_hold(tmp_path):
    manager, factory = make_manager(tmp_path)
    def handler(kind, row, *_):
        factory.identities[row] = "changed@example.invalid"
        return unknown_renewal(kind)
    factory.handler = handler
    manager.submit({"action": "like", "rows": [1, 2]})
    job = finish(manager)
    assert job["status"] == "failed" and job["error"]["code"] == "account_scope_invalid"
    assert factory.attempts == {1: 1} and not any(event[0] == "hold" for event in factory.events)


def test_pending_hold_neither_resets_nor_increments_confirmed_failure_streak(tmp_path):
    def handler(kind, row, *_):
        return unknown_renewal(kind) if row == 2 else operational_failure(kind)
    manager, factory = make_manager(tmp_path, rows=(1, 2, 3, 4), handler=handler)
    manager.submit({"action": "play", "rows": [1, 2, 3, 4], "workers": 1, "max_consecutive_failures": 2})
    job = finish(manager)
    assert job["stop_reason"] == "consecutive_failure_limit" and job["consecutive_failures"] == 2
    assert job["session_review_pending"] == 1 and job["failed"] == 2 and factory.attempts == {1: 1, 2: 1, 3: 1}


@pytest.mark.parametrize("action", ["play", "like", "check", "song", "login"])
def test_new_manager_blocks_held_alias_before_browser_or_test(tmp_path, monkeypatch, action):
    manager, factory = make_manager(tmp_path)
    factory.identities[3] = factory.identities[1]
    put_hold(factory, 1)
    # Explicitly reviewed entry3 is still blocked by remaining entry1.
    with factory(factory.path) as vault:
        vault._db.execute("DELETE FROM holds WHERE source_row=3")
        vault._db.commit()
    monkeypatch.setattr("anghami_session.capture.capture_login", lambda **_: pytest.fail("Held account opened browser"))
    reopened = ui_jobs.JobManager(factory.path, vault_factory=factory)
    reopened.submit({"action": action, "rows": [3]})
    job = finish(reopened)
    assert job["status"] == "failed" and job["error"]["code"] == "session_review_required"
    assert factory.attempts == {} and not any(event[0] == "review" for event in factory.events)


@pytest.mark.parametrize("field,value", [("count", 1), ("workers", 1), ("max_consecutive_failures", 20),
    ("song_id", TEST_SONG_ID), ("proxy_test_session", True), ("proxy_sticky_pool", True),
    ("browser", "cloakbrowser"), ("headless", True), ("no_browser", True), ("review_rows", [1])])
def test_read_only_review_rejects_every_other_control(field, value):
    with pytest.raises(ui_jobs.JobValidationError):
        ui_jobs._validate({"action": "review-sessions", "rows": [1], field: value})


@pytest.mark.parametrize("rows", [[], list(range(1, 7))])
def test_read_only_review_requires_one_to_five_selected_rows(rows):
    with pytest.raises(ui_jobs.JobValidationError):
        ui_jobs._validate({"action": "review-sessions", "rows": rows})


@pytest.mark.parametrize("use_proxy", [False, True])
def test_review_action_checks_only_eligible_entries_with_selected_connection(tmp_path, use_proxy):
    selected_proxy = object()
    manager, factory = make_manager(tmp_path, proxy_loader=lambda _: selected_proxy)
    put_hold(factory, 1)
    manager.submit({"action": "review-sessions", "rows": [1], "proxy_egypt": use_proxy})
    job = finish(manager)
    assert job["status"] == "succeeded" and job["session_reviews_cleared"] == 1 and job["session_review_pending"] == 0
    assert factory.attempts == {} and [(event[1], event[2]) for event in factory.events if event[0] == "review"] == [
        (1, {"proxy": selected_proxy} if use_proxy else {})]
    assert job["results"][0]["cleared_rows"] == [1] and job["results"][0]["session_review_cleared"] is True


def test_already_checked_but_identity_blocked_alias_cannot_be_selected_for_review(tmp_path):
    manager, factory = make_manager(tmp_path)
    factory.identities[3] = factory.identities[1]
    put_hold(factory, 1)
    with factory(factory.path) as vault:
        vault._db.execute("DELETE FROM holds WHERE source_row=3")
        vault._db.commit()
    manager.submit({"action": "review-sessions", "rows": [3]})
    job = finish(manager)
    assert job["status"] == "failed" and job["error"]["code"] == "account_scope_invalid"
    assert not any(event[0] == "review" for event in factory.events)


def test_failed_review_read_keeps_hold_and_continues_other_selected_entries(tmp_path):
    manager, factory = make_manager(tmp_path)
    put_hold(factory, 1)
    put_hold(factory, 2)
    def review_handler(vault, row, options):
        if row == 1:
            raise RequestFailure("request_transport_failed", stage="negative_control", curl_code=28)
        factory.review_handler = None
        return vault.review_saved_session(row, **options)
    factory.review_handler = review_handler
    manager.submit({"action": "review-sessions", "rows": [1, 2]})
    job = finish(manager)
    assert job["status"] == "completed_with_pending"
    assert job["session_review_pending"] == job["session_reviews_cleared"] == 1 and factory.attempts == {}
    assert [event[1] for event in factory.events if event[0] == "review"].count(1) == 1
    with factory(factory.path) as vault:
        assert vault.session_review()["session_review_rows"] == [1]


@pytest.mark.parametrize("changes", [{"source_row": 2}, {"source_row": True}, {"authenticated": False},
    {"negative_control_passed": False}, {"server_account_identity_verified": False}, {"session_review_cleared": False},
    {"cleared_rows": []}, {"cleared_rows": [True]}, {"cleared_rows": [2]}])
def test_invalid_review_proof_remains_global_account_scope_hold(tmp_path, changes):
    manager, factory = make_manager(tmp_path)
    put_hold(factory, 1)
    factory.review_handler = lambda _vault, row, _options: {"source_row": row, "passed": True, "authenticated": True,
        "negative_control_passed": True, "server_account_identity_verified": True, "session_review_cleared": True,
        "cleared_rows": [row], **changes}
    manager.submit({"action": "review-sessions", "rows": [1]})
    job = finish(manager)
    assert job["status"] == "failed" and job["error"]["code"] == "account_scope_invalid"
    with factory(factory.path) as vault:
        assert vault.session_review()["held_rows"] == [1]


def test_session_review_state_separates_identity_blocked_aliases_and_strips_all_secrets():
    failure = unknown_renewal("play")["session_failure"]
    entry = {"source_row": 1, "state": "session_review_pending", "failure_code": failure["code"],
             "failed_stage": failure["stage"], "session_failure": {**failure, "sid": "private"},
             "held_at": STAMP, "job_id": "a" * 32, "email": "private@example.invalid", "cookies": "private"}
    result = ui_server.safe_session_review({"accounts": [entry, {**entry, "source_row": True}, {**entry, "source_row": 2,
        "held_at": "2026-10-04T10:00:00"}], "held_rows": [1, 3, True, "4"], "total": 1})
    assert result["session_review_rows"] == [1] and result["held_rows"] == [1, 3] and result["total"] == 1
    assert set(result["accounts"][0]) == {"source_row", "state", "failure_code", "failed_stage", "session_failure", "held_at", "job_id"}
    assert set(result["accounts"][0]["session_failure"]) == {"code", "stage", "failure_category", "curl_code"}
    assert "private" not in json.dumps(result) and "retryable" not in json.dumps(result)


@pytest.mark.parametrize("filter_report", [ui_jobs._public_report, ui_server.safe_report])
def test_session_review_public_report_fields_are_typed_and_private_fields_removed(filter_report):
    result = filter_report({"session_review_pending": 2, "session_reviews_cleared": 1, "session_review_cleared": True,
                           "outcome": "session_review_pending", "cleared_rows": [1, 3], "email": "private@example.invalid"})
    assert result["session_review_pending"] == 2 and result["session_reviews_cleared"] == 1
    assert result["session_review_cleared"] is True and result["cleared_rows"] == [1, 3]
    assert "email" not in result
    invalid = filter_report({"session_review_pending": True, "session_reviews_cleared": 1.0,
                             "session_review_cleared": "true"})
    assert all(invalid.get(name) is None for name in ("session_review_pending", "session_reviews_cleared", "session_review_cleared"))


def test_loopback_review_api_accepts_only_narrow_payload(console):
    status, _, data = request(console, "POST", "/api/jobs", body={"action": "review-sessions", "rows": [1, 2], "proxy_egypt": True})
    assert status == 202 and console.service.manager.calls[-1]["action"] == "review-sessions"
    status, _, _ = request(console, "POST", "/api/jobs", body={"action": "review-sessions", "rows": [1], "count": 1})
    assert status == 400


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("http_status", [408, 425, 429, 500, 502, 503, 504])
def test_typed_bootstrap_http_timeout_or_cooldown_is_held_without_wait_or_replay(tmp_path, monkeypatch, action, http_status):
    failure = RequestFailure("request_rate_limited" if http_status == 429 else "request_http_failed",
                             stage="identity", http_status=http_status, retry_after_seconds=86400, retry_safe=False)
    manager, factory = make_manager(tmp_path, handler=lambda kind, row, *_:
                                    unknown_renewal(kind, session_failure=failure.diagnostics) if row == 1 else success(kind))
    monkeypatch.setattr("anghami_session.provider_recovery.wait_for_provider", lambda *_args, **_kw: pytest.fail("Unknown bootstrap was waited and repeated"))
    manager.submit({"action": action, "rows": [1, 2], "count": 3})
    job = finish(manager)
    assert job["status"] == "completed_with_pending" and job["session_review_pending"] == 1
    assert factory.attempts == {1: 1, 2: 3} and job["provider_retries"] == 0


@pytest.mark.parametrize("source_row", [True, 2, "1"])
def test_fresh_exception_report_wrong_or_malformed_raw_scope_is_still_global(tmp_path, source_row):
    def handler(kind, row, *_):
        (tmp_path / f"account-{row}.test-play-record-report.json").write_text(
            json.dumps(unknown_renewal(kind, source_row=source_row)), encoding="utf-8")
        raise RequestFailure("request_transport_failed", stage="identity", curl_code=28, retry_safe=False)
    manager, factory = make_manager(tmp_path, handler=handler)
    manager.submit({"action": "play", "rows": [1, 2]})
    job = finish(manager)
    assert job["status"] == "failed" and job["error"]["code"] == "request_scope_invalid"
    assert job["session_review_pending"] == 0 and factory.attempts == {1: 1}


@pytest.mark.parametrize("workers", [1, 2])
def test_hold_result_journal_failure_stops_new_dispatch(tmp_path, monkeypatch, workers):
    manager, factory = make_manager(tmp_path, rows=range(1, 8), handler=lambda kind, *_: unknown_renewal(kind))
    original = ui_jobs._journal
    def journal(value, path):
        if value.get("outcome") == "session_review_pending" or any(
                report.get("outcome") == "session_review_pending" for report in value.get("results", [])):
            raise OSError("synthetic journal exception")
        return original(value, path)
    monkeypatch.setattr(ui_jobs, "_journal", journal)
    manager.submit({"action": "play", "rows": list(factory.rows), "workers": workers, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "failed" and job["error"]["code"] == "journal_failed"
    assert len(factory.attempts) <= workers and max(factory.attempts.values()) == 1


def test_review_identity_mismatch_remains_global_hold_and_does_not_review_next(tmp_path):
    manager, factory = make_manager(tmp_path)
    put_hold(factory, 1)
    put_hold(factory, 2)
    def handler(*_):
        raise RequestFailure("session_identity_mismatch", stage="identity")
    factory.review_handler = handler
    manager.submit({"action": "review-sessions", "rows": [1, 2]})
    job = finish(manager)
    assert job["status"] == "failed" and job["error"]["code"] == "session_identity_mismatch"
    assert [event[1] for event in factory.events if event[0] == "review"] == [1]
    with factory(factory.path) as vault:
        assert vault.session_review()["session_review_rows"] == [1, 2]


def test_service_state_exposes_session_holds_separately_without_unlocking_credentials(tmp_path, monkeypatch):
    failure = unknown_renewal("play")["session_failure"]
    class Vault:
        def __init__(self, _path): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def summary(self): return {"records": 3, "unique_accounts": 2, "sessions_saved": 3, "duplicate_rows": 1, "states": {}}
        def test_accounts(self): return {"test_rows": [1, 2, 3], "ready_rows": [2], "accounts": []}
        def failure_review(self): return {"accounts": [], "failed_rows": [], "total": 0}
        def session_review(self):
            return {"accounts": [{"source_row": 1, "state": "session_review_pending", "failure_code": failure["code"],
                "failed_stage": failure["stage"], "session_failure": {**failure, "sid": "private"},
                "held_at": STAMP, "job_id": "a" * 32, "email": "private@example.invalid"}],
                "total": 1, "session_review_rows": [1], "held_rows": [1, 3]}
        def record(self, row): pytest.fail("State exposed private account records")
    def absent(_path): raise SessionError("Synthetic configuration unavailable")
    monkeypatch.setattr(ui_server, "AccountVault", Vault)
    monkeypatch.setattr(ui_server, "load_packetstream_proxy", absent)
    monkeypatch.setattr(ui_server, "load_test_pool", absent)
    monkeypatch.setattr(ui_server.StickyProxyPool, "load", absent)
    result = ui_server.ConsoleService(tmp_path / "synthetic.sqlite3").state()
    assert result["session_review"]["held_rows"] == [1, 3] and result["session_review"]["session_review_rows"] == [1]
    assert result["failure_review"]["total"] == 0 and result["cohort"]["ready_rows"] == [2]
    assert "private" not in json.dumps(result)
