"""A loopback-only console for the existing saved-session test workflow."""

import argparse
from datetime import datetime, timezone
import hmac
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .errors import SessionError, REQUEST_FAILURE_STAGES, safe_request_failure, safe_session_storage_failure
from .proxy import load_packetstream_proxy, save_packetstream_credentials
from .proxy_pool import StickyProxyPool, PREPARATION_MAX_INPUT_BYTES, save_preparation_routes
from .proxy_test_route import MAX_TEST_ROUTE_BYTES, MAX_TEST_ROUTES, load_test_pool, save_test_route
from .test_settings import read_test_song_id, write_test_song_id
from .ui_jobs import JobBusyError, JobManager, JobValidationError, MAX_WORKER_REQUEST, MAX_FAILURE_REQUEST, _public_proxy_failure, safe_network_usage, safe_preparation_storage_failures
from .vault import AccountVault, DEFAULT_VAULT_PATH

ASSETS = Path(__file__).with_name("ui")
MAX_BODY = 512 * 1024
# JSON may escape quotes/backslashes or non-ASCII credentials. The decoded
# route still has its independent 1 MiB limit, including before de-duplication.
MAX_TEST_PROXY_BODY = 3 * MAX_TEST_ROUTE_BYTES + 64 * 1024
MAX_PREPARATION_PROXY_BODY = 3 * PREPARATION_MAX_INPUT_BYTES + 64 * 1024
MAX_REPORT = 2 * 1024 * 1024
MAX_ACTIVE_TEST_DESCRIPTORS = 500
REPORT_NAME = re.compile(
    r"(?:account-[1-9][0-9]*\.(?:test-(?:like|play-record)(?:-batch)?|http-playback|like-readiness)-report|"
    r"test-(?:like|play-record)\.accounts-batch-report|accounts-prepare-tests-report|"
    r"accounts-last-operation|packetstream-(?:egypt|browser-proxy)-report|ui-last-job)\.json\Z"
)
# Existing reports come from several probes. Expose outcome fields only, never
# arbitrary request/response data or secrets from an older probe.
REPORT_KEYS = frozenset("""
    checked_at_utc tested_at_utc started_at finished_at created_at_utc id action
    source_row account_row song_id passed phase failed_phase failed_row failed_test
    selected_rows prepared_rows requested_accounts prepared_account_count attempted_accounts
    completed_accounts requested_tests completed_tests attempted_tests tests_per_account
    active_row active_test dry_run browser headless reduce_browser_data no_browser preparation_method connection automatic_retry automatic_retries
    results progress completed total status message error code error_code error_type pause_reason ui_read_failure
    provider country requested_country observed_country endpoint sticky sticky_session_requested
    configured country_verified proxy_used used_proxy proxy_connect_http_status proxy_connect_status
    http_status api_status operations without_session authentication_rejected authenticated
    server_account_identity_verified song_metadata_verified metadata_verified metadata_duration_seconds
    negative_control_passed browser_required password_required browser_used audio_requested audio_bytes
    play_events_sent like_events_sent browser_country_verified http_country_check proxy exit_check
    event_attempted event_attempts event_accepted event_result event_http_status mutation_attempted
    mutation_attempts mutation_accepted mutation_result mutation_http_status persisted_state_verified
    liked_before liked_after state_read_method reported_play_seconds reported_play_fraction
    downstream_statistics_verified elapsed_seconds bandwidth measurement scope get_song play_song total
    request_count request_bytes request_body_bytes response_body_bytes response_header_bytes
    measurement_complete download_bytes upload_bytes header_bytes http_transfer_bytes balance_verified
    proxy_requests_attempted origin_requests_sent saved_session_read_attempted saved_session_read_worked
    egypt_route_verified proxy_access_worked basic_proxy_auth_worked rejection_also_without_country_selection
    checks operation account_metadata readiness transport test_rows ready_rows accounts state session_saved
    records unique_accounts duplicate_rows sessions_saved title artist duration_seconds
    rows count proxy_egypt report_code test_number result_unknown upload_body_bytes download_body_bytes
    song_duration_seconds
    workers requested_workers effective_workers failure_limit_scope max_consecutive_failures consecutive_failures active_workers active_rows
    attempted succeeded failed skipped completed_tests stop_reason results_total results_truncated
    active_tests proxy_test_session proxy_mode proxy_failure failure_kind curl_code country_check_attempts
    pool_size proxy_pool_size proxy_route_number proxy_route_assignments
    connection_pending retried provider_retries account_failed outcome retry_count provider_attempts
    attempt_history session_failure session_storage_failure connection_status connection_pending_rows account_failed_rows
    review_rows renewal_attempted renewal_completed renewal_unknown
    writes_attempted writes_accepted new_likes_verified already_liked_verified
    verification_pending verification_read_attempts verification_read_retries verification_read_retryable
    validation_pending verified_session_retained candidate_retained session_validation_attempts session_validation_retries attach_validation_attempts attach_validation_retries
    account_country registered_country selection_mode connection_pending_count account_failed_count preparation_held
    attention_required_rows infrastructure_failed_count infrastructure_failed_rows recent_session_storage_failures
    session_review_pending session_review_cleared session_reviews_cleared cleared_rows
    network_usage account_network_usage account_tests_completed
    history_skipped history_confirmed history_status stop_requested
    selected_accounts eligible_accounts already_liked_skipped like_verification_held unknown_like_held
    requested_tests requested_tests_per_account duplicate_like_skipped_tests history_skipped_tests history_held_tests cached_like_skips cached_like_holds
""".split())
SAFE_WORDS = frozenset("""
    ok failed succeeded queued running stopped complete preview preparing ready login_required check_failed
    direct proxy_egypt EG chrome cloakbrowser none browser http PacketStream accepted rejected unknown not_attempted
    skipped_already_liked session_validation legacy_source preflight account_identity metadata event
    mutation state_before state_after session_lookup session_recovery login validation proxy_preflight proxy_preflight_failed
    test_failed preparation_failed cancelled metadata_region_unavailable metadata_invalid event_incomplete
    event_rejected event_unknown state_http_failed mutation_failed mutation_incomplete mutation_unknown
    readback_not_liked journal_failed request_scope_invalid song_scope_invalid session_invalid
    relations playlists prepare play like check song proxy-check proxy_authentication_rejected
    test-like test-play-record GETuserrelations GETplaylists curl_cffi
    immediate_failure result_unknown consecutive_failure_limit completed_with_failures
    test_session authentication_rejected http_failure route_unverified country_unverified response_invalid transport_error
    completed_with_pending succeeded account_failed connection_pending verification_pending test_failed provider_issue
    checking retrying waiting provider_wait provider_retry session_renewal_unknown
    review-sessions session_review session_review_pending session_review_required
    history_skipped like_history_pending confirmed verification_pending write_unknown in_progress history_review
    history_already_liked history_verification_pending like_history_failed like_history_invalid test_proxy_invalid user_stop
    ui_status_unavailable ui_unavailable
""".split())


