"""Like history is offline, durable, alias-bound, and never replays held writes."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import sqlite3
from threading import Barrier

import pytest

from anghami_session import client, like_test, vault
from anghami_session.errors import SessionError
from anghami_session.like_history import LikeHistoryError, LikeHistoryLedger, classify_like_report
from anghami_session.play_record import TEST_SONG_ID, _journal


SONG = TEST_SONG_ID
OTHER_SONG = str(int(SONG) + 1)
SOURCE = "b" * 64
JOB = "c" * 32
SECRET = "synthetic-like-history-private-value"


@pytest.fixture
def history_vault(tmp_path, monkeypatch):
    protected = {}
    def crypt(raw, *, decrypt=False):
        raw = bytes(raw)
        if decrypt:
            return protected[raw]
        encrypted = b"synthetic-protected:" + hashlib.sha256(raw).digest()
        protected[encrypted] = raw
        return encrypted
    monkeypatch.setattr(vault, "_crypt", crypt)
    monkeypatch.setattr(client.requests, "Session", lambda **_: pytest.fail("Unexpected real HTTP"))
    key = b"synthetic-history-index-key"
    path = tmp_path / "history.sqlite3"
    with sqlite3.connect(path) as database:
        database.executescript("""
            CREATE TABLE metadata (name TEXT PRIMARY KEY,value BLOB NOT NULL);
            CREATE TABLE accounts (source_row INTEGER PRIMARY KEY,email_key TEXT NOT NULL,
                record BLOB NOT NULL,session BLOB,state TEXT NOT NULL,checked_at_utc TEXT);
            CREATE INDEX account_email ON accounts(email_key);
        """)
        database.executemany("INSERT INTO metadata VALUES (?,?)", {
            "format_version": "1", "index_key": crypt(key), "source_sha256": SOURCE,
        }.items())
        for row, email in [(1, "synthetic@example.com"), (2, " SYNTHETIC@EXAMPLE.COM "), (3, "other@example.invalid")]:
            record = {"source_row": row, "country": "EG", "email": email, "password": SECRET}
            saved = {"format_version": 1, "created_at_utc": "2026-10-04T00:00:00+00:00",
                "origin": "https://play.anghami.com", "account_email": email.strip().casefold(),
                "requests": {"relations": {"method": "GET", "url": client.GATEWAY_URL + "?type=GETuserrelations&sid=" + SECRET,
                    "headers": {"cookie": "appsidsave=" + SECRET}}}}
            database.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?)", (
                row, vault._email_key(key, email), vault._pack(record), vault._pack(saved), "ready", "2026-10-04T00:00:00+00:00"))
    with vault.AccountVault(path) as store:
        yield store


def facts(row=1, song=SONG, *, state="confirmed", attempted=True):
    return {
        "source_row": row, "song_id": song, "passed": state == "confirmed",
        "authenticated": True, "negative_control_passed": True,
        "server_account_identity_verified": True, "metadata_verified": True,
        "liked_before": not attempted, "liked_after": state == "confirmed",
        "persisted_state_verified": state == "confirmed",
        "mutation_attempted": attempted, "mutation_attempts": int(attempted),
        "mutation_accepted": True if state in {"confirmed", "verification_pending"} and attempted else None,
        "mutation_result": "accepted" if state in {"confirmed", "verification_pending"} and attempted else "unknown" if attempted else "skipped_already_liked",
        "cookie": SECRET, "email": "synthetic@example.com", "raw_response": SECRET,
    }


def remember(store, report, **options):
    return store.remember_like(report["source_row"], report["song_id"], report, source_sha256=SOURCE, job_id=JOB, **options)


def assert_private(value):
    encoded = json.dumps(value)
    for text in (SECRET, "synthetic@example.com", "SYNTHETIC@EXAMPLE.COM", "other@example.invalid", "cookie", "raw_response", "identity_key"):
        assert text not in encoded


def test_confirmed_history_deduplicates_aliases_and_is_specific_to_song(history_vault):
    report = facts()
    result = remember(history_vault, report)
    assert result == {"source_row": 1, "song_id": SONG, "history_status": "confirmed", "recorded": True}
    history = history_vault.like_history(SONG, rows=[1, 2, 3])
    assert history["blocked_rows"] == [1, 2] and history["eligible_rows"] == [3]
    assert history["counts"]["confirmed"] == 2
    assert history_vault.like_history(OTHER_SONG, rows=[1, 2, 3])["eligible_rows"] == [1, 2, 3]
    assert_private(history)
    stored = history_vault._db.execute("SELECT * FROM account_song_like_history").fetchall()
    assert len(stored) == 1
    assert SECRET not in repr(stored) and "synthetic@example.com" not in repr(stored)


def test_history_survives_session_expiry_and_process_restart(history_vault, monkeypatch):
    remember(history_vault, facts())
    history_vault._db.execute("UPDATE accounts SET session=NULL,state='login_required'")
    history_vault._db.commit()
    with vault.AccountVault(history_vault.path) as reopened:
        monkeypatch.setattr(reopened, "_http_session", lambda *_args, **_kwargs: pytest.fail("Confirmed history opened HTTP"))
        result = reopened.test_like(2, SONG)
        assert result["history_skipped"] is True and result["history_confirmed"] is True
        assert result["mutation_attempted"] is False and result["authenticated"] is False
        assert result["persisted_state_verified"] is False and result["error_code"] == "history_already_liked"
        assert reopened.like_history(OTHER_SONG, rows=[1, 2])["eligible_rows"] == [1, 2]
        assert_private(result)


@pytest.mark.parametrize("state", ["verification_pending", "write_unknown"])
def test_unverified_history_remains_held_without_proxy_or_session_calls(history_vault, monkeypatch, state):
    remember(history_vault, facts(state=state))
    monkeypatch.setattr(history_vault, "_http_session", lambda *_args, **_kwargs: pytest.fail("Held history opened HTTP"))
    result = history_vault.test_like(2, SONG, proxy=object())
    assert result["passed"] is False and result["history_status"] == state
    assert result["error_code"] == "history_verification_pending"
    assert result["mutation_attempted"] is False and result["verification_pending"] is True
    assert_private(result)


def test_authenticated_already_liked_read_creates_confirmed_history(history_vault):
    result = remember(history_vault, facts(attempted=False))
    assert result["history_status"] == "confirmed"


@pytest.mark.parametrize("missing", ["authenticated", "negative_control_passed", "server_account_identity_verified", "metadata_verified"])
def test_no_fresh_account_proof_never_records_confirmation(history_vault, missing):
    report = facts()
    report[missing] = False
    assert remember(history_vault, report)["recorded"] is False
    assert history_vault.like_history(SONG, rows=[1])["eligible_rows"] == [1]


@pytest.mark.parametrize("wrong", [{"source_row": 2}, {"source_row": True}, {"song_id": OTHER_SONG}])
def test_backfill_requires_exact_row_and_song(history_vault, wrong):
    report = facts()
    report.update(wrong)
    with pytest.raises(LikeHistoryError):
        history_vault.remember_like(1, SONG, report, source_sha256=SOURCE)
    assert history_vault.like_history(SONG, rows=[1])["eligible_rows"] == [1]


@pytest.mark.parametrize("source,key", [("a" * 64, None), (SOURCE, "d" * 64), ("secret@invalid", None), (SOURCE, "non-ascii-\u2603")])
def test_frozen_source_or_identity_mismatch_is_rejected(history_vault, source, key):
    with pytest.raises(LikeHistoryError):
        history_vault.remember_like(1, SONG, facts(), source_sha256=source, identity_key=key)
    assert history_vault.like_history(SONG, rows=[1])["eligible_rows"] == [1]


def test_binding_detects_account_index_identity_tamper(history_vault):
    history_vault._db.execute("UPDATE accounts SET email_key=? WHERE source_row=1", ("d" * 64,))
    history_vault._db.commit()
    with pytest.raises(LikeHistoryError):
        remember(history_vault, facts())


def test_known_acceptance_never_downgrades_or_becomes_confirmation_without_readback(history_vault):
    remember(history_vault, facts(state="verification_pending"))
    assert remember(history_vault, facts(state="write_unknown"))["history_status"] == "verification_pending"
    assert remember(history_vault, facts())["history_status"] == "confirmed"
    assert remember(history_vault, facts(state="write_unknown"))["history_status"] == "confirmed"


def test_stored_state_readback_can_confirm_even_when_original_mutation_raised(history_vault):
    report = facts(state="write_unknown")
    report.update(liked_after=True, persisted_state_verified=True, passed=False)
    assert remember(history_vault, report)["history_status"] == "confirmed"


def test_a_previous_history_skip_cannot_forge_fresh_confirmation(history_vault):
    report = facts()
    report["history_skipped"] = True
    assert remember(history_vault, report)["recorded"] is False


def test_history_polling_uses_sql_hash_join_without_decrypting_accounts(history_vault, monkeypatch):
    remember(history_vault, facts())
    monkeypatch.setattr(vault, "_unpack", lambda _: pytest.fail("Polling decrypted an account"))
    assert history_vault.like_history(SONG, rows=[1, 2, 3])["blocked_rows"] == [1, 2]


def test_concurrent_alias_reservations_have_exactly_one_owner(history_vault):
    binding = history_vault.like_history_binding(1)
    barrier = Barrier(2)
    def reserve(row):
        with vault.AccountVault(history_vault.path) as opened:
            barrier.wait(timeout=5)
            return LikeHistoryLedger(opened._db).reserve(binding["identity_key"], SONG, row, SOURCE)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(reserve, [1, 2]))
    assert sum(owner is not None for owner, _ in results) == 1
    assert all(state == "in_progress" for _, state in results)
    assert history_vault.like_history(SONG, rows=[1, 2])["counts"]["in_progress"] == 2


def test_abandoned_reservation_does_not_expire_or_replay_after_restart(history_vault, monkeypatch):
    binding = history_vault.like_history_binding(1)
    LikeHistoryLedger(history_vault._db).reserve(binding["identity_key"], SONG, 1, SOURCE)
    with vault.AccountVault(history_vault.path) as reopened:
        monkeypatch.setattr(reopened, "_http_session", lambda *_args, **_kwargs: pytest.fail("An abandoned reservation replayed"))
        assert reopened.test_like(2, SONG)["history_status"] == "in_progress"


def test_local_skip_does_not_overwrite_an_active_peers_mutation_journal(history_vault):
    binding = history_vault.like_history_binding(1)
    ledger = LikeHistoryLedger(history_vault._db)
    owner, _ = ledger.reserve(binding["identity_key"], SONG, 1, SOURCE)
    report = facts(state="write_unknown")
    ledger.observe(binding["identity_key"], SONG, 1, SOURCE, report, owner=owner)
    path = history_vault.path.parent / "account-1.test-like-report.json"
    _journal(report, path)
    before = path.read_bytes()
    assert history_vault.test_like(1, SONG)["history_status"] == "write_unknown"
    assert path.read_bytes() == before


def test_preflight_exception_releases_only_its_nonmutation_reservation(history_vault, monkeypatch):
    def preflight(*_args, **_kwargs):
        raise SessionError("Synthetic preflight failure")
    monkeypatch.setattr(history_vault, "_http_session", preflight)
    with pytest.raises(SessionError):
        history_vault.test_like(1, SONG)
    assert history_vault.like_history(SONG, rows=[1, 2])["eligible_rows"] == [1, 2]


@pytest.mark.parametrize("state", ["confirmed", "verification_pending", "write_unknown"])
def test_vault_records_durable_returned_reports_and_skips_second_attempt(history_vault, monkeypatch, state):
    calls = []
    @contextmanager
    def session(*_args, **_kwargs):
        calls.append("session")
        yield type("SyntheticSession", (), {})()
    def runner(selected, song_id, *, report_path):
        report = facts(state=state)
        _journal(report, report_path)
        return report
    monkeypatch.setattr(history_vault, "_http_session", session)
    monkeypatch.setattr(like_test, "run_like_test", runner)
    assert history_vault.test_like(1, SONG)["source_row"] == 1
    assert history_vault.test_like(2, SONG)["history_status"] == state
    assert calls == ["session"]


@pytest.mark.parametrize("state", ["confirmed", "verification_pending", "write_unknown"])
def test_vault_records_durable_reports_even_when_runner_raises(history_vault, monkeypatch, state):
    @contextmanager
    def session(*_args, **_kwargs):
        yield type("SyntheticSession", (), {})()
    def runner(selected, song_id, *, report_path):
        report = facts(state=state)
        _journal(report, report_path)
        raise SessionError("Synthetic final-stage failure")
    monkeypatch.setattr(history_vault, "_http_session", session)
    monkeypatch.setattr(like_test, "run_like_test", runner)
    with pytest.raises(SessionError):
        history_vault.test_like(1, SONG)
    assert history_vault.like_history(SONG, rows=[1])["accounts"][0]["state"] == state


def test_real_like_adapter_saves_intent_before_post_then_confirmed_readback(history_vault, monkeypatch):
    from test_like import add_state, bootstrap, saved_session
    selected = saved_session.__wrapped__()
    bootstrap.__wrapped__(monkeypatch)
    add_state(selected._http, ["42"])
    selected._http.replies.append(__import__("test_like").Reply({"status": "ok"}))
    add_state(selected._http, ["42", SONG])
    def at_post():
        assert history_vault.like_history(SONG, rows=[1])["accounts"][0]["state"] == "write_unknown"
        raw = json.loads((history_vault.path.parent / "account-1.test-like-report.json").read_text())
        assert raw["mutation_attempted"] is True and raw["mutation_result"] == "unknown"
    selected._http.on_post = at_post
    @contextmanager
    def session(*_args, **_kwargs):
        yield selected
    monkeypatch.setattr(history_vault, "_http_session", session)
    result = history_vault.test_like(1, SONG)
    assert result["passed"] is True
    assert history_vault.like_history(SONG, rows=[1])["accounts"][0]["state"] == "confirmed"
    assert history_vault.test_like(2, SONG)["history_confirmed"] is True
    assert len([call for call in selected._http.calls if call[0] == "POST"]) == 1


@pytest.mark.parametrize("mode,expected", [
    ("already", "confirmed"), ("accepted_unverified", "verification_pending"),
    ("timeout_unverified", "write_unknown"), ("timeout_verified", "confirmed"),
    ("rejected", None),
])
def test_real_like_result_and_exception_paths_preserve_exact_history_state(history_vault, monkeypatch, mode, expected):
    from test_like import Reply, TransportFailure, add_state, bootstrap, saved_session
    selected = saved_session.__wrapped__()
    bootstrap.__wrapped__(monkeypatch)
    add_state(selected._http, ["42", SONG] if mode == "already" else ["42"])
    if mode != "already":
        selected._http.replies.append(
            TransportFailure(28) if mode.startswith("timeout") else Reply({"status": "failed"}) if mode == "rejected" else Reply({"status": "ok"})
        )
        add_state(selected._http, ["42", SONG] if mode == "timeout_verified" else ["42"])
    @contextmanager
    def session(*_args, **_kwargs):
        yield selected
    monkeypatch.setattr(history_vault, "_http_session", session)
    if mode == "already":
        assert history_vault.test_like(1, SONG)["passed"] is True
    else:
        with pytest.raises(SessionError):
            history_vault.test_like(1, SONG)
    history = history_vault.like_history(SONG, rows=[1])
    if expected is None:
        assert history["eligible_rows"] == [1]
    else:
        assert history["accounts"][0]["state"] == expected
        assert history_vault.test_like(2, SONG)["history_status"] == expected
    assert len([call for call in selected._http.calls if call[0] == "POST"]) == (0 if mode == "already" else 1)


def test_ledger_failure_at_intent_blocks_http_post_and_retains_hold(history_vault, monkeypatch):
    from test_like import add_state, bootstrap, saved_session
    selected = saved_session.__wrapped__()
    bootstrap.__wrapped__(monkeypatch)
    add_state(selected._http, ["42"])
    original = LikeHistoryLedger.observe
    def failed_save(self, identity, song, row, source, report, **options):
        if report.get("mutation_attempted") is True:
            raise LikeHistoryError()
        return original(self, identity, song, row, source, report, **options)
    monkeypatch.setattr(LikeHistoryLedger, "observe", failed_save)
    @contextmanager
    def session(*_args, **_kwargs):
        yield selected
    monkeypatch.setattr(history_vault, "_http_session", session)
    with pytest.raises(SessionError):
        history_vault.test_like(1, SONG)
    assert not [call for call in selected._http.calls if call[0] == "POST"]
    assert history_vault.like_history(SONG, rows=[1])["accounts"][0]["state"] == "in_progress"


def test_storage_failure_after_already_liked_proof_retains_reservation(history_vault, monkeypatch):
    @contextmanager
    def session(*_args, **_kwargs):
        yield type("SyntheticSession", (), {})()
    def runner(selected, song_id, *, report_path):
        report = facts(attempted=False)
        _journal(report, report_path)
        return report
    monkeypatch.setattr(history_vault, "_http_session", session)
    monkeypatch.setattr(like_test, "run_like_test", runner)
    monkeypatch.setattr(LikeHistoryLedger, "observe", lambda *_args, **_kwargs: (_ for _ in ()).throw(LikeHistoryError()))
    with pytest.raises(LikeHistoryError):
        history_vault.test_like(1, SONG)
    assert history_vault.like_history(SONG, rows=[1])["accounts"][0]["state"] == "in_progress"


def test_old_per_row_report_cannot_be_imported_after_new_preflight_failure(history_vault, monkeypatch):
    path = history_vault.path.parent / "account-1.test-like-report.json"
    _journal(facts(), path)
    monkeypatch.setattr(history_vault, "_http_session", lambda *_args, **_kwargs: (_ for _ in ()).throw(SessionError("Synthetic fail")))
    with pytest.raises(SessionError):
        history_vault.test_like(1, OTHER_SONG, declared_song_id=OTHER_SONG)
    assert history_vault.like_history(SONG, rows=[1])["eligible_rows"] == [1]
    assert history_vault.like_history(OTHER_SONG, rows=[1])["eligible_rows"] == [1]


def test_history_does_not_modify_preparation_or_account_states(history_vault):
    before = history_vault._db.execute("SELECT source_row,state,session,record FROM accounts ORDER BY source_row").fetchall()
    remember(history_vault, facts())
    remember(history_vault, facts(row=3, state="write_unknown"))
    after = history_vault._db.execute("SELECT source_row,state,session,record FROM accounts ORDER BY source_row").fetchall()
    assert before == after


def test_known_rejection_does_not_clear_an_imported_unknown_hold(history_vault):
    remember(history_vault, facts(state="write_unknown"))
    report = facts(state="write_unknown")
    report.update(mutation_accepted=False, mutation_result="rejected")
    assert remember(history_vault, report)["history_status"] == "write_unknown"


def test_tampered_timestamp_cannot_escape_in_safe_history(history_vault):
    remember(history_vault, facts())
    history_vault._db.execute("UPDATE account_song_like_history SET updated_at_utc=?", ("synthetic@example.com",))
    history_vault._db.commit()
    with pytest.raises(LikeHistoryError):
        history_vault.like_history(SONG, rows=[1])
