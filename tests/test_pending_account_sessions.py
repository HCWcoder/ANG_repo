"""Pending issued sessions stay protected and separate from ready accounts."""

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from anghami_session import client, vault as vault_module
from anghami_session.errors import RequestFailure, SessionError


STAMP = "2026-10-04T00:00:00+00:00"
SID = "synthetic-pending-session-secret"
PASSWORD = "synthetic-pending-password"


def saved_session(row=101, *, sid=SID):
    return {
        "format_version": 1, "created_at_utc": STAMP,
        "origin": "https://play.anghami.com", "account_email": f"synthetic-{row}@example.invalid",
        "renewal_method": "saved_sid",
        "requests": {"relations": {
            "method": "GET", "url": client.GATEWAY_URL + "?type=GETuserrelations&sid=" + sid,
            "headers": {"cookie": "appsidsave=" + sid},
        }},
    }


@pytest.fixture
def pending_vault(tmp_path, monkeypatch):
    protected = {}

    def crypt(raw, *, decrypt=False):
        raw = bytes(raw)
        if decrypt:
            return protected[raw]
        encrypted = b"synthetic-protected:" + hashlib.sha256(raw).digest()
        protected[encrypted] = raw
        return encrypted

    monkeypatch.setattr(vault_module, "_crypt", crypt)
    monkeypatch.setattr(client.requests, "Session", lambda *_a, **_k: pytest.fail("Pending storage attempted HTTP"))
    key = b"synthetic-pending-index-key"
    path = tmp_path / "synthetic.sqlite3"
    with sqlite3.connect(path) as database:
        database.executescript("""
            CREATE TABLE metadata (name TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE accounts (
                source_row INTEGER PRIMARY KEY, email_key TEXT NOT NULL,
                record BLOB NOT NULL, session BLOB,
                state TEXT NOT NULL DEFAULT 'login_required', checked_at_utc TEXT
            );
        """)
        database.executemany("INSERT INTO metadata VALUES (?, ?)", {
            "format_version": "1", "index_key": crypt(key),
        }.items())
        for row in (101, 102):
            email = f"synthetic-{row}@example.invalid"
            record = {"source_row": row, "country": "EG", "email": email, "password": PASSWORD}
            database.execute(
                "INSERT INTO accounts(source_row,email_key,record) VALUES (?,?,?)",
                (row, vault_module._email_key(key, email), vault_module._pack(record)),
            )
    with vault_module.AccountVault(path) as selected:
        yield selected


@pytest.fixture
def fake_session(monkeypatch):
    state = {"calls": [], "failure": None}

    class Session:
        def __init__(self, *, saved, **options):
            state["calls"].append((deepcopy(saved), dict(options)))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def check(self, *, negative_control):
            assert negative_control is True
            if state["failure"] is not None:
                raise state["failure"]
            return {"authenticated": True, "checked_at_utc": STAMP, "without_session_rejected": True}

    monkeypatch.setattr(vault_module, "AnghamiSession", Session)
    return state


def pending_table_exists(selected):
    return selected._db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_pending_sessions'",
    ).fetchone() is not None


def account_state(selected, row=101):
    return selected._db.execute("SELECT state,session,checked_at_utc FROM accounts WHERE source_row=?", (row,)).fetchone()


def assert_safe(error):
    message = str(error)
    assert SID not in message and PASSWORD not in message and "example.invalid" not in message


def test_absent_candidate_is_none_without_optional_schema_or_writes(pending_vault):
    original = pending_vault.path.read_bytes()
    assert pending_vault.pending_session(101) is None
    assert not pending_table_exists(pending_vault)
    assert pending_vault.path.read_bytes() == original