def safe_failure_review(value):
    """Expose definite account failures only, without arbitrary stored metadata."""
    accounts, seen = [], set()
    if type(value) is dict and type(value.get("accounts")) is list:
        for item in value["accounts"][:100_000]:
            if type(item) is not dict:
                continue
            row, code, stage = item.get("source_row"), item.get("failure_code"), item.get("failed_stage")
            if (type(row) is not int or not 1 <= row <= 2**31 - 1 or row in seen
                or type(code) is not str or code not in {"session_authentication_rejected", "session_identity_mismatch"}
                or type(stage) is not str or stage not in REQUEST_FAILURE_STAGES):
                continue
            stamp = item.get("failed_at")
            if type(stamp) is not str or len(stamp) > 64:
                continue
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                continue
            seen.add(row)
            accounts.append({"source_row": row, "failure_code": code, "failed_stage": stage, "failed_at": parsed.isoformat()})
    total = value.get("total") if type(value) is dict else None
    total = total if type(total) is int and len(accounts) <= total <= 2**31 - 1 else len(accounts)
    return {"accounts": accounts, "total": total, "failed_rows": [item["source_row"] for item in accounts]}


def safe_session_review(value):
    """Expose durable session holds separately from confirmed account failures."""
    accounts, seen = [], set()
    if type(value) is dict and type(value.get("accounts")) is list:
        for item in value["accounts"][:100_000]:
            if type(item) is not dict:
                continue
            row = item.get("source_row")
            failure = safe_request_failure(item.get("session_failure"))
            if (type(row) is not int or not 1 <= row <= 2**31 - 1 or row in seen
                or item.get("state") != "session_review_pending" or failure.get("failure_category") != "provider"
                or failure.get("stage") not in {"identity", "session_recovery_renewal"}
                or item.get("failure_code") != failure.get("code") or item.get("failed_stage") != failure.get("stage")):
                continue
            stamp, job_id = item.get("held_at"), item.get("job_id")
            if type(stamp) is not str or len(stamp) > 64 or type(job_id) is not str or not re.fullmatch(r"[0-9a-f]{32}", job_id):
                continue
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                continue
            seen.add(row)
            accounts.append({"source_row": row, "state": "session_review_pending", "failure_code": failure["code"],
                             "failed_stage": failure["stage"], "held_at": parsed.isoformat(), "job_id": job_id,
                             "session_failure": {name: failure[name] for name in ("code", "stage", "failure_category", "http_status", "curl_code") if name in failure}})
    total = value.get("total") if type(value) is dict else None
    total = total if type(total) is int and len(accounts) <= total <= 2**31 - 1 else len(accounts)
    rows = [item["source_row"] for item in accounts]
    blocked = value.get("held_rows") if type(value) is dict else None
    blocked = list(dict.fromkeys(row for row in blocked if type(row) is int and 1 <= row <= 2**31 - 1)) if type(blocked) is list else rows
    return {"accounts": accounts, "total": total, "held_rows": sorted(set(blocked).union(rows)), "session_review_rows": rows}


