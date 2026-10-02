"""Optional login data reduction without sharing account state."""

from hashlib import sha256
import json
import math
from pathlib import Path
import re
import tempfile
from threading import RLock
import time
from urllib.parse import unquote, urlsplit

from curl_cffi import CurlInfo, requests
from curl_cffi.const import CurlOpt

DEFAULT_CACHE_PATH = Path(__file__).resolve().parents[1] / ".anghami" / "browser-public-assets"
MAX_ASSET_BYTES = 8_000_000
MAX_CACHE_BYTES = 64_000_000
_VERSIONED_ASSET = re.compile(r"/web/(?:[A-Za-z0-9_-]+\.)+[0-9a-f]{8,64}\.(js|css)\Z")
_MAX_FINITE_CACHE_SECONDS = 86_400
_OPTIONAL_PATH = re.compile(r"/(?:assets?|static|images?|img|artworks?|covers?|albumart|avatars?|icons?|logos?|sprites?|fonts?|media|audio)(?:/|\Z)", re.I)
_SECURITY_MARKER = re.compile(r"(?:captcha|challenge|turnstile|arkose|funcaptcha|verification|verify|security|_fs-ch|cdn-cgi|__cf_chl|(?:^|[/_.-])auth|(?:^|[/_.-])login|signin|two.?factor|(?:^|[/_.-])otp)", re.I)
_SECURITY_HOSTS = ("cloudflare.com", "hcaptcha.com", "recaptcha.net", "arkoselabs.com", "funcaptcha.com")
_COSMETIC_LOGIN_IMAGES = frozenset({
    "/web/assets/img/login-newest-background.png",
    "/web/assets/img/login-moving-circle.png",
})
_PUBLIC_FONT_PATHS = frozenset({
    f"/web/assets/fonts/euclid/EuclidCircularA-{weight}.{extension}"
    for weight in ("Bold", "Light", "LightItalic", "Medium", "Regular", "SemiBold")
    for extension in ("ttf", "woff", "woff2")
}) | frozenset({
    "/web/assets/fonts/tajawal/tajawal-700.woff2",
    "/web/assets/fonts/tajawal/tajawal-latin-700.woff2",
})
_STORED_HEADERS = frozenset({
    "content-type", "cache-control", "expires", "etag", "last-modified", "vary",
    "access-control-allow-origin", "cross-origin-resource-policy",
})
_CACHE_NAME = re.compile(r"[0-9a-f]{64}\.body\Z")


def _subdomain(host, domain):
    return host == domain or host.endswith("." + domain)


def public_asset_kind(url):
    """Only verified app code and cosmetics qualify; security libraries do not."""
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.username is not None or parts.password is not None
            or parts.port not in {None, 443} or parts.query or parts.fragment
        ):
            return None
        if parts.hostname != "cdnweb.anghami.com":
            return None
        if parts.path in _COSMETIC_LOGIN_IMAGES or parts.path in _PUBLIC_FONT_PATHS:
            if "?" in url or "#" in url:
                return None
            return "image" if parts.path in _COSMETIC_LOGIN_IMAGES else "font"
        match = _VERSIONED_ASSET.fullmatch(parts.path)
        return {"js": "script", "css": "stylesheet"}.get(match.group(1)) if match else None
    except (TypeError, ValueError):
        return None


def _requires_expiry(url):
    parts = urlsplit(url)
    return (
        parts.hostname == "cdnweb.anghami.com"
        and (parts.path in _COSMETIC_LOGIN_IMAGES or parts.path in _PUBLIC_FONT_PATHS)
    )


def _cache_seconds(headers):
    """Finite public cache entries need one positive, unambiguous max-age."""
    directives = [item.strip() for item in headers.get("cache-control", "").split(",")]
    ages = [item for item in directives if item.split("=", 1)[0].strip().lower() == "max-age"]
    if len(ages) != 1:
        return None
    match = re.fullmatch(r"max-age=([0-9]+)", ages[0], re.I)
    seconds = match[1].lstrip("0") if match else ""
    if not seconds:
        return None
    return _MAX_FINITE_CACHE_SECONDS if len(seconds) > 5 else min(int(seconds), _MAX_FINITE_CACHE_SECONDS)


