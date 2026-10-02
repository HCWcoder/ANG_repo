"""Login data reduction uses only synthetic browser objects and local files."""

import json
from hashlib import sha256
from types import ModuleType, SimpleNamespace
import sys

import pytest

from anghami_session import browser_data, capture
from anghami_session.errors import SessionError
from curl_cffi import CurlInfo
from curl_cffi.const import CurlOpt

JS_URL = "https://cdnweb.anghami.com/web/main.d33bcaad6c1084af.js"
CSS_URL = "https://cdnweb.anghami.com/web/styles.7723b922a8aafa9b.css"
SDK_URL = "https://www.gstatic.com/recaptcha/releases/kemdRjWFxNjgsGdhRslyEPwU/recaptcha__en.js"
PNG_URL = "https://cdnweb.anghami.com/web/assets/img/login-newest-background.png"
FONT_BASE = "https://cdnweb.anghami.com/web/assets/fonts/euclid/EuclidCircularA-Regular"
FONT_URL = FONT_BASE + ".woff2"
PUBLIC_BINARIES = (
    (PNG_URL, "image", "image/png"),
    ("https://cdnweb.anghami.com/web/assets/img/login-moving-circle.png", "image", "image/png"),
    (FONT_BASE + ".ttf", "font", "font/ttf"),
    (FONT_BASE + ".woff", "font", "font/woff"),
    (FONT_URL, "font", "font/woff2"),
    ("https://cdnweb.anghami.com/web/assets/fonts/tajawal/tajawal-700.woff2", "font", "font/woff2"),
)
PUBLIC_HEADERS = {"content-type": "text/javascript", "cache-control": "public, max-age=31104000", "vary": "Accept-Encoding"}


@pytest.fixture(autouse=True)
def no_live_asset_transport(monkeypatch):
    monkeypatch.setattr(browser_data.requests, "Session", lambda **kwargs: pytest.fail("An offline test attempted a live asset request"))


@pytest.mark.parametrize("url,kind", [
    ("https://play.anghami.com/assets/logo.svg", "image"),
    ("https://i.anghami.com/artwork/cover.webp", "image"),
    ("https://cdn.anghami.com/media/preview.mp3", "media"),
    ("https://cdnweb.anghami.com/web/assets/fonts/interface.woff2", "font"),
    ("https://fonts.gstatic.com/s/roboto/v30/ordinary.woff2", "font"),
    ("https://artwork.anghcdn.co/webp?id=synthetic-track&size=512", "image"),
    ("https://cdnweb.anghami.com/web/assets/img/login-newest-background.png", "image"),
    ("https://cdnweb.anghami.com/web/assets/img/login-moving-circle.png", "image"),
])
def test_ordinary_optional_assets_can_be_blocked(url, kind):
    assert browser_data.should_abort_asset(url, kind)


@pytest.mark.parametrize("url,kind", [
    ("https://play.anghami.com/assets/captcha/photo.png", "image"),
    ("https://play.anghami.com/assets/photo.png?type=verification", "image"),
    ("https://play.anghami.com/_fs-ch-1T1wmsGaOgGaSxcX/assets/photo.png", "image"),
    ("https://play.anghami.com/cdn-cgi/assets/opaque.png", "image"),
    ("https://play.anghami.com/assets/auth/photo.png", "image"),
    ("https://imgs.hcaptcha.com/assets/image.png", "image"),
    ("https://challenges.cloudflare.com/assets/image.png", "image"),
    ("https://fonts.gstatic.com/recaptcha/assets/security.woff2", "font"),
    ("https://unknown.example/assets/photo.png", "image"),
    ("https://artwork.anghcdn.co/captcha/image.png", "image"),
    ("https://other.anghcdn.co/webp?id=synthetic", "image"),
    ("https://artwork.anghcdn.co/opaque", "media"),
    ("https://cdnweb.anghami.com/web/assets/img/login-newest-background.png?type=challenge", "image"),
    ("https://play.anghami.com/opaque-server-response", "image"),
    ("data:image/png;base64,c3ludGhldGlj", "image"),
    ("blob:https://play.anghami.com/synthetic", "media"),
])
def test_security_and_unfamiliar_assets_are_preserved(url, kind):
    assert not browser_data.should_abort_asset(url, kind)


@pytest.mark.parametrize("kind", ["document", "script", "stylesheet", "xhr", "fetch", "websocket", "other"])
def test_essential_request_types_are_always_preserved(kind):
    assert not browser_data.should_abort_asset("https://play.anghami.com/assets/photo.png", kind)


@pytest.mark.parametrize("url", [
    "http://cdnweb.anghami.com/web/main.d33bcaad6c1084af.js",
    "https://cdnweb.anghami.com/web/main.js",
    JS_URL + "?sid=synthetic-private-session",
    JS_URL + "#synthetic",
    "https://synthetic:private@cdnweb.anghami.com/web/main.d33bcaad6c1084af.js",
    "https://cdnweb.anghami.com:444/web/main.d33bcaad6c1084af.js",
    "https://play.anghami.com/_fs-ch-1T1wmsGaOgGaSxcX/assets/script.js",
    "https://coussa.anghami.com/gateway.php?type=authenticate",
])
def test_only_verified_versioned_public_asset_urls_are_cacheable(url):
    assert browser_data.public_asset_kind(url) is None


