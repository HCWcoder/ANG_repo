"""Log in once and capture only the session requests needed by the HTTP client."""

import getpass
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from .browser import launch_browser
from .client import GATEWAY_URL, IGNORED_HEADERS, OPERATIONS
from .errors import LOGIN_AUTHENTICATION_RESULTS, LoginCaptureError, RequestFailure, SessionError
from .proxy import GEOLOCATION_URL, ProxyCountryError, wait_country_lookup_start
from .provider_recovery import observe_provider_failure, retry_after_seconds


def _verify_browser_country(context) -> None:
    """Verify the new browser context before it visits any account site."""
    page = None
    try:
        page = context.new_page()
        if not wait_country_lookup_start():
            raise ProxyCountryError("http_failure", http_status=429, retry_after_seconds=121, retry_safe=False)
        response = page.goto(GEOLOCATION_URL, wait_until="domcontentloaded", timeout=25000)
        if response is None or type(response.status) is not int:
            raise ProxyCountryError("transport_error")
        if response.status != 200:
            failure = ProxyCountryError("authentication_rejected" if response.status == 407 else "http_failure", http_status=response.status,
                                        retry_after_seconds=retry_after_seconds(getattr(response, "headers", None)))
            observe_provider_failure(failure)
            raise failure
        data = response.json()
        if not isinstance(data, dict) or data.get("country") != "EG" or data.get("error"):
            raise ProxyCountryError("country_unverified")
    except ProxyCountryError:
        raise
    except Exception:
        raise ProxyCountryError("transport_error") from None
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                raise SessionError(
                    "The browser proxy country-check page could not close. No Anghami sign-in was attempted."
                ) from None


