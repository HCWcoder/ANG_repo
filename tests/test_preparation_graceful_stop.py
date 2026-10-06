"""Serial preparation stops only at safe account or prewrite retry boundaries."""

import sys
from threading import Event
from types import SimpleNamespace

import pytest

from anghami_session import preparation
from anghami_session.errors import RequestFailure, SessionError
from anghami_session.play_record import _Failure
from test_account_preparation import PreparationVault, SAVED, fake_capture, safe_report
from test_preparation_pending_validation import PendingVault


@pytest.mark.parametrize("invalid", [True, False, "stop", 1, object()])
def test_noncallable_stop_gate_rejected_before_selection(tmp_path, invalid):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    with pytest.raises(SessionError, match="stop check must be callable"):
        preparation.prepare_test_accounts(vault, count=1, should_stop=invalid)
    assert vault.events == [] and list(tmp_path.iterdir()) == []


def test_stop_before_first_account_does_not_lookup_sessions_or_allocate_routes(tmp_path):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    routes = []
    report = preparation.prepare_test_accounts(vault, count=2, no_browser=True,
                                               proxy_factory=lambda row: routes.append(row), should_stop=lambda: True)
    assert vault.events == [("select", 2, 1)] and routes == []
    assert report["selected_rows"] == [8, 9] and report["attempted_accounts"] == 0
    assert report["prepared_rows"] == [] and report["active_row"] is None
    assert report["phase"] == "stopped" and report["pause_reason"] == "stop_requested"
    assert report["passed"] is False and report["stop_requested"] is True and safe_report(vault) == report


def test_browser_inflight_account_finishes_attachment_enrollment_and_report_before_stop(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    fake_capture(monkeypatch, vault)
    stop = Event()
    reports = []
    def progress(report):
        reports.append(report)
        if report["phase"] == "validation":
            stop.set()
    result = preparation.prepare_test_accounts(vault, count=2, progress=progress, should_stop=stop.is_set)
    assert [event[0] for event in vault.events] == ["select", "session", "record", "capture", "attach", "enable"]
    assert result["prepared_rows"] == [8] and result["attempted_accounts"] == 1
    assert result["prepared_account_count"] == 1 and result["selected_rows"] == [8, 9]
    assert result["phase"] == "stopped" and result["pause_reason"] == "stop_requested" and result["active_row"] is None
    assert result["account_failed_rows"] == result["connection_pending_rows"] == []
    assert (tmp_path / "account-8.login-request.redacted.json").exists()
    assert reports[-1] == safe_report(vault) == result


def test_issued_http_candidate_finishes_validation_without_recovery_replay_when_stop_arrives(tmp_path, monkeypatch):
    vault = PendingVault(tmp_path / "synthetic.sqlite3")
    stop = Event()
    recoveries = []
    def recover(record, *, proxy=None):
        recoveries.append(proxy)
        stop.set()
        return SAVED, {"session_renewed": True}
    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=recover))
    report = preparation.prepare_test_accounts(vault, count=2, no_browser=True, should_stop=stop.is_set)
    assert recoveries == [None] and vault.attach_attempts == 1 and vault.pending == {}
    assert report["prepared_rows"] == [8] and report["attempted_accounts"] == 1
    assert report["pause_reason"] == "stop_requested" and ("enable", 8) in vault.events and ("enable", 9) not in vault.events


def test_stop_after_prewrite_provider_failure_preserves_pending_and_does_not_rotate(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    stop = Event()
    recovery_calls, routes = [], []
    class Route:
        def summary(self):
            return {"country": "EG"}
    def recover(record, *, proxy=None):
        recovery_calls.append(proxy)
        stop.set()
        raise RequestFailure("request_transport_failed", stage="session_recovery_preflight", curl_code=28)
    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=recover))
    def route(row):
        routes.append(row)
        return Route()
    result = preparation.prepare_test_accounts(vault, count=2, no_browser=True, proxy_factory=route, should_stop=stop.is_set)
    assert routes == [8] and len(recovery_calls) == 1
    assert result["connection_pending_rows"] == [8] and result["account_failed_rows"] == []
    assert result["provider_failure"]["curl_code"] == 28
    assert result["phase"] == "stopped" and result["pause_reason"] == "stop_requested" and result["attempted_accounts"] == 1


def test_stop_in_cooldown_uses_bound_stop_gate_without_a_new_recovery_or_route(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    stop = Event()
    recoveries, waits, routes = [], [], []
    class Route:
        def summary(self):
            return {"country": "EG"}
    def recover(record, *, proxy=None):
        recoveries.append(proxy)
        raise RequestFailure("request_rate_limited", stage="session_recovery_preflight", http_status=429)
    def wait(failure, bound_stop):
        waits.append(failure)
        assert bound_stop.is_set() is False
        stop.set()
        assert bound_stop.is_set() is True
        return False
    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=recover))
    monkeypatch.setattr(preparation, "wait_for_provider", wait)
    result = preparation.prepare_test_accounts(vault, count=2, no_browser=True,
                                               proxy_factory=lambda row: routes.append(row) or Route(), should_stop=stop.is_set)
    assert routes == [8] and len(recoveries) == len(waits) == 1
    assert result["connection_pending_rows"] == [8] and result["pause_reason"] == "stop_requested"
    assert result["provider_failure"]["http_status"] == 429


def test_unknown_renewal_keeps_error_precedence_even_if_stop_was_requested(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    stop = Event()
    error = RequestFailure("request_transport_failed", stage="identity", curl_code=28, retry_safe=False)
    error.renewal_unknown = True
    def recover(record, *, proxy=None):
        stop.set()
        raise error
    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=recover))
    with pytest.raises(RequestFailure) as failure:
        preparation.prepare_test_accounts(vault, count=2, no_browser=True, should_stop=stop.is_set)
    assert failure.value is error
    report = safe_report(vault)
    assert report["renewal_unknown"] is True and report["prepared_rows"] == []
    assert report.get("pause_reason") != "stop_requested" and report.get("stop_requested") is not True


def test_stop_report_journal_failure_keeps_fatal_error_and_committed_account(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3", sessions={8: SAVED, 9: SAVED})
    stop = Event()
    journal = preparation._journal
    def progress(report):
        if report["prepared_rows"] == [8]:
            stop.set()
    def fail_stop_report(report, path):
        if report.get("pause_reason") == "stop_requested":
            raise _Failure("journal_failed", "Synthetic fixed report failure")
        return journal(report, path)
    monkeypatch.setattr(preparation, "_journal", fail_stop_report)
    with pytest.raises(_Failure) as failure:
        preparation.prepare_test_accounts(vault, count=2, progress=progress, should_stop=stop.is_set)
    assert failure.value.code == "journal_failed" and ("enable", 8) in vault.events and ("enable", 9) not in vault.events
    assert safe_report(vault)["prepared_rows"] == [8]


@pytest.mark.parametrize("invalid_result", [1, None, "yes"])
def test_nonboolean_stop_result_fails_closed_before_account_requests(tmp_path, invalid_result):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    with pytest.raises(SessionError, match="must return true or false"):
        preparation.prepare_test_accounts(vault, count=1, should_stop=lambda: invalid_result)
    assert vault.events == [("select", 1, 1)] and safe_report(vault)["prepared_rows"] == []