def test_released_recaptcha_library_is_never_cacheable():
    assert browser_data.public_asset_kind(SDK_URL) is None
    assert browser_data.public_asset_kind(SDK_URL.replace("www.gstatic.com/", "www.gstatic.com:443/")) is None


@pytest.mark.parametrize("url", [
    SDK_URL.replace("https:", "http:"),
    SDK_URL.replace("www.gstatic.com", "gstatic.com"),
    SDK_URL.replace("www.gstatic.com", "other.gstatic.com"),
    SDK_URL.replace("www.gstatic.com", "www.gstatic.com.attacker.example"),
    SDK_URL.replace("www.gstatic.com/", "www.gstatic.com:444/"),
    SDK_URL.replace("www.gstatic.com/", "synthetic:private@www.gstatic.com/"),
    SDK_URL + "?", SDK_URL + "#", SDK_URL + "?key=synthetic", SDK_URL + "#synthetic",
    SDK_URL.replace("recaptcha__en.js", "recaptcha__fr.js"),
    SDK_URL.replace("recaptcha__en.js", "recaptcha__en.css"),
    SDK_URL.replace("kemdRjWFxNjgsGdhRslyEPwU", "short-release"),
    SDK_URL.replace("kemdRjWFxNjgsGdhRslyEPwU", "kemdRjWFxNjgsGdhRslyEPwUX"),
    SDK_URL.replace("kemdRjWFxNjgsGdhRslyEPwU", "kemdRjWFxNjgsGdhRslyEPw."),
    "https://www.google.com/recaptcha/api.js",
    "https://www.google.com/recaptcha/api2/anchor?key=synthetic",
    "https://www.google.com/recaptcha/api2/reload?key=synthetic",
    "https://www.google.com/recaptcha/api2/userverify",
    "https://www.gstatic.com/recaptcha/api2/challenge.png",
])
def test_sdk_scope_excludes_dynamic_or_unverified_security_urls(url):
    assert browser_data.public_asset_kind(url) is None
    assert not browser_data.should_abort_asset(url, "image")


@pytest.mark.parametrize("max_age,lifetime", [("31536000", 86400), ("90", 90), ("00090", 90)])
@pytest.mark.parametrize("url,mime", [(url, mime) for url, _, mime in PUBLIC_BINARIES])
def test_finite_public_cache_lifetime_respects_server_and_daily_cap(monkeypatch, tmp_path, max_age, lifetime, url, mime):
    now = [1_000.0]
    monkeypatch.setattr(browser_data.time, "time", lambda: now[0])
    cache = browser_data.PublicAssetCache(tmp_path)
    headers = {**PUBLIC_HEADERS, "content-type": mime, "cache-control": "public, max-age=" + max_age}
    assert cache.put(url, b"synthetic-public-asset", headers, anonymous=True)
    metadata = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert metadata["format_version"] == 2 and metadata["anonymous"] is True
    assert metadata["cached_at_unix"] == now[0]
    assert metadata["expires_at_unix"] == now[0] + lifetime
    assert cache.get(url) == {"body": b"synthetic-public-asset", "headers": headers}
    now[0] += lifetime
    assert cache.get(url) is None
    assert cache.put(JS_URL, b"synthetic-public-anghami", PUBLIC_HEADERS, anonymous=True)
    now[0] += 86400 * 10
    assert cache.get(JS_URL)["body"] == b"synthetic-public-anghami"


@pytest.mark.parametrize("control", [
    "public", "public, max-age=0", "public, max-age=-1", "public, max-age=abc",
    "public, max-age=1.5", 'public, max-age="90"',
    "public, max-age=90, max-age=100", "public, max-age=90, max-age=90",
])
def test_public_cosmetics_require_one_positive_server_max_age(monkeypatch, tmp_path, control):
    headers = {**PUBLIC_HEADERS, "content-type": "image/png", "cache-control": control}
    cache = browser_data.PublicAssetCache(tmp_path)
    assert not cache.put(PNG_URL, b"synthetic-public-cosmetic", headers, anonymous=True)
    calls = fake_asset_transport(monkeypatch, headers=headers)
    assert browser_data.load_anonymous_public_asset(PNG_URL) is None
    assert calls["chunks_read"] == 0 and calls["session_closed"] == 1


@pytest.mark.parametrize("corruption", [
    {"cached_at_unix": None}, {"expires_at_unix": None},
    {"cached_at_unix": True}, {"expires_at_unix": "100090"},
    {"cached_at_unix": float("nan")}, {"expires_at_unix": float("inf")},
    {"cached_at_unix": 10 ** 400},
    {"cached_at_unix": -1}, {"expires_at_unix": 100091},
    {"cached_at_unix": 100001, "expires_at_unix": 100091},
    {"headers": {**PUBLIC_HEADERS, "cache-control": "public, max-age=90, max-age=100"}},
])
@pytest.mark.parametrize("url,mime", [(PNG_URL, "image/png"), (FONT_URL, "font/woff2")])
def test_finite_public_malformed_or_future_expiration_metadata_is_a_miss(monkeypatch, tmp_path, corruption, url, mime):
    monkeypatch.setattr(browser_data.time, "time", lambda: 100000.0)
    cache = browser_data.PublicAssetCache(tmp_path)
    headers = {**PUBLIC_HEADERS, "content-type": mime, "cache-control": "public, max-age=90"}
    assert cache.put(url, b"synthetic-public-asset", headers, anonymous=True)
    path = next(tmp_path.glob("*.json"))
    metadata = json.loads(path.read_text())
    metadata.update(corruption)
    path.write_text(json.dumps(metadata))
    assert cache.get(url) is None


