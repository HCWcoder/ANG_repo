"""Configurable failure streaks are tested only with synthetic offline runners."""

from concurrent.futures import ALL_COMPLETED, wait as real_wait
import threading

import pytest

from anghami_session import proxy, ui_jobs, ui_server
from test_ui_jobs_concurrency import manager_for, finish, operational_failure, success
from test_ui_server import console, request


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("threshold", [1, 20, 21, 25, 1000, 2**53 - 1])
def test_failure_threshold_accepts_positive_safe_integer_without_twenty_cap(action, threshold):
    options = ui_jobs._validate({"action": action, "rows": [1], "max_consecutive_failures": threshold})
    assert options["max_consecutive_failures"] == threshold and options["workers"] == 1


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("threshold", [None, True, False, 0, -1, 25.0, "25", [], {}, 2**53])
def test_invalid_failure_threshold_rejects_before_job_or_configuration(tmp_path, action, threshold):
    calls = []
    manager = ui_jobs.JobManager(tmp_path / "synthetic.sqlite3", vault_factory=lambda path: calls.append(path))
    with pytest.raises(ui_jobs.JobValidationError, match="positive JavaScript-safe integer"):
        manager.submit({"action": action, "rows": [1], "max_consecutive_failures": threshold})
    assert calls == [] and manager.snapshot() is None and manager._thread is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("threshold", [21, 25, 1000, 2**53 - 1])
def test_loopback_api_accepts_explicit_threshold_above_twenty(console, action, threshold):
    payload = {"action": action, "rows": [1], "max_consecutive_failures": threshold}
    status, _, _body = request(console, "POST", "/api/jobs", body=payload)
    assert status == 202 and console.service.manager.calls == [payload]


@pytest.mark.parametrize("action", ["play", "like"])
def test_api_default_remains_first_failure(action):
    assert ui_jobs._validate({"action": action, "rows": [1]})["max_consecutive_failures"] == 1


@pytest.mark.parametrize("action", ["play", "like"])
def test_threshold_twenty_five_reserves_active_budget_and_stops_at_twenty_five_of_thirty_five(tmp_path, action):
    barrier = threading.Barrier(8, timeout=5)
    def handler(kind, row, _number, _options):
        if row <= 8:
            barrier.wait()
        return operational_failure(kind)
    manager, factory = manager_for(tmp_path, rows=range(1, 36), handler=handler)
    manager.submit({"action": action, "rows": list(factory.rows), "workers": 8, "max_consecutive_failures": 25})
    job = finish(manager)
    assert job["max_consecutive_failures"] == job["consecutive_failures"] == 25
    assert job["failure_limit_scope"] == "active_budget" and factory.peak == 8
    assert job["status"] == "failed" and job["stop_reason"] == "consecutive_failure_limit"
    assert (job["attempted"], job["failed"], job["completed_tests"], job["skipped"]) == (25, 25, 25, 10)
    assert job["writes_attempted"] == job["writes_accepted"] == 0
    assert sum(factory.attempts.values()) == 25 and all(number == 1 for number in factory.attempts.values())


@pytest.mark.parametrize("action", ["play", "like"])
def test_threshold_twenty_five_stops_new_dispatch_and_drains_started_failures_when_workers_are_higher(tmp_path, monkeypatch, action):
    all_started, release_first, release_drain = (threading.Event() for _ in range(3))
    started, lock = set(), threading.Lock()
    def handler(kind, row, _number, _options):
        with lock:
            started.add(row)
            if len(started) == 30:
                all_started.set()
        assert (release_first if row <= 25 else release_drain).wait(5)
        return operational_failure(kind)
    manager, factory = manager_for(tmp_path, rows=range(1, 36), handler=handler)
    calls = []
    def grouped_wait(active, **_kwargs):
        ordered = list(active)
        calls.append(len(active))
        if len(calls) == 1:
            assert len(active) == 30 and all_started.wait(5)
            release_first.set()
            done, pending = real_wait(ordered[:25], timeout=5, return_when=ALL_COMPLETED)
            assert len(done) == 25 and not pending
            return done, set(ordered[25:])
        assert manager.snapshot()["stop_reason"] == "consecutive_failure_limit"
        release_drain.set()
        return real_wait(active, timeout=5, return_when=ALL_COMPLETED)
    monkeypatch.setattr(ui_jobs, "wait", grouped_wait)
    try:
        manager.submit({"action": action, "rows": list(factory.rows), "workers": 30, "max_consecutive_failures": 25})
        job = finish(manager)
    finally:
        release_first.set()
        release_drain.set()
    assert calls == [30, 5] and started == set(range(1, 31))
    assert job["failure_limit_scope"] == "new_dispatch" and job["consecutive_failures"] == 25
    assert job["stop_reason"] == "consecutive_failure_limit"
    assert (job["failed"], job["attempted"], job["completed_tests"], job["skipped"]) == (30, 30, 30, 5)
    assert job["active_workers"] == 0 and all(item.closed for item in factory.instances)


