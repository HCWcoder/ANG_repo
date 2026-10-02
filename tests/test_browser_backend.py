"""Browser lifecycle and selection checks; no browser process or network is used."""

from types import ModuleType, SimpleNamespace
import sys

import pytest

from anghami_session import browser, capture
from anghami_session.errors import SessionError


def fake_playwright(monkeypatch, *, launch_error=None, close_error=None):
    calls = {"launch": [], "close": [], "stop": 0}

    class FakeBrowser:
        def close(self, *args, **kwargs):
            calls["close"].append((args, kwargs))
            if close_error:
                raise close_error

    result = FakeBrowser()

    def launch(**kwargs):
        calls["launch"].append(kwargs)
        if launch_error:
            raise launch_error
        return result

    def stop():
        calls["stop"] += 1

    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=stop)
    api = ModuleType("playwright.sync_api")
    api.sync_playwright = lambda: SimpleNamespace(start=lambda: runtime)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    return calls, result


@pytest.mark.parametrize("enable_audio", [False, True])
def test_chrome_uses_installed_channel_and_stops_runtime_on_close(monkeypatch, enable_audio):
    calls, expected = fake_playwright(monkeypatch)
    result = browser.launch_browser(headless=True, backend="chrome", enable_audio=enable_audio)
    assert result is expected
    options = {"channel": "chrome", "headless": True, "chromium_sandbox": True}
    if enable_audio:
        options["ignore_default_args"] = ["--mute-audio"]
    assert calls["launch"] == [options]
    result.close(reason="finished")
    assert calls["close"] == [((), {"reason": "finished"})]
    assert calls["stop"] == 1


def test_chrome_launch_failure_stops_runtime_before_propagating(monkeypatch):
    failure = RuntimeError("simulated launch failure")
    calls, _ = fake_playwright(monkeypatch, launch_error=failure)
    with pytest.raises(RuntimeError) as error:
        browser.launch_browser(headless=False, backend="chrome")
    assert error.value is failure
    assert calls["stop"] == 1
    assert calls["close"] == []


def test_chrome_close_failure_still_stops_runtime(monkeypatch):
    calls, _ = fake_playwright(monkeypatch, close_error=RuntimeError("simulated close failure"))
    result = browser.launch_browser(headless=True, backend="chrome")
    with pytest.raises(RuntimeError):
        result.close()
    assert calls["stop"] == 1


def fake_cloak(monkeypatch, *, license_failure=False):
    package = ModuleType("cloakbrowser")
    package.__path__ = []
    implementation = ModuleType("cloakbrowser.browser")
    original_ignored = ["--enable-automation", "--enable-unsafe-swiftshader"]
    implementation.IGNORE_DEFAULT_ARGS = original_ignored
    license_module = ModuleType("cloakbrowser.license")

    class LicenseFailure(RuntimeError):
        pass

    license_module.CloakBrowserLicenseError = LicenseFailure
    calls = []
    result = object()

    def launch(**kwargs):
        calls.append((kwargs, list(implementation.IGNORE_DEFAULT_ARGS)))
        if license_failure:
            raise LicenseFailure("private-license-value-in-a-fake-error")
        return result

    package.launch = launch
    package.browser = implementation
    package.license = license_module
    monkeypatch.setitem(sys.modules, "cloakbrowser", package)
    monkeypatch.setitem(sys.modules, "cloakbrowser.browser", implementation)
    monkeypatch.setitem(sys.modules, "cloakbrowser.license", license_module)
    return calls, implementation, original_ignored, result


@pytest.mark.parametrize("enable_audio", [False, True])
def test_cloak_keeps_launch_options_and_restores_ignored_args(monkeypatch, enable_audio):
    calls, implementation, original_ignored, expected = fake_cloak(monkeypatch)
    assert browser.launch_browser(headless=True, enable_audio=enable_audio) is expected
    expected_ignored = original_ignored + (["--mute-audio"] if enable_audio else [])
    assert calls == [({
        "headless": True, "chromium_sandbox": True, "stealth_args": False,
        "args": ["--fingerprint=71432", "--fingerprint-platform=windows"],
    }, expected_ignored)]
    assert implementation.IGNORE_DEFAULT_ARGS is original_ignored