def safe_like_history(value, song_id, ready_rows):
    """Publish per-song local eligibility without account identity keys."""
    ready = set(row for row in ready_rows if type(row) is int and 1 <= row <= 2**31 - 1)
    accounts, seen = [], set()
    if type(value) is dict and value.get("song_id") == song_id and type(value.get("accounts")) is list:
        for item in value["accounts"]:
            if type(item) is not dict:
                continue
            row, state = item.get("source_row"), item.get("state")
            if type(row) is not int or row not in ready or row in seen or type(state) is not str or state not in {"confirmed", "verification_pending", "write_unknown", "in_progress"}:
                continue
            seen.add(row)
            account = {"source_row": row, "state": state}
            stamp = item.get("last_checked_at")
            if type(stamp) is str and len(stamp) <= 64:
                try:
                    checked = datetime.fromisoformat(stamp)
                    if checked.tzinfo is not None:
                        account["last_checked_at"] = checked.isoformat()
                except ValueError:
                    pass
            accounts.append(account)
    groups = {state: [item["source_row"] for item in accounts if item["state"] == state]
              for state in ("confirmed", "verification_pending", "write_unknown", "in_progress")}
    return {"song_id": song_id, "accounts": accounts, "counts": {**{state: len(rows) for state, rows in groups.items()}, "eligible": len(ready - seen)},
            "confirmed_rows": groups["confirmed"], "verification_pending_rows": groups["verification_pending"],
            "unknown_rows": groups["write_unknown"] + groups["in_progress"], "blocked_rows": sorted(seen), "eligible_rows": sorted(ready - seen)}


def _safe_attempt_history(value, proxy_pool_size=None):
    if type(value) is not list:
        return []
    result = []
    for item in value[:3]:
        if (type(item) is not dict or type(item.get("attempt")) is not int
            or not 1 <= item["attempt"] <= 3
            or type(item.get("outcome")) is not str
            or item["outcome"] not in {"succeeded", "provider_issue", "account_failed", "verification_pending", "session_review_pending", "test_failed", "history_skipped", "like_history_pending"}):
            continue
        safe = {"attempt": item["attempt"], "outcome": item["outcome"]}
        number = item.get("proxy_route_number")
        if type(number) is int and type(proxy_pool_size) is int and 1 <= number <= proxy_pool_size:
            safe["proxy_route_number"] = number
        failure = safe_request_failure(item.get("session_failure"))
        if failure:
            safe["session_failure"] = failure
        proxy = _public_proxy_failure(item.get("proxy_failure"))
        if proxy:
            safe["proxy_failure"] = proxy
        result.append(safe)
    return result


