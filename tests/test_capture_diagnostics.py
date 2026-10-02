"""Stage-aware login diagnostics use synthetic browser responses only."""

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from anghami_session import capture
from anghami_session.errors import LoginCaptureError, SessionError, safe_login_failure


PRIVATE = "synthetic-password synthetic-session https://private.invalid/?sid=synthetic-secret"


class BrowserError(RuntimeError):
    pass


class BrowserTimeout(BrowserError):
    pass


def assert_safe(value):
    text = str(value)
    for secret in ("synthetic-password", "synthetic-session", "private.invalid", "synthetic-secret"):
        assert secret not in text


@pytest.fixture
def browser(monkeypatch):
    api = ModuleType("playwright.sync_api")
    api.Error, api.TimeoutError = BrowserError, BrowserTimeout
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    state = SimpleNamespace(
        fail_stage=None, fail_type=BrowserTimeout, close_error=None,
        page_status=200, auth_status=200, auth_body={"status": "ok"}, auth_json_error=None,
        emit_auth=True, emit_sessions=True, calls=[], callback=None,
    )

    def visit(stage):
        state.calls.append(stage)
        if stage == state.fail_stage:
            raise state.fail_type(PRIVATE)

    def auth_response():
        def response_json():
            if state.auth_json_error:
                raise state.auth_json_error
            return state.auth_body

        request = SimpleNamespace(
            url=capture.GATEWAY_URL + "?type=authenticate&password=synthetic-password",
            method="POST", headers={"cookie": "synthetic-session"},
        )
        state.callback(SimpleNamespace(request=request, status=state.auth_status, json=response_json))

    class Locator:
        def __init__(self, stage):
            self.stage = stage

        def wait_for(self, **options):
            visit(self.stage)

        def click(self):
            visit(self.stage)
            if self.stage == "submit" and state.emit_auth:
                auth_response()

        def fill(self, value):
            visit(self.stage)

    class Page:
        def goto(self, *args, **options):
            visit("login_page")
            return SimpleNamespace(status=state.page_status)

        def get_by_role(self, role, **options):
            name = options["name"]
            stage = {
                "Reject Optional": "setup", "Enter your email": "email",
                "Continue": "email", "Login": "submit",
            }[name]
            return Locator(stage)

        def get_by_text(self, *args, **options):
            return Locator("login_options")

        def get_by_placeholder(self, *args, **options):
            return Locator("password")

        def wait_for_url(self, *args, **options):
            visit("home")
            if state.emit_sessions:
                for operation in capture.OPERATIONS.values():
                    request = SimpleNamespace(
                        url=capture.GATEWAY_URL + "?type=" + operation,
                        method="GET", all_headers=lambda: {"cookie": "synthetic-session"},
                    )
                    state.callback(SimpleNamespace(request=request, status=200, json=lambda: {"status": "ok"}))

        def wait_for_timeout(self, milliseconds):
            visit("session_capture")

    class Context:
        def on(self, event, callback):
            state.callback = callback

        def new_page(self):
            visit("setup")
            return Page()

    class Browser:
        def new_context(self, **options):
            visit("context")
            return Context()

        def close(self):
            state.calls.append("browser_cleanup")
            if state.close_error:
                raise state.close_error

    def launch(**options):
        visit("browser_start")
        return Browser()

    monkeypatch.setattr(capture, "launch_browser", launch)
    return state


def login():
    return capture.capture_login(
        email="synthetic@example.invalid", password="synthetic-password",
        headless=True, browser_backend="chrome",
    )


@pytest.mark.parametrize("stage", [
    "browser_start", "context", "login_page", "login_options", "email", "password",
    "submit", "home", "session_capture",
])
def test_timeout_identifies_exact_failed_stage_and_closes_browser(browser, stage, capsys):
    browser.fail_stage = stage
    if stage == "session_capture":
        browser.emit_sessions = False
    with pytest.raises(LoginCaptureError, match="Sign-in did not finish") as caught:
        login()
    error = caught.value
    assert isinstance(error, SessionError)
    assert error.diagnostics["code"] == "login_timeout"
    assert error.diagnostics["stage"] == stage
    if stage == "browser_start":
        assert "browser_cleanup" not in browser.calls
    else:
        assert browser.calls[-1] == "browser_cleanup"
    assert error.__suppress_context__ is True
    assert_safe(error)
    assert_safe(json.dumps(error.diagnostics))
    assert_safe(capsys.readouterr())


@pytest.mark.parametrize("stage", ["browser_start", "context", "setup", "login_page", "home"])
def test_browser_error_has_safe_code_and_stage(browser, stage):
    browser.fail_stage, browser.fail_type = stage, BrowserError
    with pytest.raises(LoginCaptureError, match="could not complete Anghami sign-in") as caught:
        login()
    assert caught.value.diagnostics["code"] == "browser_error"
    assert caught.value.diagnostics["stage"] == stage
    assert_safe(caught.value)


@pytest.mark.parametrize("result", ["failed", "fail", "error"])
def test_authentication_rejection_with_http_200_is_distinct_from_home_timeout(browser, result):
    browser.fail_stage = "home"
    browser.auth_body = {"status": result, "error": PRIVATE, "password": "synthetic-password"}
    with pytest.raises(LoginCaptureError, match="Anghami rejected this sign-in") as caught:
        login()
    assert caught.value.diagnostics == {
        "code": "login_rejected", "stage": "home", "page_http_status": 200,
        "auth_http_status": 200, "authentication_result": result,
    }
    assert "wrong password" not in str(caught.value).lower()
    assert_safe(caught.value)
    assert_safe(caught.value.diagnostics)


