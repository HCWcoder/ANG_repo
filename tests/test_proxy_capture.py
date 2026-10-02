"""Proxy login capture is exercised with offline browser and transport doubles."""

from types import ModuleType, SimpleNamespace
import sys

import pytest

from anghami_session import capture, proxy as proxy_module
from anghami_session.errors import SessionError

EMAIL = "synthetic-login@example.invalid"
PASSWORD = "synthetic-private-login-password"
PROXY_USER = "synthetic-proxy-user"
PROXY_KEY = "synthetic-proxy-key"
PRIVATE_ERROR = f"https://private.invalid/?sid=private-session {PASSWORD} {PROXY_KEY}"


class FakeBrowserError(RuntimeError):
    pass


class FakeTimeout(FakeBrowserError):
    pass


def assert_redacted(value):
    text = str(value)
    for private in (PASSWORD, PROXY_USER, PROXY_KEY, PRIVATE_ERROR, "private-session"):
        assert private not in text


@pytest.fixture
def offline_browser(monkeypatch):
    api = ModuleType("playwright.sync_api")
    api.Error = FakeBrowserError
    api.TimeoutError = FakeTimeout
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    calls = []
    controls = {
        "status": 200, "data": {"country": "EG", "ip": "192.0.2.7"},
        "geo_error": None, "json_error": None, "probe_close_error": None,
        "context_error": None, "browser_close_error": None, "login_error": None,
    }
    callback = None

    class GeoPage:
        def goto(self, url, **options):
            calls.append(("geo.goto", url, options))
            if controls["geo_error"]:
                raise controls["geo_error"]
            if controls["status"] is None:
                return None

            def response_json():
                if controls["json_error"]:
                    raise controls["json_error"]
                return controls["data"]

            return SimpleNamespace(status=controls["status"], json=response_json)

        def close(self):
            calls.append(("geo.close",))
            if controls["probe_close_error"]:
                raise controls["probe_close_error"]

    class Locator:
        def wait_for(self, **options):
            pass

        def click(self):
            calls.append(("login.click",))

        def fill(self, value):
            calls.append(("login.fill", value))

    class LoginPage:
        def goto(self, url, **options):
            calls.append(("login.goto", url, options))
            if controls["login_error"]:
                raise controls["login_error"]

        def get_by_role(self, *args, **options):
            return Locator()

        def get_by_text(self, *args, **options):
            return Locator()

        def get_by_placeholder(self, *args, **options):
            return Locator()

        def wait_for_url(self, *args, **options):
            # Geo-domain cookies must never enter a saved Anghami template.
            response = SimpleNamespace(
                request=SimpleNamespace(url=proxy_module.GEOLOCATION_URL, method="GET"),
                status=200,
            )
            callback(response)
            for operation in capture.OPERATIONS.values():
                headers = {"cookie": "appsidsave=synthetic-saved-cookie", "proxy-authorization": PROXY_KEY}
                request = SimpleNamespace(
                    url=capture.GATEWAY_URL + "?type=" + operation,
                    method="GET", all_headers=lambda: headers,
                )
                callback(SimpleNamespace(request=request, status=200, json=lambda: {"status": "ok"}))

        def wait_for_timeout(self, *args):
            pytest.fail("All supported session templates were already supplied")

    class Context:
        def on(self, event, function):
            nonlocal callback
            calls.append(("context.on", event))
            callback = function

        def new_page(self):
            calls.append(("context.new_page",))
            # The probe is created before the response capture is registered.
            return GeoPage() if callback is None else LoginPage()

    class Browser:
        def new_context(self, **options):
            calls.append(("browser.new_context", options))
            if controls["context_error"]:
                raise controls["context_error"]
            return Context()

        def close(self):
            calls.append(("browser.close",))
            if controls["browser_close_error"]:
                raise controls["browser_close_error"]

    def launch(**options):
        calls.append(("browser.launch", options))
        return Browser()

    monkeypatch.setattr(capture, "launch_browser", launch)
    monkeypatch.setattr(proxy_module, "token_hex", lambda count: "0123456789abcdef")

    def verify(self):
        calls.append(("proxy.verify_country",))
        return {"country": "EG", "country_verified": True, "proxy_used": True}

    monkeypatch.setattr(proxy_module.PacketStreamProxy, "verify_country", verify)
    config = proxy_module.PacketStreamProxy(PROXY_USER, PROXY_KEY)
    return SimpleNamespace(calls=calls, controls=controls, proxy=config)


def run_capture(state, **options):
    return capture.capture_login(
        email=EMAIL, password=PASSWORD, headless=True,
        browser_backend="chrome", proxy=state.proxy, **options,
    )


def test_browser_credentials_bind_same_sticky_https_route(offline_browser):
    config = offline_browser.proxy
    first = config.browser_options()
    assert first == config.browser_options()
    transport = config.transport_options()
    assert first == {
        "server": transport["proxy"], "username": transport["proxy_auth"][0],
        "password": transport["proxy_auth"][1],
    }
    assert first["server"] == "https://proxy.packetstream.io:31111"
    assert PROXY_USER not in first["server"] and PROXY_KEY not in first["server"]
    first["password"] = "changed-local-copy"
    assert config.browser_options()["password"] == transport["proxy_auth"][1]
    assert_redacted(config.summary())
    assert_redacted(repr(config))