def test_pending_candidate_is_encrypted_bound_and_never_ready_or_enrolled(pending_vault, fake_session):
    selected = pending_vault
    before = account_state(selected)
    report = selected.save_pending_session(101, saved_session())
    assert report == {"source_row": 101, "session_pending": True}
    assert account_state(selected) == before == ("login_required", None, None)
    assert 101 not in selected.enrolled_test_rows()
    assert 101 not in selected.test_accounts()["ready_rows"]
    assert selected.pending_session(101) == saved_session()
    assert selected.pending_session(102) is None
    encrypted = selected._db.execute("SELECT session FROM account_pending_sessions WHERE source_row=101").fetchone()[0]
    for value in (SID, PASSWORD, "synthetic-101@example.invalid"):
        assert value.encode() not in encrypted and value.encode() not in selected.path.read_bytes()
        assert value not in json.dumps(report)
    foreign_key = selected._db.execute("PRAGMA foreign_key_list(account_pending_sessions)").fetchone()
    assert foreign_key[2:5] == ("accounts", "source_row", "source_row")
    with pytest.raises(SessionError, match="normal login"):
        selected.session(101)
    with pytest.raises(SessionError, match="normal login"):
        selected._http_session(101)
    with pytest.raises(SessionError, match="verified ready"):
        selected.enable_test_account(101)
    assert fake_session["calls"] == []
    with vault_module.AccountVault(selected.path) as reopened:
        assert reopened.pending_session(101) == saved_session()


def test_existing_normal_session_remains_separate_from_new_pending_candidate(pending_vault):
    old = saved_session(sid="synthetic-existing-ready-secret")
    with pending_vault._db:
        pending_vault._db.execute(
            "UPDATE accounts SET session=?,state='ready',checked_at_utc=? WHERE source_row=101",
            (vault_module._pack(old), STAMP),
        )
    before = account_state(pending_vault)
    pending_vault.save_pending_session(101, saved_session())
    assert account_state(pending_vault) == before
    assert pending_vault.session(101) == old
    assert pending_vault.pending_session(101) == saved_session()


def test_replacing_pending_candidate_preserves_other_rows(pending_vault):
    pending_vault.save_pending_session(101, saved_session())
    pending_vault.save_pending_session(102, saved_session(102))
    replacement = saved_session(sid="synthetic-newer-issued-secret")
    pending_vault.save_pending_session(101, replacement)
    assert pending_vault.pending_session(101) == replacement
    assert pending_vault.pending_session(102) == saved_session(102)
    assert account_state(pending_vault) == ("login_required", None, None)


@pytest.mark.parametrize("row", [0, -1, True, False, 1.0, "101", None, [], {}])
@pytest.mark.parametrize("method", ["save", "load"])
def test_invalid_source_rows_fail_before_database_access(row, method):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    with pytest.raises(SessionError, match="positive integer"):
        if method == "save":
            selected.save_pending_session(row, saved_session())
        else:
            selected.pending_session(row)


@pytest.mark.parametrize("saved", [{}, saved_session(102), None, {"format_version": 99}])
def test_invalid_or_wrong_identity_candidates_never_create_optional_schema(pending_vault, saved):
    # Format and identity guards retain their category instead of masquerading
    # as a failure of local persistence.
    with pytest.raises(SessionError) as failure:
        pending_vault.save_pending_session(101, saved)
    assert_safe(failure.value)
    assert not pending_table_exists(pending_vault)
    assert account_state(pending_vault) == ("login_required", None, None)


@pytest.mark.parametrize("method", ["save", "load"])
def test_missing_account_rejects_with_fixed_message(pending_vault, method):
    with pytest.raises(SessionError) as failure:
        if method == "save":
            pending_vault.save_pending_session(999, saved_session(999))
        else:
            pending_vault.pending_session(999)
    assert_safe(failure.value)
    assert not pending_table_exists(pending_vault)


def test_encryption_failure_does_not_mutate_account_or_schema(pending_vault, monkeypatch):
    def fail(_saved):
        raise OSError(SID + PASSWORD)

    monkeypatch.setattr(vault_module, "_pack", fail)
    with pytest.raises(SessionError, match="could not be saved securely") as failure:
        pending_vault.save_pending_session(101, saved_session())
    assert_safe(failure.value)
    assert not pending_table_exists(pending_vault)
    assert account_state(pending_vault) == ("login_required", None, None)


