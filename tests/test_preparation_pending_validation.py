"""Confirmed renewal candidates are preserved without replaying renewal."""

from copy import deepcopy
import sys
from types import SimpleNamespace

import pytest

from anghami_session import preparation
from anghami_session.errors import RequestFailure, SessionError
from anghami_session.proxy import ProxyCountryError
from test_account_preparation import FakeProxy, PreparationVault, SAVED, safe_report


class PendingVault(PreparationVault):
    def __init__(self, path, *, transient_failures=0, store_failure=False, failure_factory=None):
        super().__init__(path, rows=(8, 9), sessions={9: SAVED})
        self.pending = {}
        self.attach_attempts = 0
        self.transient_failures = transient_failures
        self.store_failure = store_failure
        self.failure_factory = failure_factory

    def pending_session(self, row):
        self.events.append(("pending_lookup", row))
        return deepcopy(self.pending.get(row))

    def save_pending_session(self, row, saved):
        self.events.append(("pending_save", row))
        if self.store_failure:
            raise SessionError("The pending candidate could not be saved securely.")
        self.pending[row] = deepcopy(saved)

    def attach(self, row, saved, **options):
        self.events.append(("attach", row, deepcopy(saved), options))
        if row == 8:
            self.attach_attempts += 1
            if self.attach_attempts <= self.transient_failures:
                if self.failure_factory is not None:
                    raise self.failure_factory()
                raise RequestFailure("request_transport_failed", stage="negative_control", curl_code=7)
        self.sessions[row] = deepcopy(saved)
        self.pending.pop(row, None)
        return {"source_row": row, "verified": True}


def install_recovery(monkeypatch, calls, *, failure=None):
    def recover(record, *, proxy=None):
        calls.append(proxy)
        if failure is not None:
            raise failure
        return deepcopy(SAVED), {"session_renewed": True, "identity_verified": True,
                                 "session_validation_attempts": 1, "session_validation_retries": 0}
    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=recover))


def test_attach_read_failures_retry_exact_candidate_and_proxy_without_recovery_replay(tmp_path, monkeypatch):
    vault = PendingVault(tmp_path / "accounts.sqlite3", transient_failures=2)
    calls = []
    install_recovery(monkeypatch, calls)
    proxy = FakeProxy()
    result = preparation.prepare_test_accounts(vault, count=2, no_browser=True, proxy=proxy)
    assert result["passed"] is True and result["prepared_rows"] == [8, 9]
    assert result["attach_validation_attempts"] == 3 and result["attach_validation_retries"] == 2
    assert calls == [proxy] and vault.attach_attempts == 3
    assert result["connection_pending_rows"] == result["account_failed_rows"] == []
    attempts = [event for event in vault.events if event[0] == "attach" and event[1] == 8]
    assert all(event[2] == SAVED and event[3] == {"proxy": proxy} for event in attempts)
    assert vault.pending == {} and sum(event == ("enable", 8) for event in vault.events) == 1
    safe_report(vault)


def test_exhausted_attach_validation_preserves_candidate_continues_and_next_run_only_validates(tmp_path, monkeypatch):
    vault = PendingVault(tmp_path / "accounts.sqlite3", transient_failures=3)
    calls = []
    install_recovery(monkeypatch, calls)
    result = preparation.prepare_test_accounts(vault, count=2, no_browser=True)
    assert result["phase"] == "complete" and result["passed"] is False
    assert result["connection_pending_rows"] == [8] and result["prepared_rows"] == [9]
    assert result["account_failed_rows"] == [] and vault.failure_review()["failed_rows"] == []
    assert result["validation_pending"] is True and result["renewal_completed"] is True
    assert result.get("renewal_unknown") is not True and vault.pending[8] == SAVED
    assert ("enable", 8) not in vault.events and ("enable", 9) in vault.events
    assert calls == [None] and vault.attach_attempts == 3
    safe_report(vault)
    vault.rows = [8]
    second = preparation.prepare_test_accounts(vault, count=1, no_browser=True)
    assert second["passed"] is True and second["prepared_rows"] == [8]
    assert calls == [None] and vault.attach_attempts == 4
    assert vault.pending == {} and ("enable", 8) in vault.events


def test_exhausted_recovery_reads_store_private_candidate_without_serializing_it(tmp_path, monkeypatch):
    vault = PendingVault(tmp_path / "accounts.sqlite3")
    failure = RequestFailure("request_transport_failed", stage="negative_control", curl_code=7, retry_safe=False)
    failure.validation_candidate = deepcopy(SAVED)
    failure.renewal_completed = failure.validation_pending = True
    failure.validation_read_attempts = 3
    calls = []
    install_recovery(monkeypatch, calls, failure=failure)
    result = preparation.prepare_test_accounts(vault, count=2, no_browser=True)
    assert result["connection_pending_rows"] == [8] and result["prepared_rows"] == [9]
    assert result["session_validation_attempts"] == 3 and result["session_validation_retries"] == 2
    assert calls == [None] and vault.pending[8] == SAVED
    assert result["account_failed_rows"] == [] and ("enable", 8) not in vault.events
    safe_report(vault)


def test_pending_persistence_error_holds_instead_of_losing_candidate_and_continuing(tmp_path, monkeypatch):
    vault = PendingVault(tmp_path / "accounts.sqlite3", transient_failures=3, store_failure=True)
    calls = []
    install_recovery(monkeypatch, calls)
    with pytest.raises(SessionError, match="saved securely"):
        preparation.prepare_test_accounts(vault, count=2, no_browser=True)
    result = safe_report(vault)
    assert result["phase"] == "stopped" and result["connection_pending_rows"] == []
    assert ("enable", 8) not in vault.events and ("enable", 9) not in vault.events
    assert calls == [None]


def test_unknown_renewal_still_holds_and_never_claims_candidate_or_ready_account(tmp_path, monkeypatch):
    vault = PendingVault(tmp_path / "accounts.sqlite3")
    failure = RequestFailure("request_transport_failed", stage="identity", curl_code=7, retry_safe=False)
    failure.renewal_unknown = True
    calls = []
    install_recovery(monkeypatch, calls, failure=failure)
    with pytest.raises(RequestFailure):
        preparation.prepare_test_accounts(vault, count=2, no_browser=True)
    result = safe_report(vault)
    assert result["renewal_unknown"] is True and result["prepared_rows"] == []
    assert result["connection_pending_rows"] == [] and vault.pending == {}
    assert calls == [None] and not any(event[0] == "enable" for event in vault.events)


@pytest.mark.parametrize("failures", [2, 3])
def test_country_read_after_known_renewal_retries_or_preserves_candidate_without_bootstrap_replay(tmp_path, monkeypatch, failures):
    vault = PendingVault(tmp_path / "accounts.sqlite3", transient_failures=failures,
                         failure_factory=lambda: ProxyCountryError("transport_error", curl_code=7))
    calls = []
    install_recovery(monkeypatch, calls)
    proxy = FakeProxy()
    result = preparation.prepare_test_accounts(vault, count=2, no_browser=True, proxy=proxy)
    assert result["phase"] == "complete" and calls == [proxy]
    assert vault.attach_attempts == 3 and result["account_failed_rows"] == []
    assert result.get("renewal_unknown") is not True
    if failures == 2:
        assert result["prepared_rows"] == [8, 9] and vault.pending == {}
    else:
        assert result["prepared_rows"] == [9] and result["connection_pending_rows"] == [8]
        assert vault.pending[8] == SAVED and ("enable", 8) not in vault.events
    safe_report(vault)
