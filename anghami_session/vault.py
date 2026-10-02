"""Offline account migration and isolated, individually selected sessions."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone

from .client import AnghamiSession, validate_session
from .errors import SessionError
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
        accounts = [{
            "source_row": row, "state": state, "session_saved": bool(has_session),
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

    def select_test_candidates(self, count: int, *, start_row: int = 1) -> list[int]:
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5:
            raise SessionError("The preparation count must be an integer from 1 to 5.")
        if isinstance(start_row, bool) or not isinstance(start_row, int) or start_row < 1:
            raise SessionError("The starting source row must be a positive integer.")
        condition, defaults = self._test_cohort_condition()
        identities = {row[0] for row in self._db.execute(
            f"SELECT DISTINCT email_key FROM accounts WHERE {condition}", defaults,
        )}
        selected = []
        for row, identity in self._db.execute(
            "SELECT source_row, email_key FROM accounts WHERE source_row>=? ORDER BY source_row",
            (start_row,),
        ):
            if identity in identities:
                continue
            identities.add(identity)
            selected.append(row)
            if len(selected) == count:
                break
        return selected

    def enable_test_account(self, row: int) -> dict:
        if isinstance(row, bool) or not isinstance(row, int) or row < 1:
            raise SessionError("The source row must be a positive integer.")
        already_enabled = row in self.enrolled_test_rows()
        with self._db:
            # SAVEPOINT makes CREATE TABLE and INSERT atomic even with SQLite's
            # legacy transaction mode, which does not begin a transaction for DDL.
            self._db.execute("SAVEPOINT enroll_test_account")
            try:
                state = self._db.execute(
                    "SELECT state, session IS NOT NULL FROM accounts WHERE source_row=?", (row,),
                ).fetchone()
                if state is None:
                    raise SessionError("No account exists at that source row.")
                if state[0] != "ready" or not state[1]:
                    raise SessionError("This account needs a verified ready saved session before test enrollment.")
                self.session(row)  # Validate format and its binding to this row.
                self._db.execute("""
                    CREATE TABLE IF NOT EXISTS test_accounts (
                        source_row INTEGER PRIMARY KEY REFERENCES accounts(source_row),
                        added_at_utc TEXT NOT NULL
                    )
                """)
                self._db.execute(
                    "INSERT OR IGNORE INTO test_accounts(source_row, added_at_utc) VALUES (?, ?)", (row, _now()),
                )
                self._db.execute("RELEASE SAVEPOINT enroll_test_account")
            except Exception:
                self._db.execute("ROLLBACK TO SAVEPOINT enroll_test_account")
                self._db.execute("RELEASE SAVEPOINT enroll_test_account")
                raise
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

    def attach(self, row: int, saved: dict, *, new_password: str | None = None, proxy=None) -> dict:
        saved = validate_session(saved)
        self._require_identity(row, saved)
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
        encrypted = _pack(saved)
        encrypted_record = _pack(replacement_record) if replacement_record is not None else None
        with self._db:
            self._db.execute(
                "UPDATE accounts SET session=?, record=COALESCE(?, record), state='ready', checked_at_utc=? WHERE source_row=?",
                (encrypted, encrypted_record, report["checked_at_utc"], row),
            )
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
        """Like the declared test track through one selected saved account."""
        song_id = _validated_test_song(song_id, declared_song_id)
        cohort = self.enrolled_test_rows()
        if row not in cohort:
            allowed = ", ".join(map(str, sorted(cohort)))
            raise SessionError(f"This test command is limited to the selected test accounts (source rows {allowed}).")
        from .like_test import run_like_test
        report_path = self.path.parent / f"account-{row}.test-like-report.json"
        with self._http_session(row, proxy, report_path=report_path, song_id=song_id) as session:
            options = {} if song_id == TEST_SONG_ID else {"declared_song_id": song_id}
            report = run_like_test(session, song_id, report_path=report_path, **options)
        result = {"source_row": row, **report}
        report_path.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        return result

    def check(self, row: int) -> dict:
        saved = self.session(row)
        try:
            with AnghamiSession(saved=saved) as session:
                report = session.check(negative_control=True)
        except SessionError:
            with self._db:
                self._db.execute(
                    "UPDATE accounts SET state='check_failed', checked_at_utc=? WHERE source_row=?", (_now(), row),
                )
            raise
        with self._db:
            self._db.execute(
                "UPDATE accounts SET state='ready', checked_at_utc=? WHERE source_row=?", (report["checked_at_utc"], row),
            )
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
