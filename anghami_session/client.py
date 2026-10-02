"""Replay captured, read-only requests with the browser-compatible HTTP transport."""

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from curl_cffi import CurlInfo, requests

from .errors import SessionError
from .store import DEFAULT_SESSION_PATH, load_session

GATEWAY_URL = "https://coussa.anghami.com/gateway.php"
OPERATIONS = {"relations": "GETuserrelations", "playlists": "GETplaylists"}
IGNORED_HEADERS = {"host", "content-length", "accept-encoding", "connection", "proxy-authorization"}
AUTH_HEADERS = {"cookie", "authorization", "proxy-authorization", "x-angh-session"}


def validate_session(saved: dict) -> dict:
    """Validate before any request, and retain only the supported session fields."""
    if (
        not isinstance(saved, dict)
        or saved.get("format_version") != 1
        or not isinstance(saved.get("created_at_utc"), str)
        or saved.get("origin") != "https://play.anghami.com"
        or not isinstance(saved.get("requests"), dict)
        or "relations" not in saved["requests"]
    ):
        raise SessionError("The saved session format is not supported. Capture a new login.")
    templates = {}
    for operation, template in saved["requests"].items():
        if operation not in OPERATIONS or not isinstance(template, dict):
            raise SessionError("The saved session contains an unsupported operation.")
        url = template.get("url")
        if not isinstance(url, str):
            raise SessionError("The saved request URL is invalid.")
        try:
            parts = urlsplit(url)
            query = parse_qsl(parts.query, keep_blank_values=True)
        except ValueError:
            raise SessionError("The saved request URL is invalid.") from None
        if (
            parts.scheme != "https"
            or parts.netloc != "coussa.anghami.com"
            or parts.path != "/gateway.php"
            or parts.fragment
            or template.get("method") != "GET"
        ):
            raise SessionError("The saved request is outside the expected Anghami endpoint.")
        values = [value for key, value in query if key == "type"]
        aliases = [value for key, value in query if key == "angh_type"]
        if values != [OPERATIONS[operation]] or (aliases and aliases != values):
            raise SessionError("The saved request does not match its read-only operation.")
        if any(key.lower() in {"u", "p", "password", "re_token"} for key, _ in query):
            raise SessionError("A login request cannot be used as a saved session.")
        if not any(key in {"sid", "appsid"} and value for key, value in query):
            raise SessionError("The saved request is missing its session identifier.")
        headers = template.get("headers")
        if not isinstance(headers, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            or any(c in k + v for c in "\r\n\0") for k, v in headers.items()
        ):
            raise SessionError("The saved request headers are invalid.")
        templates[operation] = {
            "method": "GET", "url": url,
            "headers": {
                k.lower(): v for k, v in headers.items()
                if not k.startswith(":") and k.lower() not in IGNORED_HEADERS
            },
        }
    result = {
        "format_version": 1,
        "created_at_utc": saved["created_at_utc"],
        "origin": saved["origin"],
        "requests": templates,
    }
    if "account_email" in saved:
        email = saved["account_email"]
        if not isinstance(email, str) or "@" not in email or any(c in email for c in "\r\n\0"):
            raise SessionError("The captured account identity is invalid.")
        result["account_email"] = email.strip().casefold()
    if "renewal_method" in saved:
        if saved["renewal_method"] != "saved_sid" or not isinstance(saved["renewal_method"], str) or not result.get("account_email"):
            raise SessionError("The saved session renewal method is not supported.")
        result["renewal_method"] = "saved_sid"
    return result