@pytest.mark.parametrize("filename", [
    "euclid/EuclidCircularA-Bold.ttf", "euclid/EuclidCircularA-Bold.woff", "euclid/EuclidCircularA-Bold.woff2",
    "euclid/EuclidCircularA-Light.ttf", "euclid/EuclidCircularA-Light.woff", "euclid/EuclidCircularA-Light.woff2",
    "euclid/EuclidCircularA-LightItalic.ttf", "euclid/EuclidCircularA-LightItalic.woff", "euclid/EuclidCircularA-LightItalic.woff2",
    "euclid/EuclidCircularA-Medium.ttf", "euclid/EuclidCircularA-Medium.woff", "euclid/EuclidCircularA-Medium.woff2",
    "euclid/EuclidCircularA-Regular.ttf", "euclid/EuclidCircularA-Regular.woff", "euclid/EuclidCircularA-Regular.woff2",
    "euclid/EuclidCircularA-SemiBold.ttf", "euclid/EuclidCircularA-SemiBold.woff", "euclid/EuclidCircularA-SemiBold.woff2",
    "tajawal/tajawal-700.woff2", "tajawal/tajawal-latin-700.woff2",
])
def test_exact_twenty_verified_public_font_paths(filename, tmp_path):
    url = "https://cdnweb.anghami.com/web/assets/fonts/" + filename
    assert browser_data.public_asset_kind(url) == "font"
    headers = {**PUBLIC_HEADERS, "content-type": "font/" + filename.rsplit(".", 1)[1]}
    cache = browser_data.PublicAssetCache(tmp_path)
    assert cache.put(url, b"synthetic-public-font", headers, anonymous=True)
    assert cache.get(url)["body"] == b"synthetic-public-font"


@pytest.mark.parametrize("url", [
    PNG_URL.replace("https:", "http:"), PNG_URL.replace("cdnweb.anghami.com", "cdn.anghami.com"),
    PNG_URL.replace("cdnweb.anghami.com/", "cdnweb.anghami.com:444/"),
    PNG_URL.replace("cdnweb.anghami.com/", "synthetic:private@cdnweb.anghami.com/"),
    PNG_URL + "?", PNG_URL + "#", PNG_URL + "?sid=synthetic", PNG_URL + "#synthetic",
    PNG_URL.replace("login-newest-background.png", "other-background.png"),
    PNG_URL.replace("login-newest-background.png", "login-newest-background.jpg"),
    FONT_URL + "?", FONT_URL + "#", FONT_URL + "?sid=synthetic", FONT_URL + "#synthetic",
    FONT_URL.replace("cdnweb.anghami.com", "other.gstatic.com"),
    FONT_URL.replace("Regular", "Thin"), FONT_URL.replace("Regular", "regular"),
    FONT_URL.replace(".woff2", ".otf"), FONT_URL.replace("euclid/", "euclid/challenge/"),
    FONT_URL.replace("euclid/", "other/"),
])
def test_public_binary_scope_does_not_expand_to_unknown_urls(url):
    assert browser_data.public_asset_kind(url) is None


@pytest.mark.parametrize("url,kind,mime", PUBLIC_BINARIES)
@pytest.mark.parametrize("bad_mime", ["application/octet-stream", "text/javascript", "image/jpeg", "font/otf"])
def test_public_binary_mime_is_exact(tmp_path, url, kind, mime, bad_mime):
    cache = browser_data.PublicAssetCache(tmp_path)
    assert not cache.put(url, b"synthetic-public-binary", {**PUBLIC_HEADERS, "content-type": bad_mime}, anonymous=True)
    if kind == "font":
        mismatched = "font/woff" if mime != "font/woff" else "font/woff2"
        assert not cache.put(url, b"synthetic-public-font", {**PUBLIC_HEADERS, "content-type": mismatched}, anonymous=True)


@pytest.mark.parametrize("url,kind,mime", PUBLIC_BINARIES)
@pytest.mark.parametrize("bad_headers", [
    {"set-cookie": "synthetic-private-cookie"}, {"authorization": "synthetic-private-token"},
    {"proxy-authorization": "synthetic-private-proxy"}, {"vary": "Cookie"},
    {"cache-control": "public"}, {"cache-control": "public, max-age=0"},
    {"cache-control": "private, max-age=90"},
])
def test_public_binary_private_or_unbounded_responses_are_rejected(monkeypatch, tmp_path, url, kind, mime, bad_headers):
    headers = {**PUBLIC_HEADERS, "content-type": mime, **bad_headers}
    cache = browser_data.PublicAssetCache(tmp_path)
    assert not cache.put(url, b"synthetic-public-binary", headers, anonymous=True)
    calls = fake_asset_transport(monkeypatch, headers=headers)
    assert browser_data.load_anonymous_public_asset(url) is None
    assert calls["chunks_read"] == 0 and calls["session_closed"] == 1