def test_cloak_denial_is_safe_and_restores_temporary_audio_options(monkeypatch):
    _, implementation, original_ignored, _ = fake_cloak(monkeypatch, license_failure=True)
    with pytest.raises(SessionError, match="license check failed") as error:
        browser.launch_browser(headless=True, enable_audio=True)
    assert "private-license-value" not in str(error.value)
    assert implementation.IGNORE_DEFAULT_ARGS is original_ignored


def test_invalid_backend_does_not_start_any_runtime():
    with pytest.raises(SessionError, match="supported browser"):
        browser.launch_browser(headless=True, backend="other")


@pytest.mark.parametrize("backend", ["chrome", "cloakbrowser"])
@pytest.mark.parametrize("enable_audio", [False, True])
def test_explicit_direct_browser_bypasses_system_proxy(monkeypatch, backend, enable_audio):
    if backend == "chrome":
        calls, expected = fake_playwright(monkeypatch)
    else:
        calls, implementation, original_ignored, expected = fake_cloak(monkeypatch)
    result = browser.launch_browser(headless=True, backend=backend,
        enable_audio=enable_audio, direct=True)
    assert result is expected
    options = calls["launch"][0] if backend == "chrome" else calls[0][0]
    assert options["args"].count("--no-proxy-server") == 1
    assert options["chromium_sandbox"] is True
    if backend == "cloakbrowser":
        assert options["stealth_args"] is False
        assert implementation.IGNORE_DEFAULT_ARGS is original_ignored
    else:
        result.close()
        assert calls["stop"] == 1


@pytest.mark.parametrize("direct", [None, 0, 1, "true", [], {}])
def test_invalid_direct_choice_does_not_start_a_browser(direct):
    with pytest.raises(SessionError, match="direct connection"):
        browser.launch_browser(headless=True, direct=direct)


def test_capture_forwards_selected_backend_and_closes_on_timeout(monkeypatch, capsys):
    class FakeBrowserError(RuntimeError):
        pass

    class FakeTimeout(FakeBrowserError):
        pass

    api = ModuleType("playwright.sync_api")
    api.Error = FakeBrowserError
    api.TimeoutError = FakeTimeout
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    calls = []

    def goto(*args, **kwargs):
        raise FakeTimeout("fake timeout with private-password")

    page = SimpleNamespace(goto=goto)
    context = SimpleNamespace(on=lambda *args: None, new_page=lambda: page)
    launched = SimpleNamespace(new_context=lambda **kwargs: context, close=lambda: calls.append("closed"))

    def launch_browser(**kwargs):
        calls.append(kwargs)
        return launched

    monkeypatch.setattr(capture, "launch_browser", launch_browser)
    with pytest.raises(SessionError, match="Sign-in did not finish") as error:
        capture.capture_login(
            email="test@example.com", password="private-password",
            headless=True, browser_backend="chrome",
        )
    assert calls == [{"headless": True, "backend": "chrome"}, "closed"]
    assert "private-password" not in str(error.value)
    assert "CloakBrowser" not in capsys.readouterr().out


def test_capture_redacts_general_browser_errors_and_closes(monkeypatch):
    class FakeBrowserError(RuntimeError):
        pass

    class FakeTimeout(FakeBrowserError):
        pass

    api = ModuleType("playwright.sync_api")
    api.Error = FakeBrowserError
    api.TimeoutError = FakeTimeout
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    closed = []

    def goto(*args, **kwargs):
        raise FakeBrowserError("raw browser log with private-password and private-session")

    context = SimpleNamespace(on=lambda *args: None, new_page=lambda: SimpleNamespace(goto=goto))
    launched = SimpleNamespace(new_context=lambda **kwargs: context, close=lambda: closed.append(True))
    monkeypatch.setattr(capture, "launch_browser", lambda **kwargs: launched)
    with pytest.raises(SessionError, match="could not complete Anghami sign-in") as error:
        capture.capture_login(email="test@example.com", password="private-password", browser_backend="chrome")
    assert closed == [True]
    assert "private-password" not in str(error.value)
    assert "private-session" not in str(error.value)
