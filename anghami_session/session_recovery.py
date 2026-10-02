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

from .client import AnghamiSession, GATEWAY_URL, OPERATIONS, validate_session
from .errors import SessionError
from .media_gateway import PlaybackGateway

_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_SERVER_COOKIE_NAMES = frozenset({"appsidsave", "oats", "ssss"})
_COOKIE_DOMAINS = frozenset({"anghami.com", ".anghami.com", "coussa.anghami.com", ".coussa.anghami.com"})
_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36"
_INVALID = "The selected account has no compatible existing legacy session. A normal browser login is required."
_FAILED = "The existing session could not be recovered. No browser login or automatic retry was attempted."


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
        return requests.Session(
            impersonate="chrome", retry=0, verify=True, debug=False,
            curl_options={CurlOpt.PROXY: "", CurlOpt.NOPROXY: "*"},
        )


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
    response = session._http.get(
        urlunsplit(parts._replace(query=urlencode(query))), headers=template["headers"],
        timeout=25, allow_redirects=False,
    )
    session._require_proxy_route(response)
    data = response.json()
    if (
        response.status_code != 200 or not isinstance(data, dict) or data.get("status") != "ok"
        or data.get("error") or not isinstance(data.get("email"), str)
        or data["email"].strip().casefold() != email
    ):
        raise SessionError("The existing session did not identify the selected account. A normal browser login is required.")


def recover_legacy_session(record, *, proxy=None):
    """Return a validated same-account bundle and safe metadata, without saving it."""
    row, email, sid, fingerprint, cookies = _source(record)
    if proxy is not None:
        try:
            if getattr(proxy, "country", None) != "EG":
                raise ValueError
            proof = proxy.verify_country()
            if not isinstance(proof, dict) or proof.get("country") != "EG" or proof.get("country_verified") is not True or proof.get("proxy_used") is not True:
                raise ValueError
        except Exception:
            raise SessionError("The proxy country check failed. No direct connection was attempted.") from None
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
            # Recheck exactly the templates that the caller will persist.
            session.check(negative_control=True)
            _merge_server_cookies(session, cookies)
            saved = validate_session(deepcopy(session._saved))
        return saved, {
            "checked_at_utc": datetime.now(timezone.utc).isoformat(), "source_row": row,
            "authenticated": True, "identity_verified": True, "session_renewed": True,
            "browser_required": False, "password_required": False, "automatic_retry": False,
            "operations": {name: "ok" for name in OPERATIONS},
        }
    except SessionError:
        # Library errors contain fixed messages only; transport exceptions and
        # unexpected parser/cookie failures are handled separately below.
        raise
    except Exception:
        raise SessionError(_FAILED) from None
