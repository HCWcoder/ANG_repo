"""One bounded local UI job at a time, with credential-free progress reports."""

from copy import deepcopy
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import threading
from time import monotonic
from uuid import uuid4

from .errors import RequestFailure, SessionError, safe_request_failure, safe_session_storage_failure
from .play_record import TEST_SONG_ID, _journal
from .proxy import PacketStreamProxy, ProxyCountryError, load_packetstream_proxy, safe_proxy_country_failure
from .proxy_pool import StickyProxyPool
from .proxy_test_route import MAX_TEST_ROUTES, load_test_pool
from .test_settings import read_test_song_id, validate_test_song_id
from .vault import AccountVault, DEFAULT_VAULT_PATH
from .bandwidth import measure_network_usage


class JobValidationError(SessionError):
    """A UI request was rejected before a worker was created."""


class JobBusyError(SessionError):
    """There is already a queued or running job."""


_ACTIONS = frozenset({"prepare", "preview", "play", "like", "check", "song", "login", "proxy-check", "review-sessions"})
_ROW_ACTIONS = frozenset({"play", "like", "check", "song", "login", "review-sessions"})
_WORKBENCH_ACTIONS = frozenset({"play", "like", "check", "song"})
_FIELDS = frozenset({"action", "rows", "review_rows", "count", "start_row", "account_country", "browser", "headless", "proxy_egypt", "proxy_sticky_pool", "proxy_test_session", "song_id", "reduce_browser_data", "no_browser", "workers", "max_consecutive_failures", "resume_preparation", "with_audio"})
MAX_WORKER_REQUEST = 2**53 - 1
# Compatibility names describe numeric integrity, not operating concurrency.
MAX_TEST_WORKERS = MAX_WORKER_REQUEST
MAX_PREPARATION_WORKERS = MAX_WORKER_REQUEST
MAX_FAILURE_REQUEST = 2**53 - 1
MAX_TEST_FAILURES = MAX_FAILURE_REQUEST
MAX_RETAINED_RESULTS = 500
MAX_PROVIDER_ATTEMPTS = 3
_WRITE_COUNTERS = ("writes_attempted", "writes_accepted", "new_likes_verified", "already_liked_verified")
_PROXY_CERTIFICATE_CODES = frozenset({51, 58, 60, 77, 82, 83, 90, 91, 98})
_NETWORK_BYTE_KEYS = (
    "request_bytes", "request_header_bytes", "upload_body_bytes", "download_body_bytes", "response_header_bytes",
    "sent_bytes", "received_bytes", "total_bytes", "direct_bytes", "proxy_bytes", "unknown_route_bytes",
)
_NETWORK_COUNT_KEYS = ("request_count", "measured_requests", "partial_requests", "unmeasured_requests", "transport_errors")
_NETWORK_SUMMARY_COUNT_KEYS = ("sampled_tests", "completed_tests", "sampled_accounts", "planned_tests")


def safe_network_usage(value):
    """Rebuild numeric transfer facts without accepting URLs, headers, or secrets."""
    if type(value) is not dict:
        return {}
    result = {}
    for key in (*_NETWORK_BYTE_KEYS, *_NETWORK_COUNT_KEYS):
        item = value.get(key)
        if type(item) is not int or not 0 <= item <= MAX_WORKER_REQUEST:
            return {}
        result[key] = item
    if (result["total_bytes"] != result["sent_bytes"] + result["received_bytes"]
        or result["total_bytes"] != result["direct_bytes"] + result["proxy_bytes"] + result["unknown_route_bytes"]
        or result["request_count"] != sum(result[key] for key in ("measured_requests", "partial_requests", "unmeasured_requests"))
        or result["transport_errors"] > result["request_count"]):
        return {}
    scope = value.get("scope")
    if type(scope) is not str or scope not in {"python_http", "legacy_play_requests", "mixed_http"}:
        return {}
    available = result["measured_requests"] + result["partial_requests"]
    measurement = "unavailable" if not available else "partial" if (
        result["partial_requests"] or result["unmeasured_requests"] or scope != "python_http"
    ) else "measured"
    declared = value.get("measurement")
    if type(declared) is not str or declared not in {"measured", "partial", "unavailable"}:
        return {}
    result.update(measurement=measurement, scope=scope)
    # Prices and billing estimates are recomputed from counters, never trusted
    # from arbitrary diagnostic metadata. Decimal GB is the user's $1 rate.
    result["price_usd_per_gb"] = 1.0
    if measurement != "unavailable":
        result["cost_usd"] = result["proxy_bytes"] / 1_000_000_000
    if any(key in value for key in _NETWORK_SUMMARY_COUNT_KEYS):
        for key in _NETWORK_SUMMARY_COUNT_KEYS:
            item = value.get(key)
            if type(item) is not int or not 0 <= item <= MAX_WORKER_REQUEST:
                return {}
            result[key] = item
        if not (result["sampled_accounts"] <= result["sampled_tests"] <= result["completed_tests"] <= result["planned_tests"]):
            return {}
        if available and result["sampled_tests"] < result["completed_tests"]:
            result["measurement"] = "partial"
        for key, maximum in (("setup_bytes", result["total_bytes"]), ("setup_proxy_bytes", result["proxy_bytes"])):
            item = value.get(key, 0)
            if type(item) is not int or not 0 <= item <= maximum:
                return {}
            result[key] = item
        if result["sampled_tests"]:
            test_bytes = result["total_bytes"] - result["setup_bytes"]
            test_proxy_bytes = result["proxy_bytes"] - result["setup_proxy_bytes"]
            average, average_cost = test_bytes / result["sampled_tests"], test_proxy_bytes / result["sampled_tests"] / 1_000_000_000
            remaining = result["planned_tests"] - result["completed_tests"]
            result.update(avg_bytes_per_test=average, avg_cost_usd_per_test=average_cost,
                          estimated_total_bytes=result["setup_bytes"] + average * result["planned_tests"],
                          estimated_total_cost_usd=result["setup_proxy_bytes"] / 1_000_000_000 + average_cost * result["planned_tests"],
                          estimated_remaining_bytes=average * remaining, estimated_remaining_cost_usd=average_cost * remaining)
            if result["sampled_accounts"]:
                result.update(avg_bytes_per_account=test_bytes / result["sampled_accounts"],
                              avg_cost_usd_per_account=test_proxy_bytes / result["sampled_accounts"] / 1_000_000_000)
    return result


def _empty_network_usage(scope="python_http"):
    return {**dict.fromkeys((*_NETWORK_BYTE_KEYS, *_NETWORK_COUNT_KEYS), 0),
            "measurement": "unavailable", "scope": scope}


def _merge_network_usage(left, right):
    left, right = safe_network_usage(left), safe_network_usage(right)
    if not left:
        return deepcopy(right) if right else _empty_network_usage()
    if not right:
        return deepcopy(left)
    result = {key: left[key] + right[key] for key in (*_NETWORK_BYTE_KEYS, *_NETWORK_COUNT_KEYS)}
    scopes = {item["scope"] for item in (left, right) if item["request_count"]}
    scope = scopes.pop() if len(scopes) == 1 else "mixed_http" if scopes else "python_http"
    result["scope"] = scope
    result["measurement"] = "unavailable" if not result["measured_requests"] + result["partial_requests"] else "partial" if (
        result["partial_requests"] or result["unmeasured_requests"] or scope != "python_http"
    ) else "measured"
    return safe_network_usage(result)


def _network_summary(test_usage, *, total_tests, completed_tests, sampled_tests, sampled_accounts, setup_usage=None):
    tests = safe_network_usage(test_usage) or _empty_network_usage()
    setup = safe_network_usage(setup_usage) or _empty_network_usage()
    result = _merge_network_usage(tests, setup)
    result.update(price_usd_per_gb=1.0, sampled_tests=sampled_tests, completed_tests=completed_tests, planned_tests=total_tests,
                  sampled_accounts=sampled_accounts, setup_bytes=setup["total_bytes"], setup_proxy_bytes=setup["proxy_bytes"])
    return safe_network_usage(result)


def summarize_network_usage(samples, *, total_tests, completed_tests, setup_usage=None):
    """Aggregate every supplied finished test, including reports beyond the UI tail."""
    usage, sampled, accounts = _empty_network_usage(), 0, set()
    for row, item in samples:
        safe = safe_network_usage(item)
        if not safe:
            continue
        usage = _merge_network_usage(usage, safe)
        if safe["measured_requests"] + safe["partial_requests"]:
            sampled += 1
            if _positive_int(row):
                accounts.add(row)
    return _network_summary(usage, total_tests=total_tests, completed_tests=completed_tests,
                            sampled_tests=sampled, sampled_accounts=len(accounts), setup_usage=setup_usage)


def legacy_network_usage(report, *, proxy_used=False):
    """Old play diagnostics measure only metadata/play calls, never the whole job."""
    if type(report) is not dict or type(report.get("bandwidth")) is not dict:
        return {}
    parts = report["bandwidth"]
    usage = _empty_network_usage("legacy_play_requests")
    def valid_count(item):
        return type(item) is dict and type(item.get("request_count")) is int and 0 <= item["request_count"] <= MAX_WORKER_REQUEST
    def complete(item):
        return valid_count(item) and all(type(item.get(key)) is int and 0 <= item[key] <= MAX_WORKER_REQUEST
            for key in ("request_bytes", "upload_body_bytes", "download_body_bytes", "response_header_bytes"))
    rows = [parts.get("total")] if complete(parts.get("total")) else [parts.get(key) for key in ("get_song", "play_song")]
    for item in rows:
        if not valid_count(item):
            continue
        values = {"request_count": item["request_count"]}
        known = []
        for key in ("request_bytes", "upload_body_bytes", "download_body_bytes", "response_header_bytes"):
            number = item.get(key)
            available = type(number) is int and 0 <= number <= MAX_WORKER_REQUEST
            known.append(available)
            values[key] = number if available else 0
        if not values["request_count"]:
            continue
        sent = max(values["request_bytes"], values["upload_body_bytes"])
        received = values["download_body_bytes"] + values["response_header_bytes"]
        one = {**_empty_network_usage("legacy_play_requests"), **values,
               "request_header_bytes": max(0, sent - values["upload_body_bytes"]),
               "sent_bytes": sent, "received_bytes": received, "total_bytes": sent + received,
               "proxy_bytes" if proxy_used is True else "direct_bytes": sent + received,
               "partial_requests": values["request_count"] if any(known) else 0,
               "unmeasured_requests": values["request_count"] if not any(known) else 0,
               "measurement": "partial" if any(known) else "unavailable"}
        usage = _merge_network_usage(usage, one)
    return usage if usage["request_count"] else {}
_OPERATIONAL_CODES = frozenset({
    "metadata_region_unavailable", "metadata_invalid", "preflight_failed", "transport_failed",
    "http_rejected", "response_invalid", "event_rejected", "state_http_failed", "state_api_failed",
    "state_read_failed", "mutation_rejected",
})
_PHASES = frozenset({
    "queued", "opening_vault", "validating_rows", "proxy_preflight", "preparing", "preview",
    "session_lookup", "session_recovery", "login", "validation", "session_validation", "preflight", "legacy_source",
    "account_identity", "metadata", "event", "state_before", "mutation", "state_after", "executing",
    "complete", "failed", "stopped", "proxy_check", "checking", "song_metadata",
    "provider_retry", "provider_wait", "connection_pending", "verification_pending", "account_failed",
    "session_review", "history_review", "verification", "audio",
})
_BOOL_KEYS = frozenset({
    "passed", "authenticated", "negative_control_passed", "server_account_identity_verified",
    "metadata_verified", "event_attempted", "event_accepted", "mutation_attempted", "mutation_accepted",
    "liked_before", "liked_after", "persisted_state_verified", "browser_required", "password_required",
    "automatic_retry", "synthetic", "downstream_statistics_verified", "dry_run", "headless",
    "session_saved", "country_verified", "proxy_used", "authentication_rejected", "measurement_complete",
    "reduce_browser_data", "no_browser", "proxy_sticky_pool", "proxy_test_session", "result_unknown",
    "renewal_attempted", "renewal_completed", "renewal_unknown",
    "verification_read_retryable", "validation_pending", "verified_session_retained", "candidate_retained",
    "session_review_cleared",
    "history_skipped", "history_confirmed", "stop_requested", "audio_requested",
})
_NUMBER_KEYS = frozenset({
    "source_row", "test_number", "song_duration_seconds", "metadata_duration_seconds", "reported_play_seconds",
    "reported_play_fraction", "elapsed_seconds", "event_attempts", "mutation_attempts", "audio_bytes",
    "http_status", "event_http_status", "metadata_http_status", "mutation_http_status",
    "proxy_connect_http_status", "requested_accounts", "prepared_account_count", "attempted_accounts",
    "active_row", "failed_row", "like_events_sent", "play_events_sent", "request_count", "request_bytes",
    "upload_body_bytes", "download_body_bytes", "response_header_bytes", "retry_count", "provider_attempts",
    "verification_read_attempts", "verification_read_retries",
    "session_validation_attempts", "session_validation_retries", "attach_validation_attempts", "attach_validation_retries",
    "workers", "requested_workers", "effective_workers", "active_workers", "max_consecutive_failures", "consecutive_failures",
    "connection_pending_count", "account_failed_count", "preparation_held", "infrastructure_failed_count",
    "session_review_pending", "session_reviews_cleared",
    "selected_accounts", "eligible_accounts", "already_liked_skipped", "like_verification_held", "unknown_like_held",
    "requested_tests", "requested_tests_per_account", "duplicate_like_skipped_tests", "history_skipped_tests", "history_held_tests",
    "cached_like_skips", "cached_like_holds",
    "public_play_count_before", "public_play_count_after", "public_play_count_change", "downstream_observation_seconds",
    "audio_decoded_seconds", "media_http_status",
})
_ENUMS = {
    "phase": _PHASES, "failed_phase": _PHASES,
    "api_status": frozenset({"ok", "failed", "unknown", "not_attempted"}),
    "event_result": frozenset({"accepted", "rejected", "unknown", "not_attempted"}),
    "mutation_result": frozenset({"accepted", "rejected", "unknown", "not_attempted", "skipped_already_liked"}),
    "browser": frozenset({"chrome", "cloakbrowser", "none"}),
    "preparation_method": frozenset({"browser", "http"}),
    "connection": frozenset({"direct", "proxy_egypt"}),
    "proxy_mode": frozenset({"test_session"}),
    "transport": frozenset({"curl_cffi"}),
    "outcome": frozenset({"succeeded", "account_failed", "connection_pending", "verification_pending", "session_review_pending", "test_failed", "history_skipped", "like_history_pending", "cancelled"}),
    "history_status": frozenset({"confirmed", "verification_pending", "write_unknown", "in_progress"}),
    "country": frozenset({"EG"}),
    "account_country": frozenset({"EG", "LB"}),
    "selection_mode": frozenset({"random_country", "row_order"}),
    "failure_limit_scope": frozenset({"active_budget", "new_dispatch"}),
    "pause_reason": frozenset({"stop_requested", "limit_reached", "ui_busy", "ui_activity", "ui_unavailable",
                               "source_changed", "scope_mismatch", "unknown_attempt", "repeated_failures",
                               "infrastructure_failed", "browser_cleanup_failed", "browser_unavailable", "proxy_preflight_failed"}),
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
        "preparation_failed", "cancelled", "connection_pending", "verification_pending", "request_transport_failed", "request_http_failed",
        "request_rate_limited", "session_authentication_rejected", "session_response_invalid", "session_control_failed", "session_identity_mismatch", "session_renewal_unknown", "request_proxy_unverified",
        "history_already_liked", "history_verification_pending",
        "like_history_failed",
    }),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value):
    return type(value) is int and 1 <= value <= 2**31 - 1