def capture_login(*, email: str | None = None, password: str | None = None,
                  headless: bool = False, browser_backend: str = "cloakbrowser",
                  proxy=None, reduce_browser_data: bool = False) -> tuple[dict, list[dict]]:
    if type(reduce_browser_data) is not bool:
        raise SessionError("Select whether to reduce browser data.")
    # Import only on explicit login; normal checks and requests need no browser package.
    try:
        from playwright.sync_api import Error as BrowserError
        from playwright.sync_api import TimeoutError as BrowserTimeout
    except ImportError:
        raise SessionError("Install requirements-login.txt before capturing a login.") from None

    if browser_backend == "cloakbrowser":
        print("Close other CloakBrowser windows before login (the free key allows one browser session).", file=sys.stderr)
    elif browser_backend == "chrome":
        print("Signing in with installed Chrome in a temporary browser profile.", file=sys.stderr)
    browser_proxy_options = None
    if proxy is not None:
        try:
            proxy.verify_country()
            browser_proxy_options = proxy.browser_options()
        except ProxyCountryError:
            raise
        except Exception:
            raise SessionError("The login proxy country check failed. No browser was opened.") from None
    if email is None:
        print("Anghami email: ", end="", file=sys.stderr, flush=True)
        email = input()
    email = email.strip()
    password = getpass.getpass("Anghami password (not saved): ") if password is None else password
    if not email or not password:
        raise SessionError("Email and password are required.")
    captured = {}
    login_metadata = []
    operation_names = {value: key for key, value in OPERATIONS.items()}
    browser = None
    stage = "browser_start"
    page_http_status = None
    auth_http_status = None
    authentication_result = None
    auth_retry_after = None
    page_retry_after = None
    submitted = False

    def provider_error():
        status = next((value for value in (auth_http_status, page_http_status)
                       if value == 429 or type(value) is int and 500 <= value <= 599), None)
        if status is None:
            return None
        return RequestFailure("request_rate_limited" if status == 429 else "request_http_failed",
                              stage="preflight", http_status=status,
                              retry_after_seconds=auth_retry_after if status == auth_http_status else page_retry_after,
                              retry_safe=not submitted)
    try:
        browser = launch_browser(headless=headless, backend=browser_backend)
        stage = "context"
        context_options = {"no_viewport": True}
        if proxy is not None:
            context_options["proxy"] = browser_proxy_options
        context = browser.new_context(**context_options)
        stage = "setup"
        if reduce_browser_data:
            from .browser_data import install_browser_data_reduction
            install_browser_data_reduction(context, proxy=proxy)
        if proxy is not None:
            _verify_browser_country(context)

        def record(response):
            nonlocal auth_http_status, authentication_result, auth_retry_after
            request = response.request
            parts = urlsplit(request.url)
            if f"{parts.scheme}://{parts.netloc}{parts.path}" != GATEWAY_URL:
                return
            query = parse_qs(parts.query)
            operation = query.get("type", [""])[0]
            if operation == "authenticate":
                observed_status = response.status
                auth_http_status = observed_status if type(observed_status) is int and 100 <= observed_status <= 599 else None
                auth_retry_after = retry_after_seconds(getattr(response, "headers", None))
                provider = provider_error()
                if provider is not None:
                    observe_provider_failure(provider)
                authentication_result = None
                try:
                    auth_body = response.json()
                    result = auth_body.get("status") if isinstance(auth_body, dict) else None
                    if type(result) is str and result in LOGIN_AUTHENTICATION_RESULTS:
                        authentication_result = result
                except Exception:
                    pass
                login_metadata.append({
                    "method": request.method,
                    "endpoint": GATEWAY_URL + "?type=authenticate",
                    "query_parameter_names": sorted(query),
                    "request_header_names": sorted(request.headers),
                    "http_status": response.status,
                    "body": "Omitted: contains protected authentication data.",
                })
            if operation not in operation_names or request.method != "GET" or response.status != 200:
                return
            try:
                headers = request.all_headers()
                data = response.json()
                # Ignore the service worker's duplicate, which omits network cookies.
                if "cookie" not in headers or not isinstance(data, dict) or data.get("status") != "ok":
                    return
                captured[operation_names[operation]] = {
                    "method": "GET", "url": request.url,
                    "headers": {
                        k: v for k, v in headers.items()
                        if not k.startswith(":") and k.lower() not in IGNORED_HEADERS
                    },
                }
            except Exception:
                # Transient/cancelled browser requests are not valid session templates.
                return

        context.on("response", record)
        page = context.new_page()
        stage = "login_page"
        page_response = page.goto("https://play.anghami.com/login", wait_until="domcontentloaded")
        observed_status = getattr(page_response, "status", None)
        if type(observed_status) is int and 100 <= observed_status <= 599:
            page_http_status = observed_status
            page_retry_after = retry_after_seconds(getattr(page_response, "headers", None))
        error = provider_error()
        if error is not None:
            observe_provider_failure(error)
            raise error
        stage = "setup"
        optional = page.get_by_role("button", name="Reject Optional", exact=True)
        try:
            optional.wait_for(state="visible", timeout=5000)
            optional.click()
        except BrowserTimeout:
            pass
        stage = "login_options"
        page.get_by_text("Other login options", exact=True).click()
        stage = "email"
        page.get_by_role("textbox", name="Enter your email").fill(email)
        page.get_by_role("button", name="Continue", exact=True).click()
        stage = "password"
        page.get_by_placeholder("Enter your password", exact=True).fill(password)
        stage = "submit"
        submitted = True
        page.get_by_role("button", name="Login", exact=True).click()
        password = None
        stage = "home"
        home_options = {"timeout": 45000}
        if reduce_browser_data:
            home_options["wait_until"] = "domcontentloaded"
        page.wait_for_url("**/home", **home_options)
        error = provider_error()
        if error is not None:
            raise error
        stage = "session_capture"
        for _ in range(30):
            if all(name in captured for name in OPERATIONS):
                break
            page.wait_for_timeout(500)
        if "relations" not in captured:
            raise LoginCaptureError("session_capture_missing", stage=stage, page_http_status=page_http_status, auth_http_status=auth_http_status, authentication_result=authentication_result)
        return {
            "format_version": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "origin": "https://play.anghami.com",
            "account_email": email.casefold(),
            "requests": captured,
        }, login_metadata
    except BrowserTimeout:
        error = provider_error()
        if error is not None:
            raise error from None
        code = "login_rejected" if stage == "home" and auth_http_status == 200 and authentication_result in {"failed", "fail", "error"} else "login_timeout"
        raise LoginCaptureError(code, stage=stage, page_http_status=page_http_status, auth_http_status=auth_http_status, authentication_result=authentication_result) from None
    except BrowserError:
        error = provider_error()
        if error is not None:
            raise error from None
        raise LoginCaptureError("browser_error", stage=stage, page_http_status=page_http_status, auth_http_status=auth_http_status, authentication_result=authentication_result) from None
    except LoginCaptureError:
        error = provider_error()
        if error is not None:
            raise error from None
        raise
    finally:
        password = None
        if browser is not None:
            try:
                browser.close()
            except Exception:
                raise LoginCaptureError("browser_cleanup_failed", stage="browser_cleanup", page_http_status=page_http_status, auth_http_status=auth_http_status, authentication_result=authentication_result) from None