def test_proxy_capture_checks_both_routes_before_credentials(offline_browser, capsys):
    state = offline_browser
    saved, metadata = run_capture(state)
    calls = state.calls
    assert calls[0] == ("proxy.verify_country",)
    assert calls[1] == ("browser.launch", {"headless": True, "backend": "chrome"})
    assert calls[2] == ("browser.new_context", {"no_viewport": True, "proxy": state.proxy.browser_options()})
    labels = [call[0] for call in calls]
    assert labels.count("browser.new_context") == 1
    assert labels.count("context.new_page") == 2
    assert labels.index("geo.goto") < labels.index("geo.close") < labels.index("login.goto") < labels.index("login.fill")
    assert next(call for call in calls if call[0] == "geo.goto") == (
        "geo.goto", proxy_module.GEOLOCATION_URL,
        {"wait_until": "domcontentloaded", "timeout": 25000},
    )
    assert calls[-1] == ("browser.close",)
    assert saved["account_email"] == EMAIL
    assert set(saved["requests"]) == set(capture.OPERATIONS)
    assert metadata == []
    assert "ipinfo" not in str(saved) and "192.0.2.7" not in str(saved)
    assert PROXY_KEY not in str(saved) and "proxy-authorization" not in str(saved)
    output = capsys.readouterr()
    assert output.out == ""
    assert "installed Chrome" in output.err
    assert_redacted(output.err)


def test_http_country_failure_blocks_launch_and_prompts(offline_browser, monkeypatch):
    def fail(self):
        raise SessionError(PRIVATE_ERROR)

    monkeypatch.setattr(proxy_module.PacketStreamProxy, "verify_country", fail)
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("Proxy failure must precede email prompt"))
    monkeypatch.setattr(capture.getpass, "getpass", lambda *_: pytest.fail("Proxy failure must precede password prompt"))
    with pytest.raises(SessionError, match="No browser was opened") as error:
        capture.capture_login(proxy=offline_browser.proxy)
    assert offline_browser.calls == []
    assert_redacted(error.value)


@pytest.mark.parametrize("status", [None, 0, 407, 302, 500, "200", True])
def test_invalid_geo_http_status_prevents_login(offline_browser, status, capsys):
    state = offline_browser
    state.controls["status"] = status
    with pytest.raises(SessionError, match="did not verify Egypt") as error:
        run_capture(state)
    labels = [call[0] for call in state.calls]
    assert "login.goto" not in labels and "login.fill" not in labels
    assert labels.count("context.new_page") == 1
    assert labels[-2:] == ["geo.close", "browser.close"]
    assert_redacted(error.value)
    assert_redacted(capsys.readouterr())


@pytest.mark.parametrize("data", [
    None, [], {}, {"country": "US"}, {"country": "eg"},
    {"country": "EG", "error": PRIVATE_ERROR},
])
def test_invalid_geo_body_prevents_login(offline_browser, data):
    state = offline_browser
    state.controls["data"] = data
    with pytest.raises(SessionError, match="did not verify Egypt") as error:
        run_capture(state)
    labels = [call[0] for call in state.calls]
    assert "login.fill" not in labels and "login.goto" not in labels
    assert labels[-2:] == ["geo.close", "browser.close"]
    assert_redacted(error.value)


@pytest.mark.parametrize("control,error_type", [
    ("geo_error", FakeBrowserError), ("geo_error", FakeTimeout),
    ("json_error", ValueError), ("probe_close_error", RuntimeError),
])
def test_geo_failures_redact_secrets_and_close_browser(offline_browser, control, error_type):
    state = offline_browser
    state.controls[control] = error_type(PRIVATE_ERROR)
    with pytest.raises(SessionError) as error:
        run_capture(state)
    labels = [call[0] for call in state.calls]
    assert "login.fill" not in labels and "login.goto" not in labels
    assert labels[-2:] == ["geo.close", "browser.close"]
    assert_redacted(error.value)


def test_context_creation_failure_closes_browser(offline_browser):
    state = offline_browser
    state.controls["context_error"] = FakeBrowserError(PRIVATE_ERROR)
    with pytest.raises(SessionError, match="could not complete Anghami sign-in") as error:
        run_capture(state)
    assert state.calls[-1] == ("browser.close",)
    assert not any(call[0] == "context.new_page" for call in state.calls)
    assert_redacted(error.value)


def test_login_error_after_proxy_verification_still_closes(offline_browser):
    state = offline_browser
    state.controls["login_error"] = FakeTimeout(PRIVATE_ERROR)
    with pytest.raises(SessionError, match="Sign-in did not finish") as error:
        run_capture(state)
    labels = [call[0] for call in state.calls]
    assert labels.index("geo.close") < labels.index("login.goto")
    assert labels[-1] == "browser.close"
    assert "login.fill" not in labels
    assert_redacted(error.value)


def test_browser_cleanup_errors_are_sanitized(offline_browser):
    offline_browser.controls["browser_close_error"] = RuntimeError(PRIVATE_ERROR)
    with pytest.raises(SessionError, match="could not close cleanly") as error:
        run_capture(offline_browser)
    assert_redacted(error.value)


def test_unproxied_default_uses_original_context_and_one_page(offline_browser, capsys):
    saved, _ = capture.capture_login(email=EMAIL, password=PASSWORD)
    calls = offline_browser.calls
    assert calls[0] == ("browser.launch", {"headless": False, "backend": "cloakbrowser"})
    assert calls[1] == ("browser.new_context", {"no_viewport": True})
    labels = [call[0] for call in calls]
    assert "proxy.verify_country" not in labels and "geo.goto" not in labels
    assert labels.count("context.new_page") == 1
    assert saved["account_email"] == EMAIL
    output = capsys.readouterr()
    assert output.out == ""
    assert "Close other CloakBrowser" in output.err
    assert_redacted(output.err)


def test_interactive_email_prompt_stays_off_stdout(offline_browser, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda: EMAIL)
    capture.capture_login(password=PASSWORD, browser_backend="chrome")
    output = capsys.readouterr()
    assert output.out == ""
    assert "Anghami email: " in output.err
    assert EMAIL not in output.err
