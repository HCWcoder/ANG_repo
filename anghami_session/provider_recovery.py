"""Bounded prewrite recovery with shared rate-limit cooldowns and safe facts."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import threading
import time

from .errors import safe_request_failure
from .proxy import ProxyCountryError, safe_proxy_country_failure

MAX_PROVIDER_ATTEMPTS = 3
MAX_COOLDOWN_WAIT_SECONDS = 120
DEFAULT_COOLDOWN_SECONDS = 15
_lock = threading.Lock()
_deadlines = {}
_clock = time.monotonic


def retry_after_seconds(headers):
    """Parse only Retry-After; never retain or expose arbitrary response headers."""
    try:
        raw = headers.get("Retry-After") if headers is not None else None
        if raw is None and headers is not None:
            raw = headers.get("retry-after")
        if not isinstance(raw, str) or len(raw) > 128:
            return None
        if raw.isascii() and raw.isdecimal():
            value = int(raw)
        else:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                return None
            value = max(0, (parsed - datetime.now(timezone.utc)).total_seconds())
        return value if 0 <= value <= 86400 else 86400
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def provider_failure(value):
    """Normalize provider diagnostics without treating an account as rejected."""
    if type(value) is dict and isinstance(value.get("session_failure"), dict):
        value = value["session_failure"]
    result = safe_request_failure(value)
    if result:
        return result if result["failure_category"] == "provider" else {}
    raw = value
    if isinstance(value, ProxyCountryError):
        raw = safe_proxy_country_failure(value)
    elif type(value) is dict and isinstance(value.get("proxy_failure"), dict):
        raw = value["proxy_failure"]
    if type(raw) is not dict:
        return {}
    try:
        proxy = ProxyCountryError(raw.get("failure_kind"), curl_code=raw.get("curl_code"),
                                  http_status=raw.get("http_status"),
                                  proxy_connect_http_status=raw.get("proxy_connect_http_status"),
                                  country_check_attempts=raw.get("country_check_attempts", 1),
                                  retry_after_seconds=raw.get("retry_after_seconds"))
    except (TypeError, ValueError):
        return {}
    safe = safe_proxy_country_failure(proxy)
    rate = safe.get("http_status") == 429 or safe.get("proxy_connect_http_status") == 429
    certificate = safe.get("curl_code") in {51, 58, 60, 77, 82, 83, 90, 91, 98}
    retryable = not certificate and safe.get("failure_kind") != "authentication_rejected" and safe.get("http_status") not in {401, 403, 407}
    safe_retry = raw.get("retryable", True) is True if type(value) is dict else getattr(value, "retry_safe", True) is not False
    result = {**safe, "code": "request_rate_limited" if rate else "request_transport_failed",
              "stage": "country_check", "failure_category": "provider",
              "retryable": retryable and safe_retry,
              "rotate_route": retryable and safe_retry and not rate}
    return result


def observe_provider_failure(value):
    """Register an actual 429 even when its request cannot be retried."""
    failure = provider_failure(value)
    if not failure:
        return
    rate = failure.get("code") == "request_rate_limited" or failure.get("http_status") == 429
    if not rate:
        return
    key = "country_check" if failure.get("stage") == "country_check" else "anghami"
    with _lock:
        seconds = max(5, failure.get("retry_after_seconds", DEFAULT_COOLDOWN_SECONDS))
        _deadlines[key] = max(_deadlines.get(key, 0), _clock() + seconds)


def wait_for_provider(value, stop=None):
    """Honor shared 429 cooldowns; defer rather than send before a long deadline."""
    failure = provider_failure(value)
    if not failure or failure.get("retryable") is not True:
        return False
    observe_provider_failure(value)
    key = "country_check" if failure.get("stage") == "country_check" else "anghami"
    started = _clock()
    return _wait_deadline(key, started, stop)


def _wait_deadline(key, started, stop):
    with _lock:
        deadline = _deadlines.get(key, 0)
    if deadline - started > MAX_COOLDOWN_WAIT_SECONDS:
        return False
    while True:
        if stop is not None and stop.is_set():
            return False
        with _lock:
            remaining = _deadlines.get(key, 0) - _clock()
        if remaining <= 0:
            return True
        if _clock() - started + remaining > MAX_COOLDOWN_WAIT_SECONDS:
            return False
        seconds = min(0.25, remaining)
        if stop is None:
            time.sleep(seconds)
        elif stop.wait(seconds):
            return False


def wait_before_provider_request(stage, stop=None):
    """New requests also respect a cooldown established by another worker."""
    key = "country_check" if stage == "country_check" else "anghami"
    return _wait_deadline(key, _clock(), stop)
