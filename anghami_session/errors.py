import math
from curl_cffi.const import CurlECode


class SessionError(RuntimeError):
    """A session error whose message is safe to display without exposing tokens."""


class SessionReviewRequiredError(SessionError):
    """A durable account hold must not become a login fallback or rejection."""

    def __init__(self):
        super().__init__("This account needs an explicit saved-session review before preparation or testing.")


class SessionStorageError(SessionError):
    """Only fixed local storage facts; never retain an SQLite/DPAPI message."""

    def __init__(self, *, operation, phase, sqlite_code=None, attempts=1):
        if (operation not in {"pending_save", "verified_save"}
                or phase not in {"encryption", "transaction"}
                or type(attempts) is not int or not 1 <= attempts <= 3
                or sqlite_code is not None and (type(sqlite_code) is not int or not 1 <= sqlite_code <= 255)):
            raise ValueError("Use fixed session storage evidence.")
        message = ("The pending session could not be saved securely." if operation == "pending_save"
                   else "The verified session could not be saved securely.")
        super().__init__(message)
        self.operation, self.phase = operation, phase
        self.sqlite_code, self.attempts = sqlite_code, attempts
        self.retry_safe = False


def safe_session_storage_failure(value):
    if isinstance(value, SessionStorageError):
        raw = {"operation": value.operation, "phase": value.phase,
               "attempts": value.attempts, "sqlite_code": value.sqlite_code}
    elif type(value) is dict:
        if set(value) - {"code", "operation", "phase", "attempts", "sqlite_code"}:
            return {}
        raw = value
    else:
        return {}
    try:
        item = SessionStorageError(operation=raw.get("operation"), phase=raw.get("phase"),
                                   attempts=raw.get("attempts"), sqlite_code=raw.get("sqlite_code"))
    except (TypeError, ValueError, AttributeError):
        return {}
    code = ("session_protection_failed" if item.phase == "encryption" else
            "session_store_busy" if item.sqlite_code in {5, 6} else "session_store_failed")
    if type(value) is dict and value.get("code") != code:
        return {}
    result = {"code": code, "operation": item.operation, "phase": item.phase, "attempts": item.attempts}
    if item.sqlite_code is not None:
        result["sqlite_code"] = item.sqlite_code
    return result


REQUEST_FAILURE_CODES = frozenset({
    "request_transport_failed", "request_http_failed", "request_rate_limited", "request_proxy_unverified",
    "session_authentication_rejected", "session_response_invalid",
    "session_control_failed", "session_identity_mismatch",
})
REQUEST_FAILURE_STAGES = frozenset({
    "relations", "playlists", "negative_control", "identity", "metadata",
    "likes_read", "song_metadata", "preflight", "country_check",
    "session_recovery_preflight", "session_recovery_renewal", "session_recovery_validation",
})
ACCOUNT_FAILURE_CODES = frozenset({"session_authentication_rejected", "session_identity_mismatch"})
_REQUEST_MESSAGES = {
    "request_transport_failed": "The connection could not complete the read request.",
    "request_http_failed": "The service did not return a successful read response.",
    "request_rate_limited": "The service requested a cooldown before more requests.",
    "request_proxy_unverified": "The configured proxy route was not confirmed. No direct fallback is permitted.",
    "session_authentication_rejected": "The service rejected the saved account session.",
    "session_response_invalid": "The read response could not be verified safely.",
    "session_control_failed": "The anonymous authentication control did not reject access.",
    "session_identity_mismatch": "The server account identity did not match the selected account.",
}


class RequestFailure(SessionError):
    """A typed read failure; never retain URLs, headers, replies or exception text."""

    def __init__(self, code, *, stage="preflight", http_status=None, curl_code=None,
                 retry_after_seconds=None, retry_safe=True):
        if type(code) is not str or code not in REQUEST_FAILURE_CODES or type(stage) is not str or stage not in REQUEST_FAILURE_STAGES:
            raise ValueError("Use a supported request failure code and stage.")
        super().__init__(_REQUEST_MESSAGES[code])
        self.code, self.stage = code, stage
        self.http_status = _http_status(http_status)
        self.curl_code = int(curl_code) if (type(curl_code) is int or isinstance(curl_code, CurlECode)) and 1 <= curl_code <= 999 else None
        self.retry_after_seconds = (
            retry_after_seconds if type(retry_after_seconds) in (int, float)
            and math.isfinite(retry_after_seconds) and 0 <= retry_after_seconds <= 86400 else None
        )
        self.retry_safe = retry_safe is True

    @property
    def diagnostics(self):
        return safe_request_failure(self)


