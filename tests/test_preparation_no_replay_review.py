"""Independent local-save regressions: retry SQLite, never account requests."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading

import pytest

from anghami_session import country_preparation as country, vault as vault_module
from anghami_session.errors import (
    SessionError, SessionReviewRequiredError, SessionStorageError, safe_session_storage_failure,
)
from test_pending_account_sessions import (
    SID, PASSWORD, account_state, fake_session, pending_vault, saved_session,
)


PRIVATE = SID + PASSWORD + "synthetic-101@example.invalid"


def assert_safe_storage(error):
    public = json.dumps(safe_session_storage_failure(error)) + str(error)
    assert SID not in public and PASSWORD not in public and "example.invalid" not in public


def test_two_simultaneous_validations_serialize_real_sqlite_writes_without_repeating_http(
        pending_vault, fake_session, monkeypatch):
    # Remote checks finish before writer intent is acquired. The two real
    # SQLite connections then serialize their short local save transactions.
    gate = threading.Barrier(2, timeout=5)
    guard_counts = Counter()
    guard_lock = threading.Lock()
    connections, statements = {}, {}
    original = vault_module.AccountVault._require_session_not_held
    original_session = vault_module.AnghamiSession

    class SynchronizedSession(original_session):
        def __init__(self, *, saved, **options):
            super().__init__(saved=saved, **options)
            self.row = int(saved["account_email"].split("@", 1)[0].rsplit("-", 1)[1])

        def check(self, *, negative_control):
            assert not connections[self.row].in_transaction
            report = super().check(negative_control=negative_control)
            gate.wait()
            return report

    def synchronize_first_upgrade(selected, row):
        original(selected, row)
        if selected._db.in_transaction:
            with guard_lock:
                guard_counts[row] += 1

    monkeypatch.setattr(vault_module, "AnghamiSession", SynchronizedSession)
    monkeypatch.setattr(vault_module.AccountVault, "_require_session_not_held", synchronize_first_upgrade)

    def attach(row):
        with vault_module.AccountVault(pending_vault.path) as selected:
            connections[row] = selected._db
            statements[row] = []
            selected._db.set_trace_callback(statements[row].append)
            return selected.attach(row, saved_session(row))

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(executor.map(attach, (101, 102)))

    assert all(report["authenticated"] is True for report in reports)
    assert len(fake_session["calls"]) == 2
    assert {saved["account_email"] for saved, _options in fake_session["calls"]} == {
        saved_session(row)["account_email"] for row in (101, 102)
    }
    assert guard_counts == {101: 1, 102: 1}
    assert all("BEGIN IMMEDIATE" in trace for trace in statements.values())
    assert all(account_state(pending_vault, row)[0] == "ready" for row in (101, 102))


def test_exhausted_busy_save_keeps_encrypted_candidate_and_checks_http_once(
        pending_vault, fake_session, monkeypatch):
    pending_vault.save_pending_session(101, saved_session())
    before = account_state(pending_vault)
    original = vault_module.AccountVault._require_session_not_held
    writes = []

    def busy_only_during_save(selected, row):
        original(selected, row)
        if selected._db.in_transaction:
            writes.append(row)
            error = sqlite3.OperationalError(PRIVATE)
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error

    monkeypatch.setattr(vault_module.AccountVault, "_require_session_not_held", busy_only_during_save)
    with pytest.raises(SessionStorageError) as failure:
        pending_vault.attach(101, saved_session())

    assert writes == [101, 101, 101] and len(fake_session["calls"]) == 1
    assert safe_session_storage_failure(failure.value) == {
        "code": "session_store_busy", "operation": "verified_save", "phase": "transaction",
        "attempts": 3, "sqlite_code": sqlite3.SQLITE_BUSY,
    }
    assert account_state(pending_vault) == before
    assert pending_vault.pending_session(101) == saved_session()
    assert 101 not in pending_vault.enrolled_test_rows()
    assert_safe_storage(failure.value)


@pytest.mark.parametrize("workers", [2, 30])
def test_parallel_enrollment_uses_local_writer_intent_after_one_validation_per_account(
        pending_vault, fake_session, workers):
    # Enrollment is another read-then-write transaction after attach. It must
    # not reintroduce the SQLite lock upgrade race fixed in session storage.
    rows = list(range(101, 101 + workers))
    with pending_vault._db:
        for row in rows[2:]:
            record = {"source_row": row, "country": "EG", "email": saved_session(row)["account_email"], "password": PASSWORD}
            pending_vault._db.execute(
                "INSERT INTO accounts(source_row,email_key,record) VALUES(?,?,?)",
                (row, vault_module._email_key(pending_vault._index_key, record["email"]), vault_module._pack(record)),
            )
    gate = threading.Barrier(workers, timeout=10)
    statements = {}

    def attach_and_enroll(row):
        with vault_module.AccountVault(pending_vault.path) as selected:
            selected.attach(row, saved_session(row))
            statements[row] = []
            selected._db.set_trace_callback(statements[row].append)
            gate.wait()
            return selected.enable_test_account(row)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        reports = list(executor.map(attach_and_enroll, rows))
    assert all(report["enabled"] is True for report in reports)
    assert len(fake_session["calls"]) == workers
    assert all("BEGIN IMMEDIATE" in trace for trace in statements.values())
    assert set(rows).issubset(pending_vault.enrolled_test_rows())


def test_nonbusy_sqlite_rejection_never_retries_http_or_drops_pending_candidate(
        pending_vault, fake_session):
    pending_vault.save_pending_session(101, saved_session())
    denied = []

    def deny_account_write(action, name, *_rest):
        if action == sqlite3.SQLITE_UPDATE and name == "accounts":
            denied.append(True)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    pending_vault._db.set_authorizer(deny_account_write)
    try:
        with pytest.raises(SessionStorageError) as failure:
            pending_vault.attach(101, saved_session())
    finally:
        pending_vault._db.set_authorizer(None)

    assert len(denied) == 1 and len(fake_session["calls"]) == 1
    assert safe_session_storage_failure(failure.value) == {
        "code": "session_store_failed", "operation": "verified_save", "phase": "transaction",
        "attempts": 1, "sqlite_code": sqlite3.SQLITE_AUTH,
    }
    assert pending_vault.pending_session(101) == saved_session()
    assert account_state(pending_vault)[0] == "login_required"
    assert_safe_storage(failure.value)


def test_review_hold_arriving_after_validation_keeps_typed_hold_and_candidate(
        pending_vault, fake_session, monkeypatch):
    pending_vault.save_pending_session(101, saved_session())
    original = vault_module.AccountVault._require_session_not_held

    def hold_only_during_save(selected, row):
        original(selected, row)
        if selected._db.in_transaction:
            raise SessionReviewRequiredError()

    monkeypatch.setattr(vault_module.AccountVault, "_require_session_not_held", hold_only_during_save)
    with pytest.raises(SessionReviewRequiredError) as failure:
        pending_vault.attach(101, saved_session())

    assert safe_session_storage_failure(failure.value) == {}
    assert len(fake_session["calls"]) == 1
    assert pending_vault.pending_session(101) == saved_session()
    assert account_state(pending_vault)[0] == "login_required"


def test_encryption_failure_reports_fixed_local_phase_without_account_rejection(
        pending_vault, fake_session, monkeypatch):
    pending_vault.save_pending_session(101, saved_session())
    before = account_state(pending_vault)

    def fail_pack(_saved):
        raise OSError(PRIVATE)

    monkeypatch.setattr(vault_module, "_pack", fail_pack)
    with pytest.raises(SessionStorageError) as failure:
        pending_vault.attach(101, saved_session())

    assert len(fake_session["calls"]) == 1 and account_state(pending_vault) == before
    assert pending_vault.pending_session(101) == saved_session()
    assert safe_session_storage_failure(failure.value) == {
        "code": "session_protection_failed", "operation": "verified_save", "phase": "encryption",
        "attempts": 1,
    }
    assert_safe_storage(failure.value)


def frozen_plan(selected):
    source = selected.path.parent / "frozen-synthetic-source.txt"
    source.write_bytes(b"synthetic offline preparation import binding")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with selected._db:
        selected._db.execute("INSERT OR REPLACE INTO metadata VALUES('source_sha256',?)", (digest,))
    return {
        "source_path": str(source), "source_sha256": digest,
        "identity_bindings": {
            str(row): country._binding(identity)
            for row, identity in selected._db.execute("SELECT source_row,email_key FROM accounts")
        },
    }


def test_retained_candidate_review_is_bound_durable_and_blocks_recovery_aliases(
        pending_vault, fake_session):
    selected = pending_vault
    record = selected.record(101)
    record["source_row"] = 103
    with selected._db:
        selected._db.execute(
            "INSERT INTO accounts(source_row,email_key,record) VALUES(103,?,?)",
            (vault_module._email_key(selected._index_key, record["email"]), vault_module._pack(record)),
        )
    selected.save_pending_session(101, saved_session())
    candidate_blob = selected._db.execute("SELECT session FROM account_pending_sessions WHERE source_row=101").fetchone()[0]
    plan = frozen_plan(selected)
    failure = SessionStorageError(operation="verified_save", phase="transaction", sqlite_code=5, attempts=3)
    failure.candidate_retained = failure.renewal_completed = failure.validation_pending = True

    isolated = country._isolate_preparation_failure(selected, plan, 101, failure, job_id="a" * 32)
    assert isolated == {
        "code": "preparation_validation_unknown", "stage": "validation", "candidate_retained": True,
    }
    proof = selected.preparation_session_review(101)
    assert proof["held_rows"] == [101, 103]
    assert proof["preparation_review"] == {
        "code": "preparation_validation_unknown", "stage": "validation", "candidate_retained": True,
    }
    assert selected._db.execute("SELECT session FROM account_pending_sessions WHERE source_row=101").fetchone()[0] == candidate_blob
    assert all(account_state(selected, row)[0] == "session_review_pending" for row in (101, 103))
    assert selected.select_test_candidates(10) == [102]
    assert selected.failure_review()["failed_rows"] == []
    with vault_module.AccountVault(selected.path) as reopened:
        assert reopened.preparation_session_review(101) == proof
        for row in (101, 103):
            with pytest.raises(SessionReviewRequiredError):
                reopened.pending_session(row)
            with pytest.raises(SessionReviewRequiredError):
                reopened.attach(row, saved_session())
    assert fake_session["calls"] == []
    assert SID not in json.dumps(proof) and PASSWORD not in json.dumps(proof)


def test_review_of_unknown_first_preparation_needs_no_saved_session_and_never_declares_failed(
        pending_vault, fake_session):
    plan = frozen_plan(pending_vault)
    proof = pending_vault.record_preparation_session_review(
        101, job_id="b" * 32, source_sha256=plan["source_sha256"],
        identity_binding=plan["identity_bindings"]["101"],
        failure_code="preparation_outcome_unknown", stage="validation", candidate_retained=False,
    )
    assert proof["session_review_pending"] is True and proof["held_rows"] == [101]
    assert account_state(pending_vault) == ("session_review_pending", None, None)
    assert 101 not in pending_vault.enrolled_test_rows()
    assert pending_vault.failure_review()["failed_rows"] == []
    assert pending_vault.select_test_candidates(10) == [102]
    assert fake_session["calls"] == []


@pytest.mark.parametrize("bad_binding", ["identity", "source"])
def test_preparation_review_rejects_changed_frozen_scope_before_account_mutation(
        pending_vault, fake_session, bad_binding):
    plan = frozen_plan(pending_vault)
    source = "c" * 64 if bad_binding == "source" else plan["source_sha256"]
    identity = "d" * 64 if bad_binding == "identity" else plan["identity_bindings"]["101"]
    before = account_state(pending_vault)
    with pytest.raises(SessionError):
        pending_vault.record_preparation_session_review(
            101, job_id="e" * 32, source_sha256=source, identity_binding=identity,
            failure_code="preparation_outcome_unknown", stage="validation", candidate_retained=False,
        )
    assert account_state(pending_vault) == before
    assert pending_vault.preparation_session_review(101) is None
    assert fake_session["calls"] == []
