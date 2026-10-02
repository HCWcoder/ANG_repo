class SessionError(RuntimeError):
    """A session error whose message is safe to display without exposing tokens."""


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
