"""Bound preparation stops persist intent and drain work using offline jobs."""

from copy import deepcopy
import json
import threading

import pytest

from anghami_session import preparation, ui_jobs, ui_preparation
from anghami_session.play_record import _Failure
from test_ui_jobs import finish, make_manager


JOB_ID = "a" * 32


def seed(manager, *, status="running", action="prepare"):
    manager._latest = {"id": JOB_ID, "action": action, "status": status,
                       "progress": {"completed": 0, "total": 5}, "stop_requested": False,
                       "message": "Synthetic local preparation.", "error": None}
    return deepcopy(manager._latest)


def marker(manager, job_id=JOB_ID):
    return manager.vault_path.parent / f"ui-preparation-progress-{job_id}.stop"


def test_exact_preparation_stop_marker_is_durable_before_report_ack(tmp_path, monkeypatch):
    manager, *_ = make_manager(tmp_path)
    seed(manager)
    journal = ui_jobs._journal
    writes = []

    def ordered(report, path):
        writes.append(path)
        if path == marker(manager):
            assert not manager._stop_requested.is_set() and not manager._latest["stop_requested"]
        elif path == manager._report_path:
            assert marker(manager).exists() and manager._stop_requested.is_set()
        return journal(report, path)

    monkeypatch.setattr(ui_jobs, "_journal", ordered)
    stopped = manager.request_stop(JOB_ID)
    assert writes == [marker(manager), manager._report_path]
    assert stopped["status"] == "running" and stopped["stop_requested"] is True
    assert manager.supports_preparation_stop is True
    assert json.loads(marker(manager).read_text())["job_id"] == JOB_ID
    assert not (tmp_path / f"ui-preparation-progress-{JOB_ID}.json").exists()


@pytest.mark.parametrize("requested", [None, True, 0, [], {}, "b" * 32, "../" + "a" * 29, "A" * 32])
def test_stale_or_invalid_preparation_stop_never_changes_job_or_marks_another_file(tmp_path, requested):
    manager, *_ = make_manager(tmp_path)
    before = seed(manager)
    with pytest.raises(ui_jobs.JobValidationError):
        manager.request_stop(requested)
    assert manager.snapshot() == before and not manager._stop_requested.is_set()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", ["succeeded", "failed", "stopped", "completed_with_pending"])
def test_terminal_preparation_stop_is_noop(tmp_path, status):
    manager, *_ = make_manager(tmp_path)
    before = seed(manager, status=status)
    assert manager.request_stop(JOB_ID) == before
    assert not manager._stop_requested.is_set() and list(tmp_path.iterdir()) == []


def test_failed_stop_marker_is_not_acknowledged_and_never_leaks_exception_text(tmp_path, monkeypatch):
    manager, *_ = make_manager(tmp_path)
    before = seed(manager)
    monkeypatch.setattr(ui_jobs, "_journal", lambda *_: (_ for _ in ()).throw(_Failure("journal_failed", "synthetic private proxy response")))
    with pytest.raises(ui_jobs.JobValidationError, match="could not be saved") as caught:
        manager.request_stop(JOB_ID)
    assert "synthetic private proxy response" not in str(caught.value)
    assert manager.snapshot() == before and not manager._stop_requested.is_set()
    assert not marker(manager).exists()


def test_failed_ack_report_keeps_durable_stop_and_dispatch_event(tmp_path, monkeypatch):
    manager, *_ = make_manager(tmp_path)
    seed(manager)
    journal = ui_jobs._journal

    def failed_report(report, path):
        if path == manager._report_path:
            raise _Failure("journal_failed", "synthetic private proxy response")
        return journal(report, path)

    monkeypatch.setattr(ui_jobs, "_journal", failed_report)
    with pytest.raises(ui_jobs.JobValidationError, match="Dispatch stopped"):
        manager.request_stop(JOB_ID)
    assert marker(manager).exists() and manager._stop_requested.is_set()
    assert manager.snapshot()["stop_requested"] is True


class DeferredThread:
    def __init__(self, *, target, args, **_options):
        self.target, self.args, self.ran = target, args, False

    def start(self):
        pass

    def join(self, *_):
        if not self.ran:
            self.ran = True
            self.target(*self.args)

    def is_alive(self):
        return False


