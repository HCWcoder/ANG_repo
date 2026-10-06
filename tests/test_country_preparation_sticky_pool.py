"""Ordered sticky-pool account preparation is exercised entirely offline."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import sys

import pytest

from anghami_session import capture, client, country_preparation as country, proxy, proxy_pool, vault as vault_module
from anghami_session.errors import SessionError
from test_country_preparation_workers import VaultFactory, SyntheticVault, install_recovery, states


PRIVATE_PASSWORD = "synthetic-sticky-account-password"
PRIVATE_USER = "synthetic-sticky-proxy-user"
PRIVATE_KEY = "synthetic-sticky-proxy-key"
LABELS = ("syntheticstickyone", "syntheticstickytwo", "syntheticstickythree")


@pytest.fixture
def imported(tmp_path, monkeypatch):
    records = {row: {
        "country": "EG", "email": f"synthetic-parallel-{row}@example.invalid", "password": PRIVATE_PASSWORD,
    } for row in range(1, 31)}
    raw = "\n".join(f"EG~{record['email']}~{PRIVATE_PASSWORD}~k=synthetic~cookie=synthetic" for record in records.values()).encode()
    source = tmp_path / "registered.txt"
    source.write_bytes(raw)
    factory = VaultFactory(tmp_path / "accounts.sqlite3", records, hashlib.sha256(raw).hexdigest())
    parent = factory(factory.path)
    monkeypatch.setattr(country, "AccountVault", factory)
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Sticky HTTP preparation opened browser login"))
    monkeypatch.setattr(capture, "launch_browser", lambda **_: pytest.fail("Sticky HTTP preparation started a browser"))
    monkeypatch.setattr(proxy.requests, "Session", lambda **_: pytest.fail("Synthetic sticky preparation made a live request"))
    plan = country.build_plan(parent, source, country="EG")
    yield parent, plan, source, factory
    parent.__exit__()


def label(profile):
    return profile.browser_options()["password"].rsplit("_session-", 1)[1]


def install_pool(monkeypatch, factory, *, labels=LABELS, verifier=None):
    selected = []
    profiles = tuple(proxy.PacketStreamProxy.from_route(
        PRIVATE_USER, PRIVATE_KEY, item, "http://proxy.packetstream.io:31112",
    ) for item in labels)
    pool = proxy_pool.StickyProxyPool(profiles)
    original_select = proxy_pool.StickyProxyPool.proxy_for_index

    def select(self, ordinal):
        assert self is pool
        selected.append((ordinal, threading.get_ident()))
        return original_select(self, ordinal)

    def verify(self):
        factory.log("verify_pool", label(self))
        if verifier is not None:
            verifier(self)
        return {**self.summary(), "country_verified": True, "proxy_used": True, "country_check_attempts": 1}

    monkeypatch.setattr(country.StickyProxyPool, "load", lambda _path: pool)
    monkeypatch.setattr(proxy_pool.StickyProxyPool, "proxy_for_index", select)
    monkeypatch.setattr(proxy.PacketStreamProxy, "verify_country", verify)
    return pool, selected


def run(parent, plan, path, **options):
    return country.run_plan(
        parent, plan, path, no_browser=True, proxy_sticky_pool=path.parent / "synthetic-pool.dpapi",
        **options,
    )


def assert_safe(value, factory):
    text = json.dumps(value) if isinstance(value, dict) else str(value)
    for secret in (PRIVATE_PASSWORD, PRIVATE_USER, PRIVATE_KEY, *LABELS):
        assert secret not in text
    assert all(record["email"] not in text for record in factory.records.values())
    assert "_session-" not in text and "proxy_auth" not in text


def test_pool_assignment_is_ordered_before_eight_workers_and_wraps_without_auth_leak(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    parent_thread = threading.get_ident()
    pool, selected = install_pool(monkeypatch, factory)
    barrier = threading.Barrier(8, timeout=5)
    original_attach = SyntheticVault.attach
    checked_rows = []

    def hook(row, profile):
        assert label(profile) == LABELS[(row - 1) % 3]
        if row <= 8:
            barrier.wait()

    def attach(self, row, saved, **options):
        assert label(options["proxy"]) == LABELS[(row - 1) % 3]
        checked_rows.append(row)
        return original_attach(self, row, saved, **options)

    monkeypatch.setattr(SyntheticVault, "attach", attach)
    install_recovery(monkeypatch, factory, hook)
    result = run(parent, plan, path, workers=8, limit=9, max_consecutive_failures=20)
    # A peer may finish while dispatch intent is checkpointed. The coordinator
    # can then abandon that reservation before account HTTP and look up the
    # same unconsumed ordinal again. Actual account routing is checked below.
    assert all(thread == parent_thread for _ordinal, thread in selected)
    ordinals = [ordinal for ordinal, _thread in selected]
    assert list(dict.fromkeys(ordinals)) == list(range(9))
    assert all(after in {before, before + 1} for before, after in zip(ordinals, ordinals[1:]))
    assert sorted(checked_rows) == list(range(1, 10))
    assert result["connection"] == "proxy_egypt" and result["counts"]["ready"] == 9
    assert result["max_consecutive_failures"] == 20
    progress = country.load_progress(path, plan)
    assert progress["proxy_pool_cursor"] == 9 and progress["proxy_pool_count"] == 3
    assert progress["proxy_pool_fingerprint"] == pool.fingerprint()
    assert all(vault.closed for vault in factory.instances[1:])
    assert_safe(result, factory)
    assert_safe(json.loads(path.read_text()), factory)
    for report_path in (factory.path.parent / "country-preparation-reports").rglob("*.json"):
        assert_safe(json.loads(report_path.read_text()), factory)


def test_checkpoint_resume_continues_next_pool_ordinal_without_repeating_accounts(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    pool, selected = install_pool(monkeypatch, factory)
    install_recovery(monkeypatch, factory)
    first = run(parent, plan, path, workers=2, limit=5, max_consecutive_failures=20)
    assert first["pause_reason"] == "limit_reached"
    second = run(parent, plan, path, workers=2, limit=2, max_consecutive_failures=20)
    assert second["counts"]["ready"] == 7
    assert list(dict.fromkeys(ordinal for ordinal, _thread in selected)) == list(range(7))
    assert sum(event[0] == "verify_pool" for event in factory.events) == 7
    assert sorted(event[1] for event in factory.events if event[0] == "recover") == list(range(1, 8))
    assert country.load_progress(path, plan)["proxy_pool_cursor"] == 7


def test_changed_pool_cannot_reassign_an_existing_checkpoint(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    install_pool(monkeypatch, factory)
    install_recovery(monkeypatch, factory)
    run(parent, plan, path, workers=2, limit=1, max_consecutive_failures=20)
    before, events = path.read_bytes(), list(factory.events)
    replacement = proxy_pool.StickyProxyPool((proxy.PacketStreamProxy.from_route(
        PRIVATE_USER, PRIVATE_KEY, "differentlabel", "http://proxy.packetstream.io:31112",
    ),))
    monkeypatch.setattr(country.StickyProxyPool, "load", lambda _path: replacement)
    with pytest.raises(SessionError):
        run(parent, plan, path, workers=2, limit=1, max_consecutive_failures=20)
    assert path.read_bytes() == before and factory.events == events


def replacement_pool():
    return proxy_pool.StickyProxyPool(tuple(proxy.PacketStreamProxy.from_route(
        PRIVATE_USER, PRIVATE_KEY, f"syntheticreplacement{ordinal}", "https://proxy.packetstream.io:31111",
    ) for ordinal in range(2)))


def install_switchable_pool(monkeypatch, factory):
    original_select = proxy_pool.StickyProxyPool.proxy_for_index
    original_pool, _selected = install_pool(monkeypatch, factory)
    active, selections = {"pool": original_pool}, []

    def select(self, ordinal):
        assert self is active["pool"]
        profile = original_select(self, ordinal)
        selections.append((ordinal, label(profile), threading.get_ident()))
        return profile

    monkeypatch.setattr(country.StickyProxyPool, "load", lambda _path: active["pool"])
    monkeypatch.setattr(proxy_pool.StickyProxyPool, "proxy_for_index", select)
    return active, selections


def bound_progress(plan, pool, *, cursor=7):
    progress = country._new_progress(plan)
    progress.update(
        status="paused", pause_reason="limit_reached", no_browser=True, workers=2,
        max_consecutive_failures=20, connection="proxy_egypt", proxy_pool_active=True,
        proxy_pool_count=len(pool), proxy_pool_fingerprint=pool.fingerprint(),
        proxy_pool_endpoint=pool.summary()["endpoint"], proxy_pool_cursor=cursor,
    )
    return progress


def test_explicit_pool_replacement_is_durable_under_lock_before_pending_account_work(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    active, selections = install_switchable_pool(monkeypatch, factory)
    progress = bound_progress(plan, active["pool"])
    progress.update(unknown_acknowledged=True, consecutive_failures=1)
    for row, state, attempts, phase, error in (
        (1, "ready", 1, "complete", None), (2, "failed", 1, "stopped", "account_failed"),
        (3, "unknown", 1, "stopped", "interrupted_unknown"),
        (4, "already_enrolled", 0, "already_enrolled", None),
        (5, "already_ready", 0, "already_ready", None),
    ):
        progress["rows"][row - 1].update(
            state=state, attempts=attempts, phase=phase, error_code=error,
            connection="proxy_egypt" if attempts else None,
        )
    with parent._db:
        for row in (1, 4, 5):
            parent._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=?", (b"synthetic", row))
        for row in (1, 4):
            parent._db.execute("INSERT INTO test_accounts VALUES(?)", (row,))
    path.write_text(json.dumps(progress), encoding="utf-8")
    history = country.load_progress(path, plan)
    active["pool"] = replacement_pool()
    original_lock, original_atomic = country.CountryJobLock, country._atomic_json
    lock_held, rebound = threading.Event(), []

    class CheckedLock:
        def __init__(self, lock_path):
            self.lock = original_lock(lock_path)

        def __enter__(self):
            self.lock.__enter__()
            lock_held.set()
            return self

        def __exit__(self, *args):
            try:
                return self.lock.__exit__(*args)
            finally:
                lock_held.clear()

    def atomic(destination, value):
        if Path(destination) == path and value["proxy_pool_fingerprint"] == active["pool"].fingerprint():
            assert lock_held.is_set()
            rebound.append(deepcopy(value))
        return original_atomic(destination, value)

    def verify(profile):
        assert lock_held.is_set()
        durable = country.load_progress(path, plan)
        assert durable["proxy_pool_fingerprint"] == active["pool"].fingerprint()
        assert durable["proxy_pool_count"] == 2 and durable["proxy_pool_cursor"] == 1
        assert durable["proxy_pool_endpoint"] == "https://proxy.packetstream.io:31111"
        assert durable["rows"][:5] == history["rows"][:5]
        assert durable["unknown_acknowledged"] is True
        assert profile.summary()["endpoint"] == "https://proxy.packetstream.io:31111"
        return {**profile.summary(), "country_verified": True, "proxy_used": True, "country_check_attempts": 1}

    monkeypatch.setattr(country, "CountryJobLock", CheckedLock)
    monkeypatch.setattr(country, "_atomic_json", atomic)
    monkeypatch.setattr(proxy.PacketStreamProxy, "verify_country", verify)
    install_recovery(monkeypatch, factory)
    result = run(parent, plan, path, workers=2, limit=1, max_consecutive_failures=20, replace_proxy_pool=True)
    assert rebound[0]["proxy_pool_cursor"] == 0 and rebound[0]["rows"] == history["rows"]
    assert [(ordinal, chosen) for ordinal, chosen, _thread in selections] == [(0, "syntheticreplacement0")]
    saved = country.load_progress(path, plan)
    assert saved["rows"][:5] == history["rows"][:5]
    for field in ("source_sha256", "plan_id", "run_id", "created_at_utc"):
        assert saved[field] == history[field]
    assert result["counts"]["ready"] == 2 and result["counts"]["unknown"] == 1
    assert result["pause_reason"] == "limit_reached"
    assert [event[1] for event in factory.events if event[0] == "recover"] == [6]
    assert not list(tmp_path.glob("progress.previous-*.json"))
    assert_safe(saved, factory)
    assert "syntheticreplacement" not in json.dumps(result)


def test_explicit_same_pool_keeps_existing_cursor_and_completed_rows(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    _pool, selected = install_pool(monkeypatch, factory)
    install_recovery(monkeypatch, factory)
    run(parent, plan, path, workers=2, limit=4, max_consecutive_failures=20)
    history = country.load_progress(path, plan)
    selected.clear()
    result = run(parent, plan, path, workers=2, limit=1, max_consecutive_failures=20, replace_proxy_pool=True)
    saved = country.load_progress(path, plan)
    assert [ordinal for ordinal, _thread in selected] == [4]
    assert saved["proxy_pool_cursor"] == 5 and saved["rows"][:4] == history["rows"][:4]
    assert saved["run_id"] == history["run_id"] and result["counts"]["ready"] == 5
    assert [event[1] for event in factory.events if event[0] == "recover"].count(1) == 1


def test_pool_replacement_preserves_latched_hold_and_all_attempt_history(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    active, selections = install_switchable_pool(monkeypatch, factory)
    progress = bound_progress(plan, active["pool"], cursor=28)
    progress.update(pause_reason="repeated_failures", failure_hold="repeated_failures",
                    consecutive_failures=20, infrastructure_failures=7, unknown_acknowledged=False)
    for item in progress["rows"][:20]:
        item.update(state="failed", attempts=1, phase="stopped", error_code="account_failed", connection="proxy_egypt")
    progress["rows"][20].update(state="unknown", attempts=1, phase="stopped", error_code="interrupted_unknown", connection="proxy_egypt")
    progress["proxy_failure"] = {"source_row": 20, "stage": "country_check", "code": "country_check_failed",
                                 "failure_kind": "transport_error", "curl_code": 56, "country_check_attempts": 3}
    path.write_text(json.dumps(progress), encoding="utf-8")
    before = country.load_progress(path, plan)
    active["pool"] = replacement_pool()
    events = list(factory.events)
    result = run(parent, plan, path, workers=2, max_consecutive_failures=100, replace_proxy_pool=True)
    saved = country.load_progress(path, plan)
    assert saved["proxy_pool_cursor"] == 0 and saved["proxy_pool_fingerprint"] == active["pool"].fingerprint()
    assert saved["proxy_pool_endpoint"] == "https://proxy.packetstream.io:31111"
    for field in ("rows", "run_id", "created_at_utc", "unknown_acknowledged", "max_consecutive_failures",
                  "infrastructure_failures", "consecutive_failures", "failure_hold", "proxy_failure", "status", "pause_reason"):
        assert saved[field] == before[field]
    assert result["pause_reason"] == result["failure_hold"] == "repeated_failures"
    assert selections == [] and factory.events == events


def test_pool_replacement_does_not_acknowledge_quarantined_attempt(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    active, selections = install_switchable_pool(monkeypatch, factory)
    progress = bound_progress(plan, active["pool"], cursor=12)
    progress.update(status="attention_required", pause_reason="unknown_attempt", unknown_acknowledged=False)
    progress["rows"][0].update(state="unknown", attempts=1, phase="stopped", error_code="interrupted_unknown", connection="proxy_egypt")
    path.write_text(json.dumps(progress), encoding="utf-8")
    history = country.load_progress(path, plan)
    active["pool"] = replacement_pool()
    result = run(parent, plan, path, workers=2, max_consecutive_failures=20, replace_proxy_pool=True)
    saved = country.load_progress(path, plan)
    assert result["status"] == "attention_required" and result["pause_reason"] == "unknown_attempt"
    assert saved["rows"] == history["rows"] and saved["unknown_acknowledged"] is False
    assert saved["proxy_pool_cursor"] == 0 and saved["proxy_pool_fingerprint"] == active["pool"].fingerprint()
    assert not selections and not any(event[0] in {"verify_pool", "recover", "attach", "enroll"} for event in factory.events)


@pytest.mark.parametrize("session_persisted", [False, True])
def test_replacement_reconciles_existing_intent_without_retrying_it(imported, tmp_path, monkeypatch, session_persisted):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    active, selections = install_switchable_pool(monkeypatch, factory)
    progress = bound_progress(plan, active["pool"])
    progress.update(status="running", pause_reason=None, active_row=1, active_rows=[1])
    progress["rows"][0].update(state="in_progress", attempts=1, phase="validation", connection="proxy_egypt")
    if session_persisted:
        with parent._db:
            parent._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=1", (b"synthetic",))
    path.write_text(json.dumps(progress), encoding="utf-8")
    active["pool"] = replacement_pool()
    install_recovery(monkeypatch, factory)
    result = run(parent, plan, path, workers=2, limit=1, max_consecutive_failures=20, replace_proxy_pool=True)
    saved = country.load_progress(path, plan)
    assert saved["rows"][0]["attempts"] == 1
    assert not any(event[0] in {"recover", "attach"} and event[1] == 1 for event in factory.events)
    if session_persisted:
        assert saved["rows"][0]["state"] == "ready" and 1 in parent.enrolled_test_rows()
        assert [event[1] for event in factory.events if event[0] == "recover"] == [2]
        assert [ordinal for ordinal, _chosen, _thread in selections] == [0]
        assert result["pause_reason"] == "limit_reached"
    else:
        assert saved["rows"][0]["state"] == "unknown" and saved["unknown_acknowledged"] is False
        assert result["pause_reason"] == "unknown_attempt" and selections == []


@pytest.mark.parametrize("invalid", ["load", "empty", "oversized", "fingerprint", "endpoint", "same_hash_changed_count"])
def test_invalid_replacement_cannot_change_old_checkpoint_or_read_accounts(imported, tmp_path, monkeypatch, invalid):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    old_pool, _selected = install_pool(monkeypatch, factory)
    path.write_text(json.dumps(bound_progress(plan, old_pool)), encoding="utf-8")
    before, events = path.read_bytes(), list(factory.events)

    class InvalidPool:
        def __len__(self):
            return 0 if invalid == "empty" else 100_001 if invalid == "oversized" else 2

        def fingerprint(self):
            return "invalid" if invalid == "fingerprint" else old_pool.fingerprint() if invalid == "same_hash_changed_count" else "a" * 64

        def summary(self):
            return {"endpoint": "https://private.invalid/?secret=synthetic" if invalid == "endpoint" else "https://proxy.packetstream.io:31111"}

    def load(_path):
        if invalid == "load":
            raise SessionError("The protected synthetic replacement could not load.")
        return InvalidPool()

    monkeypatch.setattr(country.StickyProxyPool, "load", load)
    with pytest.raises(SessionError):
        run(parent, plan, path, workers=2, max_consecutive_failures=20, replace_proxy_pool=True)
    assert path.read_bytes() == before and factory.events == events


@pytest.mark.parametrize("value", [None, 0, 1, "true", []])
def test_replacement_flag_requires_exact_boolean_before_pool_or_account_work(imported, tmp_path, monkeypatch, value):
    parent, plan, _source, factory = imported
    monkeypatch.setattr(country.StickyProxyPool, "load", lambda _path: pytest.fail("Invalid flag opened private pool"))
    with pytest.raises(SessionError):
        run(parent, plan, tmp_path / "progress.json", replace_proxy_pool=value)
    assert not (tmp_path / "progress.json").exists() and len(factory.instances) == 1


@pytest.mark.parametrize("options", [
    {"no_browser": False, "proxy_sticky_pool": "synthetic.dpapi"},
    {"no_browser": True},
    {"no_browser": True, "proxy_sticky_pool": "synthetic.dpapi", "fresh_plan": True},
])
def test_replacement_api_requires_browser_free_existing_plan_and_selected_pool(imported, tmp_path, monkeypatch, options):
    parent, plan, _source, factory = imported
    monkeypatch.setattr(country.StickyProxyPool, "load", lambda _path: pytest.fail("Invalid replacement opened private pool"))
    with pytest.raises(SessionError):
        country.run_plan(parent, plan, tmp_path / "progress.json", replace_proxy_pool=True, **options)
    assert not (tmp_path / "progress.json").exists() and len(factory.instances) == 1


@pytest.mark.parametrize("options", [
    ["--no-browser", "--proxy-sticky-pool"],
    ["--dry-run", "--no-browser", "--proxy-sticky-pool"],
    ["--status", "--no-browser", "--proxy-sticky-pool"],
    ["--stop", "--no-browser", "--proxy-sticky-pool"],
    ["--run", "--proxy-sticky-pool"],
    ["--run", "--no-browser"],
    ["--run", "--no-browser", "--proxy-sticky-pool", "--fresh-plan"],
])
def test_replacement_cli_rejects_invalid_action_before_opening_vault(monkeypatch, options):
    monkeypatch.setattr(country, "AccountVault", lambda *_: pytest.fail("Invalid replacement opened account vault"))
    monkeypatch.setattr(country, "_atomic_json", lambda *_: pytest.fail("Invalid replacement changed checkpoint"))
    with pytest.raises(SystemExit) as error:
        country.main(["--country", "EG", "--replace-proxy-pool", *options])
    assert error.value.code == 2


def test_replacement_cli_passes_explicit_flag_without_fresh_plan(imported, tmp_path, monkeypatch, capsys):
    _parent, _plan, source, factory = imported
    received = []

    def run_plan(_vault, plan, path, **options):
        received.append((plan, path, options))
        return {"status": "paused", "pause_reason": "limit_reached"}

    monkeypatch.setattr(country, "run_plan", run_plan)
    assert country.main([
        "--country", "EG", "--source", str(source), "--vault", str(factory.path), "--run",
        "--no-browser", "--workers", "8", "--limit", "5000", "--max-consecutive-failures", "20",
        "--proxy-sticky-pool", str(tmp_path / "new-synthetic-pool.dpapi"), "--replace-proxy-pool",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "paused"
    assert len(received) == 1
    options = received[0][2]
    assert options["replace_proxy_pool"] is True and options["fresh_plan"] is False
    assert options["no_browser"] is True and options["limit"] == 5000
    assert options["proxy_sticky_pool"] == tmp_path / "new-synthetic-pool.dpapi"
    assert not any(event[0] in {"verify_pool", "recover", "attach", "enroll"} for event in factory.events)


def test_proxy_failures_defer_every_account_without_failure_budget_or_account_requests(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"

    def fail(_profile):
        raise proxy.ProxyCountryError("transport_error", curl_code=28, country_check_attempts=3)

    _pool, selected = install_pool(monkeypatch, factory, verifier=fail)
    install_recovery(monkeypatch, factory, lambda *_: pytest.fail("Failed route reached account recovery"))
    result = run(parent, plan, path, workers=1, max_consecutive_failures=20)
    assert result["status"] == "completed_with_pending"
    assert result["pause_reason"] is result["failure_hold"] is None
    assert result["consecutive_failures"] == result["counts"]["failed"] == 0
    assert result["counts"]["connection_pending"] == 30
    assert len(selected) == 90 and country.load_progress(path, plan)["proxy_pool_cursor"] == 30
    assert not any(event[0] in {"record", "recover", "attach", "enroll"} for event in factory.events)
    assert all(states(path, plan)[row]["state"] == "connection_pending" for row in range(1, 31))


@pytest.mark.parametrize("kind", ["authentication_rejected", "country_unverified", "route_unverified"])
def test_failed_proxy_route_never_uses_account_failure_budget(imported, tmp_path, monkeypatch, kind):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    calls = 0

    def verifier(_profile):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise proxy.ProxyCountryError(kind)

    install_pool(monkeypatch, factory, verifier=verifier)
    install_recovery(monkeypatch, factory)
    result = run(parent, plan, path, workers=1, limit=2, max_consecutive_failures=20)
    assert result["counts"]["failed"] == 0
    assert result["pause_reason"] == "limit_reached" and result["consecutive_failures"] == 0
    if kind == "authentication_rejected":
        assert result["counts"]["ready"] == result["counts"]["connection_pending"] == 1
        assert not any(event[0] in {"record", "recover", "attach", "enroll"} and event[1] == 1 for event in factory.events)
        assert [event[1] for event in factory.events if event[0] == "recover"] == [2]
    else:
        assert result["counts"]["ready"] == 2 and calls == 3
        assert [event[1] for event in factory.events if event[0] == "recover"] == [1, 2]


def test_recovered_routes_and_exhausted_routes_both_leave_account_failure_counter_zero(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    calls = 0

    def verifier(_profile):
        nonlocal calls
        calls += 1
        if calls in {1, 2} or 4 <= calls <= 22:
            raise proxy.ProxyCountryError("http_failure", http_status=503, country_check_attempts=3)

    install_pool(monkeypatch, factory, verifier=verifier)
    install_recovery(monkeypatch, factory)
    result = run(parent, plan, path, workers=1, max_consecutive_failures=20)
    assert result["status"] == "completed_with_pending" and result["failure_hold"] is None
    assert result["counts"]["failed"] == 0 and result["counts"]["ready"] == 24
    assert result["counts"]["connection_pending"] == 6
    assert result["consecutive_failures"] == 0 and calls == 45


def test_near_threshold_dispatch_reserves_remaining_failure_slots_and_latches_twenty(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    barrier = threading.Barrier(2, timeout=5)
    threshold_saved = threading.Event()

    def verifier(profile):
        assert label(profile) in {LABELS[0], LABELS[1]}

    def account_failure(_row, _profile):
        barrier.wait()
        raise SessionError("The saved session was not accepted.")

    pool, selected = install_pool(monkeypatch, factory, verifier=verifier)
    progress = country._new_progress(plan)
    progress.update(
        no_browser=True, workers=8, max_consecutive_failures=20, consecutive_failures=18,
        proxy_pool_active=True, proxy_pool_count=3, proxy_pool_fingerprint=pool.fingerprint(),
        proxy_pool_cursor=18, connection="proxy_egypt",
    )
    for item in progress["rows"][:18]:
        item.update(state="failed", attempts=1, phase="stopped", error_code="account_failed", connection="proxy_egypt")
    path.write_text(json.dumps(progress), encoding="utf-8")
    original_atomic = country._atomic_json

    def atomic(destination, value):
        result = original_atomic(destination, value)
        if Path(destination) == path and value["consecutive_failures"] == 20:
            threshold_saved.set()
        return result

    monkeypatch.setattr(country, "_atomic_json", atomic)
    install_recovery(monkeypatch, factory, account_failure)
    result = run(parent, plan, path, workers=8, max_consecutive_failures=20)
    assert result["failure_hold"] == "repeated_failures" and result["consecutive_failures"] == 20
    assert threshold_saved.is_set()
    assert states(path, plan)[19]["state"] == states(path, plan)[20]["state"] == "failed"
    assert [ordinal for ordinal, _thread in selected] == [18, 19]
    assert all(states(path, plan)[row]["state"] == "pending" for row in range(21, 31))
    before = list(factory.events)
    held = run(parent, plan, path, workers=2, max_consecutive_failures=100, fresh_plan=True)
    assert held["pause_reason"] == "repeated_failures" and factory.events == before


def test_failed_session_validation_never_publishes_ready_or_enrolls_account(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    install_pool(monkeypatch, factory)
    install_recovery(monkeypatch, factory)
    original_attach = SyntheticVault.attach
    validated = []

    def attach(self, row, saved, **options):
        validated.append(row)
        if row in {2, 3, 4}:
            assert self._db.execute("SELECT session FROM accounts WHERE source_row=?", (row,)).fetchone()[0] is None
            raise SessionError("The saved session was not accepted.")
        return original_attach(self, row, saved, **options)

    monkeypatch.setattr(SyntheticVault, "attach", attach)
    result = run(parent, plan, path, workers=1, limit=4, max_consecutive_failures=20)
    assert validated == [1, 2, 3, 4]
    assert result["counts"]["ready"] == 1 and result["counts"]["failed"] == 3
    assert parent.enrolled_test_rows() == frozenset({1})
    assert list(parent._db.execute("SELECT source_row FROM accounts WHERE state='ready' AND session IS NOT NULL")) == [(1,)]
    assert all(states(path, plan)[row]["state"] == "failed" for row in (2, 3, 4))
    reports = list((factory.path.parent / "country-preparation-reports").rglob("*.json"))
    for report_path in reports:
        report = json.loads(report_path.read_text())
        if report.get("failed_row") in {2, 3, 4}:
            assert report["failed_phase"] == "validation" and report["prepared_rows"] == []


def test_source_country_mismatch_halts_even_with_twenty_failure_budget(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    factory.records[1]["country"] = "LB"
    install_pool(monkeypatch, factory)
    install_recovery(monkeypatch, factory, lambda *_: pytest.fail("Source-country mismatch reached recovery"))
    result = run(parent, plan, path, workers=1, max_consecutive_failures=20)
    assert result["status"] == "attention_required" and result["pause_reason"] == "scope_mismatch"
    assert states(path, plan)[1]["state"] == "unknown"
    assert all(states(path, plan)[row]["state"] == "pending" for row in range(2, 31))
    assert not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 101, 20.0, "20", [], {}])
def test_failure_budget_requires_bounded_integer_before_pool_or_account_work(imported, tmp_path, monkeypatch, value):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    monkeypatch.setattr(country.StickyProxyPool, "load", lambda _path: pytest.fail("Invalid budget loaded private pool"))
    with pytest.raises(SessionError):
        run(parent, plan, path, workers=2, max_consecutive_failures=value)
    assert not path.exists() and len(factory.instances) == 1


def test_pool_and_nondefault_budget_require_browser_free_mode(imported, tmp_path, monkeypatch):
    parent, plan, _source, _factory = imported
    monkeypatch.setattr(country.StickyProxyPool, "load", lambda _path: pytest.fail("Browser mode loaded private pool"))
    for options in ({"proxy_sticky_pool": tmp_path / "pool.dpapi"}, {"max_consecutive_failures": 20}):
        with pytest.raises(SessionError):
            country.run_plan(parent, plan, tmp_path / "progress.json", **options)


@pytest.mark.parametrize("field,value", [
    ("proxy_pool_cursor", -1), ("proxy_pool_cursor", True), ("proxy_pool_cursor", "4"),
    ("proxy_pool_count", 0), ("proxy_pool_fingerprint", "secret"),
    ("max_consecutive_failures", True), ("max_consecutive_failures", 101),
    ("consecutive_failures", 21),
])
def test_malformed_pool_checkpoint_or_counter_is_rejected_without_rewrite(imported, tmp_path, monkeypatch, field, value):
    _parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    pool, _selected = install_pool(monkeypatch, factory)
    progress = country._new_progress(plan)
    progress.update(no_browser=True, workers=2, max_consecutive_failures=20,
                    proxy_pool_active=True, proxy_pool_count=3, proxy_pool_fingerprint=pool.fingerprint())
    progress[field] = value
    path.write_text(json.dumps(progress), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SessionError):
        country.load_progress(path, plan)
    assert path.read_bytes() == before


def test_cli_optional_pool_path_and_twenty_budget_are_offline_in_preview(imported, tmp_path, monkeypatch, capsys):
    _parent, _plan, source, factory = imported
    install_pool(monkeypatch, factory)
    monkeypatch.setattr(country, "run_plan", lambda *_args, **_options: pytest.fail("Pool preview started preparation"))
    args = ["--country", "EG", "--source", str(source), "--vault", str(factory.path),
            "--no-browser", "--workers", "8", "--limit", "6000",
            "--max-consecutive-failures", "20", "--proxy-sticky-pool"]
    assert country.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and report["connection"] == "proxy_egypt"
    assert report["workers"] == 8 and report["max_consecutive_failures"] == 20
    assert not (tmp_path / "country-EG-preparation-progress.json").exists()
    assert not any(event[0] in {"verify_pool", "recover", "attach", "enroll"} for event in factory.events)
    assert_safe(report, factory)


@pytest.mark.skipif(os.name != "nt", reason="Real encrypted account vault uses Windows DPAPI")
def test_actual_vault_requires_same_account_and_server_negative_control_before_ready(tmp_path, monkeypatch):
    source, vault_path, path = tmp_path / "registered.txt", tmp_path / "accounts.sqlite3", tmp_path / "progress.json"
    source.write_text("\n".join(
        f"EG~synthetic-vault-{row}@example.invalid~{PRIVATE_PASSWORD}~appsidsave=old;session_fingerprint=device~appsidsave=old;fingerprint=device"
        for row in range(1, 11)
    ), encoding="utf-8")
    monkeypatch.setattr(client.requests, "Session", lambda **_: pytest.fail("Validation gate contacted a live service"))
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Validation gate opened browser login"))
    pool = proxy_pool.StickyProxyPool(tuple(proxy.PacketStreamProxy.from_route(
        PRIVATE_USER, PRIVATE_KEY, item, "http://proxy.packetstream.io:31112",
    ) for item in LABELS))
    monkeypatch.setattr(country.StickyProxyPool, "load", lambda _path: pool)
    monkeypatch.setattr(proxy.PacketStreamProxy, "verify_country", lambda self: {
        **self.summary(), "country_verified": True, "proxy_used": True,
    })
    validations = []
    closed = []

    class ServerValidation:
        def __init__(self, *, saved, proxy):
            self.saved, self.proxy = saved, proxy

        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.append(self.saved["account_email"])

        def check(self, *, negative_control):
            assert negative_control is True
            validations.append((self.saved["account_email"], label(self.proxy)))
            if self.saved["account_email"] == "synthetic-vault-9@example.invalid":
                raise SessionError("The saved session was not accepted.")
            return {"authenticated": True, "checked_at_utc": "2026-10-03T12:00:00+00:00"}

    def recover(record, *, proxy):
        row = record["source_row"]
        email = "wrong-identity@example.invalid" if row == 8 else record["email"]
        saved = {
            "format_version": 1, "created_at_utc": "2026-10-03T12:00:00+00:00",
            "origin": "https://play.anghami.com", "account_email": email,
            "requests": {"relations": {
                "method": "GET", "url": client.GATEWAY_URL + "?type=GETuserrelations&sid=synthetic-private-session",
                "headers": {"cookie": "appsidsave=synthetic-private-session"},
            }},
        }
        return saved, {"identity_verified": True, "authenticated": True}

    monkeypatch.setattr(vault_module, "AnghamiSession", ServerValidation)
    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=recover))
    vault_module.migrate_registered(source, vault_path)
    with vault_module.AccountVault(vault_path) as parent:
        plan = country.build_plan(parent, source, country="EG")
        result = run(parent, plan, path, workers=1, limit=3, max_consecutive_failures=20)
        assert result["counts"]["ready"] == 1 and result["counts"]["failed"] == 2
        assert parent.session(6)["account_email"] == "synthetic-vault-6@example.invalid"
        assert list(parent._db.execute("SELECT source_row FROM accounts WHERE state='ready' AND session IS NOT NULL")) == [(6,)]
        assert list(parent._db.execute("SELECT source_row FROM test_accounts")) == [(6,)]
        for row in (8, 9):
            with pytest.raises(SessionError):
                parent.session(row)
        assert [email for email, _label in validations] == [
            "synthetic-vault-6@example.invalid", "synthetic-vault-9@example.invalid",
        ]
        assert [route_label for _email, route_label in validations] == [LABELS[0], LABELS[2]]
        assert closed == [email for email, _label in validations]
    assert b"synthetic-private-session" not in vault_path.read_bytes()


@pytest.mark.parametrize("failure_kind", ["proxy", "account"])
def test_twenty_budget_refills_one_failed_slot_without_aborting_blocked_peer_preflight(imported, tmp_path, monkeypatch, failure_kind):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    peer_preflight_entered, replacement_prepared = threading.Event(), threading.Event()
    peer_timed_out = []

    def verifier(profile):
        route_label = label(profile)
        if route_label == LABELS[0]:
            assert peer_preflight_entered.wait(5)
            if failure_kind == "proxy":
                raise proxy.ProxyCountryError("authentication_rejected", http_status=407)
        elif route_label == LABELS[1]:
            peer_preflight_entered.set()
            if not replacement_prepared.wait(5):
                peer_timed_out.append(True)

    _pool, selected = install_pool(monkeypatch, factory, verifier=verifier)

    def hook(row, _profile):
        if row == 1:
            assert failure_kind == "account"
            raise SessionError("The saved session was not accepted.")
        if row == 3:
            replacement_prepared.set()

    install_recovery(monkeypatch, factory, hook)
    result = run(parent, plan, path, workers=2, limit=3, max_consecutive_failures=20)
    assert replacement_prepared.is_set() and peer_timed_out == []
    assert result["counts"]["ready"] == 2
    assert result["counts"]["failed"] == (1 if failure_kind == "account" else 0)
    assert result["counts"]["connection_pending"] == (1 if failure_kind == "proxy" else 0)
    assert result["pause_reason"] == "limit_reached"
    progress = country.load_progress(path, plan)
    assert progress["proxy_pool_cursor"] == 3 and progress["consecutive_failures"] == 0
    assert [ordinal for ordinal, _thread in selected] == [0, 1, 2]
    assert states(path, plan)[1]["state"] == ("failed" if failure_kind == "account" else "connection_pending")
    for row in (2, 3):
        assert states(path, plan)[row]["state"] == "ready" and states(path, plan)[row]["attempts"] == 1
    expected_recoveries = [2, 3] if failure_kind == "proxy" else [1, 2, 3]
    assert sorted(event[1] for event in factory.events if event[0] == "recover") == expected_recoveries
    assert all(vault.closed for vault in factory.instances[1:])


def test_default_three_failure_budget_still_drains_peer_before_dispatching_replacement(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    peer_preflight_entered, failed_saved, release_peer = threading.Event(), threading.Event(), threading.Event()
    selection_before_release = []

    def verifier(profile):
        route_label = label(profile)
        if route_label == LABELS[0]:
            assert peer_preflight_entered.wait(5)
        elif route_label == LABELS[1]:
            peer_preflight_entered.set()
            assert release_peer.wait(5)

    _pool, selected = install_pool(monkeypatch, factory, verifier=verifier)
    original_atomic, original_wait = country._atomic_json, country.wait

    def atomic(destination, value):
        result = original_atomic(destination, value)
        if Path(destination) == path and value["rows"][0]["state"] == "failed":
            failed_saved.set()
        return result

    def wait(futures, **options):
        if failed_saved.is_set() and not release_peer.is_set():
            selection_before_release.extend(ordinal for ordinal, _thread in selected)
            release_peer.set()
        return original_wait(futures, **options)

    def hook(row, _profile):
        if row == 1:
            raise SessionError("The saved session was not accepted.")

    monkeypatch.setattr(country, "_atomic_json", atomic)
    monkeypatch.setattr(country, "wait", wait)
    install_recovery(monkeypatch, factory, hook)
    result = run(parent, plan, path, workers=2, limit=3)
    assert selection_before_release == [0, 1]
    assert [ordinal for ordinal, _thread in selected] == [0, 1, 2]
    assert result["counts"]["failed"] == 1 and result["counts"]["ready"] == 2
    assert states(path, plan)[2]["state"] == "ready" and states(path, plan)[3]["state"] == "ready"


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_atomic_checkpoint_retries_only_local_windows_contention_before_publishing(tmp_path, monkeypatch, winerror):
    path = tmp_path / "progress.json"
    path.write_text('{"version":"old"}', encoding="utf-8")
    original_replace = country.os.replace
    attempts, sleeps = [], []
    now = [0.0]

    def replace(source, destination):
        attempts.append((Path(source), Path(destination)))
        if len(attempts) < 3:
            assert json.loads(path.read_text()) == {"version": "old"}
            failure = PermissionError("synthetic-private-sharing-error")
            failure.winerror = winerror
            raise failure
        return original_replace(source, destination)

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(country.os, "replace", replace)
    monkeypatch.setattr(country, "monotonic", lambda: now[0])
    monkeypatch.setattr(country, "sleep", sleep)
    country._atomic_json(path, {"version": "new", "proxy_pool_cursor": 3})
    assert len(attempts) == 3 and sleeps == [0.1, 0.1]
    assert json.loads(path.read_text()) == {"version": "new", "proxy_pool_cursor": 3}
    assert len({temporary for temporary, _target in attempts}) == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_checkpoint_contention_exhaustion_is_bounded_and_keeps_old_file(tmp_path, monkeypatch):
    path = tmp_path / "progress.json"
    original = b'{"version":"old"}'
    path.write_bytes(original)
    attempts, sleeps = [], []
    now = [0.0]

    def replace(source, _destination):
        attempts.append(Path(source))
        failure = PermissionError("synthetic-private-sharing-error")
        failure.winerror = 32
        raise failure

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(country.os, "replace", replace)
    monkeypatch.setattr(country, "monotonic", lambda: now[0])
    monkeypatch.setattr(country, "sleep", sleep)
    with pytest.raises(PermissionError):
        country._atomic_json(path, {"version": "new"})
    assert 20 <= len(attempts) <= 22 and all(seconds == 0.1 for seconds in sleeps)
    assert 2 <= now[0] <= 2.2
    assert path.read_bytes() == original and list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("failure", [OSError(28, "synthetic-private-disk-error"), KeyboardInterrupt()])
def test_unrelated_io_failure_or_interrupt_never_retries_atomic_replace(tmp_path, monkeypatch, failure):
    path = tmp_path / "progress.json"
    original = b'{"version":"old"}'
    path.write_bytes(original)
    attempts = []

    def replace(source, _destination):
        attempts.append(source)
        raise failure

    monkeypatch.setattr(country.os, "replace", replace)
    monkeypatch.setattr(country, "sleep", lambda _: pytest.fail("Unrelated I/O error entered sharing retry"))
    with pytest.raises(type(failure)):
        country._atomic_json(path, {"version": "new"})
    assert len(attempts) == 1 and path.read_bytes() == original and list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("kind", ["filesystem", "sqlite", "session"])
def test_main_failure_reports_only_fixed_kind_and_class_appropriate_numeric_evidence(imported, monkeypatch, capsys, kind):
    _parent, _plan, source, factory = imported
    private = PRIVATE_USER + PRIVATE_KEY + PRIVATE_PASSWORD
    if kind == "filesystem":
        failure = OSError(13, private)
        failure.winerror = 32
        failure.sqlite_errorcode = 5
        expected = {"error_kind": "filesystem_error", "errno": 13, "winerror": 32}
    elif kind == "sqlite":
        failure = sqlite3.OperationalError(private)
        failure.sqlite_errorcode = 5
        failure.errno = 13
        expected = {"error_kind": "sqlite_error", "sqlite_errorcode": 5}
    else:
        failure = SessionError(private)
        failure.errno = 13
        expected = {"error_kind": "session_error"}

    def fail(*_args, **_options):
        raise failure

    monkeypatch.setattr(country, "run_plan", fail)
    assert country.main(["--country", "EG", "--source", str(source), "--vault", str(factory.path), "--run", "--no-browser"]) == 1
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["diagnostics"] == expected and report["error_code"] == "country_preparation_failed"
    assert_safe(report, factory)
    assert output.err == ""


@pytest.mark.parametrize("value", [True, "32", -1, 65536, PRIVATE_KEY])
def test_runtime_failure_numeric_diagnostics_reject_invalid_or_private_values(value):
    error = OSError(PRIVATE_KEY)
    error.errno = error.winerror = value
    assert country._safe_runtime_failure(error) == {"error_kind": "filesystem_error"}


@pytest.mark.parametrize("held", [False, True])
def test_recovered_ready_intent_resets_counter_only_when_no_failure_hold_is_latched(imported, tmp_path, monkeypatch, held):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    durable_snapshots = []
    original_atomic = country._atomic_json

    def atomic(destination, value):
        result = original_atomic(destination, value)
        if Path(destination) == path:
            durable_snapshots.append(deepcopy(value))
        return result

    def verifier(_profile):
        assert durable_snapshots[-1]["consecutive_failures"] == (20 if held else 0)
        assert durable_snapshots[-1]["infrastructure_failures"] == (20 if held else 0)
        if held:
            raise proxy.ProxyCountryError("transport_error", curl_code=56, country_check_attempts=3)

    pool, selected = install_pool(monkeypatch, factory, verifier=verifier)
    progress = country._new_progress(plan)
    progress.update(
        no_browser=True, workers=2, max_consecutive_failures=20,
        consecutive_failures=20 if held else 7,
        infrastructure_failures=20 if held else 7,
        failure_hold="repeated_failures" if held else None,
        proxy_pool_active=True, proxy_pool_count=3, proxy_pool_fingerprint=pool.fingerprint(),
        proxy_pool_cursor=1, connection="proxy_egypt",
    )
    progress["rows"][0].update(state="in_progress", attempts=1, phase="validation", connection="proxy_egypt")
    path.write_text(json.dumps(progress), encoding="utf-8")
    with parent._db:
        parent._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=1", (b"synthetic",))
    monkeypatch.setattr(country, "_atomic_json", atomic)
    install_recovery(monkeypatch, factory)
    result = run(parent, plan, path, workers=2, limit=1, max_consecutive_failures=20,
                 **({"resume_after_review": True} if held else {}))
    assert states(path, plan)[1]["state"] == "ready"
    assert [ordinal for ordinal, _thread in selected] == ([1, 2, 3] if held else [1])
    assert country.load_progress(path, plan)["proxy_pool_cursor"] == 2
    if held:
        assert result["failure_hold"] == "repeated_failures" and result["consecutive_failures"] == 20
        assert result["counts"]["ready"] == result["counts"]["connection_pending"] == 1
        assert result["counts"]["failed"] == 0
    else:
        assert result["failure_hold"] is None and result["consecutive_failures"] == 0
        assert country.load_progress(path, plan)["infrastructure_failures"] == 0
        assert result["counts"]["ready"] == 2

