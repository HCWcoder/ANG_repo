"""Account holds continue peers without renewing an uncertain session again."""

from copy import deepcopy
import json
import sqlite3

import pytest

from anghami_session import country_preparation as country
from anghami_session import ui_preparation as bridge
from anghami_session.errors import RequestFailure, SessionError, SessionStorageError, safe_session_storage_failure
from test_country_preparation_workers import SyntheticVault, assert_safe, imported
from test_ui_preparation_bridge import offline_bridge, invoke


def retained_failure(message="synthetic private validation failure"):
    failure = SessionError(message)
    failure.candidate_retained = True
    failure.renewal_completed = True
    failure.validation_pending = True
    failure.retry_safe = False
    return failure


def install_review_store(monkeypatch, *, fail_commit=False, false_proof=False):
    def record(self, row, *, job_id, source_sha256, identity_binding, failure_code,
               stage, candidate_retained):
        if fail_commit:
            raise sqlite3.OperationalError("synthetic private database failure")
        identity = self._db.execute("SELECT email_key FROM accounts WHERE source_row=?", (row,)).fetchone()[0]
        assert source_sha256 == self.metadata("source_sha256")
        assert country._binding(identity) == identity_binding
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS account_preparation_review "
                             "(source_row INTEGER PRIMARY KEY,email_key BLOB,code TEXT,stage TEXT,candidate_retained INTEGER)")
            self._db.execute("INSERT INTO account_preparation_review VALUES(?,?,?,?,?)",
                             (row, identity, failure_code, stage, candidate_retained))
            self._db.execute("UPDATE accounts SET state='session_review_pending' WHERE source_row=?", (row,))
        self.factory.log("hold", row, job_id)

    def read(self, row):
        if false_proof:
            return None
        code, stage, retained = self._db.execute(
            "SELECT code,stage,candidate_retained FROM account_preparation_review WHERE source_row=?", (row,),
        ).fetchone()
        return {"source_row": row, "session_review_pending": True, "held_rows": [row],
                "preparation_review": {"code": code, "stage": stage, "candidate_retained": bool(retained)}}

    monkeypatch.setattr(SyntheticVault, "record_preparation_session_review", record, raising=False)
    monkeypatch.setattr(SyntheticVault, "preparation_session_review", read, raising=False)


def prepare_with_failure(failure, called):
    def prepare(selected, **options):
        row = options["start_row"]
        called.append(row)
        if row == 1:
            raise failure
        selected.attach(row, {"synthetic_saved_row": row}, proxy=options.get("proxy"))
        selected.enable_test_account(row)
        return {"passed": True, "selected_rows": [row], "prepared_rows": [row],
                "prepared_account_count": 1, "phase": "complete"}
    return prepare


@pytest.mark.parametrize("workers,budget", [(1, 3), (2, 20)])
@pytest.mark.parametrize("failure_kind", ["candidate_validation", "unknown_renewal", "malformed_renewal_response"])
def test_durably_held_account_does_not_stop_or_fail_unrelated_rows(imported, tmp_path, monkeypatch,
                                                                 workers, budget, failure_kind):
    parent, plan, _source, factory = imported
    install_review_store(monkeypatch)
    failure = retained_failure()
    if failure_kind == "unknown_renewal":
        failure = RequestFailure("request_transport_failed", stage="session_recovery_renewal", curl_code=7)
        failure.renewal_unknown = True
    elif failure_kind == "malformed_renewal_response":
        failure = RequestFailure("session_response_invalid", stage="identity", retry_safe=False)
        failure.renewal_unknown = True
    called = []
    path = tmp_path / "progress.json"
    result = country.run_plan(parent, plan, path, workers=workers, no_browser=True,
                              max_consecutive_failures=budget, prepare=prepare_with_failure(failure, called))
    assert result["status"] == "completed" and result["pause_reason"] is None
    assert sorted(called) == list(range(1, 9)) and called.count(1) == 1
    assert result["counts"]["ready"] == 7 and result["session_review_pending_count"] == 1
    assert result["session_review_pending_rows"] == [1]
    assert result["counts"]["failed"] == result["counts"]["unknown"] == result["consecutive_failures"] == 0
    held = country.load_progress(path, plan)["rows"][0]
    assert held["state"] == held["phase"] == "session_review_pending" and held["attempts"] == 1
    assert result["attempted_accounts"] == 8
    assert len([event for event in factory.events if event[0] == "hold"]) == 1
    assert_safe(result, factory)
    assert "synthetic private" not in json.dumps(result)


@pytest.mark.parametrize("failure_mode", ["commit_failed", "proof_missing"])
def test_review_persistence_failure_still_holds_whole_job(imported, tmp_path, monkeypatch, failure_mode):
    parent, plan, _source, factory = imported
    install_review_store(monkeypatch, fail_commit=failure_mode == "commit_failed",
                         false_proof=failure_mode == "proof_missing")
    called = []
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=2, no_browser=True,
                              max_consecutive_failures=20,
                              prepare=prepare_with_failure(retained_failure(), called))
    assert result["status"] == "attention_required" and result["pause_reason"] == "unknown_attempt"
    assert result["counts"]["unknown"] >= 1 and len(called) < 8
    assert_safe(result, factory)


