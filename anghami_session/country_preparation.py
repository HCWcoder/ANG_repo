"""Resumable, sequential normal logins for one exact imported country tag.

Preview/status are offline. Only --run starts normal browser preparation; no
play/like events are available here. Never use the UI concurrently with a run.
"""

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from uuid import uuid4

from .errors import LoginCaptureError, SessionError, safe_login_failure
from .proxy import PACKETSTREAM_ENDPOINT, load_packetstream_proxy
from .vault import AccountVault, DEFAULT_VAULT_PATH, ROOT

_STATES = {"pending", "in_progress", "ready", "already_ready", "already_enrolled", "failed", "unknown"}
_STATUSES = {"not_started", "running", "paused", "completed", "attention_required"}
_PHASES = _STATES | {"preparing", "session_lookup", "session_recovery", "login", "validation", "complete", "stopped"}
_CODES = {
    None, "stop_requested", "limit_reached", "ui_busy", "ui_activity", "ui_unavailable",
    "account_failed", "infrastructure_failed", "browser_cleanup_failed", "browser_unavailable",
    "interrupted_unknown", "unknown_attempt", "source_changed", "scope_mismatch",
    "repeated_failures",
    "proxy_preflight_failed",
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


def _identity_state(vault, row):
    result = vault._db.execute("SELECT email_key FROM accounts WHERE source_row=?", (row,)).fetchone()
    if result is None:
        raise SessionError("The frozen source row is missing from the vault.")
    identity = result[0]
    copies = list(vault._db.execute(
        "SELECT source_row,state,session IS NOT NULL FROM accounts WHERE email_key=? ORDER BY source_row",
        (identity,),
    ))
    enrolled = vault.enrolled_test_rows()
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


class SelectedRowVault:
    """Allow the normal preparation function to act only on its frozen row."""

    def __init__(self, vault, row, *, country, identity_binding=None, proxy=None):
        if type(row) is not int or row < 1:
            raise SessionError("Choose one positive frozen source row.")
        self._vault, self.row, self.country = vault, row, _country(country)
        self._proxy = proxy
        self.path = vault.path
        identity, _ = _identity_state(vault, row)
        self._binding = _binding(identity) if identity_binding is None else identity_binding
        self._check(row)

    def _check(self, row):
        if type(row) is not int or row != self.row:
            raise SessionError("The country preparation row cannot change.")
        identity, _ = _identity_state(self._vault, row)
        if _binding(identity) != self._binding:
            raise SessionError("The frozen account identity changed.")

    def select_test_candidates(self, count, *, start_row=1):
        if type(count) is not int or count != 1 or type(start_row) is not int or start_row != self.row:
            raise SessionError("Country preparation selects exactly its frozen row.")
        self._check(start_row)
        record = self.record(start_row)
        del record
        return [self.row]

    def record(self, row):
        self._check(row)
        record = self._vault.record(row)
        if record.get("country") != self.country:
            raise SessionError("The frozen row has a different country tag.")
        return record

    def session(self, row):
        self._check(row)
        return self._vault.session(row)

    def attach(self, row, saved, **options):
        self._check(row)
        if options.get("proxy") is not self._proxy:
            raise SessionError("The selected preparation connection cannot change.")
        return self._vault.attach(row, saved, **options)

    def enable_test_account(self, row):
        self._check(row)
        return self._vault.enable_test_account(row)


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
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _new_progress(plan):
    return {
        "format_version": 1, "country": plan["country"], "source_sha256": plan["source_sha256"],
        "plan_id": plan["plan_id"], "run_id": uuid4().hex, "status": "not_started", "pause_reason": None,
        "created_at_utc": _now(), "updated_at_utc": _now(), "active_row": None,
        "tagged_rows": plan["tagged_rows"], "duplicate_rows": plan["duplicate_rows"],
        "unknown_acknowledged": False, "infrastructure_failures": 0, "consecutive_failures": 0,
        "reduce_browser_data": True,
        "no_browser": False,
        "connection": "direct",
        "failure_hold": None,
        "proxy_failure": None,
        "rows": [{**row, "attempts": 0, "phase": row["state"], "error_code": None, "login_failure": None, "connection": None} for row in plan["rows"]],
    }


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
    return {name: value[name] for name in ("source_row", "stage", "code")}


def _safe_proxy_failure(exc, row, stage):
    """Classify only fixed local messages; never save exception/profile data."""
    code = "country_check_failed" if stage == "country_check" else "proxy_error"
    if isinstance(exc, SessionError):
        try:
            code = _PROXY_FAILURE_MESSAGES.get(str(exc), code)
        except Exception:
            pass
    return {"source_row": row, "stage": stage, "code": code}


def _failure_hold(progress):
    if progress.get("failure_hold") in _FAILURE_HOLDS:
        return progress["failure_hold"]
    if progress.get("pause_reason") in _FAILURE_HOLDS:
        return progress["pause_reason"]
    if progress.get("infrastructure_failures", 0) >= 3:
        return "infrastructure_failed"
    if progress.get("consecutive_failures", 0) >= 3:
        return "repeated_failures"
    return None


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
        if type(value.get("unknown_acknowledged")) is not bool or type(value.get("infrastructure_failures")) is not int or not 0 <= value["infrastructure_failures"] <= 3:
            raise ValueError
        consecutive = value.get("consecutive_failures", 0)
        if type(consecutive) is not int or not 0 <= consecutive <= 3:
            raise ValueError
        if type(value.get("reduce_browser_data", True)) is not bool:
            raise ValueError
        if type(value.get("no_browser", False)) is not bool:
            raise ValueError
        if value.get("connection", "direct") not in {"direct", "proxy_egypt"}:
            raise ValueError
        if value.get("failure_hold") not in _FAILURE_HOLDS | {None}:
            raise ValueError
        for row in rows:
            if (
                type(row.get("source_row")) is not int or row.get("state") not in _STATES
                or type(row.get("attempts")) is not int or not 0 <= row["attempts"] <= 1
                or row.get("phase") not in _PHASES or row.get("error_code") not in _CODES
                or (row["state"] in {"pending", "already_ready", "already_enrolled"}) != (row["attempts"] == 0)
                or row.get("connection") not in {None, "direct", "proxy_egypt"}
            ):
                raise ValueError
        result = {name: value.get(name, template[name]) for name in template if name != "rows"}
        result["failure_hold"] = _failure_hold(value)
        result["proxy_failure"] = _stored_proxy_failure(value.get("proxy_failure"), {row["source_row"] for row in rows})
        result["rows"] = [{
            **{name: row[name] for name in ("source_row", "state", "attempts", "phase", "error_code")},
            "login_failure": _stored_login_failure(row.get("login_failure")),
            "connection": row.get("connection") or ("direct" if row["attempts"] else None),
        } for row in rows]
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
        return result
    except (OSError, ValueError, TypeError, KeyError):
        raise SessionError("The country progress file is invalid or belongs to a different frozen plan.") from None


def summarize(progress):
    counts = {state: sum(row["state"] == state for row in progress["rows"]) for state in sorted(_STATES)}
    attempted = [row["source_row"] for row in progress["rows"] if row["attempts"]]
    pending = [row["source_row"] for row in progress["rows"] if row["state"] == "pending"]
    no_browser = progress.get("no_browser", False)
    summary = {
        "country": progress["country"], "status": progress["status"], "pause_reason": progress["pause_reason"],
        "tagged_rows": progress["tagged_rows"], "unique_accounts": len(progress["rows"]),
        "duplicate_rows": progress["duplicate_rows"], "counts": counts,
        "active_row": progress["active_row"], "last_attempted_rows": attempted[-5:], "next_rows": pending[:5],
        "connection": progress.get("connection", "direct"), "browser": "none" if no_browser else "cloakbrowser", "headless": not no_browser,
        "no_browser": no_browser, "browser_required": not no_browser,
        "preparation_method": "http" if no_browser else "browser",
        "reduce_browser_data": False if no_browser else progress.get("reduce_browser_data", True),
        "automatic_retry": False, "play_events_sent": 0, "like_events_sent": 0,
        "consecutive_failures": progress.get("consecutive_failures", 0),
        "failure_hold": _failure_hold(progress),
        "proxy_failure": _stored_proxy_failure(progress.get("proxy_failure"), {row["source_row"] for row in progress["rows"]}),
        "recent_failures": [{
            "source_row": row["source_row"], "error_code": row["error_code"],
            "login_failure": _stored_login_failure(row.get("login_failure")),
            "connection": row.get("connection") or ("direct" if row["attempts"] else None),
        } for row in progress["rows"] if row["state"] in {"failed", "unknown"}][-5:],
    }
    if summary["connection"] == "proxy_egypt":
        summary["proxy"] = {"provider": "PacketStream", "country": "EG", "endpoint": PACKETSTREAM_ENDPOINT, "sticky": True}
    return summary


def _ui_state(path):
    path = Path(path)
    if not path.exists():
        return None, None
    try:
        if path.stat().st_size > 10_000_000:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
        status, job_id = value["status"], value["id"]
        if status not in {"queued", "running", "succeeded", "failed"} or not isinstance(job_id, str) or not job_id:
            raise ValueError
        return job_id, "ui_busy" if status in {"queued", "running"} else None
    except (OSError, ValueError, KeyError, TypeError):
        return None, "ui_unavailable"


def _failure_code(exc):
    if not isinstance(exc, Exception) or isinstance(exc, EOFError):
        return "interrupted_unknown"
    if isinstance(exc, SessionError):
        message = str(exc)
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


def run_plan(vault, plan, progress_path, *, limit=None, prepare=None, ui_path=None, stop_path=None, fresh_plan=False, resume_after_review=False, reduce_browser_data=True, proxy_egypt=False, no_browser=False):
    """Run each frozen pending row at most once, using normal count-one preparation."""
    if limit is not None and (type(limit) is not int or limit < 1):
        raise SessionError("The optional run limit must be a positive integer.")
    if type(resume_after_review) is not bool or (resume_after_review and limit != 1):
        raise SessionError("Resuming a failure pause requires an explicit review and a one-account limit.")
    if type(reduce_browser_data) is not bool:
        raise SessionError("The browser data selection must be true or false.")
    if type(proxy_egypt) is not bool:
        raise SessionError("The proxy selection must be true or false.")
    if type(no_browser) is not bool:
        raise SessionError("The browser-free selection must be true or false.")
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
        hold = _failure_hold(progress)
        if hold is not None and not resume_after_review:
            # Re-running the same command cannot consume another account after a
            # failure pause. This also catches a crash just before saving the pause.
            if progress["status"] != "paused" or progress["pause_reason"] != hold:
                progress.update(status="paused", pause_reason=hold, active_row=None, updated_at_utc=_now())
                _atomic_json(progress_path, progress)
            return summarize(progress)
        if fresh_plan:
            replacement = _new_progress(plan)
            old_rows = {row["source_row"]: row for row in progress["rows"]}
            for row in replacement["rows"]:
                previous = old_rows[row["source_row"]]
                if previous["state"] in {"failed", "unknown", "in_progress"}:
                    row.update(previous)
            replacement["unknown_acknowledged"] = True
            replacement["infrastructure_failures"] = progress["infrastructure_failures"]
            replacement["consecutive_failures"] = progress["consecutive_failures"]
            replacement["failure_hold"] = hold
            replacement["proxy_failure"] = progress["proxy_failure"]
            if progress_path.exists():
                _atomic_json(progress_path.with_name(progress_path.stem + ".previous-" + progress["run_id"] + ".json"), progress)
            progress = replacement
        progress["reduce_browser_data"] = reduce_browser_data
        progress["no_browser"] = no_browser
        progress["connection"] = "proxy_egypt" if proxy_egypt else "direct"
        if not proxy_egypt:
            progress["proxy_failure"] = None
        def checkpoint():
            progress["updated_at_utc"] = _now()
            progress["failure_hold"] = _failure_hold(progress)
            _atomic_json(progress_path, progress)
        def pause(reason, *, attention=False):
            progress.update(status="attention_required" if attention else "paused", pause_reason=reason, active_row=None)
            checkpoint()
            return summarize(progress)
        baseline_ui, reason = _ui_state(ui_path)
        if reason is not None:
            return pause(reason)
        for item in progress["rows"]:
            if item["state"] != "in_progress":
                continue
            row = item["source_row"]
            selected = SelectedRowVault(vault, row, country=plan["country"], identity_binding=plan["identity_bindings"][str(row)])
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
            else:
                item.update(state="unknown", phase="stopped", error_code="interrupted_unknown")
                progress["unknown_acknowledged"] = False
            checkpoint()
        if any(row["state"] == "unknown" for row in progress["rows"]) and not progress["unknown_acknowledged"]:
            return pause("unknown_attempt", attention=True)
        stop_path.unlink(missing_ok=True)  # An explicit --run resumes a stopped job.
        progress.update(status="running", pause_reason=None, active_row=None)
        checkpoint()
        attempted = 0
        for item in progress["rows"]:
            if item["state"] != "pending":
                continue
            if stop_path.exists():
                return pause("stop_requested")
            if limit is not None and attempted >= limit:
                return pause("limit_reached")
            job_id, reason = _ui_state(ui_path)
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
                item.update(state=state, phase=state)
                checkpoint()
                continue
            proxy = None
            if proxy_egypt:
                progress["proxy_failure"] = None
                proxy_stage = "profile_load"
                try:
                    proxy = load_packetstream_proxy(vault.path.parent / "packetstream.dpapi")
                    proxy_stage = "profile_country"
                    if type(getattr(proxy, "country", None)) is not str or proxy.country != "EG":
                        raise SessionError("The saved proxy route must select Egypt.")
                    proxy_stage = "country_check"
                    verification = proxy.verify_country()
                    if (
                        not isinstance(verification, dict) or verification.get("country") != "EG"
                        or verification.get("country_verified") is not True or verification.get("proxy_used") is not True
                    ):
                        raise SessionError("The selected proxy route was not verified.")
                except (KeyboardInterrupt, EOFError):
                    return pause("stop_requested")
                except Exception as exc:
                    progress["proxy_failure"] = _safe_proxy_failure(exc, row, proxy_stage)
                    return pause("proxy_preflight_failed")
                # The paid preflight can take time. Never proceed if selection,
                # source, stop request or a UI job changed during that check.
                if stop_path.exists():
                    return pause("stop_requested")
                job_id, reason = _ui_state(ui_path)
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
                    item.update(state=state, phase=state)
                    checkpoint()
                    continue
            selected = SelectedRowVault(vault, row, country=plan["country"], identity_binding=plan["identity_bindings"][str(row)], proxy=proxy)
            item.update(state="in_progress", attempts=1, phase="preparing", error_code=None, login_failure=None, connection=progress["connection"])
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
                if (
                    not isinstance(result, dict) or result.get("passed") is not True
                    or result.get("selected_rows") != [row] or result.get("prepared_rows") != [row]
                    or result.get("prepared_account_count") != 1
                ):
                    item.update(state="unknown", phase="stopped", error_code="scope_mismatch")
                    progress["unknown_acknowledged"] = False
                    return pause("scope_mismatch", attention=True)
            except BaseException as exc:
                code = _failure_code(exc)
                if code == "proxy_preflight_failed":
                    progress["proxy_failure"] = _safe_proxy_failure(exc, row, "preparation")
                item.update(
                    state="unknown" if code == "interrupted_unknown" else "failed", phase="stopped", error_code=code,
                    login_failure=safe_login_failure(exc) or None,
                )
                progress["active_row"] = None
                if code == "interrupted_unknown":
                    progress["unknown_acknowledged"] = False
                    return pause("unknown_attempt", attention=True)
                if code != "proxy_preflight_failed":
                    progress["infrastructure_failures"] = min(3, progress["infrastructure_failures"] + 1) if code == "infrastructure_failed" else 0
                progress["consecutive_failures"] = min(3, progress["consecutive_failures"] + 1)
                checkpoint()
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
            job_id, reason = _ui_state(ui_path)
            if reason is not None or job_id != baseline_ui:
                return pause(reason or "ui_activity")
            if stop_path.exists():
                return pause("stop_requested")
        progress.update(status="completed", pause_reason=None, active_row=None)
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
    parser.add_argument("--fresh-plan", action="store_true", help="Acknowledge quarantined interrupted rows and continue other pending rows; never retries failures")
    parser.add_argument("--resume-after-review", action="store_true", help="After reviewing a failure pause, allow one pending diagnostic account; requires --run --limit 1")
    parser.add_argument("--full-browser-data", action="store_true", help="Preserve normal browser downloads during login")
    parser.add_argument("--no-browser", action="store_true", help="Reuse saved sessions or recover each row's legacy SID through HTTP; never open a browser")
    parser.add_argument("--proxy-egypt", action="store_true", help="Use the saved PacketStream Egypt route for browser login and HTTP validation")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.fresh_plan and not args.run:
        parser.error("--fresh-plan requires --run")
    if args.resume_after_review and (not args.run or args.limit != 1):
        parser.error("--resume-after-review requires --run --limit 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
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
                        **({"no_browser": True} if args.no_browser else {}),
                    )
                else:
                    progress = load_progress(path, plan)
                    if not args.status:
                        progress["no_browser"] = args.no_browser
                        progress["reduce_browser_data"] = not args.full_browser_data and not args.no_browser
                        progress["connection"] = "proxy_egypt" if args.proxy_egypt else "direct"
                    result = summarize(progress)
                    result["dry_run"] = not args.status
        print(json.dumps(result, indent=2), flush=True)
        return 0
    except (SessionError, OSError, sqlite3.Error):
        print(json.dumps({"error_code": "country_preparation_failed", "message": "The local preparation plan could not continue. Check the source, vault, progress and job state."}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