def safe_request_failure(value):
    """Reconstruct only supported facts; classification is derived, not trusted."""
    if isinstance(value, RequestFailure):
        raw = {"code": value.code, "stage": value.stage, "http_status": value.http_status,
               "curl_code": value.curl_code, "retry_after_seconds": value.retry_after_seconds,
               "retryable": value.retry_safe}
    elif type(value) is dict:
        raw = value
    else:
        return {}
    try:
        item = RequestFailure(raw.get("code"), stage=raw.get("stage", "preflight"),
                              http_status=raw.get("http_status"), curl_code=raw.get("curl_code"),
                              retry_after_seconds=raw.get("retry_after_seconds"),
                              retry_safe=raw.get("retryable", True) is True)
    except (TypeError, ValueError, AttributeError):
        return {}
    category = "account" if item.code in ACCOUNT_FAILURE_CODES else (
        "provider" if item.code in {"request_transport_failed", "request_http_failed", "request_rate_limited", "request_proxy_unverified"} else "unknown"
    )
    certificate = item.curl_code in {51, 58, 60, 77, 82, 83, 90, 91, 98}
    authentication = item.http_status in {401, 403, 407}
    retryable = category == "provider" and item.retry_safe and item.code != "request_proxy_unverified" and not certificate and not authentication
    result = {"code": item.code, "stage": item.stage, "failure_category": category,
              "retryable": retryable, "rotate_route": retryable and item.code != "request_rate_limited" and item.http_status != 429}
    for name in ("http_status", "curl_code", "retry_after_seconds"):
        number = getattr(item, name)
        if number is not None:
            result[name] = number
    return result


LOGIN_FAILURE_CODES = frozenset({
    "login_timeout", "login_rejected", "browser_error", "session_capture_missing", "browser_cleanup_failed",
})
LOGIN_FAILURE_STAGES = frozenset({
    "browser_start", "context", "setup", "login_page", "login_options", "email", "password",
    "submit", "home", "session_capture", "browser_cleanup",
})
LOGIN_AUTHENTICATION_RESULTS = frozenset({"ok", "error", "fail", "failed", "success"})
_LOGIN_FAILURE_MESSAGES = {
    "login_timeout": "Sign-in did not finish. Check the account credentials or any verification required by Anghami, then retry.",
    "login_rejected": "Anghami rejected this sign-in. Check the account credentials and any verification required, then retry.",
    "browser_error": "The browser could not complete Anghami sign-in. Check that the site is accessible with the selected browser, then retry. The previous session was kept.",
    "session_capture_missing": "No authenticated session request was observed. The previous session was kept.",
    "browser_cleanup_failed": "The login browser could not close cleanly. Check its processes before retrying.",
}


def _http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


class LoginCaptureError(SessionError):
    """A fixed login failure with a stage and optional numeric HTTP evidence."""

    def __init__(self, code, *, stage, page_http_status=None, auth_http_status=None, authentication_result=None):
        if type(code) is not str or code not in LOGIN_FAILURE_CODES or type(stage) is not str or stage not in LOGIN_FAILURE_STAGES:
            raise ValueError("Use a supported login failure code and stage.")
        super().__init__(_LOGIN_FAILURE_MESSAGES[code])
        self.code = code
        self.stage = stage
        self.page_http_status = _http_status(page_http_status)
        self.auth_http_status = _http_status(auth_http_status)
        self.authentication_result = (
            authentication_result if type(authentication_result) is str
            and authentication_result in LOGIN_AUTHENTICATION_RESULTS else None
        )

    @property
    def diagnostics(self):
        return safe_login_failure(self)


def safe_login_failure(error):
    """Reconstruct known facts; arbitrary exception fields and text stay private."""
    if not isinstance(error, LoginCaptureError):
        return {}
    try:
        code, stage = error.code, error.stage
        if type(code) is not str or code not in LOGIN_FAILURE_CODES or type(stage) is not str or stage not in LOGIN_FAILURE_STAGES:
            return {}
        result = {"code": code, "stage": stage}
        for name in ("page_http_status", "auth_http_status"):
            value = _http_status(getattr(error, name, None))
            if value is not None:
                result[name] = value
        authentication_result = getattr(error, "authentication_result", None)
        if type(authentication_result) is str and authentication_result in LOGIN_AUTHENTICATION_RESULTS:
            result["authentication_result"] = authentication_result
        return result
    except Exception:
        return {}
