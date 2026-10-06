"""Provider recovery uses synthetic account-bound transports and never the network."""

import json
import threading

import pytest

from anghami_session import play_record, provider_recovery, proxy, proxy_pool, ui_jobs
from anghami_session.errors import RequestFailure, safe_request_failure
from test_ui_jobs_concurrency import ConcurrentVault, ConcurrentVaultFactory, finish, success


@pytest.fixture(autouse=True)
def no_wait_or_network(monkeypatch):
    waits = []
    monkeypatch.setattr(provider_recovery, "wait_for_provider", lambda failure, stop=None: waits.append(failure) or not (stop and stop.is_set()))
    return waits


def make_manager(tmp_path, handler, *, rows=range(1, 17), pool_size=4):
    pool = proxy_pool.StickyProxyPool(tuple(proxy.PacketStreamProxy.from_route(
        "synthetic-retry-user", "synthetic-retry-key", f"syntheticroute{index}", "http://proxy.packetstream.io:31112")
        for index in range(pool_size)))
    factory = ConcurrentVaultFactory(tmp_path / "synthetic.sqlite3", rows, handler)
    manager = ui_jobs.JobManager(factory.path, vault_factory=factory,
        test_proxy_loader=lambda path: pool,
        proxy_loader=lambda path: pytest.fail("Provider retry switched proxy modes"))
    return manager, factory, pool


def run(manager, factory, action="play", *, count=1, workers=8, maximum=20):
    manager.submit({"action": action, "rows": list(factory.rows), "proxy_test_session": True,
                    "count": count, "workers": workers, "max_consecutive_failures": maximum})
    return finish(manager)


@pytest.mark.parametrize("action", ["play", "like"])
def test_eight_workers_retry_same_account_on_next_route_then_keep_it_for_repeats_and_aliases(tmp_path, action):
    ready, release, guard = threading.Event(), threading.Event(), threading.Lock()
    entered = []

    def handler(kind, row, number, options):
        if number == 1 and row <= 8:
            with guard:
                entered.append(row)
                if len(entered) == 8:
                    ready.set()
            assert release.wait(5)
        if number == 1 and row != 17:
            raise proxy.ProxyCountryError("transport_error", curl_code=56)
        return success(kind)

    manager, factory, pool = make_manager(tmp_path, handler, rows=range(1, 18))
    factory.identities[17] = factory.identities[1].upper()
    manager.submit({"action": action, "rows": list(factory.rows), "proxy_test_session": True,
                    "count": 3, "workers": 8, "max_consecutive_failures": 20})
    try:
        assert ready.wait(5)
        assert manager.snapshot()["active_workers"] == 8
    finally:
        release.set()
    job = finish(manager)
    assert job["status"] == "succeeded" and job["succeeded"] == 51
    assert job["failed"] == job["connection_pending"] == job["account_failed"] == 0
    assert job["retried"] == job["provider_retries"] == 16
    for row in range(1, 17):
        calls = [event for event in factory.events if event[0] == "attempt" and event[2] == row]
        start = (row - 1) % len(pool)
        assert calls[0][5]["proxy"] is pool.proxy_for_index(start)
        assert all(event[5]["proxy"] is pool.proxy_for_index(start + 1) for event in calls[1:])
        assert len(calls) == 4
    alias_calls = [event for event in factory.events if event[0] == "attempt" and event[2] == 17]
    assert all(event[5]["proxy"] is pool.proxy_for_index(1) for event in alias_calls)
    assert factory.peak == 8
    assert all(report["provider_attempts"] in {1, 2} for report in job["results"])
    text = json.dumps(job)
    assert "synthetic-retry-key" not in text and "syntheticroute" not in text


