from requests.exceptions import HTTPError

import importlib.util
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("send_vote", ROOT / "send_vote.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DummyCookies(dict):
    def set(self, key, value):
        self[key] = value

    def get(self, key, default=None):
        return super().get(key, default)


class DummySession:
    def __init__(self):
        self.cookies = DummyCookies()
        self.cookies["fingerprint"] = "stored-fingerprint"
        self.cookies["xxlfingerprint"] = "stored-uuid"

    def get(self, *args, **kwargs):
        raise HTTPError("forbidden")


def test_initialize_browser_fingerprint_falls_back_to_saved_cookie_values():
    session = DummySession()

    fingerprint, fingerprint_id = module.initialize_browser_fingerprint(
        session,
        "session-uuid",
        fallback_cookies={"fingerprint": "cookie-fingerprint", "xxlfingerprint": "cookie-uuid"},
    )

    assert fingerprint == "cookie-fingerprint"
    assert fingerprint_id == "cookie-uuid"


def test_initialize_browser_fingerprint_uses_existing_session_cookies_when_no_fallback_is_provided():
    session = DummySession()

    fingerprint, fingerprint_id = module.initialize_browser_fingerprint(
        session,
        "session-uuid",
        fallback_cookies=None,
    )

    assert fingerprint == "stored-fingerprint"
    assert fingerprint_id == "stored-uuid"


def test_assert_response_ok_reports_http_details():
    class DummyResponse:
        ok = False
        status_code = 403
        reason = "Forbidden"
        text = "blocked by bot protection"

    with pytest.raises(AssertionError) as exc_info:
        module.assert_response_ok(DummyResponse(), "vote")

    message = str(exc_info.value)
    assert "403 Forbidden" in message
    assert "blocked by bot protection" in message