def _write_counts(action, report):
    """Count recorded writes separately from completed or passing checks."""
    counts = dict.fromkeys(_WRITE_COUNTERS, 0)
    if action not in {"play", "like"} or not isinstance(report, dict):
        return counts
    if (action == "like" and report.get("history_skipped") is True
        and report.get("mutation_attempted") is False and type(report.get("mutation_attempts")) is int
        and report["mutation_attempts"] == 0 and report.get("mutation_result") == "not_attempted"):
        return counts
    prefix = "event" if action == "play" else "mutation"
    attempted = report.get(f"{prefix}_attempted") is True
    counts["writes_attempted"] = int(attempted)
    counts["writes_accepted"] = int(attempted and report.get(f"{prefix}_accepted") is True
                                    and report.get(f"{prefix}_result") == "accepted")
    if action == "like":
        verified = (report.get("passed") is True and report.get("persisted_state_verified") is True
                    and report.get("liked_after") is True)
        counts["new_likes_verified"] = int(verified and report.get("liked_before") is False
                                           and counts["writes_accepted"] == 1)
        counts["already_liked_verified"] = int(verified and report.get("liked_before") is True
                                               and report.get("mutation_attempted") is False
                                               and report.get("mutation_result") == "skipped_already_liked")
    return counts


def _action_summary(job):
    """A finished attempt is not evidence that a play or like was sent."""
    action = job.get("action")
    if action not in {"play", "like"}:
        return ""
    attempted = job.get("writes_attempted", 0)
    pending = job.get("connection_pending", 0)
    label = "likes" if action == "like" else "play records"
    parts = [f"0 {label} sent" if not attempted else f"{attempted} {action} request(s) attempted"]
    if action == "like" and (attempted or job.get("already_liked_verified", 0)):
        parts.extend((f"{job.get('new_likes_verified', 0)} new likes verified",
                      f"{job.get('already_liked_verified', 0)} already liked"))
    elif action == "play" and attempted:
        parts.append(f"{job.get('writes_accepted', 0)} play records accepted")
    if pending:
        parts.append(f"{pending} connections pending")
    if job.get("verification_pending", 0):
        parts.append(f"{job['verification_pending']} accepted likes awaiting verification")
    if job.get("session_review_pending", 0):
        parts.append(f"{job['session_review_pending']} accounts held for saved-session review")
    if action == "like":
        if job.get("already_liked_skipped", 0):
            parts.append(f"{job['already_liked_skipped']} accounts skipped from confirmed song history")
        if job.get("like_verification_held", 0) + job.get("unknown_like_held", 0):
            parts.append(f"{job.get('like_verification_held', 0) + job.get('unknown_like_held', 0)} accounts held for read-only like verification")
    return "; ".join(parts) + "."


def _validate(payload):
    if not isinstance(payload, dict) or not set(payload).issubset(_FIELDS):
        raise JobValidationError("Use the supported test controls only.")
    action = payload.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise JobValidationError("Select a supported action.")
    if action == "review-sessions" and not set(payload) <= {"action", "rows", "proxy_egypt"}:
        raise JobValidationError("Saved-session review accepts 1-5 held rows and Direct or the saved Egypt proxy only.")
    if "workers" in payload and action not in {"prepare", "play", "like"}:
        raise JobValidationError("Choose concurrency only for account preparation or play or like tests.")
    if "max_consecutive_failures" in payload and action not in {"play", "like"}:
        raise JobValidationError("Choose a failure limit only for play or like tests.")
    workers, maximum = payload.get("workers", 1), payload.get("max_consecutive_failures", 1)
    if type(workers) is not int or not 1 <= workers <= MAX_WORKER_REQUEST:
        raise JobValidationError("Workers must be a positive JavaScript-safe integer.")
    if type(maximum) is not int or not 1 <= maximum <= MAX_FAILURE_REQUEST:
        raise JobValidationError("Consecutive test failures must be a positive JavaScript-safe integer.")
    count = payload.get("count", 1)
    if action in {"prepare", "preview"}:
        if type(count) is not int or count < 1:
            raise JobValidationError("Accounts to add must be a whole number of at least 1.")
    elif type(count) is not int or not 1 <= count <= 5:
        raise JobValidationError("Choose a count from 1 to 5.")
    start = payload.get("start_row", 1)
    if not _positive_int(start):
        raise JobValidationError("The starting source row must be a positive integer.")
    browser = payload.get("browser", "chrome")
    if not isinstance(browser, str) or browser not in {"chrome", "cloakbrowser"}:
        raise JobValidationError("Choose Chrome or CloakBrowser.")
    for field in ("headless", "proxy_egypt", "proxy_sticky_pool", "proxy_test_session", "reduce_browser_data", "no_browser", "resume_preparation", "with_audio"):
        if field in payload and type(payload[field]) is not bool:
            raise JobValidationError("Browser and proxy selections must be true or false.")
    if "with_audio" in payload and action != "play":
        raise JobValidationError("Real audio delivery is only available for play tests.")
    if "no_browser" in payload and action not in {"prepare", "preview"}:
        raise JobValidationError("Choose a preparation method only for account preparation or preview.")
    no_browser = payload.get("no_browser", False)
    if action == "prepare" and workers > 1 and not no_browser:
        raise JobValidationError("Multiple preparation workers require browser-free preparation.")
    sticky_pool = payload.get("proxy_sticky_pool", False)
    test_session = payload.get("proxy_test_session", False)
    if "proxy_test_session" in payload and action not in _WORKBENCH_ACTIONS:
        raise JobValidationError("Choose a saved test route only for Workbench tests.")
    if test_session and sticky_pool:
        raise JobValidationError("Choose one proxy mode for this test.")
    if sticky_pool and (action not in {"prepare", "preview"} or not no_browser):
        raise JobValidationError("Choose the sticky route pool only for browser-free account preparation.")
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
    review_rows = payload.get("review_rows")
    account_country = payload.get("account_country")
    if "account_country" in payload and (
        action not in {"prepare", "preview"} or type(account_country) is not str
        or account_country not in {"EG", "LB"} or "start_row" in payload or "review_rows" in payload
    ):
        raise JobValidationError("Choose EG or LB for random registered-account preparation, or use starting rows or explicit failed-account review separately.")
    if "review_rows" in payload and (
        action != "prepare" or "start_row" in payload or "rows" in payload
        or not isinstance(review_rows, list) or not 1 <= len(review_rows) <= 5
        or any(not _positive_int(row) for row in review_rows) or len(set(review_rows)) != len(review_rows)
        or len(review_rows) != count
    ):
        raise JobValidationError("Select 1-5 distinct failed-account review rows and use their exact count.")
    if action == "prepare" and workers > 1 and account_country not in {"EG", "LB"}:
        raise JobValidationError("Multiple preparation workers require a selected registered account country.")
    if "resume_preparation" in payload and (
        action != "prepare" or not no_browser or workers < 2 or account_country not in {"EG", "LB"}
        or "start_row" in payload or "review_rows" in payload or "rows" in payload
    ):
        raise JobValidationError("A saved preparation continuation requires parallel browser-free country preparation.")
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
        "proxy_egypt": payload.get("proxy_egypt", False) or sticky_pool or test_session,
        "proxy_sticky_pool": sticky_pool,
        "proxy_test_session": test_session,
        "reduce_browser_data": False if no_browser else payload.get("reduce_browser_data", False),
        "no_browser": no_browser,
        "with_audio": action == "play" and payload.get("with_audio", False),
    }
    if requested_song is not None:
        options["song_id"] = requested_song
    if review_rows is not None:
        options["review_rows"] = list(review_rows)
    if account_country is not None:
        options["account_country"] = account_country
    if action in {"play", "like"}:
        options.update(workers=workers, max_consecutive_failures=maximum)
    elif action == "prepare":
        options["workers"] = workers
        if payload.get("resume_preparation"):
            options["resume_preparation"] = True
    return options


def _public_proxy_failure(value):
    """Reconstruct typed diagnostic facts without retaining arbitrary metadata."""
    if type(value) is not dict:
        return {}
    try:
        error = ProxyCountryError(
            value.get("failure_kind"), curl_code=value.get("curl_code"),
            http_status=value.get("http_status"), proxy_connect_http_status=value.get("proxy_connect_http_status"),
            country_check_attempts=value.get("country_check_attempts"),
            retry_after_seconds=value.get("retry_after_seconds"),
            retry_safe=value.get("retryable", True) is True,
        )
        return safe_proxy_country_failure(error)
    except (TypeError, ValueError):
        return {}


def safe_preparation_storage_failures(value):
    """Project only source rows and numeric local-save evidence."""
    if type(value) is not list:
        return []
    result = []
    for item in value[-5:]:
        if type(item) is not dict or not _positive_int(item.get("source_row")):
            continue
        safe = safe_session_storage_failure({key: fact for key, fact in item.items() if key != "source_row"})
        if safe:
            result.append({"source_row": item["source_row"], **safe})
    return result