@pytest.mark.parametrize("result", ["ok", "success"])
def test_auth_success_without_home_keeps_timeout_code(browser, result):
    browser.fail_stage = "home"
    browser.auth_body = {"status": result}
    with pytest.raises(LoginCaptureError) as caught:
        login()
    assert caught.value.diagnostics["code"] == "login_timeout"
    assert caught.value.diagnostics["authentication_result"] == result


@pytest.mark.parametrize("body", [
    {"status": PRIVATE}, {"status": "FAILED"}, {"status": 0}, {"status": True},
    {"status": ["failed"]}, {"status": {"error": PRIVATE}}, {}, [], None,
])
def test_unknown_or_unsafe_authentication_status_preserves_timeout_without_raw_body(browser, body):
    browser.fail_stage, browser.auth_body = "home", body
    with pytest.raises(LoginCaptureError) as caught:
        login()
    assert caught.value.diagnostics == {
        "code": "login_timeout", "stage": "home", "page_http_status": 200, "auth_http_status": 200,
    }
    assert_safe(caught.value.diagnostics)


def test_authentication_json_failure_is_not_exposed_or_inferred(browser):
    browser.fail_stage = "home"
    browser.auth_json_error = ValueError(PRIVATE)
    with pytest.raises(LoginCaptureError) as caught:
        login()
    assert caught.value.diagnostics["code"] == "login_timeout"
    assert "authentication_result" not in caught.value.diagnostics
    assert_safe(caught.value)


@pytest.mark.parametrize("status", [None, True, False, "200", 99, 600, 200.0, [], {}])
def test_numeric_http_evidence_is_strict_and_bounded(browser, status):
    browser.fail_stage = "home"
    browser.page_status = browser.auth_status = status
    with pytest.raises(LoginCaptureError) as caught:
        login()
    assert caught.value.diagnostics == {
        "code": "login_timeout", "stage": "home", "authentication_result": "ok",
    }


def test_missing_session_capture_has_own_stage_and_code(browser):
    browser.emit_sessions = False
    with pytest.raises(LoginCaptureError, match="No authenticated session request") as caught:
        login()
    assert caught.value.diagnostics["code"] == "session_capture_missing"
    assert caught.value.diagnostics["stage"] == "session_capture"
    assert browser.calls.count("session_capture") == 30
    assert browser.calls[-1] == "browser_cleanup"


def test_cleanup_failure_overrides_rejected_login_and_preserves_safe_http_evidence(browser):
    browser.fail_stage = "home"
    browser.auth_body = {"status": "failed", "error": PRIVATE}
    browser.close_error = RuntimeError(PRIVATE)
    with pytest.raises(LoginCaptureError, match="could not close cleanly") as caught:
        login()
    assert caught.value.diagnostics == {
        "code": "browser_cleanup_failed", "stage": "browser_cleanup", "page_http_status": 200,
        "auth_http_status": 200, "authentication_result": "failed",
    }
    assert browser.calls[-1] == "browser_cleanup"
    assert_safe(caught.value)


def test_success_keeps_return_contract_and_does_not_return_failure_diagnostics(browser):
    saved, metadata = login()
    assert set(saved["requests"]) == set(capture.OPERATIONS)
    assert saved["account_email"] == "synthetic@example.invalid"
    assert len(metadata) == 1 and metadata[0]["http_status"] == 200
    assert metadata[0]["body"] == "Omitted: contains protected authentication data."
    assert "login_failure" not in saved and "authentication_result" not in metadata[0]
    assert browser.calls[-1] == "browser_cleanup"


@pytest.mark.parametrize("code,stage", [(PRIVATE, "home"), ("login_timeout", PRIVATE), ([], "home"), ("login_timeout", {})])
def test_diagnostic_constructor_rejects_unknown_enums_without_echoing_them(code, stage):
    with pytest.raises(ValueError) as caught:
        LoginCaptureError(code, stage=stage)
    assert_safe(caught.value)


def test_safe_diagnostics_reconstruct_fields_and_ignore_overridden_property_and_private_args():
    class UntrustedError(LoginCaptureError):
        @property
        def diagnostics(self):
            return {"url": PRIVATE, "password": PRIVATE}

    error = UntrustedError("login_timeout", stage="home", page_http_status=200, auth_http_status=401)
    error.args = (PRIVATE,)
    error.authentication_result = PRIVATE
    error.page_http_status = True
    error.headers = {"cookie": PRIVATE}
    assert safe_login_failure(error) == {"code": "login_timeout", "stage": "home", "auth_http_status": 401}
    error.stage = PRIVATE
    assert safe_login_failure(error) == {}
    assert safe_login_failure(SessionError(PRIVATE)) == {}


@pytest.mark.parametrize("result", [PRIVATE, True, 0, {}, [], None])
def test_constructor_drops_unknown_authentication_result(result):
    error = LoginCaptureError("login_timeout", stage="home", authentication_result=result)
    assert error.diagnostics == {"code": "login_timeout", "stage": "home"}
