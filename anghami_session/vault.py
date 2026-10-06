"""Offline account migration and isolated, individually selected sessions."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone

from .client import AnghamiSession, validate_session
from .errors import SessionError, SessionReviewRequiredError, SessionStorageError
from .play_record import TEST_SONG_ID, _validated_test_song
from .store import _crypt, load_protected_bytes, save_protected_bytes

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VAULT_PATH = ROOT / ".anghami" / "accounts.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _email(value: str) -> str:
    value = value.strip().casefold()
    if "@" not in value or any(c in value for c in "\r\n\0"):
        raise SessionError("The account email is invalid.")
    return value


def _pack(value: dict) -> bytes:
    return _crypt(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _unpack(value: bytes) -> dict:
    try:
        result = json.loads(_crypt(value, decrypt=True).decode("utf-8"))
    except (UnicodeError, ValueError):
        raise SessionError("An encrypted account record could not be read.") from None
    if not isinstance(result, dict):
        raise SessionError("An encrypted account record has an unsupported format.")
    return result


def _email_key(key: bytes, email: str) -> str:
    return hmac.new(key, _email(email).encode("utf-8"), hashlib.sha256).hexdigest()


def _parse_fields(value: str, row: int) -> dict:
    result = {}
    for item in value.split(";"):
        if not item.strip():
            continue
        name, separator, content = item.partition("=")
        name = name.strip()
        if not separator or not name or name in result:
            raise SessionError(f"Invalid or duplicate session field on source line {row}.")
        result[name] = content
    return result


def parse_registered(raw: bytes) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise SessionError("The account list must use UTF-8 encoding.") from None
    records = []
    for row, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split("~")
        if len(fields) != 5 or not fields[0].strip() or not fields[2]:
            raise SessionError(f"Expected five account fields on source line {row}.")
        _email(fields[1])
        records.append({
            "source_row": row, "country": fields[0].strip(),
            "email": fields[1].strip(), "password": fields[2],
            "legacy_metadata": _parse_fields(fields[3], row),
            "legacy_cookies": _parse_fields(fields[4], row),
        })
    if not records:
        raise SessionError("The account list is empty.")
    return records


def migrate_registered(source: Path, destination: Path = DEFAULT_VAULT_PATH, *, progress=None) -> dict:
    """Import all rows offline, retaining duplicates and an exact encrypted backup."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise SessionError("The account vault cannot replace the source text file.")
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if destination.exists():
        with AccountVault(destination) as vault:
            if vault.metadata("source_sha256") != digest:
                raise SessionError("This vault contains a different import. Select a new --vault path to preserve it.")
            return {**vault.summary(), "already_imported": True}
    records = parse_registered(raw)
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup_relative = Path("backups") / f"registered-{digest}.dpapi"
    backup = destination.parent / backup_relative
    if backup.exists():
        if hashlib.sha256(load_protected_bytes(backup)).hexdigest() != digest:
            raise SessionError("The existing encrypted source backup does not match.")
    else:
        save_protected_bytes(raw, backup)
        if hashlib.sha256(load_protected_bytes(backup)).hexdigest() != digest:
            raise SessionError("Encrypted source backup verification failed.")
    del raw
    index_key = os.urandom(32)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".sqlite3.tmp", delete=False) as file:
        temporary = Path(file.name)
    connection = None
    try:
        connection = sqlite3.connect(temporary)
        connection.executescript("""
            PRAGMA synchronous=FULL;
            CREATE TABLE metadata (name TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE accounts (
                source_row INTEGER PRIMARY KEY,
                email_key TEXT NOT NULL,
                record BLOB NOT NULL,
                session BLOB,
                state TEXT NOT NULL DEFAULT 'login_required',
                checked_at_utc TEXT
            );
            CREATE INDEX account_email ON accounts(email_key);
        """)
        metadata = {
            "format_version": "1", "source_sha256": digest,
            "imported_at_utc": _now(), "backup_relative": backup_relative.as_posix(),
            "index_key": _crypt(index_key),
        }
        connection.executemany("INSERT INTO metadata VALUES (?, ?)", metadata.items())
        for index, record in enumerate(records, 1):
            connection.execute(
                "INSERT INTO accounts(source_row, email_key, record) VALUES (?, ?, ?)",
                (record["source_row"], _email_key(index_key, record["email"]), _pack(record)),
            )
            if progress is not None and (index % 1000 == 0 or index == len(records)):
                progress(index, len(records))
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SessionError("Account vault integrity check failed.")
        connection.close()
        connection = None
        # Do not overwrite a vault created concurrently while this one was importing.
        if destination.exists():
            raise SessionError("An account vault was created concurrently. The existing vault was preserved.")
        # On Windows os.rename fails if the destination exists, unlike os.replace.
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
        Path(str(temporary) + "-journal").unlink(missing_ok=True)
    with AccountVault(destination) as vault:
        return {**vault.summary(), "already_imported": False}


