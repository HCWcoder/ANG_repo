"""Encrypted PacketStream credentials and verified sticky routes."""

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from secrets import token_hex
import time
import unicodedata

from curl_cffi import requests
from curl_cffi.const import CurlECode, CurlInfo, CurlOpt
from curl_cffi.curl import CurlError

from .bandwidth import bandwidth_transport_options, measured_request
from .errors import SessionError
from .country_lookup import wait_country_lookup_start
from . import store

DEFAULT_PROXY_PATH = Path(__file__).resolve().parents[1] / ".anghami" / "packetstream.dpapi"
PACKETSTREAM_ENDPOINT = "https://proxy.packetstream.io:31111"
GEOLOCATION_URL = "https://api.country.is/"
_COUNTRY_SUFFIX = "_country-EG"
SUPPORTED_ROUTE_COUNTRIES = frozenset({"EG", "US"})
_INVALID_CREDENTIALS = "PacketStream credentials must be nonempty base values without whitespace, control characters, or routing modifiers."
_AUTH_REJECTED = "PacketStream proxy authentication was rejected (HTTP 407). Check the configured credentials and available balance."
COUNTRY_CHECK_MAX_ATTEMPTS = 3
_TRANSIENT_CURL_CODES = frozenset({5, 6, 7, 18, 28, 35, 52, 55, 56})
_TRANSIENT_HTTP_STATUSES = frozenset({502, 503, 504})
_CURL_CODES = frozenset(int(value) for value in CurlECode if int(value) != 0)
_COUNTRY_FAILURE_MESSAGES = {
    "authentication_rejected": _AUTH_REJECTED,
    "http_failure": "The proxy country check did not return HTTP 200.",
    "route_unverified": "The proxy country check did not prove a successful proxy tunnel.",
    "country_unverified": "The proxy country check did not verify Egypt.",
    "response_invalid": "The proxy country check failed. No direct connection was attempted.",
    "transport_error": "The proxy country check failed. No direct connection was attempted.",
}
COUNTRY_FAILURE_KINDS = frozenset(_COUNTRY_FAILURE_MESSAGES)


def _safe_curl_code(value):
    if type(value) is int or isinstance(value, CurlECode):
        code = int(value)
        return code if code in _CURL_CODES else None
    return None


def _safe_http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


class ProxyCountryError(SessionError):
    """Fixed country-check failure with bounded, secret-free numeric evidence."""

    def __init__(self, failure_kind, *, curl_code=None, http_status=None, proxy_connect_http_status=None, country_check_attempts=1, retry_after_seconds=None, retry_safe=True, expected_country="EG"):
        if type(failure_kind) is not str or failure_kind not in COUNTRY_FAILURE_KINDS:
            raise ValueError("Use a supported proxy country-check failure kind.")
        if type(expected_country) is not str or expected_country not in SUPPORTED_ROUTE_COUNTRIES:
            raise ValueError("Use a supported PacketStream route country.")
        if type(country_check_attempts) is not int or not 1 <= country_check_attempts <= COUNTRY_CHECK_MAX_ATTEMPTS:
            raise ValueError("Use a bounded proxy country-check attempt count.")
        message = _COUNTRY_FAILURE_MESSAGES[failure_kind]
        if failure_kind == "country_unverified" and expected_country == "US":
            message = "The proxy country check did not verify the configured country."
        super().__init__(message)
        self.failure_kind = failure_kind
        self.curl_code = _safe_curl_code(curl_code)
        self.http_status = _safe_http_status(http_status)
        self.proxy_connect_http_status = _safe_http_status(proxy_connect_http_status)
        self.country_check_attempts = country_check_attempts
        self.retry_safe = retry_safe is True
        self.retry_after_seconds = (
            retry_after_seconds if type(retry_after_seconds) in (int, float)
            and math.isfinite(retry_after_seconds) and 0 <= retry_after_seconds <= 86400 else None
        )

    @property
    def diagnostics(self):
        return safe_proxy_country_failure(self)


def safe_proxy_country_failure(error):
    """Validate typed fields again; arbitrary attributes and text stay private."""
    if not isinstance(error, ProxyCountryError):
        return {}
    try:
        kind, attempts = error.failure_kind, error.country_check_attempts
        if type(kind) is not str or kind not in COUNTRY_FAILURE_KINDS or type(attempts) is not int or not 1 <= attempts <= COUNTRY_CHECK_MAX_ATTEMPTS:
            return {}
        result = {"failure_kind": kind, "country_check_attempts": attempts}
        curl_code = _safe_curl_code(error.curl_code)
        if curl_code is not None:
            result["curl_code"] = curl_code
        for name in ("http_status", "proxy_connect_http_status"):
            status = _safe_http_status(getattr(error, name, None))
            if status is not None:
                result[name] = status
        delay = getattr(error, "retry_after_seconds", None)
        if type(delay) in (int, float) and math.isfinite(delay) and 0 <= delay <= 86400:
            result["retry_after_seconds"] = delay
        if getattr(error, "retry_safe", True) is False:
            result["retryable"] = False
        return result
    except Exception:
        return {}


