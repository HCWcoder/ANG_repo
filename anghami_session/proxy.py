"""Encrypted PacketStream credentials and a single verified Egypt route."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from secrets import token_hex
import unicodedata

from curl_cffi import requests
from curl_cffi.const import CurlInfo, CurlOpt

from .errors import SessionError
from . import store

DEFAULT_PROXY_PATH = Path(__file__).resolve().parents[1] / ".anghami" / "packetstream.dpapi"
PACKETSTREAM_ENDPOINT = "https://proxy.packetstream.io:31111"
GEOLOCATION_URL = "https://ipinfo.io/json"
_COUNTRY_SUFFIX = "_country-EG"
_INVALID_CREDENTIALS = "PacketStream credentials must be nonempty base values without whitespace, control characters, or routing modifiers."
_AUTH_REJECTED = "PacketStream proxy authentication was rejected (HTTP 407). Check the configured credentials and available balance."


class _CountryFailure(SessionError):
    """A fixed country-check failure created locally, not by the transport."""


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

    def __post_init__(self):
        username, auth_key = _normalize_credentials(self.username, self.auth_key)
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "auth_key", auth_key)

    def transport_options(self) -> dict:
        return {
            "impersonate": "chrome", "proxy": PACKETSTREAM_ENDPOINT,
            "proxy_auth": (self.username, self.auth_key + _COUNTRY_SUFFIX + "_session-" + self._session_label),
            "retry": 0, "verify": True, "debug": False,
            "curl_options": {CurlOpt.NOPROXY: ""},
            "curl_infos": [CurlInfo.USED_PROXY, CurlInfo.HTTP_CONNECTCODE],
        }

    def browser_options(self) -> dict:
        """Use the same Egypt route and sticky credentials in a new context."""
        return {
            "server": PACKETSTREAM_ENDPOINT,
            "username": self.username,
            "password": self.auth_key + _COUNTRY_SUFFIX + "_session-" + self._session_label,
        }

    def summary(self) -> dict:
        return {"provider": "PacketStream", "country": "EG", "endpoint": PACKETSTREAM_ENDPOINT, "sticky": True}

    def verify_country(self) -> dict:
        """Make one cookie-free proxy request, failing closed on route or country."""
        response = None
        try:
            with requests.Session(**self.transport_options()) as transport:
                response = transport.get(GEOLOCATION_URL, timeout=25, allow_redirects=False)
                if _proxy_auth_rejected(response):
                    raise _CountryFailure(_AUTH_REJECTED)
                if type(getattr(response, "status_code", None)) is not int or response.status_code != 200:
                    raise _CountryFailure("The proxy country check did not return HTTP 200.")
                infos = getattr(response, "infos", {})
                if (
                    not isinstance(infos, dict) or type(infos.get(CurlInfo.USED_PROXY)) is not int
                    or infos[CurlInfo.USED_PROXY] != 1 or type(infos.get(CurlInfo.HTTP_CONNECTCODE)) is not int
                    or infos[CurlInfo.HTTP_CONNECTCODE] != 200
                ):
                    raise _CountryFailure("The proxy country check did not prove a successful proxy tunnel.")
                data = response.json()
                if not isinstance(data, dict) or data.get("country") != "EG" or data.get("error"):
                    raise _CountryFailure("The proxy country check did not verify Egypt.")
                return {
                    **self.summary(), "country_verified": True, "proxy_used": True,
                    "http_status": 200, "proxy_connect_http_status": 200,
                }
        except _CountryFailure as exc:
            raise SessionError(str(exc)) from None
        except Exception as exc:
            failure_response = getattr(exc, "response", None)
            if response is None:
                response = failure_response
            if _proxy_auth_rejected(failure_response, exc):
                raise SessionError(_AUTH_REJECTED) from None
            raise SessionError("The proxy country check failed. No direct connection was attempted.") from None
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass


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