class AnghamiSession:
    def __init__(self, path: Path = DEFAULT_SESSION_PATH, *, saved: dict | None = None, proxy=None):
        self._saved = validate_session(load_session(path) if saved is None else saved)
        self._proxy = proxy
        self._proxy_check = None
        # Regular requests/httpx returned 403 for this same valid session.
        self._http = self._new_transport()

    def _new_transport(self):
        options = {"impersonate": "chrome"} if self._proxy is None else self._proxy.transport_options()
        return requests.Session(**options)

    @property
    def proxy_summary(self):
        if self._proxy is None:
            return None
        result = self._proxy.summary()
        if self._proxy_check is not None:
            result["exit_check"] = self._proxy_check
        return result

    def _require_proxy_route(self, response):
        # A reused HTTPS tunnel has no new CONNECT response, so libcurl reports
        # zero here. USED_PROXY still identifies the route for that request.
        if self._proxy is not None:
            infos = getattr(response, "infos", None)
            if (
                not isinstance(infos, dict)
                or type(infos.get(CurlInfo.USED_PROXY)) is not int or infos[CurlInfo.USED_PROXY] != 1
                or type(infos.get(CurlInfo.HTTP_CONNECTCODE)) is not int or infos[CurlInfo.HTTP_CONNECTCODE] not in {0, 200}
            ):
                raise SessionError("The configured proxy route was not confirmed. No direct fallback is permitted.")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self._http.close()

    def _template(self, operation: str) -> dict:
        template = self._saved["requests"].get(operation)
        if template is None:
            raise SessionError("No captured request for that operation. Refresh the login session.")
        return template

    def request(self, operation: str = "relations") -> dict:
        template = self._template(operation)
        try:
            response = self._http.get(
                template["url"], headers=template["headers"],
                timeout=25, allow_redirects=False,
            )
        except Exception as exc:
            # Transport exceptions may contain a URL with session secrets.
            raise SessionError(f"Anghami request failed ({type(exc).__name__}).") from None
        self._require_proxy_route(response)
        if response.status_code != 200:
            raise SessionError(f"Anghami returned HTTP {response.status_code}. Refresh or check the saved session.")
        try:
            data = response.json()
        except ValueError:
            raise SessionError("Anghami returned an unexpected response format.") from None
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise SessionError("The saved session was not accepted. Run python -m anghami_session login.")
        return data

    def song(self, song_id: str | int) -> dict:
        """Read one song using the selected account's existing HTTP session."""
        song_id = str(song_id)
        if not song_id.isascii() or not song_id.isdecimal() or not 1 <= len(song_id) <= 20:
            raise SessionError("Song ID must contain one to twenty decimal digits.")
        template = self._template("relations")
        parts = urlsplit(template["url"])
        common = {"output", "sid", "appsid", "fingerprint", "web2", "language", "lang", "userlanguageprod"}
        query = [(k, v) for k, v in parse_qsl(parts.query) if k in common]
        query.extend((("type", "GETsong"), ("angh_type", "GETsong"), ("songId", song_id)))
        try:
            response = self._http.get(
                urlunsplit(parts._replace(query=urlencode(query))),
                headers=template["headers"], timeout=25, allow_redirects=False,
            )
        except Exception as exc:
            raise SessionError(f"Song request failed ({type(exc).__name__}).") from None
        self._require_proxy_route(response)
        if response.status_code != 200:
            raise SessionError(f"Song request returned HTTP {response.status_code}.")
        try:
            data = response.json()
        except ValueError:
            raise SessionError("Song request returned an unexpected response format.") from None
        if (
            not isinstance(data, dict) or isinstance(data.get("status"), bool)
            or data.get("status") not in (1, "1", "ok") or str(data.get("id")) != song_id
        ):
            raise SessionError("The requested song was not returned by Anghami.")
        return data

    def media_source(self, song_id: str | int) -> dict:
        from .media_gateway import PlaybackGateway
        return PlaybackGateway(self).media_source(song_id)

    def check(self, *, negative_control: bool = False) -> dict:
        results = {operation: self.request(operation)["status"] for operation in self._saved["requests"]}
        report = {
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "authenticated": True,
            "http_status": 200,
            "api_status": "ok",
            "operations": results,
            "transport": "curl_cffi",
            "browser_required": False,
            "password_required": False,
            "session_saved_at_utc": self._saved["created_at_utc"],
        }
        if self.proxy_summary is not None:
            report["proxy"] = self.proxy_summary
        if negative_control:
            template = self._template("relations")
            parts = urlsplit(template["url"])
            query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in {"sid", "appsid"}]
            url = urlunsplit(parts._replace(query=urlencode(query)))
            headers = {k: v for k, v in template["headers"].items() if k.lower() not in AUTH_HEADERS}
            try:
                # A fresh transport cannot inherit cookies from the positive check.
                with self._new_transport() as anonymous:
                    response = anonymous.get(url, headers=headers, timeout=25, allow_redirects=False)
                    self._require_proxy_route(response)
                    control = response.json()
            except Exception as exc:
                raise SessionError(f"Unauthenticated control failed ({type(exc).__name__}).") from None
            rejected = response.status_code == 200 and isinstance(control, dict) and control.get("status") == "failed"
            if not rejected:
                raise SessionError("The unauthenticated control did not reject authentication as expected.")
            report["without_session"] = {
                "http_status": response.status_code,
                "api_status": "failed",
                "authentication_rejected": True,
            }
        return report
