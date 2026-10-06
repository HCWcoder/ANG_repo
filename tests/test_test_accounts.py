"""Managed test enrollment uses temporary synthetic accounts and no network."""

from copy import deepcopy
import json
import sqlite3

import pytest

from anghami_session import client, like_test, play_record, vault as vault_module
from anghami_session.errors import SessionError, SessionStorageError


SECRET = "synthetic-account-password-and-session-secret"
DEFAULT_ROWS = frozenset({1, 2, 3, 4, 5, 7})
STAMP = "2026-10-01T00:00:00+00:00"


def saved_session(email):
    return {
        "format_version": 1, "created_at_utc": STAMP,
        "origin": "https://play.anghami.com", "account_email": email.strip().casefold(),
        "requests": {"relations": {
            "method": "GET",
            "url": client.GATEWAY_URL + "?type=GETuserrelations&sid=" + SECRET,
            "headers": {"cookie": "appsidsave=" + SECRET},
        }},
    }


@pytest.fixture
def synthetic_vault(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Managed-cohort tests must never create a real HTTP transport")

    monkeypatch.setattr(client.requests, "Session", forbidden)
    # Storage encryption is replaced only for this disposable synthetic database.
    # Real DPAPI behavior is covered separately by test_account_vault.py.
    monkeypatch.setattr(vault_module, "_crypt", lambda value, decrypt=False: value)
    path = tmp_path / "synthetic.sqlite3"
    key = b"synthetic-email-index-key"
    identities = {row: f"account-{row}@example.invalid" for row in range(1, 15)}
    identities.update({
        6: identities[1].upper(),
        9: identities[8].upper(),
        12: identities[3],
    })
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE metadata (name TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE accounts (
                source_row INTEGER PRIMARY KEY, email_key TEXT NOT NULL,
                record BLOB NOT NULL, session BLOB,
                state TEXT NOT NULL DEFAULT 'login_required', checked_at_utc TEXT
            );
            CREATE INDEX account_email ON accounts(email_key);
        """)
        connection.executemany("INSERT INTO metadata VALUES (?, ?)", {
            "format_version": "1", "index_key": key, "imported_at_utc": STAMP,
            "source_sha256": "a" * 64, "backup_relative": "backups/synthetic.dpapi",
        }.items())
        for row, email in identities.items():
            record = {
                "source_row": row, "country": "EG", "email": email,
                "password": SECRET, "legacy_metadata": {}, "legacy_cookies": {},
            }
            connection.execute(
                "INSERT INTO accounts(source_row, email_key, record) VALUES (?, ?, ?)",
                (row, vault_module._email_key(key, email), vault_module._pack(record)),
            )
    with vault_module.AccountVault(path) as selected:
        yield selected


def mark_ready(selected, row, *, state="ready", saved=None):
    if saved is None:
        saved = saved_session(selected.record(row)["email"])
    with selected._db:
        selected._db.execute(
            "UPDATE accounts SET state=?, session=? WHERE source_row=?",
            (state, vault_module._pack(saved), row),
        )
    return saved


def has_cohort_table(selected):
    return selected._db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='test_accounts'",
    ).fetchone() is not None


@pytest.fixture
def fake_session(monkeypatch):
    state = {"events": [], "sessions": [], "failure": None}

    class Session:
        def __init__(self, *, saved, **options):
            self.saved = deepcopy(saved)
            self.options = options
            state["sessions"].append(self)
            state["events"].append("transport")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            state["events"].append("closed")

        def check(self, *, negative_control):
            assert negative_control is True
            state["events"].append("positive_and_negative_check")
            if state["failure"]:
                raise state["failure"]
            return {
                "authenticated": True, "checked_at_utc": STAMP,
                "without_session_rejected": True, "browser_required": False,
            }

    monkeypatch.setattr(vault_module, "AnghamiSession", Session)
    return state


class FakeProxy:
    def __init__(self, events, *, fail=False):
        self.events = events
        self.fail = fail
        self.verifications = 0

    def verify_country(self):
        self.verifications += 1
        self.events.append("proxy_country")
        if self.fail:
            raise SessionError("The proxy country was not confirmed.")
        return {"country": "EG", "country_verified": True, "proxy_used": True}


def test_status_and_selection_do_not_create_or_change_optional_schema(synthetic_vault):
    selected = synthetic_vault
    original = selected.path.read_bytes()
    traced = []
    selected._db.set_trace_callback(traced.append)
    assert selected.enrolled_test_rows() == DEFAULT_ROWS
    assert selected.summary()["records"] == 14
    assert selected.test_accounts()["test_rows"] == sorted(DEFAULT_ROWS)
    assert selected.select_test_candidates(5) == [8, 10, 11, 13, 14]
    assert not has_cohort_table(selected)
    assert selected.path.read_bytes() == original
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in traced)
    assert not any(SECRET in statement for statement in traced)


def test_initial_cohort_fallback_supports_existing_synthetic_vaults():
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    assert selected.enrolled_test_rows() == DEFAULT_ROWS == play_record.TEST_ACCOUNT_ROWS


def test_safe_status_lists_only_existing_rows_with_saved_ready_sessions(synthetic_vault):
    selected = synthetic_vault
    mark_ready(selected, 1)
    mark_ready(selected, 2, state="check_failed")
    with selected._db:
        selected._db.execute("UPDATE accounts SET state='ready' WHERE source_row=3")
        selected._db.execute("DELETE FROM accounts WHERE source_row=5")
    report = selected.test_accounts()
    assert report["test_rows"] == [1, 2, 3, 4, 7]
    assert report["ready_rows"] == [1]
    assert report["accounts"] == [
        {"source_row": 1, "state": "ready", "session_saved": True},
        {"source_row": 2, "state": "check_failed", "session_saved": True},
        {"source_row": 3, "state": "ready", "session_saved": False},
        {"source_row": 4, "state": "login_required", "session_saved": False},
        {"source_row": 7, "state": "login_required", "session_saved": False},
    ]
    assert SECRET not in json.dumps(report)
    assert "example.invalid" not in json.dumps(report)
    assert not has_cohort_table(selected)


def test_candidates_are_ordered_distinct_and_exclude_all_enrolled_identities(synthetic_vault):
    selected = synthetic_vault
    assert selected.select_test_candidates(5) == [8, 10, 11, 13, 14]
    assert selected.select_test_candidates(3, start_row=9) == [9, 10, 11]
    mark_ready(selected, 8)
    selected.enable_test_account(8)
    assert selected.select_test_candidates(5) == [10, 11, 13, 14]
    assert selected.select_test_candidates(5, start_row=14) == [14]
    assert selected.select_test_candidates(1, start_row=15) == []


@pytest.mark.parametrize("count,start_row", [
    (0, 1), (-1, 1), (True, 1), ("2", 1), (1, 0), (1, True), (1, "8"),
])
def test_invalid_candidate_bounds_are_rejected_before_database_access(count, start_row):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    with pytest.raises(SessionError):
        selected.select_test_candidates(count, start_row=start_row)


@pytest.mark.parametrize("case", ["missing", "no_session", "not_ready", "wrong_identity", "bad_format"])
def test_failed_enrollment_never_creates_a_table_or_expands_cohort(synthetic_vault, case):
    selected = synthetic_vault
    row = 99 if case == "missing" else 8
    if case == "no_session":
        with selected._db:
            selected._db.execute("UPDATE accounts SET state='ready' WHERE source_row=8")
    elif case == "not_ready":
        mark_ready(selected, row, state="check_failed")
    elif case == "wrong_identity":
        mark_ready(selected, row, saved=saved_session("unrelated@example.invalid"))
    elif case == "bad_format":
        mark_ready(selected, row, saved={"unsupported": SECRET})
    with pytest.raises(SessionError) as error:
        selected.enable_test_account(row)
    assert SECRET not in str(error.value)
    assert selected.enrolled_test_rows() == DEFAULT_ROWS
    assert not has_cohort_table(selected)


def test_successful_enrollment_is_bound_persistent_and_idempotent(synthetic_vault):
    selected = synthetic_vault
    mark_ready(selected, 8)
    report = selected.enable_test_account(8)
    assert report == {
        "source_row": 8, "enabled": True, "already_enabled": False,
        "state": "ready", "session_saved": True,
    }
    stored = selected._db.execute("SELECT * FROM test_accounts").fetchall()
    assert len(stored) == 1 and stored[0][0] == 8 and isinstance(stored[0][1], str)
    assert SECRET not in json.dumps(report)
    assert "example.invalid" not in json.dumps(report)
    again = selected.enable_test_account(8)
    assert again == {**report, "already_enabled": True}
    assert selected._db.execute("SELECT * FROM test_accounts").fetchall() == stored
    with vault_module.AccountVault(selected.path) as reopened:
        assert reopened.enrolled_test_rows() == DEFAULT_ROWS | {8}
        assert reopened.test_accounts()["ready_rows"] == [8]
        assert reopened.select_test_candidates(5) == [10, 11, 13, 14]


def test_failed_insert_rolls_back_optional_table_creation(synthetic_vault):
    selected = synthetic_vault
    mark_ready(selected, 8)

    def deny_enrollment_insert(action, name, *_rest):
        if action == sqlite3.SQLITE_INSERT and name == "test_accounts":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    selected._db.set_authorizer(deny_enrollment_insert)
    try:
        with pytest.raises(SessionStorageError) as failure:
            selected.enable_test_account(8)
        assert failure.value.sqlite_code == sqlite3.SQLITE_AUTH
        assert failure.value.attempts == 1
    finally:
        selected._db.set_authorizer(None)
    assert selected.enrolled_test_rows() == DEFAULT_ROWS
    assert not has_cohort_table(selected)
    assert selected.session(8)["account_email"] == "account-8@example.invalid"


def test_default_attach_keeps_original_signature_and_does_not_enroll(synthetic_vault, fake_session):
    selected = synthetic_vault
    saved = saved_session(selected.record(8)["email"])
    report = selected.attach(8, saved)
    assert fake_session["sessions"][0].options == {}
    assert fake_session["events"] == ["transport", "positive_and_negative_check", "closed"]
    assert report["authenticated"] is True
    assert selected.session(8) == saved
    assert selected.enrolled_test_rows() == DEFAULT_ROWS
    assert not has_cohort_table(selected)


def test_proxy_attach_verifies_country_before_origin_and_reuses_exact_route(synthetic_vault, fake_session):
    selected = synthetic_vault
    proxy = FakeProxy(fake_session["events"])
    saved = saved_session(selected.record(8)["email"])
    selected.attach(8, saved, proxy=proxy)
    assert fake_session["events"] == ["proxy_country", "transport", "positive_and_negative_check", "closed"]
    assert proxy.verifications == 1
    session = fake_session["sessions"][0]
    assert session.options == {"proxy": proxy}
    assert session._proxy_check == {"country": "EG", "country_verified": True, "proxy_used": True}
    assert selected.session(8) == saved
    assert not has_cohort_table(selected)


@pytest.mark.parametrize("failure", ["proxy", "authentication", "identity"])
def test_failed_attach_preserves_prior_session_and_never_enrolls(synthetic_vault, fake_session, failure):
    selected = synthetic_vault
    prior = mark_ready(selected, 8)
    replacement = deepcopy(prior)
    replacement["created_at_utc"] = "2026-10-02T00:00:00+00:00"
    proxy = FakeProxy(fake_session["events"], fail=failure == "proxy")
    if failure == "authentication":
        fake_session["failure"] = SessionError("The saved session was not accepted.")
    elif failure == "identity":
        replacement["account_email"] = "wrong@example.invalid"
    with pytest.raises(SessionError):
        selected.attach(8, replacement, proxy=proxy, new_password="synthetic-replacement")
    assert selected.session(8) == prior
    assert selected.record(8)["password"] == SECRET
    assert selected.enrolled_test_rows() == DEFAULT_ROWS
    assert not has_cohort_table(selected)
    if failure != "authentication":
        assert fake_session["sessions"] == []
    if failure == "identity":
        assert proxy.verifications == 0


@pytest.mark.parametrize("command", ["test_like", "test_play_record"])
def test_newly_enrolled_account_is_admitted_with_only_its_bound_session(synthetic_vault, fake_session, monkeypatch, command):
    selected = synthetic_vault
    saved = mark_ready(selected, 8)
    selected.enable_test_account(8)
    calls = []

    def runner(session, song_id, *, report_path):
        calls.append((session.saved, song_id, report_path))
        return {"passed": True, "song_id": song_id}

    module = like_test if command == "test_like" else play_record
    name = "run_like_test" if command == "test_like" else "run_play_record_test"
    monkeypatch.setattr(module, name, runner)
    result = getattr(selected, command)(8, play_record.TEST_SONG_ID)
    assert result == {"source_row": 8, "passed": True, "song_id": play_record.TEST_SONG_ID}
    expected_path = selected.path.parent / ("account-8." + command.replace("_", "-") + "-report.json")
    assert calls == [(saved, play_record.TEST_SONG_ID, expected_path)]
    assert json.loads(expected_path.read_text(encoding="utf-8")) == result
    assert fake_session["sessions"][0].options == {}


@pytest.mark.parametrize("command", ["test_like", "test_play_record"])
def test_unenrolled_account_and_wrong_track_fail_before_proxy_or_transport(synthetic_vault, fake_session, command):
    selected = synthetic_vault
    proxy = FakeProxy(fake_session["events"])
    with pytest.raises(SessionError, match="selected test accounts"):
        getattr(selected, command)(10, play_record.TEST_SONG_ID, proxy=proxy)
    mark_ready(selected, 8)
    selected.enable_test_account(8)
    with pytest.raises(SessionError, match="declared test song"):
        getattr(selected, command)(8, "42", proxy=proxy)
    assert fake_session["sessions"] == []
    assert proxy.verifications == 0
