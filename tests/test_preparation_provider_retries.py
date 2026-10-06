"""Provider recovery and account review are validated entirely offline."""

from copy import deepcopy
import json
import threading

import pytest

from anghami_session import country_preparation as country, preparation, proxy, proxy_pool
from anghami_session.errors import LoginCaptureError, RequestFailure, safe_request_failure
from anghami_session.play_record import _Failure
from test_account_preparation import PreparationVault, SAVED, fake_recovery
from test_country_preparation_workers import imported, install_recovery, states, SyntheticVault
from test_country_preparation_sticky_pool import imported as sticky_imported
from test_session_recovery import record, transport, Reply


def install_routes(monkeypatch, factory, verifier=None, *, count=3):
    pool = proxy_pool.StickyProxyPool(tuple(proxy.PacketStreamProxy.from_route(
        "syntheticUser", "syntheticPassword", f"SyntheticRoute{number}",
        "http://proxy.packetstream.io:31112",
    ) for number in range(count)))
    selected = []
    original = proxy_pool.StickyProxyPool.proxy_for_index

    def choose(self, index):
        selected.append(index)
        return original(self, index)

    def verify(self):
        factory.log("verify", self)
        if verifier is not None:
            verifier(self)
        return {"country": "EG", "country_verified": True, "proxy_used": True}

    monkeypatch.setattr(country.StickyProxyPool, "load", lambda _: pool)
    monkeypatch.setattr(proxy_pool.StickyProxyPool, "proxy_for_index", choose)
    monkeypatch.setattr(proxy.PacketStreamProxy, "verify_country", verify)
    return pool, selected


def country_run(parent, plan, path, **options):
    return country.run_plan(parent, plan, path, no_browser=True,
                            proxy_sticky_pool=path.parent / "synthetic-pool.dpapi",
                            max_consecutive_failures=20, **options)


def test_prewrite_country_failure_rotates_same_account_then_attaches_on_healthy_route(imported, tmp_path, monkeypatch):
    parent, plan, _, factory = imported
    first = True

    def verify(_profile):
        nonlocal first
        if first:
            first = False
            raise proxy.ProxyCountryError("transport_error", curl_code=56)

    pool, selected = install_routes(monkeypatch, factory, verify)
    install_recovery(monkeypatch, factory)
    result = country_run(parent, plan, tmp_path / "progress.json", workers=1, limit=1)
    assert selected == [0, 1]
    assert result["counts"]["ready"] == 1 and result["counts"]["failed"] == 0
    assert result["consecutive_failures"] == 0
    recovery = [event for event in factory.events if event[0] == "recover"]
    attached = [event for event in factory.events if event[0] == "attach"]
    assert len(recovery) == len(attached) == 1
    assert recovery[0][1] == attached[0][1] == 1
    assert recovery[0][2] is attached[0][2] is pool._proxies[1]


def test_eight_workers_exhaust_three_routes_without_quarantining_accounts_and_resume(imported, tmp_path, monkeypatch):
    parent, plan, _, factory = imported
    guard = threading.Lock()
    entered = set()
    first_batch = threading.Barrier(8, timeout=5)

    def fail(_profile):
        owner = threading.get_ident()
        with guard:
            first = owner not in entered
            entered.add(owner)
        if first:
            first_batch.wait()
        raise proxy.ProxyCountryError("transport_error", curl_code=28)

    pool, selected = install_routes(monkeypatch, factory, fail)
    install_recovery(monkeypatch, factory)
    path = tmp_path / "progress.json"
    first = country_run(parent, plan, path, workers=8)
    assert first["status"] == "completed_with_pending" and first["pause_reason"] is None
    assert first["counts"]["connection_pending"] == 8
    assert first["counts"]["failed"] == first["consecutive_failures"] == 0
    assert len(selected) == 24
    assert all(item["provider_retry_count"] == 2 for item in country.load_progress(path, plan)["rows"])
    assert not any(event[0] in {"record", "recover", "attach", "enroll"} for event in factory.events)
    assert all(item["state"] == "connection_pending" for item in states(path, plan).values())
    before = len(selected)
    before_verifications = sum(event[0] == "verify" for event in factory.events)

    def healthy(profile):
        factory.log("verify", profile)
        return {"country": "EG", "country_verified": True, "proxy_used": True}

    monkeypatch.setattr(proxy.PacketStreamProxy, "verify_country", healthy)
    second = country_run(parent, plan, path, workers=8)
    assert second["status"] == "completed" and second["counts"]["ready"] == 8
    assert list(dict.fromkeys(selected[before:])) == list(range(8, 16))
    assert sum(event[0] == "verify" for event in factory.events) - before_verifications == 8
    assert country.load_progress(path, plan)["proxy_pool_cursor"] == 16
    assert sorted(event[1] for event in factory.events if event[0] == "recover") == list(range(1, 9))
    recovered = [event for event in factory.events if event[0] == "recover"]
    assert all(event[2] is pool._proxies[(event[1] + 7) % len(pool)] for event in recovered)


