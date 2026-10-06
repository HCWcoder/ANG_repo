"""Local pending-session failures hold synthetic preparation without replay."""

from copy import deepcopy
import sys
from types import SimpleNamespace

import pytest

from anghami_session import country_preparation as country
from anghami_session import preparation
from anghami_session.errors import RequestFailure, SessionError
from test_country_preparation_workers import (
    PRIVATE_ERROR, PRIVATE_PASSWORD, SyntheticVault, assert_safe, imported, row_from,
)
from test_preparation_pending_validation import PendingVault, install_recovery
from test_account_preparation import safe_report


@pytest.mark.parametrize("private_error", [False, True])
def test_local_pending_load_error_stops_before_old_session_or_renewal(tmp_path, monkeypatch, private_error):
    vault = PendingVault(tmp_path / "accounts.sqlite3")
    calls = []
    install_recovery(monkeypatch, calls)

    def fail_load(row):
        vault.events.append(("pending_lookup", row))
        if private_error:
            raise OSError(PRIVATE_ERROR + PRIVATE_PASSWORD)
        raise SessionError("The pending session could not be unlocked or validated.")

    monkeypatch.setattr(vault, "pending_session", fail_load)
    with pytest.raises(preparation.PreparationSessionStoreError) as failure:
        preparation.prepare_test_accounts(vault, count=2, no_browser=True)
    assert calls == [] and vault.events == [("select", 2, 1), ("pending_lookup", 8)]
    result = safe_report(vault)
    assert result["account_failed_rows"] == result["connection_pending_rows"] == []
    assert result["local_session_failure"] == {
        "code": "pending_session_load_failed", "operation": "load", "renewal_completed": False,
    }
    assert country._failure_code(failure.value) == "infrastructure_failed"
    assert PRIVATE_ERROR not in str(failure.value) and PRIVATE_PASSWORD not in str(failure.value)


def test_local_pending_save_error_preserves_confirmed_renewal_hold_not_account_rejection(tmp_path, monkeypatch):
    vault = PendingVault(tmp_path / "accounts.sqlite3", transient_failures=3, store_failure=True)
    calls = []
    install_recovery(monkeypatch, calls)
    with pytest.raises(preparation.PreparationSessionStoreError) as failure:
        preparation.prepare_test_accounts(vault, count=2, no_browser=True)
    assert calls == [None] and vault.attach_attempts == 0
    result = safe_report(vault)
    assert result["local_session_failure"] == {
        "code": "pending_session_save_failed", "operation": "save", "renewal_completed": True,
    }
    assert result["renewal_completed"] is True and result.get("renewal_unknown") is not True
    assert result["account_failed_rows"] == result["connection_pending_rows"] == result["prepared_rows"] == []
    assert failure.value.retry_safe is False
    assert country._failure_code(failure.value) == "interrupted_unknown"
    assert not any(event[0] == "enable" for event in vault.events)


def test_frozen_pending_load_guard_retains_scope_failure(tmp_path, monkeypatch):
    vault = PendingVault(tmp_path / "accounts.sqlite3")

    def fail_scope(_row):
        raise SessionError("The frozen account identity changed.")

    monkeypatch.setattr(vault, "pending_session", fail_scope)
    with pytest.raises(SessionError) as failure:
        preparation.prepare_test_accounts(vault, count=2, no_browser=True)
    assert country._failure_code(failure.value) == "scope_mismatch"
    assert "local_session_failure" not in safe_report(vault)


