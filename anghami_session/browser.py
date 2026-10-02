"""Explicit browser backends for normal login and player checks."""

from threading import Lock

from .errors import SessionError

_CLOAK_LAUNCH_LOCK = Lock()


def launch_browser(*, headless: bool, backend: str = "cloakbrowser", enable_audio: bool = False,
                   direct: bool = False):
    """Launch an isolated browser without downloading or using a personal profile.

    Imports stay local so ordinary saved-session requests need no browser runtime.
    ``enable_audio`` removes Playwright's mute flag while launching either backend.
    ``direct`` bypasses system proxy/PAC settings for an explicitly local job.
    """
    if backend not in {"cloakbrowser", "chrome"}:
        raise SessionError("Select a supported browser: cloakbrowser or chrome.")
    if type(direct) is not bool:
        raise SessionError("Select whether this browser uses a direct connection.")
    if backend == "cloakbrowser":
        try:
            from cloakbrowser import launch
            from cloakbrowser.license import CloakBrowserLicenseError
            import cloakbrowser.browser as cloak_browser
        except ImportError:
            raise SessionError("Install requirements-login.txt before launching a login browser.") from None
        try:
            # The wrapper supplies ignore_default_args itself, so passing that
            # keyword would collide. Restore its exact prior list after launch.
            with _CLOAK_LAUNCH_LOCK:
                original_ignored = cloak_browser.IGNORE_DEFAULT_ARGS
                try:
                    if enable_audio:
                        cloak_browser.IGNORE_DEFAULT_ARGS = list(dict.fromkeys(
                            [*original_ignored, "--mute-audio"]
                        ))
                    return launch(
                        headless=headless, chromium_sandbox=True, stealth_args=False,
                        args=["--fingerprint=71432", "--fingerprint-platform=windows"]
                            + (["--no-proxy-server"] if direct else []),
                    )
                finally:
                    cloak_browser.IGNORE_DEFAULT_ARGS = original_ignored
        except CloakBrowserLicenseError:
            raise SessionError(
                "CloakBrowser could not start because its license check failed. "
                "Check the key and available session seat, or select --browser chrome."
            ) from None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SessionError("Install requirements-login.txt before launching a login browser.") from None
    playwright = sync_playwright().start()
    options = {"channel": "chrome", "headless": headless, "chromium_sandbox": True}
    if direct:
        options["args"] = ["--no-proxy-server"]
    if enable_audio:
        options["ignore_default_args"] = ["--mute-audio"]
    try:
        browser = playwright.chromium.launch(**options)
    except BaseException:
        try:
            playwright.stop()
        except Exception:
            pass
        raise

    original_close = browser.close

    def close_with_cleanup(*args, **kwargs):
        try:
            return original_close(*args, **kwargs)
        finally:
            playwright.stop()

    browser.close = close_with_cleanup
    return browser
