"""Large requested concurrency is bounded by synthetic tasks, never a live API."""

from concurrent.futures import ALL_COMPLETED, wait as real_wait
import json
import threading
from time import monotonic, sleep

import pytest

from anghami_session import ui_jobs, ui_server
from test_ui_jobs_concurrency import manager_for, finish, operational_failure, success


@pytest.mark.parametrize("action", ["play", "like"])
def test_large_worker_request_uses_only_distinct_selected_account_identities(tmp_path, monkeypatch, action):
    manager, factory = manager_for(tmp_path, rows=[1, 2, 3, 4])
    factory.identities[2] = factory.identities[1].upper()
    sizes = []
    executor = ui_jobs.ThreadPoolExecutor
    def bounded_executor(*, max_workers, **kwargs):
        sizes.append(max_workers)
        return executor(max_workers=max_workers, **kwargs)
    monkeypatch.setattr(ui_jobs, "ThreadPoolExecutor", bounded_executor)
    queued = manager.submit({"action": action, "rows": [1, 2, 3, 4], "count": 3,
                             "workers": 2**53 - 1, "max_consecutive_failures": 20})
    job = finish(manager)
    assert queued["workers"] == queued["requested_workers"] == 2**53 - 1
    assert queued["effective_workers"] == 0
    assert sizes == [3]
    assert job["effective_workers"] == 3 and job["failure_limit_scope"] == "active_budget"
    assert job["succeeded"] == job["attempted"] == 12 and factory.peak <= 3
    assert factory.attempts == {1: 3, 2: 3, 3: 3, 4: 3}


@pytest.mark.parametrize("action", ["play", "like"])
def test_more_than_twenty_workers_run_then_threshold_stops_new_dispatch_and_drains(tmp_path, monkeypatch, action):
    all_started, release_failures, release_drain = threading.Event(), threading.Event(), threading.Event()
    started, lock = set(), threading.Lock()
    def handler(kind, row, _number, _options):
        with lock:
            started.add(row)
            if len(started) == 25:
                all_started.set()
        assert (release_failures if row <= 20 else release_drain).wait(5)
        return operational_failure(kind) if row <= 20 else success(kind)
    manager, factory = manager_for(tmp_path, rows=range(1, 51), handler=handler)
    calls = []
    def grouped_wait(active, **_kwargs):
        ordered = list(active)
        calls.append(len(ordered))
        if len(calls) == 1:
            assert len(ordered) == 25 and all_started.wait(5)
            snapshot = manager.snapshot()
            assert snapshot["effective_workers"] == snapshot["active_workers"] == 25
            assert snapshot["failure_limit_scope"] == "new_dispatch"
            release_failures.set()
            done, pending = real_wait(ordered[:20], timeout=5, return_when=ALL_COMPLETED)
            assert len(done) == 20 and not pending
            return done, set(ordered[20:])
        assert manager.snapshot()["stop_reason"] == "consecutive_failure_limit"
        release_drain.set()
        return real_wait(active, timeout=5, return_when=ALL_COMPLETED)
    monkeypatch.setattr(ui_jobs, "wait", grouped_wait)
    try:
        manager.submit({"action": action, "rows": list(factory.rows), "workers": 25,
                        "max_consecutive_failures": 20})
        job = finish(manager)
    finally:
        release_failures.set()
        release_drain.set()
    assert factory.peak == 25 and started == set(range(1, 26))
    assert calls == [25, 5]
    assert job["status"] == "failed" and job["stop_reason"] == "consecutive_failure_limit"
    assert (job["failed"], job["succeeded"], job["attempted"], job["completed_tests"], job["skipped"]) == (20, 5, 25, 25, 25)
    assert job["consecutive_failures"] == 20 and job["active_workers"] == 0 and job["active_tests"] == []
    assert all(item.closed for item in factory.instances)


@pytest.mark.parametrize("action", ["play", "like"])
def test_large_concurrency_keeps_unknown_write_immediate_stop(tmp_path, monkeypatch, action):
    all_started, release_unknown, release_drain = threading.Event(), threading.Event(), threading.Event()
    seen, lock = set(), threading.Lock()
    def handler(kind, row, _number, _options):
        with lock:
            seen.add(row)
            if len(seen) == 25:
                all_started.set()
        assert (release_unknown if row == 1 else release_drain).wait(5)
        if row != 1:
            return success(kind)
        prefix = "event" if kind == "play" else "mutation"
        return {"passed": False, f"{prefix}_attempted": True,
                f"{prefix}_result": "unknown", "error_code": f"{prefix}_incomplete"}
    manager, factory = manager_for(tmp_path, rows=range(1, 41), handler=handler)
    invocations = 0
    def grouped_wait(active, **_kwargs):
        nonlocal invocations
        invocations += 1
        ordered = list(active)
        if invocations == 1:
            assert len(ordered) == 25 and all_started.wait(5)
            release_unknown.set()
            done, pending = real_wait(ordered[:1], timeout=5, return_when=ALL_COMPLETED)
            assert not pending
            return done, set(ordered[1:])
        assert manager.snapshot()["stop_reason"] == "result_unknown"
        release_drain.set()
        return real_wait(active, timeout=5, return_when=ALL_COMPLETED)
    monkeypatch.setattr(ui_jobs, "wait", grouped_wait)
    try:
        manager.submit({"action": action, "rows": list(factory.rows), "workers": 25,
                        "max_consecutive_failures": 20})
        job = finish(manager)
    finally:
        release_unknown.set()
        release_drain.set()
    assert seen == set(range(1, 26)) and factory.attempts[1] == 1
    assert job["status"] == "failed" and job["stop_reason"] == "result_unknown"
    assert job["error"]["result_unknown"] is True and job["failed"] == 1
    assert job["attempted"] == 25 and job["active_workers"] == 0