def _country_error(kind, response, *, attempt, exception=None, expected_country="EG"):
    from .provider_recovery import observe_provider_failure, retry_after_seconds
    infos = getattr(response, "infos", {})
    failure = ProxyCountryError(
        kind, curl_code=getattr(exception, "code", None) if isinstance(exception, CurlError) else None,
        http_status=getattr(response, "status_code", None),
        proxy_connect_http_status=infos.get(CurlInfo.HTTP_CONNECTCODE) if isinstance(infos, dict) else None,
        country_check_attempts=attempt,
        retry_after_seconds=retry_after_seconds(getattr(response, "headers", None)),
        expected_country=expected_country,
    )
    observe_provider_failure(failure)
    return failure


def _route_verified(response):
    infos = getattr(response, "infos", {})
    return (
        isinstance(infos, dict) and type(infos.get(CurlInfo.USED_PROXY)) is int
        and infos[CurlInfo.USED_PROXY] == 1 and type(infos.get(CurlInfo.HTTP_CONNECTCODE)) is int
        and infos[CurlInfo.HTTP_CONNECTCODE] == 200
    )


def _normalize_credentials(username, auth_key) -> tuple[str, str]:
    for value in (username, auth_key):
        if (
            not isinstance(value, str) or not value
            or any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
        ):
            raise SessionError(_INVALID_CREDENTIALS)
    if ":" in username:
        raise SessionError(_INVALID_CREDENTIALS)
    if auth_key.endswith(_COUNTRY_SUFFIX):
        auth_key = auth_key[:-len(_COUNTRY_SUFFIX)]
    if not auth_key or any(
        marker in value.casefold()
        for value in (username, auth_key) for marker in ("_country", "_session")
    ):
        raise SessionError(_INVALID_CREDENTIALS)
    return username, auth_key


def _proxy_auth_rejected(response, exception=None) -> bool:
    if response is not None:
        infos = getattr(response, "infos", {})
        if getattr(response, "status_code", None) == 407:
            return True
        if isinstance(infos, dict) and infos.get(CurlInfo.HTTP_CONNECTCODE) == 407:
            return True
    # CONNECT failures can raise before a populated Response is attached. Use
    # only an HTTP status marker for classification; never expose this text.
    if exception is None:
        return False
    try:
        text = str(exception)
    except Exception:
        return False
    return re.search(
        r"(?:HTTP(?:/\d(?:\.\d)?)?\s+|response\s*[:=]?\s*)407\b",
        text, re.IGNORECASE,
    ) is not None


