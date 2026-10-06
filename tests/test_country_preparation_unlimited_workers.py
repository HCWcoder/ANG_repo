"""Requested concurrency is uncapped while executors follow frozen work size."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from anghami_session import country_preparation as country
from anghami_session.errors import SessionError
from test_country_preparation_workers import (
    assert_safe, imported, install_recovery,
)
from test_country_preparation_fifty_workers import large_import
from test_ui_preparation_bridge import JOB_ID, invoke, offline_bridge


@pytest.mark.parametrize("requested", [51, 500, country.MAX_WORKER_REQUEST])
def test_huge_request_for_five_unique_rows_creates_only_five_actual_workers(
        imported, tmp_path, monkeypatch, requested):
    parent, _full, source, factory = imported
    plan = country.build_selected_plan(parent, source, [8, 2, 5, 1, 7], country="EG")
    barrier = threading.Barrier(5, timeout=10)
    capacities, owners = [], set()
    lock = threading.Lock()

    class TracedExecutor(ThreadPoolExecutor):
        def __init__(self, *args, **options):
            capacities.append(options["max_workers"])
            super().__init__(*args, **options)

    def hook(_row, _proxy):
        with lock:
            owners.add(threading.get_ident())
        barrier.wait()

    monkeypatch.setattr(country, "ThreadPoolExecutor", TracedExecutor)
    install_recovery(monkeypatch, factory, hook)
    path = tmp_path / "progress.json"
    result = country.run_plan(parent, plan, path, workers=requested, no_browser=True, max_consecutive_failures=20)
    assert result["status"] == "completed" and result["counts"]["ready"] == 5
    assert result["workers"] == result["requested_workers"] == requested
    assert result["effective_workers"] == len(owners) == 5 and capacities == [5]
    assert result["failure_limit_scope"] == "active_budget"
    stored = country.load_progress(path, plan)
    assert stored["workers"] == stored["requested_workers"] == requested and stored["effective_workers"] == 5
    assert_safe(result, factory)


def test_concurrency_is_not_capped_at_fifty_when_sixty_frozen_accounts_are_available(
        large_import, tmp_path, monkeypatch):
    parent, _full, source, factory = large_import
    plan = country.build_selected_plan(parent, source, list(range(1, 61)), country="EG")
    barrier = threading.Barrier(60, timeout=20)
    owners = set()
    lock = threading.Lock()

    def hook(_row, _proxy):
        with lock:
            owners.add(threading.get_ident())
        barrier.wait()

    install_recovery(monkeypatch, factory, hook)
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=60,
                              no_browser=True, max_consecutive_failures=20)
    assert result["status"] == "completed" and result["counts"]["ready"] == 60
    assert result["effective_workers"] == result["requested_workers"] == len(owners) == 60
    assert result["failure_limit_scope"] == "new_dispatch"
    assert all(v.closed for v in factory.instances[1:])
    assert_safe(result, factory)


def test_executor_clamps_to_explicit_run_limit_and_resumes_remaining_rows(
        imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    capacities = []

    class TracedExecutor(ThreadPoolExecutor):
        def __init__(self, *args, **options):
            capacities.append(options["max_workers"])
            super().__init__(*args, **options)

    monkeypatch.setattr(country, "ThreadPoolExecutor", TracedExecutor)
    install_recovery(monkeypatch, factory)
    path = tmp_path / "progress.json"
    first = country.run_plan(parent, plan, path, workers=1000, no_browser=True, limit=3, max_consecutive_failures=20)
    assert first["pause_reason"] == "limit_reached" and first["counts"]["ready"] == 3
    assert first["effective_workers"] == 3 and capacities == [3]
    second = country.run_plan(parent, plan, path, workers=1000, no_browser=True, max_consecutive_failures=20)
    assert second["counts"]["ready"] == 8 and second["effective_workers"] == 5 and capacities == [3, 5]
    recoveries = [event[1] for event in factory.events if event[0] == "recover"]
    assert len(recoveries) == len(set(recoveries)) == 8


def test_completed_frozen_cohort_reports_zero_effective_workers_without_executor(
        imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    install_recovery(monkeypatch, factory)
    path = tmp_path / "progress.json"
    country.run_plan(parent, plan, path, workers=8, no_browser=True, max_consecutive_failures=20)
    before = list(factory.events)
    monkeypatch.setattr(country, "ThreadPoolExecutor", lambda **_: pytest.fail("Completed cohort allocated an executor"))
    result = country.run_plan(parent, plan, path, workers=country.MAX_WORKER_REQUEST, no_browser=True, max_consecutive_failures=20)
    assert result["status"] == "completed" and result["counts"]["ready"] == 8
    assert result["workers"] == result["requested_workers"] == country.MAX_WORKER_REQUEST
    assert result["effective_workers"] == 0 and factory.events == before


def test_held_unknown_and_failed_rows_do_not_increase_effective_pool(
        imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    progress = country._new_progress(plan)
    progress["rows"][0].update(state="unknown", attempts=1, phase="stopped", error_code="interrupted_unknown")
    progress["rows"][1].update(state="failed", attempts=1, phase="stopped", error_code="account_failed")
    progress["unknown_acknowledged"] = True
    country._atomic_json(path, progress)
    install_recovery(monkeypatch, factory)
    result = country.run_plan(parent, plan, path, workers=500, no_browser=True, max_consecutive_failures=20)
    assert result["effective_workers"] == result["counts"]["ready"] == 6
    assert result["counts"]["unknown"] == result["counts"]["failed"] == 1
    assert not any(event[0] == "recover" and event[1] in {1, 2} for event in factory.events)


@pytest.mark.parametrize("tamper", [
    {"requested_workers": 2}, {"requested_workers": True}, {"effective_workers": True},
    {"effective_workers": -1}, {"effective_workers": 9}, {"effective_workers": 2.0},
])
def test_untrusted_requested_effective_checkpoint_fields_reject_without_rewrite(imported, tmp_path, tamper):
    _parent, plan, _source, _factory = imported
    progress = country._new_progress(plan)
    progress.update(no_browser=True, workers=1000, requested_workers=1000, effective_workers=8)
    progress.update(tamper)
    path = tmp_path / "progress.json"
    country._atomic_json(path, progress)
    before = path.read_bytes()
    with pytest.raises(SessionError, match="progress file is invalid"):
        country.load_progress(path, plan)
    assert path.read_bytes() == before


def test_ui_bridge_persists_and_reports_large_requested_count_with_work_clamp(offline_bridge):
    fixture = offline_bridge
    result = invoke(fixture, workers=country.MAX_WORKER_REQUEST)
    initial, called = fixture.run_calls[0]
    assert initial["workers"] == initial["requested_workers"] == called["workers"] == country.MAX_WORKER_REQUEST
    assert initial["effective_workers"] == len(fixture.rows)
    assert result["requested_workers"] == country.MAX_WORKER_REQUEST and result["effective_workers"] == len(fixture.rows)
    assert all(report["effective_workers"] == len(fixture.rows) for report in fixture.published)
    assert result["passed"] is True


def test_cli_large_worker_preview_remains_offline_and_clamps_to_limit(imported, monkeypatch, capsys):
    _parent, _plan, source, factory = imported
    before = list(factory.events)
    assert country.main(["--country", "EG", "--vault", str(factory.path), "--source", str(source),
                         "--no-browser", "--workers", str(country.MAX_WORKER_REQUEST), "--limit", "2"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["requested_workers"] == country.MAX_WORKER_REQUEST and report["effective_workers"] == 2
    assert report["dry_run"] is True and not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events[len(before):])
