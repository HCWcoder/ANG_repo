"""Recover a selected account's valid legacy session using normal HTTP reads.

This does not authenticate a password or acquire a new device fingerprint. A
rejected existing session stops the operation; the caller owns any persistence.
"""

from copy import deepcopy
from datetime import datetime, timezone
import re
import time
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

from curl_cffi import CurlOpt, requests
from curl_cffi.curl import CurlError

from .bandwidth import bandwidth_transport_options, measured_request
from .client import AnghamiSession, GATEWAY_URL, OPERATIONS, validate_session
from .errors import RequestFailure, SessionError, safe_request_failure
from .provider_recovery import observe_provider_failure, provider_failure, retry_after_seconds, wait_before_provider_request, wait_for_provider
from .media_gateway import PlaybackGateway
from .proxy import ProxyCountryError

_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_SERVER_COOKIE_NAMES = frozenset({"appsidsave", "oats", "ssss"})
_COOKIE_DOMAINS = frozenset({"anghami.com", ".anghami.com", "coussa.anghami.com", ".coussa.anghami.com"})
_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36"
_INVALID = "The selected account has no compatible existing legacy session. A normal browser login is required."
_FAILED = "The existing session could not be recovered. No browser login or automatic retry was attempted."
MAX_VALIDATION_READ_ATTEMPTS = 3
_VALIDATION_READ_STAGES = frozenset({"relations", "playlists", "negative_control", "session_recovery_validation"})
_TRANSIENT_VALIDATION_CURL_CODES = frozenset({5, 6, 7, 18, 28, 35, 52, 55, 56, 81, 92, 95, 96})
_TRANSIENT_VALIDATION_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def read_only_validation_failure(value, *, allow_cooldown_refusal=False):
    """Recognize transient validation reads without enabling renewal replays."""
    failure = provider_failure(value)
    country_read = isinstance(value, ProxyCountryError)
    if country_read and failure.get("failure_kind") not in {"transport_error", "http_failure"}:
        return False
    if ((failure.get("stage") not in _VALIDATION_READ_STAGES and not country_read)
            or failure.get("http_status") in {401, 403, 407}
            or failure.get("proxy_connect_http_status") in {401, 403, 407}
            or failure.get("curl_code") in {51, 58, 60, 77, 82, 83, 90, 91, 98}):
        return False
    rate_limited = failure.get("http_status") == 429 or failure.get("proxy_connect_http_status") == 429
    if failure.get("retryable") is not True:
        return (
            allow_cooldown_refusal is True
            and failure.get("code") == "request_rate_limited"
            and rate_limited
        )
    if country_read:
        if failure.get("failure_kind") == "transport_error":
            return failure.get("curl_code") in _TRANSIENT_VALIDATION_CURL_CODES or rate_limited
        return any(failure.get(name) in _TRANSIENT_VALIDATION_HTTP_STATUSES for name in ("http_status", "proxy_connect_http_status"))
    if failure.get("code") == "request_transport_failed":
        return failure.get("curl_code") in _TRANSIENT_VALIDATION_CURL_CODES
    return (
        failure.get("code") in {"request_http_failed", "request_rate_limited"}
        and failure.get("http_status") in _TRANSIENT_VALIDATION_HTTP_STATUSES
    )


def retry_readonly_validation(check):
    """Retry only a caller-bound candidate's GET checks, never session renewal."""
    if not callable(check):
        raise SessionError("Session validation requires a read-only check.")
    for attempt in range(1, MAX_VALIDATION_READ_ATTEMPTS + 1):
        try:
            return check(), attempt
        except SessionError as exc:
            exc.validation_read_attempts = attempt
            exc.validation_read_retries = attempt - 1
            if (attempt == MAX_VALIDATION_READ_ATTEMPTS or not isinstance(exc, (RequestFailure, ProxyCountryError))
                    or not read_only_validation_failure(exc)):
                raise
            failure = provider_failure(exc)
            if failure.get("http_status") != 429 and failure.get("proxy_connect_http_status") != 429:
                time.sleep(0.5 * attempt)
            if not wait_for_provider(exc):
                raise


def _cookie_value(value):
    return (
        isinstance(value, str) and len(value) <= 4096
        and all(33 <= ord(char) <= 126 and char not in '\";,\\' for char in value)
    )


def _sid_value(value):
    return _cookie_value(value) and bool(value) and value not in {"undefined", "null"} and "&" not in value