@pytest.mark.parametrize("key", ["workers", "requested_workers", "effective_workers", "active_workers"])
@pytest.mark.parametrize("bad", [True, False, -1, 1.0, "25", 2**53, None])
def test_public_worker_metadata_requires_safe_integers(key, bad):
    assert ui_jobs._public_report({key: bad}) == {}
    assert ui_server.safe_report({key: bad}) == {key: None}


def test_zero_effective_workers_and_failure_scope_are_public_without_a_fixed_operating_cap():
    value = {"workers": 10_000, "requested_workers": 10_000, "effective_workers": 0,
             "active_workers": 0, "failure_limit_scope": "new_dispatch", "password": "synthetic-secret"}
    expected = {key: item for key, item in value.items() if key != "password"}
    assert ui_jobs._public_report(value) == expected
    assert ui_server.safe_report(value) == expected
    assert "synthetic-secret" not in json.dumps(expected)


def test_active_test_visibility_has_its_own_output_bound_not_a_worker_limit():
    descriptors = [{"source_row": row, "test_number": 1, "started_at": "2026-10-04T00:00:00+00:00",
                    "password": "synthetic-secret"} for row in range(1, 602)]
    result = ui_server.safe_report({"active_tests": descriptors})["active_tests"]
    assert len(result) == ui_server.MAX_ACTIVE_TEST_DESCRIPTORS == 500
    assert result[24]["source_row"] == 25
    assert all(set(item) == {"source_row", "test_number", "started_at"} for item in result)


@pytest.mark.parametrize("action", ["play", "like"])
def test_peer_failure_finishing_during_result_journal_is_drained_before_replacement(tmp_path, monkeypatch, action):
    all_started, release_first, release_failure, release_drain = (threading.Event() for _ in range(4))
    started, lock, futures = set(), threading.Lock(), {}
    def handler(kind, row, _number, _options):
        with lock:
            started.add(row)
            if len(started) == 3:
                all_started.set()
        gate = release_first if row == 1 else release_failure if row == 2 else release_drain
        assert gate.wait(5)
        return operational_failure(kind) if row == 2 else success(kind)
    manager, factory = manager_for(tmp_path, rows=range(1, 6), handler=handler)
    executor_type = ui_jobs.ThreadPoolExecutor
    def tracking_executor(**kwargs):
        executor = executor_type(**kwargs)
        submit = executor.submit
        def tracked_submit(callback, *args, **options):
            future = submit(callback, *args, **options)
            futures[args[2]] = future
            return future
        executor.submit = tracked_submit
        return executor
    monkeypatch.setattr(ui_jobs, "ThreadPoolExecutor", tracking_executor)
    journal = ui_jobs._journal
    overlap_seen = []
    def overlapping_journal(value, path):
        if path.name == f"account-1.{action}-1.redacted.json":
            release_failure.set()
            deadline = monotonic() + 5
            while not futures[2].done() and monotonic() < deadline:
                sleep(0.001)
            assert futures[2].done()
            overlap_seen.append(True)
        return journal(value, path)
    monkeypatch.setattr(ui_jobs, "_journal", overlapping_journal)
    waits = []
    def first_only_wait(active, **_kwargs):
        waits.append(len(active))
        if len(waits) == 1:
            assert all_started.wait(5)
            release_first.set()
            done, pending = real_wait([futures[1]], timeout=5, return_when=ALL_COMPLETED)
            assert not pending
            return done, set(active).difference(done)
        assert manager.snapshot()["stop_reason"] == "consecutive_failure_limit"
        release_drain.set()
        return real_wait(active, timeout=5, return_when=ALL_COMPLETED)
    monkeypatch.setattr(ui_jobs, "wait", first_only_wait)
    try:
        manager.submit({"action": action, "rows": list(factory.rows), "workers": 3,
                        "max_consecutive_failures": 1})
        job = finish(manager)
    finally:
        release_first.set()
        release_failure.set()
        release_drain.set()
    assert overlap_seen == [True] and waits == [3, 1]
    assert started == {1, 2, 3} and factory.attempts == {1: 1, 2: 1, 3: 1}
    assert job["failure_limit_scope"] == "new_dispatch" and job["stop_reason"] == "consecutive_failure_limit"
    assert (job["failed"], job["succeeded"], job["skipped"]) == (1, 2, 2)


def test_preparation_progress_uses_effective_workers_and_labels_requested_count(tmp_path):
    manager = ui_jobs.JobManager(tmp_path / "synthetic.sqlite3")
    manager._latest = {"status": "running", "progress": {"completed": 0, "total": 100}, "workers": 1000}
    manager._preparation_progress({"workers": 1000, "requested_workers": 1000, "effective_workers": 7,
                                   "active_workers": 3, "active_rows": [1, 2, 3], "prepared_account_count": 4,
                                   "failure_limit_scope": "active_budget", "phase": "preparing"})
    job = manager.snapshot()
    assert job["message"] == "Prepared 4 account(s); 3 of 7 worker(s) active (1000 requested)."
    assert job["requested_workers"] == 1000 and job["effective_workers"] == 7 and job["active_workers"] == 3


@pytest.mark.parametrize("effective", [True, -1, 1001, 7.0, "7", 2**53])
def test_preparation_progress_does_not_publish_invalid_effective_worker_count(tmp_path, effective):
    manager = ui_jobs.JobManager(tmp_path / "synthetic.sqlite3")
    manager._latest = {"status": "running", "progress": {"completed": 0, "total": 100}}
    manager._preparation_progress({"workers": 1000, "effective_workers": effective,
                                   "active_workers": 3, "prepared_account_count": 4, "phase": "preparing"})
    assert "effective_workers" not in manager.snapshot()
