"""Parallel UI preparation dispatch/progress is checked with offline bridges."""

import json
import sys
import threading
from types import SimpleNamespace

import pytest

from anghami_session import preparation, ui_jobs
from test_ui_jobs import PASSWORD, SESSION_SECRET, finish, make_manager


def report(count, *, prepared=None, pending=(), held=(), active=(), phase="complete"):
    prepared = count - len(pending) - len(held) if prepared is None else prepared
    return {
        "requested_accounts": count, "selected_rows": list(range(21, 21 + count)),
        "prepared_rows": list(range(21, 21 + prepared)), "prepared_account_count": prepared,
        "attempted_accounts": count if phase == "complete" else prepared + len(pending) + len(held),
        "workers": 10, "active_workers": len(active), "active_rows": list(active),
        "connection_pending_count": len(pending), "connection_pending_rows": list(pending),
        "account_failed_count": 0, "account_failed_rows": [],
        "preparation_held": len(held), "attention_required_rows": list(held),
        "passed": prepared == count, "phase": phase,
        "country": "EG", "account_country": "EG", "connection": "proxy_egypt",
        "status": "attention_required" if held else "completed",
        "pause_reason": "unknown_attempt" if held else None,
        "no_browser": True, "browser": "none", "browser_required": False,
        "preparation_method": "http", "play_events_sent": 0, "like_events_sent": 0,
        "password": PASSWORD, "sid": SESSION_SECRET, "validation_candidate": {"sid": SESSION_SECRET},
    }


def install_bridge(monkeypatch, callback):
    monkeypatch.setitem(sys.modules, "anghami_session.ui_preparation", SimpleNamespace(run_parallel_ui_preparation=callback))
    monkeypatch.setattr(preparation, "prepare_test_accounts", lambda *_args, **_options: pytest.fail("Parallel preparation fell through to the serial preparer"))


@pytest.mark.parametrize("country", ["EG", "LB"])
@pytest.mark.parametrize("route", [{}, {"proxy_egypt": True}, {"proxy_sticky_pool": True}])
def test_prepare_ten_workers_dispatches_parallel_bridge_once_and_preserves_controls(tmp_path, monkeypatch, country, route):
    manager, vaults, _proxy, _loads = make_manager(tmp_path)
    calls = []
    def bridge(vault, options, *, job_id, progress, pool_loader):
        calls.append((vault, dict(options), job_id, pool_loader))
        assert callable(progress) and pool_loader is manager._pool_loader
        result = report(options["count"])
        result.update(country=country, account_country=country,
                      connection="proxy_egypt" if route else "direct")
        progress(result)
        return result
    install_bridge(monkeypatch, bridge)
    monkeypatch.setattr(manager, "_parallel_tests", lambda *_args, **_options: pytest.fail("Preparation used engagement test workers"))
    queued = manager.submit({"action": "prepare", "count": 12, "workers": 10,
                             "no_browser": True, "account_country": country, **route})
    result = finish(manager)
    assert len(calls) == 1 and calls[0][0] is vaults[0]
    assert calls[0][1]["workers"] == 10 and calls[0][1]["account_country"] == country
    assert calls[0][1]["no_browser"] is True and calls[0][2] == queued["id"]
    assert calls[0][1]["proxy_egypt"] is bool(route)
    assert result["status"] == "succeeded" and result["workers"] == 10
    assert result["progress"] == {"completed": 12, "total": 12}
    assert result["active_workers"] == 0 and result["active_rows"] == []


def test_parallel_preparation_progress_publishes_activity_and_pending_counts_without_private_material(tmp_path, monkeypatch):
    manager, _vaults, _proxy, _loads = make_manager(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def bridge(_vault, options, *, job_id, progress, pool_loader):
        progress(report(options["count"], prepared=1, pending=(25, 26), held=(27,),
                        active=(22, 23, 24), phase="preparing"))
        entered.set()
        assert release.wait(3), "Offline progress inspection did not release the bridge"
        final = report(options["count"], pending=(25, 26), held=(27,))
        progress(final)
        return final
    install_bridge(monkeypatch, bridge)
    manager.submit({"action": "prepare", "count": 12, "workers": 10,
                    "no_browser": True, "account_country": "EG"})
    try:
        assert entered.wait(3), "Preparation did not invoke the parallel bridge"
        running = manager.snapshot()
        assert running["status"] == "running" and running["phase"] == "preparing"
        assert running["workers"] == 10 and running["active_workers"] == 3
        assert running["active_rows"] == [22, 23, 24]
        assert running["connection_pending"] == 2 and running["account_failed"] == 0
        assert running["preparation_held"] == 1
        assert running["progress"] == {"completed": 1, "total": 12}
        rendered = json.dumps(running) + manager._report_path.read_text()
        assert PASSWORD not in rendered and SESSION_SECRET not in rendered
    finally:
        release.set()
    result = finish(manager)
    assert result["status"] == "completed_with_pending" and result["error"] is None
    assert result["connection_pending"] == 2 and result["preparation_held"] == 1
    assert result["active_workers"] == 0 and result["active_rows"] == []


def test_parallel_preparation_held_accounts_complete_with_pending_instead_of_fatal_job(tmp_path, monkeypatch):
    manager, _vaults, _proxy, _loads = make_manager(tmp_path)
    calls = []
    def bridge(_vault, options, *, job_id, progress, pool_loader):
        calls.append(job_id)
        return report(options["count"], held=(30, 31))
    install_bridge(monkeypatch, bridge)
    queued = manager.submit({"action": "prepare", "count": 12, "workers": 10,
                             "no_browser": True, "account_country": "EG"})
    result = finish(manager)
    assert calls == [queued["id"]]
    assert result["status"] == "completed_with_pending" and result["phase"] == "complete"
    assert result["error"] is None and result["preparation_held"] == 2
    assert result["connection_pending"] == 0 and result["account_failed"] == 0
    assert result["progress"] == {"completed": 10, "total": 12}


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_preparation_resume_requires_boolean_and_rejects_before_job_creation(tmp_path, value):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit({"action": "prepare", "count": 12, "workers": 10,
                        "no_browser": True, "account_country": "EG", "resume_preparation": value})
    assert factories == [] and loads == []
    assert manager.snapshot() is None and manager._thread is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("payload", [
    {"action": "preview", "no_browser": True, "account_country": "EG"},
    {"action": "prepare", "no_browser": True, "account_country": "EG", "workers": 1},
    {"action": "prepare", "workers": 10, "account_country": "EG"},
    {"action": "prepare", "no_browser": True, "workers": 10},
    {"action": "prepare", "no_browser": True, "workers": 10, "account_country": "EG", "start_row": 1},
    {"action": "prepare", "no_browser": True, "workers": 10, "account_country": "EG", "review_rows": [31]},
    {"action": "prepare", "no_browser": True, "workers": 10, "account_country": "EG", "rows": [31]},
    {"action": "play", "rows": [7]},
])
def test_preparation_resume_cannot_escape_parallel_country_scope(payload):
    with pytest.raises(ui_jobs.JobValidationError):
        ui_jobs._validate({**payload, "resume_preparation": True})