def should_abort_asset(url, resource_type):
    """Conservative reduction: unfamiliar resources and security always pass."""
    if resource_type not in {"image", "font", "media"}:
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in {"http", "https"} or not host:
            return False
        if any(_subdomain(host, domain) for domain in _SECURITY_HOSTS):
            return False
        if (
            resource_type == "image" and host == "cdnweb.anghami.com"
            and parts.path in _COSMETIC_LOGIN_IMAGES and not parts.query
        ):
            return True
        if _SECURITY_MARKER.search(unquote(parts.path + "?" + parts.query)):
            return False
        if resource_type == "image" and host == "artwork.anghcdn.co":
            return True
        if resource_type == "font" and host == "fonts.gstatic.com" and parts.path.startswith("/s/"):
            return True
        return _subdomain(host, "anghami.com") and _OPTIONAL_PATH.search(parts.path) is not None
    except (TypeError, ValueError):
        return False


def _cache_headers(url, headers):
    kind = public_asset_kind(url)
    if kind is None or not isinstance(headers, dict):
        return None
    lowered = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str) or any(c in name + value for c in "\r\n\0"):
            return None
        lowered[name.lower()] = value
    if "set-cookie" in lowered or "authorization" in lowered or "proxy-authorization" in lowered:
        return None
    directives = {item.strip().split("=", 1)[0].lower() for item in lowered.get("cache-control", "").split(",")}
    if "public" not in directives or directives.intersection({"private", "no-store", "no-cache"}):
        return None
    vary = {item.strip().lower() for item in lowered.get("vary", "").split(",") if item.strip()}
    if vary - {"accept-encoding"}:
        return None
    mime = lowered.get("content-type", "").split(";", 1)[0].strip().lower()
    if kind == "font":
        accepted = {"font/" + urlsplit(url).path.rsplit(".", 1)[1]}
    else:
        accepted = {
            "stylesheet": {"text/css"}, "image": {"image/png"},
            "script": {"text/javascript", "application/javascript", "application/x-javascript"},
        }[kind]
    if mime not in accepted:
        return None
    if _requires_expiry(url) and _cache_seconds(lowered) is None:
        return None
    return {name: value for name, value in lowered.items() if name in _STORED_HEADERS}


class PublicAssetCache:
    """Bounded public bytes only; corruption is a normal cache miss."""

    def __init__(self, directory=DEFAULT_CACHE_PATH, *, max_asset_bytes=MAX_ASSET_BYTES, max_total_bytes=MAX_CACHE_BYTES):
        self.directory = Path(directory)
        self.max_asset_bytes = max_asset_bytes
        self.max_total_bytes = max_total_bytes
        self._lock = RLock()

    def _paths(self, url):
        key = sha256(url.encode("utf-8")).hexdigest()
        return self.directory / (key + ".body"), self.directory / (key + ".json")

    def get(self, url):
        if public_asset_kind(url) is None:
            return None
        try:
            with self._lock:
                body_path, metadata_path = self._paths(url)
                if body_path.stat().st_size > self.max_asset_bytes or metadata_path.stat().st_size > 16_384:
                    return None
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(metadata, dict) or metadata.get("format_version") != 2
                    or metadata.get("anonymous") is not True
                    or metadata.get("url") != url
                    or not isinstance(metadata.get("headers"), dict)
                    or set(metadata["headers"]) - _STORED_HEADERS
                ):
                    return None
                headers = _cache_headers(url, metadata["headers"])
                if headers is None:
                    return None
                if _requires_expiry(url):
                    stored = metadata.get("cached_at_unix")
                    expires = metadata.get("expires_at_unix")
                    if (
                        type(stored) not in {int, float} or type(expires) not in {int, float}
                        or not math.isfinite(stored) or not math.isfinite(expires)
                        or expires != stored + _cache_seconds(headers)
                        or not 0 <= stored <= time.time() < expires
                    ):
                        return None
                body = body_path.read_bytes()
                if (
                    len(body) > self.max_asset_bytes or metadata.get("body_length") != len(body)
                    or metadata.get("body_sha256") != sha256(body).hexdigest()
                ):
                    return None
                return {"body": body, "headers": headers}
        except (OSError, ValueError, TypeError, OverflowError):
            return None

    @staticmethod
    def _atomic_write(path, value):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(value)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def put(self, url, body, headers, *, anonymous=False):
        headers = _cache_headers(url, headers)
        if anonymous is not True or headers is None or not isinstance(body, bytes) or not body or len(body) > min(self.max_asset_bytes, self.max_total_bytes):
            return False
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                body_path, metadata_path = self._paths(url)
                entries = [path for path in self.directory.glob("*.body") if _CACHE_NAME.fullmatch(path.name) and path != body_path]
                total = sum(path.stat().st_size for path in entries)
                for path in sorted(entries, key=lambda item: item.stat().st_mtime):
                    if total + len(body) <= self.max_total_bytes:
                        break
                    total -= path.stat().st_size
                    path.unlink()
                    path.with_suffix(".json").unlink(missing_ok=True)
                metadata = {
                    "format_version": 2, "anonymous": True, "url": url, "body_length": len(body),
                    "body_sha256": sha256(body).hexdigest(), "headers": headers,
                }
                if _requires_expiry(url):
                    stored = time.time()
                    metadata.update(
                        cached_at_unix=stored,
                        expires_at_unix=stored + _cache_seconds(headers),
                    )
                self._atomic_write(body_path, body)
                self._atomic_write(metadata_path, json.dumps(metadata).encode("utf-8"))
                return True
        except (OSError, ValueError, TypeError):
            return False


