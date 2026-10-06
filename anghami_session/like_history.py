"""Durable, identity-and-song-bound facts about completed like attempts.

Only the vault's HMAC identity index, numeric source row, song ID, fixed states,
and audit timestamps are stored. Sessions, credentials, responses, and email
addresses never enter this ledger. A reserved or uncertain attempt is held;
there is deliberately no expiry that silently permits another append.
"""

from datetime import datetime, timezone
import re
from uuid import uuid4

from .errors import SessionError
from .test_settings import validate_test_song_id


HISTORY_STATES = frozenset({"confirmed", "verification_pending", "write_unknown", "in_progress"})
_PRIORITY = {"in_progress": 0, "write_unknown": 1, "verification_pending": 2, "confirmed": 3}
_IDENTITY = re.compile(r"[0-9a-f]{64}\Z")
_OWNER = re.compile(r"[0-9a-f]{32}\Z")
_REJECTED = "known_rejected"


class LikeHistoryError(SessionError):
    code = "like_history_failed"

    def __init__(self):
        super().__init__("The durable like history could not be validated or saved. No automatic append retry is permitted.")


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _binding(identity_key, song_id, source_row=None, source_sha256=None, owner=None, job_id=None):
    if type(identity_key) is not str or not _IDENTITY.fullmatch(identity_key):
        raise LikeHistoryError()
    try:
        song = validate_test_song_id(song_id)
    except SessionError:
        raise LikeHistoryError() from None
    if source_row is not None and (type(source_row) is not int or not 1 <= source_row <= 2**31 - 1):
        raise LikeHistoryError()
    if source_sha256 is not None and (type(source_sha256) is not str or not _IDENTITY.fullmatch(source_sha256)):
        raise LikeHistoryError()
    if owner is not None and (type(owner) is not str or not _OWNER.fullmatch(owner)):
        raise LikeHistoryError()
    if job_id is not None and (type(job_id) is not str or not _OWNER.fullmatch(job_id)):
        raise LikeHistoryError()
    return song


def classify_like_report(report, source_row, song_id):
    """Require fresh account proof before trusting any mutation or stored state."""
    if type(report) is not dict or report.get("source_row") != source_row or type(report.get("source_row")) is not int or report.get("song_id") != song_id:
        raise LikeHistoryError()
    if report.get("history_skipped") is True:
        return None
    if not all(report.get(key) is True for key in (
        "authenticated", "negative_control_passed", "server_account_identity_verified", "metadata_verified",
    )):
        return None
    attempted, attempts = report.get("mutation_attempted"), report.get("mutation_attempts")
    if type(attempted) is not bool or type(attempts) is not int or attempts != int(attempted):
        return None
    if report.get("persisted_state_verified") is True and report.get("liked_after") is True:
        if attempted or report.get("liked_before") is True:
            return "confirmed"
    if not attempted:
        return None
    accepted, result = report.get("mutation_accepted"), report.get("mutation_result")
    if accepted is False and result == "rejected":
        return _REJECTED
    if accepted is True and result == "accepted":
        return "verification_pending"
    return "write_unknown"