def test_country_429_waits_and_retries_exact_route_without_rotation(imported, tmp_path, monkeypatch):
    parent, plan, _, factory = imported
    calls, waits = [], []

    def verify(profile):
        calls.append(profile)
        if len(calls) == 1:
            raise proxy.ProxyCountryError("http_failure", http_status=429, retry_after_seconds=8)

    _, selected = install_routes(monkeypatch, factory, verify)
    monkeypatch.setattr(country, "wait_for_provider", lambda diagnostic, stop=None: waits.append(deepcopy(diagnostic)) or True)
    install_recovery(monkeypatch, factory)
    result = country_run(parent, plan, tmp_path / "progress.json", workers=1, limit=1)
    assert result["counts"]["ready"] == 1 and result["counts"]["failed"] == 0
    assert selected == [0] and len(calls) == 2 and calls[0] is calls[1]
    assert len(waits) == 1 and waits[0]["http_status"] == 429
    assert waits[0]["rotate_route"] is False and waits[0]["retry_after_seconds"] == 8


def test_prewrite_session_provider_failure_rotates_but_never_retries_renewal_unknown(imported, tmp_path, monkeypatch):
    parent, plan, _, factory = imported
    pool, selected = install_routes(monkeypatch, factory)
    attempted = []

    def recover(row, profile):
        attempted.append((row, profile))
        if row == 1 and len(attempted) == 1:
            raise RequestFailure("request_transport_failed", stage="session_recovery_preflight", curl_code=28)
        if row == 2:
            error = RequestFailure("request_transport_failed", stage="session_recovery_renewal", curl_code=28, retry_safe=False)
            error.renewal_unknown = True
            raise error

    install_recovery(monkeypatch, factory, recover)
    path = tmp_path / "progress.json"
    result = country_run(parent, plan, path, workers=1, limit=3)
    assert result["status"] == "attention_required" and result["pause_reason"] == "unknown_attempt"
    assert result["counts"]["ready"] == result["counts"]["unknown"] == 1
    assert result["counts"]["failed"] == result["counts"]["connection_pending"] == 0
    assert selected == [0, 1, 1]
    assert [row for row, _ in attempted] == [1, 1, 2]
    assert attempted[0][1] is pool._proxies[0] and attempted[1][1] is pool._proxies[1]
    before = list(factory.events)
    held = country_run(parent, plan, path, workers=1)
    assert held["pause_reason"] == "unknown_attempt" and factory.events == before


def test_prewrite_journal_failure_still_holds_without_any_account_requests(imported, tmp_path, monkeypatch):
    parent, plan, _, factory = imported
    install_routes(monkeypatch, factory)
    install_recovery(monkeypatch, factory)
    monkeypatch.setattr(preparation, "_journal", lambda *_: (_ for _ in ()).throw(_Failure("journal_failed", "The local report could not be saved.")))
    result = country_run(parent, plan, tmp_path / "progress.json", workers=1)
    assert result["status"] == "attention_required" and result["pause_reason"] == "unknown_attempt"
    assert result["counts"]["unknown"] == 1 and result["counts"]["pending"] == 7
    assert not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)


@pytest.mark.parametrize("index", [0, 3])
def test_recovery_transport_failure_before_renewal_is_retryable_without_any_post(record, transport, index):
    from anghami_session import session_recovery
    replies, instances, calls = transport
    replies[index] = OSError("synthetic secret transport")
    with pytest.raises(RequestFailure) as caught:
        session_recovery.recover_legacy_session(record)
    failure = safe_request_failure(caught.value)
    assert failure["failure_category"] == "provider" and failure["retryable"] is True
    assert getattr(caught.value, "renewal_unknown", False) is False
    assert not any(method == "POST" for _, method, _, _ in calls)
    assert all(instance.closed for instance in instances)


@pytest.mark.parametrize("index", [4, 5, 7])
def test_recovery_transport_failure_during_or_after_renewal_is_never_retryable(record, transport, index):
    from anghami_session import session_recovery
    replies, instances, calls = transport
    replies[index] = OSError("synthetic secret transport")
    with pytest.raises(RequestFailure) as caught:
        session_recovery.recover_legacy_session(record)
    assert safe_request_failure(caught.value)["retryable"] is False
    assert caught.value.renewal_unknown is True
    assert sum(method == "POST" for _, method, _, _ in calls) == 1
    assert len(calls) == index + 1 and all(instance.closed for instance in instances)