@pytest.mark.parametrize("failure_kind", ["scope", "pending_save", "account", "journal"])
def test_candidate_flag_never_overrides_global_or_confirmed_account_errors(imported, tmp_path, monkeypatch,
                                                                         failure_kind):
    parent, plan, _source, factory = imported
    install_review_store(monkeypatch)
    failure = retained_failure("The frozen account identity changed.")
    if failure_kind == "pending_save":
        failure = retained_failure()
        failure.operation = "pending_save"
        failure.renewal_unknown = True
    elif failure_kind == "account":
        failure = RequestFailure("session_authentication_rejected", stage="identity")
        failure.candidate_retained = failure.renewal_completed = failure.validation_pending = True
    elif failure_kind == "journal":
        from anghami_session.play_record import _Failure
        failure = _Failure("journal_failed", "Synthetic journal failure")
        failure.candidate_retained = failure.renewal_completed = failure.validation_pending = True
    called = []
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=2, no_browser=True,
                              max_consecutive_failures=20, prepare=prepare_with_failure(failure, called))
    assert not [event for event in factory.events if event[0] == "hold"]
    if failure_kind == "account":
        assert result["account_failed_count"] == 1 and result["session_review_pending_count"] == 0
    else:
        assert result["status"] == "attention_required" and len(called) < 8


def test_preparation_review_table_survives_account_state_tampering(imported, monkeypatch):
    parent, plan, _source, _factory = imported
    install_review_store(monkeypatch)
    assert country._isolate_preparation_failure(parent, plan, 1, retained_failure(), job_id="a" * 32)
    with parent._db:
        parent._db.execute("UPDATE accounts SET state='login_required' WHERE source_row=1")
    _identity, state = country._identity_state(parent, 1)
    assert state == "session_review_pending"


def test_parallel_ui_reports_new_durable_review_holds_in_running_and_final_counts(offline_bridge):
    fixture = offline_bridge
    fixture.outcomes[fixture.rows[0]] = "session_review_pending"
    result = invoke(fixture)
    assert result["prepared_account_count"] == 9 and result["preparation_held"] == 1
    assert result["session_review_pending_count"] == 1 and result["session_review_pending_rows"] == [fixture.rows[0]]
    assert result["attention_required_rows"] == [fixture.rows[0]] and result["account_failed_count"] == 0


def test_retained_verified_save_failure_keeps_safe_numeric_diagnostics_and_continues(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    install_review_store(monkeypatch)
    failure = SessionStorageError(operation="verified_save", phase="transaction", sqlite_code=5, attempts=3)
    failure.candidate_retained = failure.renewal_completed = failure.validation_pending = True
    called = []
    path = tmp_path / "progress.json"
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True, max_consecutive_failures=20,
                              prepare=prepare_with_failure(failure, called))
    assert result["status"] == "completed" and len(called) == 8
    expected = safe_session_storage_failure(failure)
    assert country.load_progress(path, plan)["rows"][0]["session_storage_failure"] == expected
    assert result["recent_session_storage_failures"] == [{"source_row": 1, **expected}]
    assert result["account_failed_count"] == result["consecutive_failures"] == 0
    assert_safe(result, factory)


@pytest.mark.parametrize("verified_retained", [False, True])
def test_nonretained_local_save_failure_stops_globally_before_ordinary_failure_budget(imported, tmp_path, monkeypatch,
                                                                                   verified_retained):
    parent, plan, _source, factory = imported
    install_review_store(monkeypatch)
    failure = SessionStorageError(operation="verified_save", phase="transaction", sqlite_code=13)
    if verified_retained:
        failure.verified_session_retained = failure.renewal_completed = True
    called = []
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=2, no_browser=True,
                              max_consecutive_failures=20, prepare=prepare_with_failure(failure, called))
    assert result["status"] == "paused" and result["pause_reason"] == "infrastructure_failed"
    assert result["consecutive_failures"] < 20 and len(called) < 8
    assert result["account_failed_count"] == 0
    assert result["recent_session_storage_failures"][0]["sqlite_code"] == 13
    assert not [event for event in factory.events if event[0] == "hold"]


def test_candidate_cannot_be_misclassified_as_account_rejection_when_hold_api_unavailable(imported, tmp_path):
    parent, plan, _source, _factory = imported
    called = []
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=2, no_browser=True,
                              max_consecutive_failures=20,
                              prepare=prepare_with_failure(retained_failure(), called))
    assert result["status"] == "attention_required" and result["pause_reason"] == "unknown_attempt"
    assert result["account_failed_count"] == 0 and len(called) < 8


def test_checkpoint_review_attempt_requires_typed_review_proof(imported, tmp_path):
    parent, plan, _source, _factory = imported
    progress = country._new_progress(plan)
    progress["rows"][0].update(state="session_review_pending", attempts=1, phase="session_review_pending")
    path = tmp_path / "progress.json"
    country._atomic_json(path, progress)
    with pytest.raises(SessionError, match="invalid"):
        country.load_progress(path, plan)


def test_ui_report_projects_only_fixed_storage_diagnostics(offline_bridge, monkeypatch):
    fixture = offline_bridge
    original = bridge.country.run_plan
    expected = safe_session_storage_failure(SessionStorageError(operation="verified_save", phase="transaction", sqlite_code=5, attempts=3))
    def run(*args, **options):
        result = original(*args, **options)
        result["recent_session_storage_failures"] = [
            {"source_row": fixture.rows[0], **expected},
            {"source_row": fixture.rows[1], **expected, "sid": "synthetic private sid"},
        ]
        return result
    monkeypatch.setattr(bridge.country, "run_plan", run)
    result = invoke(fixture)
    assert result["recent_session_storage_failures"] == [{"source_row": fixture.rows[0], **expected}]
    assert "synthetic private sid" not in json.dumps(result)
