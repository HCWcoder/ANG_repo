"""The measurement relay is checked with synthetic loopback traffic only."""

import importlib.util
import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from anghami_session import proxy as proxy_module
from anghami_session import preparation, vault as vault_module
from anghami_session.errors import SessionError
from curl_cffi.const import CurlInfo


_SPEC = importlib.util.spec_from_file_location(
    "synthetic_preparation_bandwidth_probe",
    Path(__file__).with_name("probe_prepare_bandwidth.py"),
)
probe = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = probe
_SPEC.loader.exec_module(probe)


def receive_exact(connection, count):
    result = bytearray()
    while len(result) < count:
        chunk = connection.recv(count - len(result))
        if not chunk:
            raise AssertionError("Synthetic forwarding closed before all bytes arrived")
        result.extend(chunk)
    return bytes(result)


def relay_client(relay):
    address = urlsplit(relay.proxy_url)
    assert address.hostname == "127.0.0.1"
    return socket.create_connection((address.hostname, address.port), timeout=2)


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Synthetic relay condition did not become true")
        time.sleep(0.01)


def test_relay_forwards_and_counts_both_directions_exactly(monkeypatch):
    upload = b"CONNECT synthetic.invalid:443 HTTP/1.1\r\n\r\n" + bytes(range(256)) * 300
    download = b"HTTP/1.1 200 Connection Established\r\n\r\n" + bytes(range(255, -1, -1)) * 450
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(1)
    upstream.settimeout(2)
    upstream_errors = []
    received = []

    def serve():
        try:
            with upstream.accept()[0] as connection:
                connection.settimeout(2)
                received.append(receive_exact(connection, len(upload)))
                connection.sendall(download)
        except BaseException as exc:
            upstream_errors.append(exc)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    monkeypatch.setattr(
        probe.MeteredRelay, "_connect_upstream",
        lambda self: socket.create_connection(upstream.getsockname(), timeout=2),
    )
    relay = probe.MeteredRelay(byte_limit=500_000, time_limit=3, idle_timeout=2).start()
    try:
        with relay_client(relay) as client:
            client.sendall(upload)
            assert receive_exact(client, len(download)) == download
        wait_until(lambda: relay.snapshot()["total_bytes"] == len(upload) + len(download))
    finally:
        relay.close()
        worker.join(timeout=2)
        upstream.close()
    assert not worker.is_alive()
    assert upstream_errors == []
    assert received == [upload]
    result = relay.snapshot()
    assert result["upload_bytes"] == len(upload)
    assert result["download_bytes"] == len(download)
    assert result["total_bytes"] == len(upload) + len(download)
    assert result["connections"] == 1
    assert result["failures"] == 0
    assert not result["limited"]


def test_failed_upstream_connection_closes_client_and_redacts_failure(monkeypatch):
    attempted = threading.Event()

    def fail(self):
        attempted.set()
        raise OSError("synthetic-private-proxy-password")

    monkeypatch.setattr(probe.MeteredRelay, "_connect_upstream", fail)
    with probe.MeteredRelay(time_limit=2, idle_timeout=1) as relay:
        with relay_client(relay) as client:
            assert attempted.wait(timeout=1)
            try:
                assert client.recv(1) == b""
            except ConnectionResetError:
                pass
        wait_until(lambda: relay.snapshot()["failures"] == 1)
    result = relay.snapshot()
    assert result["upload_bytes"] == result["download_bytes"] == result["total_bytes"] == 0
    assert result["connections"] == 1
    assert "synthetic-private-proxy-password" not in str(result)