def _source(record):
    if not isinstance(record, dict) or type(record.get("source_row")) is not int or record["source_row"] < 1:
        raise SessionError(_INVALID)
    email = record.get("email")
    if (
        not isinstance(email, str) or len(email) > 320 or "@" not in email
        or any(ord(char) < 33 or ord(char) == 127 for char in email.strip())
    ):
        raise SessionError(_INVALID)
    metadata, cookies = record.get("legacy_metadata"), record.get("legacy_cookies")
    if not isinstance(metadata, dict) or not isinstance(cookies, dict) or not 1 <= len(cookies) <= 100:
        raise SessionError(_INVALID)
    if any(
        not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name) or not _cookie_value(value)
        for name, value in cookies.items()
    ) or len("; ".join(f"{name}={value}" for name, value in cookies.items())) > 65536:
        raise SessionError(_INVALID)
    sid, fingerprint = metadata.get("appsidsave"), metadata.get("session_fingerprint")
    cookie_sid = cookies.get("appsidsave")
    if (
        not _sid_value(sid) or not _cookie_value(fingerprint) or not fingerprint
        or fingerprint in {"undefined", "null"} or cookies.get("fingerprint") != fingerprint
        or not isinstance(cookie_sid, str)
        or not (cookie_sid == sid or unquote_plus(cookie_sid) == sid)
    ):
        raise SessionError(_INVALID)
    return record["source_row"], email.strip().casefold(), sid, fingerprint, dict(cookies)


def _bundle(email, sid, fingerprint, cookies):
    headers = {
        "user-agent": _AGENT, "origin": "https://play.anghami.com",
        "referer": "https://play.anghami.com/", "cookie": _cookie_header(cookies),
    }
    common = {
        "sid": sid, "appsid": sid, "fingerprint": fingerprint,
        "output": "jsonhp", "language": "en", "lang": "en", "userlanguageprod": "en", "web2": "true",
    }
    return validate_session({
        "format_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "origin": "https://play.anghami.com", "account_email": email,
        "requests": {
            name: {"method": "GET", "url": GATEWAY_URL + "?" + urlencode({**common, "type": operation, "angh_type": operation}), "headers": dict(headers)}
            for name, operation in OPERATIONS.items()
        },
    })


def _cookie_header(cookies):
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


class _RecoverySession(AnghamiSession):
    def _new_transport(self):
        if self._proxy is not None:
            return super()._new_transport()
        # Direct must not silently inherit HTTP(S)_PROXY from the environment.
        return requests.Session(**bandwidth_transport_options({
            "impersonate": "chrome", "retry": 0, "verify": True, "debug": False,
            "curl_options": {CurlOpt.PROXY: "", CurlOpt.NOPROXY: "*"},
        }))


def _merge_server_cookies(session, cookies):
    result = dict(cookies)
    jar = getattr(getattr(session._http, "cookies", None), "jar", ())
    for cookie in jar:
        if (
            cookie.name in _SERVER_COOKIE_NAMES and cookie.domain in _COOKIE_DOMAINS
            and cookie.path == "/" and _cookie_value(cookie.value)
            and (cookie.expires is None or cookie.expires > time.time())
        ):
            result[cookie.name] = cookie.value
    for template in session._saved["requests"].values():
        template["headers"]["cookie"] = _cookie_header(result)
    return result


def _profile_identity(session, email):
    template = session._template("relations")
    parts = urlsplit(template["url"])
    query = [(name, value) for name, value in parse_qsl(parts.query) if name not in {"type", "angh_type"}]
    query.extend((("type", "GETprofile"), ("angh_type", "GETprofile")))
    if not wait_before_provider_request("session_recovery_preflight"):
        raise RequestFailure("request_rate_limited", stage="session_recovery_preflight", http_status=429, retry_after_seconds=121, retry_safe=False)
    try:
        response = measured_request(session._http, "get",
            urlunsplit(parts._replace(query=urlencode(query))), headers=template["headers"],
            timeout=25, allow_redirects=False,
        )
    except (CurlError, OSError) as exc:
        raise RequestFailure("request_transport_failed", stage="session_recovery_preflight", curl_code=getattr(exc, "code", None)) from None
    except Exception:
        raise RequestFailure("session_response_invalid", stage="session_recovery_preflight") from None
    session._require_proxy_route(response)
    if response.status_code != 200:
        failure = RequestFailure("request_rate_limited" if response.status_code == 429 else "request_http_failed", stage="session_recovery_preflight", http_status=response.status_code,
                                 retry_after_seconds=retry_after_seconds(getattr(response, "headers", None)))
        observe_provider_failure(failure)
        raise failure
    try:
        data = response.json()
    except Exception:
        raise RequestFailure("session_response_invalid", stage="session_recovery_preflight") from None
    if not isinstance(data, dict):
        raise RequestFailure("session_response_invalid", stage="session_recovery_preflight")
    if data.get("status") == "failed":
        raise RequestFailure("session_authentication_rejected", stage="session_recovery_preflight")
    if (data.get("status") != "ok" or data.get("error")
            or not isinstance(data.get("email"), str) or not data["email"].strip()):
        raise RequestFailure("session_response_invalid", stage="session_recovery_preflight")
    if data["email"].strip().casefold() != email:
        raise RequestFailure("session_identity_mismatch", stage="session_recovery_preflight")