def _public_report(value):
    """Accept only known typed report facts; never serialize arbitrary API data."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key == "validation_pending":
            if type(item) is bool:
                result[key] = item
        elif key in _BOOL_KEYS and (type(item) is bool or item is None):
            result[key] = item
        elif key == "proxy_pool_size" and type(item) is int and 1 <= item <= MAX_TEST_ROUTES:
            result[key] = item
        elif key == "proxy_route_number" and type(item) is int and type(value.get("proxy_pool_size")) is int and 1 <= item <= value["proxy_pool_size"] <= MAX_TEST_ROUTES:
            result[key] = item
        elif key == "retry_count" and type(item) is int and 0 <= item < MAX_PROVIDER_ATTEMPTS:
            result[key] = item
        elif key == "provider_attempts" and type(item) is int and 1 <= item <= MAX_PROVIDER_ATTEMPTS:
            result[key] = item
        elif key in {"retry_count", "provider_attempts"}:
            continue
        elif key == "verification_read_attempts":
            if type(item) is int and 0 <= item <= 3:
                result[key] = item
        elif key == "verification_read_retries":
            if type(item) is int and 0 <= item <= 2:
                result[key] = item
        elif key in {"session_validation_attempts", "attach_validation_attempts"}:
            if type(item) is int and 0 <= item <= 3:
                result[key] = item
        elif key in {"session_validation_retries", "attach_validation_retries"}:
            if type(item) is int and 0 <= item <= 2:
                result[key] = item
        elif key in {"workers", "requested_workers", "effective_workers", "active_workers"}:
            minimum = 1 if key in {"workers", "requested_workers"} else 0
            if type(item) is int and minimum <= item <= MAX_WORKER_REQUEST:
                result[key] = item
        elif key in {"max_consecutive_failures", "consecutive_failures"}:
            minimum = 1 if key == "max_consecutive_failures" else 0
            if type(item) is int and minimum <= item <= MAX_FAILURE_REQUEST:
                result[key] = item
        elif key in {"session_review_pending", "session_reviews_cleared"}:
            if type(item) is int and 0 <= item <= 2**31 - 1:
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
        elif key in {"selected_rows", "prepared_rows", "connection_pending_rows", "account_failed_rows", "active_rows", "attention_required_rows", "infrastructure_failed_rows", "cleared_rows"} and isinstance(item, list) and all(_positive_int(row) for row in item):
            result[key] = list(item)
        elif key in {"checked_at_utc", "session_saved_at_utc", "started_at", "finished_at"} and isinstance(item, str) and len(item) <= 64:
            try:
                result[key] = datetime.fromisoformat(item).isoformat()
            except ValueError:
                pass
        elif key == "operations" and isinstance(item, dict):
            result[key] = {name: "ok" for name in ("relations", "playlists") if item.get(name) == "ok"}
        elif key == "without_session":
            result[key] = _public_report(item)
        elif key == "proxy_failure":
            safe = _public_proxy_failure(item)
            if safe:
                result[key] = safe
        elif key == "session_failure" and isinstance(item, dict):
            safe = safe_request_failure(item)
            if safe:
                result[key] = safe
        elif key == "session_storage_failure":
            safe = safe_session_storage_failure(item)
            if safe:
                result[key] = safe
        elif key == "recent_session_storage_failures":
            result[key] = safe_preparation_storage_failures(item)
        elif key == "ui_read_failure":
            from .country_preparation import safe_ui_read_failure
            safe = safe_ui_read_failure(item)
            if safe:
                result[key] = safe
        elif key == "provider_failure" and isinstance(item, dict):
            from .provider_recovery import provider_failure
            safe = provider_failure(item)
            if safe:
                result[key] = safe
        elif key == "attempt_history" and isinstance(item, list):
            history = []
            for attempt in item[:MAX_PROVIDER_ATTEMPTS]:
                if not isinstance(attempt, dict) or type(attempt.get("attempt")) is not int or not 1 <= attempt["attempt"] <= MAX_PROVIDER_ATTEMPTS:
                    continue
                if attempt.get("outcome") not in {"succeeded", "provider_issue", "account_failed", "verification_pending", "session_review_pending", "test_failed", "history_skipped", "like_history_pending"}:
                    continue
                entry = {"attempt": attempt["attempt"], "outcome": attempt["outcome"]}
                route = attempt.get("proxy_route_number")
                if type(route) is int and type(value.get("proxy_pool_size")) is int and 1 <= route <= value["proxy_pool_size"] <= MAX_TEST_ROUTES:
                    entry["proxy_route_number"] = route
                for name in ("session_failure", "proxy_failure"):
                    filtered = _public_report({name: attempt.get(name)})
                    entry.update(filtered)
                history.append(entry)
            result[key] = history
        elif key == "proxy" and isinstance(item, dict):
            if item.get("provider") == "PacketStream" and item.get("country") in {"EG", "US"}:
                result[key] = {"provider": "PacketStream", "country": item["country"], "sticky": item.get("sticky") is True}
                for flag in ("country_verified", "proxy_used"):
                    if type(item.get(flag)) is bool:
                        result[key][flag] = item[flag]
                if isinstance(item.get("exit_check"), dict):
                    result[key]["exit_check"] = _public_report(item["exit_check"])
        elif key == "bandwidth" and isinstance(item, dict):
            result[key] = {name: _public_report(item[name]) for name in ("get_song", "play_song", "total") if isinstance(item.get(name), dict)}
        elif key in {"network_usage", "account_network_usage"}:
            safe = safe_network_usage(item)
            if safe:
                result[key] = safe
        elif key == "account_tests_completed" and type(item) is int and 0 <= item <= MAX_WORKER_REQUEST:
            result[key] = item
    return result


class _JobFailure(Exception):
    def __init__(self, details):
        self.details = details


class JobManager:
    """Run jobs sequentially. Each worker owns its SQLite vault connection."""

    supports_preparation_stop = True

    def __init__(self, vault_path=DEFAULT_VAULT_PATH, *, vault_factory=AccountVault, proxy_loader=load_packetstream_proxy, pool_loader=StickyProxyPool.load, test_proxy_loader=load_test_pool):
        self.vault_path = Path(vault_path).resolve()
        self._vault_factory = vault_factory
        self._proxy_loader = proxy_loader
        self._pool_loader = pool_loader
        self._test_proxy_loader = test_proxy_loader
        self._lock = threading.RLock()
        self._latest = None
        self._thread = None
        self._stop_requested = threading.Event()
        self._report_path = self.vault_path.parent / "ui-last-job.json"
        self._started_monotonic = None
        self._network_tests = _empty_network_usage()
        self._network_setup = _empty_network_usage()
        self._network_unassigned = _empty_network_usage()
        self._network_keys = set()
        self._network_accounts = set()
        self._network_by_account = {}
        self._network_account_tests = {}
        self._network_sampled_tests = 0
        self._network_completed_tests = 0

    def snapshot(self):
        with self._lock:
            snapshot = deepcopy(self._latest)
            if snapshot is not None and snapshot["status"] == "running" and self._started_monotonic is not None:
                snapshot["elapsed_seconds"] = max(0.0, monotonic() - self._started_monotonic)
            return snapshot

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
            options["_defer_like_proxy"] = action == "like" and callable(getattr(self._vault_factory, "like_history", None))
            if options["proxy_test_session"] and not options["_defer_like_proxy"]:
                try:
                    selected = self._test_proxy_loader(self.vault_path.parent / "packetstream-test-route.dpapi")
                    if isinstance(selected, PacketStreamProxy):
                        # Legacy injected single profiles remain the exact
                        # immutable instance after the pool validator checks it.
                        StickyProxyPool((selected,))
                    elif not isinstance(selected, StickyProxyPool) or not 1 <= len(selected) <= MAX_TEST_ROUTES:
                        raise ValueError
                    options["_test_proxy_pool"] = selected
                except Exception:
                    raise JobValidationError("The saved Workbench test route pool could not be loaded. Save valid routes before testing.") from None
            job = {
                "id": uuid4().hex, "action": action, "status": "queued",
                "progress": {"completed": 0, "total": total}, "phase": "queued",
                "message": "Queued locally.", "results": [], "error": None,
                "started_at": None, "finished_at": None,
                "rows": options["rows"], "count": options["count"],
                "proxy_egypt": options["proxy_egypt"] or action == "proxy-check",
                "proxy_sticky_pool": options["proxy_sticky_pool"],
                "proxy_test_session": options["proxy_test_session"],
                "reduce_browser_data": options["reduce_browser_data"],
                "no_browser": options["no_browser"],
                "song_id": options["song_id"],
                "elapsed_seconds": 0.0,
                "stop_requested": False,
            }
            self._network_tests = _empty_network_usage()
            self._network_setup = _empty_network_usage()
            self._network_unassigned = _empty_network_usage()
            self._network_keys = set()
            self._network_accounts = set()
            self._network_by_account = {}
            self._network_account_tests = {}
            self._network_sampled_tests = self._network_completed_tests = 0
            if action in _WORKBENCH_ACTIONS:
                job["network_usage"] = _network_summary(self._network_tests, total_tests=total,
                    completed_tests=0, sampled_tests=0, sampled_accounts=0)
            if action in _WORKBENCH_ACTIONS:
                job.update(connection_pending=0, verification_pending=0, session_review_pending=0, retried=0, provider_retries=0, account_failed=0)
            if action == "review-sessions":
                job.update(session_review_pending=0, session_reviews_cleared=0)
            if action == "prepare":
                job.update(workers=options["workers"], requested_workers=options["workers"],
                           effective_workers=0, active_workers=0, active_rows=[])
            if options.get("account_country"):
                job.update(account_country=options["account_country"], selection_mode="random_country")
            if options["proxy_test_session"] and "_test_proxy_pool" in options:
                job["proxy_mode"] = "test_session"
                job["proxy_pool_size"] = self._test_pool_size(options["_test_proxy_pool"])
                job["proxy_route_assignments"] = []
            if action in {"play", "like"}:
                job.update(workers=options["workers"], requested_workers=options["workers"], effective_workers=0,
                           max_consecutive_failures=options["max_consecutive_failures"],
                           consecutive_failures=0, active_workers=0, active_rows=[], active_tests=[], attempted=0,
                           succeeded=0, failed=0, skipped=0, completed_tests=0, stop_reason=None,
                           connection_pending=0, verification_pending=0, session_review_pending=0, retried=0, provider_retries=0, account_failed=0,
                           results_total=0, results_truncated=0, **dict.fromkeys(_WRITE_COUNTERS, 0))
            if action == "like":
                job.update(selected_accounts=len(options["rows"]), eligible_accounts=len(options["rows"]),
                           selected_rows=list(options["rows"]), requested_tests=total, requested_tests_per_account=options["count"],
                           already_liked_skipped=0, like_verification_held=0, unknown_like_held=0,
                           duplicate_like_skipped_tests=0, history_skipped_tests=0, history_held_tests=0,
                           cached_like_skips=0, cached_like_holds=0)
            try:
                _journal(job, self._report_path)
            except Exception:
                raise JobValidationError("The local job report could not be saved. Check the report folder before testing.") from None
            self._latest = job
            self._stop_requested = threading.Event()
            self._started_monotonic = None
            queued = deepcopy(job)
            self._thread = threading.Thread(target=self._run, args=(options,), name="anghami-ui-job", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._fail({"code": "worker_failed", "message": "The local worker could not start. No test operation was run."})
                raise JobValidationError("The local worker could not start. No test operation was run.") from None
            return queued

    def request_stop(self, job_id):
        """Stop dispatching and retrying; let requests already in flight finish."""
        with self._lock:
            if (type(job_id) is not str or len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id)
                or self._latest is None or self._latest["id"] != job_id):
                raise JobValidationError("Refresh the active job before requesting a stop.")
            if self._latest["action"] not in {"play", "like", "prepare"}:
                raise JobValidationError("This stop control is available for preparation, play and like tests.")
            if self._latest["status"] not in {"queued", "running"}:
                return deepcopy(self._latest)
            preparing = self._latest["action"] == "prepare"
            if preparing:
                # Country workers read this exact job's durable marker. A new
                # queued job may not have created its checkpoint yet.
                stop_path = self.vault_path.parent / f"ui-preparation-progress-{job_id}.stop"
                try:
                    _journal({"job_id": job_id, "stop_requested": True, "requested_at": _now()}, stop_path)
                except BaseException:
                    raise JobValidationError("The preparation stop request could not be saved. Keep the console open and try again; active preparation was not interrupted.") from None
            self._stop_requested.set()
            try:
                message = ("Stop requested; waiting for in-flight account preparation to finish safely." if preparing
                           else "Stop requested; waiting for in-flight tests to finish safely.")
                self._update(stop_requested=True, message=message)
            except BaseException:
                self._latest["stop_requested"] = True
                raise JobValidationError("Dispatch stopped, but the stop request could not be saved. Keep the console open while active requests finish.") from None
            return deepcopy(self._latest)

    def _filter_like_selection(self, vault, options):
        reader = getattr(vault, "like_history", None)
        if options["action"] != "like" or not callable(reader):
            return
        original = list(options["rows"])
        requested_count = options["count"]
        identities = self._freeze_identities(vault, original)
        history = reader(options["song_id"], rows=original)
        if type(history) is not dict or history.get("song_id") != options["song_id"] or type(history.get("accounts")) is not list:
            raise _JobFailure({"code": "like_history_invalid", "message": "The saved like history could not be validated. No like was sent."})
        states = {}
        for account in history["accounts"]:
            row = account.get("source_row") if type(account) is dict else None
            state = account.get("state") if type(account) is dict else None
            if (type(row) is not int or row not in identities or row in states
                or type(state) is not str or state not in {"confirmed", "verification_pending", "write_unknown", "in_progress"}):
                raise _JobFailure({"code": "like_history_invalid", "message": "The saved like history did not match the selected accounts. No like was sent."})
            states[row] = state
        # Every alias of one identity must agree before any operation starts.
        identity_states = {}
        for row in original:
            state = states.get(row)
            key = identities[row]
            if key in identity_states and identity_states[key] != state:
                raise _JobFailure({"code": "account_scope_invalid", "message": "Selected aliases disagree about their saved song history. No like was sent."})
            identity_states[key] = state
        eligible, seen = [], set()
        for row in original:
            if row not in states and identities[row] not in seen:
                eligible.append(row)
                seen.add(identities[row])
        confirmed = sum(state == "confirmed" for state in states.values())
        pending = sum(state == "verification_pending" for state in states.values())
        unknown = sum(state in {"write_unknown", "in_progress"} for state in states.values())
        duplicates = (len(original) - len(states)) * requested_count - len(eligible)
        options.update(rows=eligible, count=1, _like_history_enabled=True,
                       _test_identities={row: identities[row] for row in eligible})
        self._update(rows=list(eligible), count=1, eligible_accounts=len(eligible),
                     already_liked_skipped=confirmed, like_verification_held=pending, unknown_like_held=unknown,
                     duplicate_like_skipped_tests=duplicates, history_skipped_tests=confirmed * requested_count,
                     history_held_tests=(pending + unknown) * requested_count,
                     progress={"completed": 0, "total": len(eligible)},
                     network_usage=_network_summary(_empty_network_usage(), total_tests=len(eligible), completed_tests=0,
                                                   sampled_tests=0, sampled_accounts=0))

    def _load_deferred_test_proxy(self, options):
        try:
            selected = self._test_proxy_loader(self.vault_path.parent / "packetstream-test-route.dpapi")
            if isinstance(selected, PacketStreamProxy):
                StickyProxyPool((selected,))
            elif not isinstance(selected, StickyProxyPool) or not 1 <= len(selected) <= MAX_TEST_ROUTES:
                raise ValueError
            options["_test_proxy_pool"] = selected
            self._update(proxy_mode="test_session", proxy_pool_size=self._test_pool_size(selected), proxy_route_assignments=[])
        except Exception:
            raise _JobFailure({"code": "test_proxy_invalid", "message": "The saved Workbench test routes could not be loaded. No like was sent."}) from None

    def _update(self, **fields):
        with self._lock:
            candidate = deepcopy(self._latest)
            candidate.update(deepcopy(fields))
            if self._started_monotonic is not None:
                candidate["elapsed_seconds"] = max(0.0, monotonic() - self._started_monotonic)
            _journal(candidate, self._report_path)
            self._latest = candidate

    def _phase(self, phase, message):
        self._update(phase=phase, message=message)

    def _record_network_result(self, report):
        """Accumulate once before the recent-results tail is trimmed."""
        if self._latest["action"] not in _WORKBENCH_ACTIONS or not _positive_int(report.get("source_row")) or report.get("history_skipped") is True:
            return {}
        key = (report["source_row"], report.get("test_number", 1))
        if key in self._network_keys:
            return {}
        self._network_keys.add(key)
        self._network_completed_tests += 1
        usage = safe_network_usage(report.get("network_usage"))
        row = report["source_row"]
        self._network_account_tests[row] = self._network_account_tests.get(row, 0) + 1
        self._network_by_account[row] = _merge_network_usage(self._network_by_account.get(row), usage)
        if usage:
            self._network_tests = _merge_network_usage(self._network_tests, usage)
            if usage["measured_requests"] + usage["partial_requests"]:
                self._network_sampled_tests += 1
                self._network_accounts.add(report["source_row"])
        # A first check already exposes exactly this traffic in network_usage.
        # Avoid duplicating its full dictionary in large single-check runs.
        account = {"account_tests_completed": self._network_account_tests[row]}
        if self._network_account_tests[row] > 1:
            account["account_network_usage"] = self._network_by_account[row]
        return account

    def _current_network_summary(self):
        network_total = self._latest["progress"]["total"] - self._latest.get("cached_like_skips", 0) - self._latest.get("cached_like_holds", 0)
        return _network_summary(self._network_tests, total_tests=network_total,
            completed_tests=self._network_completed_tests, sampled_tests=self._network_sampled_tests,
            sampled_accounts=len(self._network_accounts), setup_usage=self._network_setup)

    def _append(self, report, *, completed=None):
        with self._lock:
            results = deepcopy(self._latest["results"])
            safe = _public_report(report)
            safe.update(self._record_network_result(safe))
            results.append(safe)
            fields = {"results": results}
            if self._latest["action"] in _WORKBENCH_ACTIONS:
                fields["network_usage"] = self._current_network_summary()
            if self._latest["action"] in {"play", "like"}:
                if _positive_int(safe.get("source_row")) and _positive_int(safe.get("test_number")):
                    _journal(safe, self.vault_path.parent / "ui-test-reports" / self._latest["id"] / f"account-{safe['source_row']}.{self._latest['action']}-{safe['test_number']}.redacted.json")
                excess = max(0, len(results) - MAX_RETAINED_RESULTS)
                fields["results"] = results[excess:]
                fields["results_total"] = self._latest["results_total"] + 1
                fields["results_truncated"] = self._latest["results_truncated"] + excess
            if completed is not None:
                fields["progress"] = {"completed": completed, "total": self._latest["progress"]["total"]}
            self._update(**fields)

    def _fail(self, error):
        with self._lock:
            candidate = deepcopy(self._latest)
            finished_with_failures = error.get("code") == "completed_with_failures"
            candidate.update(status="completed_with_failures" if finished_with_failures else "failed",
                             phase="complete" if finished_with_failures else "failed",
                             message=error["message"], error=deepcopy(error), finished_at=_now())
            if self._started_monotonic is not None:
                candidate["elapsed_seconds"] = max(0.0, monotonic() - self._started_monotonic)
            if candidate["action"] in {"play", "like"}:
                candidate.update(active_workers=0, active_rows=[], active_tests=[],
                                 skipped=candidate["progress"]["total"] - candidate["attempted"])
                candidate["message"] = f"{_action_summary(candidate)} {error['message']}"
                if error.get("code") == "journal_failed":
                    candidate["stop_reason"] = "journal_failed"
                elif candidate.get("stop_reason") is None:
                    candidate["stop_reason"] = "immediate_failure"
            elif candidate["action"] == "prepare":
                candidate.update(active_workers=0, active_rows=[])
            # Preserve the failure in memory even when storage itself has failed.
            self._latest = candidate
            try:
                _journal(candidate, self._report_path)
            except Exception:
                self._latest.update(status="failed", phase="failed")
                if candidate["action"] in {"play", "like"}:
                    self._latest["stop_reason"] = "journal_failed"
                self._latest["error"] = {
                    "code": "journal_failed", "message": "The job report could not be saved. An attempted write may be unknown; check existing account reports before another test.",
                }
                self._latest["message"] = self._latest["error"]["message"]

    def _run(self, options):
        # Individual account scopes do not propagate here: this scope is only
        # setup overhead, never an extra copy of their transfer counters.
        with measure_network_usage() as meter:
            self._run_unmetered(options)
        if options["action"] in _WORKBENCH_ACTIONS:
            with self._lock:
                self._network_setup = _merge_network_usage(meter.snapshot(), self._network_unassigned)
                try:
                    self._update(network_usage=self._current_network_summary())
                except BaseException:
                    self._fail({"code": "journal_failed", "message": "The final job report could not be saved. Review existing account reports before another test."})

    def _run_unmetered(self, options):
        try:
            self._started_monotonic = monotonic()
            self._update(status="running", phase="opening_vault", message="Starting local checks.", started_at=_now())
            action = options["action"]
            if self._latest.get("stop_requested"):
                self._update(status="stopped", phase="stopped", stop_reason="user_stop", finished_at=_now(),
                             message="Stopped before account preparation started." if action == "prepare" else "Stopped before any account test started.",
                             skipped=self._latest["progress"]["total"])
                return
            if action == "proxy-check":
                self._phase("proxy_check", "Verifying an Egypt exit without account requests.")
                proxy = self._proxy_loader(self.vault_path.parent / "packetstream.dpapi")
                checked = proxy.verify_country()
                if not isinstance(checked, dict) or checked.get("country_verified") is not True or checked.get("proxy_used") is not True:
                    raise SessionError("Egypt proxy verification did not confirm its result.")
                self._append({"passed": True, "country": "EG", "proxy": {**proxy.summary(), **_public_report(checked)}}, completed=1)
            else:
                with self._vault_factory(self.vault_path) as vault:
                    self._filter_like_selection(vault, options)
                    self._validate_rows(vault, options)
                    proxy = None
                    empty_like = action == "like" and options.get("_like_history_enabled") and not options["rows"]
                    if options["proxy_test_session"] and not empty_like:
                        if options.get("_defer_like_proxy"):
                            self._load_deferred_test_proxy(options)
                        self._assign_test_routes(vault, options)
                    if (action in _WORKBENCH_ACTIONS or action == "review-sessions") and "_test_identities" not in options:
                        options["_test_identities"] = self._freeze_identities(vault, options["rows"])
                    if action in {"play", "like"}:
                        effective = self._test_worker_count(options, options["_test_identities"])
                        self._update(requested_workers=options["workers"], effective_workers=effective,
                                     failure_limit_scope="active_budget" if effective <= options["max_consecutive_failures"] else "new_dispatch")
                    if options["proxy_egypt"] and not options["proxy_sticky_pool"] and not options["proxy_test_session"] and action != "preview" and not empty_like:
                        self._phase("proxy_preflight", "Loading the encrypted Egypt proxy configuration.")
                        proxy = self._proxy_loader(self.vault_path.parent / "packetstream.dpapi")
                    if not empty_like and action in {"play", "like"} and (options["workers"] != 1 or options["max_consecutive_failures"] != 1):
                        identities = options.get("_test_identities") or self._freeze_identities(vault, options["rows"])
                    else:
                        identities = None
                    if identities is None and not empty_like:
                        self._perform(vault, options, proxy)
                if identities is not None:
                    self._parallel_tests(options, proxy, identities)
            pending = (self.snapshot().get("connection_pending", 0) + self.snapshot().get("verification_pending", 0)
                       + self.snapshot().get("session_review_pending", 0)
                       + self.snapshot().get("like_verification_held", 0) + self.snapshot().get("unknown_like_held", 0)
                       + self.snapshot().get("preparation_held", 0))
            if action in {"check", "song"} and self.snapshot().get("account_failed", 0):
                raise _JobFailure({"code": "completed_with_failures", "message": "Read-only checks finished; confirmed account failures are available for review."})
            remaining = {}
            if action in {"play", "like"}:
                job = self.snapshot()
                remaining["skipped"] = job["progress"]["total"] - job["attempted"]
            if self._latest.get("stop_requested"):
                if action == "prepare":
                    remaining.update(active_workers=0, active_rows=[])
                    message = f"Prepared {self.snapshot()['progress']['completed']} account(s). Stopped after in-flight preparation finished."
                else:
                    message = f"{_action_summary(self.snapshot())} Stopped after in-flight tests finished."
                self._update(status="stopped", phase="stopped", stop_reason="user_stop", finished_at=_now(),
                             message=message, **remaining)
                return
            self._update(status="completed_with_pending" if pending else "succeeded", phase="complete",
                         message=_action_summary(self.snapshot()) or (
                             "Finished; interrupted accounts are held for review." if self.snapshot().get("preparation_held", 0)
                             else "Finished; accounts remain held for saved-session review." if self.snapshot().get("session_review_pending", 0)
                             else "Finished; accounts waiting for a working connection can be retried later." if pending
                             else "Completed successfully."), finished_at=_now(), **remaining)
        except _JobFailure as exc:
            self._network_unassigned = _merge_network_usage(self._network_unassigned, getattr(exc, "_network_usage", None))
            self._fail(exc.details)
        except BaseException as exc:
            self._network_unassigned = _merge_network_usage(self._network_unassigned, getattr(exc, "_network_usage", None))
            error = self._safe_error(options["action"], exc, no_browser=options["no_browser"])
            proxy_failure = safe_proxy_country_failure(exc)
            if proxy_failure:
                error["proxy_failure"] = proxy_failure
            self._fail(error)

    def _validate_rows(self, vault, options):
        if options.get("review_rows"):
            review = vault.failure_review()
            eligible = review.get("failed_rows", []) if isinstance(review, dict) else []
            if not set(options["review_rows"]).issubset(eligible):
                raise _JobFailure({"code": "account_scope_invalid", "message": "A selected row is no longer in failed-account review. Refresh the account list."})
        if options["action"] not in _ROW_ACTIONS:
            return
        self._phase("validating_rows", "Checking every selected source row before starting.")
        review_reader = getattr(vault, "session_review", None)
        review = review_reader() if callable(review_reader) else {}
        held = review.get("held_rows", []) if type(review) is dict else []
        held = {row for row in held if _positive_int(row)} if type(held) is list else set()
        if options["action"] != "review-sessions" and held.intersection(options["rows"]):
            raise _JobFailure({"code": "session_review_required", "message": "A selected account is held for saved-session review. Check it using the read-only review action before another login or test."})
        for row in options["rows"]:
            vault.record(row)
        if options["action"] == "review-sessions":
            eligible = review.get("session_review_rows", []) if type(review) is dict else []
            eligible = {row for row in eligible if _positive_int(row)} if type(eligible) is list else set()
            if not set(options["rows"]).issubset(eligible):
                raise _JobFailure({"code": "account_scope_invalid", "message": "A selected row is no longer held for saved-session review. Refresh the review list."})
            return
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
        fields = {}
        if type(safe.get("workers")) is int and 1 <= safe["workers"] <= MAX_PREPARATION_WORKERS:
            fields["workers"] = safe["workers"]
            fields["requested_workers"] = safe["workers"]
            effective = safe.get("effective_workers")
            effective = effective if type(effective) is int and 0 <= effective <= safe["workers"] else None
            if effective is not None:
                fields["effective_workers"] = effective
            denominator = effective if effective is not None else safe["workers"]
            if type(safe.get("active_workers")) is int and 0 <= safe["active_workers"] <= denominator:
                fields["active_workers"] = safe["active_workers"]
                requested = f" ({safe['workers']} requested)" if effective is not None and effective != safe["workers"] else ""
                message = f"Prepared {completed} account(s); {safe['active_workers']} of {denominator} worker(s) active{requested}."
            if "active_rows" in safe:
                fields["active_rows"] = safe["active_rows"]
        for name in ("failure_limit_scope",):
            if name in safe:
                fields[name] = safe[name]
        for name, target in (("connection_pending_count", "connection_pending"),
                             ("account_failed_count", "account_failed"), ("preparation_held", "preparation_held"),
                             ("infrastructure_failed_count", "preparation_infrastructure_failed")):
            if type(safe.get(name)) is int:
                fields[target] = safe[name]
        with self._lock:
            self._update(phase=phase, message=message, progress={"completed": completed, "total": self._latest["progress"]["total"]}, **fields)

    @staticmethod
    def _serial_preparation_metrics(report):
        """Expose a serial worker only after a concrete selection is known."""
        report = report if type(report) is dict else {}
        selected = report.get("selected_rows")
        selected = selected if type(selected) is list and selected and all(_positive_int(row) for row in selected) else []
        phase, row = report.get("phase"), report.get("active_row")
        terminal = type(phase) is str and phase in {"complete", "stopped", "failed"}
        attempted = report.get("attempted_accounts")
        effective = int(bool(selected) and (not terminal or type(attempted) is int and attempted > 0))
        active = bool(effective and not terminal and type(phase) is str and phase in {
            "preparing", "session_lookup", "session_recovery", "login", "validation", "session_validation",
            "proxy_preflight", "provider_retry", "provider_wait", "connection_pending", "account_failed",
        } and _positive_int(row) and row in selected)
        return {**report, "workers": 1, "requested_workers": 1, "effective_workers": effective,
                "active_workers": int(active), "active_rows": [row] if active else []}

    @staticmethod
    def _identity(record):
        email = record.get("email") if isinstance(record, dict) else None
        if type(email) is not str or not email.strip():
            raise _JobFailure({"code": "account_scope_invalid", "message": "The selected account identity could not be bound before testing."})
        return hashlib.sha256(email.strip().casefold().encode("utf-8")).digest()

    def _freeze_identities(self, vault, rows):
        identities = {}
        for row in rows:
            record = vault.record(row)
            try:
                identities[row] = self._identity(record)
            finally:
                del record
        return identities

    @staticmethod
    def _test_pool_size(pool):
        return 1 if isinstance(pool, PacketStreamProxy) else len(pool)

    def _assign_test_routes(self, vault, options):
        """Bind routes to private account identities in submitted row order."""
        identities = self._freeze_identities(vault, options["rows"])
        pool = options["_test_proxy_pool"]
        size = self._test_pool_size(pool)
        identity_routes, profiles, numbers = {}, {}, {}
        for row in options["rows"]:
            identity = identities[row]
            if identity not in identity_routes:
                index = len(identity_routes) % size
                selected = pool if isinstance(pool, PacketStreamProxy) else pool.proxy_for_index(index)
                identity_routes[identity] = selected, index + 1
            profiles[row], numbers[row] = identity_routes[identity]
        assignments = [{"source_row": row, "proxy_route_number": numbers[row]} for row in options["rows"]]
        # No invocation can begin before the complete public assignment is
        # durable. Profile objects and identity digests stay in private options.
        try:
            self._update(proxy_pool_size=size, proxy_route_assignments=assignments)
        except Exception:
            raise _JobFailure({"code": "journal_failed", "message": "The Workbench proxy assignments could not be saved. No test operation was started."}) from None
        options["_test_identities"] = identities
        options["_test_proxies"] = profiles
        options["_test_route_numbers"] = numbers

    @staticmethod
    def _row_proxy(options, proxy, row):
        if not options.get("proxy_test_session"):
            return proxy
        selected = options.get("_test_proxies", {}).get(row)
        if selected is None:
            raise _JobFailure({"code": "account_scope_invalid", "message": "The selected account has no frozen Workbench proxy route."})
        return selected

    def _check_test_identity(self, vault, options, row):
        if "_test_identities" not in options:
            return
        record = vault.record(row)
        try:
            if self._identity(record) != options.get("_test_identities", {}).get(row):
                raise _JobFailure({"code": "account_scope_invalid", "message": "A selected account changed after its Workbench proxy route was assigned."})
        finally:
            del record

    @classmethod
    def _route_metadata(cls, options, row):
        if not options.get("proxy_test_session") or row is None:
            return {}
        return {"proxy_pool_size": cls._test_pool_size(options["_test_proxy_pool"]),
                "proxy_route_number": options["_test_route_numbers"][row]}

    def _next_test_route(self, options, proxy, row, identity, *, used_routes=()):
        """Rotate one identity only; aliases and future repetitions retain it."""
        if not options.get("proxy_test_session"):
            if not isinstance(proxy, PacketStreamProxy):
                return proxy
            replacement = PacketStreamProxy(proxy.username, proxy.auth_key)
            object.__setattr__(replacement, "_endpoint", proxy._endpoint)
            return replacement
        with self._lock:
            pool = options["_test_proxy_pool"]
            size = self._test_pool_size(pool)
            if size <= 1:
                return None
            start = options["_test_route_numbers"][row]
            replacement = None
            for offset in range(size):
                number = (start + offset) % size + 1
                candidate = pool.proxy_for_index(number - 1)
                if all(candidate != used for used in used_routes):
                    replacement = candidate
                    break
            if replacement is None:
                return None
            profiles, numbers = dict(options["_test_proxies"]), dict(options["_test_route_numbers"])
            for alias, bound in options["_test_identities"].items():
                if bound == identity:
                    profiles[alias], numbers[alias] = replacement, number
            assignments = [{"source_row": alias, "proxy_route_number": numbers[alias]} for alias in options["rows"]]
            try:
                self._update(proxy_route_assignments=assignments)
            except Exception:
                raise _JobFailure({"code": "journal_failed", "message": "The updated route assignment could not be saved. No retry was started."}) from None
            options["_test_proxies"], options["_test_route_numbers"] = profiles, numbers
            return replacement

    def _retry_activity(self, options, row, number, attempt, status):
        with self._lock:
            active = deepcopy(self._latest.get("active_tests", []))
            for item in active:
                if item.get("source_row") == row and item.get("test_number") == number:
                    item.update(provider_attempts=attempt, retry_count=attempt - 1,
                                connection_status=status, **self._route_metadata(options, row))
            try:
                self._update(active_tests=active)
            except Exception:
                raise _JobFailure({"code": "journal_failed", "message": "The connection retry intent could not be saved. No retry was started."}) from None

    @staticmethod
    def _test_metadata(options, row=None):
        if options.get("action") not in _WORKBENCH_ACTIONS:
            return {}
        if options.get("proxy_test_session"):
            return {"proxy_test_session": True, "proxy_mode": "test_session", **JobManager._route_metadata(options, row)}
        return {"proxy_test_session": False}

    @classmethod
    def _test_report(cls, report, options, row):
        scoped = dict(report) if isinstance(report, dict) else {}
        for name in ("proxy_test_session", "proxy_mode", "proxy_pool_size", "proxy_route_number"):
            scoped.pop(name, None)
        scoped.update(cls._test_metadata(options, row))
        return _public_report(scoped)

    @classmethod
    def _timed_report(cls, report, options, started_at, started_monotonic, *, result_unknown=False, source_row=None):
        # Runner timestamps are untrusted. Record local wall-clock facts and a
        # monotonic duration after this individual planned call finishes.
        return {**cls._test_report(report, options, source_row),
                "started_at": started_at, "finished_at": _now(),
                "elapsed_seconds": max(0.0, monotonic() - started_monotonic),
                "result_unknown": result_unknown}

    @staticmethod
    def _confirmed_failure(action, report, exc=None):
        """Continue only a known completed prewrite failure or explicit rejection."""
        attempted, result = ("event_attempted", "event_result") if action == "play" else ("mutation_attempted", "mutation_result")
        if report.get(attempted) is True and report.get(result) not in {"accepted", "rejected"}:
            return False
        if isinstance(exc, ProxyCountryError) and report.get(attempted) is not True:
            details = safe_proxy_country_failure(exc)
            return details.get("failure_kind") in {"transport_error", "http_failure"} and details.get("curl_code") not in _PROXY_CERTIFICATE_CODES and details.get("http_status") not in {401, 403, 407}
        return report.get("passed") is False and report.get("error_code") in _OPERATIONAL_CODES and (
            report.get(attempted) is False and report.get(result) == "not_attempted"
            or report.get(attempted) is True and report.get(result) == "rejected"
        )

    @staticmethod
    def _accepted_like_verification_pending(action, report):
        """An acknowledged append needs only a read, never a whole-test replay."""
        from .like_test import retryable_like_verification_failure
        return (
            action == "like" and report.get("passed") is False
            and report.get("failed_phase") == "state_after"
            and report.get("error_code") in {"state_read_failed", "state_http_failed", "request_rate_limited"}
            and report.get("mutation_attempted") is True
            and type(report.get("mutation_attempts")) is int and report["mutation_attempts"] == 1
            and report.get("mutation_accepted") is True and report.get("mutation_result") == "accepted"
            and type(report.get("mutation_http_status")) is int and report["mutation_http_status"] == 200
            and report.get("liked_before") is False and report.get("liked_after") is None
            and report.get("persisted_state_verified") is False
            and report.get("renewal_completed") is True
            and retryable_like_verification_failure(report.get("session_failure"))
        )

    @staticmethod
    def _no_engagement_write_proof(action, report):
        """Require exact zero-write evidence before discarding arbitrary fields."""
        if action not in {"play", "like"} or type(report) is not dict:
            return False
        prefix = "event" if action == "play" else "mutation"
        expected = {"attempted": False, "attempts": 0, "accepted": False, "result": "not_attempted"}
        for name in ("event", "mutation"):
            for suffix, wanted in expected.items():
                key = f"{name}_{suffix}"
                if name != prefix and key not in report:
                    continue
                if type(report.get(key)) is not type(wanted) or report[key] != wanted:
                    return False
        for key in (*_WRITE_COUNTERS, "writes_unknown", "event_unknown_count", "mutation_unknown_count"):
            if key in report and (type(report[key]) is not int or report[key] != 0):
                return False
        for key in ("event_unknown", "mutation_unknown", "write_unknown"):
            if key in report and report[key] is not False:
                return False
        if "api_status" in report and report["api_status"] != "not_attempted":
            return False
        return True

    @staticmethod
    def _session_review_eligible(action, report, failure, *, fresh_scoped=False):
        """Quarantine an unconfirmed bootstrap only with exact no-write proof."""
        failure = safe_request_failure(failure)
        if (not fresh_scoped or action not in {"play", "like"}
            or failure.get("failure_category") != "provider" or failure.get("stage") != "identity"
            or failure.get("http_status") in {401, 403, 407}
            or failure.get("curl_code") in _PROXY_CERTIFICATE_CODES):
            return False
        transient = (
            failure.get("code") == "request_transport_failed"
            and failure.get("curl_code") in {5, 6, 7, 18, 28, 35, 52, 55, 56, 81, 92, 95, 96}
            or failure.get("code") in {"request_http_failed", "request_rate_limited"}
            and failure.get("http_status") in {408, 425, 429, 500, 502, 503, 504}
        )
        return bool(transient and report.get("passed") is False
                    and report.get("failed_phase") == "account_identity"
                    and report.get("renewal_attempted") is True and report.get("renewal_completed") is False
                    and JobManager._no_engagement_write_proof(action, report))

    def _test_attempt(self, options, proxy, row, number, identity, stopping, start_gate=None, *, _vault=None):
        action = options["action"]
        name = "test-play-record" if action == "play" else "test-like"
        path = self.vault_path.parent / f"account-{row}.{name}-report.json"
        before, report = self._report_signature(path), {}
        if start_gate is not None:
            start_gate.wait()
        if stopping.is_set():
            return {"kind": "skipped", "finished": monotonic()}
        started_at, started_monotonic = _now(), monotonic()
        proxy_failure = {}
        caught = None
        fresh_scoped = False
        no_write_proven = False
        try:
            with (nullcontext(_vault) if _vault is not None else self._vault_factory(self.vault_path)) as vault:
                record = vault.record(row)
                try:
                    if self._identity(record) != identity:
                        raise _JobFailure({"code": "account_scope_invalid", "message": "A selected account changed after this test job was queued."})
                finally:
                    del record
                if row not in vault.enrolled_test_rows():
                    raise _JobFailure({"code": "account_not_prepared", "message": "A selected account is no longer enrolled for testing."})
                cohort = vault.test_accounts()
                if not isinstance(cohort, dict) or row not in cohort.get("ready_rows", []):
                    raise _JobFailure({"code": "account_not_ready", "message": "A selected account is no longer ready for testing."})
                if read_test_song_id(self.vault_path.parent / "test-settings.json") != options["song_id"]:
                    raise _JobFailure({"code": "song_scope_invalid", "message": "The declared test song changed while the job was running."})
                if stopping.is_set():
                    return {"kind": "skipped", "finished": monotonic()}
                runner = vault.test_play_record if action == "play" else vault.test_like
                test_options = {} if proxy is None else {"proxy": proxy}
                if options["song_id"] != TEST_SONG_ID:
                    test_options["declared_song_id"] = options["song_id"]
                if action == "play" and options.get("with_audio"):
                    test_options["with_audio"] = True
                raw = runner(row, options["song_id"], **test_options)
                no_write_proven = self._no_engagement_write_proof(action, raw)
                report = _public_report(raw)
                if isinstance(raw, dict) and (
                    "source_row" in raw and (type(raw["source_row"]) is not int or raw["source_row"] != row)
                    or "song_id" in raw and validate_test_song_id(raw["song_id"]) != options["song_id"]
                ):
                    raise _JobFailure({"code": "request_scope_invalid", "message": "The test result did not match its selected account and song."})
                fresh_scoped = type(raw) is dict and raw.get("song_id") == options["song_id"]
                if type(raw) is dict and raw.get("history_skipped") is True:
                    state = raw.get("history_status")
                    if (action != "like" or not fresh_scoped or raw.get("source_row") != row
                        or state not in {"confirmed", "verification_pending", "write_unknown", "in_progress"}
                        or raw.get("mutation_attempted") is not False or type(raw.get("mutation_attempts")) is not int
                        or raw["mutation_attempts"] != 0 or raw.get("mutation_result") != "not_attempted"
                        or raw.get("mutation_accepted") is not False or raw.get("persisted_state_verified") is not False
                        or raw.get("authenticated") is not False
                        or raw.get("passed") is not (state == "confirmed")
                        or raw.get("history_confirmed") is not (state == "confirmed")):
                        raise _JobFailure({"code": "request_scope_invalid", "message": "The cached like result did not contain the required local no-write proof."})
                    kind = "history_skipped" if state == "confirmed" else "like_history_pending"
                    return {"kind": kind, "report": self._timed_report(report, options, started_at, started_monotonic, source_row=row),
                            "fatal": False, "error": {"result_unknown": False}, "finished": monotonic()}
                if report.get("passed") is True:
                    return {"kind": "succeeded", "report": self._timed_report(report, options, started_at, started_monotonic, source_row=row), "finished": monotonic()}
                error = {"code": "test_not_confirmed", "message": "The test did not confirm success. Check its account report before another write."}
                confirmed = self._confirmed_failure(action, report)
        except BaseException as exc:
            caught = exc
            if not report:
                raw = self._changed_raw_report(path, before)
                no_write_proven = self._no_engagement_write_proof(action, raw)
                report = _public_report(raw)
                fresh_scoped = bool(report) and report.get("song_id") == options["song_id"]
                if ("source_row" in raw and (type(raw["source_row"]) is not int or raw["source_row"] != row)
                    or "song_id" in raw and raw["song_id"] != options["song_id"]):
                    caught = exc = _JobFailure({"code": "request_scope_invalid", "message": "The test report did not match its selected account and song."})
                    fresh_scoped = False
            error = deepcopy(exc.details) if isinstance(exc, _JobFailure) else self._safe_error(action, exc)
            if report.get("error_code") in {"journal_failed", "like_history_failed"}:
                error = {"code": "journal_failed", "message": "The account report could not be saved. Review existing reports before another test."}
            confirmed = not isinstance(exc, (_JobFailure, KeyboardInterrupt, EOFError)) and error["code"] != "journal_failed" and self._confirmed_failure(action, report, exc)
            proxy_failure = safe_proxy_country_failure(exc)
        # A fresh account report recovered after an exception must obey the
        # same scope binding as a returned report before any deferral is allowed.
        if ("source_row" in report and (type(report["source_row"]) is not int or report["source_row"] != row)
            or "song_id" in report and report["song_id"] != options["song_id"]):
            caught = _JobFailure({"code": "request_scope_invalid", "message": "The test report did not match its selected account and song."})
            error = deepcopy(caught.details)
            confirmed = False
            fresh_scoped = False
        attempted, result = ("event_attempted", "event_result") if action == "play" else ("mutation_attempted", "mutation_result")
        error.update(source_row=row, test_number=number)
        for key, target in (("failed_phase", "failed_phase"), ("error_code", "report_code")):
            if report.get(key):
                error[target] = report[key]
        error["result_unknown"] = not confirmed and (
            not report or report.get(attempted) is True and report.get(result) not in {"accepted", "rejected"}
        )
        if confirmed:
            error["message"] = "A planned test confirmed a failure. No automatic retry was made."
        if proxy_failure:
            error["proxy_failure"] = proxy_failure
            report["proxy_failure"] = proxy_failure
        session_failure = safe_request_failure(caught) or safe_request_failure(report.get("session_failure"))
        if session_failure:
            report["session_failure"] = session_failure
        report = self._timed_report({**report, "passed": False}, options, started_at, started_monotonic, result_unknown=error["result_unknown"], source_row=row)
        return {"kind": "failed", "report": report, "error": error,
                "fatal": not confirmed, "finished": monotonic(), "_exception": caught,
                "_fresh_scoped_report": fresh_scoped, "_no_write_proven": no_write_proven}

    def _test_once(self, options, proxy, row, number, identity, stopping, start_gate=None, *, _vault=None):
        # The complete account attempt includes validation and all existing
        # provider/readback retries. A retry is not a second billable sample.
        try:
            with measure_network_usage(propagate=False) as meter:
                result = self._test_once_unmetered(options, proxy, row, number, identity, stopping, start_gate, _vault=_vault)
        except BaseException as exc:
            try:
                exc._network_usage = safe_network_usage(meter.snapshot()) or _empty_network_usage()
            except (AttributeError, TypeError):
                pass
            raise
        if type(result) is dict and type(result.get("report")) is dict:
            result["report"]["network_usage"] = safe_network_usage(meter.snapshot()) or _empty_network_usage()
        return result

    def _test_once_unmetered(self, options, proxy, row, number, identity, stopping, start_gate=None, *, _vault=None):
        """Retry verified provider faults only before renewal or engagement writes."""
        from .provider_recovery import provider_failure, wait_for_provider
        if start_gate is not None:
            start_gate.wait()
        history = []
        started_at, started_monotonic = _now(), monotonic()
        def finalize(result):
            if result.get("fatal"):
                stopping.set()
            outcome = "test_failed" if result["kind"] == "failed" else result["kind"]
            result["report"] = self._test_report({**result["report"], "outcome": outcome,
                "retry_count": len(history) - 1, "provider_attempts": len(history), "attempt_history": history}, options, row)
            if history and options.get("proxy_test_session"):
                result["report"]["proxy_route_number"] = history[-1]["proxy_route_number"]
            result["report"].update(started_at=started_at, finished_at=_now(), elapsed_seconds=max(0.0, monotonic() - started_monotonic))
            return result

        previous = None
        used_routes = [proxy]
        for attempt in range(1, MAX_PROVIDER_ATTEMPTS + 1):
            if stopping.is_set():
                if previous is not None:
                    # A peer hold can arrive after this account's preflight but
                    # before its retry. Preserve the completed connection
                    # attempt instead of misreporting it as never started.
                    previous.update(kind="connection_pending", fatal=False)
                    previous["error"].update(code="connection_pending", result_unknown=False,
                        message="Connection recovery was deferred when the run stopped.")
                    previous["report"].update(error_code="connection_pending", result_unknown=False)
                    return finalize(previous)
                return {"kind": "skipped", "finished": monotonic()}
            self._retry_activity(options, row, number, attempt, "checking")
            result = self._test_attempt(options, proxy, row, number, identity, stopping, _vault=_vault)
            exc = result.pop("_exception", None)
            fresh_scoped = result.pop("_fresh_scoped_report", False) is True
            no_write_proven = result.pop("_no_write_proven", False) is True
            if result["kind"] == "skipped":
                if previous is not None:
                    previous.update(kind="connection_pending", fatal=False)
                    previous["error"].update(code="connection_pending", result_unknown=False,
                        message="Connection recovery was deferred when the run stopped.")
                    previous["report"].update(error_code="connection_pending", result_unknown=False)
                    return finalize(previous)
                return result
            previous = result
            report = result["report"]
            if result["kind"] in {"history_skipped", "like_history_pending"}:
                history.append({"attempt": attempt, "outcome": result["kind"]})
                return finalize(result)
            failure = safe_request_failure(exc) or safe_request_failure(report.get("session_failure"))
            provider = provider_failure(exc) or provider_failure(report)
            written = report.get("event_attempted") is True or report.get("mutation_attempted") is True
            recovery_allowed = (not isinstance(exc, (_JobFailure, KeyboardInterrupt, EOFError))
                                and result.get("error", {}).get("code") != "journal_failed")
            verification_pending = (recovery_allowed and result["kind"] == "failed"
                                    and self._accepted_like_verification_pending(options["action"], report))
            account_failure = recovery_allowed and failure.get("failure_category") == "account" and not written
            provider_issue = recovery_allowed and bool(provider) and not written
            renewal_unknown = (recovery_allowed and result["kind"] != "succeeded" and report.get("renewal_attempted") is True
                               and report.get("renewal_completed") is not True and not account_failure)
            session_review_pending = (renewal_unknown and no_write_proven and self._session_review_eligible(
                options["action"], report, failure, fresh_scoped=fresh_scoped))
            entry = {"attempt": attempt,
                     "outcome": "succeeded" if result["kind"] == "succeeded" else "verification_pending" if verification_pending else "session_review_pending" if session_review_pending else "account_failed" if account_failure else "provider_issue" if provider_issue and not renewal_unknown else "test_failed"}
            entry.update(self._route_metadata(options, row))
            entry.pop("proxy_pool_size", None)
            for field in ("session_failure", "proxy_failure"):
                if report.get(field):
                    entry[field] = report[field]
            history.append(entry)
            if verification_pending:
                result.update(kind="verification_pending", fatal=False)
                result["error"].update(code="verification_pending", result_unknown=False,
                    message="The like was accepted; saved-state verification is pending. The append was not resent.")
                # Retain the original read error and typed diagnostics in the
                # account report; only the scheduling outcome changes.
                report["result_unknown"] = False
            elif account_failure:
                try:
                    with (nullcontext(_vault) if _vault is not None else self._vault_factory(self.vault_path)) as vault:
                        record = vault.record(row)
                        try:
                            if self._identity(record) != identity:
                                raise _JobFailure({"code": "account_scope_invalid", "message": "The account changed before its review status could be saved."})
                        finally:
                            del record
                        vault.record_account_failure(row, failure)
                except BaseException:
                    result.update(kind="failed", fatal=True,
                                  error={"code": "journal_failed", "message": "The failed-account review status could not be saved.", "result_unknown": False})
                else:
                    result.update(kind="account_failed", fatal=failure.get("code") == "session_identity_mismatch")
                    result["error"]["result_unknown"] = False
            elif session_review_pending:
                try:
                    with (nullcontext(_vault) if _vault is not None else self._vault_factory(self.vault_path)) as vault:
                        record = vault.record(row)
                        try:
                            if self._identity(record) != identity:
                                raise _JobFailure({"code": "account_scope_invalid", "message": "The account changed before its saved-session hold could be recorded."})
                        finally:
                            del record
                        vault.record_session_review(row, failure, job_id=self.snapshot()["id"])
                except _JobFailure as exc:
                    entry["outcome"] = "test_failed"
                    result.update(kind="failed", fatal=True, error={**exc.details, "result_unknown": False})
                except BaseException:
                    entry["outcome"] = "test_failed"
                    result.update(kind="failed", fatal=True,
                                  error={"code": "journal_failed", "message": "The saved-session review hold could not be recorded. Review existing reports before another test.", "result_unknown": False})
                else:
                    result.update(kind="session_review_pending", fatal=False)
                    result["error"].update(code="session_review_pending", result_unknown=False,
                        message="This account is held for saved-session review. Its renewal was not repeated and no engagement was attempted.")
                    report.update(error_code="session_renewal_unknown", result_unknown=False, renewal_unknown=True)
            elif renewal_unknown:
                result.update(kind="failed", fatal=True)
                result["error"].update(code="session_renewal_unknown", result_unknown=True,
                    message="The session renewal result is unknown. Review this account before another preparation or test.")
                report.update(error_code="session_renewal_unknown", result_unknown=True, renewal_unknown=True)
            elif provider_issue:
                result["fatal"] = False
                can_retry = (attempt < MAX_PROVIDER_ATTEMPTS and not report.get("renewal_attempted")
                             and provider.get("retryable") is True and not stopping.is_set())
                if can_retry:
                    self._retry_activity(options, row, number, attempt, "waiting" if provider.get("rotate_route") is not True else "retrying")
                    can_retry = wait_for_provider(provider, stop=stopping)
                if can_retry:
                    try:
                        if provider.get("rotate_route") is True:
                            replacement = self._next_test_route(options, proxy, row, identity, used_routes=used_routes)
                            if options.get("proxy_test_session") and replacement is None:
                                can_retry = False
                            else:
                                proxy = replacement
                                used_routes.append(proxy)
                    except _JobFailure as exc:
                        result.update(kind="failed", fatal=True, error={**exc.details, "result_unknown": False})
                    else:
                        if can_retry:
                            continue
                if not can_retry and not result.get("fatal"):
                    result.update(kind="connection_pending", fatal=False)
                    result["error"].update(code="connection_pending", result_unknown=False,
                                           message="The account is waiting for a working connection; its account status was kept.")
                    report.update(error_code="connection_pending", result_unknown=False)
            return finalize(result)

    def _read_workbench(self, vault, options, proxy, row):
        try:
            with measure_network_usage(propagate=False) as meter:
                report = self._read_workbench_unmetered(vault, options, proxy, row)
        except BaseException as exc:
            try:
                exc._network_usage = safe_network_usage(meter.snapshot()) or _empty_network_usage()
            except (AttributeError, TypeError):
                pass
            raise
        if type(report) is dict:
            report["network_usage"] = safe_network_usage(meter.snapshot()) or _empty_network_usage()
        return report

    def _read_workbench_unmetered(self, vault, options, proxy, row):
        from .provider_recovery import provider_failure, wait_for_provider
        action, history = options["action"], []
        identity = options["_test_identities"][row]
        used_routes = [proxy]
        started_at, started_monotonic = _now(), monotonic()
        for attempt in range(1, MAX_PROVIDER_ATTEMPTS + 1):
            self._check_test_identity(vault, options, row)
            if read_test_song_id(self.vault_path.parent / "test-settings.json") != options["song_id"]:
                raise _JobFailure({"code": "song_scope_invalid", "message": "The declared song changed during the read-only test."})
            report, caught = {}, None
            try:
                with vault._http_session(row, proxy) as session:
                    if action == "check":
                        report = session.check(negative_control=True)
                        if (not isinstance(report, dict) or report.get("authenticated") is not True
                            or not isinstance(report.get("without_session"), dict)
                            or report["without_session"].get("authentication_rejected") is not True):
                            raise RequestFailure("session_control_failed", stage="negative_control")
                    else:
                        from .playback import song_summary
                        report = {**song_summary(session.song(options["song_id"])), "metadata_verified": True}
                        if report["song_id"] != options["song_id"]:
                            raise _JobFailure({"code": "song_scope_invalid", "message": "The metadata did not match the declared song."})
                    if proxy is not None:
                        report["proxy"] = session.proxy_summary
            except (SessionError, _JobFailure) as exc:
                caught = exc
            failure = safe_request_failure(caught)
            provider = provider_failure(caught)
            account_failed = failure.get("failure_category") == "account"
            entry = {"attempt": attempt, "outcome": "succeeded" if caught is None else "provider_issue" if provider else "account_failed" if account_failed else "test_failed"}
            entry.update(self._route_metadata(options, row))
            entry.pop("proxy_pool_size", None)
            if failure:
                entry["session_failure"] = failure
                report["session_failure"] = failure
            proxy_failure = safe_proxy_country_failure(caught)
            if proxy_failure:
                entry["proxy_failure"] = proxy_failure
                report["proxy_failure"] = proxy_failure
            history.append(entry)
            if provider and attempt < MAX_PROVIDER_ATTEMPTS and provider.get("retryable") is True and wait_for_provider(provider):
                if provider.get("rotate_route") is True:
                    replacement = self._next_test_route(options, proxy, row, identity, used_routes=used_routes)
                    if not options.get("proxy_test_session") or replacement is not None:
                        proxy = replacement
                        used_routes.append(proxy)
                        continue
                else:
                    continue
            if account_failed:
                try:
                    vault.record_account_failure(row, failure)
                except Exception:
                    raise _JobFailure({"code": "journal_failed", "message": "The account review status could not be saved."}) from None
                if failure.get("code") == "session_identity_mismatch":
                    raise _JobFailure({"code": "account_scope_invalid", "message": "The server identity did not match the selected account. Its session was kept for review."})
            elif caught is not None and not provider:
                raise caught
            outcome = "connection_pending" if provider else "account_failed" if account_failed else "succeeded"
            self._update(connection_pending=self._latest["connection_pending"] + bool(provider),
                         account_failed=self._latest["account_failed"] + account_failed,
                         retried=self._latest["retried"] + (attempt > 1), provider_retries=self._latest["provider_retries"] + attempt - 1)
            return self._timed_report({**report, "passed": caught is None, "source_row": row, "outcome": outcome,
                                      "retry_count": attempt - 1, "provider_attempts": attempt, "attempt_history": history},
                                     options, started_at, started_monotonic, source_row=row)

    @staticmethod
    def _test_worker_count(options, identities):
        return min(options["workers"], len(set(identities.values())), len(options["rows"]) * options["count"])

    def _parallel_tests(self, options, proxy, identities):
        """Coordinate bounded per-test tasks; workers never share vault/HTTP state."""
        pending = deque((row, 1) for row in options["rows"])
        active, active_identities, stopping = {}, set(), self._stop_requested
        maximum = options["max_consecutive_failures"]
        effective = self._test_worker_count(options, identities)
        strict_active_budget = effective <= maximum
        stats = {name: 0 for name in ("attempted", "succeeded", "failed", "skipped", "completed_tests", "consecutive_failures",
                                    "connection_pending", "verification_pending", "session_review_pending", "retried", "provider_retries", "account_failed", *_WRITE_COUNTERS)}
        if options["action"] == "like":
            stats.update({name: self._latest.get(name, 0) for name in (
                "already_liked_skipped", "like_verification_held", "unknown_like_held", "history_skipped_tests", "history_held_tests", "cached_like_skips", "cached_like_holds")})
        terminal, reason, storage_failed = None, None, False
        last_failure = None
        job_id = self.snapshot()["id"]
        def stop(error, why):
            nonlocal terminal, reason
            if terminal is None:
                terminal, reason = error, why
            stopping.set()
        def publish():
            nonlocal storage_failed
            visible = {(item.get("source_row"), item.get("test_number")): item for item in self._latest.get("active_tests", [])}
            fields = {**stats, "requested_workers": options["workers"], "effective_workers": effective,
                      "failure_limit_scope": "active_budget" if strict_active_budget else "new_dispatch",
                      "active_workers": len(active), "active_rows": sorted(item[0] for item in active.values()),
                      "active_tests": [{**visible.get((item[0], item[1]), {}), "source_row": item[0], "test_number": item[1], "started_at": item[3], **self._route_metadata(options, item[0])}
                                       for item in sorted(active.values(), key=lambda item: (item[0], item[1]))],
                      "progress": {"completed": stats["succeeded"] + stats.get("cached_like_skips", 0) + stats.get("cached_like_holds", 0), "total": self._latest["progress"]["total"]},
                      "stop_reason": reason, "phase": "executing", "message": f"Finished {stats['completed_tests']} test(s); {len(active)} worker(s) active."}
            if not storage_failed:
                try:
                    self._update(**fields)
                    return True
                except BaseException:
                    storage_failed = True
                    stop({"code": "journal_failed", "message": "The job report could not be saved. Existing account reports must be reviewed before another test."}, "journal_failed")
                    fields["stop_reason"] = reason
            with self._lock:
                self._latest.update(deepcopy(fields))
            return False
        def consume(future):
            nonlocal last_failure, storage_failed
            row, number, identity, started_at, started_monotonic = active.pop(future)
            active_identities.remove(identity)
            try:
                result = future.result()
            except _JobFailure as exc:
                stopping.set()
                result = {"kind": "failed", "report": self._timed_report({"passed": False, "network_usage": safe_network_usage(getattr(exc, "_network_usage", None)) or _empty_network_usage()}, options, started_at, started_monotonic, source_row=row), "fatal": True,
                          "error": {**exc.details, "source_row": row, "test_number": number, "result_unknown": False}}
            except BaseException as exc:
                stopping.set()
                result = {"kind": "failed", "report": self._timed_report({"passed": False, "network_usage": safe_network_usage(getattr(exc, "_network_usage", None)) or _empty_network_usage()}, options, started_at, started_monotonic, result_unknown=True, source_row=row), "fatal": True,
                          "error": {"code": "worker_failed", "message": "A worker stopped without a confirmed test outcome.", "source_row": row, "test_number": number, "result_unknown": True}}
            if result["kind"] == "skipped":
                stats["attempted"] -= 1
                return
            cached = result["kind"] in {"history_skipped", "like_history_pending"}
            if cached:
                stats["attempted"] -= 1
            else:
                stats["completed_tests"] += 1
            for key, value in _write_counts(options["action"], result.get("report", {})).items():
                stats[key] += value
            retry_count = result.get("report", {}).get("retry_count", 0)
            stats["retried"] += retry_count > 0
            stats["provider_retries"] += retry_count
            if result["kind"] == "succeeded":
                stats["succeeded"] += 1
                if reason != "consecutive_failure_limit":
                    stats["consecutive_failures"] = 0
            elif result["kind"] == "history_skipped":
                stats["already_liked_skipped"] += 1
                stats["history_skipped_tests"] += 1
                stats["cached_like_skips"] += 1
            elif result["kind"] == "like_history_pending":
                field = "like_verification_held" if result["report"].get("history_status") == "verification_pending" else "unknown_like_held"
                stats[field] += 1
                stats["history_held_tests"] += 1
                stats["cached_like_holds"] += 1
            elif result["kind"] == "connection_pending":
                stats["connection_pending"] += 1
            elif result["kind"] == "verification_pending":
                stats["verification_pending"] += 1
            elif result["kind"] == "session_review_pending":
                stats["session_review_pending"] += 1
            else:
                stats["failed"] += 1
                stats["account_failed"] += result["kind"] == "account_failed"
                stats["consecutive_failures"] = min(maximum, stats["consecutive_failures"] + 1)
                last_failure = result["error"]
                if result["fatal"]:
                    stop(last_failure, "result_unknown" if last_failure.get("result_unknown") else "immediate_failure")
                elif stats["consecutive_failures"] >= maximum:
                    stop({**last_failure, "code": "consecutive_failure_limit", "message": "The configured consecutive failure limit was reached. Remaining tests stopped without automatic retries."}, "consecutive_failure_limit")
            report = {**result.get("report", {}), "source_row": row, "test_number": number}
            report["result_unknown"] = result.get("error", {}).get("result_unknown", False)
            if result["kind"] == "failed" and "error_code" not in report:
                report["error_code"] = "test_failed"
            safe = _public_report(report)
            with self._lock:
                safe.update(self._record_network_result(safe))
            if not storage_failed:
                try:
                    _journal(safe, self.vault_path.parent / "ui-test-reports" / job_id / f"account-{row}.{options['action']}-{number}.redacted.json")
                except BaseException:
                    storage_failed = True
                    stop({"code": "journal_failed", "message": "A per-test report could not be saved. Review existing account reports before another test."}, "journal_failed")
            with self._lock:
                self._latest["network_usage"] = self._current_network_summary()
                self._latest["results"].append(safe)
                self._latest["results_total"] += 1
                excess = len(self._latest["results"]) - MAX_RETAINED_RESULTS
                if excess > 0:
                    del self._latest["results"][:excess]
                    self._latest["results_truncated"] += excess
            if result["kind"] in {"account_failed", "verification_pending", "session_review_pending", "history_skipped", "like_history_pending"}:
                # An unresolved accepted like must not be repeated by another
                # selected alias. Unrelated account identities continue.
                pending_copy = [item for item in pending if identities[item[0]] != identity]
                pending.clear()
                pending.extend(pending_copy)
            if terminal is None and result["kind"] not in {"account_failed", "connection_pending", "verification_pending", "session_review_pending", "history_skipped", "like_history_pending"} and number < options["count"]:
                pending.appendleft((row, number + 1))
        def drain_finished(initial=()):
            """Observe every completed peer before dispatching replacement work."""
            completed = set(initial).intersection(active)
            drained = False
            while True:
                completed.update(future for future in active if future.done())
                if not completed:
                    return drained
                for future in sorted(completed, key=lambda item: item.result().get("finished", 0) if item.exception() is None else 0):
                    consume(future)
                    drained = True
                # A peer may finish while another result is being journaled.
                completed = set()
        with ThreadPoolExecutor(max_workers=effective, thread_name_prefix="ui-test") as executor:
            while active or pending and terminal is None:
                if drain_finished():
                    publish()
                while (terminal is None and not stopping.is_set() and pending and len(active) < effective
                       and (not strict_active_budget or len(active) < maximum - stats["consecutive_failures"])):
                    if drain_finished():
                        publish()
                    if (terminal is not None or stopping.is_set() or not pending
                        or (strict_active_budget and len(active) >= maximum - stats["consecutive_failures"])):
                        break
                    selected = None
                    for _ in range(len(pending)):
                        row, number = pending.popleft()
                        if identities[row] not in active_identities:
                            selected = row, number
                            break
                        pending.append((row, number))
                    if selected is None:
                        break
                    row, number = selected
                    start_gate = threading.Event()
                    try:
                        selected_proxy = self._row_proxy(options, proxy, row)
                        if drain_finished():
                            publish()
                        if (terminal is not None or stopping.is_set()
                            or (strict_active_budget and len(active) >= maximum - stats["consecutive_failures"])):
                            pending.appendleft((row, number))
                            break
                        future = executor.submit(self._test_once, options, selected_proxy, row, number, identities[row], stopping, start_gate)
                    except BaseException:
                        stop({"code": "worker_failed", "message": "A local test worker could not start. No automatic retry was made."}, "immediate_failure")
                        break
                    active[future] = row, number, identities[row], _now(), monotonic()
                    active_identities.add(identities[row])
                    stats["attempted"] += 1
                    try:
                        publish()
                    finally:
                        # No vault/account call starts before its dispatch intent
                        # is durable. A failed journal sets stopping before release.
                        start_gate.set()
                if active:
                    done, _ = wait(active, timeout=0.25, return_when=FIRST_COMPLETED)
                    if drain_finished(done):
                        publish()
                elif stopping.is_set() or terminal is not None:
                    break
            stats["skipped"] = self._latest["progress"]["total"] - stats["attempted"]
            publish()
        if terminal is not None:
            raise _JobFailure(terminal)
        if stats["failed"]:
            try:
                self._update(stop_reason="completed_with_failures")
            except Exception:
                raise _JobFailure({"code": "journal_failed", "message": "The final job report could not be saved. Review existing account reports before another test."}) from None
            raise _JobFailure({**(last_failure or {}), "code": "completed_with_failures", "message": "All planned tests finished, with confirmed failures. Review the account reports before another run."})

    def _serial_test_state(self, row, *, outcome=None, test_number=1, started_at=None, route_metadata=None, retry_count=0, report=None):
        with self._lock:
            if outcome is None:
                fields = {"attempted": self._latest["attempted"] + 1, "active_workers": 1, "active_rows": [row],
                          "active_tests": [{"source_row": row, "test_number": test_number, "started_at": started_at or _now(), **(route_metadata or {})}]}
            else:
                cached = outcome in {"history_skipped", "like_history_pending"}
                fields = {"completed_tests": self._latest["completed_tests"] + (not cached),
                          "succeeded": self._latest["succeeded"] + (outcome == "succeeded"),
                          "failed": self._latest["failed"] + (outcome in {"failed", "account_failed"}),
                          "account_failed": self._latest["account_failed"] + (outcome == "account_failed"),
                          "connection_pending": self._latest["connection_pending"] + (outcome == "connection_pending"),
                          "verification_pending": self._latest["verification_pending"] + (outcome == "verification_pending"),
                          "session_review_pending": self._latest["session_review_pending"] + (outcome == "session_review_pending"),
                          "retried": self._latest["retried"] + (retry_count > 0),
                          "provider_retries": self._latest["provider_retries"] + retry_count,
                          "active_workers": 0, "active_rows": [], "active_tests": [],
                          "consecutive_failures": 0 if outcome == "succeeded" else self._latest["consecutive_failures"] if outcome in {"connection_pending", "verification_pending", "session_review_pending", "history_skipped", "like_history_pending"} else 1}
                if cached:
                    fields["attempted"] = self._latest["attempted"] - 1
                    if outcome == "history_skipped":
                        fields.update(already_liked_skipped=self._latest["already_liked_skipped"] + 1,
                                      history_skipped_tests=self._latest["history_skipped_tests"] + 1,
                                      cached_like_skips=self._latest["cached_like_skips"] + 1)
                    else:
                        counter = "like_verification_held" if report.get("history_status") == "verification_pending" else "unknown_like_held"
                        fields.update({counter: self._latest[counter] + 1,
                                       "history_held_tests": self._latest["history_held_tests"] + 1,
                                       "cached_like_holds": self._latest["cached_like_holds"] + 1})
                fields.update({key: self._latest[key] + value
                               for key, value in _write_counts(self._latest["action"], report).items()})
            try:
                self._update(**fields)
            except BaseException:
                if outcome is not None:
                    self._latest.update(fields)
                raise _JobFailure({"code": "journal_failed", "message": "The local job report could not be saved. Review account reports before another test."}) from None

    def _perform(self, vault, options, proxy):
        action = options["action"]
        if action == "review-sessions":
            completed = 0
            for row in options["rows"]:
                self._check_test_identity(vault, options, row)
                self._phase("session_review", f"Checking row {row}'s saved session using read-only validation.")
                try:
                    report = vault.review_saved_session(row, **({"proxy": proxy} if proxy is not None else {}))
                    if (type(report) is not dict or type(report.get("source_row")) is not int or report.get("source_row") != row
                        or report.get("passed") is not True or report.get("session_review_cleared") is not True
                        or report.get("authenticated") is not True or report.get("negative_control_passed") is not True
                        or report.get("server_account_identity_verified") is not True
                        or type(report.get("cleared_rows")) is not list or not all(_positive_int(item) for item in report["cleared_rows"])
                        or row not in report["cleared_rows"]):
                        raise _JobFailure({"code": "account_scope_invalid", "message": "Saved-session review did not return the required account-bound validation proof."})
                except RequestFailure as exc:
                    failure = safe_request_failure(exc)
                    if failure.get("code") == "session_identity_mismatch":
                        raise _JobFailure({"code": "session_identity_mismatch", "message": "Saved-session review found a different server account identity. The hold was kept."}) from None
                    report = {"source_row": row, "passed": False, "session_review_cleared": False,
                              "outcome": "session_review_pending", "session_failure": failure,
                              "error_code": failure["code"]}
                except ProxyCountryError as exc:
                    report = {"source_row": row, "passed": False, "session_review_cleared": False,
                              "outcome": "session_review_pending", "proxy_failure": safe_proxy_country_failure(exc),
                              "error_code": "proxy_preflight_failed"}
                completed += report.get("passed") is True
                self._update(session_review_pending=self._latest["session_review_pending"] + (report.get("passed") is not True),
                             session_reviews_cleared=self._latest["session_reviews_cleared"] + (report.get("passed") is True))
                self._append(report, completed=completed)
            return
        if action in {"prepare", "preview"}:
            from .preparation import prepare_test_accounts
            if action == "prepare" and options["workers"] > 1:
                from .ui_preparation import run_parallel_ui_preparation
                self._phase("preparing", f"Preparing registered accounts with {options['workers']} HTTP workers.")
                report = run_parallel_ui_preparation(
                    vault, options, job_id=self.snapshot()["id"],
                    progress=self._preparation_progress, pool_loader=self._pool_loader,
                )
                self._append(report, completed=report["prepared_account_count"])
                self._update(connection_pending=report.get("connection_pending_count", 0),
                             account_failed=report.get("account_failed_count", 0),
                             preparation_held=report.get("preparation_held", 0),
                             preparation_infrastructure_failed=report.get("infrastructure_failed_count", 0),
                             active_workers=0, active_rows=[])
                self._update(**{name: value for name, value in _public_report(report).items()
                                if name in {"requested_workers", "effective_workers", "failure_limit_scope"}})
                if report.get("pause_reason") == "stop_requested" and self._stop_requested.is_set():
                    return
                if report.get("phase") != "complete":
                    if report.get("pause_reason") == "ui_unavailable":
                        from .country_preparation import safe_ui_read_failure
                        details = {"code": "ui_status_unavailable", "pause_reason": "ui_unavailable",
                                   "message": "Preparation paused because the local UI job status could not be read. Prepared sessions are saved; the failure limit was not reached."}
                        diagnostic = safe_ui_read_failure(report.get("ui_read_failure"))
                        if diagnostic:
                            details["ui_read_failure"] = diagnostic
                        raise _JobFailure(details)
                    raise _JobFailure({"code": "preparation_failed", "message": "Preparation paused; review its saved checkpoint before continuing."})
                if report.get("account_failed_count"):
                    raise _JobFailure({"code": "completed_with_failures", "message": "Preparation finished; confirmed account failures are available for review."})
                if report.get("infrastructure_failed_count"):
                    raise _JobFailure({"code": "completed_with_failures", "message": "Preparation finished; infrastructure errors require review and were not counted as account rejections."})
                if report.get("passed") is True or report.get("connection_pending_count") or report.get("preparation_held"):
                    return
                raise _JobFailure({"code": "preparation_failed", "message": "Account preparation did not complete. Check its saved checkpoint."})
            proxy_factory = None
            if options["proxy_sticky_pool"] and action != "preview":
                pool = self._pool_loader(self.vault_path.parent / "packetstream-sticky-pool.dpapi")
                cursor_path = self.vault_path.parent / "ui-sticky-pool-position.json"
                fingerprint = pool.fingerprint()
                cursor = 0
                if cursor_path.exists():
                    try:
                        saved_cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
                        if (
                            type(saved_cursor) is not dict or set(saved_cursor) != {"fingerprint", "cursor"}
                            or type(saved_cursor["fingerprint"]) is not str or len(saved_cursor["fingerprint"]) != 64
                            or any(char not in "0123456789abcdef" for char in saved_cursor["fingerprint"])
                            or type(saved_cursor["cursor"]) is not int or not 0 <= saved_cursor["cursor"] < 2**63
                        ):
                            raise ValueError
                        # A validated replacement list begins at its first
                        # route. Re-saving the same list preserves its position.
                        cursor = saved_cursor["cursor"] if saved_cursor["fingerprint"] == fingerprint else 0
                    except Exception:
                        raise SessionError("The saved sticky pool position is invalid or belongs to another pool.") from None
                def proxy_factory(_row):
                    nonlocal cursor
                    selected = pool.proxy_for_index(cursor)
                    cursor += 1
                    _journal({"fingerprint": fingerprint, "cursor": cursor}, cursor_path)
                    return selected
            country = options.get("account_country")
            message = (f"Selecting random {country}-tagged registered accounts. " if country else "")
            message += "Selecting additional unique registered accounts." if action == "preview" else "Reusing and validating registered account sessions without a browser." if options["no_browser"] else "Preparing normal saved account sessions."
            self._phase("preview" if action == "preview" else "preparing", message)
            preparation_progress = self._preparation_progress
            if action == "prepare":
                preparation_progress = lambda value: self._preparation_progress(self._serial_preparation_metrics(value))
            report = prepare_test_accounts(
                vault, count=options["count"], start_row=options["start_row"], proxy=proxy,
                browser_backend=options["browser"], headless=options["headless"],
                dry_run=action == "preview", progress=preparation_progress,
                **({"reduce_browser_data": True} if options["reduce_browser_data"] else {}),
                **({"no_browser": True} if options["no_browser"] else {}),
                **({"proxy_factory": proxy_factory} if proxy_factory is not None else {}),
                **({"selected_rows": options["review_rows"]} if options.get("review_rows") else {}),
                **({"account_country": country} if country else {}),
                **({"should_stop": self._stop_requested.is_set} if action == "prepare" else {}),
            )
            if action == "prepare":
                report = self._serial_preparation_metrics(report)
                self._preparation_progress(report)
            if action == "preview" and options["proxy_egypt"]:
                # Preview selects a future route without unlocking credentials
                # or making requests. Its report must retain that selection.
                report = {**report, "connection": "proxy_egypt"}
            self._append(report, completed=options["count"] if action == "preview" else report["prepared_account_count"])
            if action == "prepare" and report.get("pause_reason") == "stop_requested" and self._stop_requested.is_set():
                return
            if action == "prepare" and report.get("phase") == "complete":
                pending = report.get("connection_pending_rows", [])
                account_failed = report.get("account_failed_rows", [])
                self._update(connection_pending=len(pending), account_failed=len(account_failed))
                if account_failed:
                    raise _JobFailure({"code": "completed_with_failures", "message": "Preparation finished; confirmed account failures are available for review."})
                if pending:
                    return
            if action == "prepare" and report.get("passed") is not True:
                raise _JobFailure({"code": "preparation_failed", "message": "Account preparation did not complete. Check the preparation report before continuing."})
            return
        completed = 0
        pending_review_identities = set()
        for row in options["rows"]:
            if action in {"play", "like"} and self._stop_requested.is_set():
                break
            if action in {"play", "like"} and options["_test_identities"][row] in pending_review_identities:
                continue
            selected_proxy = self._row_proxy(options, proxy, row)
            if action in {"play", "like"}:
                stopping = self._stop_requested
                for number in range(1, options["count"] + 1):
                    if stopping.is_set():
                        break
                    self._phase("executing", f"Row {row}: {action} test {number} of {options['count']}.")
                    started_at = _now()
                    self._serial_test_state(row, test_number=number, started_at=started_at, route_metadata=self._route_metadata(options, row))
                    result = self._test_once(options, selected_proxy, row, number, options["_test_identities"][row], stopping, _vault=vault)
                    kind = result["kind"]
                    if kind == "skipped":
                        self._update(attempted=self._latest["attempted"] - 1, active_workers=0, active_rows=[], active_tests=[])
                        break
                    self._serial_test_state(row, outcome=kind, retry_count=result.get("report", {}).get("retry_count", 0), report=result.get("report"))
                    report = {**result["report"], "source_row": row, "test_number": number}
                    completed += kind in {"succeeded", "history_skipped", "like_history_pending"}
                    self._append(report, completed=completed)
                    if kind in {"failed", "account_failed"}:
                        raise _JobFailure(result["error"])
                    if kind in {"verification_pending", "session_review_pending", "history_skipped", "like_history_pending"}:
                        pending_review_identities.add(options["_test_identities"][row])
                        break
                    if kind == "connection_pending":
                        break
                    selected_proxy = self._row_proxy(options, selected_proxy, row)
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
                    self._check_test_identity(vault, options, row)
                    self._phase("checking" if action == "check" else "song_metadata", f"Checking row {row}'s saved session." if action == "check" else f"Reading test song metadata for row {row}.")
                    report = self._read_workbench(vault, options, selected_proxy, row)
                completed += report.get("passed", True) is True
                self._append({**self._test_report(report, options, row), "source_row": row}, completed=completed)

    @staticmethod
    def _report_signature(path):
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    @classmethod
    def _changed_report(cls, path, before):
        return _public_report(cls._changed_raw_report(path, before))

    @classmethod
    def _changed_raw_report(cls, path, before):
        if cls._report_signature(path) == before:
            return {}
        try:
            if path.stat().st_size > 256_000:
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if type(value) is dict else {}
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
        if getattr(exc, "code", None) == "like_history_failed":
            return {"code": "journal_failed", "message": "The like history could not be saved. No further likes were dispatched; review existing reports before another test."}
        if action in {"play", "like"}:
            return {"code": "test_failed", "message": "The test stopped on its first failure. An attempted write may be unknown; check the account report before running it again. No automatic retry was made."}
        if action == "proxy-check":
            return {"code": "proxy_check_failed", "message": "Egypt proxy verification failed. Check the saved proxy credentials, balance, and route availability. No direct fallback was made."}
        if action == "prepare":
            storage_failure = safe_session_storage_failure(exc) or safe_session_storage_failure(getattr(exc, "storage_failure", None))
            if storage_failure:
                return {
                    "code": storage_failure["code"],
                    "message": "The local session save could not finish. This is a storage issue; the account was not marked as rejected.",
                    "session_storage_failure": storage_failure,
                }
            try:
                request_failure = safe_request_failure(exc)
            except (AttributeError, TypeError, ValueError):
                request_failure = {}
            proxy_failure = safe_proxy_country_failure(exc)
            renewal_unknown = getattr(exc, "renewal_unknown", False) is True
            if request_failure or proxy_failure or renewal_unknown:
                if renewal_unknown:
                    code = "session_renewal_unknown"
                    message = "Session renewal could not be confirmed. Review this account before retrying; the renewal was not repeated."
                elif request_failure:
                    code = request_failure["code"]
                    messages = {
                        "request_transport_failed": "The connection failed during saved-session read validation. This does not prove the account is invalid.",
                        "request_http_failed": "Saved-session validation did not receive a successful read response. This does not prove the account is invalid.",
                        "request_rate_limited": "Session validation received a rate limit. A cooldown is required before more validation requests.",
                        "request_proxy_unverified": "The selected proxy route could not be verified. Validation cannot use an unverified route.",
                        "session_authentication_rejected": "The service rejected the saved account session. This account needs review.",
                        "session_identity_mismatch": "The server account identity did not match the selected account. This account needs review.",
                        "session_response_invalid": "Session validation could not verify the service response. Review the account before retrying.",
                        "session_control_failed": "The anonymous access check did not reject access. Session validation needs review.",
                    }
                    message = messages[code]
                    if request_failure.get("http_status") == 429:
                        message = messages["request_rate_limited"]
                else:
                    code = "proxy_preflight_failed"
                    if proxy_failure.get("http_status") == 429 or proxy_failure.get("proxy_connect_http_status") == 429:
                        message = "Egypt route validation received a rate limit. A cooldown is required before this route can be used."
                    elif proxy_failure.get("failure_kind") == "authentication_rejected":
                        message = "Egypt proxy authentication was rejected. Check the proxy credentials and balance; the account was not rejected."
                    else:
                        message = "Egypt proxy validation failed before the account could be prepared. Check the route connection and country verification."
                error = {"code": code, "message": message + (" No browser login was attempted." if no_browser else "")}
                if request_failure:
                    error["session_failure"] = request_failure
                if proxy_failure:
                    error["proxy_failure"] = proxy_failure
                if renewal_unknown:
                    error["renewal_unknown"] = True
                error.update(_public_report({name: getattr(exc, name, None) for name in (
                    "renewal_completed", "validation_pending", "session_validation_attempts", "session_validation_retries",
                    "attach_validation_attempts", "attach_validation_retries",
                ) if hasattr(exc, name)}))
                return error
        if action == "prepare" and no_browser:
            return {"code": "session_preparation_failed", "message": "Registered session recovery or validation failed. Check the selected source row, session validity, and connection. No browser login was attempted."}
        if action in {"prepare", "login"}:
            return {"code": "session_preparation_failed", "message": "Normal login or saved-session validation failed. Check the selected source row and proxy configuration. Previously saved sessions were kept."}
        if action == "preview":
            return {"code": "selection_failed", "message": "The requested selection is unavailable. Check the start row or choose fewer accounts."}
        return {"code": "session_check_failed", "message": "The saved-session or test song check failed. Refresh the selected session or check the proxy configuration."}