def test_empty_relay_closes_promptly_and_releases_listener():
    relay = probe.MeteredRelay(time_limit=2, idle_timeout=1).start()
    address = urlsplit(relay.proxy_url)
    started = time.monotonic()
    relay.close()
    assert time.monotonic() - started < 1.5
    with pytest.raises(OSError):
        socket.create_connection((address.hostname, address.port), timeout=0.25)
    result = relay.snapshot()
    assert result["connections"] == 0
    assert result["total_bytes"] == 0


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_byte_cap_closes_tunnel_without_counting_more_than_limit(monkeypatch, direction):
    local, upstream = socket.socketpair()
    upstream.settimeout(2)
    monkeypatch.setattr(probe.MeteredRelay, "_connect_upstream", lambda self: local)
    relay = probe.MeteredRelay(byte_limit=512, time_limit=2, idle_timeout=1).start()
    try:
        with relay_client(relay) as client:
            if direction == "upload":
                client.sendall(b"u" * 4096)
            else:
                upstream.sendall(b"d" * 4096)
            wait_until(lambda: relay.snapshot()["limited"])
            try:
                assert client.recv(4096) == b""
            except ConnectionResetError:
                pass
        if direction == "upload":
            forwarded = bytearray()
            while True:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                forwarded.extend(chunk)
            assert forwarded == b"u" * 512
    finally:
        relay.close()
        upstream.close()
        local.close()
    result = relay.snapshot()
    assert result["total_bytes"] == 512
    assert result[f"{direction}_bytes"] == 512
    assert result["download_bytes" if direction == "upload" else "upload_bytes"] == 0
    assert result["limited"] is True
    assert result["failures"] == 0


def test_global_deadline_closes_even_an_empty_listener():
    relay = probe.MeteredRelay(time_limit=0.15, idle_timeout=1).start()
    address = urlsplit(relay.proxy_url)
    started = time.monotonic()
    try:
        wait_until(lambda: relay.snapshot()["limited"], timeout=1)
        assert time.monotonic() - started < 0.8
        with pytest.raises(OSError):
            socket.create_connection((address.hostname, address.port), timeout=0.25)
    finally:
        relay.close()
    result = relay.snapshot()
    assert result["limited"] is True
    assert result["connections"] == result["total_bytes"] == result["failures"] == 0


def test_idle_upstream_closes_active_connection_without_global_limit(monkeypatch):
    local, upstream = socket.socketpair()
    connected = threading.Event()

    def connect(self):
        connected.set()
        return local

    monkeypatch.setattr(probe.MeteredRelay, "_connect_upstream", connect)
    with probe.MeteredRelay(time_limit=2, idle_timeout=0.1) as relay:
        with relay_client(relay) as client:
            assert connected.wait(timeout=1)
            assert client.recv(1) == b""
    upstream.close()
    local.close()
    result = relay.snapshot()
    assert result["connections"] == 1
    assert result["total_bytes"] == result["failures"] == 0
    assert result["limited"] is False


def test_metered_proxy_preserves_auth_and_routes_country_preflight_through_relay(monkeypatch):
    base = proxy_module.PacketStreamProxy("synthetic-user", "synthetic-auth-key")
    original_transport = base.transport_options()
    original_browser = base.browser_options()
    relay = SimpleNamespace(proxy_url="http://127.0.0.1:23456")
    metered = probe.MeteredProxy(base, relay)
    sessions = []
    requests = []
    closed = []

    class Response:
        status_code = 200
        infos = {CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 200}

        def json(self):
            return {"country": "EG", "ip": "192.0.2.6"}

        def close(self):
            closed.append(True)

    class Transport:
        def __init__(self, **options):
            sessions.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **options):
            requests.append((url, options))
            return Response()

    monkeypatch.setattr(proxy_module.requests, "Session", Transport)
    expected_transport = {**original_transport, "proxy": relay.proxy_url}
    expected_browser = {**original_browser, "server": relay.proxy_url}
    assert metered.transport_options() == expected_transport
    assert metered.browser_options() == expected_browser
    assert base.transport_options() == original_transport
    assert base.browser_options() == original_browser
    assert metered.summary() == base.summary()
    assert expected_browser["username"] == original_transport["proxy_auth"][0]
    assert expected_browser["password"] == original_transport["proxy_auth"][1]
    assert "_country-EG_session-" in expected_browser["password"]
    result = metered.verify_country()
    assert sessions == [expected_transport]
    assert requests == [(proxy_module.GEOLOCATION_URL, {"timeout": 25, "allow_redirects": False})]
    assert result["country_verified"] is True
    assert closed == [True]


