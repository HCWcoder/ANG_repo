"""HTTP byte accounting uses synthetic transports and a loopback server only."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from gzip import compress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Barrier, Thread
from types import SimpleNamespace

from curl_cffi import requests
from curl_cffi.const import CurlInfo, CurlOpt
from curl_cffi.curl import CurlError
import pytest

from anghami_session import client, like_test, play_record, proxy, session_recovery
from anghami_session.bandwidth import (
    BANDWIDTH_CURL_INFOS, MAX_COUNTER, bandwidth_transport_options,
    measure_network_usage, measured_request,
)


SECRET = "synthetic-private-session-cookie-password-route"
URL = "https://private.invalid/?sid=" + SECRET


class Reply:
    def __init__(self, payload=None, *, request=1200, upload=300, download=80, headers=100, proxied=0):
        self.payload = payload or {"status": "ok"}
        self.infos = {
            CurlInfo.REQUEST_SIZE: request, CurlInfo.SIZE_UPLOAD_T: upload,
            CurlInfo.SIZE_DOWNLOAD_T: download, CurlInfo.HEADER_SIZE: headers,
            CurlInfo.USED_PROXY: proxied, CurlInfo.HTTP_CONNECTCODE: 200 if proxied else 0,
            "private-extra": SECRET,
        }
        self.status_code = 200
        self.headers = {"set-cookie": SECRET}
        self.content = SECRET.encode()

    def json(self):
        return deepcopy(self.payload)

    def close(self):
        pass


class Transport:
    def __init__(self, replies):
        self.replies = replies
        self.proxies = {}
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    post = get


def assert_private(value):
    encoded = json.dumps(value, allow_nan=False)
    assert SECRET not in encoded and URL not in encoded
    assert "set-cookie" not in encoded and "private-extra" not in encoded


def test_successful_post_body_is_counted_once_and_route_is_numeric():
    transport = Transport([Reply(proxied=1)])
    with measure_network_usage() as meter:
        measured_request(transport, "post", URL, data=SECRET, headers={"cookie": SECRET})
    result = meter.snapshot()
    assert result["request_count"] == result["measured_requests"] == 1
    assert result["request_bytes"] == result["sent_bytes"] == 1200
    assert result["upload_body_bytes"] == 300
    assert result["request_header_bytes"] == 900
    assert result["received_bytes"] == 180 and result["total_bytes"] == 1380
    assert result["proxy_bytes"] == 1380 and result["direct_bytes"] == 0
    assert result["measurement"] == "measured" and result["scope"] == "python_http"
    assert_private(result)


def test_failed_transfer_uses_attached_response_and_preserves_exception():
    failure = CurlError(SECRET, 28)
    failure.response = Reply(request=420, upload=20, download=35, headers=80)
    transport = Transport([failure])
    with measure_network_usage() as meter:
        with pytest.raises(CurlError) as caught:
            measured_request(transport, "get", URL)
    assert caught.value is failure and transport.calls == 1
    result = meter.snapshot()
    assert result["request_count"] == result["measured_requests"] == result["transport_errors"] == 1
    assert result["total_bytes"] == 535 and result["measurement"] == "measured"
    assert_private(result)


def test_missing_error_response_does_not_read_reset_curl_or_claim_zero_coverage():
    failure = CurlError(SECRET, 7)
    transport = Transport([failure])
    transport.curl = SimpleNamespace(getinfo=lambda *_: pytest.fail("A reset handle was queried"))
    with measure_network_usage() as meter:
        with pytest.raises(CurlError):
            measured_request(transport, "get", URL)
    result = meter.snapshot()
    assert result["unmeasured_requests"] == result["transport_errors"] == 1
    assert result["total_bytes"] == 0 and result["measurement"] == "unavailable"
    assert_private(result)


@pytest.mark.parametrize("value", [None, True, False, "120", SECRET, -1, 2.5, float("nan"), float("inf"), MAX_COUNTER + 1])
def test_invalid_numeric_stat_marks_partial_and_keeps_known_bytes(value):
    reply = Reply(request=value, upload=10, download=30, headers=40)
    transport = Transport([reply])
    with measure_network_usage() as meter:
        measured_request(transport, "post", URL, data=SECRET)
    result = meter.snapshot()
    assert result["partial_requests"] == 1 and result["measured_requests"] == 0
    assert result["measurement"] == "partial"
    assert result["sent_bytes"] == 10 and result["total_bytes"] == 80
    assert_private(result)


def test_native_response_attributes_cover_sessions_created_outside_meter():
    response = SimpleNamespace(request_size=850, upload_size=250, download_size=10, header_size=40)
    with measure_network_usage() as meter:
        measured_request(Transport([response]), "post", URL)
    result = meter.snapshot()
    assert result["measurement"] == "measured"
    assert result["sent_bytes"] == 850 and result["total_bytes"] == 900


def test_repeated_calls_include_failed_and_successful_transfers_exactly_once():
    failure = CurlError(SECRET, 28)
    failure.response = Reply()
    transport = Transport([Reply(), failure, Reply(proxied=1)])
    with measure_network_usage() as meter:
        for _ in range(3):
            try:
                measured_request(transport, "get", URL)
            except CurlError:
                pass
    result = meter.snapshot()
    assert result["request_count"] == result["measured_requests"] == 3
    assert result["transport_errors"] == 1 and result["total_bytes"] == 4140
    assert result["proxy_bytes"] == 1380 and result["direct_bytes"] == 2760


def test_unknown_route_is_kept_separate_from_proxy_cost():
    response = Reply(proxied=None)
    transport = SimpleNamespace(get=lambda *args, **kwargs: response)
    with measure_network_usage() as meter:
        measured_request(transport, "get", URL)
    result = meter.snapshot()
    assert result["unknown_route_bytes"] == result["total_bytes"] == 1380
    assert result["proxy_bytes"] == result["direct_bytes"] == 0
    assert result["measurement"] == "measured"


def test_nested_scope_can_isolate_account_from_setup_then_restore_setup():
    transport = Transport([Reply(), Reply(), Reply()])
    with measure_network_usage() as parent:
        measured_request(transport, "get", URL)
        with measure_network_usage(propagate=False) as child:
            measured_request(transport, "get", URL)
        measured_request(transport, "get", URL)
    assert parent.snapshot()["request_count"] == 2
    assert child.snapshot()["request_count"] == 1
    with measure_network_usage() as empty:
        pass
    assert empty.snapshot()["measurement"] == "unavailable"


def test_default_nested_scope_is_inclusive_once_without_leaking_after_exit():
    transport = Transport([Reply(), Reply()])
    with measure_network_usage() as parent:
        with measure_network_usage() as child:
            measured_request(transport, "get", URL)
    measured_request(transport, "get", URL)
    assert parent.snapshot()["request_count"] == child.snapshot()["request_count"] == 1


def test_worker_contexts_do_not_mix_accounts_or_parent_setup():
    barrier = Barrier(2)
    def account(number):
        with measure_network_usage(propagate=False) as meter:
            barrier.wait(timeout=5)
            measured_request(Transport([Reply(request=number * 100, upload=0)]), "get", URL)
            return meter.snapshot()
    with measure_network_usage() as parent:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first, second = list(workers.map(account, [1, 2]))
    assert first["sent_bytes"] == 100 and second["sent_bytes"] == 200
    assert first["request_count"] == second["request_count"] == 1
    assert parent.snapshot()["request_count"] == 0


def test_transport_options_only_add_numeric_infos_inside_scope():
    options = {"retry": 0, "proxy": URL, "proxy_auth": (SECRET, SECRET), "curl_infos": [CurlInfo.HTTP_CONNECTCODE]}
    assert bandwidth_transport_options(options) == options
    with measure_network_usage():
        enhanced = bandwidth_transport_options(options)
    assert options["curl_infos"] == [CurlInfo.HTTP_CONNECTCODE]
    assert enhanced["retry"] == 0 and enhanced["proxy_auth"] == options["proxy_auth"]
    assert all(info in enhanced["curl_infos"] for info in BANDWIDTH_CURL_INFOS)
    assert len(enhanced["curl_infos"]) == len(set(enhanced["curl_infos"]))


@pytest.fixture
def fake_session(monkeypatch):
    replies, calls = [], []
    class FakeHTTP:
        def __init__(self, **options):
            self.proxies = {"all": options["proxy"]} if options.get("proxy") else {}
            self.retry = SimpleNamespace(count=0)
            self.cookies = SimpleNamespace(jar=[])
        def request(self, method, *args, **kwargs):
            calls.append(method)
            response = replies.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        def get(self, *args, **kwargs):
            return self.request("get", *args, **kwargs)
        def post(self, *args, **kwargs):
            return self.request("post", *args, **kwargs)
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.close()
    monkeypatch.setattr(client.requests, "Session", FakeHTTP)
    saved = {
        "format_version": 1, "created_at_utc": "2026-10-04T00:00:00+00:00",
        "origin": "https://play.anghami.com", "account_email": "synthetic@example.com",
        "requests": {"relations": {
            "method": "GET", "url": client.GATEWAY_URL + "?type=GETuserrelations&sid=" + SECRET + "&appsid=" + SECRET + "&fingerprint=" + SECRET,
            "headers": {"cookie": "fingerprint=" + SECRET, "user-agent": "Synthetic Chrome/152.0.0.0"},
        }},
    }
    return saved, replies, calls


@pytest.mark.parametrize("action,expected", [("play", 5), ("like", 9)])
def test_full_action_covers_preflight_negative_control_auth_metadata_write_and_readback(fake_session, action, expected):
    from test_like import discovery, playlist
    saved, replies, calls = fake_session
    song_id = play_record.TEST_SONG_ID
    replies.extend([
        Reply({"status": "ok"}), Reply({"status": "failed"}),
        Reply({"status": "ok", "authenticate": {
            "email": "synthetic@example.com", "reqkey": "R" * 32, "reskey": "S" * 32,
            "socketsessionid": SECRET, "signingkey": SECRET,
        }}),
        Reply({"status": 1, "id": song_id, "duration": "114.99"}),
    ])
    if action == "like":
        replies.extend([Reply(discovery(["42"])), Reply(playlist(["42"]))])
    replies.append(Reply({"status": "ok"}))
    if action == "like":
        replies.extend([Reply(discovery(["42", song_id])), Reply(playlist(["42", song_id]))])
    with measure_network_usage() as meter:
        with client.AnghamiSession(saved=saved) as session:
            runner = play_record.run_play_record_test if action == "play" else like_test.run_like_test
            report = runner(session, song_id)
    assert report["passed"] is True and not replies
    assert len(calls) == meter.snapshot()["request_count"] == expected
    assert meter.snapshot()["total_bytes"] == expected * 1380
    assert meter.snapshot()["measurement"] == "measured"
    assert_private(meter.snapshot())
    if action == "play":
        assert report["bandwidth"]["total"]["request_count"] == 2
        assert meter.snapshot()["request_count"] == 5  # Legacy subset never added twice.


def test_country_preflight_retries_and_profile_identity_are_included(fake_session, monkeypatch):
    saved, replies, calls = fake_session
    monkeypatch.setattr(proxy, "wait_country_lookup_start", lambda: True)
    monkeypatch.setattr(proxy.time, "sleep", lambda _: None)
    failure = CurlError(SECRET, 28)
    failure.response = Reply(proxied=1)
    replies.extend([failure, Reply({"country": "EG"}, proxied=1)])
    route = proxy.PacketStreamProxy("synthetic", "synthetic-secret")
    with measure_network_usage() as meter:
        assert route.verify_country()["country_verified"] is True
        replies.append(Reply({"status": "ok", "email": "synthetic@example.com"}))
        with client.AnghamiSession(saved=saved) as session:
            session_recovery._profile_identity(session, "synthetic@example.com")
    result = meter.snapshot()
    assert calls == ["get", "get", "get"]
    assert result["request_count"] == 3 and result["transport_errors"] == 1
    assert result["proxy_bytes"] == 2760 and result["direct_bytes"] == 1380
    assert_private(result)


@pytest.fixture
def loopback_http():
    compressed = compress(b"synthetic-compressed-response" * 1500)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(compressed)))
            self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(compressed)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_address[1]), compressed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_installed_curl_post_request_size_includes_upload_body_once(loopback_http):
    url, _ = loopback_http
    body = b"x" * 10000
    with measure_network_usage() as meter:
        with requests.Session(**bandwidth_transport_options({
            "retry": 0, "curl_options": {CurlOpt.PROXY: "", CurlOpt.NOPROXY: "*"},
        })) as transport:
            response = measured_request(transport, "post", url, data=body, timeout=5)
    result = meter.snapshot()
    assert response.upload_size == len(body)
    assert response.request_size > len(body)
    assert result["sent_bytes"] == response.request_size
    assert result["request_header_bytes"] == response.request_size - len(body)
    assert result["total_bytes"] == response.request_size + response.download_size + response.header_size
    assert result["total_bytes"] < 2 * len(body)
    assert result["measurement"] == "measured" and result["direct_bytes"] == result["total_bytes"]


def test_installed_curl_counts_compressed_wire_body_not_decoded_response(loopback_http):
    url, compressed = loopback_http
    with measure_network_usage() as meter:
        with requests.Session(**bandwidth_transport_options({
            "retry": 0, "curl_options": {CurlOpt.PROXY: "", CurlOpt.NOPROXY: "*"},
        })) as transport:
            response = measured_request(transport, "get", url, timeout=5)
    result = meter.snapshot()
    assert len(response.content) > len(compressed)
    assert result["download_body_bytes"] == len(compressed)
    assert result["received_bytes"] == len(compressed) + response.header_size
    assert result["measurement"] == "measured"
