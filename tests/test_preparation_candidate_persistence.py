"""Issued candidates and uncertain preparation identities are never replayed."""

from copy import deepcopy
import hashlib
import json

import pytest

from anghami_session import preparation, vault as vault_module
from anghami_session.errors import SessionError, SessionStorageError, safe_session_storage_failure
from test_account_preparation import SAVED, safe_report
from test_pending_account_sessions import pending_vault, saved_session
from test_preparation_pending_validation import PendingVault, install_recovery


JOB = "a" * 32
SOURCE = "b" * 64


def bind(store, row=101):
    with store._db:
        store._db.execute("INSERT OR REPLACE INTO metadata VALUES ('source_sha256',?)", (SOURCE,))
    identity = store._db.execute("SELECT email_key FROM accounts WHERE source_row=?", (row,)).fetchone()[0]
    return {"job_id": JOB, "source_sha256": SOURCE,
            "identity_binding": hashlib.sha256(str(identity).encode()).hexdigest()}


def add_alias(store):
    record = deepcopy(store.record(101))
    record["source_row"] = 103
    with store._db:
        store._db.execute(
            "INSERT INTO accounts(source_row,email_key,record) VALUES (103,(SELECT email_key FROM accounts WHERE source_row=101),?)",
            (vault_module._pack(record),),
        )


def test_confirmed_candidate_is_saved_before_first_attach_get(tmp_path, monkeypatch):
    store = PendingVault(tmp_path / "synthetic.sqlite3")
    calls = []
    install_recovery(monkeypatch, calls)
    result = preparation.prepare_test_accounts(store, count=2, no_browser=True)
    events = [event[0] for event in store.events]
    assert events.index("pending_save") < events.index("attach")
    assert result["passed"] is True and calls == [None] and store.pending == {}


def test_generic_validation_keeps_known_candidate_for_bound_local_review(tmp_path, monkeypatch):
    store = PendingVault(tmp_path / "synthetic.sqlite3", transient_failures=1,
                         failure_factory=lambda: SessionError("Synthetic private untyped validation"))
    calls = []
    install_recovery(monkeypatch, calls)
    with pytest.raises(preparation.PreparationCandidateReviewError) as failure:
        preparation.prepare_test_accounts(store, count=2, no_browser=True)
    assert failure.value.candidate_retained is True and failure.value.retry_safe is False
    assert store.pending[8] == SAVED and calls == [None] and store.attach_attempts == 1
    result = safe_report(store)
    assert result["candidate_retained"] is result["validation_pending"] is result["renewal_completed"] is True
    assert result.get("renewal_unknown") is not True
    assert result["preparation_review"] == {"code": "preparation_validation_unknown", "stage": "validation", "candidate_retained": True}
    assert "Synthetic private" not in json.dumps(result)


def test_verified_storage_failure_retains_candidate_and_fixed_diagnostic(tmp_path, monkeypatch):
    store = PendingVault(tmp_path / "synthetic.sqlite3", transient_failures=1,
                         failure_factory=lambda: SessionStorageError(operation="verified_save", phase="transaction", sqlite_code=5, attempts=3))
    install_recovery(monkeypatch, [])
    with pytest.raises(SessionStorageError) as failure:
        preparation.prepare_test_accounts(store, count=2, no_browser=True)
    assert failure.value.candidate_retained is True and store.pending[8] == SAVED
    result = safe_report(store)
    assert result["session_storage_failure"] == {"code": "session_store_busy", "operation": "verified_save", "phase": "transaction", "attempts": 3, "sqlite_code": 5}
    assert result.get("renewal_unknown") is not True and not store.reviewed


def test_postattach_enrollment_storage_failure_keeps_verified_session_without_false_pending_or_recovery_replay(tmp_path, monkeypatch):
    store = PendingVault(tmp_path / "synthetic.sqlite3")
    recoveries = []
    install_recovery(monkeypatch, recoveries)
    original_enable = store.enable_test_account
    def fail_enrollment(_row):
        raise SessionStorageError(operation="verified_save", phase="transaction", sqlite_code=5, attempts=3)
    monkeypatch.setattr(store, "enable_test_account", fail_enrollment)
    with pytest.raises(SessionStorageError) as failure:
        preparation.prepare_test_accounts(store, count=2, no_browser=True)
    assert store.sessions[8] == SAVED and store.pending == {} and not store.reviewed
    assert failure.value.verified_session_retained is True and failure.value.renewal_completed is True
    assert getattr(failure.value, "candidate_retained", False) is False
    assert getattr(failure.value, "renewal_unknown", False) is False
    result = safe_report(store)
    assert result["verified_session_retained"] is True and result["prepared_rows"] == []
    assert result.get("candidate_retained") is not True and result.get("renewal_unknown") is not True
    monkeypatch.setattr(store, "enable_test_account", original_enable)
    store.rows = [8]
    resumed = preparation.prepare_test_accounts(store, count=1, no_browser=True)
    assert resumed["prepared_rows"] == [8] and recoveries == [None]
    assert sum(event == ("enable", 8) for event in store.events) == 1


