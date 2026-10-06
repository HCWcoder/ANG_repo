"""Resumable preparation for one exact imported country tag.

Preview/status are offline. Only --run starts browser or HTTP preparation; no
play/like events are available here. Never use the UI concurrently with a run.
"""

import argparse
from contextlib import contextmanager, nullcontext
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
from queue import Empty, Queue
import sqlite3
import tempfile
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

from .errors import LoginCaptureError, SessionError, SessionReviewRequiredError, safe_login_failure, safe_request_failure, safe_session_storage_failure
from .provider_recovery import provider_failure, wait_for_provider
from .proxy import COUNTRY_CHECK_MAX_ATTEMPTS, PACKETSTREAM_ENDPOINT, ProxyCountryError, load_packetstream_proxy, safe_proxy_country_failure
from .proxy_pool import DEFAULT_STICKY_POOL_PATH, StickyProxyPool
from .vault import AccountVault, DEFAULT_VAULT_PATH, ROOT

_STATES = {"pending", "connection_pending", "in_progress", "ready", "already_ready", "already_enrolled", "failed", "unknown", "session_review_pending"}
_STATUSES = {"not_started", "running", "paused", "completed", "completed_with_pending", "attention_required"}
_PHASES = _STATES | {"preparing", "proxy_preflight", "provider_retry", "provider_wait", "session_lookup", "session_recovery", "login", "validation", "complete", "stopped"}
MAX_WORKER_REQUEST = 2**53 - 1  # Exact integer transport through the local JSON UI.
MAX_HTTP_WORKERS = MAX_WORKER_REQUEST  # Compatibility name; no operational worker cap.
MAX_FAILURE_BUDGET = 100
_CODES = {
    None, "stop_requested", "limit_reached", "ui_busy", "ui_activity", "ui_unavailable",
    "account_failed", "infrastructure_failed", "browser_cleanup_failed", "browser_unavailable",
    "interrupted_unknown", "unknown_attempt", "source_changed", "scope_mismatch",
    "repeated_failures",
    "proxy_preflight_failed",
    "provider_unavailable",
}
_PROXY_ENV = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy"}
_FAILURE_HOLDS = {"repeated_failures", "infrastructure_failed", "browser_cleanup_failed", "browser_unavailable"}
_PROXY_FAILURE_STAGES = {"profile_load", "profile_country", "country_check", "preparation"}
_PROXY_FAILURE_MESSAGES = {
    "PacketStream proxy credentials are not configured. Save them before using the Egypt proxy.": "credentials_missing",
    "The PacketStream proxy configuration could not be unlocked. Use the Windows user who saved it or configure it again.": "configuration_locked",
    "The PacketStream proxy configuration is invalid. Configure it again.": "configuration_invalid",
    "The saved proxy route must select Egypt.": "profile_country_mismatch",
    "The selected proxy route was not verified.": "route_unverified",
    "PacketStream proxy authentication was rejected (HTTP 407). Check the configured credentials and available balance.": "authentication_rejected",
    "The proxy country check did not return HTTP 200.": "country_http_not_ok",
    "The proxy country check did not prove a successful proxy tunnel.": "proxy_tunnel_unconfirmed",
    "The proxy country check did not verify Egypt.": "egypt_unverified",
    "The proxy country check failed. No direct connection was attempted.": "country_check_failed",
    "The login proxy country check failed. No browser was opened.": "country_check_failed",
    "The browser proxy country check did not verify Egypt. No Anghami sign-in was attempted.": "egypt_unverified",
    "The browser proxy country-check page could not close. No Anghami sign-in was attempted.": "browser_check_cleanup_failed",
    "The configured proxy route was not confirmed. No direct fallback is permitted.": "proxy_tunnel_unconfirmed",
}
_PROXY_FAILURE_CODES = set(_PROXY_FAILURE_MESSAGES.values()) | {"proxy_error"}
_UI_READ_MAX_ATTEMPTS = 6
_UI_READ_RETRY_SECONDS = 0.1


def _now():
    return datetime.now(timezone.utc).isoformat()


def _country(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Z]{2}", value) is None:
        raise SessionError("Use an exact uppercase two-letter country tag.")
    return value


def _binding(identity):
    raw = identity if isinstance(identity, bytes) else str(identity).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _source_hash(vault, path):
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise SessionError("The imported source file could not be read.") from None
    digest = hashlib.sha256(raw).hexdigest()
    if digest != vault.metadata("source_sha256"):
        raise SessionError("The source file does not match this vault import.")
    return digest, raw


def _identity_state(vault, row, *, enrolled_rows=None):
    result = vault._db.execute("SELECT email_key FROM accounts WHERE source_row=?", (row,)).fetchone()
    if result is None:
        raise SessionError("The frozen source row is missing from the vault.")
    identity = result[0]
    copies = list(vault._db.execute(
        "SELECT source_row,state,session IS NOT NULL FROM accounts WHERE email_key=? ORDER BY source_row",
        (identity,),
    ))
    held = any(state == "session_review_pending" for _, state, _ in copies)
    table = vault._db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_session_review'",
    ).fetchone()
    if table is not None:
        held = held or vault._db.execute(
            "SELECT 1 FROM account_session_review WHERE email_key=? LIMIT 1", (identity,),
        ).fetchone() is not None
    preparation_table = vault._db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_preparation_review'",
    ).fetchone()
    if preparation_table is not None:
        held = held or vault._db.execute(
            "SELECT 1 FROM account_preparation_review WHERE email_key=? LIMIT 1", (identity,),
        ).fetchone() is not None
    if held:
        return identity, "session_review_pending"
    enrolled = vault.enrolled_test_rows() if enrolled_rows is None else enrolled_rows
    ready = any(state == "ready" and saved for _, state, saved in copies)
    enabled = any(source_row in enrolled for source_row, _, _ in copies)
    return identity, "already_ready" if ready else "already_enrolled" if enabled else "pending"


def build_plan(vault, source_path, *, country="LB"):
    """Bind exact source-country rows and deduplicate their imported identities."""
    country = _country(country)
    source_path = Path(source_path).resolve()
    digest, raw = _source_hash(vault, source_path)
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeError:
        raise SessionError("The imported source must use UTF-8 encoding.") from None
    finally:
        del raw
    tagged = [number for number, line in enumerate(lines, 1) if line.strip() and line.partition("~")[0].strip() == country]
    del lines
    seen, rows, bindings = set(), [], {}
    for row in tagged:
        identity, state = _identity_state(vault, row)
        binding = _binding(identity)
        if binding in seen:
            continue
        seen.add(binding)
        rows.append({"source_row": row, "state": state})
        bindings[str(row)] = binding
    manifest = {"country": country, "source_sha256": digest, "identity_bindings": bindings}
    plan_id = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        **manifest, "plan_id": plan_id, "source_path": str(source_path),
        "tagged_rows": len(tagged), "duplicate_rows": len(tagged) - len(rows), "rows": rows,
    }


def build_selected_plan(vault, source_path, rows, *, country="EG"):
    """Bind only the supplied frozen country rows, preserving their exact order."""
    country = _country(country)
    if (not isinstance(rows, (list, tuple)) or not rows
            or any(type(row) is not int or row < 1 for row in rows)
            or len(set(rows)) != len(rows)):
        raise SessionError("Choose distinct positive frozen source rows.")
    source_path = Path(source_path).resolve()
    digest, raw = _source_hash(vault, source_path)
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeError:
        raise SessionError("The imported source must use UTF-8 encoding.") from None
    finally:
        del raw
    selected, bindings, seen = [], {}, set()
    enrolled_rows = vault.enrolled_test_rows()
    for row in rows:
        if row > len(lines) or not lines[row - 1].strip() or lines[row - 1].partition("~")[0].strip() != country:
            raise SessionError("A selected frozen source row has a different country tag or is missing.")
        record = vault.record(row)
        try:
            if not isinstance(record, dict) or record.get("country") != country:
                raise SessionError("A selected frozen vault row has a different country tag.")
        finally:
            del record
        identity, state = _identity_state(vault, row, enrolled_rows=enrolled_rows)
        binding = _binding(identity)
        if binding in seen:
            raise SessionError("Choose one frozen source row per registered account identity.")
        seen.add(binding)
        selected.append({"source_row": row, "state": state})
        bindings[str(row)] = binding
    del lines
    manifest = {"country": country, "source_sha256": digest, "identity_bindings": bindings}
    plan_id = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        **manifest, "plan_id": plan_id, "source_path": str(source_path),
        "tagged_rows": len(selected), "duplicate_rows": 0, "rows": selected,
    }


class SelectedRowVault:
    """Allow the normal preparation function to act only on its frozen row."""

    def __init__(self, vault, row, *, country, identity_binding=None, proxy=None):
        if type(row) is not int or row < 1:
            raise SessionError("Choose one positive frozen source row.")
        self._vault, self.row, self.country = vault, row, _country(country)
        self._proxy = proxy
        self._attach_acknowledged = False
        self._enrollment_acknowledged = False
        self.path = vault.path
        identity, _ = _identity_state(vault, row)
        self._binding = _binding(identity) if identity_binding is None else identity_binding
        self._check(row)

    def _check(self, row):
        if type(row) is not int or row != self.row:
            raise SessionError("The country preparation row cannot change.")
        identity, state = _identity_state(self._vault, row)
        if _binding(identity) != self._binding:
            raise SessionError("The frozen account identity changed.")
        if state == "session_review_pending":
            raise SessionReviewRequiredError()

    def select_test_candidates(self, count, *, start_row=1):
        if type(count) is not int or count != 1 or type(start_row) is not int or start_row != self.row:
            raise SessionError("Country preparation selects exactly its frozen row.")
        self._check(start_row)
        record = self.record(start_row)
        del record
        return [self.row]

    def _require_session_not_held(self, row):
        self._check(row)

    def record(self, row):
        self._check(row)
        record = self._vault.record(row)
        if record.get("country") != self.country:
            raise SessionError("The frozen row has a different country tag.")
        return record

    def session(self, row):
        self._check(row)
        return self._vault.session(row)

    def pending_session(self, row):
        self._check(row)
        load = getattr(self._vault, "pending_session", None)
        return load(row) if callable(load) else None

    def save_pending_session(self, row, saved):
        self._check(row)
        save = getattr(self._vault, "save_pending_session", None)
        if not callable(save):
            raise SessionError("The frozen pending session could not be saved securely.")
        return save(row, saved)

    def attach(self, row, saved, **options):
        self._check(row)
        if options.get("proxy") is not self._proxy:
            raise SessionError("The selected preparation connection cannot change.")
        result = self._vault.attach(row, saved, **options)
        self._attach_acknowledged = True
        return result

    def enable_test_account(self, row):
        self._check(row)
        result = self._vault.enable_test_account(row)
        self._enrollment_acknowledged = True
        return result

    def record_account_failure(self, row, failure):
        self._check(row)
        return self._vault.record_account_failure(row, failure)

    def failure_review(self):
        review = self._vault.failure_review()
        rows = [item for item in review.get("accounts", []) if item.get("source_row") == self.row]
        return {"accounts": rows, "failed_rows": [item["source_row"] for item in rows], "total": len(rows)}


class CountryJobLock:
    """An OS lock released automatically if the local process exits."""

    def __init__(self, path):
        self.path, self._stream = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise SessionError("A country preparation job is already running.") from None
        self._stream = stream
        return self

    def __exit__(self, *_):
        if self._stream is None:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None