def safe_report(value, key="", depth=0, proxy_pool_size=None):
    if depth > 20:
        return None
    if key in {"network_usage", "account_network_usage"}:
        return safe_network_usage(value)
    if key == "account_tests_completed":
        return value if type(value) is int and 0 <= value <= MAX_WORKER_REQUEST else None
    if key == "account_country":
        return value if type(value) is str and value in {"EG", "LB"} else None
    if key == "registered_country":
        return value if type(value) is str and value in {"EG", "LB", "Other"} else "Other"
    if key == "failure_limit_scope":
        return value if type(value) is str and value in {"active_budget", "new_dispatch"} else None
    if key in {"workers", "requested_workers", "effective_workers", "active_workers"}:
        minimum = 1 if key in {"workers", "requested_workers"} else 0
        return value if type(value) is int and minimum <= value <= MAX_WORKER_REQUEST else None
    if key in {"max_consecutive_failures", "consecutive_failures"}:
        minimum = 1 if key == "max_consecutive_failures" else 0
        return value if type(value) is int and minimum <= value <= MAX_FAILURE_REQUEST else None
    if key == "selection_mode":
        return value if type(value) is str and value in {"random_country", "row_order"} else None
    if key in {"renewal_attempted", "renewal_completed", "renewal_unknown", "verification_read_retryable", "validation_pending", "verified_session_retained", "candidate_retained", "session_review_cleared", "history_skipped", "history_confirmed", "stop_requested"}:
        return value if type(value) is bool else None
    if key == "proxy_failure":
        return _public_proxy_failure(value)
    if key == "session_failure":
        return safe_request_failure(value)
    if key == "session_storage_failure":
        return safe_session_storage_failure(value)
    if key == "recent_session_storage_failures":
        return safe_preparation_storage_failures(value)
    if key == "ui_read_failure":
        from .country_preparation import safe_ui_read_failure
        return safe_ui_read_failure(value)
    if key == "pause_reason":
        from .ui_jobs import _public_report
        return _public_report({"pause_reason": value}).get("pause_reason")
    if key == "attempt_history":
        return _safe_attempt_history(value, proxy_pool_size)
    if key in {"connection_pending", "verification_pending", "session_review_pending", "session_reviews_cleared", "retried", "provider_retries", "account_failed", "writes_attempted", "writes_accepted", "new_likes_verified", "already_liked_verified"}:
        return value if type(value) is int and 0 <= value <= 2**31 - 1 else None
    if key == "retry_count":
        return value if type(value) is int and 0 <= value <= 2 else None
    if key == "provider_attempts":
        return value if type(value) is int and 1 <= value <= 3 else None
    if key == "verification_read_attempts":
        return value if type(value) is int and 0 <= value <= 3 else None
    if key == "verification_read_retries":
        return value if type(value) is int and 0 <= value <= 2 else None
    if key in {"session_validation_attempts", "attach_validation_attempts"}:
        return value if type(value) is int and 0 <= value <= 3 else None
    if key in {"session_validation_retries", "attach_validation_retries"}:
        return value if type(value) is int and 0 <= value <= 2 else None
    if key == "connection_status":
        return value if type(value) is str and value in {"checking", "retrying", "waiting"} else None
    if key == "outcome":
        return value if type(value) is str and value in {"succeeded", "account_failed", "connection_pending", "verification_pending", "session_review_pending", "test_failed", "history_skipped", "like_history_pending", "cancelled"} else None
    if key == "history_status":
        return value if type(value) is str and value in {"confirmed", "verification_pending", "write_unknown", "in_progress"} else None
    if key in {"selected_accounts", "eligible_accounts", "already_liked_skipped", "like_verification_held", "unknown_like_held", "requested_tests", "requested_tests_per_account", "duplicate_like_skipped_tests", "history_skipped_tests", "history_held_tests", "cached_like_skips", "cached_like_holds"}:
        return value if type(value) is int and 0 <= value <= MAX_WORKER_REQUEST else None
    if key in {"review_rows", "connection_pending_rows", "account_failed_rows", "cleared_rows"}:
        return list(value) if type(value) is list and all(type(row) is int and 1 <= row <= 2**31 - 1 for row in value) else []
    if key == "active_tests":
        result = []
        if type(value) is not list:
            return result
        for item in value[:MAX_ACTIVE_TEST_DESCRIPTORS]:
            if (type(item) is not dict
                or type(item.get("source_row")) is not int or not 1 <= item["source_row"] <= 2**31 - 1
                or type(item.get("test_number")) is not int or not 1 <= item["test_number"] <= 5):
                continue
            started = item.get("started_at")
            if type(started) is not str or len(started) > 64:
                continue
            try:
                parsed = datetime.fromisoformat(started)
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                descriptor = {"source_row": item["source_row"], "test_number": item["test_number"], "started_at": parsed.isoformat()}
                size, number = item.get("proxy_pool_size"), item.get("proxy_route_number")
                if (type(size) is int and type(number) is int and 1 <= number <= size <= MAX_TEST_ROUTES
                    and (proxy_pool_size is None or size == proxy_pool_size)):
                    descriptor.update(proxy_pool_size=size, proxy_route_number=number)
                for name in ("retry_count", "provider_attempts", "connection_status"):
                    fact = safe_report(item.get(name), name)
                    if fact is not None:
                        descriptor[name] = fact
                result.append(descriptor)
        return result
    if key == "proxy_route_assignments":
        if type(value) is not list or proxy_pool_size is None:
            return []
        return [{"source_row": item["source_row"], "proxy_route_number": item["proxy_route_number"]}
                for item in value[:100_000] if type(item) is dict
                and type(item.get("source_row")) is int and 1 <= item["source_row"] <= 2**31 - 1
                and type(item.get("proxy_route_number")) is int and 1 <= item["proxy_route_number"] <= proxy_pool_size]
    if key in {"pool_size", "proxy_pool_size", "proxy_route_number"}:
        maximum = proxy_pool_size if key == "proxy_route_number" else MAX_TEST_ROUTES
        return value if type(value) is int and maximum is not None and 1 <= value <= maximum else None
    if isinstance(value, dict):
        size = value.get("proxy_pool_size", proxy_pool_size)
        size = size if type(size) is int and 1 <= size <= MAX_TEST_ROUTES else None
        excluded = set()
        if proxy_pool_size is not None and "proxy_pool_size" in value and size != proxy_pool_size:
            excluded = {"proxy_pool_size", "proxy_route_number"}
            size = proxy_pool_size
        return {name: safe_report(item, name, depth + 1, size) for name, item in value.items() if name in REPORT_KEYS and name not in excluded}
    if isinstance(value, list):
        return [safe_report(item, key, depth + 1, proxy_pool_size) for item in value[:500]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if value in SAFE_WORDS or (key in {"song_id"} and value.isascii() and value.isdecimal()):
            return value
        if key in {"checked_at_utc", "tested_at_utc", "started_at", "finished_at", "created_at_utc"}:
            try:
                return datetime.fromisoformat(value).isoformat()
            except ValueError:
                return None
        if key == "endpoint" and value in {"https://proxy.packetstream.io:31111", "http://proxy.packetstream.io:31112"}:
            return value
        return "[redacted]"
    return None


class ConsoleService:
    def __init__(self, vault_path=DEFAULT_VAULT_PATH, manager=None):
        self.vault_path = Path(vault_path).resolve()
        self.manager = manager if manager is not None else JobManager(self.vault_path)
        self.lock = threading.RLock()
        self.test_settings_path = self.vault_path.parent / "test-settings.json"
        self._country_source_hash = None
        self._registered_countries = {}
        self._preparation_source_hash = None
        self._preparation_country_tags = {}

    def _preparation_availability(self, vault, summary, *, start_row=1):
        unavailable = {"available": False, "counts": {"EG": 0, "LB": 0, "all": 0}, "start_row": start_row}
        source_hash = summary.get("source_sha256") if type(summary) is dict else None
        tags_reader = getattr(vault, "preparation_country_tags", None)
        count_reader = getattr(vault, "preparation_availability", None)
        if (type(source_hash) is not str or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
                or not callable(tags_reader) or not callable(count_reader)):
            return unavailable
        try:
            with self.lock:
                if self._preparation_source_hash != source_hash:
                    self._preparation_source_hash = None
                    self._preparation_country_tags.clear()
                    tags = tags_reader()
                    if type(tags) is not dict or any(
                            type(row) is not int or not 1 <= row <= 2**31 - 1
                            or type(country) is not str or country not in {"EG", "LB", "Other"}
                            for row, country in tags.items()):
                        return unavailable
                    self._preparation_country_tags.update(tags)
                    self._preparation_source_hash = source_hash
                value = count_reader(self._preparation_country_tags, start_row=start_row)
            if (type(value) is not dict or value.get("available") is not True
                    or value.get("start_row") != start_row or type(value.get("counts")) is not dict):
                return unavailable
            counts = value["counts"]
            records = summary.get("records")
            if type(records) is not int or not 0 <= records <= 2**53 - 1:
                return unavailable
            if any(type(counts.get(name)) is not int or not 0 <= counts[name] <= records for name in ("EG", "LB", "all")):
                return unavailable
            return {"available": True, "counts": {name: counts[name] for name in ("EG", "LB", "all")}, "start_row": start_row}
        except (SessionError, OSError, ValueError, TypeError):
            return unavailable

    def preparation_availability(self, start_row=1):
        """Read a row-order pool size without selection, login or proxy checks."""
        if type(start_row) is not int or not 1 <= start_row <= 2**53 - 1:
            raise ValueError
        unavailable = {"available": False, "counts": {"EG": 0, "LB": 0, "all": 0}, "start_row": start_row}
        try:
            with AccountVault(self.vault_path) as vault:
                return self._preparation_availability(vault, vault.summary(), start_row=start_row)
        except (SessionError, OSError):
            return unavailable

    def _safe_cohort(self, vault, value, source_hash):
        """Publish safe cohort labels; immutable source metadata is cached once."""
        if type(value) is not dict:
            return {"test_rows": [], "ready_rows": [], "accounts": []}
        def rows(name):
            supplied = value.get(name)
            return list(dict.fromkeys(row for row in supplied if type(row) is int and 1 <= row <= 2**31 - 1)) if type(supplied) is list else []
        test_rows = rows("test_rows")
        cohort_rows = set(test_rows)
        ready_rows = [row for row in rows("ready_rows") if row in cohort_rows]
        stable_source = source_hash if type(source_hash) is str and re.fullmatch(r"[0-9a-f]{64}", source_hash) else None
        with self.lock:
            if stable_source is None or stable_source != self._country_source_hash:
                self._country_source_hash = stable_source
                self._registered_countries.clear()
            missing = set(ready_rows).difference(self._registered_countries)
            reader = getattr(vault, "registered_countries", None)
            if missing and callable(reader):
                countries = reader(missing)
                if type(countries) is dict:
                    for row in missing:
                        country = countries.get(row)
                        self._registered_countries[row] = country if type(country) is str and country in {"EG", "LB"} else "Other"
            accounts, seen = [], set()
            supplied = value.get("accounts")
            if type(supplied) is list:
                for item in supplied:
                    row = item.get("source_row") if type(item) is dict else None
                    if type(row) is not int or row not in cohort_rows or row in seen:
                        continue
                    seen.add(row)
                    state = item.get("state")
                    accounts.append({
                        "source_row": row,
                        "state": state if type(state) is str and state in {"ready", "login_required", "check_failed", "account_failed", "session_review_pending"} else "unknown",
                        "session_saved": item.get("session_saved") is True,
                        "registered_country": self._registered_countries.get(row, "Other"),
                    })
        return {"test_rows": test_rows, "ready_rows": ready_rows, "accounts": accounts}

    def reports(self):
        folder = self.vault_path.parent
        result = []
        if folder.is_dir():
            for path in folder.glob("*.json"):
                if not REPORT_NAME.fullmatch(path.name) or path.resolve().parent != folder.resolve():
                    continue
                info = path.stat()
                if info.st_size <= MAX_REPORT:
                    result.append({
                        "name": path.name, "size": info.st_size,
                        "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                    })
        return sorted(result, key=lambda item: item["modified_at"], reverse=True)[:100]

    def read_report(self, name):
        if not isinstance(name, str) or not REPORT_NAME.fullmatch(name):
            raise FileNotFoundError
        path = self.vault_path.parent / name
        if path.resolve().parent != self.vault_path.parent.resolve() or not path.is_file():
            raise FileNotFoundError
        if path.stat().st_size > MAX_REPORT:
            raise ValueError
        report = safe_report(json.loads(path.read_text(encoding="utf-8")))
        if name == "ui-last-job.json":
            report = self._enrich_job_usage(report)
        return {"name": name, "report": report}

    def _enrich_job_usage(self, job):
        if not isinstance(job, dict):
            return job
        # Historical reports remain unchanged on disk. The adapter supplies
        # explicit partial coverage for their older metadata/play counters.
        from .legacy_network_reporting import enrich_job_usage
        return enrich_job_usage(job, self.vault_path.parent)

    def job_snapshot(self):
        return self._enrich_job_usage(self.manager.snapshot())

    def state(self):
        vault_summary = {"records": 0, "unique_accounts": 0, "sessions_saved": 0}
        cohort = {"test_rows": [], "ready_rows": [], "accounts": []}
        failure_review = {"accounts": [], "failed_rows": [], "total": 0}
        session_review = {"accounts": [], "held_rows": [], "session_review_rows": [], "total": 0}
        song_id = read_test_song_id(self.test_settings_path)
        like_history = safe_like_history({}, song_id, [])
        preparation_availability = {"available": False, "counts": {"EG": 0, "LB": 0, "all": 0}, "start_row": 1}
        vault_available = False
        try:
            with AccountVault(self.vault_path) as vault:
                summary = vault.summary()
                vault_summary = {name: summary[name] for name in ("records", "unique_accounts", "sessions_saved", "duplicate_rows", "states")}
                preparation_availability = self._preparation_availability(vault, summary)
                cohort = self._safe_cohort(vault, vault.test_accounts(), summary.get("source_sha256"))
                if callable(getattr(vault, "failure_review", None)):
                    failure_review = safe_failure_review(vault.failure_review())
                if callable(getattr(vault, "session_review", None)):
                    session_review = safe_session_review(vault.session_review())
                if callable(getattr(vault, "like_history", None)):
                    like_history = safe_like_history(vault.like_history(song_id, rows=cohort["ready_rows"]), song_id, cohort["ready_rows"])
                else:
                    like_history = safe_like_history({}, song_id, cohort["ready_rows"])
                vault_available = True
        except (SessionError, OSError):
            pass
        proxy = {"configured": False, "provider": "PacketStream", "country": "EG"}
        try:
            profile = load_packetstream_proxy(self.vault_path.parent / "packetstream.dpapi")
            proxy = {"configured": True, **profile.summary()}
        except SessionError:
            pass
        sticky_pool = {"configured": False, "pool_size": 0}
        try:
            pool = StickyProxyPool.load(self.vault_path.parent / "packetstream-sticky-pool.dpapi")
            sticky_pool = {"configured": True, **pool.summary()}
        except SessionError:
            pass
        test_proxy = {"configured": False, "provider": "PacketStream", "country": "EG", "pool_size": 0}
        try:
            pool = load_test_pool(self.vault_path.parent / "packetstream-test-route.dpapi")
            test_proxy = {"configured": True, **pool.summary()}
        except SessionError:
            pass
        return {
            "vault": vault_summary, "vault_available": vault_available,
            "cohort": cohort, "failure_review": failure_review, "session_review": session_review, "like_history": like_history, "preparation_availability": preparation_availability, "proxy": proxy, "sticky_pool": sticky_pool, "test_proxy": test_proxy, "test_song_id": song_id,
            "limits": {"accounts": 5, "test_accounts": len(cohort["ready_rows"]), "tests_per_account": 5,
                       "workers": None, "preparation_workers": None, "worker_integer_max": MAX_WORKER_REQUEST,
                       "max_consecutive_failures": None, "failure_integer_max": MAX_FAILURE_REQUEST},
            "job_controls": {"stop_preparation": getattr(self.manager, "supports_preparation_stop", False) is True},
            "job": self.job_snapshot(), "reports": self.reports(),
        }

    def submit(self, payload):
        with self.lock:
            return self.manager.submit(payload)

    def stop_job(self, payload):
        if type(payload) is not dict or set(payload) != {"job_id"}:
            raise ValueError
        with self.lock:
            return {"job": self.manager.request_stop(payload["job_id"])}

    def save_test_song(self, payload):
        if set(payload) != {"song_id"}:
            raise ValueError
        with self.lock:
            job = self.manager.snapshot()
            if job and job["status"] in {"queued", "running"}:
                raise JobBusyError("An operation is running.")
            song_id = write_test_song_id(payload["song_id"], self.test_settings_path)
            return {"test_song_id": song_id}

    def find(self, payload):
        if set(payload) != {"email"} or not isinstance(payload["email"], str) or len(payload["email"]) > 254:
            raise ValueError
        with AccountVault(self.vault_path) as vault:
            return {"source_rows": vault.find(payload["email"])}

    def save_proxy(self, payload):
        if set(payload) != {"username", "auth_key"}:
            raise ValueError
        if not all(isinstance(payload[name], str) and len(payload[name]) <= 256 for name in payload):
            raise ValueError
        with self.lock:
            job = self.manager.snapshot()
            if job and job["status"] in {"queued", "running"}:
                raise JobBusyError("An operation is running.")
            return {"configured": True, **save_packetstream_credentials(
                payload["username"], payload["auth_key"], self.vault_path.parent / "packetstream.dpapi",
            )}

    def save_test_proxy(self, payload):
        if type(payload) is not dict or set(payload) != {"route"} or type(payload["route"]) is not str:
            raise ValueError
        with self.lock:
            job = self.manager.snapshot()
            if job and job["status"] in {"queued", "running"}:
                raise JobBusyError("An operation is running.")
            return {"configured": True, **save_test_route(
                payload["route"], self.vault_path.parent / "packetstream-test-route.dpapi",
            )}

    def save_preparation_proxy(self, payload):
        if type(payload) is not dict or set(payload) != {"routes"} or type(payload["routes"]) is not str:
            raise ValueError
        with self.lock:
            job = self.manager.snapshot()
            if job and job["status"] in {"queued", "running"}:
                raise JobBusyError("An operation is running.")
            return {"configured": True, **save_preparation_routes(
                payload["routes"], self.vault_path.parent / "packetstream-sticky-pool.dpapi",
            )}

    def remove_preparation_proxy(self, payload):
        if type(payload) is not dict or payload:
            raise ValueError
        with self.lock:
            job = self.manager.snapshot()
            if job and job["status"] in {"queued", "running"}:
                raise JobBusyError("An operation is running.")
            try:
                (self.vault_path.parent / "packetstream-sticky-pool.dpapi").unlink(missing_ok=True)
            except OSError:
                raise SessionError("The preparation route pool could not be removed.") from None
            return {"configured": False, "pool_size": 0}


class ConsoleHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        self._body_consumed = False

    def log_message(self, *_):
        # URLs, request bodies and credentials must never appear in access logs.
        pass

    def _send(self, status, value, mime="application/json; charset=utf-8"):
        data = json.dumps(value, allow_nan=False).encode() if mime.startswith("application/json") else value
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Anghami-Test-Console", "1")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, code, message):
        # Drain small rejected POSTs before closing the socket. On Windows,
        # unread request bytes can reset the connection and hide the response.
        if self.command == "POST" and not self._body_consumed:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) == 1 and not self.headers.get("Transfer-Encoding"):
                value = lengths[0]
                if value.isascii() and value.isdecimal() and int(value) <= max(MAX_BODY, MAX_TEST_PROXY_BODY) * 2:
                    try:
                        self.rfile.read(int(value))
                    except (OSError, TimeoutError):
                        pass
            self._body_consumed = True
        self._send(status, {"error": {"code": code, "message": message}})

    def _allowed_request(self, api=False):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self._error(403, "host_rejected", "Use this console's local address.")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allowed_origins:
            self._error(403, "origin_rejected", "This request did not come from the local console.")
            return False
        if api:
            token = self.headers.get("X-App-Token", "")
            if not token.isascii() or not hmac.compare_digest(token, self.server.token):
                self._error(403, "token_rejected", "Reload the console and try again.")
                return False
        return True

    def do_GET(self):
        try:
            parts = urlsplit(self.path)
        except ValueError:
            self._error(400, "invalid_path", "The requested local address is invalid.")
            return
        if not self._allowed_request(api=parts.path.startswith("/api/")):
            return
        try:
            if parts.path == "/api/state" and not parts.query:
                self._send(200, self.server.service.state())
            elif parts.path == "/api/preparation-availability":
                query = parse_qs(parts.query, keep_blank_values=True)
                if set(query) != {"start_row"} or len(query["start_row"]) != 1:
                    self._error(400, "invalid_start_row", "Enter a valid starting source row.")
                    return
                raw = query["start_row"][0]
                if (re.fullmatch(r"[1-9][0-9]{0,15}", raw) is None or int(raw) > 2**53 - 1):
                    self._error(400, "invalid_start_row", "Enter a valid starting source row.")
                    return
                self._send(200, self.server.service.preparation_availability(int(raw)))
            elif parts.path == "/api/job" and not parts.query:
                reader = getattr(self.server.service, "job_snapshot", None)
                self._send(200, {"job": reader() if callable(reader) else self.server.service.manager.snapshot()})
            elif parts.path == "/api/reports":
                query = parse_qs(parts.query, keep_blank_values=True)
                if set(query) != {"name"} or len(query["name"]) != 1:
                    raise FileNotFoundError
                self._send(200, self.server.service.read_report(query["name"][0]))
            elif parts.path in {"/", "/index.html", "/styles.css", "/selection.js", "/app.js"} and not parts.query:
                name = "index.html" if parts.path in {"/", "/index.html"} else parts.path[1:]
                raw = (ASSETS / name).read_bytes()
                if name == "index.html":
                    raw = raw.replace(b"__APP_TOKEN__", self.server.token.encode())
                mime = {"index.html": "text/html; charset=utf-8", "styles.css": "text/css; charset=utf-8", "selection.js": "text/javascript; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}[name]
                self._send(200, raw, mime)
            else:
                self._error(404, "not_found", "This item is not available.")
        except FileNotFoundError:
            self._error(404, "not_found", "This item is not available.")
        except Exception:
            self._error(500, "read_failed", "The local data could not be loaded. Refresh and try again.")

    def do_POST(self):
        try:
            parts = urlsplit(self.path)
        except ValueError:
            self._error(400, "invalid_path", "The requested local address is invalid.")
            return
        if not self._allowed_request(api=True):
            return
        if parts.query or parts.path not in {"/api/jobs", "/api/jobs/stop", "/api/find", "/api/proxy", "/api/test-song", "/api/test-proxy", "/api/preparation-proxy", "/api/preparation-proxy/remove"}:
            self._error(404, "not_found", "This action is not available.")
            return
        if self.headers.get_content_type() != "application/json":
            self._error(415, "json_required", "Send this action from the console.")
            return
        try:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError
            length_text = self.headers.get("Content-Length", "")
            if not length_text.isascii() or not length_text.isdecimal():
                raise ValueError
            length = int(length_text)
            body_limit = MAX_TEST_PROXY_BODY if parts.path == "/api/test-proxy" else MAX_PREPARATION_PROXY_BODY if parts.path == "/api/preparation-proxy" else MAX_BODY
            if length > body_limit:
                self._error(413, "body_too_large", "This request is too large.")
                return
            raw = self.rfile.read(length)
            self._body_consumed = True
            if len(raw) != length:
                raise ValueError
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except Exception:
            self._error(400, "invalid_json", "The action could not be read. Check its fields and try again.")
            return
        try:
            if parts.path == "/api/jobs":
                self._send(202, {"job": self.server.service.submit(payload)})
            elif parts.path == "/api/jobs/stop":
                self._send(202, self.server.service.stop_job(payload))
            elif parts.path == "/api/find":
                self._send(200, self.server.service.find(payload))
            elif parts.path == "/api/test-song":
                self._send(200, self.server.service.save_test_song(payload))
            elif parts.path == "/api/test-proxy":
                self._send(200, self.server.service.save_test_proxy(payload))
            elif parts.path == "/api/preparation-proxy":
                self._send(200, self.server.service.save_preparation_proxy(payload))
            elif parts.path == "/api/preparation-proxy/remove":
                self._send(200, self.server.service.remove_preparation_proxy(payload))
            else:
                self._send(200, self.server.service.save_proxy(payload))
        except JobBusyError:
            self._error(409, "busy", "Another operation is running. Wait for it to finish.")
        except (JobValidationError, ValueError, SessionError):
            self._error(400, "invalid_action", "Check the selected accounts, counts and settings, then try again.")
        except Exception:
            self._error(500, "action_failed", "The action could not be started. Check the latest report.")


def create_server(*, port=0, service=None):
    server = ThreadingHTTPServer(("127.0.0.1", port), ConsoleHandler)
    server.daemon_threads = True
    server.token = secrets.token_urlsafe(32)
    server.service = ConsoleService() if service is None else service
    actual_port = server.server_address[1]
    server.base_url = f"http://127.0.0.1:{actual_port}"
    server.allowed_hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
    server.allowed_origins = {f"http://{host}" for host in server.allowed_hosts}
    return server


def _existing_console(port):
    if not port:
        return False
    connection = HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        recognized = response.status == 200 and response.getheader("X-Anghami-Test-Console") == "1"
        if recognized:
            response.read(64 * 1024)
        return recognized
    except (OSError, ValueError, HTTPException):
        return False
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("Choose a valid local port.")
    try:
        if _existing_console(args.port):
            url = f"http://127.0.0.1:{args.port}"
            print(f"Anghami Test Console: {url}", flush=True)
            print("Using the console already running.", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        try:
            server = create_server(port=args.port)
        except OSError:
            server = create_server(port=0)
        print(f"Anghami Test Console: {server.base_url}", flush=True)
        print("Keep this process running while using the console.", flush=True)
        if not args.no_browser:
            webbrowser.open(server.base_url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except Exception:
        print("The local console could not start. Check the environment and try again.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
