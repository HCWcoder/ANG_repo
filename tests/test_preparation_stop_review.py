"""Independent Stop preparation review uses only disposable offline jobs."""

import json
import threading
from types import SimpleNamespace

import pytest

from anghami_session import country_preparation as country, ui_jobs, ui_server
from test_ui_jobs import finish, make_manager
from test_ui_parallel_preparation_integration import install_bridge, report


PRIVATE = "synthetic-private-stop https://private.invalid/?sid=synthetic"


def stop_report(count, *, prepared=1, reason="stop_requested"):
    value = report(count, prepared=prepared, phase="stopped")
    value.update(status="paused", pause_reason=reason, passed=False, active_workers=0, active_rows=[])
    return value


@pytest.mark.parametrize("capability", [None, False, 0, 1, "true", {}, True])
def test_preparation_stop_capability_requires_explicit_boolean_true(tmp_path, capability):
    manager = SimpleNamespace(snapshot=lambda: None, supports_preparation_stop=capability)
    service = ui_server.ConsoleService(tmp_path / "absent.sqlite3", manager=manager)
    assert service.state()["job_controls"] == {"stop_preparation": capability is True}


def test_legacy_manager_has_no_preparation_stop_capability(tmp_path):
    manager = SimpleNamespace(snapshot=lambda: None)
    service = ui_server.ConsoleService(tmp_path / "absent.sqlite3", manager=manager)
    assert service.state()["job_controls"] == {"stop_preparation": False}


def test_stopped_report_is_terminal_for_cli_and_cannot_resume_its_old_owner(tmp_path):
    path = tmp_path / "ui-last-job.json"
    owner = "a" * 32
    path.write_text(json.dumps({"id": owner, "status": "stopped"}), encoding="utf-8")
    assert country._ui_state(path) == (owner, None)
    assert country._owned_ui_state(path, owner) == (owner, "ui_activity")


def test_stop_before_checkpoint_preserves_progress_and_cannot_stop_the_next_job(tmp_path, monkeypatch):
    manager, _vaults, _proxy, _loads = make_manager(tmp_path)
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = []

    def bridge(_vault, options, *, job_id, progress, pool_loader):
        number = len(calls)
        calls.append(job_id)
        marker = tmp_path / f"ui-preparation-progress-{job_id}.stop"
        assert not (tmp_path / f"ui-preparation-progress-{job_id}.json").exists()
        assert not marker.exists()
        assert not manager._stop_requested.is_set()
        entered[number].set()
        assert release[number].wait(3)
        if number == 0:
            assert marker.exists() and manager._stop_requested.is_set()
            return stop_report(options["count"])
        assert not marker.exists() and not manager._stop_requested.is_set()
        return report(options["count"])

    install_bridge(monkeypatch, bridge)
    payload = {"action": "prepare", "count": 2, "workers": 2, "account_country": "EG", "no_browser": True}
    first = manager.submit(payload)
    try:
        assert entered[0].wait(3)
        requested = manager.request_stop(first["id"])
        assert requested["status"] == "running" and requested["stop_requested"] is True
        marker = tmp_path / f"ui-preparation-progress-{first['id']}.stop"
        assert json.loads(marker.read_text(encoding="utf-8"))["job_id"] == first["id"]
    finally:
        release[0].set()
    stopped = finish(manager)
    assert stopped["status"] == "stopped" and stopped["stop_reason"] == "user_stop"
    assert stopped["error"] is None and stopped["progress"] == {"completed": 1, "total": 2}
    assert stopped["active_workers"] == 0 and stopped["active_rows"] == []

    second = manager.submit(payload)
    try:
        assert second["id"] != first["id"] and entered[1].wait(3)
        with pytest.raises(ui_jobs.JobValidationError):
            manager.request_stop(first["id"])
        assert not manager._stop_requested.is_set()
        assert not (tmp_path / f"ui-preparation-progress-{second['id']}.stop").exists()
    finally:
        release[1].set()
    completed = finish(manager)
    assert completed["status"] == "succeeded" and completed["stop_requested"] is False
    assert completed["progress"] == {"completed": 2, "total": 2}


def seeded_running_manager(tmp_path):
    manager, _vaults, _proxy, _loads = make_manager(tmp_path)
    manager._latest = {
        "id": "a" * 32, "action": "prepare", "status": "running",
        "stop_requested": False, "progress": {"completed": 0, "total": 2},
    }
    return manager


def test_failed_stop_marker_write_does_not_falsely_claim_dispatch_stopped(tmp_path, monkeypatch):
    manager = seeded_running_manager(tmp_path)

    def fail_journal(_value, _path):
        raise OSError(PRIVATE)

    monkeypatch.setattr(ui_jobs, "_journal", fail_journal)
    with pytest.raises(ui_jobs.JobValidationError) as failure:
        manager.request_stop("a" * 32)
    assert PRIVATE not in str(failure.value)
    assert manager.snapshot()["stop_requested"] is False and not manager._stop_requested.is_set()
    assert not (tmp_path / f"ui-preparation-progress-{'a' * 32}.stop").exists()


def test_failed_ui_report_after_stop_keeps_durable_marker_and_in_memory_stop(tmp_path, monkeypatch):
    manager = seeded_running_manager(tmp_path)

    def fail_update(**_fields):
        raise OSError(PRIVATE)

    monkeypatch.setattr(manager, "_update", fail_update)
    with pytest.raises(ui_jobs.JobValidationError) as failure:
        manager.request_stop("a" * 32)
    assert PRIVATE not in str(failure.value)
    assert manager.snapshot()["stop_requested"] is True and manager._stop_requested.is_set()
    marker = tmp_path / f"ui-preparation-progress-{'a' * 32}.stop"
    assert json.loads(marker.read_text(encoding="utf-8"))["job_id"] == "a" * 32


@pytest.mark.parametrize("reason", ["scope_mismatch", "unknown_attempt"])
def test_stop_does_not_hide_a_concurrent_preparation_safety_hold(tmp_path, monkeypatch, reason):
    manager, _vaults, _proxy, _loads = make_manager(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def bridge(_vault, options, *, job_id, progress, pool_loader):
        entered.set()
        assert release.wait(3)
        value = stop_report(options["count"], reason=reason)
        value["status"] = "attention_required"
        return value

    install_bridge(monkeypatch, bridge)
    queued = manager.submit({"action": "prepare", "count": 2, "workers": 2,
                             "account_country": "EG", "no_browser": True})
    try:
        assert entered.wait(3)
        manager.request_stop(queued["id"])
    finally:
        release.set()
    stopped = finish(manager)
    assert stopped["status"] == "failed" and stopped["error"]["code"] == "preparation_failed"
    assert stopped["progress"] == {"completed": 1, "total": 2}