@contextmanager
def direct_runtime(*, browser=True):
    """Strict Direct routing only in this runner process; restore all settings."""
    if browser:
        from . import capture
    previous = {name: value for name, value in os.environ.items() if name.casefold() in _PROXY_ENV}
    original_launch = capture.launch_browser if browser else None
    try:
        for name in previous:
            os.environ.pop(name, None)
        os.environ["NO_PROXY"] = "*"
        if browser:
            capture.launch_browser = partial(original_launch, direct=True)
        yield
    finally:
        if browser:
            capture.launch_browser = original_launch
        for name in list(os.environ):
            if name.casefold() in _PROXY_ENV:
                os.environ.pop(name, None)
        os.environ.update(previous)


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        replacement_deadline = monotonic() + 2
        while True:
            try:
                os.replace(temporary, path)
                break
            except OSError as exc:
                windows_contention = os.name == "nt" and (
                    isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
                )
                if not windows_contention or monotonic() >= replacement_deadline:
                    raise
                # Only retry this local atomic rename. Account HTTP is untouched.
                sleep(0.1)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _new_progress(plan):
    return {
        "format_version": 1, "country": plan["country"], "source_sha256": plan["source_sha256"],
        "plan_id": plan["plan_id"], "run_id": uuid4().hex, "status": "not_started", "pause_reason": None,
        "created_at_utc": _now(), "updated_at_utc": _now(), "active_row": None,
        "active_rows": [], "workers": 1,
        "max_consecutive_failures": 3,
        "proxy_pool_active": False, "proxy_pool_count": 0,
        "proxy_pool_fingerprint": None, "proxy_pool_cursor": 0,
        "proxy_pool_endpoint": None, "proxy_pool_country": None, "ui_read_failure": None,
        "tagged_rows": plan["tagged_rows"], "duplicate_rows": plan["duplicate_rows"],
        "unknown_acknowledged": False, "infrastructure_failures": 0, "consecutive_failures": 0,
        "reduce_browser_data": True,
        "no_browser": False,
        "connection": "direct",
        "failure_hold": None,
        "proxy_failure": None,
        "rows": [{**row, "attempts": 0, "phase": row["state"], "error_code": None, "login_failure": None, "connection": None} for row in plan["rows"]],
    }


def _effective_workers(progress, requested, *, limit=None):
    remaining = sum(row["state"] in {"pending", "connection_pending", "in_progress"} for row in progress["rows"])
    return min(requested, remaining, remaining if limit is None else limit)


