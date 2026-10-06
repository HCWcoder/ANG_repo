"""Checkpoint-time completions are counted before coordinator replacements."""

from concurrent.futures import Future
from copy import deepcopy
from pathlib import Path

import pytest

from anghami_session import country_preparation as country
from anghami_session.errors import RequestFailure
from test_country_preparation_fifty_workers import large_import
from test_country_preparation_sticky_pool import install_pool
from test_country_preparation_workers import assert_safe


FAILED = {"kind": "failed", "code": "account_failed", "login_failure": None, "proxy_failure": None}
PROVIDER = RequestFailure("request_transport_failed", stage="preflight", curl_code=7, retry_safe=False).diagnostics
CONNECTION_PENDING = {"kind": "connection_pending", "code": "provider_unavailable", "provider_failure": PROVIDER}


def install_controlled_executor(monkeypatch, *, complete_initial):
    futures, submitted = {}, []

    class ControlledExecutor:
        def __init__(self, *, max_workers, **_options):
            assert max_workers == 21
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            assert all(future.done() for future in futures.values())
        def submit(self, _function, _path, _plan, row, **_options):
            # The coordinator must observe the threshold before any replacement.
            assert row <= 21, "A replacement started before counting a finished failure"
            submitted.append(row)
            future = futures[row] = Future()
            if row == 21:
                complete_initial(futures)
            return future

    monkeypatch.setattr(country, "ThreadPoolExecutor", ControlledExecutor)
    return futures, submitted


def test_peer_finishing_during_another_consumed_result_checkpoint_prevents_replacement(
        large_import, tmp_path, monkeypatch):
    parent, plan, _source, factory = large_import
    path = tmp_path / "progress.json"

    def complete_initial(futures):
        for row in range(2, 21):
            futures[row].set_result(deepcopy(FAILED))
        futures[21].set_result(deepcopy(CONNECTION_PENDING))

    futures, submitted = install_controlled_executor(monkeypatch, complete_initial=complete_initial)
    original_atomic = country._atomic_json
    triggered = []

    def checkpoint(destination, progress):
        result = original_atomic(destination, progress)
        if Path(destination) == path and len(futures) == 21 and not futures[1].done():
            second = next(item for item in progress["rows"] if item["source_row"] == 2)
            if second["state"] == "failed":
                # Future1 was checked before future2 in the consume loop.
                triggered.append(True)
                futures[1].set_result(deepcopy(FAILED))
        return result

    monkeypatch.setattr(country, "_atomic_json", checkpoint)
    result = country.run_plan(parent, plan, path, workers=21, no_browser=True, max_consecutive_failures=20)
    assert triggered == [True] and submitted == list(range(1, 22))
    assert result["pause_reason"] == result["failure_hold"] == "repeated_failures"
    assert result["consecutive_failures"] == result["counts"]["failed"] == 20
    assert result["counts"]["connection_pending"] == 1 and result["counts"]["pending"] == 59
    assert result["active_rows"] == [] and not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)
    assert_safe(result, factory)


def test_peer_finishing_during_replacement_intent_checkpoint_restores_pending_row_and_pool_cursor(
        large_import, tmp_path, monkeypatch):
    parent, plan, _source, factory = large_import
    path = tmp_path / "progress.json"
    progress = country._new_progress(plan)
    progress.update(no_browser=True, workers=21, max_consecutive_failures=20, consecutive_failures=19)
    for item in progress["rows"][-19:]:
        item.update(state="failed", phase="stopped", attempts=1, error_code="account_failed")
    prior = progress["rows"][21]
    prior.update(state="connection_pending", phase="connection_pending", attempts=1,
                 error_code="provider_unavailable", provider_failure=deepcopy(PROVIDER), connection="proxy_egypt")
    original_pending = deepcopy(prior)
    country._atomic_json(path, progress)
    _pool, selected = install_pool(monkeypatch, factory)

    def complete_initial(futures):
        for row in range(2, 22):
            futures[row].set_result(deepcopy(CONNECTION_PENDING))

    futures, submitted = install_controlled_executor(monkeypatch, complete_initial=complete_initial)
    original_atomic = country._atomic_json
    triggered = []

    def checkpoint(destination, value):
        result = original_atomic(destination, value)
        if Path(destination) == path and len(futures) == 21 and not futures[1].done():
            replacement = next(item for item in value["rows"] if item["source_row"] == 22)
            if replacement["state"] == "in_progress":
                triggered.append(True)
                futures[1].set_result(deepcopy(FAILED))
        return result

    monkeypatch.setattr(country, "_atomic_json", checkpoint)
    result = country.run_plan(parent, plan, path, workers=21, no_browser=True, max_consecutive_failures=20,
                              proxy_sticky_pool=tmp_path / "synthetic-pool.dpapi")
    stored = country.load_progress(path, plan)
    restored = next(item for item in stored["rows"] if item["source_row"] == 22)
    assert triggered == [True] and submitted == list(range(1, 22))
    assert result["pause_reason"] == result["failure_hold"] == "repeated_failures"
    assert result["consecutive_failures"] == result["counts"]["failed"] == 20
    assert {name: restored[name] for name in original_pending} == original_pending
    assert stored["proxy_pool_cursor"] == result["proxy_pool"]["next_ordinal"] == 21
    assert [ordinal for ordinal, _owner in selected] == list(range(22))
    assert result["active_rows"] == [] and not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)
    assert_safe(result, factory)