@pytest.mark.parametrize("url,kind,mime", PUBLIC_BINARIES)
@pytest.mark.parametrize("request_kind", ["fetch", "xhr", "other"])
def test_worker_public_binaries_use_anonymous_cache_without_account_state(monkeypatch, tmp_path, url, kind, mime, request_kind):
    headers = {**PUBLIC_HEADERS, "content-type": mime}
    calls = fake_asset_transport(monkeypatch, headers=headers, chunks=(b"synthetic-public-binary",))
    cache = browser_data.PublicAssetCache(tmp_path)
    context = FakeContext()
    controller = browser_data.install_browser_data_reduction(context, cache=cache)
    account_headers = {"cookie": "synthetic-private-cookie", "x-angh-session": "synthetic-private-sid"}
    route = FakeRoute(url, request_kind, headers=account_headers)
    context.routes[0][1](route)
    assert route.calls == [("fulfill", {"status": 200, "body": b"synthetic-public-binary", "headers": headers})]
    assert calls["get"] == [(url, {"timeout": 10, "allow_redirects": False, "stream": True})]
    assert controller.cache_writes == controller.anonymous_requests == 1
    assert "synthetic-private" not in str(json.loads(next(tmp_path.glob("*.json")).read_text()))
    warm = FakeRoute(url, request_kind, headers={"cookie": "synthetic-other-account"})
    context.routes[0][1](warm)
    assert warm.calls[0][0] == "fulfill" and controller.cache_hits == 1
    assert len(calls["get"]) == 1 and route.request.all_headers() == account_headers


@pytest.mark.parametrize("url,kind,mime", PUBLIC_BINARIES)
def test_native_public_images_and_fonts_still_abort_before_cache(tmp_path, url, kind, mime):
    cache = browser_data.PublicAssetCache(tmp_path)
    assert cache.put(url, b"synthetic-public-binary", {**PUBLIC_HEADERS, "content-type": mime}, anonymous=True)
    context = FakeContext()
    controller = browser_data.install_browser_data_reduction(context, cache=cache)
    route = FakeRoute(url, kind)
    context.routes[0][1](route)
    assert route.calls == [("abort", {})]
    assert controller.blocked_requests == 1
    assert controller.cache_hits == controller.anonymous_requests == 0


@pytest.mark.parametrize("url,expected", [(url, kind) for url, kind, _ in PUBLIC_BINARIES])
@pytest.mark.parametrize("kind", ["document", "script", "stylesheet", "image", "font", "media"])
def test_binary_cache_mismatched_resource_types_are_excluded(url, expected, kind):
    request = FakeRoute(url, kind).request
    assert browser_data.BrowserDataReduction._eligible_request(request) is (kind == expected)


@pytest.mark.parametrize("url", [JS_URL])
def test_public_asset_cache_roundtrip_and_integrity(tmp_path, url):
    cache = browser_data.PublicAssetCache(tmp_path)
    assert cache.put(url, b"synthetic-public-script", {
        **PUBLIC_HEADERS, "content-encoding": "gzip", "content-length": "1",
        "x-unrecognized-header": "synthetic-private-header",
    }, anonymous=True)
    cached = cache.get(url)
    assert cached == {"body": b"synthetic-public-script", "headers": PUBLIC_HEADERS}
    metadata = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert "synthetic-private-header" not in str(metadata)
    next(tmp_path.glob("*.body")).write_bytes(b"tampered-public-body")
    assert cache.get(url) is None


@pytest.mark.parametrize("headers", [
    {**PUBLIC_HEADERS, "set-cookie": "synthetic-private-cookie"},
    {**PUBLIC_HEADERS, "authorization": "synthetic-private-token"},
    {**PUBLIC_HEADERS, "cache-control": "public, private"},
    {**PUBLIC_HEADERS, "cache-control": "public, no-store"},
    {**PUBLIC_HEADERS, "cache-control": "public, no-cache"},
    {**PUBLIC_HEADERS, "cache-control": "max-age=100"},
    {**PUBLIC_HEADERS, "vary": "Cookie"},
    {**PUBLIC_HEADERS, "vary": "Authorization"},
    {**PUBLIC_HEADERS, "vary": "*"},
    {**PUBLIC_HEADERS, "content-type": "application/json"},
])
@pytest.mark.parametrize("url", [JS_URL])
def test_private_or_incompatible_responses_never_enter_cache(tmp_path, headers, url):
    cache = browser_data.PublicAssetCache(tmp_path)
    assert not cache.put(url, b"synthetic", headers, anonymous=True)
    assert list(tmp_path.iterdir()) == []


def test_file_and_total_bounds_evict_old_public_entries(tmp_path):
    cache = browser_data.PublicAssetCache(tmp_path, max_asset_bytes=8, max_total_bytes=12)
    assert not cache.put(JS_URL, b"x" * 9, PUBLIC_HEADERS, anonymous=True)
    assert cache.put(JS_URL, b"first123", PUBLIC_HEADERS, anonymous=True)
    css_headers = {**PUBLIC_HEADERS, "content-type": "text/css"}
    assert cache.put(CSS_URL, b"second12", css_headers, anonymous=True)
    assert cache.get(JS_URL) is None
    assert cache.get(CSS_URL)["body"] == b"second12"
    assert sum(path.stat().st_size for path in tmp_path.glob("*.body")) <= 12


def test_cache_read_and_write_failure_are_normal_misses(tmp_path):
    occupied = tmp_path / "not-a-directory"
    occupied.write_text("synthetic")
    cache = browser_data.PublicAssetCache(occupied)
    assert cache.get(JS_URL) is None
    assert not cache.put(JS_URL, b"synthetic", PUBLIC_HEADERS, anonymous=True)