def _stored_login_failure(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid login diagnostics.")
    error = LoginCaptureError(
        value.get("code"), stage=value.get("stage"),
        page_http_status=value.get("page_http_status"), auth_http_status=value.get("auth_http_status"),
        authentication_result=value.get("authentication_result"),
    )
    return safe_login_failure(error) or None


def _stored_proxy_failure(value, rows):
    if value is None:
        return None
    if (
        not isinstance(value, dict) or type(value.get("source_row")) is not int
        or value["source_row"] not in rows
        or type(value.get("stage")) is not str or value["stage"] not in _PROXY_FAILURE_STAGES
        or type(value.get("code")) is not str or value["code"] not in _PROXY_FAILURE_CODES
    ):
        raise ValueError("Invalid proxy diagnostics.")
    result = {name: value[name] for name in ("source_row", "stage", "code")}
    diagnostic_names = {"failure_kind", "curl_code", "http_status", "proxy_connect_http_status", "country_check_attempts", "retry_after_seconds"}
    if diagnostic_names.intersection(value):
        diagnostics = {name: value[name] for name in diagnostic_names if name in value}
        error = ProxyCountryError(
            diagnostics.get("failure_kind"), curl_code=diagnostics.get("curl_code"),
            http_status=diagnostics.get("http_status"),
            proxy_connect_http_status=diagnostics.get("proxy_connect_http_status"),
            country_check_attempts=diagnostics.get("country_check_attempts"),
            retry_after_seconds=diagnostics.get("retry_after_seconds"),
        )
        clean = safe_proxy_country_failure(error)
        if clean != diagnostics:
            raise ValueError("Invalid proxy country-check diagnostics.")
        result.update(clean)
    return result


def _safe_proxy_failure(exc, row, stage):
    """Classify only fixed local messages; never save exception/profile data."""
    code = "country_check_failed" if stage == "country_check" else "proxy_error"
    if isinstance(exc, SessionError):
        try:
            code = _PROXY_FAILURE_MESSAGES.get(str(exc), code)
        except Exception:
            pass
    return {"source_row": row, "stage": stage, "code": code, **safe_proxy_country_failure(exc)}


def _failure_hold(progress):
    if progress.get("failure_hold") in _FAILURE_HOLDS:
        return progress["failure_hold"]
    if progress.get("pause_reason") in _FAILURE_HOLDS:
        return progress["pause_reason"]
    maximum = progress.get("max_consecutive_failures", 3)
    custom = progress.get("no_browser", False) and maximum != 3
    if not custom and progress.get("infrastructure_failures", 0) >= maximum:
        return "infrastructure_failed"
    if progress.get("consecutive_failures", 0) >= maximum:
        return "repeated_failures"
    return None


def _stored_finalization_failure(value):
    if value is None:
        return None
    required = {"code", "failed_phase", "error_kind", "prior_error_code", "reconciled"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"errno", "winerror", "report_error_code"}:
        raise ValueError("Invalid local finalization diagnostics.")
    enums = {
        "code": {"journal_failed", "postcommit_reporting_failed"},
        "failed_phase": {"validation", "complete"},
        "error_kind": {"filesystem_error", "report_error"},
        "prior_error_code": {"account_failed"},
        "reconciled": {"worker_committed", "post_run_committed"},
        "report_error_code": {"preparation_failed", "journal_failed"},
    }
    for name, choices in enums.items():
        if name in value and (type(value[name]) is not str or value[name] not in choices):
            raise ValueError("Invalid local finalization diagnostics.")
    for name in ("errno", "winerror"):
        if name in value and (value["error_kind"] != "filesystem_error" or type(value[name]) is not int or not 0 <= value[name] <= 65535):
            raise ValueError("Invalid local finalization diagnostics.")
    return dict(value)


def _stored_session_store_failure(value):
    if value is None:
        return None
    from .preparation import safe_session_store_failure
    result = safe_session_store_failure(value)
    if not result:
        raise ValueError("Invalid local pending-session diagnostics.")
    return result


def _stored_preparation_review(value):
    if value is None:
        return None
    if (type(value) is not dict or set(value) != {"code", "stage", "candidate_retained"}
            or type(value.get("candidate_retained")) is not bool
            or (value.get("code"), value.get("stage")) not in {
                ("preparation_validation_unknown", "validation"),
                ("preparation_outcome_unknown", "session_recovery_renewal"),
            } or (value["stage"] == "session_recovery_renewal" and value["candidate_retained"])):
        raise ValueError("Invalid preparation review evidence.")
    return dict(value)


def _stored_local_storage_failure(value):
    if value is None:
        return None
    safe = safe_session_storage_failure(value)
    if not safe:
        raise ValueError("Invalid local session storage evidence.")
    return safe


def load_progress(path, plan):
    """Read a matching frozen manifest; untrusted fields cannot enter reports."""
    path = Path(path)
    if not path.exists():
        return _new_progress(plan)
    try:
        if path.stat().st_size > 10_000_000:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
        template = _new_progress(plan)
        if not isinstance(value, dict) or type(value.get("format_version")) is not int or value["format_version"] != 1:
            raise ValueError
        for name in ("country", "source_sha256", "plan_id", "tagged_rows", "duplicate_rows"):
            if value.get(name) != template[name]:
                raise ValueError
        rows = value.get("rows")
        if (
            not isinstance(rows, list) or len(rows) != len(plan["rows"])
            or not all(isinstance(row, dict) for row in rows)
            or [row.get("source_row") for row in rows] != [row["source_row"] for row in plan["rows"]]
        ):
            raise ValueError
        if value.get("status") not in _STATUSES or value.get("pause_reason") not in _CODES:
            raise ValueError
        maximum = value.get("max_consecutive_failures", 3)
        if type(maximum) is not int or not 1 <= maximum <= MAX_FAILURE_BUDGET:
            raise ValueError
        if maximum != 3 and value.get("no_browser", False) is not True:
            raise ValueError
        if type(value.get("unknown_acknowledged")) is not bool or type(value.get("infrastructure_failures")) is not int or not 0 <= value["infrastructure_failures"] <= maximum:
            raise ValueError
        consecutive = value.get("consecutive_failures", 0)
        if type(consecutive) is not int or not 0 <= consecutive <= maximum:
            raise ValueError
        if type(value.get("reduce_browser_data", True)) is not bool:
            raise ValueError
        if type(value.get("no_browser", False)) is not bool:
            raise ValueError
        workers = value.get("workers", 1)
        if type(workers) is not int or not 1 <= workers <= MAX_WORKER_REQUEST:
            raise ValueError
        requested = value.get("requested_workers", workers)
        effective = value.get("effective_workers", min(workers, len(rows)))
        if (type(requested) is not int or requested != workers
                or type(effective) is not int or not 0 <= effective <= min(workers, len(rows))):
            raise ValueError
        if workers > 1 and not value.get("no_browser", False):
            raise ValueError
        if value.get("connection", "direct") not in {"direct", "proxy_egypt"}:
            raise ValueError
        if value.get("failure_hold") not in _FAILURE_HOLDS | {None}:
            raise ValueError
        pool_count, pool_cursor = value.get("proxy_pool_count", 0), value.get("proxy_pool_cursor", 0)
        pool_fingerprint = value.get("proxy_pool_fingerprint")
        pool_active = value.get("proxy_pool_active", False)
        pool_endpoint = value.get("proxy_pool_endpoint")
        pool_country = value.get("proxy_pool_country", "EG" if pool_count else None)
        if (
            type(pool_active) is not bool or type(pool_count) is not int or not 0 <= pool_count <= 100_000
            or type(pool_cursor) is not int or not 0 <= pool_cursor <= (2 ** 63 - 1)
            or (pool_count == 0 and (pool_fingerprint is not None or pool_cursor != 0 or pool_active))
            or (pool_count > 0 and (type(pool_fingerprint) is not str or re.fullmatch(r"[0-9a-f]{64}", pool_fingerprint) is None))
            or (pool_active and (value.get("no_browser", False) is not True or value.get("connection") != "proxy_egypt"))
            or pool_endpoint not in {None, "http://proxy.packetstream.io:31112", "https://proxy.packetstream.io:31111", "mixed"}
            or pool_country not in {None, "EG", "US"}
            or (pool_count > 0 and pool_country is None)
            or (pool_count == 0 and pool_endpoint is not None)
            or (pool_count == 0 and pool_country is not None)
        ):
            raise ValueError
        for row in rows:
            if (
                type(row.get("source_row")) is not int or row.get("state") not in _STATES
                or type(row.get("attempts")) is not int or not 0 <= row["attempts"] <= 1
                or row.get("phase") not in _PHASES or row.get("error_code") not in _CODES
                or (row["state"] != "session_review_pending"
                    and (row["state"] in {"pending", "already_ready", "already_enrolled"}) != (row["attempts"] == 0))
                or (row["state"] == "session_review_pending" and row["attempts"] == 1
                    and _stored_preparation_review(row.get("preparation_review")) is None)
                or row.get("connection") not in {None, "direct", "proxy_egypt"}
            ):
                raise ValueError
        result = {name: value.get(name, template[name]) for name in template if name != "rows"}
        result["proxy_pool_country"] = pool_country
        result.update(requested_workers=requested, effective_workers=effective)
        result["failure_hold"] = _failure_hold(value)
        ui_read_failure = value.get("ui_read_failure")
        if ui_read_failure is not None and not safe_ui_read_failure(ui_read_failure):
            raise ValueError
        result["ui_read_failure"] = safe_ui_read_failure(ui_read_failure) or None
        result["proxy_failure"] = _stored_proxy_failure(value.get("proxy_failure"), {row["source_row"] for row in rows})
        result["rows"] = [{
            **{name: row[name] for name in ("source_row", "state", "attempts", "phase", "error_code")},
            "login_failure": _stored_login_failure(row.get("login_failure")),
            "connection": row.get("connection") or ("direct" if row["attempts"] else None),
        } for row in rows]
        for stored, original in zip(result["rows"], rows):
            review = _stored_preparation_review(original.get("preparation_review"))
            if review is not None:
                if stored["state"] != "session_review_pending" or stored["attempts"] != 1:
                    raise ValueError
                stored["preparation_review"] = review
            local_storage = _stored_local_storage_failure(original.get("session_storage_failure"))
            if local_storage is not None:
                if stored["state"] not in {"failed", "unknown", "session_review_pending"}:
                    raise ValueError
                stored["session_storage_failure"] = local_storage
            legacy_provider = original.get("legacy_provider_error_code")
            if legacy_provider is not None:
                if legacy_provider != "proxy_preflight_failed":
                    raise ValueError
                stored["legacy_provider_error_code"] = legacy_provider
            if original.get("provider_failure") is not None:
                diagnostic = provider_failure(original["provider_failure"])
                if not diagnostic or stored["state"] != "connection_pending":
                    raise ValueError
                stored["provider_failure"] = diagnostic
            retry_count = original.get("provider_retry_count", 0)
            if type(retry_count) is not int or not 0 <= retry_count <= 2:
                raise ValueError
            if retry_count:
                stored["provider_retry_count"] = retry_count
            finalization = _stored_finalization_failure(original.get("local_finalization_failure"))
            if finalization is not None:
                if stored["state"] != "ready" or stored["attempts"] != 1:
                    raise ValueError
                stored["local_finalization_failure"] = finalization
            session_store = _stored_session_store_failure(original.get("local_session_failure"))
            if session_store is not None:
                expected = "interrupted_unknown" if session_store["renewal_completed"] else "infrastructure_failed"
                if stored["error_code"] != expected or stored["state"] != ("unknown" if session_store["renewal_completed"] else "failed"):
                    raise ValueError
                stored["local_session_failure"] = session_store
        # These arbitrary strings are never displayed; validate before rewriting.
        if re.fullmatch(r"[0-9a-f]{32}", result["run_id"]) is None:
            raise ValueError
        for name in ("created_at_utc", "updated_at_utc"):
            datetime.fromisoformat(result[name])
        if result["active_row"] is not None and (
            type(result["active_row"]) is not int
            or result["active_row"] not in {row["source_row"] for row in rows}
        ):
            raise ValueError
        active_rows = value.get("active_rows", [] if result["active_row"] is None else [result["active_row"]])
        stored_rows = {item["source_row"]: item for item in rows}
        if (
            not isinstance(active_rows, list) or len(active_rows) > effective
            or any(type(row) is not int or row not in stored_rows for row in active_rows)
            or len(set(active_rows)) != len(active_rows)
            or result["active_row"] != (active_rows[0] if active_rows else None)
            or any(stored_rows[row]["state"] not in {"in_progress", "unknown"} for row in active_rows)
        ):
            raise ValueError
        result["active_rows"] = active_rows
        return result
    except (OSError, ValueError, TypeError, KeyError):
        raise SessionError("The country progress file is invalid or belongs to a different frozen plan.") from None


def summarize(progress):
    counts = {state: sum(row["state"] == state for row in progress["rows"]) for state in sorted(_STATES)}
    account_failed = sum(row["state"] == "failed" and row["error_code"] == "account_failed" for row in progress["rows"])
    attempted = [row["source_row"] for row in progress["rows"] if row["attempts"]]
    pending = [row["source_row"] for row in progress["rows"] if row["state"] in {"pending", "connection_pending"}]
    no_browser = progress.get("no_browser", False)
    summary = {
        "country": progress["country"], "status": progress["status"], "pause_reason": progress["pause_reason"],
        "tagged_rows": progress["tagged_rows"], "unique_accounts": len(progress["rows"]),
        "duplicate_rows": progress["duplicate_rows"], "counts": counts,
        "account_failed_count": account_failed,
        "session_review_pending_count": counts["session_review_pending"],
        "session_review_pending_rows": [row["source_row"] for row in progress["rows"]
                                        if row["state"] == "session_review_pending"],
        "infrastructure_failed_count": counts["failed"] - account_failed,
        "attempted_accounts": len(attempted),
        "active_row": progress["active_row"], "last_attempted_rows": attempted[-5:], "next_rows": pending[:5],
        "active_rows": progress.get("active_rows", [] if progress["active_row"] is None else [progress["active_row"]]),
        "workers": progress.get("workers", 1),
        "requested_workers": progress.get("workers", 1),
        "effective_workers": progress.get("effective_workers", min(progress.get("workers", 1), len(progress["rows"]))),
        "connection": progress.get("connection", "direct"), "browser": "none" if no_browser else "cloakbrowser", "headless": not no_browser,
        "no_browser": no_browser, "browser_required": not no_browser,
        "preparation_method": "http" if no_browser else "browser",
        "reduce_browser_data": False if no_browser else progress.get("reduce_browser_data", True),
        "automatic_retry": False, "play_events_sent": 0, "like_events_sent": 0,
        "account_automatic_retry": False,
        "country_check_max_attempts": COUNTRY_CHECK_MAX_ATTEMPTS if progress.get("connection", "direct") == "proxy_egypt" else 0,
        "country_check_retry_scope": "cookie_free_only" if progress.get("connection", "direct") == "proxy_egypt" else None,
        "consecutive_failures": progress.get("consecutive_failures", 0),
        "max_consecutive_failures": progress.get("max_consecutive_failures", 3),
        "failure_hold": _failure_hold(progress),
        "connection_pending_rows": [row["source_row"] for row in progress["rows"] if row["state"] == "connection_pending"],
        "recent_provider_failures": [{"source_row": row["source_row"], "provider_retry_count": row.get("provider_retry_count", 0),
                                      **provider_failure(row.get("provider_failure"))}
                                     for row in progress["rows"] if row["state"] == "connection_pending"][-5:],
        "proxy_failure": _stored_proxy_failure(progress.get("proxy_failure"), {row["source_row"] for row in progress["rows"]}),
        "ui_read_failure": safe_ui_read_failure(progress.get("ui_read_failure")) or None,
        "recent_failures": [{
            "source_row": row["source_row"], "error_code": row["error_code"],
            "login_failure": _stored_login_failure(row.get("login_failure")),
            "connection": row.get("connection") or ("direct" if row["attempts"] else None),
        } for row in progress["rows"] if row["state"] in {"failed", "unknown"}][-5:],
    }
    if summary["connection"] == "proxy_egypt":
        summary["proxy"] = {
            "provider": "PacketStream", "country": progress.get("proxy_pool_country") or "EG",
            "endpoint": PACKETSTREAM_ENDPOINT, "sticky": True,
        }
    if progress.get("proxy_pool_active", False):
        summary["proxy"]["endpoint"] = progress.get("proxy_pool_endpoint") or "pool"
        summary["proxy_pool"] = {
            "pool_size": progress["proxy_pool_count"], "selection": "ordered_wrap",
            "next_ordinal": progress["proxy_pool_cursor"],
            "next_entry": progress["proxy_pool_cursor"] % progress["proxy_pool_count"] + 1,
            "fingerprint": progress["proxy_pool_fingerprint"],
            "endpoint": progress.get("proxy_pool_endpoint"),
        }
    reconciled = [row for row in progress["rows"] if row.get("local_finalization_failure") is not None]
    if reconciled:
        summary["local_finalization_reconciled"] = len(reconciled)
        summary["recent_local_finalization_failures"] = [
            {"source_row": row["source_row"], **_stored_finalization_failure(row["local_finalization_failure"])}
            for row in reconciled[-5:]
        ]
    local_session_failures = [row for row in progress["rows"] if row.get("local_session_failure") is not None]
    if local_session_failures:
        summary["recent_local_session_failures"] = [
            {"source_row": row["source_row"], **_stored_session_store_failure(row["local_session_failure"])}
            for row in local_session_failures[-5:]
        ]
    local_storage = [row for row in progress["rows"] if row.get("session_storage_failure") is not None]
    if local_storage:
        summary["recent_session_storage_failures"] = [
            {"source_row": row["source_row"], **_stored_local_storage_failure(row["session_storage_failure"])}
            for row in local_storage[-5:]
        ]
    if no_browser and progress.get("max_consecutive_failures", 3) != 3:
        summary["failure_limit_scope"] = ("new_dispatch" if summary["effective_workers"] > summary["max_consecutive_failures"]
                                          else "active_budget")
    return summary


def _migrate_provider_rows(progress):
    """Reclassify only old, explicitly identified proxy failures under the lock."""
    changed = False
    for item in progress["rows"]:
        if item["state"] != "failed" or item["error_code"] != "proxy_preflight_failed":
            continue
        evidence = progress.get("proxy_failure")
        diagnostic = provider_failure(evidence) if isinstance(evidence, dict) and evidence.get("source_row") == item["source_row"] else {}
        if not diagnostic:
            from .errors import RequestFailure
            diagnostic = provider_failure(RequestFailure("request_transport_failed", stage="country_check", retry_safe=False))
        item.update(state="connection_pending", phase="connection_pending", error_code="provider_unavailable",
                    provider_failure=diagnostic, legacy_provider_error_code="proxy_preflight_failed")
        changed = True
    if changed and not any(item["state"] in {"failed", "unknown", "in_progress"} for item in progress["rows"]):
        progress.update(consecutive_failures=0, infrastructure_failures=0, failure_hold=None)
        if progress.get("pause_reason") in {"proxy_preflight_failed", "repeated_failures"}:
            progress.update(status="not_started", pause_reason=None)
    return changed


def safe_ui_read_failure(value):
    """Only fixed ownership-read facts, with no path or exception text."""
    if type(value) is not dict or set(value) - {"code", "stage", "attempts", "errno", "winerror"}:
        return {}
    combinations = {
        "ui_file_unreadable": {"stat", "read"},
        "ui_report_missing": {"stat", "read"},
        "ui_report_too_large": {"stat"},
        "ui_report_invalid": {"parse", "schema"},
    }
    if (type(value.get("code")) is not str or type(value.get("stage")) is not str
            or value["code"] not in combinations or value["stage"] not in combinations[value["code"]]
            or type(value.get("attempts")) is not int or not 1 <= value["attempts"] <= _UI_READ_MAX_ATTEMPTS
            or any(type(value[key]) is not int or not 0 <= value[key] <= 65535
                   for key in ("errno", "winerror") if key in value)
            or value["code"] not in {"ui_file_unreadable", "ui_report_missing"}
               and any(key in value for key in ("errno", "winerror"))):
        return {}
    return dict(value)


class _UiState(tuple):
    """Keep the existing two-value API while retaining safe read diagnostics."""

    def __new__(cls, job_id, reason, *, failure=None):
        value = super().__new__(cls, (job_id, reason))
        value.failure = safe_ui_read_failure(failure) or None
        return value


def _ui_read_diagnostic(code, stage, attempts, error=None):
    diagnostic = {"code": code, "stage": stage, "attempts": attempts}
    if error is not None:
        for key in ("errno", "winerror"):
            number = getattr(error, key, None)
            if type(number) is int and 0 <= number <= 65535:
                diagnostic[key] = number
    return safe_ui_read_failure(diagnostic)


def _ui_state(path):
    """Read fresh ownership, retrying only short Windows file-sharing faults.

    A successful retry must prove the current owner. Malformed data, a missing
    report, a terminal owner and a different job never reuse a previous value.
    """
    path = Path(path)
    for attempt in range(1, _UI_READ_MAX_ATTEMPTS + 1):
        stage = "stat"
        try:
            if path.stat().st_size > 10_000_000:
                return _UiState(None, "ui_unavailable", failure=_ui_read_diagnostic("ui_report_too_large", stage, attempt))
            stage = "read"
            raw = path.read_text(encoding="utf-8")
            stage = "parse"
            value = json.loads(raw)
            stage = "schema"
            status, job_id = value["status"], value["id"]
            if status not in {"queued", "running", "succeeded", "failed", "stopped", "completed_with_failures", "completed_with_pending"} or not isinstance(job_id, str) or not job_id:
                raise ValueError
            return _UiState(job_id, "ui_busy" if status in {"queued", "running"} else None)
        except FileNotFoundError as exc:
            return _UiState(None, None, failure=_ui_read_diagnostic("ui_report_missing", stage, attempt, exc))
        except OSError as exc:
            sharing_fault = os.name == "nt" and (
                isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
            )
            if sharing_fault and stage in {"stat", "read"} and attempt < _UI_READ_MAX_ATTEMPTS:
                sleep(_UI_READ_RETRY_SECONDS)
                continue
            return _UiState(None, "ui_unavailable", failure=_ui_read_diagnostic("ui_file_unreadable", stage, attempt, exc))
        except UnicodeError:
            return _UiState(None, "ui_unavailable", failure=_ui_read_diagnostic("ui_report_invalid", "parse", attempt))
        except (ValueError, KeyError, TypeError):
            return _UiState(None, "ui_unavailable", failure=_ui_read_diagnostic("ui_report_invalid", stage, attempt))


def _owned_ui_state(path, owned_ui_job_id=None, *, progress=None):
    snapshot = _ui_state(path)
    job_id, reason = snapshot
    if progress is not None and snapshot.failure is not None and (reason is not None or owned_ui_job_id is not None):
        progress["ui_read_failure"] = dict(snapshot.failure)
    if owned_ui_job_id is None:
        return snapshot
    if job_id == owned_ui_job_id and reason == "ui_busy":
        return _UiState(job_id, None)
    return _UiState(job_id, reason or "ui_activity", failure=snapshot.failure)


def _failure_code(exc):
    if not isinstance(exc, Exception) or isinstance(exc, EOFError):
        return "interrupted_unknown"
    if getattr(exc, "renewal_unknown", False) is True:
        return "interrupted_unknown"
    if isinstance(exc, SessionReviewRequiredError):
        return "session_review_pending"
    from .preparation import safe_session_store_failure
    local_session_failure = safe_session_store_failure(exc)
    if local_session_failure:
        return "interrupted_unknown" if local_session_failure["renewal_completed"] else "infrastructure_failed"
    if safe_session_storage_failure(exc):
        if getattr(exc, "verified_session_retained", False) is True:
            return "infrastructure_failed"
        return "interrupted_unknown" if getattr(exc, "renewal_completed", False) is True else "infrastructure_failed"
    from .play_record import _Failure
    if isinstance(exc, _Failure) and exc.code == "journal_failed":
        return "interrupted_unknown"
    failure = safe_request_failure(exc)
    if failure:
        if failure["code"] == "session_identity_mismatch":
            return "scope_mismatch"
        if getattr(exc, "renewal_unknown", False) is True or failure["failure_category"] == "unknown":
            return "interrupted_unknown"
        if failure["failure_category"] == "provider":
            return "provider_unavailable"
        return "account_failed"
    if isinstance(exc, SessionError):
        message = str(exc)
        if message in {
            "The country preparation row cannot change.", "The frozen account identity changed.",
            "The frozen row has a different country tag.",
            "Country preparation selects exactly its frozen row.",
            "The selected preparation connection cannot change.",
        }:
            return "scope_mismatch"
        if message == "The login browser could not close cleanly. Check its processes before retrying.":
            return "browser_cleanup_failed"
        if message.startswith((
            "The proxy country check", "The login proxy country check",
            "The browser proxy country check", "The browser proxy country-check",
            "PacketStream proxy authentication was rejected", "PacketStream proxy credentials",
            "The PacketStream proxy configuration",
            "The configured proxy route was not confirmed.",
        )):
            return "proxy_preflight_failed"
        if message.startswith(("CloakBrowser could not start because its license check failed.", "Install requirements-login.txt")):
            return "browser_unavailable"
        if message.startswith("The browser could not complete Anghami sign-in."):
            return "infrastructure_failed"
        return "account_failed"
    return "infrastructure_failed"


class _WorkerStopped(Exception):
    def __init__(self, reason, *, attention=False, ui_read_failure=None):
        self.reason, self.attention = reason, attention
        self.ui_read_failure = safe_ui_read_failure(ui_read_failure) or None


def _mark_skipped(item, state):
    """A held or completed identity is no longer an attempted pending row."""
    item.update(state=state, attempts=0, phase=state, error_code=None,
                login_failure=None, connection=None)
    for field in ("provider_failure", "provider_retry_count", "local_session_failure", "local_finalization_failure", "session_storage_failure", "preparation_review"):
        item.pop(field, None)


def _isolate_preparation_failure(vault, plan, row, exc, *, job_id):
    """Continue peers only after an uncertain account is durably quarantined.

    A known issued candidate may need read-only validation or local commit
    review. A typed renewal with an unknown response may also be held, but is
    never retried here. Scope, journal and candidate-persistence faults remain
    whole-job holds because the coordinator cannot prove safe isolation.
    """
    from .preparation import safe_session_store_failure
    from .play_record import _Failure
    if (not isinstance(exc, Exception) or isinstance(exc, _Failure)
            or safe_session_store_failure(exc)
            or _failure_code(exc) in {"scope_mismatch", "browser_cleanup_failed", "browser_unavailable"}):
        return False
    if isinstance(exc, SessionError) and _failure_code(SessionError(str(exc))) == "scope_mismatch":
        return False
    diagnostic = safe_request_failure(exc)
    if diagnostic and (diagnostic.get("failure_category") == "account"
                       or diagnostic.get("code") == "session_identity_mismatch"):
        return False
    storage_operation = getattr(exc, "operation", None)
    if storage_operation in {"pending_save", "pending_load"}:
        return False
    retained = (getattr(exc, "candidate_retained", False) is True
                and getattr(exc, "renewal_completed", False) is True
                and getattr(exc, "validation_pending", False) is True)
    uncertain_renewal = (getattr(exc, "renewal_unknown", False) is True
                        and diagnostic.get("failure_category") in {"provider", "unknown"}
                        and diagnostic.get("stage") in {"identity", "session_recovery_renewal"})
    if not retained and not uncertain_renewal:
        return False
    record_hold = getattr(vault, "record_preparation_session_review", None)
    read_hold = getattr(vault, "preparation_session_review", None)
    if not callable(record_hold) or not callable(read_hold):
        failure = SessionError("The preparation session review could not be saved securely.")
        failure.renewal_unknown = True
        failure.retry_safe = False
        raise failure from None
    try:
        digest, raw = _source_hash(vault, plan["source_path"])
        del raw
    except SessionError:
        raise _WorkerStopped("source_changed", attention=True) from None
    if digest != plan["source_sha256"]:
        raise _WorkerStopped("source_changed", attention=True)
    identity, _state = _identity_state(vault, row)
    if _binding(identity) != plan["identity_bindings"][str(row)]:
        raise _WorkerStopped("scope_mismatch", attention=True)
    stage = "validation" if retained else "session_recovery_renewal"
    failure_code = "preparation_validation_unknown" if retained else "preparation_outcome_unknown"
    expected = {"code": failure_code, "stage": stage, "candidate_retained": retained}
    try:
        record_hold(row, job_id=job_id, source_sha256=plan["source_sha256"],
                    identity_binding=plan["identity_bindings"][str(row)],
                    failure_code=failure_code, stage=stage, candidate_retained=retained)
        proof = read_hold(row)
        identity, state = _identity_state(vault, row)
        if (not isinstance(proof, dict) or proof.get("source_row") != row
                or proof.get("session_review_pending") is not True
                or proof.get("preparation_review") != expected
                or state != "session_review_pending"
                or _binding(identity) != plan["identity_bindings"][str(row)]):
            raise ValueError
    except Exception:
        failure = SessionError("The preparation session review could not be saved securely.")
        failure.renewal_unknown = True
        failure.retry_safe = False
        raise failure from None
    return expected


def _local_committed_ready(selected):
    """Prove a stored row's binding and explicit enrollment with local reads."""
    row, vault = selected.row, selected._vault
    try:
        record = selected.record(row)
    except SessionReviewRequiredError:
        raise
    except SessionError:
        raise SessionError("The frozen account identity changed.") from None
    del record
    state = vault._db.execute("SELECT state,session IS NOT NULL FROM accounts WHERE source_row=?", (row,)).fetchone()
    if not state or state[0] != "ready" or not state[1]:
        return False
    try:
        saved = selected.session(row)
        del saved
        enrolled = vault._db.execute("SELECT 1 FROM test_accounts WHERE source_row=?", (row,)).fetchone()
    except SessionReviewRequiredError:
        raise
    except (SessionError, sqlite3.Error):
        return False
    try:
        record = selected.record(row)
        del record
    except SessionReviewRequiredError:
        raise
    except SessionError:
        raise SessionError("The frozen account identity changed.") from None
    return bool(enrolled)


def _local_json_evidence(path):
    path = Path(path)
    if path.stat().st_size > 256_000:
        raise ValueError
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError
    return value


def reconcile_finalization_failures(vault, plan, progress_path, *, baseline_attempts):
    """Reconcile reviewed, newly attempted reporting failures using local proof."""
    frozen_rows = {item["source_row"] for item in plan["rows"]}
    if not isinstance(baseline_attempts, dict) or any(
        type(row) is not int or row not in frozen_rows or type(attempts) is not int or attempts not in {0, 1}
        for row, attempts in baseline_attempts.items()
    ):
        raise SessionError("Choose frozen source rows and their original bounded attempt counts for reconciliation.")
    progress_path = Path(progress_path)
    with CountryJobLock(vault.path.parent / "country-preparation.lock"):
        digest, raw = _source_hash(vault, plan["source_path"])
        del raw
        if digest != plan["source_sha256"]:
            raise SessionError("The frozen source import changed.")
        progress = load_progress(progress_path, plan)
        if progress["status"] == "running" or progress["active_row"] is not None or progress["active_rows"]:
            raise SessionError("Finish and review the preparation job before reconciling local finalization failures.")
        _, ui_reason = _ui_state(vault.path.parent / "ui-last-job.json")
        if ui_reason is not None:
            raise SessionError("Finish other local preparation jobs before reconciling local finalization failures.")
        from .client import OPERATIONS
        eligible = [item for item in progress["rows"] if (
            baseline_attempts.get(item["source_row"]) == 0 and item["attempts"] == 1
            and item["state"] == "failed" and item["error_code"] == "account_failed"
            and item.get("login_failure") is None
        )]
        reconciled, unproven = [], []
        for item in eligible:
            row = item["source_row"]
            try:
                report = _local_json_evidence(vault.path.parent / "country-preparation-reports" / progress["run_id"] / f"account-{row}.redacted.json")
                metadata = _local_json_evidence(vault.path.parent / f"account-{row}.session-recovery.redacted.json")
                if (
                    report.get("selected_rows") != [row] or report.get("prepared_rows") != [row]
                    or any(type(value) is not int for value in report["selected_rows"] + report["prepared_rows"])
                    or type(report.get("prepared_account_count")) is not int or report["prepared_account_count"] != 1
                    or type(report.get("attempted_accounts")) is not int or report["attempted_accounts"] != 1
                    or report.get("passed") is not False or report.get("phase") != "stopped"
                    or report.get("failed_phase") not in {"validation", "complete"}
                    or type(report.get("failed_row")) is not int or report["failed_row"] != row
                    or type(report.get("active_row")) is not int or report["active_row"] != row
                    or report.get("error_code") not in {"preparation_failed", "journal_failed"}
                    or report.get("no_browser") is not True or report.get("dry_run") is not False
                    or report.get("preparation_method") != "http" or report.get("browser") != "none"
                    or report.get("login_failure") is not None
                    or type(metadata.get("source_row")) is not int or metadata["source_row"] != row
                    or any(metadata.get(name) is not True for name in ("authenticated", "identity_verified", "session_renewed"))
                    or not isinstance(metadata.get("operations"), dict)
                    or any(metadata["operations"].get(name) != "ok" for name in OPERATIONS)
                ):
                    raise ValueError
                report_time = datetime.fromisoformat(report["checked_at_utc"])
                proof_time = datetime.fromisoformat(metadata["checked_at_utc"])
                if report_time.tzinfo is None or proof_time.tzinfo is None or proof_time < report_time:
                    raise ValueError
                selected = SelectedRowVault(vault, row, country=plan["country"], identity_binding=plan["identity_bindings"][str(row)])
                if not _local_committed_ready(selected):
                    raise ValueError
            except (SessionError, OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                unproven.append(row)
                continue
            item["local_finalization_failure"] = _stored_finalization_failure({
                "code": "postcommit_reporting_failed", "failed_phase": report["failed_phase"],
                "error_kind": "report_error", "prior_error_code": item["error_code"],
                "report_error_code": report["error_code"], "reconciled": "post_run_committed",
            })
            item.update(state="ready", phase="complete", error_code=None)
            reconciled.append(row)
        if reconciled:
            # The OS lock and final source check cover the entire reviewed batch.
            # Preserve counters and held thresholds exactly; this sends no requests.
            digest, raw = _source_hash(vault, plan["source_path"])
            del raw
            if digest != plan["source_sha256"]:
                raise SessionError("The frozen source import changed.")
            for row in reconciled:
                selected = SelectedRowVault(vault, row, country=plan["country"], identity_binding=plan["identity_bindings"][str(row)])
                if not _local_committed_ready(selected):
                    raise SessionError("The committed local preparation proof changed.")
            progress["updated_at_utc"] = _now()
            _atomic_json(progress_path, progress)
        return {"examined": len(eligible), "reconciled_rows": reconciled, "unproven_rows": unproven,
                "account_requests_sent": 0, "summary": summarize(progress)}


def _http_worker(vault_path, plan, row, *, prepare, proxy_egypt, report_path, events, stopping, draining, ui_path, baseline_ui, stop_path, selected_proxy=None, drain_on_failure=True, retry_pool=None, pool_index=0, owned_ui_job_id=None):
    """Retry only classified prewrite connection failures, never account writes."""
    profile, route_offset, attempts = selected_proxy, 0, 0
    used_routes = set()
    while True:
        attempts += 1
        result = _http_worker_once(
            vault_path, plan, row, prepare=prepare, proxy_egypt=proxy_egypt, report_path=report_path,
            events=events, stopping=stopping, draining=draining, ui_path=ui_path,
            baseline_ui=baseline_ui, stop_path=stop_path, selected_proxy=profile,
            drain_on_failure=drain_on_failure, owned_ui_job_id=owned_ui_job_id,
        )
        result["provider_retry_count"] = attempts - 1
        if result.get("kind") != "connection_pending":
            return result
        diagnostic = result.get("provider_failure") or {}
        if diagnostic.get("retryable") is not True or attempts >= 3 or stopping.is_set() or stop_path.exists():
            return result
        if diagnostic.get("http_status") == 429 or diagnostic.get("code") == "request_rate_limited":
            events.put((row, "provider_wait"))
            if not wait_for_provider(diagnostic, stop=stopping):
                return result
            # The same exact route remains selected after its quota cooldown.
            continue
        if diagnostic.get("rotate_route") is not True or retry_pool is None:
            return result
        events.put((row, "provider_retry"))
        used_routes.add(pool_index % len(retry_pool))
        route_offset += 1
        position = (pool_index + route_offset) % len(retry_pool)
        if position in used_routes:
            return result
        used_routes.add(position)
        profile = retry_pool.proxy_for_index(pool_index + route_offset)


def _http_worker_once(vault_path, plan, row, *, prepare, proxy_egypt, report_path, events, stopping, draining, ui_path, baseline_ui, stop_path, selected_proxy=None, drain_on_failure=True, owned_ui_job_id=None):
    """Keep the SQLite connection, saved credentials and HTTP transports local."""
    proxy_stage = None
    started_preparation = False
    try:
        with AccountVault(vault_path) as worker_vault:
            def gate():
                if stopping.is_set() or stop_path.exists():
                    raise _WorkerStopped("stop_requested")
                ui_state = _owned_ui_state(ui_path, owned_ui_job_id)
                job_id, reason = ui_state
                if stopping.is_set() or stop_path.exists():
                    raise _WorkerStopped("stop_requested")
                if reason is not None or job_id != baseline_ui:
                    raise _WorkerStopped(reason or "ui_activity", ui_read_failure=ui_state.failure)
                try:
                    digest, raw = _source_hash(worker_vault, plan["source_path"])
                    del raw
                except SessionError:
                    raise _WorkerStopped("source_changed", attention=True) from None
                if digest != plan["source_sha256"]:
                    raise _WorkerStopped("source_changed", attention=True)
                identity, state = _identity_state(worker_vault, row)
                if _binding(identity) != plan["identity_bindings"][str(row)]:
                    raise _WorkerStopped("scope_mismatch", attention=True)
                return state

            state = gate()
            if state != "pending":
                return {"kind": "skipped", "state": state}
            proxy = selected_proxy
            if proxy_egypt:
                events.put((row, "proxy_preflight"))
                proxy_stage = "profile_load"
                if proxy is None:
                    proxy = load_packetstream_proxy(worker_vault.path.parent / "packetstream.dpapi")
                proxy_stage = "profile_country"
                expected_country = getattr(selected_proxy, "country", "EG")
                if type(expected_country) is not str or expected_country not in {"EG", "US"} or proxy.country != expected_country:
                    message = "The selected sticky proxy route country is invalid." if selected_proxy is not None else "The saved proxy route must select Egypt."
                    raise SessionError(message)
                proxy_stage = "country_check"
                verification = proxy.verify_country()
                if (
                    not isinstance(verification, dict) or verification.get("country") != expected_country
                    or verification.get("country_verified") is not True or verification.get("proxy_used") is not True
                ):
                    raise SessionError("The selected proxy route was not verified.")
            state = gate()
            if state != "pending":
                return {"kind": "skipped", "state": state}
            selected = SelectedRowVault(
                worker_vault, row, country=plan["country"],
                identity_binding=plan["identity_bindings"][str(row)], proxy=proxy,
            )
            record = selected.record(row)  # Prove the exact country before any account request.
            del record
            gate()
            proxy_stage = "preparation" if proxy_egypt else None
            started_preparation = True
            def account_progress(report):
                phase = report.get("phase") if isinstance(report, dict) else None
                if phase in _PHASES:
                    events.put((row, phase))
            from .preparation import PreparationFinalizationError, safe_finalization_failure
            try:
                result = prepare(
                    selected, count=1, start_row=row, proxy=proxy, browser_backend="cloakbrowser",
                    headless=False, reduce_browser_data=False, no_browser=True,
                    report_path=report_path, progress=account_progress,
                )
            except PreparationFinalizationError as exc:
                evidence = safe_finalization_failure(exc)
                if not evidence or exc.row != row or not selected._attach_acknowledged or not selected._enrollment_acknowledged:
                    raise
                try:
                    digest, raw = _source_hash(worker_vault, plan["source_path"])
                except SessionError:
                    draining.set()
                    return {"kind": "failed", "code": "scope_mismatch", "login_failure": None, "proxy_failure": None}
                del raw
                if digest != plan["source_sha256"]:
                    draining.set()
                    return {"kind": "failed", "code": "scope_mismatch", "login_failure": None, "proxy_failure": None}
                if not _local_committed_ready(selected):
                    raise
                return {"kind": "ready", "local_finalization_failure": {
                    **evidence, "prior_error_code": "account_failed", "reconciled": "worker_committed",
                }}
            except Exception as exc:
                review = _isolate_preparation_failure(
                        worker_vault, plan, row, exc,
                        job_id=owned_ui_job_id or Path(report_path).parent.name)
                if review:
                    return {"kind": "held", "state": "session_review_pending", "preparation_review": review,
                            "session_storage_failure": safe_session_storage_failure(exc) or None}
                raise
            if isinstance(result, dict) and result.get("selected_rows") == [row]:
                if result.get("connection_pending_rows") == [row] and not result.get("prepared_rows"):
                    diagnostic = provider_failure(result.get("provider_failure"))
                    if diagnostic:
                        return {"kind": "connection_pending", "code": "provider_unavailable", "provider_failure": diagnostic}
                if result.get("account_failed_rows") == [row] and not result.get("prepared_rows"):
                    return {"kind": "failed", "code": "account_failed", "login_failure": None, "proxy_failure": None}
            if (
                not isinstance(result, dict) or result.get("passed") is not True
                or result.get("selected_rows") != [row] or result.get("prepared_rows") != [row]
                or result.get("prepared_account_count") != 1
            ):
                draining.set()
                return {"kind": "failed", "code": "scope_mismatch", "login_failure": None, "proxy_failure": None}
            selected._check(row)
            return {"kind": "ready"}
    except _WorkerStopped as exc:
        draining.set()
        return {"kind": "aborted", "reason": exc.reason, "attention": exc.attention,
                "ui_read_failure": exc.ui_read_failure}
    except BaseException as exc:
        code = _failure_code(exc)
        if code == "session_review_pending":
            return {"kind": "skipped", "state": "session_review_pending"}
        from .preparation import safe_session_store_failure
        local_session_failure = safe_session_store_failure(exc)
        local_storage_failure = safe_session_storage_failure(exc)
        diagnostic = provider_failure(exc)
        if diagnostic and code != "interrupted_unknown":
            return {"kind": "connection_pending", "code": "provider_unavailable", "provider_failure": diagnostic,
                    "proxy_failure": _safe_proxy_failure(exc, row, proxy_stage) if isinstance(exc, ProxyCountryError) else None}
        if code not in {"interrupted_unknown", "scope_mismatch"} and proxy_stage in {"profile_load", "profile_country", "country_check"}:
            code = "proxy_preflight_failed"
        if local_session_failure or local_storage_failure or drain_on_failure or code in {"interrupted_unknown", "scope_mismatch", "browser_cleanup_failed", "browser_unavailable"} or proxy_stage in {"profile_load", "profile_country"}:
            draining.set()
        return {
            "kind": "failed", "code": code, "login_failure": safe_login_failure(exc) or None,
            "proxy_failure": _safe_proxy_failure(exc, row, proxy_stage) if code == "proxy_preflight_failed" else None,
            "preparation_started": started_preparation,
            "local_session_failure": local_session_failure or None,
            "session_storage_failure": local_storage_failure or None,
        }


def _run_parallel_http(vault, plan, progress, *, checkpoint, pause, prepare, limit, workers, proxy_egypt, ui_path, baseline_ui, stop_path, resume_after_review, sticky_pool=None, owned_ui_job_id=None):
    """Only the coordinator writes the country checkpoint and schedules work."""
    events, stopping, draining = Queue(), Event(), Event()
    pending = iter(item for item in progress["rows"] if item["state"] in {"pending", "connection_pending"})
    dispatched_rows = set()
    active, attempted, exhausted = {}, 0, False
    pause_reason, attention, drain_batch = None, False, False
    phase_dirty, last_phase_checkpoint = False, monotonic()
    maximum = progress.get("max_consecutive_failures", 3)
    custom_failure_budget = maximum != 3
    acknowledged_unknown = {
        item["source_row"] for item in progress["rows"]
        if item["state"] == "unknown" and progress["unknown_acknowledged"]
    }

    def active_snapshot():
        progress["active_rows"] = sorted(item["source_row"] for item in active.values())
        progress["active_row"] = progress["active_rows"][0] if progress["active_rows"] else None

    def stop(reason, *, needs_attention=False):
        nonlocal pause_reason, attention
        if pause_reason is None:
            pause_reason, attention = reason, needs_attention
        stopping.set()
        progress.update(status="attention_required" if attention else "paused", pause_reason=pause_reason)
        checkpoint()

    def coordinator_gate():
        if stop_path.exists():
            stop("stop_requested")
            return False
        job_id, reason = _owned_ui_state(ui_path, owned_ui_job_id, progress=progress)
        if stop_path.exists():
            stop("stop_requested")
            return False
        if reason is not None or job_id != baseline_ui:
            stop(reason or "ui_activity")
            return False
        try:
            digest, raw = _source_hash(vault, plan["source_path"])
            del raw
        except SessionError:
            stop("source_changed", needs_attention=True)
            return False
        if digest != plan["source_sha256"]:
            stop("source_changed", needs_attention=True)
            return False
        return True

    def consume(future, item):
        nonlocal drain_batch
        row = item["source_row"]
        try:
            result = future.result()
        except BaseException:
            result = {"kind": "failed", "code": "interrupted_unknown", "login_failure": None, "proxy_failure": None}
        kind = result.get("kind")
        if kind == "ready":
            identity, state = _identity_state(vault, row)
            if _binding(identity) != plan["identity_bindings"][str(row)]:
                result = {"kind": "failed", "code": "scope_mismatch"}
                kind = "failed"
            elif state == "session_review_pending":
                result = {"kind": "skipped", "state": state}
                kind = "skipped"
        if kind == "ready":
            item.update(state="ready", phase="complete", error_code=None, login_failure=None)
            if result.get("local_finalization_failure") is not None:
                item["local_finalization_failure"] = _stored_finalization_failure(result["local_finalization_failure"])
            # A threshold reached by another finished worker stays held while draining.
            if _failure_hold(progress) is None or resume_after_review:
                progress.update(infrastructure_failures=0, consecutive_failures=0, failure_hold=None)
        elif kind == "held":
            review = _stored_preparation_review(result.get("preparation_review"))
            if review is None:
                raise SessionError("The preparation account review proof is invalid.")
            _identity, state = _identity_state(vault, row)
            if state != "session_review_pending" or _binding(_identity) != plan["identity_bindings"][str(row)]:
                stop("scope_mismatch", needs_attention=True)
                return
            try:
                proof = vault.preparation_session_review(row)
                if (not isinstance(proof, dict) or proof.get("source_row") != row
                        or proof.get("session_review_pending") is not True
                        or proof.get("preparation_review") != review):
                    raise ValueError
            except Exception:
                item.update(state="unknown", phase="stopped", error_code="interrupted_unknown")
                progress["unknown_acknowledged"] = False
                stop("unknown_attempt", needs_attention=True)
                return
            item.update(state="session_review_pending", attempts=1, phase="session_review_pending",
                        error_code=None, login_failure=None, preparation_review=review)
            item.pop("provider_failure", None)
            local_storage = _stored_local_storage_failure(result.get("session_storage_failure"))
            if local_storage is not None:
                item["session_storage_failure"] = local_storage
        elif kind in {"skipped", "aborted"}:
            state = result["state"] if kind == "skipped" else "pending"
            _mark_skipped(item, state)
            if kind == "aborted":
                ui_read_failure = safe_ui_read_failure(result.get("ui_read_failure"))
                if ui_read_failure:
                    progress["ui_read_failure"] = ui_read_failure
                stop(result["reason"], needs_attention=result["attention"])
        elif kind == "connection_pending":
            item.update(state="connection_pending", phase="connection_pending", error_code="provider_unavailable",
                        login_failure=None, provider_failure=result["provider_failure"],
                        provider_retry_count=result.get("provider_retry_count", 0))
            if result.get("proxy_failure") is not None:
                progress["proxy_failure"] = result["proxy_failure"]
        else:
            code = result.get("code", "interrupted_unknown")
            unknown = code in {"interrupted_unknown", "scope_mismatch"}
            item.update(state="unknown" if unknown else "failed", phase="stopped", error_code=code, login_failure=result.get("login_failure"))
            local_session_failure = _stored_session_store_failure(result.get("local_session_failure"))
            if local_session_failure is not None:
                item["local_session_failure"] = local_session_failure
            local_storage_failure = _stored_local_storage_failure(result.get("session_storage_failure"))
            if local_storage_failure is not None:
                item["session_storage_failure"] = local_storage_failure
            if result.get("proxy_failure") is not None:
                progress["proxy_failure"] = result["proxy_failure"]
            if unknown:
                progress["unknown_acknowledged"] = False
                stop("scope_mismatch" if code == "scope_mismatch" else "unknown_attempt", needs_attention=True)
            else:
                if code != "proxy_preflight_failed":
                    progress["infrastructure_failures"] = min(maximum, progress["infrastructure_failures"] + 1) if code == "infrastructure_failed" else 0
                progress["consecutive_failures"] = min(maximum, progress["consecutive_failures"] + 1)
                drain_batch = not custom_failure_budget
                proxy_configuration_failure = result.get("proxy_failure", {}).get("stage") in {"profile_load", "profile_country"} if isinstance(result.get("proxy_failure"), dict) else False
                if local_session_failure is not None or local_storage_failure is not None:
                    stop("infrastructure_failed")
                elif code in {"browser_cleanup_failed", "browser_unavailable"} or proxy_configuration_failure or (code == "proxy_preflight_failed" and not custom_failure_budget):
                    stop(code)
                elif not custom_failure_budget and progress["infrastructure_failures"] >= maximum:
                    stop("infrastructure_failed")
                elif resume_after_review or progress["consecutive_failures"] >= maximum:
                    stop("infrastructure_failed" if resume_after_review and not custom_failure_budget and code == "infrastructure_failed" else "repeated_failures")
        checkpoint()

    # Tasks never wait on other tasks and only this thread touches `vault`.
    # Every HTTP call already has a timeout; shutdown waits for in-flight work.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="country-http") as pool:
        while active or (not exhausted and pause_reason is None):
            try:
                if pause_reason is None and not coordinator_gate():
                    continue
                while pause_reason is None and not drain_batch and not draining.is_set() and len(active) < workers and not exhausted:
                    # A peer can finish while another result is checkpointed.
                    # Count every finished outcome before replacing its slot.
                    if any(future.done() for future in active):
                        break
                    if (custom_failure_budget and workers <= maximum and not resume_after_review
                            and len(active) >= maximum - progress["consecutive_failures"]):
                        # Every active task could fail. Reserve the remaining
                        # budget when concurrency fits within the failure limit.
                        # Larger requested pools stop new dispatch at the
                        # observed limit and drain already-submitted accounts.
                        break
                    if limit is not None and attempted >= limit:
                        if not active:
                            if any(entry["state"] in {"pending", "connection_pending"} for entry in progress["rows"]):
                                stop("limit_reached")
                            else:
                                exhausted = True
                        break
                    try:
                        item = next(pending)
                    except StopIteration:
                        exhausted = True
                        break
                    row = item["source_row"]
                    identity, state = _identity_state(vault, row)
                    if _binding(identity) != plan["identity_bindings"][str(row)]:
                        stop("scope_mismatch", needs_attention=True)
                        break
                    if state != "pending":
                        _mark_skipped(item, state)
                        checkpoint()
                        continue
                    if not coordinator_gate():
                        break
                    prior_item = dict(item)
                    selected_proxy = None
                    pool_index = progress["proxy_pool_cursor"]
                    if sticky_pool is not None:
                        selected_proxy = sticky_pool.proxy_for_index(progress["proxy_pool_cursor"])
                        if selected_proxy is None or getattr(selected_proxy, "country", None) != sticky_pool.summary()["country"]:
                            raise SessionError("The selected sticky proxy pool route is invalid.")
                        progress["proxy_pool_cursor"] += 1
                    item.update(state="in_progress", attempts=1, phase="preparing", error_code=None, login_failure=None, connection=progress["connection"])
                    item.pop("provider_failure", None)
                    progress["active_rows"] = sorted([*(entry["source_row"] for entry in active.values()), row])
                    progress["active_row"] = progress["active_rows"][0]
                    checkpoint()  # Durable dispatch intent before credentials and proxy requests.
                    phase_dirty, last_phase_checkpoint = False, monotonic()
                    # Writing a large checkpoint can take longer than a peer's
                    # failed request. Do not submit a replacement after that failure.
                    if draining.is_set() or stopping.is_set() or any(future.done() for future in active):
                        item.clear()
                        item.update(prior_item)
                        if sticky_pool is not None:
                            progress["proxy_pool_cursor"] -= 1
                        active_snapshot()
                        checkpoint()
                        pending = iter(entry for entry in progress["rows"]
                                       if entry["state"] in {"pending", "connection_pending"}
                                       and entry["source_row"] not in dispatched_rows)
                        break
                    attempted += 1
                    dispatched_rows.add(row)
                    report_path = vault.path.parent / "country-preparation-reports" / progress["run_id"] / f"account-{row}.redacted.json"
                    future = pool.submit(
                        _http_worker, vault.path, plan, row, prepare=prepare, proxy_egypt=proxy_egypt,
                        report_path=report_path, events=events, stopping=stopping, draining=draining, ui_path=ui_path,
                        baseline_ui=baseline_ui, stop_path=stop_path,
                        selected_proxy=selected_proxy,
                        retry_pool=sticky_pool, pool_index=pool_index,
                        drain_on_failure=not custom_failure_budget,
                        owned_ui_job_id=owned_ui_job_id,
                    )
                    active[future] = item
                while True:
                    try:
                        row, phase = events.get_nowait()
                    except Empty:
                        break
                    for item in active.values():
                        if item["source_row"] == row and item["state"] == "in_progress" and item["phase"] != phase:
                            item["phase"] = phase
                            phase_dirty = True
                            break
                if phase_dirty and monotonic() - last_phase_checkpoint >= 1:
                    checkpoint()
                    phase_dirty, last_phase_checkpoint = False, monotonic()
                if active:
                    done, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                    # Handle all already-completed outcomes before dispatching replacements.
                    for future in list(active):
                        if future in done or future.done():
                            item = active.pop(future)
                            active_snapshot()
                            consume(future, item)
                            phase_dirty, last_phase_checkpoint = False, monotonic()
                else:
                    drain_batch = False
                    draining.clear()
                    if limit is not None and attempted >= limit and not exhausted and pause_reason is None:
                        if any(entry["state"] in {"pending", "connection_pending"} for entry in progress["rows"]):
                            stop("limit_reached")
                        else:
                            exhausted = True
            except (KeyboardInterrupt, EOFError):
                # A live row remains quarantined until its worker proves a safe outcome.
                for item in active.values():
                    item.update(state="unknown", phase="stopped", error_code="interrupted_unknown")
                progress["unknown_acknowledged"] = False
                stop("stop_requested")
            except BaseException:
                # A failed progress sink cannot leave workers dispatching reads
                # while the coordinator unwinds and its durable rows stay held.
                stopping.set()
                draining.set()
                raise
    active_snapshot()
    unknown_rows = {item["source_row"] for item in progress["rows"] if item["state"] == "unknown"}
    if unknown_rows and unknown_rows <= acknowledged_unknown:
        # An interrupted active worker may have proved completion while draining.
        # Preserve the explicit review of historical quarantined rows in that case.
        progress["unknown_acknowledged"] = True
    if unknown_rows and not progress["unknown_acknowledged"]:
        return pause("scope_mismatch" if pause_reason == "scope_mismatch" else "unknown_attempt", attention=True)
    if pause_reason is not None:
        return pause(pause_reason, attention=attention)
    progress.update(status="completed_with_pending" if any(item["state"] == "connection_pending" for item in progress["rows"]) else "completed",
                    pause_reason=None, active_row=None, active_rows=[])
    checkpoint()
    return summarize(progress)


def run_plan(vault, plan, progress_path, *, limit=None, prepare=None, ui_path=None, stop_path=None, fresh_plan=False, resume_after_review=False, reduce_browser_data=True, proxy_egypt=False, no_browser=False, workers=1, proxy_sticky_pool=None, max_consecutive_failures=3, replace_proxy_pool=False, owned_ui_job_id=None, progress_callback=None):
    """Run each frozen pending row at most once, using normal count-one preparation."""
    if limit is not None and (type(limit) is not int or limit < 1):
        raise SessionError("The optional run limit must be a positive integer.")
    if owned_ui_job_id is not None and (type(owned_ui_job_id) is not str or re.fullmatch(r"[0-9a-f]{32}", owned_ui_job_id) is None):
        raise SessionError("The owning UI preparation job identifier is invalid.")
    if progress_callback is not None and not callable(progress_callback):
        raise SessionError("The preparation progress callback must be callable.")
    if type(resume_after_review) is not bool or (resume_after_review and limit != 1):
        raise SessionError("Resuming a failure pause requires an explicit review and a one-account limit.")
    if type(reduce_browser_data) is not bool:
        raise SessionError("The browser data selection must be true or false.")
    if type(proxy_egypt) is not bool:
        raise SessionError("The proxy selection must be true or false.")
    if type(no_browser) is not bool:
        raise SessionError("The browser-free selection must be true or false.")
    if type(workers) is not int or not 1 <= workers <= MAX_WORKER_REQUEST:
        raise SessionError("The requested preparation worker count must be a positive JavaScript-safe integer.")
    if workers > 1 and not no_browser:
        raise SessionError("Multiple workers require browser-free preparation.")
    if type(max_consecutive_failures) is not int or not 1 <= max_consecutive_failures <= MAX_FAILURE_BUDGET:
        raise SessionError(f"Choose 1-{MAX_FAILURE_BUDGET} consecutive preparation failures.")
    if max_consecutive_failures != 3 and not no_browser:
        raise SessionError("A custom failure budget requires browser-free preparation.")
    if proxy_sticky_pool is not None and not no_browser:
        raise SessionError("The ordered sticky proxy pool requires browser-free preparation.")
    if type(replace_proxy_pool) is not bool:
        raise SessionError("The sticky proxy pool replacement selection must be true or false.")
    if replace_proxy_pool and (not no_browser or proxy_sticky_pool is None or fresh_plan):
        raise SessionError("Replacing the sticky proxy pool requires browser-free preparation with a selected pool and the existing plan.")
    if proxy_sticky_pool is not None:
        proxy_egypt = True
    if no_browser:
        reduce_browser_data = False
    progress_path = Path(progress_path)
    ui_path = vault.path.parent / "ui-last-job.json" if ui_path is None else Path(ui_path)
    stop_path = progress_path.with_suffix(".stop") if stop_path is None else Path(stop_path)
    if prepare is None:
        from .preparation import prepare_test_accounts
        prepare = prepare_test_accounts
    runtime = nullcontext() if proxy_egypt else direct_runtime(browser=False) if no_browser else direct_runtime()
    with CountryJobLock(vault.path.parent / "country-preparation.lock"), runtime:
        # An immediate import mismatch must leave the existing checkpoint intact.
        digest, raw = _source_hash(vault, plan["source_path"])
        del raw
        if digest != plan["source_sha256"]:
            raise SessionError("The frozen source import changed.")
        progress = load_progress(progress_path, plan)
        sticky_pool = StickyProxyPool.load(proxy_sticky_pool) if proxy_sticky_pool is not None else None
        if sticky_pool is not None:
            pool_count, fingerprint = len(sticky_pool), sticky_pool.fingerprint()
            endpoint = sticky_pool.summary().get("endpoint")
            pool_country = sticky_pool.summary().get("country")
            if (
                type(pool_count) is not int or not 1 <= pool_count <= 100_000
                or type(fingerprint) is not str or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            ):
                raise SessionError("The selected sticky proxy pool does not match the saved preparation checkpoint.")
            if type(endpoint) is not str or endpoint not in {"http://proxy.packetstream.io:31112", "https://proxy.packetstream.io:31111", "mixed"}:
                raise SessionError("The selected sticky proxy pool endpoint is invalid.")
            changed_pool = bool(progress["proxy_pool_count"] and progress["proxy_pool_fingerprint"] != fingerprint)
            if changed_pool and not replace_proxy_pool:
                raise SessionError("The selected sticky proxy pool does not match the saved preparation checkpoint.")
            if progress["proxy_pool_count"] and not changed_pool and progress["proxy_pool_count"] != pool_count:
                raise SessionError("The selected sticky proxy pool does not match the saved preparation checkpoint.")
            if changed_pool:
                # The explicit replacement changes only the validated route binding.
                # Preserve every row and hold, including a pause that prevents work.
                progress.update(proxy_pool_count=pool_count, proxy_pool_fingerprint=fingerprint,
                                proxy_pool_cursor=0, proxy_pool_endpoint=endpoint,
                                proxy_pool_country=pool_country, updated_at_utc=_now())
                _atomic_json(progress_path, progress)
        if _migrate_provider_rows(progress):
            progress["updated_at_utc"] = _now()
            _atomic_json(progress_path, progress)
        hold = _failure_hold(progress)
        if hold is not None and not resume_after_review:
            # Re-running the same command cannot consume another account after a
            # failure pause. This also catches a crash just before saving the pause.
            if progress["status"] != "paused" or progress["pause_reason"] != hold or progress["active_row"] is not None or progress["active_rows"]:
                progress.update(status="paused", pause_reason=hold, active_row=None, active_rows=[], updated_at_utc=_now())
                _atomic_json(progress_path, progress)
            return summarize(progress)
        if fresh_plan:
            replacement = _new_progress(plan)
            old_rows = {row["source_row"]: row for row in progress["rows"]}
            for row in replacement["rows"]:
                previous = old_rows[row["source_row"]]
                if previous["state"] in {"failed", "unknown", "in_progress", "connection_pending", "session_review_pending"}:
                    row.update(previous)
            replacement["unknown_acknowledged"] = True
            replacement["infrastructure_failures"] = progress["infrastructure_failures"]
            replacement["consecutive_failures"] = progress["consecutive_failures"]
            replacement["failure_hold"] = hold
            replacement["proxy_failure"] = progress["proxy_failure"]
            for name in ("max_consecutive_failures", "proxy_pool_active", "proxy_pool_count", "proxy_pool_fingerprint", "proxy_pool_cursor", "proxy_pool_endpoint", "proxy_pool_country"):
                replacement[name] = progress[name]
            if progress_path.exists():
                _atomic_json(progress_path.with_name(progress_path.stem + ".previous-" + progress["run_id"] + ".json"), progress)
            progress = replacement
        # The OS lock proves no prior job remains active. Its durable in_progress
        # rows are reconciled below, rather than displayed as running threads.
        progress.update(active_row=None, active_rows=[])
        progress["reduce_browser_data"] = reduce_browser_data
        progress["no_browser"] = no_browser
        progress["workers"] = workers
        progress["requested_workers"] = workers
        progress["effective_workers"] = _effective_workers(progress, workers, limit=limit)
        progress["max_consecutive_failures"] = max_consecutive_failures
        progress["consecutive_failures"] = min(max_consecutive_failures, progress["consecutive_failures"])
        progress["infrastructure_failures"] = min(max_consecutive_failures, progress["infrastructure_failures"])
        progress["proxy_pool_active"] = sticky_pool is not None
        if sticky_pool is not None:
            progress["proxy_pool_count"], progress["proxy_pool_fingerprint"] = pool_count, fingerprint
            progress["proxy_pool_endpoint"] = endpoint
            progress["proxy_pool_country"] = pool_country
        progress["connection"] = "proxy_egypt" if proxy_egypt else "direct"
        if not proxy_egypt:
            progress["proxy_failure"] = None
        def checkpoint():
            if workers == 1:
                progress["active_rows"] = [] if progress["active_row"] is None else [progress["active_row"]]
            progress["updated_at_utc"] = _now()
            progress["failure_hold"] = _failure_hold(progress)
            _atomic_json(progress_path, progress)
            if progress_callback is not None:
                try:
                    progress_callback(summarize(progress))
                except Exception:
                    raise SessionError("The preparation progress could not be reported.") from None
        def pause(reason, *, attention=False):
            progress.update(status="attention_required" if attention else "paused", pause_reason=reason, active_row=None, active_rows=[])
            checkpoint()
            return summarize(progress)
        updated_hold = _failure_hold(progress)
        if updated_hold is not None and not resume_after_review:
            return pause(updated_hold)
        baseline_ui, reason = _owned_ui_state(ui_path, owned_ui_job_id, progress=progress)
        if reason is not None:
            return pause(reason)
        for item in progress["rows"]:
            if item["state"] != "in_progress":
                continue
            row = item["source_row"]
            try:
                selected = SelectedRowVault(vault, row, country=plan["country"], identity_binding=plan["identity_bindings"][str(row)])
            except SessionReviewRequiredError:
                _mark_skipped(item, "session_review_pending")
                checkpoint()
                continue
            state = vault._db.execute("SELECT state,session IS NOT NULL FROM accounts WHERE source_row=?", (row,)).fetchone()
            if state and state[0] == "ready" and state[1]:
                try:
                    saved = selected.session(row)
                    del saved
                    selected.enable_test_account(row)
                except Exception:
                    item.update(state="unknown", phase="stopped", error_code="interrupted_unknown")
                    progress["unknown_acknowledged"] = False
                else:
                    item.update(state="ready", phase="complete", error_code=None)
                    if _failure_hold(progress) is None:
                        progress.update(infrastructure_failures=0, consecutive_failures=0, failure_hold=None)
            else:
                item.update(state="unknown", phase="stopped", error_code="interrupted_unknown")
                progress["unknown_acknowledged"] = False
            checkpoint()
        if any(row["state"] == "unknown" for row in progress["rows"]) and not progress["unknown_acknowledged"]:
            return pause("unknown_attempt", attention=True)
        effective_workers = _effective_workers(progress, workers, limit=limit)
        progress["effective_workers"] = effective_workers
        if owned_ui_job_id is not None and stop_path.exists():
            # A unique UI job's stop file may arrive during the ownership read.
            # It is a current cancellation, not the previous CLI resume marker.
            return pause("stop_requested")
        if owned_ui_job_id is None:
            stop_path.unlink(missing_ok=True)  # An explicit CLI --run resumes a stopped job.
        progress.update(status="running", pause_reason=None, active_row=None, active_rows=[])
        checkpoint()
        if effective_workers == 0:
            progress.update(status="completed", pause_reason=None)
            checkpoint()
            return summarize(progress)
        if workers > 1 or sticky_pool is not None or max_consecutive_failures != 3:
            return _run_parallel_http(
                vault, plan, progress, checkpoint=checkpoint, pause=pause, prepare=prepare, limit=limit,
                workers=effective_workers, proxy_egypt=proxy_egypt, ui_path=ui_path, baseline_ui=baseline_ui,
                stop_path=stop_path, resume_after_review=resume_after_review,
                sticky_pool=sticky_pool,
                owned_ui_job_id=owned_ui_job_id,
            )
        attempted = 0
        for item in progress["rows"]:
            if item["state"] not in {"pending", "connection_pending"}:
                continue
            if stop_path.exists():
                return pause("stop_requested")
            if limit is not None and attempted >= limit:
                return pause("limit_reached")
            job_id, reason = _owned_ui_state(ui_path, owned_ui_job_id, progress=progress)
            if stop_path.exists():
                return pause("stop_requested")
            if reason is not None or job_id != baseline_ui:
                return pause(reason or "ui_activity")
            try:
                digest, raw = _source_hash(vault, plan["source_path"])
            except SessionError:
                return pause("source_changed", attention=True)
            del raw
            if digest != plan["source_sha256"]:
                return pause("source_changed", attention=True)
            row = item["source_row"]
            identity, state = _identity_state(vault, row)
            if _binding(identity) != plan["identity_bindings"][str(row)]:
                return pause("scope_mismatch", attention=True)
            if state != "pending":
                _mark_skipped(item, state)
                checkpoint()
                continue
            proxy = None
            if proxy_egypt:
                progress["proxy_failure"] = None
                proxy_stage = "profile_load"
                try:
                    proxy = load_packetstream_proxy(vault.path.parent / "packetstream.dpapi")
                    proxy_stage = "profile_country"
                    expected_country = getattr(proxy, "country", None)
                    if type(expected_country) is not str or expected_country != "EG":
                        raise SessionError("The saved proxy route must select Egypt.")
                    proxy_stage = "country_check"
                    verification = proxy.verify_country()
                    if (
                        not isinstance(verification, dict) or verification.get("country") != expected_country
                        or verification.get("country_verified") is not True or verification.get("proxy_used") is not True
                    ):
                        raise SessionError("The selected proxy route was not verified.")
                except (KeyboardInterrupt, EOFError):
                    return pause("stop_requested")
                except Exception as exc:
                    diagnostic = provider_failure(exc)
                    if diagnostic:
                        item.update(state="connection_pending", attempts=1, phase="connection_pending",
                                    error_code="provider_unavailable", login_failure=None,
                                    connection=progress["connection"], provider_failure=diagnostic)
                        progress["proxy_failure"] = _safe_proxy_failure(exc, row, proxy_stage)
                        attempted += 1
                        checkpoint()
                        continue
                    progress["proxy_failure"] = _safe_proxy_failure(exc, row, proxy_stage)
                    return pause("proxy_preflight_failed")
                # The paid preflight can take time. Never proceed if selection,
                # source, stop request or a UI job changed during that check.
                if stop_path.exists():
                    return pause("stop_requested")
                job_id, reason = _owned_ui_state(ui_path, owned_ui_job_id, progress=progress)
                if stop_path.exists():
                    return pause("stop_requested")
                if reason is not None or job_id != baseline_ui:
                    return pause(reason or "ui_activity")
                try:
                    digest, raw = _source_hash(vault, plan["source_path"])
                except SessionError:
                    return pause("source_changed", attention=True)
                del raw
                if digest != plan["source_sha256"]:
                    return pause("source_changed", attention=True)
                identity, state = _identity_state(vault, row)
                if _binding(identity) != plan["identity_bindings"][str(row)]:
                    return pause("scope_mismatch", attention=True)
                if state != "pending":
                    _mark_skipped(item, state)
                    checkpoint()
                    continue
            selected = SelectedRowVault(vault, row, country=plan["country"], identity_binding=plan["identity_bindings"][str(row)], proxy=proxy)
            item.update(state="in_progress", attempts=1, phase="preparing", error_code=None, login_failure=None, connection=progress["connection"])
            item.pop("provider_failure", None)
            progress["active_row"] = row
            checkpoint()  # Durable intent before credentials or browser requests.
            attempted += 1
            def account_progress(report):
                phase = report.get("phase") if isinstance(report, dict) else None
                if phase in _PHASES:
                    item["phase"] = phase
                    checkpoint()
            try:
                result = prepare(
                    selected, count=1, start_row=row, proxy=proxy, browser_backend="cloakbrowser",
                    headless=not no_browser, reduce_browser_data=reduce_browser_data, progress=account_progress,
                    **({"no_browser": True} if no_browser else {}),
                )
                if isinstance(result, dict) and result.get("selected_rows") == [row] and result.get("connection_pending_rows") == [row] and not result.get("prepared_rows"):
                    diagnostic = provider_failure(result.get("provider_failure"))
                    if diagnostic:
                        item.update(state="connection_pending", phase="connection_pending", error_code="provider_unavailable",
                                    login_failure=None, provider_failure=diagnostic)
                        progress["active_row"] = None
                        checkpoint()
                        continue
                if isinstance(result, dict) and result.get("selected_rows") == [row] and result.get("account_failed_rows") == [row] and not result.get("prepared_rows"):
                    raise SessionError("The saved account session was rejected during preparation.")
                if (
                    not isinstance(result, dict) or result.get("passed") is not True
                    or result.get("selected_rows") != [row] or result.get("prepared_rows") != [row]
                    or result.get("prepared_account_count") != 1
                ):
                    item.update(state="unknown", phase="stopped", error_code="scope_mismatch")
                    progress["unknown_acknowledged"] = False
                    return pause("scope_mismatch", attention=True)
                selected._check(row)
            except BaseException as exc:
                try:
                    isolated = _isolate_preparation_failure(
                        vault, plan, row, exc, job_id=owned_ui_job_id or progress["run_id"],
                    )
                except _WorkerStopped as stopped:
                    item.update(state="unknown", phase="stopped", error_code="scope_mismatch")
                    progress["unknown_acknowledged"] = False
                    progress["active_row"] = None
                    return pause(stopped.reason, attention=stopped.attention)
                except Exception as hold_failure:
                    exc = hold_failure
                    isolated = False
                if isolated:
                    item.update(state="session_review_pending", attempts=1, phase="session_review_pending",
                                error_code=None, login_failure=None, preparation_review=isolated)
                    local_storage = safe_session_storage_failure(exc)
                    if local_storage:
                        item["session_storage_failure"] = local_storage
                    progress["active_row"] = None
                    checkpoint()
                    continue
                code = _failure_code(exc)
                if code == "session_review_pending":
                    _mark_skipped(item, "session_review_pending")
                    progress["active_row"] = None
                    checkpoint()
                    continue
                from .preparation import safe_session_store_failure
                local_session_failure = safe_session_store_failure(exc)
                local_storage_failure = safe_session_storage_failure(exc)
                if code == "provider_unavailable":
                    item.update(state="connection_pending", phase="connection_pending", error_code=code,
                                login_failure=None, provider_failure=provider_failure(exc))
                    progress["active_row"] = None
                    checkpoint()
                    continue
                if code == "proxy_preflight_failed":
                    progress["proxy_failure"] = _safe_proxy_failure(exc, row, "preparation")
                item.update(
                    state="unknown" if code in {"interrupted_unknown", "scope_mismatch"} else "failed", phase="stopped", error_code=code,
                    login_failure=safe_login_failure(exc) or None,
                )
                if local_session_failure:
                    item["local_session_failure"] = local_session_failure
                if local_storage_failure:
                    item["session_storage_failure"] = local_storage_failure
                progress["active_row"] = None
                if code in {"interrupted_unknown", "scope_mismatch"}:
                    progress["unknown_acknowledged"] = False
                    return pause("scope_mismatch" if code == "scope_mismatch" else "unknown_attempt", attention=True)
                if code != "proxy_preflight_failed":
                    progress["infrastructure_failures"] = min(3, progress["infrastructure_failures"] + 1) if code == "infrastructure_failed" else 0
                progress["consecutive_failures"] = min(3, progress["consecutive_failures"] + 1)
                checkpoint()
                if local_session_failure or local_storage_failure:
                    return pause("infrastructure_failed")
                if code in {"browser_cleanup_failed", "browser_unavailable", "proxy_preflight_failed"} or progress["infrastructure_failures"] >= 3:
                    return pause(code)
                if resume_after_review:
                    return pause("infrastructure_failed" if code == "infrastructure_failed" else "repeated_failures")
                if progress["consecutive_failures"] >= 3:
                    return pause("repeated_failures")
            else:
                item.update(state="ready", phase="complete", error_code=None, login_failure=None)
                progress.update(active_row=None, infrastructure_failures=0, consecutive_failures=0, failure_hold=None)
                checkpoint()
            job_id, reason = _owned_ui_state(ui_path, owned_ui_job_id, progress=progress)
            if reason is not None or job_id != baseline_ui:
                return pause(reason or "ui_activity")
            if stop_path.exists():
                return pause("stop_requested")
        progress.update(status="completed_with_pending" if any(item["state"] == "connection_pending" for item in progress["rows"]) else "completed",
                        pause_reason=None, active_row=None)
        checkpoint()
        return summarize(progress)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", required=True, help="Exact uppercase source tag, e.g. LB")
    parser.add_argument("--source", type=Path, default=ROOT / "registered.txt")
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT_PATH)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--run", action="store_true", help="Prepare pending rows; resume the same frozen plan")
    action.add_argument("--dry-run", action="store_true", help="Offline preview; this is the default")
    action.add_argument("--status", action="store_true", help="Show private-safe checkpoint counts without requests")
    action.add_argument("--stop", action="store_true", help="Finish the active account, then pause the running job")
    parser.add_argument("--limit", type=int, help="Maximum new account attempts in this invocation")
    parser.add_argument("--workers", type=int, default=1, help="Requested positive HTTP preparation worker count; active workers are limited to remaining accounts; more than one requires --no-browser")
    parser.add_argument("--fresh-plan", action="store_true", help="Acknowledge quarantined interrupted rows and continue other pending rows; never retries failures")
    parser.add_argument("--resume-after-review", action="store_true", help="After reviewing a failure pause, allow one pending diagnostic account; requires --run --limit 1")
    parser.add_argument("--full-browser-data", action="store_true", help="Preserve normal browser downloads during login")
    parser.add_argument("--no-browser", action="store_true", help="Reuse saved sessions or recover each row's legacy SID through HTTP; never open a browser")
    parser.add_argument("--proxy-egypt", action="store_true", help="Use the saved PacketStream Egypt route for browser login and HTTP validation")
    parser.add_argument("--proxy-sticky-pool", nargs="?", const=DEFAULT_STICKY_POOL_PATH, type=Path, help="Cycle the protected sticky Egypt pool in order, wrapping and resuming its saved cursor; requires --no-browser")
    parser.add_argument("--replace-proxy-pool", action="store_true", help="Bind a different validated pool to the existing plan and reset only its route cursor; requires --run --no-browser --proxy-sticky-pool")
    parser.add_argument("--max-consecutive-failures", type=int, default=3, help=f"Consecutive failure pause threshold, 1-{MAX_FAILURE_BUDGET}; a nondefault budget requires --no-browser")
    return parser


def _safe_runtime_failure(error):
    """Keep numeric local error evidence; never serialize exception messages."""
    kind = "sqlite_error" if isinstance(error, sqlite3.Error) else "filesystem_error" if isinstance(error, OSError) else "session_error"
    result = {"error_kind": kind}
    fields = ("sqlite_errorcode",) if kind == "sqlite_error" else ("errno", "winerror") if kind == "filesystem_error" else ()
    for name in fields:
        try:
            value = getattr(error, name, None)
        except Exception:
            continue
        if type(value) is int and 0 <= value <= 65535:
            result[name] = value
    return result


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.fresh_plan and not args.run:
        parser.error("--fresh-plan requires --run")
    if args.resume_after_review and (not args.run or args.limit != 1):
        parser.error("--resume-after-review requires --run --limit 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if not 1 <= args.workers <= MAX_WORKER_REQUEST:
        parser.error("--workers must be a positive JavaScript-safe integer")
    if args.workers > 1 and not args.no_browser:
        parser.error("--workers greater than one requires --no-browser")
    if not 1 <= args.max_consecutive_failures <= MAX_FAILURE_BUDGET:
        parser.error(f"--max-consecutive-failures must be between 1 and {MAX_FAILURE_BUDGET}")
    if args.max_consecutive_failures != 3 and not args.no_browser:
        parser.error("--max-consecutive-failures other than three requires --no-browser")
    if args.proxy_sticky_pool is not None and not args.no_browser:
        parser.error("--proxy-sticky-pool requires --no-browser")
    if args.replace_proxy_pool and (not args.run or not args.no_browser or args.proxy_sticky_pool is None):
        parser.error("--replace-proxy-pool requires --run --no-browser --proxy-sticky-pool")
    if args.replace_proxy_pool and args.fresh_plan:
        parser.error("--replace-proxy-pool cannot be combined with --fresh-plan")
    try:
        code = _country(args.country)
        path = args.vault.resolve().parent / f"country-{code}-preparation-progress.json"
        if args.stop:
            _atomic_json(path.with_suffix(".stop"), {"stop": True})
            result = {"country": code, "stop_requested": True, "play_events_sent": 0, "like_events_sent": 0}
        else:
            with AccountVault(args.vault) as vault:
                plan = build_plan(vault, args.source, country=code)
                if args.run:
                    result = run_plan(
                        vault, plan, path, limit=args.limit, fresh_plan=args.fresh_plan,
                        resume_after_review=args.resume_after_review, reduce_browser_data=not args.full_browser_data,
                        proxy_egypt=args.proxy_egypt,
                        workers=args.workers,
                        proxy_sticky_pool=args.proxy_sticky_pool,
                        replace_proxy_pool=args.replace_proxy_pool,
                        max_consecutive_failures=args.max_consecutive_failures,
                        **({"no_browser": True} if args.no_browser else {}),
                    )
                else:
                    progress = load_progress(path, plan)
                    if not args.status:
                        progress["no_browser"] = args.no_browser
                        progress["workers"] = args.workers
                        progress["requested_workers"] = args.workers
                        progress["effective_workers"] = _effective_workers(progress, args.workers, limit=args.limit)
                        progress["max_consecutive_failures"] = args.max_consecutive_failures
                        progress["reduce_browser_data"] = not args.full_browser_data and not args.no_browser
                        progress["connection"] = "proxy_egypt" if args.proxy_egypt or args.proxy_sticky_pool is not None else "direct"
                        progress["proxy_pool_active"] = args.proxy_sticky_pool is not None
                        if args.proxy_sticky_pool is not None:
                            sticky_pool = StickyProxyPool.load(args.proxy_sticky_pool)
                            if progress["proxy_pool_count"] and (progress["proxy_pool_count"] != len(sticky_pool) or progress["proxy_pool_fingerprint"] != sticky_pool.fingerprint()):
                                raise SessionError("The selected sticky proxy pool does not match the saved preparation checkpoint.")
                            progress["proxy_pool_count"], progress["proxy_pool_fingerprint"] = len(sticky_pool), sticky_pool.fingerprint()
                            progress["proxy_pool_endpoint"] = sticky_pool.summary().get("endpoint")
                            progress["proxy_pool_country"] = sticky_pool.summary().get("country")
                    result = summarize(progress)
                    result["dry_run"] = not args.status
        print(json.dumps(result, indent=2), flush=True)
        return 0
    except (SessionError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error_code": "country_preparation_failed", "message": "The local preparation plan could not continue. Check the source, vault, progress and job state.", "diagnostics": _safe_runtime_failure(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
