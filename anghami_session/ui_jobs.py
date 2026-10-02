"""One bounded local UI job at a time, with credential-free progress reports."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
from uuid import uuid4

from .errors import SessionError
from .play_record import TEST_SONG_ID, _journal
from .proxy import load_packetstream_proxy
from .test_settings import read_test_song_id, validate_test_song_id
from .vault import AccountVault, DEFAULT_VAULT_PATH


class JobValidationError(SessionError):
    """A UI request was rejected before a worker was created."""


class JobBusyError(SessionError):
    """There is already a queued or running job."""


_ACTIONS = frozenset({"prepare", "preview", "play", "like", "check", "song", "login", "proxy-check"})
_ROW_ACTIONS = frozenset({"play", "like", "check", "song", "login"})
_WORKBENCH_ACTIONS = frozenset({"play", "like", "check", "song"})
_FIELDS = frozenset({"action", "rows", "count", "start_row", "browser", "headless", "proxy_egypt", "song_id", "reduce_browser_data", "no_browser"})
_PHASES = frozenset({
    "queued", "opening_vault", "validating_rows", "proxy_preflight", "preparing", "preview",
    "session_lookup", "session_recovery", "login", "validation", "session_validation", "preflight", "legacy_source",
    "account_identity", "metadata", "state_before", "mutation", "state_after", "executing",
    "complete", "failed", "stopped", "proxy_check", "checking", "song_metadata",
})
_BOOL_KEYS = frozenset({
    "passed", "authenticated", "negative_control_passed", "server_account_identity_verified",
    "metadata_verified", "event_attempted", "event_accepted", "mutation_attempted", "mutation_accepted",
    "liked_before", "liked_after", "persisted_state_verified", "browser_required", "password_required",
    "automatic_retry", "synthetic", "downstream_statistics_verified", "dry_run", "headless",
    "session_saved", "country_verified", "proxy_used", "authentication_rejected", "measurement_complete",
    "reduce_browser_data", "no_browser",
})
_NUMBER_KEYS = frozenset({
    "source_row", "test_number", "song_duration_seconds", "metadata_duration_seconds", "reported_play_seconds",
    "reported_play_fraction", "elapsed_seconds", "event_attempts", "mutation_attempts", "audio_bytes",
    "http_status", "event_http_status", "metadata_http_status", "mutation_http_status",
    "proxy_connect_http_status", "requested_accounts", "prepared_account_count", "attempted_accounts",
    "active_row", "failed_row", "like_events_sent", "play_events_sent", "request_count", "request_bytes",
    "upload_body_bytes", "download_body_bytes", "response_header_bytes",
})
_ENUMS = {
    "phase": _PHASES, "failed_phase": _PHASES,
    "api_status": frozenset({"ok", "failed", "unknown", "not_attempted"}),
    "event_result": frozenset({"accepted", "rejected", "unknown", "not_attempted"}),
    "mutation_result": frozenset({"accepted", "rejected", "unknown", "not_attempted", "skipped_already_liked"}),
    "browser": frozenset({"chrome", "cloakbrowser", "none"}),
    "preparation_method": frozenset({"browser", "http"}),
    "connection": frozenset({"direct", "proxy_egypt"}),
    "transport": frozenset({"curl_cffi"}),
    "country": frozenset({"EG"}),
    "error_code": frozenset({
        "proxy_preflight_failed", "metadata_region_unavailable", "metadata_invalid", "test_failed",
        "preflight_failed", "transport_failed", "http_rejected", "response_invalid", "event_rejected",
        "event_incomplete", "journal_failed", "legacy_source_invalid", "legacy_endpoint_invalid",
        "request_scope_invalid", "song_scope_invalid", "metadata_repeat_blocked", "event_repeat_blocked",
        "legacy_claim_invalid", "account_identity_missing", "session_ambiguous", "session_credentials_missing",
        "playlist_invalid", "playlist_discovery_invalid", "playlist_identity_missing", "playlist_count_invalid",
        "likes_order_invalid", "likes_order_incomplete", "read_scope_invalid", "state_http_failed",
        "state_api_failed", "state_read_failed", "playlist_identity_changed", "mutation_scope_invalid",
        "mutation_repeat_blocked", "mutation_http_failed", "mutation_reply_invalid", "mutation_rejected",
        "mutation_transport_failed", "mutation_incomplete", "mutation_failed", "readback_not_liked",
        "preparation_failed", "cancelled",
    }),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value):
    return type(value) is int and 1 <= value <= 2**31 - 1


def _validate(payload):
    if not isinstance(payload, dict) or not set(payload).issubset(_FIELDS):
        raise JobValidationError("Use the supported test controls only.")
    action = payload.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise JobValidationError("Select a supported action.")
    count = payload.get("count", 1)
    if type(count) is not int or not 1 <= count <= 5:
        raise JobValidationError("Choose a count from 1 to 5.")
    start = payload.get("start_row", 1)
    if not _positive_int(start):
        raise JobValidationError("The starting source row must be a positive integer.")
    browser = payload.get("browser", "chrome")
    if not isinstance(browser, str) or browser not in {"chrome", "cloakbrowser"}:
        raise JobValidationError("Choose Chrome or CloakBrowser.")
    for field in ("headless", "proxy_egypt", "reduce_browser_data", "no_browser"):
        if field in payload and type(payload[field]) is not bool:
            raise JobValidationError("Browser and proxy selections must be true or false.")
    if "no_browser" in payload and action not in {"prepare", "preview"}:
        raise JobValidationError("Choose a preparation method only for account preparation or preview.")
    no_browser = payload.get("no_browser", False)
    rows = payload.get("rows")
    if "rows" in payload and (
        not isinstance(rows, list) or not rows
        or (action not in _WORKBENCH_ACTIONS and len(rows) > 5)
        or any(not _positive_int(row) for row in rows) or len(set(rows)) != len(rows)
    ):
        raise JobValidationError(
            "Choose distinct positive source rows." if action in _WORKBENCH_ACTIONS
            else "Choose 1-5 distinct positive source rows."
        )
    if action in _ROW_ACTIONS and rows is None:
        raise JobValidationError("Select the account rows for this action.")
    requested_song = None
    if "song_id" in payload:
        if action not in {"play", "like", "song"}:
            raise JobValidationError("Choose a song only for song tests.")
        try:
            requested_song = validate_test_song_id(payload["song_id"])
        except SessionError:
            raise JobValidationError("Enter a positive numeric test song ID.") from None
    options = {
        "action": action, "rows": list(rows or []), "count": count, "start_row": start,
        "browser": browser, "headless": False if no_browser else payload.get("headless", True),
        "proxy_egypt": payload.get("proxy_egypt", False),
        "reduce_browser_data": False if no_browser else payload.get("reduce_browser_data", False),
        "no_browser": no_browser,
    }
    if requested_song is not None:
        options["song_id"] = requested_song
    return options


def _public_report(value):
    """Accept only known typed report facts; never serialize arbitrary API data."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key in _BOOL_KEYS and (type(item) is bool or item is None):
            result[key] = item
        elif key in _NUMBER_KEYS and type(item) in (int, float) and math.isfinite(item) and item >= 0:
            result[key] = item
        elif key in _ENUMS and isinstance(item, str) and item in _ENUMS[key]:
            result[key] = item
        elif key == "song_id":
            try:
                result[key] = validate_test_song_id(item)
            except SessionError:
                pass
        elif key in {"selected_rows", "prepared_rows"} and isinstance(item, list) and all(_positive_int(row) for row in item):
            result[key] = list(item)
        elif key in {"checked_at_utc", "session_saved_at_utc"} and isinstance(item, str) and len(item) <= 64:
            try:
                result[key] = datetime.fromisoformat(item).isoformat()
            except ValueError:
                pass
        elif key == "operations" and isinstance(item, dict):
            result[key] = {name: "ok" for name in ("relations", "playlists") if item.get(name) == "ok"}
        elif key == "without_session":
            result[key] = _public_report(item)
        elif key == "proxy" and isinstance(item, dict):
            if item.get("provider") == "PacketStream" and item.get("country") == "EG":
                result[key] = {"provider": "PacketStream", "country": "EG", "sticky": item.get("sticky") is True}
                for flag in ("country_verified", "proxy_used"):
                    if type(item.get(flag)) is bool:
                        result[key][flag] = item[flag]
                if isinstance(item.get("exit_check"), dict):
                    result[key]["exit_check"] = _public_report(item["exit_check"])
        elif key == "bandwidth" and isinstance(item, dict):
            result[key] = {name: _public_report(item[name]) for name in ("get_song", "play_song", "total") if isinstance(item.get(name), dict)}
    return result