def test_preparation_provider_exhaustion_continues_next_account_and_keeps_review_empty(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "accounts.sqlite3", sessions={8: SAVED, 9: SAVED})
    routes = [object(), object(), object(), object()]
    selected, attempts = [], []

    def choose(row):
        selected.append(row)
        return routes[len(selected) - 1]

    class Route:
        def summary(self):
            return {"country": "EG"}

    routes[:] = [Route() for _ in routes]
    original = vault.attach

    def attach(row, saved, **options):
        attempts.append((row, options["proxy"]))
        if row == 8:
            raise RequestFailure("request_transport_failed", stage="preflight", curl_code=56)
        return original(row, saved, **options)

    vault.attach = attach
    result = preparation.prepare_test_accounts(vault, count=2, no_browser=True, proxy_factory=choose)
    assert selected == [8, 8, 8, 9]
    assert result["connection_pending_rows"] == [8] and result["prepared_rows"] == [9]
    assert result["account_failed_rows"] == [] and result["phase"] == "complete"
    assert len({id(profile) for row, profile in attempts if row == 8}) == 3
    assert vault.failure_review()["failed_rows"] == []
    assert ("enable", 8) not in vault.events and ("enable", 9) in vault.events


def test_confirmed_auth_rejection_enters_review_and_explicit_retry_recovers_fresh_session(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "accounts.sqlite3", sessions={8: SAVED, 9: SAVED},
                             failure=(8, RequestFailure("session_authentication_rejected", stage="relations")))
    first = preparation.prepare_test_accounts(vault, count=2, no_browser=True)
    assert first["account_failed_rows"] == [8] and first["prepared_rows"] == [9]
    assert first["connection_pending_rows"] == []
    assert vault.failure_review()["failed_rows"] == [8]
    vault.failure = None
    fake_recovery(monkeypatch, vault)
    before = len(vault.events)
    second = preparation.prepare_test_accounts(vault, count=1, selected_rows=[8], no_browser=True)
    assert second["passed"] is True and second["prepared_rows"] == [8]
    assert not any(event[0] == "session" for event in vault.events[before:])
    assert any(event[0] == "recover" for event in vault.events[before:])


def test_identity_mismatch_is_reviewed_and_keeps_immediate_scope_hold(tmp_path):
    vault = PreparationVault(tmp_path / "accounts.sqlite3", sessions={8: SAVED, 9: SAVED},
                             failure=(8, RequestFailure("session_identity_mismatch", stage="identity")))
    with pytest.raises(RequestFailure):
        preparation.prepare_test_accounts(vault, count=2, no_browser=True)
    assert vault.failure_review()["failed_rows"] == [8]
    assert not any(event[0] == "session" and event[1] == 9 for event in vault.events)


def test_explicit_review_selection_rejects_unknown_rows_or_duplicate_account_aliases(tmp_path):
    vault = PreparationVault(tmp_path / "accounts.sqlite3")
    with pytest.raises(Exception, match="currently listed"):
        preparation.prepare_test_accounts(vault, count=1, selected_rows=[8], no_browser=True)
    failure = safe_request_failure(RequestFailure("session_authentication_rejected"))
    vault.record_account_failure(8, failure)
    vault.record_account_failure(9, failure)
    with pytest.raises(Exception, match="one reviewed source row"):
        preparation.prepare_test_accounts(vault, count=2, selected_rows=[8, 9], no_browser=True)
    assert not any(event[0] in {"session", "attach", "enable"} for event in vault.events)


