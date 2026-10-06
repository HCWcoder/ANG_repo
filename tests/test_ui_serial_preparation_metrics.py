"""Serial browser/HTTP preparation reports actual activity using offline fakes."""

import threading

import pytest

from anghami_session import preparation, ui_jobs
from anghami_session.errors import SessionError
from test_ui_jobs import finish, make_manager


@pytest.mark.parametrize("no_browser", [False, True])
@pytest.mark.parametrize("outcome", ["complete", "stopped"])
def test_serial_preparation_publishes_selected_capacity_and_active_row_then_clears_activity(tmp_path, monkeypatch, no_browser, outcome):
    manager, _vaults, _proxy, _loads = make_manager(tmp_path)
    started, release = threading.Event(), threading.Event()
    calls = []
    def prepare(_vault, **options):
        calls.append(options)
        initial = {"selected_rows": [7, 8], "requested_accounts": 2, "prepared_account_count": 0,
                   "attempted_accounts": 0, "phase": "preparing", "passed": False}
        options["progress"](initial)
        selected = manager.snapshot()
        assert selected["effective_workers"] == 1 and selected["active_workers"] == 0
        options["progress"]({**initial, "active_row": 7, "attempted_accounts": 1,
                             "phase": "session_recovery" if no_browser else "login"})
        started.set()
        assert release.wait(3)
        return {**initial, "active_row": 8, "attempted_accounts": 2, "phase": outcome,
                "prepared_account_count": 2 if outcome == "complete" else 0,
                "passed": outcome == "complete"}
    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    monkeypatch.setattr(manager, "_parallel_tests", lambda *_args: pytest.fail("Serial preparation started test workers"))
    queued = manager.submit({"action": "prepare", "count": 2, "no_browser": no_browser})
    try:
        assert started.wait(3)
        running = manager.snapshot()
        assert queued["effective_workers"] == 0
        assert running["requested_workers"] == running["workers"] == running["effective_workers"] == 1
        assert running["active_workers"] == 1 and running["active_rows"] == [7]
        assert "1 of 1 worker(s) active" in running["message"]
    finally:
        release.set()
    result = finish(manager)
    assert len(calls) == 1 and calls[0].get("no_browser", False) is no_browser
    assert "account_country" not in calls[0] and "selected_rows" not in calls[0]
    assert result["effective_workers"] == 1 and result["active_workers"] == 0 and result["active_rows"] == []
    assert result["status"] == ("succeeded" if outcome == "complete" else "failed")
    assert result["results"][0]["effective_workers"] == 1 and result["results"][0]["active_workers"] == 0


@pytest.mark.parametrize("no_browser", [False, True])
def test_no_serial_candidates_retains_zero_effective_workers(tmp_path, monkeypatch, no_browser):
    manager, _vaults, _proxy, _loads = make_manager(tmp_path)
    def no_candidates(*_args, **_options):
        raise SessionError("Synthetic registered source has no eligible candidates")
    monkeypatch.setattr(preparation, "prepare_test_accounts", no_candidates)
    manager.submit({"action": "prepare", "count": 1, "no_browser": no_browser})
    job = finish(manager)
    assert job["status"] == "failed"
    assert job["requested_workers"] == 1 and job["effective_workers"] == 0
    assert job["active_workers"] == 0 and job["active_rows"] == []


@pytest.mark.parametrize("report", [
    {"selected_rows": [], "phase": "preparing", "active_row": 7},
    {"selected_rows": [True], "phase": "login", "active_row": 1},
    {"selected_rows": [7], "phase": "complete", "attempted_accounts": 0, "active_row": 7},
    {"selected_rows": [7], "phase": "stopped", "attempted_accounts": True, "active_row": 7},
])
def test_serial_capacity_requires_selection_and_terminal_attempt_evidence(report):
    metrics = ui_jobs.JobManager._serial_preparation_metrics(report)
    assert metrics["workers"] == metrics["requested_workers"] == 1
    assert metrics["effective_workers"] == 0 and metrics["active_workers"] == 0 and metrics["active_rows"] == []


@pytest.mark.parametrize("row,phase", [(8, "login"), (True, "login"), (7, "stopped"), (7, "complete"), (7, "unexpected"),
                                       (7, "preview"), (7, "queued"), (7, [])])
def test_serial_activity_requires_known_selected_row_and_active_phase(row, phase):
    metrics = ui_jobs.JobManager._serial_preparation_metrics({"selected_rows": [7], "active_row": row,
                                                            "attempted_accounts": 1, "phase": phase})
    assert metrics["active_workers"] == 0 and metrics["active_rows"] == []