@pytest.mark.parametrize("action", ["play", "like"])
def test_success_resets_large_threshold_streak_even_when_total_failures_exceed_threshold(tmp_path, action):
    manager, factory = manager_for(tmp_path, rows=range(1, 50),
                                   handler=lambda kind, row, *_: success(kind) if row == 25 else operational_failure(kind))
    manager.submit({"action": action, "rows": list(factory.rows), "workers": 1, "max_consecutive_failures": 25})
    job = finish(manager)
    assert (job["failed"], job["succeeded"], job["attempted"], job["consecutive_failures"]) == (48, 1, 49, 24)
    assert job["status"] == "completed_with_failures" and job["stop_reason"] == "completed_with_failures"


@pytest.mark.parametrize("action", ["play", "like"])
def test_connection_pending_neither_counts_as_failure_nor_resets_streak(tmp_path, action):
    def handler(kind, row, _number, _options):
        if row == 25:
            raise proxy.ProxyCountryError("transport_error", curl_code=28, country_check_attempts=3)
        return operational_failure(kind)
    manager, factory = manager_for(tmp_path, rows=range(1, 36), handler=handler)
    manager.submit({"action": action, "rows": list(factory.rows), "workers": 1, "max_consecutive_failures": 25})
    job = finish(manager)
    assert (job["failed"], job["connection_pending"], job["attempted"], job["consecutive_failures"]) == (25, 1, 26, 25)
    assert job["stop_reason"] == "consecutive_failure_limit" and factory.attempts[25] == 3
    assert max(factory.attempts) == 26 and job["account_failed"] == 0


@pytest.mark.parametrize("action", ["play", "like"])
def test_unknown_session_renewal_still_stops_immediately_at_maximum_numeric_threshold(tmp_path, action):
    def handler(kind, row, *_args):
        report = operational_failure(kind)
        report.update(error_code="request_transport_failed", failed_phase="account_identity",
                      renewal_attempted=True, renewal_completed=False,
                      session_failure={"code": "request_transport_failed", "stage": "identity",
                                       "curl_code": 7, "retryable": False})
        return report
    manager, factory = manager_for(tmp_path, rows=range(1, 36), handler=handler)
    manager.submit({"action": action, "rows": list(factory.rows), "workers": 1,
                    "max_consecutive_failures": 2**53 - 1})
    job = finish(manager)
    assert job["error"]["code"] == "session_renewal_unknown" and job["stop_reason"] == "result_unknown"
    assert job["attempted"] == job["failed"] == 1 and job["skipped"] == 34
    assert factory.attempts == {1: 1} and job["writes_attempted"] == 0 and job["account_failed"] == 0


@pytest.mark.parametrize("key", ["max_consecutive_failures", "consecutive_failures"])
@pytest.mark.parametrize("bad", [True, False, -1, 1.0, "25", [], {}, None, 2**53])
def test_failure_metadata_is_a_safe_integer_only(key, bad):
    assert ui_jobs._public_report({key: bad}) == {}
    assert ui_server.safe_report({key: bad}) == {key: None}


def test_public_failure_metadata_exposes_large_threshold_and_zero_streak():
    value = {"max_consecutive_failures": 2**53 - 1, "consecutive_failures": 0}
    assert ui_jobs._public_report(value) == value and ui_server.safe_report(value) == value