class LikeHistoryLedger:
    def __init__(self, connection):
        self._db = connection

    def exists(self):
        return self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_song_like_history'").fetchone() is not None

    def _schema(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS account_song_like_history (
                email_key TEXT NOT NULL, song_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('confirmed','verification_pending','write_unknown','in_progress')),
                source_row INTEGER NOT NULL, source_sha256 TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL, owner TEXT, job_id TEXT,
                PRIMARY KEY(email_key,song_id)
            )
        """)

    def state(self, identity_key, song_id):
        song_id = _binding(identity_key, song_id)
        try:
            if not self.exists():
                return None
            row = self._db.execute("SELECT state FROM account_song_like_history WHERE email_key=? AND song_id=?", (identity_key, song_id)).fetchone()
            if row is None:
                return None
            if type(row[0]) is not str or row[0] not in HISTORY_STATES:
                raise LikeHistoryError()
            return row[0]
        except Exception:
            raise LikeHistoryError() from None

    def reserve(self, identity_key, song_id, source_row, source_sha256):
        """Claim one identity/song atomically before opening a proxy or session."""
        song_id = _binding(identity_key, song_id, source_row, source_sha256)
        owner = uuid4().hex
        try:
            with self._db:
                self._schema()
                inserted = self._db.execute(
                    "INSERT OR IGNORE INTO account_song_like_history VALUES (?,?,?,?,?,?,?,NULL)",
                    (identity_key, song_id, "in_progress", source_row, source_sha256, _stamp(), owner),
                ).rowcount
                row = self._db.execute("SELECT state,owner FROM account_song_like_history WHERE email_key=? AND song_id=?", (identity_key, song_id)).fetchone()
                if row is None or row[0] not in HISTORY_STATES:
                    raise LikeHistoryError()
                return (owner, "in_progress") if inserted == 1 else (None, row[0])
        except Exception:
            raise LikeHistoryError() from None

    def observe(self, identity_key, song_id, source_row, source_sha256, report, *, owner=None, job_id=None):
        song_id = _binding(identity_key, song_id, source_row, source_sha256, owner, job_id)
        state = classify_like_report(report, source_row, song_id)
        if state is None:
            return False
        try:
            with self._db:
                self._schema()
                existing = self._db.execute("SELECT state,owner FROM account_song_like_history WHERE email_key=? AND song_id=?", (identity_key, song_id)).fetchone()
                if existing is not None and existing[0] not in HISTORY_STATES:
                    raise LikeHistoryError()
                if owner is not None and (existing is None or existing[1] != owner):
                    raise LikeHistoryError()
                # A known rejection marks this call as safely releasable, but
                # retains its reservation until its remaining reads finish.
                # Imported historical failures cannot clear somebody's hold.
                if state == _REJECTED:
                    if owner is not None and existing[0] != "confirmed":
                        self._db.execute("UPDATE account_song_like_history SET state='in_progress',updated_at_utc=? WHERE email_key=? AND song_id=? AND owner=?", (_stamp(), identity_key, song_id, owner))
                    return False
                # Never downgrade a persisted proof or a known acceptance.
                if existing is not None and _PRIORITY[existing[0]] > _PRIORITY[state]:
                    return False
                self._db.execute(
                    "INSERT INTO account_song_like_history VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(email_key,song_id) DO UPDATE SET state=excluded.state,source_row=excluded.source_row,"
                    "source_sha256=excluded.source_sha256,updated_at_utc=excluded.updated_at_utc,"
                    "owner=COALESCE(excluded.owner,account_song_like_history.owner),job_id=COALESCE(excluded.job_id,account_song_like_history.job_id)",
                    (identity_key, song_id, state, source_row, source_sha256, _stamp(), owner, job_id),
                )
                return True
        except Exception:
            raise LikeHistoryError() from None

    def release_prewrite(self, identity_key, song_id, owner):
        """Release only an orderly call that never reached mutation intent."""
        song_id = _binding(identity_key, song_id, owner=owner)
        try:
            with self._db:
                self._db.execute(
                    "DELETE FROM account_song_like_history WHERE email_key=? AND song_id=? AND owner=? AND state='in_progress'",
                    (identity_key, song_id, owner),
                )
        except Exception:
            raise LikeHistoryError() from None


def local_history_report(source_row, song_id, state):
    if state not in HISTORY_STATES:
        raise LikeHistoryError()
    confirmed = state == "confirmed"
    return {
        "source_row": source_row, "song_id": song_id, "checked_at_utc": _stamp(),
        "passed": confirmed, "phase": "complete" if confirmed else "history_review",
        "history_skipped": True, "history_confirmed": confirmed, "history_status": state,
        "error_code": "history_already_liked" if confirmed else "history_verification_pending",
        "verification_pending": not confirmed,
        "authenticated": False, "negative_control_passed": False,
        "server_account_identity_verified": False, "metadata_verified": False,
        "persisted_state_verified": False,
        "mutation_attempted": False, "mutation_attempts": 0, "mutation_accepted": False,
        "mutation_result": "not_attempted", "api_status": "not_attempted",
        "renewal_attempted": False, "renewal_completed": False,
        "browser_required": False, "password_required": False,
        "audio_bytes": 0, "automatic_retry": False, "elapsed_seconds": 0,
    }