def recover_legacy_session(record, *, proxy=None):
    """Return a validated same-account bundle and safe metadata, without saving it."""
    row, email, sid, fingerprint, cookies = _source(record)
    if proxy is not None:
        try:
            if getattr(proxy, "country", None) != "EG":
                raise ValueError
            proof = proxy.verify_country()
            if not isinstance(proof, dict) or proof.get("country") != "EG" or proof.get("country_verified") is not True or proof.get("proxy_used") is not True:
                raise ProxyCountryError("response_invalid")
        except ProxyCountryError:
            raise
        except (CurlError, OSError) as exc:
            raise ProxyCountryError("transport_error", curl_code=getattr(exc, "code", None)) from None
        except Exception:
            raise ProxyCountryError("response_invalid") from None
    renewal_started = False
    validation_candidate = None
    try:
        with _RecoverySession(saved=_bundle(email, sid, fingerprint, cookies), proxy=proxy) as session:
            session.check(negative_control=True)
            cookies = _merge_server_cookies(session, cookies)
            _profile_identity(session, email)
            cookies = _merge_server_cookies(session, cookies)
            # This marker is assigned only after source binding and independent
            # server identity and anonymous-control checks have all succeeded.
            session._saved["renewal_method"] = "saved_sid"
            session._saved = validate_session(session._saved)
            gateway = PlaybackGateway(session)
            if not wait_before_provider_request("session_recovery_renewal"):
                raise RequestFailure("request_rate_limited", stage="session_recovery_renewal", http_status=429, retry_after_seconds=121, retry_safe=False)
            # A renewal can change the server session even if its response is
            # lost. Never replay this operation through another proxy.
            renewal_started = True
            gateway.bootstrap()
            issued_sid = gateway.tokens["socketsessionid"]
            if not _sid_value(issued_sid):
                raise SessionError("The existing session renewal returned an unsupported session identifier.")
            _merge_server_cookies(session, cookies)
            for template in session._saved["requests"].values():
                parts = urlsplit(template["url"])
                query = [(name, issued_sid if name in {"sid", "appsid"} else value) for name, value in parse_qsl(parts.query)]
                template["url"] = urlunsplit(parts._replace(query=urlencode(query)))
                template["headers"]["x-angh-session"] = issued_sid
            # Retain this issued candidate privately if its GET validation is
            # temporarily unavailable. Never reconstruct it by renewing again.
            validation_candidate = validate_session(deepcopy(session._saved))
            # Recheck exactly the templates that the caller will persist.
            _, validation_attempts = retry_readonly_validation(lambda: session.check(negative_control=True))
            _merge_server_cookies(session, cookies)
            saved = validate_session(deepcopy(session._saved))
        return saved, {
            "checked_at_utc": datetime.now(timezone.utc).isoformat(), "source_row": row,
            "authenticated": True, "identity_verified": True, "session_renewed": True,
            "browser_required": False, "password_required": False, "automatic_retry": False,
            "session_validation_attempts": validation_attempts,
            "session_validation_retries": validation_attempts - 1,
            "operations": {name: "ok" for name in OPERATIONS},
        }
    except RequestFailure as exc:
        if renewal_started:
            if validation_candidate is not None and read_only_validation_failure(exc, allow_cooldown_refusal=True):
                exc.validation_candidate = validation_candidate
                exc.renewal_completed = True
                exc.validation_pending = True
                exc.retry_safe = False
                raise
            exc.retry_safe = False
            if safe_request_failure(exc).get("failure_category") != "account":
                exc.renewal_unknown = True
        raise
    except SessionError as exc:
        # Library errors contain fixed messages only; transport exceptions and
        # unexpected parser/cookie failures are handled separately below.
        if renewal_started:
            exc.renewal_unknown = True
            exc.retry_safe = False
        raise
    except Exception:
        error = SessionError(_FAILED)
        if renewal_started:
            error.renewal_unknown = True
            error.retry_safe = False
        raise error from None