def test_twenty_confirmed_account_failures_are_reviewed_and_latch_without_provider_retries(sticky_imported, tmp_path, monkeypatch):
    parent, plan, _, factory = sticky_imported
    pool, selected = install_routes(monkeypatch, factory)

    def reject(_row, _profile):
        raise RequestFailure("session_authentication_rejected", stage="session_recovery_preflight")

    def review(self, row, failure):
        assert failure["failure_category"] == "account"
        factory.log("review", row)

    monkeypatch.setattr(SyntheticVault, "record_account_failure", review, raising=False)
    install_recovery(monkeypatch, factory, reject)
    path = tmp_path / "progress.json"
    result = country_run(parent, plan, path, workers=8)
    assert result["pause_reason"] == result["failure_hold"] == "repeated_failures"
    assert result["counts"]["failed"] == result["consecutive_failures"] == 20
    assert result["counts"]["connection_pending"] == 0
    # An intent rolled back after a peer finishes can look up the same cached
    # profile again. Those local tuple lookups are not network attempts.
    assert list(dict.fromkeys(selected)) == list(range(20))
    stored = country.load_progress(path, plan)
    assert stored["proxy_pool_cursor"] == result["proxy_pool"]["next_ordinal"] == 20
    verified = [event for event in factory.events if event[0] == "verify"]
    recovered = [event for event in factory.events if event[0] == "recover"]
    assert len(verified) == len(recovered) == 20
    assert sorted(event[1] for event in recovered) == list(range(1, 21))
    assert all(event[2] is pool._proxies[(event[1] - 1) % len(pool)] for event in recovered)
    assert all(row.get("provider_retry_count", 0) == 0 for row in stored["rows"])
    assert sorted(event[1] for event in factory.events if event[0] == "review") == list(range(1, 21))
    assert not any(event[0] in {"attach", "enroll"} for event in factory.events)
    assert all(states(path, plan)[row]["state"] == "pending" for row in range(21, 31))


def test_completed_renewal_then_validation_network_failure_never_replays_recovery(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    vault = PreparationVault(tmp_path / "accounts.sqlite3", failure=(8, RequestFailure("request_transport_failed", stage="relations")))
    selected, recovered = [], []

    class Route:
        def summary(self):
            return {"country": "EG"}

    def recover(record, *, proxy):
        recovered.append(proxy)
        return SAVED, {"session_renewed": True, "identity_verified": True}

    def choose(row):
        selected.append(row)
        return Route()

    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=recover))
    result = preparation.prepare_test_accounts(vault, count=2, no_browser=True, proxy_factory=choose)
    assert selected == [8, 9] and len(recovered) == 2
    assert vault.pending_candidates == {8: SAVED}
    report = json.loads((tmp_path / "accounts-prepare-tests-report.json").read_text())
    assert report.get("renewal_unknown") is not True and report["phase"] == "complete"
    assert report["connection_pending_rows"] == [8] and report["prepared_rows"] == [9]
    assert result["candidate_retained"] is True
    assert sum(event[0] == "attach" and event[1] == 8 for event in vault.events) == 1


@pytest.mark.parametrize("payload,category", [
    ({"status": "failed"}, "account"),
    ({"status": "ok", "email": "other@example.invalid"}, "account"),
    ({"status": "ok"}, "unknown"),
    ({"status": "ok", "email": ""}, "unknown"),
    ({"status": "error", "error": "synthetic"}, "unknown"),
])
def test_profile_only_known_authentication_or_wrong_identity_flags_the_account(record, transport, payload, category):
    from anghami_session import session_recovery
    replies, instances, calls = transport
    replies[3] = Reply(payload)
    with pytest.raises(RequestFailure) as caught:
        session_recovery.recover_legacy_session(record)
    assert safe_request_failure(caught.value)["failure_category"] == category
    assert len(calls) == 4 and not any(method == "POST" for _, method, _, _ in calls)
    assert all(instance.closed for instance in instances)


@pytest.mark.parametrize("mixed_hold", [False, True])
def test_legacy_provider_failures_migrate_to_pending_and_only_provider_hold_is_released(sticky_imported, tmp_path, monkeypatch, mixed_hold):
    parent, plan, _, factory = sticky_imported
    pool, selected = install_routes(monkeypatch, factory)
    install_recovery(monkeypatch, factory)
    progress = country._new_progress(plan)
    progress.update(status="paused", pause_reason="repeated_failures", failure_hold="repeated_failures",
                    no_browser=True, max_consecutive_failures=20, consecutive_failures=20,
                    connection="proxy_egypt", proxy_pool_active=True, proxy_pool_count=len(pool),
                    proxy_pool_fingerprint=pool.fingerprint(), proxy_pool_cursor=20,
                    proxy_pool_endpoint=pool.summary()["endpoint"])
    for item in progress["rows"][:20]:
        item.update(state="failed", attempts=1, phase="stopped", error_code="proxy_preflight_failed",
                    connection="proxy_egypt")
    if mixed_hold:
        progress["rows"][0]["error_code"] = "account_failed"
    progress["proxy_failure"] = {"source_row": 20, "stage": "country_check", "code": "country_check_failed",
                                 "failure_kind": "transport_error", "curl_code": 28, "country_check_attempts": 3}
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    result = country_run(parent, plan, path, workers=1, limit=2)
    stored = country.load_progress(path, plan)
    assert stored["proxy_failure"] == progress["proxy_failure"]
    if mixed_hold:
        assert result["failure_hold"] == result["pause_reason"] == "repeated_failures"
        assert result["consecutive_failures"] == 20 and result["counts"]["failed"] == 1
        assert result["counts"]["connection_pending"] == 19 and selected == []
        assert not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)
    else:
        assert result["failure_hold"] is None and result["consecutive_failures"] == 0
        assert result["counts"]["failed"] == 0 and result["counts"]["ready"] == 2
        assert result["counts"]["connection_pending"] == 18 and selected == [20, 21]
        assert result["pause_reason"] == "limit_reached"
    assert stored["rows"][19]["legacy_provider_error_code"] == "proxy_preflight_failed"
    assert stored["rows"][19]["provider_failure"]["curl_code"] == 28