class SyntheticVault:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def select_test_candidates(self, count, *, start_row):
        self.calls.append(("select", count, start_row))
        return [8]

    def session(self, row):
        self.calls.append(("session", row))
        raise SessionError("Synthetic row has no session")


@pytest.mark.parametrize("reduce_browser_data", [False, True])
def test_main_forwards_explicit_cloakbrowser_and_records_backend(monkeypatch, tmp_path, capsys, reduce_browser_data):
    calls = []
    vault = SyntheticVault(calls)
    base_proxy = object()
    metered_proxy = object()
    snapshots = {
        "upload_bytes": 123, "download_bytes": 456, "total_bytes": 579,
        "connections": 2, "failures": 0, "duration_seconds": 0.25,
        "limited": False,
    }

    class Relay:
        def start(self):
            calls.append(("relay.start",))
            return self

        def close(self):
            calls.append(("relay.close",))

        def snapshot(self):
            return dict(snapshots)

    relay = Relay()

    def wrap(base, measured):
        assert base is base_proxy
        assert measured is relay
        return metered_proxy

    def prepare(selected_vault, **options):
        assert selected_vault is vault
        callback = options.pop("progress")
        calls.append(("prepare", options))
        callback({"phase": "complete"})
        return {"passed": True, "prepared_rows": [8]}

    monkeypatch.setattr(vault_module, "AccountVault", lambda: vault)
    monkeypatch.setattr(proxy_module, "load_packetstream_proxy", lambda: base_proxy)
    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    monkeypatch.setattr(probe, "MeteredRelay", lambda: relay)
    monkeypatch.setattr(probe, "MeteredProxy", wrap)
    report_path = tmp_path / "synthetic-bandwidth.json"
    monkeypatch.setattr(probe, "REPORT_PATH", report_path)
    arguments = ["--run", "--start-row", "8", "--browser", "cloakbrowser"]
    if reduce_browser_data:
        arguments.append("--reduce-browser-data")
    assert probe.main(arguments) == 0
    assert calls == [
        ("select", 1, 8), ("session", 8), ("relay.start",),
        ("prepare", {
            "count": 1, "start_row": 8, "proxy": metered_proxy,
            "browser_backend": "cloakbrowser", "headless": True,
            **({"reduce_browser_data": True} if reduce_browser_data else {}),
        }),
        ("relay.close",),
    ]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert json.loads(capsys.readouterr().out) == report
    assert report["browser_backend"] == "cloakbrowser"
    assert report["headless"] is True
    assert report["reduce_browser_data"] is reduce_browser_data
    assert report["passed"] is report["prepared"] is report["was_new_login"] is True
    assert report["source_row"] == 8
    assert report["total_bytes"] == 579
    assert report["phase_snapshots"] == [{"phase": "complete", **snapshots}]


def test_main_preview_selects_one_row_without_proxy_browser_or_network(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(vault_module, "AccountVault", lambda: SyntheticVault(calls))

    def forbidden(*args, **kwargs):
        pytest.fail("Offline preview reached a proxy, relay, or account preparation")

    monkeypatch.setattr(proxy_module, "load_packetstream_proxy", forbidden)
    monkeypatch.setattr(preparation, "prepare_test_accounts", forbidden)
    monkeypatch.setattr(probe, "MeteredRelay", forbidden)
    report_path = tmp_path / "must-not-be-written.json"
    monkeypatch.setattr(probe, "REPORT_PATH", report_path)
    assert probe.main(["--start-row", "8", "--browser", "cloakbrowser", "--reduce-browser-data"]) == 0
    assert calls == [("select", 1, 8), ("session", 8)]
    result = json.loads(capsys.readouterr().out)
    assert result["live_run"] is False
    assert result["source_row"] == 8
    assert result["was_new_login"] is True
    assert result["browser_backend"] == "cloakbrowser"
    assert result["reduce_browser_data"] is True
    assert result["network_requests"] == 0
    assert not report_path.exists()
