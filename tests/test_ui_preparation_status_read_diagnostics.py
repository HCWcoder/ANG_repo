"""Preparation reports distinguish local status reads from account failures."""

import json
import sys
from types import SimpleNamespace

import pytest

from anghami_session import country_preparation as country, ui_jobs, ui_server
from test_ui_jobs import finish, make_manager


def diagnostic():
    return {"code": "ui_file_unreadable", "stage": "read", "attempts": country._UI_READ_MAX_ATTEMPTS,
            "errno": 13, "winerror": 32}


def test_exhausted_status_read_is_reported_without_account_failure_or_retry(tmp_path, monkeypatch):
    calls = []
    def bridge(_vault, options, **_kwargs):
        calls.append(options["count"])
        return {"phase": "stopped", "status": "paused", "pause_reason": "ui_unavailable",
                "ui_read_failure": diagnostic(), "selected_rows": [21, 22], "prepared_rows": [21],
                "requested_accounts": 2, "prepared_account_count": 1, "attempted_accounts": 1,
                "account_failed_count": 0, "connection_pending_count": 0, "preparation_held": 0,
                "workers": 30, "active_workers": 0, "active_rows": [], "passed": False,
                "no_browser": True, "play_events_sent": 0, "like_events_sent": 0}
    monkeypatch.setitem(sys.modules, "anghami_session.ui_preparation", SimpleNamespace(run_parallel_ui_preparation=bridge))
    manager, *_ = make_manager(tmp_path)
    manager.submit({"action": "prepare", "count": 2, "account_country": "EG", "workers": 30, "no_browser": True})
    job = finish(manager)
    assert calls == [2]
    assert job["status"] == "failed" and job["error"]["code"] == "ui_status_unavailable"
    assert job["error"]["ui_read_failure"] == diagnostic()
    assert "failure limit was not reached" in job["error"]["message"]
    assert job["progress"] == {"completed": 1, "total": 2} and job["account_failed"] == 0
    assert job["results"][0]["pause_reason"] == "ui_unavailable"
    assert job["results"][0]["ui_read_failure"] == diagnostic()
    assert ui_server.safe_report(job["error"])["code"] == "ui_status_unavailable"


def test_safe_status_diagnostics_survive_both_console_boundaries():
    report = {"ui_read_failure": diagnostic(), "pause_reason": "ui_unavailable"}
    assert ui_jobs._public_report(report) == report
    assert ui_server.safe_report(report) == report


@pytest.mark.parametrize("extra", [{"path": "private-credentials"}, {"response": "private-credentials"}, {"sid": "private-credentials"}])
def test_status_diagnostic_rejects_arbitrary_private_fields(extra):
    report = {"ui_read_failure": {**diagnostic(), **extra}}
    assert ui_jobs._public_report(report) == {}
    assert ui_server.safe_report(report) == {"ui_read_failure": {}}
    assert "private-credentials" not in json.dumps(ui_server.safe_report(report))