def test_pending_save_scope_guard_is_preserved_without_storage_reclassification(tmp_path, monkeypatch):
    store = PendingVault(tmp_path / "synthetic.sqlite3")
    install_recovery(monkeypatch, [])
    def reject_scope(*_):
        raise SessionError("The frozen account identity changed.")
    monkeypatch.setattr(store, "save_pending_session", reject_scope)
    with pytest.raises(SessionError, match="frozen account identity") as failure:
        preparation.prepare_test_accounts(store, count=2, no_browser=True)
    assert not isinstance(failure.value, preparation.PreparationSessionStoreError)
    assert store.attach_attempts == 0 and store.pending == {}
    assert "local_session_failure" not in safe_report(store)


def test_review_without_saved_candidate_is_durable_and_blocks_all_aliases(pending_vault):
    store = pending_vault
    bindings = bind(store)
    add_alias(store)
    before = store._db.execute("SELECT source_row,record,session,checked_at_utc FROM accounts ORDER BY source_row").fetchall()
    proof = store.record_preparation_session_review(101, **bindings, failure_code="preparation_validation_unknown")
    assert proof == {"source_row": 101, "session_review_pending": True, "held_rows": [101, 103],
                     "preparation_review": {"code": "preparation_validation_unknown", "stage": "validation", "candidate_retained": False}}
    assert store._db.execute("SELECT source_row,record,session,checked_at_utc FROM accounts ORDER BY source_row").fetchall() == before
    assert store.select_test_candidates(10, country="EG") == [102]
    assert store.failure_review()["total"] == 0 and store.session_review()["held_rows"] == [101, 103]
    for method in (store.session, store.pending_session, store.enable_test_account):
        with pytest.raises(SessionError, match="explicit saved-session review"):
            method(103)
    with vault_module.AccountVault(store.path) as reopened:
        assert reopened.preparation_session_review(101) == proof
    text = json.dumps(proof) + json.dumps(store.session_review())
    assert "example.invalid" not in text and "synthetic-pending" not in text


def test_retained_candidate_review_proves_encrypted_candidate_exists(pending_vault):
    store = pending_vault
    bindings = bind(store)
    store.save_pending_session(101, saved_session())
    candidate_blob = store._db.execute("SELECT session FROM account_pending_sessions WHERE source_row=101").fetchone()[0]
    result = store.record_preparation_session_review(101, **bindings, candidate_retained=True,
                                                    failure_code="preparation_validation_unknown")
    assert result["preparation_review"]["candidate_retained"] is True
    assert store._db.execute("SELECT session FROM account_pending_sessions WHERE source_row=101").fetchone()[0] == candidate_blob
    assert store._db.execute("SELECT session FROM accounts WHERE source_row=101").fetchone()[0] is None
    with store._db:
        store._db.execute("UPDATE account_pending_sessions SET session=? WHERE source_row=101", (b"tampered",))
    with pytest.raises(SessionError, match="could not be verified securely"):
        store.preparation_session_review(101)


def test_review_cannot_claim_retained_candidate_without_durable_blob(pending_vault):
    store = pending_vault
    bindings = bind(store)
    with pytest.raises(SessionError, match="candidate could not be confirmed"):
        store.record_preparation_session_review(101, **bindings, candidate_retained=True)
    assert not store._preparation_review_table_exists()
    assert store._db.execute("SELECT state FROM accounts WHERE source_row=101").fetchone()[0] == "login_required"


@pytest.mark.parametrize("field", ["source_sha256", "identity_binding"])
def test_wrong_frozen_binding_cannot_quarantine_different_import_or_identity(pending_vault, field):
    store = pending_vault
    bindings = bind(store)
    bindings[field] = "c" * 64
    with pytest.raises(SessionError):
        store.record_preparation_session_review(101, **bindings)
    assert not store._preparation_review_table_exists() and store.session_review()["held_rows"] == []


def test_table_hold_survives_state_tamper_and_invalidates_its_proof(pending_vault):
    store = pending_vault
    store.record_preparation_session_review(101, **bind(store))
    with store._db:
        store._db.execute("UPDATE accounts SET state='login_required' WHERE source_row=101")
    assert store.select_test_candidates(10, country="EG") == [102]
    with pytest.raises(SessionError, match="explicit saved-session review"):
        store.session(101)
    with pytest.raises(SessionError, match="could not be verified securely"):
        store.preparation_session_review(101)


@pytest.mark.parametrize("raw", [
    {"code": "session_store_busy", "operation": "verified_save", "phase": "transaction", "attempts": True, "sqlite_code": 5},
    {"code": "session_store_failed", "operation": "verified_save", "phase": "transaction", "attempts": 1, "secret": "private"},
    {"code": "session_store_busy", "operation": "verified_save", "phase": "encryption", "attempts": 1, "sqlite_code": 5},
])
def test_storage_diagnostics_reject_unbounded_or_inconsistent_fields(raw):
    assert safe_session_storage_failure(raw) == {}