@pytest.mark.parametrize("existing", [False, True])
def test_pending_insert_failure_rolls_back_schema_or_keeps_existing_candidate(pending_vault, existing):
    if existing:
        pending_vault.save_pending_session(101, saved_session())

    def deny(action, name, *_rest):
        if action == sqlite3.SQLITE_INSERT and name == "account_pending_sessions":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    pending_vault._db.set_authorizer(deny)
    try:
        with pytest.raises(SessionError, match="could not be saved securely") as failure:
            pending_vault.save_pending_session(101, saved_session(sid="synthetic-replacement-secret"))
    finally:
        pending_vault._db.set_authorizer(None)
    assert_safe(failure.value)
    assert pending_table_exists(pending_vault) is existing
    if existing:
        assert pending_vault.pending_session(101) == saved_session()
    assert account_state(pending_vault) == ("login_required", None, None)


@pytest.mark.parametrize("corrupted", [b"corrupted-protected-data", {}, saved_session(102)])
def test_pending_load_validates_protected_envelope_and_account_binding(pending_vault, corrupted):
    pending_vault.save_pending_session(101, saved_session())
    blob = corrupted if type(corrupted) is bytes else vault_module._pack(corrupted)
    with pending_vault._db:
        pending_vault._db.execute("UPDATE account_pending_sessions SET session=? WHERE source_row=101", (blob,))
    with pytest.raises(SessionError, match="could not be unlocked or validated") as failure:
        pending_vault.pending_session(101)
    assert_safe(failure.value)
    assert account_state(pending_vault) == ("login_required", None, None)


def test_successful_attach_clears_only_target_pending_candidate_after_validation(pending_vault, fake_session):
    pending_vault.save_pending_session(101, saved_session())
    pending_vault.save_pending_session(102, saved_session(102))
    report = pending_vault.attach(101, saved_session())
    assert report["authenticated"] is True
    assert len(fake_session["calls"]) == 1
    assert pending_vault.session(101) == saved_session()
    assert pending_vault.pending_session(101) is None
    assert pending_vault.pending_session(102) == saved_session(102)
    assert account_state(pending_vault)[0] == "ready"
    assert 101 not in pending_vault.enrolled_test_rows()


def test_attach_read_failure_keeps_pending_candidate_and_normal_state(pending_vault, fake_session):
    pending_vault.save_pending_session(101, saved_session())
    before = account_state(pending_vault)
    fake_session["failure"] = RequestFailure("request_transport_failed", stage="negative_control", curl_code=7)
    with pytest.raises(RequestFailure):
        pending_vault.attach(101, saved_session())
    assert pending_vault.pending_session(101) == saved_session()
    assert account_state(pending_vault) == before


def test_pending_clear_failure_rolls_back_ready_save_and_account_review_clear(pending_vault, fake_session):
    pending_vault.save_pending_session(101, saved_session())
    with pending_vault._db:
        pending_vault._db.execute("UPDATE accounts SET state='account_failed' WHERE source_row=101")
        pending_vault._db.execute("CREATE TABLE account_failure_review (source_row INTEGER PRIMARY KEY)")
        pending_vault._db.execute("INSERT INTO account_failure_review VALUES (101)")
    before = account_state(pending_vault)

    def deny(action, name, *_rest):
        if action == sqlite3.SQLITE_DELETE and name == "account_pending_sessions":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    pending_vault._db.set_authorizer(deny)
    try:
        with pytest.raises(SessionError, match="verified session could not be saved securely") as failure:
            pending_vault.attach(101, saved_session())
    finally:
        pending_vault._db.set_authorizer(None)
    assert_safe(failure.value)
    assert account_state(pending_vault) == before
    assert pending_vault.pending_session(101) == saved_session()
    assert pending_vault._db.execute("SELECT source_row FROM account_failure_review").fetchall() == [(101,)]


def test_attach_without_pending_table_does_not_create_optional_schema(pending_vault, fake_session):
    pending_vault.attach(101, saved_session())
    assert not pending_table_exists(pending_vault)
    assert pending_vault.session(101) == saved_session()
