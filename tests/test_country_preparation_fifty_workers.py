"""Fifty-worker preparation uses synthetic identities and no remote services."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading

import pytest

from anghami_session import country_preparation as country
from anghami_session import capture
from anghami_session.errors import SessionError
from test_country_preparation_workers import (
    PRIVATE_ERROR, PRIVATE_PASSWORD, VaultFactory, assert_safe, install_recovery,
)


@pytest.fixture
def large_import(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Offline test opened a browser"))
    records = {row: {"country": "EG", "email": f"synthetic-parallel-{row}@example.invalid",
                     "password": PRIVATE_PASSWORD} for row in range(1, 81)}
    raw = "\n".join(f"EG~{record['email']}~{PRIVATE_PASSWORD}~k=synthetic~cookie=synthetic"
                    for record in records.values()).encode()
    source = tmp_path / "registered.txt"
    source.write_bytes(raw)
    factory = VaultFactory(tmp_path / "accounts.sqlite3", records, hashlib.sha256(raw).hexdigest())
    parent = factory(factory.path)
    monkeypatch.setattr(country, "AccountVault", factory)
    plan = country.build_selected_plan(parent, source, list(records), country="EG")
    yield parent, plan, source, factory
    parent.__exit__()


def test_fifty_accounts_really_run_concurrently_with_distinct_identity_and_thread_owned_connections(
        large_import, tmp_path, monkeypatch):
    parent, _full, source, factory = large_import
    plan = country.build_selected_plan(parent, source, list(range(1, 51)), country="EG")
    barrier = threading.Barrier(50, timeout=20)
    owners, active = {}, set()
    peak = 0
    lock = threading.Lock()

    def hook(row, _proxy):
        nonlocal peak
        with lock:
            assert row not in owners
            owners[row] = threading.get_ident()
            active.add(row)
            peak = max(peak, len(active))
        barrier.wait()
        with lock:
            active.remove(row)

    install_recovery(monkeypatch, factory, hook)
    path = tmp_path / "progress.json"
    result = country.run_plan(parent, plan, path, workers=50, no_browser=True, max_consecutive_failures=20)
    assert result["status"] == "completed" and result["counts"]["ready"] == 50
    assert result["workers"] == peak == result["attempted_accounts"] == 50
    assert result["failure_limit_scope"] == "new_dispatch"
    assert len(set(owners.values())) == 50 and set(owners) == set(range(1, 51))
    assert len(factory.instances[1:]) == 50 and all(v.closed for v in factory.instances[1:])
    assert len({id(v._db) for v in factory.instances[1:]}) == 50
    assert {v.owner for v in factory.instances[1:]} == set(owners.values())
    assert result["active_rows"] == [] and result["max_consecutive_failures"] == 20
    assert_safe(result, factory)


def test_twenty_observed_failures_stop_replacements_and_drain_fifty_worker_pool(
        large_import, tmp_path, monkeypatch):
    parent, plan, _source, factory = large_import
    barrier = threading.Barrier(50, timeout=20)
    threshold = threading.Event()
    submissions, reached = [], []

    class TracedExecutor(ThreadPoolExecutor):
        def submit(self, function, *args, **options):
            submissions.append((args[2], threshold.is_set()))
            return super().submit(function, *args, **options)

    def hook(row, _proxy):
        if row <= 50:
            barrier.wait()
        if row <= 20:
            raise SessionError(PRIVATE_ERROR)
        assert threshold.wait(20)

    def progress(summary):
        if summary["pause_reason"] == "repeated_failures":
            reached.append(summary["consecutive_failures"])
            threshold.set()

    monkeypatch.setattr(country, "ThreadPoolExecutor", TracedExecutor)
    install_recovery(monkeypatch, factory, hook)
    path = tmp_path / "progress.json"
    result = country.run_plan(parent, plan, path, workers=50, no_browser=True,
                              max_consecutive_failures=20, progress_callback=progress)
    assert threshold.is_set() and reached and set(reached) == {20}
    assert result["status"] == "paused" and result["pause_reason"] == result["failure_hold"] == "repeated_failures"
    assert result["max_consecutive_failures"] == result["consecutive_failures"] == result["counts"]["failed"] == 20
    assert result["counts"]["ready"] >= 30 and result["counts"]["unknown"] == 0
    assert 50 <= len(submissions) <= 69 and not any(after_limit for _row, after_limit in submissions)
    assert result["counts"]["pending"] >= 11 and result["active_rows"] == []
    assert all(v.closed for v in factory.instances[1:])
    assert result["failure_limit_scope"] == "new_dispatch"
    before = len(factory.events), len(submissions)
    held = country.run_plan(parent, plan, path, workers=50, no_browser=True, max_consecutive_failures=20)
    assert held["failure_hold"] == "repeated_failures" and before == (len(factory.events), len(submissions))
    assert_safe(result, factory)
    assert_safe(held, factory)


def test_already_submitted_failures_can_exceed_twenty_without_new_dispatch(
        large_import, tmp_path, monkeypatch):
    parent, plan, _source, factory = large_import
    barrier = threading.Barrier(50, timeout=20)

    def hook(row, _proxy):
        if row <= 50:
            barrier.wait()
        raise SessionError(PRIVATE_ERROR)

    install_recovery(monkeypatch, factory, hook)
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=50, no_browser=True,
                              max_consecutive_failures=20)
    assert result["pause_reason"] == "repeated_failures" and result["failure_hold"] == "repeated_failures"
    assert result["counts"]["failed"] >= 50 and result["consecutive_failures"] == 20
    assert result["failure_limit_scope"] == "new_dispatch" and result["active_rows"] == []
    assert_safe(result, factory)


@pytest.mark.parametrize("workers", [10, 16, 20])
def test_pools_up_to_twenty_keep_strict_remaining_active_failure_budget(
        large_import, tmp_path, monkeypatch, workers):
    parent, plan, _source, factory = large_import
    barrier = threading.Barrier(workers, timeout=20)

    def hook(row, _proxy):
        if row <= workers:
            barrier.wait()
        raise SessionError(PRIVATE_ERROR)

    install_recovery(monkeypatch, factory, hook)
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=workers, no_browser=True,
                              max_consecutive_failures=20)
    recoveries = [event[1] for event in factory.events if event[0] == "recover"]
    assert len(recoveries) == len(set(recoveries)) == result["counts"]["failed"] == 20
    assert result["counts"]["pending"] == 60 and result["counts"]["unknown"] == 0
    assert result["pause_reason"] == "repeated_failures" and result["failure_limit_scope"] == "active_budget"
    assert_safe(result, factory)


def test_graceful_stop_finishes_fifty_started_accounts_and_keeps_remaining_rows_pending(
        large_import, tmp_path, monkeypatch):
    parent, plan, _source, factory = large_import
    barrier = threading.Barrier(50, timeout=20)
    stopped = threading.Event()
    path = tmp_path / "progress.json"
    stop = path.with_suffix(".stop")

    def hook(row, _proxy):
        assert row <= 50
        barrier.wait()
        if row == 1:
            stop.write_text("stop", encoding="utf-8")
            stopped.set()
        else:
            assert stopped.wait(20)

    install_recovery(monkeypatch, factory, hook)
    result = country.run_plan(parent, plan, path, workers=50, no_browser=True, max_consecutive_failures=20)
    assert result["pause_reason"] == "stop_requested" and result["counts"]["ready"] == 50
    assert result["counts"]["pending"] == 30 and result["active_rows"] == []
    assert result["counts"]["unknown"] == result["counts"]["failed"] == 0
    assert all(v.closed for v in factory.instances[1:])
    assert_safe(result, factory)


@pytest.mark.parametrize("workers", [country.MAX_WORKER_REQUEST + 1, True, "50", 50.0])
def test_unsafe_or_noninteger_worker_requests_reject_before_store_access(workers):
    with pytest.raises(SessionError, match="positive JavaScript-safe integer"):
        country.run_plan(object(), {}, "unused", workers=workers, no_browser=True)