class FakeContext:
    def __init__(self):
        self.routes = []
        self.events = {}

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def on(self, event, handler):
        self.events.setdefault(event, []).append(handler)


class FakeRoute:
    def __init__(self, url, kind, *, headers=None):
        self.calls = []
        self.request = SimpleNamespace(url=url, resource_type=kind, method="GET", all_headers=lambda: headers or {})

    def continue_(self, **kwargs):
        self.calls.append(("continue", kwargs))

    def abort(self):
        self.calls.append(("abort", {}))

    def fulfill(self, **kwargs):
        self.calls.append(("fulfill", kwargs))


@pytest.mark.parametrize("url", [
    SDK_URL, SDK_URL.replace("www.gstatic.com/", "www.gstatic.com:443/"),
    "https://www.google.com/recaptcha/api.js",
    "https://www.google.com/recaptcha/api2/anchor?key=synthetic",
    "https://www.google.com/recaptcha/api2/reload?key=synthetic",
    "https://www.google.com/recaptcha/api2/userverify",
    "https://www.gstatic.com/recaptcha/api2/challenge.png",
    "https://fonts.gstatic.com/recaptcha/assets/security.woff2",
    "https://imgs.hcaptcha.com/assets/image.png",
    "https://challenges.cloudflare.com/assets/image.png",
])
@pytest.mark.parametrize("kind", ["script", "fetch", "xhr", "other", "image", "font"])
def test_security_resources_continue_natively_without_cache_or_anonymous_loader(monkeypatch, tmp_path, url, kind):
    def forbidden(*_args, **_options):
        pytest.fail("Security resource used the public asset cache or anonymous loader")

    cache = browser_data.PublicAssetCache(tmp_path)
    monkeypatch.setattr(cache, "get", forbidden)
    monkeypatch.setattr(cache, "put", forbidden)
    monkeypatch.setattr(browser_data, "load_anonymous_public_asset", forbidden)
    context = FakeContext()
    controller = browser_data.install_browser_data_reduction(context, cache=cache)
    headers = {"cookie": "synthetic-private-cookie", "user-agent": "synthetic-browser-agent"}
    route = FakeRoute(url, kind, headers=headers)
    if url.endswith("/userverify"):
        route.request.method = "POST"
    method = route.request.method
    context.routes[0][1](route)
    assert route.calls == [("continue", {})]
    assert route.request.url == url and route.request.method == method
    assert route.request.all_headers() == headers
    assert controller.blocked_requests == controller.cache_hits == controller.cache_writes == 0
    assert controller.anonymous_requests == controller.anonymous_bytes == 0
    assert list(tmp_path.iterdir()) == []


def test_existing_anonymous_v2_recaptcha_cache_is_ignored_before_file_access(monkeypatch, tmp_path):
    cache = browser_data.PublicAssetCache(tmp_path)
    body = b"synthetic-old-public-recaptcha-library"
    body_path, metadata_path = cache._paths(SDK_URL)
    body_path.write_bytes(body)
    metadata_path.write_text(json.dumps({
        "format_version": 2, "anonymous": True, "url": SDK_URL,
        "body_length": len(body), "body_sha256": sha256(body).hexdigest(),
        "headers": PUBLIC_HEADERS, "cached_at_unix": 1000.0, "expires_at_unix": 87400.0,
    }), encoding="utf-8")
    monkeypatch.setattr(browser_data.time, "time", lambda: 1000.0)
    monkeypatch.setattr(cache, "_paths", lambda *_args: pytest.fail("Old reCAPTCHA cache entry was accessed"))
    assert cache.get(SDK_URL) is None
    assert not cache.put(SDK_URL, body, PUBLIC_HEADERS, anonymous=True)
    assert browser_data.load_anonymous_public_asset(SDK_URL) is None
    assert body_path.read_bytes() == body and metadata_path.exists()


def test_routing_cache_hits_and_misses_without_changing_account_headers(tmp_path):
    context = FakeContext()
    cache = browser_data.PublicAssetCache(tmp_path)
    cache.put(JS_URL, b"public-js", PUBLIC_HEADERS, anonymous=True)
    controller = browser_data.install_browser_data_reduction(context, cache=cache)
    handler = context.routes[0][1]
    assert context.routes[0][0] == "**/*"
    public = FakeRoute(JS_URL, "script")
    handler(public)
    assert public.calls == [("fulfill", {"status": 200, "body": b"public-js", "headers": PUBLIC_HEADERS})]
    assert controller.cache_hits == 1
    cookie_bound = FakeRoute(JS_URL, "script", headers={"cookie": "synthetic-private-cookie"})
    handler(cookie_bound)
    assert cookie_bound.calls[0][0] == "fulfill"
    assert cookie_bound.request.all_headers() == {"cookie": "synthetic-private-cookie"}
    for headers in ({"authorization": "synthetic-private-token"}, {"proxy-authorization": "synthetic-private-proxy"}):
        bound = FakeRoute(JS_URL, "script", headers=headers)
        handler(bound)
        assert bound.calls == [("continue", {})]
        assert bound.request.all_headers() == headers
    gateway = FakeRoute("https://coussa.anghami.com/gateway.php?sid=synthetic", "xhr", headers={"cookie": "synthetic-private-cookie"})
    handler(gateway)
    assert gateway.calls == [("continue", {})]
    security = FakeRoute("https://play.anghami.com/_fs-ch-1T1wmsGaOgGaSxcX/assets/script.js", "script")
    handler(security)
    assert security.calls == [("continue", {})]