@pytest.mark.parametrize("action", ["play", "like"])
def test_eighty_provider_faults_exhaust_three_routes_without_failure_stop_or_account_failure(tmp_path, action):
    def handler(*args):
        raise proxy.ProxyCountryError("transport_error", curl_code=28)
    manager, factory, pool = make_manager(tmp_path, handler, rows=range(1, 81))
    job = run(manager, factory, action)
    assert job["status"] == "completed_with_pending"
    assert (job["attempted"], job["completed_tests"], job["connection_pending"], job["failed"], job["skipped"]) == (80, 80, 80, 0, 0)
    assert job["consecutive_failures"] == job["account_failed"] == 0 and job["stop_reason"] is None
    assert job["retried"] == 80 and job["provider_retries"] == 160
    assert factory.attempts == dict.fromkeys(factory.rows, 3)
    assert all(report["outcome"] == "connection_pending" and len(report["attempt_history"]) == 3 for report in job["results"])
    assert all(report["result_unknown"] is False for report in job["results"])


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("recover", [False, True])
def test_rate_limit_waits_and_retries_same_route_without_ip_rotation(tmp_path, no_wait_or_network, action, recover):
    def handler(kind, row, number, options):
        if not recover or number == 1:
            raise proxy.ProxyCountryError("http_failure", http_status=429, proxy_connect_http_status=200, retry_after_seconds=30)
        return success(kind)
    manager, factory, pool = make_manager(tmp_path, handler, rows=range(1, 9))
    job = run(manager, factory, action)
    assert job["status"] == ("succeeded" if recover else "completed_with_pending")
    assert job["failed"] == job["consecutive_failures"] == 0
    assert len(no_wait_or_network) == 8 * (1 if recover else 2)
    assert all(failure["http_status"] == 429 and failure["rotate_route"] is False for failure in no_wait_or_network)
    for row in factory.rows:
        calls = [event for event in factory.events if event[0] == "attempt" and event[2] == row]
        assert all(event[5]["proxy"] is pool.proxy_for_index(row - 1) for event in calls)
    assert job["proxy_route_assignments"] == [{"source_row": row, "proxy_route_number": (row - 1) % len(pool) + 1} for row in factory.rows]


@pytest.mark.parametrize("action", ["play", "like"])
def test_long_provider_cooldown_defers_without_retry_or_rotation(tmp_path, monkeypatch, action):
    monkeypatch.setattr(provider_recovery, "wait_for_provider", lambda failure, stop=None: False)
    manager, factory, pool = make_manager(tmp_path, lambda *args: (_ for _ in ()).throw(
        RequestFailure("request_rate_limited", stage="relations", http_status=429, retry_after_seconds=3600)), rows=[1, 2])
    job = run(manager, factory, action)
    assert job["status"] == "completed_with_pending" and factory.attempts == {1: 1, 2: 1}
    assert job["failed"] == job["provider_retries"] == 0 and job["connection_pending"] == 2


@pytest.mark.parametrize("action", ["play", "like"])
def test_confirmed_account_rejection_is_reviewed_and_excluded_from_future_repetitions_and_aliases(tmp_path, monkeypatch, action):
    reviewed = []
    def mark(vault, row, failure):
        reviewed.append((row, failure))
    monkeypatch.setattr(ConcurrentVault, "record_account_failure", mark, raising=False)
    def handler(kind, row, number, options):
        if row == 1:
            raise RequestFailure("session_authentication_rejected", stage="relations")
        return success(kind)
    manager, factory, pool = make_manager(tmp_path, handler, rows=range(1, 10))
    factory.identities[9] = factory.identities[1]
    job = run(manager, factory, action, count=3)
    assert job["status"] == "completed_with_failures" and job["account_failed"] == job["failed"] == 1
    assert job["succeeded"] == 21 and job["skipped"] == 5
    assert factory.attempts[1] == 1 and 9 not in factory.attempts
    assert len(reviewed) == 1 and reviewed[0][0] == 1 and reviewed[0][1]["failure_category"] == "account"
    assert job["provider_retries"] == job["connection_pending"] == 0