def test_parallel_dispatch_rebuild_never_repeats_a_previously_exhausted_provider_row(imported, tmp_path, monkeypatch):
    parent, plan, _, factory = imported
    path = tmp_path / "progress.json"
    row_three_intent = threading.Event()
    peer_draining = threading.Event()
    calls, original_atomic = [], country._atomic_json

    def worker(_path, _plan, row, **options):
        calls.append(row)
        if row == 1:
            return {"kind": "connection_pending", "provider_failure": safe_request_failure(RequestFailure("request_transport_failed")), "provider_retry_count": 2}
        if row == 2:
            assert row_three_intent.wait(5)
            options["draining"].set()
            peer_draining.set()
            return {"kind": "failed", "code": "account_failed", "login_failure": None, "proxy_failure": None}
        return {"kind": "ready"}

    def atomic(destination, value):
        result = original_atomic(destination, value)
        if destination == path and value["rows"][2]["state"] == "in_progress" and not row_three_intent.is_set():
            row_three_intent.set()
            assert peer_draining.wait(5)
        return result

    monkeypatch.setattr(country, "_http_worker", worker)
    monkeypatch.setattr(country, "_atomic_json", atomic)
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True)
    assert result["status"] == "completed_with_pending"
    assert sorted(calls) == list(range(1, 9)) and calls.count(1) == 1
    assert result["counts"]["connection_pending"] == result["counts"]["failed"] == 1
    assert result["counts"]["ready"] == 6
    assert states(path, plan)[1]["state"] == "connection_pending"


@pytest.mark.parametrize("status", [429, 500, 503])
@pytest.mark.parametrize("field", ["auth_http_status", "page_http_status"])
def test_legacy_browser_diagnostic_provider_status_never_enters_account_review(tmp_path, monkeypatch, status, field):
    from test_account_preparation import fake_capture
    error = LoginCaptureError("login_rejected", stage="home", authentication_result="failed", **{field: status})
    vault = PreparationVault(tmp_path / "accounts.sqlite3", sessions={9: SAVED})
    fake_capture(monkeypatch, vault, failure=error)
    result = preparation.prepare_test_accounts(vault, count=2)
    assert result["connection_pending_rows"] == [8] and result["account_failed_rows"] == []
    assert result["prepared_rows"] == [9] and result["phase"] == "complete"
    assert vault.failure_review()["failed_rows"] == []
    assert sum(event[0] == "capture" for event in vault.events) == 1


def test_recovery_profile_429_response_registers_shared_cooldown_without_renewal(record, transport):
    from anghami_session import provider_recovery, session_recovery
    replies, _, calls = transport
    replies[3] = Reply({"status": "failed"}, status=429)
    replies[3].headers = {"retry-after": "600"}
    with pytest.raises(RequestFailure) as caught:
        session_recovery.recover_legacy_session(record)
    failure = safe_request_failure(caught.value)
    assert failure["failure_category"] == "provider" and failure["retryable"] is True
    assert failure["retry_after_seconds"] == 600
    assert provider_recovery._deadlines["anghami"] - provider_recovery._clock() > 599
    assert len(calls) == 4 and not any(method == "POST" for _, method, _, _ in calls)


def test_peer_cooldown_before_renewal_defers_without_false_unknown_intent(record, transport, monkeypatch):
    from anghami_session import session_recovery
    _, _, calls = transport
    observed = []

    def ready(stage):
        observed.append(stage)
        return stage != "session_recovery_renewal"

    monkeypatch.setattr(session_recovery, "wait_before_provider_request", ready)
    with pytest.raises(RequestFailure) as caught:
        session_recovery.recover_legacy_session(record)
    failure = safe_request_failure(caught.value)
    assert failure["failure_category"] == "provider" and failure["retryable"] is False
    assert getattr(caught.value, "renewal_unknown", False) is False
    assert observed == ["session_recovery_preflight", "session_recovery_renewal"]
    assert len(calls) == 4 and not any(method == "POST" for _, method, _, _ in calls)