def test_cache_and_anonymous_loader_failure_continue_unchanged_request(monkeypatch, tmp_path):
    context = FakeContext()
    cache = browser_data.PublicAssetCache(tmp_path)
    controller = browser_data.install_browser_data_reduction(context, cache=cache)
    route = FakeRoute(JS_URL, "script")
    cache.get = lambda url: (_ for _ in ()).throw(OSError("synthetic-private-error"))
    monkeypatch.setattr(browser_data, "load_anonymous_public_asset", lambda *args, **kwargs: None)
    context.routes[0][1](route)
    assert route.calls == [("continue", {})]
    assert controller.anonymous_requests == controller.anonymous_failures == 1
    assert context.events == {}


@pytest.mark.parametrize("url", [JS_URL])
def test_failed_cache_fulfillment_falls_back_to_unchanged_request(tmp_path, url):
    context = FakeContext()
    cache = browser_data.PublicAssetCache(tmp_path)
    assert cache.put(url, b"public-js", PUBLIC_HEADERS, anonymous=True)
    controller = browser_data.install_browser_data_reduction(context, cache=cache)
    route = FakeRoute(url, "script", headers={"accept": "synthetic-accept"})

    def failed_fulfill(**kwargs):
        raise OSError("synthetic-cache-fulfillment-failure")

    route.fulfill = failed_fulfill
    context.routes[0][1](route)
    assert route.calls == [("continue", {})]
    assert route.request.all_headers() == {"accept": "synthetic-accept"}
    assert controller.cache_hits == 0


@pytest.mark.parametrize("url,mime", [(PNG_URL, "image/png"), (FONT_URL, "font/woff2")])
def test_expired_public_cache_and_anonymous_failure_fall_back_unchanged(monkeypatch, tmp_path, url, mime):
    now = [1_000.0]
    monkeypatch.setattr(browser_data.time, "time", lambda: now[0])
    cache = browser_data.PublicAssetCache(tmp_path)
    assert cache.put(url, b"public-asset", {**PUBLIC_HEADERS, "content-type": mime, "cache-control": "public, max-age=90"}, anonymous=True)
    now[0] += 90
    calls = fake_asset_transport(monkeypatch, get_error=OSError("synthetic-public-load-failure"))
    context = FakeContext()
    controller = browser_data.install_browser_data_reduction(context, cache=cache)
    headers = {"cookie": "synthetic-private-cookie", "x-angh-session": "synthetic-private-sid"}
    route = FakeRoute(url, "fetch", headers=headers)
    context.routes[0][1](route)
    assert route.calls == [("continue", {})]
    assert route.request.all_headers() == headers
    assert controller.anonymous_requests == controller.anonymous_failures == 1
    assert controller.cache_hits == 0 and calls["session_closed"] == 1


def fake_asset_transport(monkeypatch, *, status=200, headers=None, chunks=(b"public-js",), infos=None, get_error=None, stream_error=None):
    calls = {"options": [], "get": [], "session_closed": 0, "response_closed": 0, "chunks_read": 0}

    def contents():
        for chunk in chunks:
            calls["chunks_read"] += 1
            yield chunk
        if stream_error is not None:
            raise stream_error

    response = SimpleNamespace(
        status_code=status, headers=PUBLIC_HEADERS if headers is None else headers,
        infos={} if infos is None else infos, iter_content=contents,
        close=lambda: calls.__setitem__("response_closed", calls["response_closed"] + 1),
    )

    class Session:
        def __init__(self, **options):
            calls["options"].append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            calls["session_closed"] += 1

        def get(self, url, **options):
            calls["get"].append((url, options))
            if get_error is not None:
                raise get_error
            return response

    monkeypatch.setattr(browser_data.requests, "Session", Session)
    return calls


@pytest.mark.parametrize("kind", ["script", "fetch", "xhr", "other"])
@pytest.mark.parametrize("url", [JS_URL])
def test_anonymous_miss_populates_v2_cache_without_browser_credentials(monkeypatch, tmp_path, kind, url):
    calls = fake_asset_transport(monkeypatch)
    context = FakeContext()
    cache = browser_data.PublicAssetCache(tmp_path)
    controller = browser_data.install_browser_data_reduction(context, cache=cache)
    route = FakeRoute(url, kind, headers={"cookie": "synthetic-private-cookie", "x-angh-session": "synthetic-private-sid"})
    context.routes[0][1](route)
    assert route.calls == [("fulfill", {"status": 200, "body": b"public-js", "headers": PUBLIC_HEADERS})]
    assert calls["get"] == [(url, {"timeout": 10, "allow_redirects": False, "stream": True})]
    assert calls["options"] == [{"impersonate": "chrome", "retry": 0, "verify": True, "debug": False, "curl_options": {CurlOpt.PROXY: "", CurlOpt.NOPROXY: "*", CurlOpt.TIMEOUT_MS: 10_000}}]
    assert calls["session_closed"] == calls["response_closed"] == 1
    assert controller.anonymous_bytes == len(b"public-js")
    assert controller.cache_writes == controller.anonymous_requests == 1
    assert cache.get(url)["body"] == b"public-js"
    metadata = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert metadata["format_version"] == 2 and metadata["anonymous"] is True
    assert "synthetic-private" not in str(metadata)
    warm = FakeRoute(url, kind, headers={"cookie": "synthetic-other-account"})
    context.routes[0][1](warm)
    assert warm.calls[0][0] == "fulfill" and controller.cache_hits == 1
    assert len(calls["get"]) == 1