@pytest.mark.parametrize("action", ["play", "like"])
def test_server_identity_mismatch_is_reviewed_and_holds_immediately(tmp_path, monkeypatch, action):
    reviewed = []
    monkeypatch.setattr(ConcurrentVault, "record_account_failure", lambda vault, row, failure: reviewed.append(row), raising=False)
    manager, factory, pool = make_manager(tmp_path, lambda *args: (_ for _ in ()).throw(
        RequestFailure("session_identity_mismatch", stage="identity")), rows=[1, 2])
    job = run(manager, factory, action, workers=1)
    assert job["status"] == "failed" and reviewed == [1] and factory.attempts == {1: 1}
    assert job["account_failed"] == 1 and job["connection_pending"] == job["provider_retries"] == 0


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("boundary", ["engagement", "renewal_unknown", "renewal_complete"])
def test_provider_fault_never_replays_an_attempted_engagement_or_session_renewal(tmp_path, action, boundary):
    manager, factory, pool = make_manager(tmp_path, lambda *args: None, rows=[1, 2])
    def handler(kind, row, number, options):
        if row == 1:
            report = {"passed": False, "event_attempted": kind == "play" and boundary == "engagement",
                "event_result": "unknown" if kind == "play" and boundary == "engagement" else "not_attempted",
                "mutation_attempted": kind == "like" and boundary == "engagement",
                "mutation_result": "unknown" if kind == "like" and boundary == "engagement" else "not_attempted",
                "renewal_attempted": boundary.startswith("renewal"), "renewal_completed": boundary == "renewal_complete", "session_failure": safe_request_failure(
                    RequestFailure("request_transport_failed", stage="relations"))}
            name = "test-play-record" if kind == "play" else "test-like"
            play_record._journal(report, factory.path.parent / f"account-{row}.{name}-report.json")
            raise RequestFailure("request_transport_failed", stage="relations")
        return success(kind)
    factory.handler = handler
    job = run(manager, factory, action, workers=1)
    assert factory.attempts[1] == 1 and job["provider_retries"] == 0
    if boundary in {"engagement", "renewal_unknown"}:
        assert job["status"] == "failed" and factory.attempts == {1: 1}
        assert job["results"][0]["result_unknown"] is True
        if boundary == "renewal_unknown":
            assert job["error"]["code"] == "session_renewal_unknown" and job["results"][0]["renewal_unknown"] is True
    else:
        assert job["status"] == "completed_with_pending" and factory.attempts == {1: 1, 2: 1}
        assert job["connection_pending"] == 1 and job["failed"] == 0


def test_route_retry_journal_failure_holds_before_second_transport(tmp_path, monkeypatch):
    manager, factory, pool = make_manager(tmp_path, lambda *args: (_ for _ in ()).throw(proxy.ProxyCountryError("transport_error", curl_code=56)), rows=[1, 2])
    original = ui_jobs._journal
    def journal(value, path):
        if path == manager._report_path and value.get("proxy_route_assignments") == [
            {"source_row": 1, "proxy_route_number": 2}, {"source_row": 2, "proxy_route_number": 2}]:
            raise OSError("synthetic report write unavailable")
        return original(value, path)
    monkeypatch.setattr(ui_jobs, "_journal", journal)
    job = run(manager, factory, workers=1)
    assert job["status"] == "failed" and job["error"]["code"] == "journal_failed"
    assert factory.attempts == {1: 1} and job["provider_retries"] == 0


def test_changed_account_before_retry_is_held_and_never_rebound_to_other_identity(tmp_path):
    manager, factory, pool = make_manager(tmp_path, lambda *args: None, rows=[1, 2])
    def handler(kind, row, number, options):
        factory.identities[row] = "synthetic-changed@example.invalid"
        raise proxy.ProxyCountryError("transport_error", curl_code=56)
    factory.handler = handler
    job = run(manager, factory, workers=1)
    assert job["status"] == "failed" and job["error"]["code"] == "account_scope_invalid"
    assert factory.attempts == {1: 1}


