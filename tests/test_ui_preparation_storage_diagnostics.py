"""Local save diagnostics cross the console boundary without private text."""

import json

import pytest

from anghami_session.errors import SessionStorageError, safe_session_storage_failure
from anghami_session.ui_jobs import JobManager, _public_report
from anghami_session.ui_server import safe_report


@pytest.mark.parametrize("operation", ["pending_save", "verified_save"])
@pytest.mark.parametrize("sqlite_code", [5, 6, 13, None])
def test_storage_failure_exposes_only_fixed_local_evidence(operation, sqlite_code):
    failure = SessionStorageError(operation=operation, phase="transaction", sqlite_code=sqlite_code, attempts=3)
    failure.args = ("private-session-and-proxy-response",)
    failure.candidate = {"sid": "private-session-and-proxy-response"}
    diagnostic = safe_session_storage_failure(failure)
    error = JobManager._safe_error("prepare", failure, no_browser=True)
    assert error["session_storage_failure"] == diagnostic
    assert "not marked as rejected" in error["message"]
    assert _public_report({"session_storage_failure": diagnostic}) == {"session_storage_failure": diagnostic}
    assert safe_report({"session_storage_failure": diagnostic}) == {"session_storage_failure": diagnostic}
    assert "private-session-and-proxy-response" not in json.dumps(error)


def test_storage_diagnostics_reject_extra_secret_fields():
    diagnostic = safe_session_storage_failure(SessionStorageError(operation="verified_save", phase="encryption"))
    malicious = {**diagnostic, "sid": "private-session-and-proxy-response"}
    assert _public_report({"session_storage_failure": malicious}) == {}
    assert safe_report({"session_storage_failure": malicious}) == {"session_storage_failure": {}}


def test_recent_storage_diagnostics_preserve_rows_and_reject_private_extras():
    diagnostic = safe_session_storage_failure(SessionStorageError(operation="verified_save", phase="transaction", sqlite_code=5, attempts=3))
    rows = [{"source_row": 12, **diagnostic}, {"source_row": 13, **diagnostic, "raw": "private-session-and-proxy-response"}]
    expected = {"recent_session_storage_failures": [rows[0]]}
    assert _public_report({"recent_session_storage_failures": rows}) == expected
    assert safe_report({"recent_session_storage_failures": rows}) == expected