@pytest.mark.parametrize("url,expected", [(JS_URL, "script"), (CSS_URL, "stylesheet")])
@pytest.mark.parametrize("kind", ["script", "stylesheet", "fetch", "xhr", "other", "image", "font", "media", "document"])
def test_exact_public_asset_request_types(url, expected, kind):
    route = FakeRoute(url, kind, headers={"cookie": "synthetic-private-cookie"})
    assert browser_data.BrowserDataReduction._eligible_request(route.request) is (
        kind in {expected, "fetch", "xhr", "other"}
    )


@pytest.mark.parametrize("kind", ["fetch", "xhr", "other"])
@pytest.mark.parametrize("reason", ["post", "authorization", "proxy_authorization", "private_url"])
@pytest.mark.parametrize("asset_url", [JS_URL, SDK_URL])
def test_precache_requests_keep_method_url_and_authorization_guards(monkeypatch, tmp_path, kind, reason, asset_url):
    headers = {"cookie": "synthetic-private-cookie"}
    if reason == "authorization":
        headers["Authorization"] = "synthetic-private-token"
    elif reason == "proxy_authorization":
        headers["Proxy-Authorization"] = "synthetic-private-proxy"
    url = asset_url if reason != "private_url" else "https://coussa.anghami.com/gateway.php?sid=synthetic-private-sid"
    route = FakeRoute(url, kind, headers=headers)
    if reason == "post":
        route.request.method = "POST"
    context = FakeContext()
    controller = browser_data.install_browser_data_reduction(context, cache=browser_data.PublicAssetCache(tmp_path))
    monkeypatch.setattr(browser_data, "load_anonymous_public_asset", lambda *args, **kwargs: pytest.fail("ineligible request fetched anonymously"))
    context.routes[0][1](route)
    assert route.calls == [("continue", {})]
    assert route.request.all_headers() == headers
    assert controller.anonymous_requests == controller.cache_hits == 0


@pytest.mark.parametrize("url,mime", [(JS_URL, "text/javascript")] + [(url, mime) for url, _, mime in PUBLIC_BINARIES])
def test_anonymous_loader_preserves_same_proxy_route_and_sticky_auth(monkeypatch, url, mime):
    options = {"impersonate": "chrome", "proxy": "http://127.0.0.1:12345", "proxy_auth": ("synthetic-provider", "synthetic-eg-sticky"), "retry": 0, "verify": True, "debug": False, "curl_options": {CurlOpt.NOPROXY: ""}}
    calls = fake_asset_transport(monkeypatch, headers={**PUBLIC_HEADERS, "content-type": mime}, infos={CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 200})
    proxy = SimpleNamespace(transport_options=lambda: dict(options))
    assert browser_data.load_anonymous_public_asset(url, proxy=proxy)["body"] == b"public-js"
    assert calls["options"] == [{**options, "curl_options": {CurlOpt.NOPROXY: "", CurlOpt.TIMEOUT_MS: 10_000}}]
    assert options["curl_options"] == {CurlOpt.NOPROXY: ""}
    assert calls["get"][0][1] == {"timeout": 10, "allow_redirects": False, "stream": True}
    assert calls["session_closed"] == calls["response_closed"] == 1


@pytest.mark.parametrize("status,headers", [
    (301, PUBLIC_HEADERS), (401, PUBLIC_HEADERS), (403, PUBLIC_HEADERS),
    (200, {**PUBLIC_HEADERS, "set-cookie": "synthetic-private-cookie"}),
    (200, {**PUBLIC_HEADERS, "cache-control": "private"}),
    (200, {**PUBLIC_HEADERS, "vary": "Cookie"}),
    (200, {**PUBLIC_HEADERS, "content-type": "application/json"}),
])
def test_rejected_anonymous_responses_are_closed_without_reading_body(monkeypatch, status, headers):
    calls = fake_asset_transport(monkeypatch, status=status, headers=headers)
    assert browser_data.load_anonymous_public_asset(JS_URL) is None
    assert calls["chunks_read"] == 0
    assert calls["session_closed"] == calls["response_closed"] == 1


@pytest.mark.parametrize("infos", [{}, {CurlInfo.USED_PROXY: 0, CurlInfo.HTTP_CONNECTCODE: 200}, {CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 407}, {CurlInfo.USED_PROXY: True, CurlInfo.HTTP_CONNECTCODE: 200}])
def test_anonymous_proxy_download_requires_confirmed_tunnel(monkeypatch, infos):
    calls = fake_asset_transport(monkeypatch, infos=infos)
    proxy = SimpleNamespace(transport_options=lambda: {"proxy": "http://127.0.0.1:12345"})
    assert browser_data.load_anonymous_public_asset(JS_URL, proxy=proxy) is None
    assert calls["chunks_read"] == 0
    assert calls["session_closed"] == calls["response_closed"] == 1