def test_stop_arriving_between_provider_attempts_preserves_first_attempt_and_used_route(tmp_path, monkeypatch):
    def wait_then_hold(failure, stop=None):
        assert stop is not None
        stop.set()
        return True
    monkeypatch.setattr(provider_recovery, "wait_for_provider", wait_then_hold)
    manager, factory, pool = make_manager(tmp_path, lambda *args: (_ for _ in ()).throw(
        proxy.ProxyCountryError("transport_error", curl_code=56)), rows=[1, 2])
    job = run(manager, factory, workers=1)
    assert (job["attempted"], job["completed_tests"], job["connection_pending"], job["failed"], job["skipped"]) == (1, 1, 1, 0, 1)
    assert factory.attempts == {1: 1} and job["provider_retries"] == 0
    report = job["results"][0]
    assert report["proxy_route_number"] == 1 and report["provider_attempts"] == 1
    assert report["attempt_history"][0]["proxy_failure"]["curl_code"] == 56
    assert report["outcome"] == "connection_pending" and report["result_unknown"] is False


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("pool_size", [1, 2, 3, 4])
def test_transport_recovery_uses_each_distinct_pool_route_once_with_three_attempt_ceiling(tmp_path, action, pool_size):
    manager, factory, pool = make_manager(tmp_path, lambda *args: (_ for _ in ()).throw(
        proxy.ProxyCountryError("transport_error", curl_code=56)), rows=[1], pool_size=pool_size)
    job = run(manager, factory, action)
    attempts = min(pool_size, 3)
    assert job["status"] == "completed_with_pending" and factory.attempts == {1: attempts}
    calls = [event for event in factory.events if event[0] == "attempt"]
    assert [event[5]["proxy"] for event in calls] == [pool.proxy_for_index(index) for index in range(attempts)]
    assert job["failed"] == job["consecutive_failures"] == 0 and job["connection_pending"] == 1
    report = job["results"][0]
    assert report["provider_attempts"] == attempts and report["retry_count"] == attempts - 1
    assert [entry["proxy_route_number"] for entry in report["attempt_history"]] == list(range(1, attempts + 1))


@pytest.mark.parametrize("category", ["provider", "account"])
def test_local_result_scope_guard_wins_over_report_recovery_diagnostics(tmp_path, monkeypatch, category):
    monkeypatch.setattr(ConcurrentVault, "record_account_failure", lambda *args: pytest.fail("Unbound result entered account review"), raising=False)
    diagnostic = safe_request_failure(RequestFailure(
        "request_transport_failed" if category == "provider" else "session_authentication_rejected", stage="relations"))
    def handler(kind, row, number, options):
        return {"passed": False, "song_id": str(int(play_record.TEST_SONG_ID) + 1),
                "renewal_attempted": True, "renewal_completed": False,
                "session_failure": diagnostic, "event_attempted": False, "event_result": "not_attempted"}
    manager, factory, pool = make_manager(tmp_path, handler, rows=[1, 2])
    job = run(manager, factory, workers=1)
    assert job["status"] == "failed" and job["error"]["code"] == "request_scope_invalid"
    assert factory.attempts == {1: 1}
    assert job["provider_retries"] == job["connection_pending"] == job["account_failed"] == 0


def test_local_journal_guard_wins_over_fresh_provider_failure_report(tmp_path):
    manager, factory, pool = make_manager(tmp_path, lambda *args: None, rows=[1, 2])
    def handler(kind, row, number, options):
        report = {"passed": False, "event_attempted": False, "event_result": "not_attempted",
                  "renewal_attempted": True, "renewal_completed": False,
                  "error_code": "request_transport_failed", "session_failure": safe_request_failure(
                      RequestFailure("request_transport_failed", stage="relations"))}
        play_record._journal(report, factory.path.parent / f"account-{row}.test-play-record-report.json")
        raise play_record._Failure("journal_failed", "The synthetic retry journal could not be saved.")
    factory.handler = handler
    job = run(manager, factory, workers=1)
    assert job["status"] == "failed" and job["error"]["code"] == "journal_failed"
    assert factory.attempts == {1: 1}
    assert job["provider_retries"] == job["connection_pending"] == job["account_failed"] == 0


def test_public_proxy_cooldown_refusal_keeps_nonretryable_fact():
    error = proxy.ProxyCountryError("http_failure", http_status=429, retry_after_seconds=121, retry_safe=False)
    safe = ui_jobs._public_proxy_failure(proxy.safe_proxy_country_failure(error))
    assert safe["retryable"] is False
    recovered = provider_recovery.provider_failure({"proxy_failure": safe})
    assert recovered["retryable"] is False and recovered["rotate_route"] is False