class _JobFailure(Exception):
    def __init__(self, details):
        self.details = details


class JobManager:
    """Run jobs sequentially. Each worker owns its SQLite vault connection."""

    def __init__(self, vault_path=DEFAULT_VAULT_PATH, *, vault_factory=AccountVault, proxy_loader=load_packetstream_proxy):
        self.vault_path = Path(vault_path).resolve()
        self._vault_factory = vault_factory
        self._proxy_loader = proxy_loader
        self._lock = threading.RLock()
        self._latest = None
        self._thread = None
        self._report_path = self.vault_path.parent / "ui-last-job.json"

    def snapshot(self):
        with self._lock:
            return deepcopy(self._latest)

    def submit(self, payload):
        options = _validate(payload)
        action = options["action"]
        total = (
            len(options["rows"]) * options["count"] if action in {"play", "like"}
            else options["count"] if action in {"prepare", "preview"}
            else len(options["rows"]) if action in _ROW_ACTIONS else 1
        )
        with self._lock:
            if self._latest is not None and self._latest["status"] in {"queued", "running"}:
                raise JobBusyError("A test job is already running. Wait for it to finish.")
            declared_song = read_test_song_id(self.vault_path.parent / "test-settings.json")
            if "song_id" in options and options["song_id"] != declared_song:
                raise JobValidationError("The configured test song changed. Refresh the workspace before running this test.")
            options["song_id"] = declared_song
            job = {
                "id": uuid4().hex, "action": action, "status": "queued",
                "progress": {"completed": 0, "total": total}, "phase": "queued",
                "message": "Queued locally.", "results": [], "error": None,
                "started_at": None, "finished_at": None,
                "rows": options["rows"], "count": options["count"],
                "proxy_egypt": options["proxy_egypt"] or action == "proxy-check",
                "reduce_browser_data": options["reduce_browser_data"],
                "no_browser": options["no_browser"],
                "song_id": options["song_id"],
            }
            try:
                _journal(job, self._report_path)
            except Exception:
                raise JobValidationError("The local job report could not be saved. Check the report folder before testing.") from None
            self._latest = job
            queued = deepcopy(job)
            self._thread = threading.Thread(target=self._run, args=(options,), name="anghami-ui-job", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._fail({"code": "worker_failed", "message": "The local worker could not start. No test operation was run."})
                raise JobValidationError("The local worker could not start. No test operation was run.") from None
            return queued

    def _update(self, **fields):
        with self._lock:
            candidate = deepcopy(self._latest)
            candidate.update(deepcopy(fields))
            _journal(candidate, self._report_path)
            self._latest = candidate

    def _phase(self, phase, message):
        self._update(phase=phase, message=message)

    def _append(self, report, *, completed=None):
        with self._lock:
            results = deepcopy(self._latest["results"])
            results.append(_public_report(report))
            fields = {"results": results}
            if completed is not None:
                fields["progress"] = {"completed": completed, "total": self._latest["progress"]["total"]}
            self._update(**fields)

    def _fail(self, error):
        with self._lock:
            candidate = deepcopy(self._latest)
            candidate.update(status="failed", phase="failed", message=error["message"], error=deepcopy(error), finished_at=_now())
            # Preserve the failure in memory even when storage itself has failed.
            self._latest = candidate
            try:
                _journal(candidate, self._report_path)
            except Exception:
                self._latest["error"] = {
                    "code": "journal_failed", "message": "The job report could not be saved. An attempted write may be unknown; check existing account reports before another test.",
                }
                self._latest["message"] = self._latest["error"]["message"]

    def _run(self, options):
        try:
            self._update(status="running", phase="opening_vault", message="Starting local checks.", started_at=_now())
            action = options["action"]
            if action == "proxy-check":
                self._phase("proxy_check", "Verifying an Egypt exit without account requests.")
                proxy = self._proxy_loader(self.vault_path.parent / "packetstream.dpapi")
                checked = proxy.verify_country()
                if not isinstance(checked, dict) or checked.get("country_verified") is not True or checked.get("proxy_used") is not True:
                    raise SessionError("Egypt proxy verification did not confirm its result.")
                self._append({"passed": True, "country": "EG", "proxy": {**proxy.summary(), **_public_report(checked)}}, completed=1)
            else:
                with self._vault_factory(self.vault_path) as vault:
                    self._validate_rows(vault, options)
                    proxy = None
                    if options["proxy_egypt"] and action != "preview":
                        self._phase("proxy_preflight", "Loading the encrypted Egypt proxy configuration.")
                        proxy = self._proxy_loader(self.vault_path.parent / "packetstream.dpapi")
                    self._perform(vault, options, proxy)
            self._update(status="succeeded", phase="complete", message="Completed successfully.", finished_at=_now())
        except _JobFailure as exc:
            self._fail(exc.details)
        except BaseException as exc:
            self._fail(self._safe_error(options["action"], exc, no_browser=options["no_browser"]))

    def _validate_rows(self, vault, options):
        if options["action"] not in _ROW_ACTIONS:
            return
        self._phase("validating_rows", "Checking every selected source row before starting.")
        for row in options["rows"]:
            vault.record(row)
        if options["action"] in _WORKBENCH_ACTIONS and not set(options["rows"]).issubset(vault.enrolled_test_rows()):
            raise _JobFailure({"code": "account_not_prepared", "message": "A selected row is not prepared for testing. Prepare it before using the test workbench."})
        if options["action"] in _WORKBENCH_ACTIONS:
            cohort = vault.test_accounts()
            ready = cohort.get("ready_rows") if isinstance(cohort, dict) else None
            ready_rows = {row for row in ready if _positive_int(row)} if isinstance(ready, list) else set()
            if not set(options["rows"]).issubset(ready_rows):
                raise _JobFailure({"code": "account_not_ready", "message": "A selected test account is not ready. Refresh or prepare its saved session before testing."})

    def _preparation_progress(self, report):
        safe = _public_report(report)
        completed = safe.get("prepared_account_count", 0)
        phase = safe.get("phase", "preparing")
        row = safe.get("active_row")
        message = f"Prepared {completed} account(s)." if row is None else f"Row {row}: {phase.replace('_', ' ')}. Prepared {completed} account(s)."
        with self._lock:
            self._update(phase=phase, message=message, progress={"completed": completed, "total": self._latest["progress"]["total"]})

    def _perform(self, vault, options, proxy):
        action = options["action"]
        if action in {"prepare", "preview"}:
            from .preparation import prepare_test_accounts
            self._phase("preview" if action == "preview" else "preparing", "Selecting additional unique registered accounts." if action == "preview" else "Reusing and validating registered account sessions without a browser." if options["no_browser"] else "Preparing normal saved account sessions.")
            report = prepare_test_accounts(
                vault, count=options["count"], start_row=options["start_row"], proxy=proxy,
                browser_backend=options["browser"], headless=options["headless"],
                dry_run=action == "preview", progress=self._preparation_progress,
                **({"reduce_browser_data": True} if options["reduce_browser_data"] else {}),
                **({"no_browser": True} if options["no_browser"] else {}),
            )
            if action == "preview" and options["proxy_egypt"]:
                # Preview selects a future route without unlocking credentials
                # or making requests. Its report must retain that selection.
                report = {**report, "connection": "proxy_egypt"}
            self._append(report, completed=options["count"] if action == "preview" else report["prepared_account_count"])
            if action == "prepare" and report.get("passed") is not True:
                raise _JobFailure({"code": "preparation_failed", "message": "Account preparation did not complete. Check the preparation report before continuing."})
            return
        completed = 0
        for row in options["rows"]:
            if action in {"play", "like"}:
                runner = vault.test_play_record if action == "play" else vault.test_like
                name = "test-play-record" if action == "play" else "test-like"
                path = self.vault_path.parent / f"account-{row}.{name}-report.json"
                for number in range(1, options["count"] + 1):
                    self._phase("executing", f"Row {row}: {action} test {number} of {options['count']}.")
                    before = self._report_signature(path)
                    try:
                        test_options = {} if proxy is None else {"proxy": proxy}
                        if options["song_id"] != TEST_SONG_ID:
                            test_options["declared_song_id"] = options["song_id"]
                        report = runner(row, options["song_id"], **test_options)
                    except BaseException as exc:
                        failed = self._changed_report(path, before)
                        if failed:
                            self._append({**failed, "source_row": row, "test_number": number})
                        error = self._safe_error(action, exc)
                        error["source_row"] = row
                        error["test_number"] = number
                        if failed.get("failed_phase"):
                            error["failed_phase"] = failed["failed_phase"]
                        if failed.get("error_code"):
                            error["report_code"] = failed["error_code"]
                        error["result_unknown"] = (
                            failed.get("event_attempted") is True and failed.get("event_result") == "unknown"
                            or failed.get("mutation_attempted") is True and failed.get("mutation_result") == "unknown"
                        )
                        raise _JobFailure(error) from None
                    if not isinstance(report, dict) or report.get("passed") is not True:
                        self._append({**_public_report(report), "source_row": row, "test_number": number})
                        raise _JobFailure({"code": "test_not_confirmed", "message": "The test did not confirm success. Remaining tests stopped; check the account report before another write.", "source_row": row, "test_number": number})
                    completed += 1
                    self._append({**_public_report(report), "source_row": row, "test_number": number}, completed=completed)
            else:
                if action == "login":
                    from .capture import capture_login
                    self._phase("login", f"Signing in normally to row {row}.")
                    record = vault.record(row)
                    login_options = {
                        "email": record["email"], "password": record["password"],
                        "browser_backend": options["browser"], "headless": options["headless"],
                    }
                    if proxy is not None:
                        login_options["proxy"] = proxy
                    if options["reduce_browser_data"]:
                        login_options["reduce_browser_data"] = True
                    try:
                        saved, _metadata = capture_login(**login_options)
                    finally:
                        login_options.clear()
                        del record
                    self._phase("validation", f"Validating and saving row {row}'s account-bound session.")
                    report = vault.attach(row, saved, **({"proxy": proxy} if proxy is not None else {}))
                    report = {**report, "reduce_browser_data": options["reduce_browser_data"]}
                else:
                    self._phase("checking" if action == "check" else "song_metadata", f"Checking row {row}'s saved session." if action == "check" else f"Reading test song metadata for row {row}.")
                    with vault._http_session(row, proxy) as session:
                        if action == "check":
                            report = session.check(negative_control=True)
                            if (
                                not isinstance(report, dict) or report.get("authenticated") is not True
                                or not isinstance(report.get("without_session"), dict)
                                or report["without_session"].get("authentication_rejected") is not True
                            ):
                                raise SessionError("Saved-session authentication and its negative control were not confirmed.")
                            report = {**report, "passed": True}
                        else:
                            from .playback import song_summary
                            report = {**song_summary(session.song(options["song_id"])), "metadata_verified": True, "passed": True}
                            if report["song_id"] != options["song_id"]:
                                raise SessionError("The song metadata did not match the declared test song.")
                            if proxy is not None:
                                report["proxy"] = session.proxy_summary
                completed += 1
                self._append({**_public_report(report), "source_row": row}, completed=completed)

    @staticmethod
    def _report_signature(path):
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    @classmethod
    def _changed_report(cls, path, before):
        if cls._report_signature(path) == before:
            return {}
        try:
            if path.stat().st_size > 256_000:
                return {}
            return _public_report(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return {}

    @staticmethod
    def _safe_error(action, exc, *, no_browser=False):
        # Even SessionError may be supplied by an extension or transport. Never
        # expose its text, URL, headers, proxy username, or authentication key.
        if isinstance(exc, (KeyboardInterrupt, EOFError)):
            return {"code": "interrupted", "message": "The operation stopped. Existing saved sessions were kept; check account reports before another write."}
        if isinstance(exc, FileNotFoundError):
            return {"code": "file_missing", "message": "A required local vault or configuration file is missing."}
        if getattr(exc, "code", None) == "journal_failed":
            return {"code": "journal_failed", "message": "The local report could not be saved. Check existing reports before another test."}
        if action in {"play", "like"}:
            return {"code": "test_failed", "message": "The test stopped on its first failure. An attempted write may be unknown; check the account report before running it again. No automatic retry was made."}
        if action == "proxy-check":
            return {"code": "proxy_check_failed", "message": "Egypt proxy verification failed. Check the saved proxy credentials, balance, and route availability. No direct fallback was made."}
        if action == "prepare" and no_browser:
            return {"code": "session_preparation_failed", "message": "Registered session recovery or validation failed. Check the selected source row, session validity, and connection. No browser login was attempted."}
        if action in {"prepare", "login"}:
            return {"code": "session_preparation_failed", "message": "Normal login or saved-session validation failed. Check the selected source row and proxy configuration. Previously saved sessions were kept."}
        if action == "preview":
            return {"code": "selection_failed", "message": "The requested selection is unavailable. Check the start row or choose fewer accounts."}
        return {"code": "session_check_failed", "message": "The saved-session or test song check failed. Refresh the selected session or check the proxy configuration."}