def load_anonymous_public_asset(url, *, proxy=None, max_bytes=MAX_ASSET_BYTES):
    """Fetch one eligible public asset without account headers or cookies."""
    if public_asset_kind(url) is None:
        return None
    response = None
    try:
        options = dict(
            {"impersonate": "chrome", "retry": 0, "verify": True, "debug": False,
             "curl_options": {CurlOpt.PROXY: "", CurlOpt.NOPROXY: "*"}}
            if proxy is None else proxy.transport_options()
        )
        options["curl_options"] = {
            **options.get("curl_options", {}), CurlOpt.TIMEOUT_MS: 10_000,
        }
        # A new cookie jar and one fixed URL; browser request data is never used.
        with requests.Session(**options) as transport:
            response = transport.get(url, timeout=10, allow_redirects=False, stream=True)
            if type(response.status_code) is not int or response.status_code != 200:
                return None
            if proxy is not None:
                infos = getattr(response, "infos", {})
                if (
                    not isinstance(infos, dict) or type(infos.get(CurlInfo.USED_PROXY)) is not int
                    or infos[CurlInfo.USED_PROXY] != 1
                    or type(infos.get(CurlInfo.HTTP_CONNECTCODE)) is not int
                    or infos[CurlInfo.HTTP_CONNECTCODE] != 200
                ):
                    return None
            raw_headers = dict(response.headers)
            headers = _cache_headers(url, raw_headers)
            if headers is None:
                return None
            length = next((value for name, value in raw_headers.items() if name.lower() == "content-length"), None)
            if length is not None and (not length.isdecimal() or int(length) > max_bytes):
                return None
            body = bytearray()
            for chunk in response.iter_content():
                if not isinstance(chunk, bytes) or len(body) + len(chunk) > max_bytes:
                    return None
                body.extend(chunk)
            if not body:
                return None
            return {"body": bytes(body), "headers": headers}
    except Exception:
        return None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


class BrowserDataReduction:
    def __init__(self, context, cache=None, proxy=None):
        self.cache = PublicAssetCache() if cache is None else cache
        self._proxy = proxy
        self.blocked_requests = self.cache_hits = self.cache_writes = 0
        self.anonymous_requests = self.anonymous_failures = self.anonymous_bytes = 0
        context.route("**/*", self._route)

    @staticmethod
    def _eligible_request(request):
        try:
            kind = public_asset_kind(request.url)
            # Service-worker precaching requests these public assets via fetch.
            if (
                request.method != "GET" or kind is None
                or request.resource_type not in {kind, "fetch", "xhr", "other"}
            ):
                return False
            headers = request.all_headers()
            return not {name.lower() for name in headers}.intersection({"authorization", "proxy-authorization"})
        except Exception:
            return False

    def _route(self, route):
        request = route.request
        if should_abort_asset(request.url, request.resource_type):
            route.abort()
            self.blocked_requests += 1
            return
        if self._eligible_request(request):
            try:
                cached = self.cache.get(request.url)
            except Exception:
                cached = None
            cache_hit = cached is not None
            if cached is None:
                self.anonymous_requests += 1
                cached = load_anonymous_public_asset(
                    request.url, proxy=self._proxy, max_bytes=self.cache.max_asset_bytes,
                )
                if cached is None:
                    self.anonymous_failures += 1
                else:
                    self.anonymous_bytes += len(cached["body"])
                    try:
                        if self.cache.put(request.url, cached["body"], cached["headers"], anonymous=True):
                            self.cache_writes += 1
                    except Exception:
                        pass
            if cached is not None:
                try:
                    route.fulfill(status=200, body=cached["body"], headers=cached["headers"])
                except Exception:
                    pass
                else:
                    if cache_hit:
                        self.cache_hits += 1
                    return
        # No override of headers, cookies, method, URL or account identity.
        route.continue_()

def install_browser_data_reduction(context, *, cache=None, proxy=None):
    return BrowserDataReduction(context, cache, proxy)