def test_queued_stop_prevents_vault_or_proxy_loading_and_does_not_stop_next_job(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_jobs.threading, "Thread", DeferredThread)
    manager, factories, _proxy, loads = make_manager(tmp_path)
    queued = manager.submit({"action": "prepare", "count": 5, "proxy_egypt": True})
    stopped = manager.request_stop(queued["id"])
    assert stopped["status"] == "queued"
    job = finish(manager)
    assert job["status"] == "stopped" and job["stop_reason"] == "user_stop" and job["error"] is None
    assert factories == loads == []
    assert marker(manager, queued["id"]).exists()
    monkeypatch.setattr(preparation, "prepare_test_accounts", lambda *_a, **_k: {
        "passed": True, "phase": "complete", "prepared_account_count": 1,
    })
    following = manager.submit({"action": "prepare"})
    assert following["id"] != queued["id"] and following["stop_requested"] is False
    assert not manager._stop_requested.is_set() and not marker(manager, following["id"]).exists()
    with pytest.raises(ui_jobs.JobValidationError):
        manager.request_stop(queued["id"])
    assert not manager._stop_requested.is_set()
    assert finish(manager)["status"] == "succeeded"


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("failure", [None, "unknown_attempt", "journal_failed"])
def test_manual_stop_finishes_current_preparation_and_preserves_fatal_outcomes(tmp_path, monkeypatch, parallel, failure):
    entered, release, drained = threading.Event(), threading.Event(), threading.Event()
    manager, *_ = make_manager(tmp_path)

    def prepare(_vault, *args, **options):
        if not parallel:
            assert callable(options["should_stop"]) and not options["should_stop"]()
        entered.set()
        assert release.wait(3)
        assert manager._stop_requested.is_set()
        if parallel:
            assert marker(manager, manager.snapshot()["id"]).exists()
        else:
            assert options["should_stop"]()
        drained.set()
        if failure == "journal_failed":
            raise _Failure("journal_failed", "synthetic private preparation report")
        return {"passed": False, "phase": "stopped", "pause_reason": failure or "stop_requested",
                "prepared_account_count": 2, "requested_accounts": 5,
                "selected_rows": [7, 8, 9, 10, 11], "prepared_rows": [7, 8],
                "connection_pending_count": 0, "account_failed_count": 0,
                "preparation_held": int(failure == "unknown_attempt"), "active_workers": 0, "active_rows": []}

    if parallel:
        monkeypatch.setattr(ui_preparation, "run_parallel_ui_preparation", prepare)
    else:
        monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    queued = manager.submit({"action": "prepare", "count": 5, "no_browser": True,
                             "account_country": "EG", "workers": 3 if parallel else 1})
    assert entered.wait(3)
    requested = manager.request_stop(queued["id"])
    assert requested["status"] == "running" and not drained.is_set()
    release.set()
    job = finish(manager)
    assert drained.is_set() and job["stop_requested"] is True and job["active_workers"] == 0
    if failure:
        assert job["status"] == "failed" and job["error"]["code"] == ("journal_failed" if failure == "journal_failed" else "preparation_failed")
    else:
        assert job["status"] == "stopped" and job["stop_reason"] == "user_stop" and job["error"] is None
        assert job["progress"] == {"completed": 2, "total": 5}
        assert job["results"][0]["prepared_rows"] == [7, 8]


def test_unrequested_parallel_stop_is_not_reported_as_successful_manual_cancellation(tmp_path, monkeypatch):
    manager, *_ = make_manager(tmp_path)
    monkeypatch.setattr(ui_preparation, "run_parallel_ui_preparation", lambda *_a, **_k: {
        "passed": False, "phase": "stopped", "pause_reason": "stop_requested", "prepared_account_count": 0,
    })
    manager.submit({"action": "prepare", "count": 5, "account_country": "EG", "no_browser": True, "workers": 2})
    job = finish(manager)
    assert job["status"] == "failed" and job["error"]["code"] == "preparation_failed"