class AccountVault:
    def __init__(self, path: Path = DEFAULT_VAULT_PATH):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise SessionError("No account vault. Run python main.py accounts import first.")
        self._db = sqlite3.connect(self.path, timeout=10)
        try:
            if self.metadata("format_version") != "1":
                raise SessionError("The account vault format is not supported.")
            self._index_key = _crypt(self.metadata("index_key"), decrypt=True)
        except Exception:
            self._db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._db.close()

    def metadata(self, name: str):
        row = self._db.execute("SELECT value FROM metadata WHERE name=?", (name,)).fetchone()
        if row is None:
            raise SessionError("The account vault metadata is incomplete.")
        return row[0]

    def summary(self) -> dict:
        total, unique = self._db.execute("SELECT COUNT(*), COUNT(DISTINCT email_key) FROM accounts").fetchone()
        states = dict(self._db.execute("SELECT state, COUNT(*) FROM accounts GROUP BY state"))
        return {
            "records": total, "unique_accounts": unique, "duplicate_rows": total - unique,
            "states": states,
            "sessions_saved": self._db.execute("SELECT COUNT(*) FROM accounts WHERE session IS NOT NULL").fetchone()[0],
            "imported_at_utc": self.metadata("imported_at_utc"),
            "source_sha256": self.metadata("source_sha256"),
            "encrypted_source_backup": str(self.path.parent / self.metadata("backup_relative")),
        }

    def find(self, email: str) -> list[int]:
        return [row[0] for row in self._db.execute(
            "SELECT source_row FROM accounts WHERE email_key=? ORDER BY source_row",
            (_email_key(self._index_key, email),),
        )]

    def _test_account_table_exists(self) -> bool:
        if not hasattr(self, "_db"):
            return False
        return self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='test_accounts'"
        ).fetchone() is not None

    def enrolled_test_rows(self) -> frozenset[int]:
        """Read the managed cohort without changing the imported vault schema."""
        from .play_record import TEST_ACCOUNT_ROWS
        rows = set(TEST_ACCOUNT_ROWS)
        if self._test_account_table_exists():
            rows.update(row[0] for row in self._db.execute("SELECT source_row FROM test_accounts"))
        return frozenset(rows)

    def _test_cohort_condition(self) -> tuple[str, tuple]:
        # Bind only the fixed initial cohort; enrollment can grow without
        # exceeding SQLite's parameter limit or exposing account identities.
        from .play_record import TEST_ACCOUNT_ROWS
        defaults = tuple(sorted(TEST_ACCOUNT_ROWS))
        condition = "source_row IN (" + ",".join("?" for _ in defaults) + ")"
        if self._test_account_table_exists():
            condition += " OR source_row IN (SELECT source_row FROM test_accounts)"
        return condition, defaults

    def test_accounts(self) -> dict:
        condition, defaults = self._test_cohort_condition()
        held = set(self.session_review()["held_rows"])
        accounts = [{
            "source_row": row, "state": "session_review_pending" if row in held and state != "account_failed" else state, "session_saved": bool(has_session),
        } for row, state, has_session in self._db.execute(
            f"SELECT source_row, state, session IS NOT NULL FROM accounts WHERE {condition} ORDER BY source_row",
            defaults,
        )]
        return {
            "test_rows": [account["source_row"] for account in accounts],
            "accounts": accounts,
            "ready_rows": [
                account["source_row"] for account in accounts
                if account["state"] == "ready" and account["session_saved"]
            ],
        }

    def registered_countries(self, rows) -> dict:
        """Read imported country labels in batches, exposing no account identity."""
        selected = sorted({row for row in rows if type(row) is int and 1 <= row <= 2**31 - 1})
        result = {}
        for start in range(0, len(selected), 900):
            batch = selected[start:start + 900]
            query = "SELECT source_row, record FROM accounts WHERE source_row IN (" + ",".join("?" for _ in batch) + ")"
            for row, encrypted in self._db.execute(query, batch):
                country = _unpack(encrypted).get("country")
                normalized = country.strip().upper() if type(country) is str and len(country) <= 16 else ""
                result[row] = normalized if normalized in {"EG", "LB"} else "Other"
        return result

    def preparation_country_tags(self) -> dict:
        """Read exact immutable import labels once, without publishing identities."""
        # A verified import backup needs one DPAPI operation instead of decrypting
        # every account record. Minimal synthetic vaults can use the record path.
        try:
            backup_relative = self.metadata("backup_relative")
        except SessionError:
            backup_relative = None
        if backup_relative is not None:
            relative = Path(backup_relative)
            backup = (self.path.parent / relative).resolve()
            if relative.is_absolute() or backup.parent != (self.path.parent / "backups").resolve():
                raise SessionError("The encrypted import country labels could not be verified.")
            raw = load_protected_bytes(backup)
            if hashlib.sha256(raw).hexdigest() != self.metadata("source_sha256"):
                raise SessionError("The encrypted import country labels could not be verified.")
            try:
                lines = raw.decode("utf-8-sig").splitlines()
            except UnicodeError:
                raise SessionError("The encrypted import country labels could not be verified.") from None
            finally:
                del raw
            tags = {row: line.partition("~")[0].strip() for row, line in enumerate(lines, 1) if line.strip()}
            del lines
            return {row: tag if tag in {"EG", "LB"} else "Other" for row, tag in tags.items()}
        return {
            row: country if type(country := _unpack(packed).get("country")) is str and country in {"EG", "LB"} else "Other"
            for row, packed in self._db.execute("SELECT source_row, record FROM accounts ORDER BY source_row")
        }

    def failure_review(self) -> dict:
        """Read confirmed account rejections only; provider outages never enter here."""
        from .errors import ACCOUNT_FAILURE_CODES, REQUEST_FAILURE_STAGES
        exists = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_failure_review'").fetchone()
        if exists is None:
            return {"accounts": [], "total": 0, "failed_rows": []}
        accounts = []
        for row, code, stage, failed_at in self._db.execute(
            "SELECT r.source_row, r.failure_code, r.failed_stage, r.failed_at FROM account_failure_review r "
            "JOIN accounts a ON a.source_row=r.source_row WHERE a.state='account_failed' ORDER BY r.source_row"
        ):
            if type(row) is not int or row < 1 or code not in ACCOUNT_FAILURE_CODES or stage not in REQUEST_FAILURE_STAGES:
                continue
            try:
                parsed = datetime.fromisoformat(failed_at)
                if parsed.tzinfo is None:
                    continue
            except (TypeError, ValueError):
                continue
            accounts.append({"source_row": row, "failure_code": code, "failed_stage": stage, "failed_at": parsed.isoformat()})
        return {"accounts": accounts, "total": len(accounts), "failed_rows": [r["source_row"] for r in accounts]}

    def record_account_failure(self, row: int, failure) -> dict:
        """Atomically quarantine all source aliases of a confirmed rejected account."""
        from .errors import safe_request_failure
        safe = safe_request_failure(failure)
        if type(row) is not int or row < 1 or safe.get("failure_category") != "account":
            raise SessionError("Only a confirmed account rejection can enter account review.")
        identity = self._db.execute("SELECT email_key FROM accounts WHERE source_row=?", (row,)).fetchone()
        if identity is None:
            raise SessionError("No account exists at that source row.")
        stamp = _now()
        with self._db:
            self._db.execute("SAVEPOINT record_account_failure")
            try:
                self._db.execute("CREATE TABLE IF NOT EXISTS account_failure_review (source_row INTEGER PRIMARY KEY REFERENCES accounts(source_row), failure_code TEXT NOT NULL, failed_stage TEXT NOT NULL, failed_at TEXT NOT NULL)")
                self._db.execute(
                    "INSERT OR REPLACE INTO account_failure_review(source_row, failure_code, failed_stage, failed_at) "
                    "SELECT source_row, ?, ?, ? FROM accounts WHERE email_key=?",
                    (safe["code"], safe["stage"], stamp, identity[0]),
                )
                self._db.execute("UPDATE accounts SET state='account_failed', checked_at_utc=? WHERE email_key=?", (stamp, identity[0]))
                self._db.execute("RELEASE SAVEPOINT record_account_failure")
            except Exception:
                self._db.execute("ROLLBACK TO SAVEPOINT record_account_failure")
                self._db.execute("RELEASE SAVEPOINT record_account_failure")
                raise
        return {"source_row": row, **safe}

    def _clear_account_failure(self, row):
        exists = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_failure_review'").fetchone()
        if exists is not None:
            self._db.execute("DELETE FROM account_failure_review WHERE source_row=?", (row,))

    def _session_review_table_exists(self) -> bool:
        return hasattr(self, "_db") and self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_session_review'",
        ).fetchone() is not None

    def _preparation_review_table_exists(self) -> bool:
        return hasattr(self, "_db") and self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_preparation_review'",
        ).fetchone() is not None

    def _require_session_not_held(self, row: int) -> None:
        if not hasattr(self, "_db"):
            return
        account = self._db.execute("SELECT email_key,state FROM accounts WHERE source_row=?", (row,)).fetchone()
        if account is None:
            return
        held = account[1] == "session_review_pending" or self._db.execute(
            "SELECT 1 FROM accounts WHERE email_key=? AND state='session_review_pending'", (account[0],),
        ).fetchone() is not None
        if not held and self._session_review_table_exists():
            held = self._db.execute(
                "SELECT 1 FROM account_session_review WHERE source_row=? OR email_key=?", (row, account[0]),
            ).fetchone() is not None
        if not held and self._preparation_review_table_exists():
            held = self._db.execute(
                "SELECT 1 FROM account_preparation_review WHERE source_row=? OR email_key=?", (row, account[0]),
            ).fetchone() is not None
        if held:
            raise SessionReviewRequiredError()

    def _blocked_session_review_rows(self) -> list[int]:
        identities = "SELECT email_key FROM accounts WHERE state='session_review_pending'"
        extra = ""
        if self._session_review_table_exists():
            identities += " UNION SELECT email_key FROM account_session_review"
            extra = " OR source_row IN (SELECT source_row FROM account_session_review)"
        if self._preparation_review_table_exists():
            identities += " UNION SELECT email_key FROM account_preparation_review"
            extra += " OR source_row IN (SELECT source_row FROM account_preparation_review)"
        return [row[0] for row in self._db.execute(
            "SELECT source_row FROM accounts WHERE email_key IN (" + identities + ")" + extra + " ORDER BY source_row",
        )]

    def record_preparation_session_review(self, row: int, *, job_id, source_sha256, identity_binding,
                                          failure_code="preparation_outcome_unknown", stage="validation",
                                          candidate_retained=False) -> dict:
        """Durably isolate an exact uncertain preparation identity, without replay."""
        if (type(row) is not int or row < 1 or type(job_id) is not str
                or re.fullmatch(r"[0-9a-f]{32}", job_id) is None
                or type(source_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
                or type(identity_binding) is not str or re.fullmatch(r"[0-9a-f]{64}", identity_binding) is None
                or failure_code not in {"preparation_outcome_unknown", "preparation_validation_unknown"}
                or stage not in {"validation", "session_recovery_renewal"}
                or type(candidate_retained) is not bool):
            raise SessionError("Use an exact frozen preparation review binding.")
        if self.metadata("source_sha256") != source_sha256:
            raise SessionError("The preparation review import binding changed.")
        stamp, held_rows = _now(), []
        def hold():
            held_rows.clear()
            selected = self._db.execute("SELECT email_key FROM accounts WHERE source_row=?", (row,)).fetchone()
            if selected is None or hashlib.sha256(str(selected[0]).encode()).hexdigest() != identity_binding:
                raise SessionError("The frozen account identity changed.")
            if self.metadata("source_sha256") != source_sha256:
                raise SessionError("The preparation review import binding changed.")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS account_preparation_review (
                    source_row INTEGER PRIMARY KEY REFERENCES accounts(source_row), email_key TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL, failure_code TEXT NOT NULL, failed_stage TEXT NOT NULL,
                    held_at TEXT NOT NULL, job_id TEXT NOT NULL, candidate_retained INTEGER NOT NULL,
                    record_sha256 TEXT NOT NULL, session_sha256 TEXT, pending_sha256 TEXT, previous_state TEXT NOT NULL
                )
            """)
            self._db.execute("CREATE INDEX IF NOT EXISTS account_preparation_review_identity ON account_preparation_review(email_key)")
            pending_exists = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_pending_sessions'").fetchone() is not None
            aliases = self._db.execute("SELECT source_row,record,session,state FROM accounts WHERE email_key=? ORDER BY source_row", (selected[0],)).fetchall()
            for alias, record_blob, session_blob, state in aliases:
                record = _unpack(record_blob)
                if _email_key(self._index_key, record["email"]) != selected[0]:
                    raise SessionError("The frozen account identity changed.")
                if state == "account_failed":
                    if alias == row:
                        raise SessionError("A confirmed rejected account cannot enter preparation review.")
                    continue
                pending = self._db.execute("SELECT session FROM account_pending_sessions WHERE source_row=?", (alias,)).fetchone() if pending_exists else None
                pending_blob = pending[0] if pending is not None else None
                if pending_blob is not None:
                    self._require_identity(alias, validate_session(_unpack(pending_blob)))
                if alias == row and candidate_retained and pending_blob is None:
                    raise SessionError("The retained preparation candidate could not be confirmed.")
                previous = self._db.execute("SELECT previous_state FROM account_preparation_review WHERE source_row=?", (alias,)).fetchone()
                self._db.execute(
                    "INSERT OR REPLACE INTO account_preparation_review VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (alias, selected[0], source_sha256, failure_code, stage, stamp, job_id,
                     int(candidate_retained and pending_blob is not None), hashlib.sha256(record_blob).hexdigest(),
                     hashlib.sha256(session_blob).hexdigest() if session_blob is not None else None,
                     hashlib.sha256(pending_blob).hexdigest() if pending_blob is not None else None,
                     previous[0] if previous is not None else state),
                )
                self._db.execute("UPDATE accounts SET state='session_review_pending' WHERE source_row=?", (alias,))
                held_rows.append(alias)
        try:
            self._store_session_transaction("pending_save", "record_preparation_review", hold)
            proof = self.preparation_session_review(row)
            if proof is None or proof["held_rows"] != held_rows:
                raise SessionError("The preparation review hold could not be confirmed.")
            return proof
        except SessionError:
            raise
        except Exception:
            raise SessionError("The preparation review hold could not be saved securely.") from None

    def preparation_session_review(self, row: int) -> dict | None:
        """Verify durable source, identity and ciphertext bindings; expose fixed facts."""
        if type(row) is not int or row < 1:
            raise SessionError("The source row must be a positive integer.")
        if not self._preparation_review_table_exists():
            return None
        try:
            selected = self._db.execute("SELECT email_key FROM account_preparation_review WHERE source_row=?", (row,)).fetchone()
            if selected is None:
                return None
            held_rows, safe = [], None
            for item in self._db.execute(
                "SELECT r.source_row,r.email_key,r.source_sha256,r.failure_code,r.failed_stage,r.held_at,r.job_id,"
                "r.candidate_retained,r.record_sha256,r.session_sha256,r.pending_sha256,a.email_key,a.record,a.session,a.state "
                "FROM account_preparation_review r JOIN accounts a ON a.source_row=r.source_row WHERE r.email_key=? ORDER BY r.source_row", (selected[0],),
            ):
                alias, identity, source, code, stage, stamp, job, retained, record_hash, session_hash, pending_hash, account_identity, record_blob, session_blob, state = item
                if (identity != account_identity or identity != selected[0] or source != self.metadata("source_sha256")
                        or code not in {"preparation_outcome_unknown", "preparation_validation_unknown"}
                        or stage not in {"validation", "session_recovery_renewal"}
                        or type(job) is not str or re.fullmatch(r"[0-9a-f]{32}", job) is None
                        or retained not in (0, 1) or datetime.fromisoformat(stamp).tzinfo is None
                        or state != "session_review_pending" or hashlib.sha256(record_blob).hexdigest() != record_hash
                        or (hashlib.sha256(session_blob).hexdigest() if session_blob is not None else None) != session_hash
                        or _email_key(self._index_key, _unpack(record_blob)["email"]) != identity):
                    raise ValueError
                pending_table = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_pending_sessions'").fetchone()
                pending = self._db.execute("SELECT session FROM account_pending_sessions WHERE source_row=?", (alias,)).fetchone() if pending_table else None
                pending_blob = pending[0] if pending is not None else None
                if (hashlib.sha256(pending_blob).hexdigest() if pending_blob is not None else None) != pending_hash or retained and pending_blob is None:
                    raise ValueError
                if pending_blob is not None:
                    self._require_identity(alias, validate_session(_unpack(pending_blob)))
                held_rows.append(alias)
                if alias == row:
                    safe = {"code": code, "stage": stage, "candidate_retained": bool(retained)}
            if safe is None:
                raise ValueError
            return {"source_row": row, "session_review_pending": True, "held_rows": held_rows, "preparation_review": safe}
        except Exception:
            raise SessionError("The preparation review hold could not be verified securely.") from None

    def record_session_review(self, row: int, failure, job_id: str) -> dict:
        """Hold a renewal with an unknown response without rejecting the account."""
        from .errors import safe_request_failure
        safe = safe_request_failure(failure)
        if (type(row) is not int or row < 1 or type(job_id) is not str
                or re.fullmatch(r"[0-9a-f]{32}", job_id) is None
                or safe.get("failure_category") != "provider"
                or safe.get("stage") not in {"identity", "session_recovery_renewal"}):
            raise SessionError("Only a typed uncertain session renewal can enter session review.")
        diagnostics = {key: safe[key] for key in ("code", "stage", "http_status", "curl_code") if key in safe}
        stamp = _now()
        try:
            with self._db:
                self._db.execute("SAVEPOINT record_session_review")
                try:
                    selected = self._db.execute(
                        "SELECT email_key,record,session,state FROM accounts WHERE source_row=?", (row,),
                    ).fetchone()
                    if selected is None or selected[2] is None or selected[3] == "account_failed":
                        raise ValueError
                    record = _unpack(selected[1])
                    if _email_key(self._index_key, record["email"]) != selected[0]:
                        raise ValueError
                    self._require_identity(row, validate_session(_unpack(selected[2])))
                    self._db.execute("""
                        CREATE TABLE IF NOT EXISTS account_session_review (
                            source_row INTEGER PRIMARY KEY REFERENCES accounts(source_row),
                            email_key TEXT NOT NULL, failure_code TEXT NOT NULL,
                            failed_stage TEXT NOT NULL, held_at TEXT NOT NULL, job_id TEXT NOT NULL,
                            http_status INTEGER, curl_code INTEGER,
                            record_sha256 TEXT NOT NULL, session_sha256 TEXT,
                            previous_state TEXT NOT NULL
                        )
                    """)
                    self._db.execute("CREATE INDEX IF NOT EXISTS account_session_review_identity ON account_session_review(email_key)")
                    aliases = self._db.execute(
                        "SELECT source_row,record,session,state FROM accounts WHERE email_key=? ORDER BY source_row",
                        (selected[0],),
                    ).fetchall()
                    held_rows = []
                    for alias, packed_record, packed_session, state in aliases:
                        if _email_key(self._index_key, _unpack(packed_record)["email"]) != selected[0]:
                            raise ValueError
                        # A confirmed rejection keeps its stronger, separate review.
                        if state == "account_failed":
                            continue
                        previous = self._db.execute(
                            "SELECT previous_state FROM account_session_review WHERE source_row=?", (alias,),
                        ).fetchone()
                        self._db.execute(
                            "INSERT OR REPLACE INTO account_session_review "
                            "(source_row,email_key,failure_code,failed_stage,held_at,job_id,http_status,curl_code,record_sha256,session_sha256,previous_state) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (alias, selected[0], diagnostics["code"], diagnostics["stage"], stamp, job_id,
                             diagnostics.get("http_status"), diagnostics.get("curl_code"),
                             hashlib.sha256(packed_record).hexdigest(),
                             hashlib.sha256(packed_session).hexdigest() if packed_session is not None else None,
                             previous[0] if previous is not None else state),
                        )
                        self._db.execute("UPDATE accounts SET state='session_review_pending' WHERE source_row=?", (alias,))
                        held_rows.append(alias)
                    self._db.execute("RELEASE SAVEPOINT record_session_review")
                except Exception:
                    self._db.execute("ROLLBACK TO SAVEPOINT record_session_review")
                    self._db.execute("RELEASE SAVEPOINT record_session_review")
                    raise
        except Exception:
            raise SessionError("The session review hold could not be saved securely.") from None
        return {"source_row": row, "session_review_pending": True, "held_rows": held_rows,
                "session_failure": diagnostics}

    def session_review(self) -> dict:
        """Return only row numbers and typed diagnostics from renewal holds."""
        from .errors import safe_request_failure
        preparation_accounts = []
        if self._preparation_review_table_exists():
            for row, stamp, job in self._db.execute(
                "SELECT source_row,held_at,job_id FROM account_preparation_review ORDER BY source_row",
            ).fetchall():
                proof = self.preparation_session_review(row)
                facts = proof["preparation_review"]
                preparation_accounts.append({"source_row": row, "state": "session_review_pending",
                                             "failure_code": facts["code"], "failed_stage": facts["stage"],
                                             "held_at": stamp, "job_id": job, "preparation_review": facts})
        if not self._session_review_table_exists():
            return {"accounts": preparation_accounts, "total": len(preparation_accounts),
                    "held_rows": self._blocked_session_review_rows(),
                    "session_review_rows": [account["source_row"] for account in preparation_accounts]}
        try:
            accounts = []
            for row, code, stage, held_at, job_id, http, curl in self._db.execute(
                "SELECT r.source_row,r.failure_code,r.failed_stage,r.held_at,r.job_id,r.http_status,r.curl_code "
                "FROM account_session_review r JOIN accounts a ON a.source_row=r.source_row ORDER BY r.source_row",
            ):
                safe = safe_request_failure({"code": code, "stage": stage, "http_status": http, "curl_code": curl})
                stamp = datetime.fromisoformat(held_at)
                if (type(row) is not int or row < 1 or stamp.tzinfo is None
                        or type(job_id) is not str or re.fullmatch(r"[0-9a-f]{32}", job_id) is None
                        or safe.get("failure_category") != "provider"
                        or stage not in {"identity", "session_recovery_renewal"}):
                    raise ValueError
                diagnostics = {key: safe[key] for key in ("code", "stage", "failure_category", "http_status", "curl_code") if key in safe}
                accounts.append({"source_row": row, "state": "session_review_pending", "failure_code": code,
                                 "failed_stage": stage, "held_at": stamp.isoformat(), "job_id": job_id,
                                 "session_failure": diagnostics})
        except Exception:
            raise SessionError("The session review holds could not be read securely.") from None
        # A preparation hold is distinct from a typed uncertain network write,
        # while both remain excluded from ordinary use and fresh selections.
        existing_rows = {account["source_row"] for account in accounts}
        accounts.extend(account for account in preparation_accounts if account["source_row"] not in existing_rows)
        accounts.sort(key=lambda account: account["source_row"])
        rows = [account["source_row"] for account in accounts]
        return {"accounts": accounts, "total": len(accounts), "held_rows": self._blocked_session_review_rows(), "session_review_rows": rows}

    def _session_review_snapshot(self, row: int, *, require_saved_binding=True):
        """Capture immutable ciphertext and hold bindings for an explicit review."""
        if type(row) is not int or row < 1:
            raise SessionError("The source row must be a positive integer.")
        if not self._session_review_table_exists():
            raise SessionError("This account has no saved-session review hold.")
        selected = self._db.execute(
            "SELECT email_key,record,session,state FROM accounts WHERE source_row=?", (row,),
        ).fetchone()
        if selected is None or selected[2] is None or selected[3] == "account_failed":
            raise SessionError("This account has no saved session available for review.")
        aliases = self._db.execute(
            "SELECT a.source_row,a.email_key,a.record,a.session,a.state,r.email_key,r.failure_code,r.failed_stage,"
            "r.held_at,r.job_id,r.http_status,r.curl_code,r.record_sha256,r.session_sha256,r.previous_state "
            "FROM accounts a JOIN account_session_review r ON r.source_row=a.source_row "
            "WHERE a.email_key=? ORDER BY a.source_row", (selected[0],),
        ).fetchall()
        bound = next((alias for alias in aliases if alias[0] == row), None)
        if bound is None:
            raise SessionError("This account has no saved-session review hold.")
        if (bound[1] != bound[5] or (require_saved_binding and (
                bound[12] != hashlib.sha256(bound[2]).hexdigest()
                or bound[13] != hashlib.sha256(bound[3]).hexdigest()))):
            raise SessionError("The saved session changed after its review hold. Capture and verify a new login.")
        return selected, aliases

    def _clear_verified_session_review(self, row, selected, aliases, checked_at, *, replacement_session=None,
                                       replacement_record=None):
        """Clear only proved, unchanged bindings within the committing transaction."""
        current = self._db.execute(
            "SELECT email_key,record,session,state FROM accounts WHERE source_row=?", (row,),
        ).fetchone()
        if current != selected:
            raise SessionError("The account changed during session review; its hold was kept.")
        cleared = []
        for alias in aliases:
            alias_row = alias[0]
            current_alias = self._db.execute(
                "SELECT a.source_row,a.email_key,a.record,a.session,a.state,r.email_key,r.failure_code,r.failed_stage,"
                "r.held_at,r.job_id,r.http_status,r.curl_code,r.record_sha256,r.session_sha256,r.previous_state "
                "FROM accounts a JOIN account_session_review r ON r.source_row=a.source_row WHERE a.source_row=?",
                (alias_row,),
            ).fetchone()
            if alias_row == row and current_alias != alias:
                raise SessionError("The account hold changed during session review; its hold was kept.")
            new_capture = alias_row == row and replacement_session is not None
            if (current_alias != alias or alias[1] != alias[5] or alias[3] != selected[2]
                    or alias[4] == "account_failed" or alias[14] == "account_failed"
                    or (not new_capture and (alias[12] != hashlib.sha256(alias[2]).hexdigest()
                    or alias[13] != hashlib.sha256(alias[3]).hexdigest()))):
                continue
            # A replacement proves only its selected row, unless its ciphertext
            # is exactly the old validated session shared by that alias.
            if replacement_session is not None and alias_row != row and replacement_session != selected[2]:
                continue
            self._db.execute("DELETE FROM account_session_review WHERE source_row=?", (alias_row,))
            self._db.execute(
                "UPDATE accounts SET session=COALESCE(?,session),record=COALESCE(?,record),state='ready',checked_at_utc=? WHERE source_row=?",
                (replacement_session if alias_row == row else None, replacement_record if alias_row == row else None,
                 checked_at, alias_row),
            )
            cleared.append(alias_row)
        if row not in cleared:
            raise SessionError("The session review binding could not be confirmed; its hold was kept.")
        return cleared

    def review_saved_session(self, row: int, *, proxy=None) -> dict:
        """Explicit GET-only proof; never renew a session or send an engagement."""
        from .session_recovery import _profile_identity
        try:
            selected, aliases = self._session_review_snapshot(row)
            saved = validate_session(_unpack(selected[2]))
            self._require_identity(row, saved)
            if _email_key(self._index_key, saved["account_email"]) != selected[0]:
                raise SessionError("The selected account identity could not be confirmed for review.")
        except SessionError:
            raise
        except Exception:
            raise SessionError("The held saved session could not be unlocked or validated.") from None
        options = {} if proxy is None else {"proxy": proxy}
        proxy_check = proxy.verify_country() if proxy is not None else None
        with AnghamiSession(saved=saved, **options) as session:
            if proxy is not None:
                session._proxy_check = proxy_check
            report = session.check(negative_control=True)
            if (report.get("authenticated") is not True
                    or report.get("without_session", {}).get("authentication_rejected") is not True):
                raise SessionError("The saved-session authentication proofs could not be confirmed.")
            _profile_identity(session, saved["account_email"])
        try:
            with self._db:
                self._db.execute("SAVEPOINT clear_session_review")
                try:
                    cleared = self._clear_verified_session_review(row, selected, aliases, report["checked_at_utc"])
                    self._db.execute("RELEASE SAVEPOINT clear_session_review")
                except Exception:
                    self._db.execute("ROLLBACK TO SAVEPOINT clear_session_review")
                    self._db.execute("RELEASE SAVEPOINT clear_session_review")
                    raise
        except Exception:
            raise SessionError("The verified session review could not be saved securely; its hold was kept.") from None
        return {"source_row": row, "passed": True, "authenticated": True, "negative_control_passed": True,
                "server_account_identity_verified": True, "session_review_cleared": True,
                "cleared_rows": cleared, "checked_at_utc": report["checked_at_utc"]}

    def _candidate_excluded_identities(self, *, include_failure_review=False):
        """Share mutable eligibility rules between selection and its UI count."""
        condition, defaults = self._test_cohort_condition()
        identities = {row[0] for row in self._db.execute(
            f"SELECT DISTINCT email_key FROM accounts WHERE {condition}", defaults,
        )}
        identities.update(row[0] for row in self._db.execute(
            "SELECT DISTINCT email_key FROM accounts WHERE state='session_review_pending'",
        ))
        if self._session_review_table_exists():
            identities.update(row[0] for row in self._db.execute("SELECT DISTINCT email_key FROM account_session_review"))
        if self._preparation_review_table_exists():
            identities.update(row[0] for row in self._db.execute("SELECT DISTINCT email_key FROM account_preparation_review"))
        if include_failure_review:
            # A rejected identity remains excluded even when another imported
            # alias has an older state or belongs to a different country tag.
            identities.update(row[0] for row in self._db.execute(
                "SELECT DISTINCT email_key FROM accounts WHERE state='account_failed'",
            ))
            if self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_failure_review'",
            ).fetchone() is not None:
                identities.update(row[0] for row in self._db.execute(
                    "SELECT DISTINCT a.email_key FROM accounts a "
                    "JOIN account_failure_review r ON r.source_row=a.source_row",
                ))
        return identities

    def _candidate_rows(self, *, start_row=1, country=None, randomize=False, country_tags=None):
        identities = self._candidate_excluded_identities(include_failure_review=country is not None or randomize)
        columns = "source_row, email_key" if country is None or country_tags is not None else "source_row, email_key, record"
        for entry in self._db.execute(
            f"SELECT {columns} FROM accounts WHERE source_row>=? AND state!='account_failed' ORDER BY source_row",
            (start_row,),
        ):
            row, identity = entry[:2]
            if identity in identities:
                continue
            if country is not None:
                tag = country_tags.get(row) if country_tags is not None else _unpack(entry[2]).get("country")
                if tag != country:
                    continue
            identities.add(identity)
            yield row

    def preparation_availability(self, country_tags, *, start_row=1) -> dict:
        """Count exactly the current selector pools using cached import labels."""
        if type(start_row) is not int or not 1 <= start_row <= 2**53 - 1:
            raise SessionError("The starting source row must be a positive integer.")
        if type(country_tags) is not dict or any(
                type(row) is not int or not 1 <= row <= 2**31 - 1 or type(tag) is not str or tag not in {"EG", "LB", "Other"}
                for row, tag in country_tags.items()):
            raise SessionError("The imported country labels are not available.")
        if any(row not in country_tags for row, in self._db.execute("SELECT source_row FROM accounts")):
            raise SessionError("The imported country labels are not available.")
        return {
            "available": True, "start_row": start_row,
            "counts": {
                "EG": sum(1 for _ in self._candidate_rows(country="EG", randomize=True, country_tags=country_tags)),
                "LB": sum(1 for _ in self._candidate_rows(country="LB", randomize=True, country_tags=country_tags)),
                "all": sum(1 for _ in self._candidate_rows(start_row=start_row)),
            },
        }

    def select_test_candidates(self, count: int, *, start_row: int = 1, country=None, randomize=False) -> list[int]:
        if type(count) is not int or count < 1:
            raise SessionError("The preparation count must be a positive integer.")
        if isinstance(start_row, bool) or not isinstance(start_row, int) or start_row < 1:
            raise SessionError("The starting source row must be a positive integer.")
        if country is not None and (type(country) is not str or country not in {"EG", "LB"}):
            raise SessionError("Select the registered account country: EG or LB.")
        if type(randomize) is not bool:
            raise SessionError("The random account selection must be true or false.")
        if country is not None and start_row != 1:
            raise SessionError("Country selection cannot be combined with a starting source row.")
        selected = []
        for row in self._candidate_rows(start_row=start_row, country=country, randomize=randomize):
            selected.append(row)
            if not randomize and len(selected) == count:
                break
        if randomize:
            return random.sample(selected, min(count, len(selected)))
        return selected

    def enable_test_account(self, row: int) -> dict:
        if isinstance(row, bool) or not isinstance(row, int) or row < 1:
            raise SessionError("The source row must be a positive integer.")
        already_enabled = row in self.enrolled_test_rows()
        def enroll():
            state = self._db.execute(
                "SELECT state, session IS NOT NULL FROM accounts WHERE source_row=?", (row,),
            ).fetchone()
            if state is None:
                raise SessionError("No account exists at that source row.")
            self._require_session_not_held(row)
            if state[0] != "ready" or not state[1]:
                raise SessionError("This account needs a verified ready saved session before test enrollment.")
            self.session(row)  # Local format/identity checks; no HTTP is repeated.
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS test_accounts (
                    source_row INTEGER PRIMARY KEY REFERENCES accounts(source_row),
                    added_at_utc TEXT NOT NULL
                )
            """)
            self._db.execute(
                "INSERT OR IGNORE INTO test_accounts(source_row, added_at_utc) VALUES (?, ?)", (row, _now()),
            )
        self._store_session_transaction("verified_save", "enroll_test_account", enroll)
        return {
            "source_row": row, "enabled": True, "already_enabled": already_enabled,
            "state": "ready", "session_saved": True,
        }

    def record(self, row: int) -> dict:
        result = self._db.execute("SELECT record FROM accounts WHERE source_row=?", (row,)).fetchone()
        if result is None:
            raise SessionError("No account exists at that source row.")
        return _unpack(result[0])

    def session(self, row: int) -> dict:
        self._require_session_not_held(row)
        result = self._db.execute("SELECT session FROM accounts WHERE source_row=?", (row,)).fetchone()
        if result is None:
            raise SessionError("No account exists at that source row.")
        if result[0] is None:
            raise SessionError("This row needs a normal login for the new session workflow. Use accounts login with the same row.")
        saved = validate_session(_unpack(result[0]))
        self._require_identity(row, saved)
        return saved

    def _require_identity(self, row: int, saved: dict) -> None:
        if saved.get("account_email") != _email(self.record(row)["email"]):
            raise SessionError("The captured session does not identify this account. Sign in to the selected account.")

    def save_pending_session(self, row: int, saved: dict) -> dict:
        """Protect a known issued candidate separately from ready saved sessions."""
        if type(row) is not int or row < 1:
            raise SessionError("The source row must be a positive integer.")
        self._require_session_not_held(row)
        candidate = validate_session(saved)
        self._require_identity(row, candidate)
        try:
            encrypted = _pack(candidate)
        except Exception:
            raise SessionStorageError(operation="pending_save", phase="encryption") from None
        def save():
            self._require_session_not_held(row)
            self._require_identity(row, candidate)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS account_pending_sessions (
                    source_row INTEGER PRIMARY KEY REFERENCES accounts(source_row),
                    session BLOB NOT NULL
                )
            """)
            self._db.execute(
                "INSERT OR REPLACE INTO account_pending_sessions(source_row, session) VALUES (?, ?)",
                (row, encrypted),
            )
        self._store_session_transaction("pending_save", "save_pending_session", save)
        return {"source_row": row, "session_pending": True}

    def _store_session_transaction(self, operation, savepoint, write):
        """Retry only an unchanged local write after its whole savepoint rolls back."""
        for attempt in range(1, 4):
            try:
                with self._db:
                    if not self._db.in_transaction:
                        # Acquire local write intent before guard SELECTs. This
                        # lets SQLite's busy timeout serialize workers instead
                        # of failing an immediate read-to-write lock upgrade.
                        # All remote GET checks have already finished.
                        self._db.execute("BEGIN IMMEDIATE")
                    self._db.execute("SAVEPOINT " + savepoint)
                    try:
                        write()
                        self._db.execute("RELEASE SAVEPOINT " + savepoint)
                    except BaseException:
                        self._db.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                        self._db.execute("RELEASE SAVEPOINT " + savepoint)
                        raise
                return
            except (SessionReviewRequiredError, SessionError):
                # Identity and peer review guards are never storage failures.
                raise
            except Exception as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                code = code & 255 if type(code) is int and 1 <= code <= 65535 else None
                if isinstance(exc, sqlite3.OperationalError) and code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} and attempt < 3:
                    time.sleep(0.05 * attempt)
                    continue
                raise SessionStorageError(operation=operation, phase="transaction", sqlite_code=code,
                                          attempts=attempt) from None

    def pending_session(self, row: int) -> dict | None:
        """Read only a protected candidate; this never declares an account ready."""
        if type(row) is not int or row < 1:
            raise SessionError("The source row must be a positive integer.")
        self._require_session_not_held(row)
        try:
            if self._db.execute("SELECT 1 FROM accounts WHERE source_row=?", (row,)).fetchone() is None:
                raise ValueError
            exists = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_pending_sessions'",
            ).fetchone()
            if exists is None:
                return None
            result = self._db.execute(
                "SELECT session FROM account_pending_sessions WHERE source_row=?", (row,),
            ).fetchone()
            if result is None:
                return None
            saved = validate_session(_unpack(result[0]))
            self._require_identity(row, saved)
            return saved
        except Exception:
            raise SessionError("The pending session could not be unlocked or validated.") from None

    def _clear_pending_session(self, row: int) -> None:
        exists = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_pending_sessions'",
        ).fetchone()
        if exists is not None:
            self._db.execute("DELETE FROM account_pending_sessions WHERE source_row=?", (row,))

    def attach(self, row: int, saved: dict, *, new_password: str | None = None, proxy=None,
               review_session: bool = False) -> dict:
        if type(review_session) is not bool:
            raise SessionError("Explicit session review must be true or false.")
        review_snapshot = None
        if self._session_review_table_exists() and self._db.execute(
            "SELECT 1 FROM account_session_review WHERE source_row=?", (row,),
        ).fetchone() is not None:
            if not review_session:
                self._require_session_not_held(row)
            review_snapshot = self._session_review_snapshot(row, require_saved_binding=False)
        else:
            self._require_session_not_held(row)
        saved = validate_session(saved)
        self._require_identity(row, saved)
        if review_snapshot is not None and _email_key(self._index_key, saved["account_email"]) != review_snapshot[0][0]:
            raise SessionError("The selected account identity could not be confirmed for review.")
        replacement_record = None
        if new_password is not None:
            if not new_password:
                raise SessionError("The replacement password cannot be empty.")
            replacement_record = self.record(row)
            replacement_record["password"] = new_password
        options = {}
        proxy_check = None
        if proxy is not None:
            proxy_check = proxy.verify_country()
            options["proxy"] = proxy
        with AnghamiSession(saved=saved, **options) as session:
            if proxy is not None:
                session._proxy_check = proxy_check
            report = session.check(negative_control=True)
            if review_snapshot is not None:
                from .session_recovery import _profile_identity
                if (report.get("authenticated") is not True
                        or report.get("without_session", {}).get("authentication_rejected") is not True):
                    raise SessionError("The saved-session authentication proofs could not be confirmed.")
                _profile_identity(session, saved["account_email"])
        try:
            encrypted = _pack(saved)
            encrypted_record = _pack(replacement_record) if replacement_record is not None else None
        except Exception:
            raise SessionStorageError(operation="verified_save", phase="encryption") from None
        def save():
            if review_snapshot is not None:
                cleared = self._clear_verified_session_review(
                    row, *review_snapshot, report["checked_at_utc"], replacement_session=encrypted,
                    replacement_record=encrypted_record,
                )
                report.update(session_review_cleared=True, cleared_rows=cleared,
                              server_account_identity_verified=True)
            else:
                # A peer hold arriving while GET checks ran must remain a hold.
                self._require_session_not_held(row)
                self._require_identity(row, saved)
                self._db.execute(
                    "UPDATE accounts SET session=?, record=COALESCE(?, record), state='ready', checked_at_utc=? WHERE source_row=?",
                    (encrypted, encrypted_record, report["checked_at_utc"], row),
                )
                self._clear_account_failure(row)
            self._clear_pending_session(row)
        self._store_session_transaction("verified_save", "attach_verified_session", save)
        return {"source_row": row, **report}

    def _http_session(self, row: int, proxy=None, *, report_path=None, song_id=None):
        saved = self.session(row)
        if proxy is None:
            return AnghamiSession(saved=saved)
        from .play_record import _journal
        preflight = {
            "source_row": row, "song_id": str(song_id) if song_id is not None else None,
            "passed": False, "phase": "proxy_preflight", "proxy": proxy.summary(),
            "event_attempted": False, "event_attempts": 0,
            "mutation_attempted": False, "mutation_attempts": 0,
            "browser_required": False, "audio_bytes": 0, "automatic_retry": False,
        }
        _journal(preflight, report_path)
        try:
            checked = proxy.verify_country()
        except SessionError:
            preflight.update({"phase": "failed", "failed_phase": "proxy_preflight", "error_code": "proxy_preflight_failed"})
            _journal(preflight, report_path)
            raise
        session = AnghamiSession(saved=saved, proxy=proxy)
        session._proxy_check = checked
        return session

    def request(self, row: int, operation: str, *, proxy=None) -> dict:
        with self._http_session(row, proxy) as session:
            result = session.request(operation)
            if proxy is not None:
                result["proxy"] = session.proxy_summary
            return result

    def song(self, row: int, song_id: str | int) -> dict:
        with AnghamiSession(saved=self.session(row)) as session:
            return session.song(song_id)

    def probe_playback(self, row: int, song_id: str | int, *, seconds: float = 5) -> dict:
        from .playback import probe_playback
        with AnghamiSession(saved=self.session(row)) as session:
            return {"source_row": row, **probe_playback(session, song_id, seconds=seconds)}

    def test_play_record(self, row: int, song_id: str | int, *, proxy=None, declared_song_id=TEST_SONG_ID) -> dict:
        """One synthetic record on the declared test track and selected test cohort."""
        from .play_record import run_play_record_test
        song_id = _validated_test_song(song_id, declared_song_id)
        cohort = self.enrolled_test_rows()
        if row not in cohort:
            allowed = ", ".join(map(str, sorted(cohort)))
            raise SessionError(f"This test command is limited to the selected test accounts (source rows {allowed}).")
        report_path = self.path.parent / f"account-{row}.test-play-record-report.json"
        with self._http_session(row, proxy, report_path=report_path, song_id=song_id) as session:
            options = {} if song_id == TEST_SONG_ID else {"declared_song_id": song_id}
            report = run_play_record_test(session, song_id, report_path=report_path, **options)
        result = {"source_row": row, **report}
        report_path.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        return result

    def test_like(self, row: int, song_id: str | int, *, proxy=None, declared_song_id=TEST_SONG_ID) -> dict:
        """Append once per identity/song, retaining confirmed and uncertain facts."""
        song_id = _validated_test_song(song_id, declared_song_id)
        cohort = self.enrolled_test_rows()
        if row not in cohort:
            allowed = ", ".join(map(str, sorted(cohort)))
            raise SessionError(f"This test command is limited to the selected test accounts (source rows {allowed}).")
        from .like_test import run_like_test
        from .like_history import LikeHistoryError, LikeHistoryLedger, classify_like_report, local_history_report
        from .play_record import _journal
        report_path = self.path.parent / f"account-{row}.test-like-report.json"
        binding = self.like_history_binding(row)
        ledger = LikeHistoryLedger(self._db)
        owner, state = ledger.reserve(binding["identity_key"], song_id, row, binding["source_sha256"])
        if owner is None:
            # A peer may still be using this row's raw journal. A local skip
            # must not overwrite its durable unknown-write intent or result.
            return local_history_report(row, song_id, state)
        intent_seen, protected_fact_seen, latest_classification = False, False, None

        def record_history(report):
            nonlocal intent_seen, protected_fact_seen, latest_classification
            if type(report) is not dict or ("source_row" in report and (type(report["source_row"]) is not int or report["source_row"] != row)):
                raise LikeHistoryError()
            if report.get("mutation_attempted") is True:
                # Mark locally before saving: a ledger-storage failure at intent
                # must retain the reservation and block a subsequent append.
                intent_seen = True
            if self.like_history_binding(row) != binding:
                raise LikeHistoryError()
            facts = {**report, "source_row": row}
            latest_classification = classify_like_report(facts, row, song_id)
            if report.get("mutation_attempted") is True and latest_classification is None:
                raise LikeHistoryError()
            if latest_classification in {"confirmed", "verification_pending", "write_unknown"}:
                protected_fact_seen = True
            ledger.observe(binding["identity_key"], song_id, row, binding["source_sha256"], facts, owner=owner)

        try:
            # Replace any older per-row report before opening the saved session.
            # Failed preflight must not accidentally import a prior song's like.
            _journal({
                "source_row": row, "song_id": song_id, "phase": "session_lookup", "passed": False,
                "mutation_attempted": False, "mutation_attempts": 0, "mutation_accepted": False,
                "mutation_result": "not_attempted", "renewal_attempted": False,
                "renewal_completed": False, "checked_at_utc": _now(),
            }, report_path)
            with self._http_session(row, proxy, report_path=report_path, song_id=song_id) as session:
                # Internal hook preserves the established runner call signature.
                # It is invoked after durable intent and before any append POST.
                session._like_history_record = record_history
                options = {} if song_id == TEST_SONG_ID else {"declared_song_id": song_id}
                report = run_like_test(session, song_id, report_path=report_path, **options)
            record_history(report)
            result = {"source_row": row, **report}
            _journal(result, report_path)
            return result
        except BaseException:
            # Preserve verified or uncertain write facts even when readback,
            # transport, or a later stage raised instead of returning a report.
            try:
                if report_path.is_file() and report_path.stat().st_size <= 2_000_000:
                    saved_report = json.loads(report_path.read_text(encoding="utf-8"))
                    record_history(saved_report)
            except (ValueError, UnicodeError, OSError):
                # Mutation intent already has its own durable held reservation.
                # A corrupt final report cannot make that reservation eligible.
                pass
            raise
        finally:
            if not intent_seen and not protected_fact_seen or latest_classification == "known_rejected":
                if self.like_history_binding(row) != binding:
                    raise LikeHistoryError()
                ledger.release_prewrite(binding["identity_key"], song_id, owner)

    def like_history_binding(self, row: int) -> dict:
        """Private frozen import/identity binding for execution or offline backfill."""
        from .like_history import LikeHistoryError
        try:
            if type(row) is not int or not 1 <= row <= 2**31 - 1:
                raise ValueError
            selected = self._db.execute("SELECT email_key,record FROM accounts WHERE source_row=?", (row,)).fetchone()
            if selected is None or type(selected[0]) is not str or re.fullmatch(r"[0-9a-f]{64}", selected[0]) is None:
                raise ValueError
            record = _unpack(selected[1])
            if type(record.get("source_row")) is not int or record["source_row"] != row or not hmac.compare_digest(_email_key(self._index_key, record["email"]), selected[0]):
                raise ValueError
            source = self.metadata("source_sha256")
            if type(source) is not str or re.fullmatch(r"[0-9a-f]{64}", source) is None:
                raise ValueError
            return {"source_sha256": source, "identity_key": selected[0]}
        except Exception:
            raise LikeHistoryError() from None

    def remember_like(self, row: int, song_id: str | int, report: dict, *, source_sha256: str,
                      identity_key: str | None = None, job_id: str | None = None) -> dict:
        """Import one exact, source-bound safe report without any account request."""
        from .like_history import LikeHistoryError, LikeHistoryLedger
        from .test_settings import validate_test_song_id
        song_id = validate_test_song_id(song_id)
        binding = self.like_history_binding(row)
        if (type(source_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None or not hmac.compare_digest(source_sha256, binding["source_sha256"])
                or identity_key is not None and (type(identity_key) is not str or re.fullmatch(r"[0-9a-f]{64}", identity_key) is None or not hmac.compare_digest(identity_key, binding["identity_key"]))):
            raise LikeHistoryError()
        ledger = LikeHistoryLedger(self._db)
        recorded = ledger.observe(binding["identity_key"], song_id, row, binding["source_sha256"], report, job_id=job_id)
        return {"source_row": row, "song_id": song_id, "history_status": ledger.state(binding["identity_key"], song_id), "recorded": recorded}

    def like_history(self, song_id: str | int, rows=None) -> dict:
        """Read safe song-specific eligibility using indexed hashes, not sessions."""
        from .like_history import HISTORY_STATES, LikeHistoryError, LikeHistoryLedger
        from .test_settings import validate_test_song_id
        song_id = validate_test_song_id(song_id)
        if rows is not None:
            if type(rows) not in (list, tuple, set, frozenset) or any(type(row) is not int or not 1 <= row <= 2**31 - 1 for row in rows):
                raise LikeHistoryError()
            selected_rows = sorted(set(rows))
            batches = [selected_rows[start:start + 900] for start in range(0, len(selected_rows), 900)]
        else:
            batches = [None]
        ledger = LikeHistoryLedger(self._db)
        try:
            exists = ledger.exists()
            counts = dict.fromkeys(sorted(HISTORY_STATES), 0)
            accounts, eligible = [], []
            for batch in batches:
                where = "" if batch is None else " WHERE a.source_row IN (" + ",".join("?" for _ in batch) + ")"
                if exists:
                    query = "SELECT a.source_row,h.state,h.updated_at_utc FROM accounts a LEFT JOIN account_song_like_history h ON h.email_key=a.email_key AND h.song_id=?" + where + " ORDER BY a.source_row"
                    values = (song_id, *(batch or ()))
                else:
                    query = "SELECT a.source_row,NULL,NULL FROM accounts a" + where + " ORDER BY a.source_row"
                    values = tuple(batch or ())
                for row, state, stamp in self._db.execute(query, values):
                    if state is None:
                        eligible.append(row)
                    elif type(state) is str and state in HISTORY_STATES:
                        if type(stamp) is not str or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:\+00:00|Z)", stamp) is None:
                            raise LikeHistoryError()
                        datetime.fromisoformat(stamp)
                        counts[state] += 1
                        accounts.append({"source_row": row, "state": state, "last_checked_at": stamp})
                    else:
                        raise LikeHistoryError()
            counts["eligible"] = len(eligible)
            return {"song_id": song_id, "accounts": accounts, "counts": counts,
                    "blocked_rows": [account["source_row"] for account in accounts], "eligible_rows": eligible}
        except Exception:
            raise LikeHistoryError() from None

    def check(self, row: int) -> dict:
        saved = self.session(row)
        try:
            with AnghamiSession(saved=saved) as session:
                report = session.check(negative_control=True)
        except SessionError as exc:
            from .errors import safe_request_failure
            failure = safe_request_failure(exc)
            if failure.get("failure_category") == "account":
                self.record_account_failure(row, exc)
            elif not failure:
                with self._db:
                    self._db.execute("UPDATE accounts SET state='check_failed', checked_at_utc=? WHERE source_row=?", (_now(), row))
            raise
        with self._db:
            self._require_session_not_held(row)
            self._db.execute(
                "UPDATE accounts SET state='ready', checked_at_utc=? WHERE source_row=?", (report["checked_at_utc"], row),
            )
            self._clear_account_failure(row)
        return {"source_row": row, **report}

    def restore_source(self, destination: Path) -> dict:
        destination = Path(destination)
        raw = load_protected_bytes(self.path.parent / self.metadata("backup_relative"))
        if hashlib.sha256(raw).hexdigest() != self.metadata("source_sha256"):
            raise SessionError("The encrypted source backup failed verification.")
        # Exclusive creation protects the original registered.txt and existing files.
        with destination.open("xb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        return {"restored": True, "bytes": len(raw), "output": str(destination.resolve())}