@pytest.mark.parametrize("case", ["declared_size", "stream_size", "stream_error", "get_error"])
def test_anonymous_download_limits_and_failures_close_transport(monkeypatch, case):
    calls = fake_asset_transport(
        monkeypatch, headers={**PUBLIC_HEADERS, **({"content-length": "100"} if case == "declared_size" else {})},
        chunks=(b"public-js",),
        stream_error=OSError("synthetic-private-error") if case == "stream_error" else None,
        get_error=OSError("synthetic-private-error") if case == "get_error" else None,
    )
    assert browser_data.load_anonymous_public_asset(JS_URL, max_bytes=8 if case in {"declared_size", "stream_size"} else 100) is None
    assert calls["session_closed"] == 1
    assert calls["response_closed"] == (0 if case == "get_error" else 1)


def test_unverified_urls_do_not_open_anonymous_transport():
    assert browser_data.load_anonymous_public_asset("https://coussa.anghami.com/gateway.php?sid=synthetic") is None
    assert browser_data.load_anonymous_public_asset(JS_URL + "?sid=synthetic") is None


def test_cache_requires_anonymous_v2_provenance(tmp_path):
    cache = browser_data.PublicAssetCache(tmp_path)
    assert not cache.put(JS_URL, b"public-js", PUBLIC_HEADERS)
    assert cache.put(JS_URL, b"public-js", PUBLIC_HEADERS, anonymous=True)
    path = next(tmp_path.glob("*.json"))
    metadata = json.loads(path.read_text())
    metadata["format_version"] = 1
    path.write_text(json.dumps(metadata))
    assert cache.get(JS_URL) is None


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("use_proxy", [False, True])
def test_capture_flag_preserves_session_identity_and_default_behavior(monkeypatch, tmp_path, enabled, use_proxy):
    class BrowserError(RuntimeError):
        pass
    api = ModuleType("playwright.sync_api")
    api.Error = BrowserError
    api.TimeoutError = type("BrowserTimeout", (BrowserError,), {})
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    context = FakeContext()
    context_options = []
    waits = []
    closed = []
    cache = browser_data.PublicAssetCache(tmp_path)
    original_install = browser_data.install_browser_data_reduction
    installed = []

    def install(current, **options):
        installed.append(options)
        return original_install(current, cache=cache, **options)

    monkeypatch.setattr(browser_data, "install_browser_data_reduction", install)
    proxy_options = {"server": "http://127.0.0.1:12345", "username": "synthetic-provider", "password": "synthetic-eg-sticky"}
    proxy = SimpleNamespace(verify_country=lambda: {"country_verified": True}, browser_options=lambda: proxy_options) if use_proxy else None
    monkeypatch.setattr(capture, "_verify_browser_country", lambda current: None)
    locator = SimpleNamespace(wait_for=lambda **kwargs: None, click=lambda: None, fill=lambda value: None)

    def wait_for_url(url, **options):
        waits.append((url, options))
        for operation in capture.OPERATIONS.values():
            request = SimpleNamespace(
                url=capture.GATEWAY_URL + "?type=" + operation + "&sid=synthetic-sid&appsid=synthetic-sid&fingerprint=synthetic-fingerprint",
                method="GET", resource_type="xhr",
                all_headers=lambda: {"cookie": "synthetic-cookie", "user-agent": "synthetic-agent"},
            )
            response = SimpleNamespace(request=request, status=200, json=lambda: {"status": "ok"})
            for callback in context.events.get("response", []):
                callback(response)

    page = SimpleNamespace(
        goto=lambda *args, **kwargs: None, get_by_role=lambda *args, **kwargs: locator,
        get_by_text=lambda *args, **kwargs: locator, get_by_placeholder=lambda *args, **kwargs: locator,
        wait_for_url=wait_for_url, wait_for_timeout=lambda *args: pytest.fail("Required sessions were captured"),
    )
    context.new_page = lambda: page
    browser = SimpleNamespace(new_context=lambda **options: context_options.append(options) or context, close=lambda: closed.append(True))
    monkeypatch.setattr(capture, "launch_browser", lambda **kwargs: browser)
    saved, _ = capture.capture_login(email="Synthetic@Example.invalid", password="synthetic-password", browser_backend="chrome", headless=True, reduce_browser_data=enabled, proxy=proxy)
    assert saved["account_email"] == "synthetic@example.invalid"
    assert set(saved["requests"]) == set(capture.OPERATIONS)
    for template in saved["requests"].values():
        assert template["headers"] == {"cookie": "synthetic-cookie", "user-agent": "synthetic-agent"}
        assert "sid=synthetic-sid" in template["url"] and "fingerprint=synthetic-fingerprint" in template["url"]
    assert context_options == [{"no_viewport": True, **({"proxy": proxy_options} if use_proxy else {})}]
    assert installed == ([{"proxy": proxy}] if enabled else [])
    assert closed == [True]
    assert waits == [("**/home", {"timeout": 45000, **({"wait_until": "domcontentloaded"} if enabled else {})})]
    assert bool(context.routes) is enabled


@pytest.mark.parametrize("value", [None, 1, "true", []])
def test_capture_rejects_nonboolean_flag_before_browser(monkeypatch, value):
    monkeypatch.setattr(capture, "launch_browser", lambda **kwargs: pytest.fail("Invalid flag opened a browser"))
    with pytest.raises(SessionError, match="reduce browser data"):
        capture.capture_login(reduce_browser_data=value)