@pytest.mark.parametrize("workers", [1, 10])
@pytest.mark.parametrize("operation", ["load", "save"])
def test_country_local_pending_failure_holds_immediately_at_twenty_budget_and_survives_restart(
        imported, tmp_path, monkeypatch, workers, operation):
    parent, _full, source, factory = imported
    plan = country.build_selected_plan(parent, source, [1], country="EG")
    pending_loads, recoveries = [], []

    def pending_load(self, row):
        pending_loads.append(row)
        if operation == "load":
            raise SessionError("The pending session could not be unlocked or validated.")
        return None

    def pending_save(self, row, saved):
        self.factory.log("pending_save", row)
        raise OSError(PRIVATE_ERROR + PRIVATE_PASSWORD)

    def recover(record, *, proxy=None):
        row = row_from(record)
        recoveries.append(row)
        factory.log("recover", row, proxy)
        return {"synthetic_saved_row": row}, {"session_renewed": True}

    def attach(self, row, saved, **options):
        self.factory.log("validation", row)
        raise RequestFailure("request_transport_failed", stage="negative_control", curl_code=7)

    monkeypatch.setattr(SyntheticVault, "pending_session", pending_load, raising=False)
    monkeypatch.setattr(SyntheticVault, "save_pending_session", pending_save, raising=False)
    monkeypatch.setattr(SyntheticVault, "attach", attach)
    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=recover))
    path = tmp_path / "progress.json"
    result = country.run_plan(parent, plan, path, workers=workers, no_browser=True, max_consecutive_failures=20)
    expected_code = "infrastructure_failed" if operation == "load" else "interrupted_unknown"
    assert result["pause_reason"] == ("infrastructure_failed" if operation == "load" else "unknown_attempt")
    assert result["status"] == ("paused" if operation == "load" else "attention_required")
    assert result["counts"]["ready"] == result["counts"]["connection_pending"] == 0
    assert result["attempted_accounts"] == 1
    row = country.load_progress(path, plan)["rows"][0]
    assert row["error_code"] == expected_code and row["local_session_failure"]["operation"] == operation
    assert recoveries == ([] if operation == "load" else [1])
    assert sum(event[0] == "validation" for event in factory.events) == 0
    assert not any(event[0] in {"attach", "enroll"} for event in factory.events)
    before = deepcopy((factory.events, recoveries, pending_loads))
    again = country.run_plan(parent, plan, path, workers=workers, no_browser=True, max_consecutive_failures=20)
    assert again["pause_reason"] == result["pause_reason"]
    assert before == (factory.events, recoveries, pending_loads)
    assert_safe(result, factory)
    assert_safe(again, factory)
    assert PRIVATE_ERROR not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", [
    {"code": "pending_session_load_failed", "operation": "load", "renewal_completed": 0},
    {"code": "pending_session_save_failed", "operation": "save", "renewal_completed": False},
    {"code": "pending_session_load_failed", "operation": "load", "renewal_completed": False, "secret": PRIVATE_ERROR},
])
def test_local_pending_diagnostics_reject_unsafe_stored_fields(imported, tmp_path, value):
    parent, plan, _source, factory = imported
    progress = country._new_progress(plan)
    progress["rows"][0].update(state="failed", attempts=1, phase="stopped", error_code="infrastructure_failed",
                               local_session_failure=value)
    path = tmp_path / "progress.json"
    country._atomic_json(path, progress)
    with pytest.raises(SessionError, match="progress file is invalid"):
        country.load_progress(path, plan)


def test_attempted_summary_counts_attempts_only_including_held_history(imported):
    _parent, plan, _source, factory = imported
    progress = country._new_progress(plan)
    progress["rows"][0].update(state="already_ready", attempts=0)
    progress["rows"][1].update(state="unknown", attempts=1)
    progress["rows"][2].update(state="ready", attempts=1)
    progress["rows"][3].update(state="connection_pending", attempts=1)
    result = country.summarize(progress)
    assert result["attempted_accounts"] == 3
    assert_safe(result, factory)


def test_selected_plan_reads_enrolled_identity_set_once_without_state_changes(imported, monkeypatch):
    parent, _full, source, _factory = imported
    calls = []
    original = parent.enrolled_test_rows

    def enrolled():
        calls.append(True)
        return original()

    monkeypatch.setattr(parent, "enrolled_test_rows", enrolled)
    plan = country.build_selected_plan(parent, source, [8, 2, 5], country="EG")
    assert len(calls) == 1 and [row["source_row"] for row in plan["rows"]] == [8, 2, 5]
    assert all(row["state"] == "pending" for row in plan["rows"])