@dataclass(frozen=True, slots=True)
class PacketStreamProxy:
    username: str = field(repr=False)
    auth_key: str = field(repr=False)
    country: str = field(default="EG", init=False)
    _session_label: str = field(default_factory=lambda: token_hex(8), init=False, repr=False)
    _endpoint: str = field(default=PACKETSTREAM_ENDPOINT, init=False, repr=False)

    def __post_init__(self):
        if type(self.country) is not str or self.country not in SUPPORTED_ROUTE_COUNTRIES:
            raise SessionError("The PacketStream route country is not supported.")
        username, auth_key = _normalize_credentials(self.username, self.auth_key)
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "auth_key", auth_key)

    @classmethod
    def from_route(cls, username, auth_key, session_label, endpoint=PACKETSTREAM_ENDPOINT, *, country="EG"):
        """Construct a supplied sticky route without accepting arbitrary destinations."""
        if (
            type(session_label) is not str
            or re.fullmatch(r"[A-Za-z0-9]{1,64}", session_label) is None
            or type(country) is not str or country not in SUPPORTED_ROUTE_COUNTRIES
            or type(endpoint) is not str
            or endpoint not in {PACKETSTREAM_ENDPOINT, "http://proxy.packetstream.io:31112"}
            or any(
                not isinstance(value, str)
                or any(marker in value.casefold() for marker in ("_country", "_session"))
                for value in (username, auth_key)
            )
        ):
            raise SessionError("The PacketStream sticky route is invalid.")
        proxy = cls(username, auth_key)
        object.__setattr__(proxy, "country", country)
        object.__setattr__(proxy, "_session_label", session_label)
        object.__setattr__(proxy, "_endpoint", endpoint)
        return proxy

    def transport_options(self) -> dict:
        return bandwidth_transport_options({
            "impersonate": "chrome", "proxy": self._endpoint,
            "proxy_auth": (self.username, self.auth_key + f"_country-{self.country}_session-" + self._session_label),
            "retry": 0, "verify": True, "debug": False,
            "curl_options": {CurlOpt.NOPROXY: ""},
            "curl_infos": [CurlInfo.USED_PROXY, CurlInfo.HTTP_CONNECTCODE],
        })

    def browser_options(self) -> dict:
        """Use the same country route and sticky credentials in a new context."""
        return {
            "server": self._endpoint,
            "username": self.username,
            "password": self.auth_key + f"_country-{self.country}_session-" + self._session_label,
        }

    def summary(self) -> dict:
        return {"provider": "PacketStream", "country": self.country, "endpoint": self._endpoint, "sticky": True}

    def verify_country(self) -> dict:
        """Retry bounded cookie-free checks only, keeping the same sticky route."""
        from .provider_recovery import MAX_COOLDOWN_WAIT_SECONDS
        for attempt in range(1, COUNTRY_CHECK_MAX_ATTEMPTS + 1):
            response, failure, retryable = None, None, False
            try:
                if not wait_country_lookup_start():
                    raise ProxyCountryError("http_failure", http_status=429, retry_after_seconds=MAX_COOLDOWN_WAIT_SECONDS + 1, country_check_attempts=attempt, retry_safe=False)
                with requests.Session(**self.transport_options()) as transport:
                    response = measured_request(transport, "get", GEOLOCATION_URL, proxy_used=True, timeout=25, allow_redirects=False)
                    if _proxy_auth_rejected(response):
                        raise _country_error("authentication_rejected", response, attempt=attempt)
                    if type(getattr(response, "status_code", None)) is not int or response.status_code != 200:
                        retryable = type(response.status_code) is int and response.status_code in _TRANSIENT_HTTP_STATUSES and _route_verified(response)
                        raise _country_error("http_failure", response, attempt=attempt)
                    if not _route_verified(response):
                        raise _country_error("route_unverified", response, attempt=attempt)
                    try:
                        data = response.json()
                    except Exception:
                        raise _country_error("response_invalid", response, attempt=attempt) from None
                    if not isinstance(data, dict) or data.get("country") != self.country or data.get("error"):
                        raise _country_error("country_unverified", response, attempt=attempt, expected_country=self.country)
                    return {
                        **self.summary(), "country_verified": True, "proxy_used": True,
                        "http_status": 200, "proxy_connect_http_status": 200,
                        "country_check_attempts": attempt,
                    }
            except ProxyCountryError as exc:
                failure = exc
            except Exception as exc:
                failure_response = getattr(exc, "response", None)
                if response is None:
                    response = failure_response
                if _proxy_auth_rejected(failure_response, exc):
                    failure = _country_error("authentication_rejected", response, attempt=attempt, exception=exc)
                else:
                    failure = _country_error("transport_error", response, attempt=attempt, exception=exc)
                    retryable = (
                        failure.curl_code in _TRANSIENT_CURL_CODES
                        and failure.http_status not in {403, 407, 429}
                        and failure.proxy_connect_http_status not in {403, 407, 429}
                        # A real origin response with contradictory route evidence
                        # is definitive. An incomplete connection may be retried.
                        and (failure.http_status is None or _route_verified(response))
                    )
            finally:
                if response is not None:
                    close = getattr(response, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
            if not retryable or attempt == COUNTRY_CHECK_MAX_ATTEMPTS:
                raise failure from None
            time.sleep(0.5 * attempt)


def save_packetstream_credentials(username, auth_key, path=DEFAULT_PROXY_PATH) -> dict:
    proxy = PacketStreamProxy(username, auth_key)
    data = {
        "format_version": 1, "provider": "PacketStream", "country": "EG",
        "username": proxy.username, "auth_key": proxy.auth_key,
    }
    try:
        store.save_protected_bytes(json.dumps(data).encode("utf-8"), Path(path))
    except Exception:
        raise SessionError("The PacketStream proxy configuration could not be saved securely.") from None
    return proxy.summary()


def load_packetstream_proxy(path=DEFAULT_PROXY_PATH) -> PacketStreamProxy:
    try:
        raw = store.load_protected_bytes(Path(path))
    except FileNotFoundError:
        raise SessionError("PacketStream proxy credentials are not configured. Save them before using the Egypt proxy.") from None
    except Exception:
        raise SessionError("The PacketStream proxy configuration could not be unlocked. Use the Windows user who saved it or configure it again.") from None
    try:
        data = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(data, dict)
            or set(data) != {"format_version", "provider", "country", "username", "auth_key"}
            or type(data["format_version"]) is not int or data["format_version"] != 1
            or data["provider"] != "PacketStream" or data["country"] != "EG"
        ):
            raise ValueError
        return PacketStreamProxy(data["username"], data["auth_key"])
    except Exception:
        raise SessionError("The PacketStream proxy configuration is invalid. Configure it again.") from None
