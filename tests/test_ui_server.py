"""The local console is exercised through loopback HTTP with synthetic services."""

import http.client
import json
import socket
import threading
from types import SimpleNamespace

import pytest

from anghami_session import test_settings, ui_jobs, ui_server
from anghami_session.errors import SessionError


SECRET = "synthetic-only-ui-secret-must-not-appear"
EMAIL = "synthetic-only-ui@example.invalid"


class FakeManager:
    def __init__(self):
        self.calls = []
        self.current = None
        self.failure = None

    def snapshot(self):
        return self.current

    def submit(self, payload):
        if self.failure is not None:
            raise self.failure
        ui_jobs._validate(payload)
        self.calls.append(payload)
        self.current = {"id": "synthetic-job", "status": "queued"}
        return self.current


class FakeService:
    def __init__(self):
        self.manager = FakeManager()
        self.find_calls = []
        self.proxy_calls = []

    def state(self):
        return {
            "counts": {"imported": 10, "session_ready": 2},
            "cohort": [1, 7], "proxy_configured": False,
            "test_song_id": "1263607749", "job": None,
            "reports": [], "limits": {"max_count": 5},
        }

    def submit(self, payload):
        return self.manager.submit(payload)

    def find(self, payload):
        self.find_calls.append(payload)
        return {"source_rows": [7]}

    def save_proxy(self, payload):
        self.proxy_calls.append(payload)
        return {"configured": True, "provider": "PacketStream", "country": "EG"}

    def read_report(self, name):
        raise FileNotFoundError


@pytest.fixture
def console():
    service = FakeService()
    server = ui_server.create_server(port=0, service=service)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    yield SimpleNamespace(
        server=server, service=service, port=server.server_address[1],
        host=f"127.0.0.1:{server.server_address[1]}",
    )
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.fixture
def real_console(tmp_path, monkeypatch):
    folder = tmp_path / ".anghami"
    folder.mkdir()
    vault_calls = []
    proxy_calls = []

    class SyntheticVault:
        def __init__(self, path):
            assert path == folder / "accounts.sqlite3"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def summary(self):
            return {
                "records": 10, "unique_accounts": 9, "sessions_saved": 2,
                "duplicate_rows": 1, "states": {"ready": 2, "login_required": 8},
                "encrypted_source_backup": SECRET, "source_sha256": SECRET,
            }

        def test_accounts(self):
            return {
                "test_rows": [1, 7], "ready_rows": [1, 7],
                "accounts": [{"source_row": 7, "state": "ready", "session_saved": True}],
            }

        def find(self, email):
            vault_calls.append(email)
            return [7]

    def absent_proxy(path):
        assert path == folder / "packetstream.dpapi"
        raise SessionError("Synthetic proxy is not configured")

    def save_proxy(username, auth_key, path):
        from anghami_session.proxy import PacketStreamProxy
        PacketStreamProxy(username, auth_key)
        proxy_calls.append((username, auth_key, path))
        return {"provider": "PacketStream", "country": "EG"}

    monkeypatch.setattr(ui_server, "AccountVault", SyntheticVault)
    monkeypatch.setattr(ui_server, "load_packetstream_proxy", absent_proxy)
    monkeypatch.setattr(ui_server, "save_packetstream_credentials", save_proxy)
    service = ui_server.ConsoleService(vault_path=folder / "accounts.sqlite3", manager=FakeManager())
    server = ui_server.create_server(port=0, service=service)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    yield SimpleNamespace(
        server=server, service=service, folder=folder, port=server.server_address[1],
        host=f"127.0.0.1:{server.server_address[1]}", vault_calls=vault_calls,
        proxy_calls=proxy_calls,
    )
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def request(console, method, path, *, body=None, token=True, headers=None):
    options = {} if headers is None else dict(headers)
    if token:
        options.setdefault("X-App-Token", console.server.token)
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        options.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", console.port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=options)
        response = connection.getresponse()
        content = response.read()
        return response.status, dict(response.getheaders()), content
    finally:
        connection.close()


def raw_request(console, text):
    with socket.create_connection(("127.0.0.1", console.port), timeout=3) as connection:
        connection.sendall(text.encode("ascii"))
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = connection.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def test_console_binds_loopback_and_has_unique_tokens(console):
    assert console.server.server_address[0] == "127.0.0.1"
    assert console.server.base_url == f"http://{console.host}"
    assert isinstance(console.server.token, str) and len(console.server.token) >= 32
    second = ui_server.create_server(port=0, service=FakeService())
    try:
        assert second.token != console.server.token
    finally:
        second.server_close()


def test_index_and_static_assets_have_security_headers(console):
    status, headers, content = request(console, "GET", "/", token=False)
    assert status == 200
    assert console.server.token.encode() in content
    assert b"__APP_TOKEN__" not in content
    assert "text/html" in headers["Content-Type"]
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    csp = headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "Access-Control-Allow-Origin" not in headers
    for path, content_type in [("/app.js", "javascript"), ("/selection.js", "javascript"), ("/styles.css", "text/css")]:
        status, headers, content = request(console, "GET", path, token=False)
        assert status == 200
        assert content_type in headers["Content-Type"]
        assert content
        assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("token", [None, "", "wrong-token", "wrong-token-with-very-long-value", "\u00e9"])
def test_api_state_requires_exact_app_token(console, token):
    headers = {} if token is None else {"X-App-Token": token}
    status, _, content = request(console, "GET", "/api/state", token=False, headers=headers)
    assert status == 403
    assert console.server.token.encode() not in content
    assert console.service.manager.calls == []


def test_api_state_returns_safe_service_snapshot(console):
    status, headers, content = request(console, "GET", "/api/state")
    assert status == 200
    assert "application/json" in headers["Content-Type"]
    assert json.loads(content) == console.service.state()


@pytest.mark.parametrize("action", ["play", "like"])
def test_test_request_submits_exactly_one_bounded_job(console, action):
    payload = {"action": action, "rows": [1, 7], "count": 3, "proxy_egypt": True}
    status, _, content = request(console, "POST", "/api/jobs", body=payload)
    assert status == 202
    assert json.loads(content)["job"] == console.service.manager.current
    assert console.service.manager.calls == [payload]
    for _ in range(3):
        status, _, _ = request(console, "GET", "/api/job")
        assert status == 200
    assert console.service.manager.calls == [payload]


@pytest.mark.parametrize("payload", [
    {}, {"action": "unsupported"}, {"action": "like"},
    {"action": "like", "rows": [7], "count": 0},
    {"action": "like", "rows": [7], "count": 6},
    {"action": "like", "rows": [7], "count": True},
    {"action": "play", "rows": [1, 1]},
    {"action": "play", "rows": [True]},
    {"action": "play", "rows": [7], "proxy_egypt": "true"},
    {"action": "prepare", "headless": "true"},
    *[{"action": "prepare", "reduce_browser_data": value} for value in ("true", 1, 0, None, [], {})],
    {"action": "prepare", "browser": "unsupported"},
    {"action": "prepare", "start_row": -1},
    {"action": "play", "rows": [7], "song_id": "unexpected-song"},
    {"action": "play", "rows": [7], "auto_repeat": True},
])
def test_invalid_controls_cannot_start_job(console, payload):
    status, _, _ = request(console, "POST", "/api/jobs", body=payload)
    assert status == 400
    assert console.service.manager.calls == []


@pytest.mark.parametrize("value", [False, True])
@pytest.mark.parametrize("use_proxy", [False, True])
def test_api_accepts_boolean_browser_data_selection_independently_of_proxy(console, value, use_proxy):
    payload = {"action": "prepare", "count": 1, "reduce_browser_data": value, "proxy_egypt": use_proxy}
    status, _, _ = request(console, "POST", "/api/jobs", body=payload)
    assert status == 202
    assert console.service.manager.calls == [payload]


def test_safe_report_retains_browser_data_choice_without_private_strings():
    result = ui_server.safe_report({"reduce_browser_data": True, "password": "private-password"})
    assert result == {"reduce_browser_data": True}
    assert ui_server.safe_report({"reduce_browser_data": "private-password"}) == {"reduce_browser_data": "[redacted]"}


def test_offline_preview_route_survives_safe_report_without_proxy_secrets():
    assert ui_server.safe_report({"connection": "proxy_egypt", "dry_run": True, "auth_key": SECRET}) == {"connection": "proxy_egypt", "dry_run": True}


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("use_proxy", [False, True])
@pytest.mark.parametrize("value", [False, True])
def test_api_accepts_explicit_preparation_method_independently_of_connection(console, action, use_proxy, value):
    payload = {"action": action, "count": 1, "no_browser": value, "proxy_egypt": use_proxy}
    status, _, _ = request(console, "POST", "/api/jobs", body=payload)
    assert status == 202 and console.service.manager.calls == [payload]


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_api_rejects_non_boolean_preparation_method_without_creating_job(console, value):
    status, _, content = request(console, "POST", "/api/jobs", body={"action": "prepare", "no_browser": value})
    assert status == 400 and console.service.manager.calls == []
    assert console.service.manager.current is None
    assert SECRET.encode() not in content


def test_api_cannot_apply_no_browser_to_session_refresh(console):
    status, _, _ = request(console, "POST", "/api/jobs", body={"action": "login", "rows": [7], "no_browser": True})
    assert status == 400 and console.service.manager.calls == []


def test_http_preparation_report_is_visible_without_registered_session_material(real_console):
    name = "accounts-prepare-tests-report.json"
    raw = {
        "no_browser": True, "browser": "none", "browser_required": False,
        "preparation_method": "http", "phase": "session_recovery", "passed": False,
        "password": SECRET, "email": EMAIL, "cookies": SECRET, "request": {"headers": {"cookie": SECRET}},
    }
    (real_console.folder / name).write_text(json.dumps(raw), encoding="utf-8")
    status, _, content = request(real_console, "GET", "/api/reports?name=" + name)
    assert status == 200
    report = json.loads(content)["report"]
    assert report == {
        "no_browser": True, "browser": "none", "browser_required": False,
        "preparation_method": "http", "phase": "session_recovery", "passed": False,
    }
    assert SECRET.encode() not in content and EMAIL.encode() not in content


def test_busy_job_returns_conflict_without_second_submit(console):
    console.service.manager.failure = ui_jobs.JobBusyError("A test job is already running.")
    status, _, content = request(console, "POST", "/api/jobs", body={"action": "like", "rows": [7]})
    assert status == 409
    assert console.service.manager.calls == []
    assert json.loads(content).get("error")


@pytest.mark.parametrize("path", ["/", "/app.js", "/api/state"])
@pytest.mark.parametrize("host", ["evil.example:80", "127.0.0.1:1", "localhost.evil.example:80"])
def test_foreign_or_wrong_port_host_is_rejected(console, path, host):
    status, _, content = request(console, "GET", path, headers={"Host": host})
    assert status == 403
    assert console.server.token.encode() not in content


@pytest.mark.parametrize("hostname", ["127.0.0.1", "localhost"])
def test_local_origin_and_host_are_allowed(console, hostname):
    host = f"{hostname}:{console.port}"
    status, _, _ = request(console, "GET", "/api/state", headers={"Host": host, "Origin": f"http://{host}"})
    assert status == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1:1", "https://127.0.0.1:1"])
def test_foreign_origin_cannot_submit_job_even_with_token(console, origin):
    status, _, _ = request(console, "POST", "/api/jobs", body={"kind": "like"}, headers={"Origin": origin})
    assert status == 403
    assert console.service.manager.calls == []


@pytest.mark.parametrize("body", [b"{", b"[]", b"null", b'"text"', b"1", b"true"])
def test_job_rejects_invalid_json_or_non_object(console, body):
    status, _, _ = request(console, "POST", "/api/jobs", body=body, headers={"Content-Type": "application/json"})
    assert status == 400
    assert console.service.manager.calls == []


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_job_rejects_non_json_content_type(console, content_type):
    headers = {} if content_type is None else {"Content-Type": content_type}
    status, _, _ = request(console, "POST", "/api/jobs", body=b"{}", headers=headers)
    assert status == 415
    assert console.service.manager.calls == []


def test_oversized_json_rejected_before_submit(console):
    status, _, _ = request(console, "POST", "/api/jobs", body=b" " * (16 * 1024 + 1), headers={"Content-Type": "application/json"})
    assert status == 413
    assert console.service.manager.calls == []


@pytest.mark.parametrize("length", [None, "not-a-number", "-1"])
def test_invalid_content_length_rejected(console, length):
    lines = [
        "POST /api/jobs HTTP/1.1", f"Host: {console.host}",
        f"X-App-Token: {console.server.token}", "Content-Type: application/json",
        "Connection: close",
    ]
    if length is not None:
        lines.append(f"Content-Length: {length}")
    response = raw_request(console, "\r\n".join(lines) + "\r\n\r\n")
    assert response.split(b"\r\n", 1)[0].split()[1] == b"400"
    assert console.service.manager.calls == []


@pytest.mark.parametrize("extra_headers", [
    ["Content-Length: 2", "Content-Length: 2"],
    ["Content-Length: 2", "Transfer-Encoding: chunked"],
    ["Transfer-Encoding: chunked"],
])
def test_ambiguous_body_framing_rejected(console, extra_headers):
    lines = [
        "POST /api/jobs HTTP/1.1", f"Host: {console.host}",
        f"X-App-Token: {console.server.token}", "Content-Type: application/json",
        "Connection: close", *extra_headers,
    ]
    response = raw_request(console, "\r\n".join(lines) + "\r\n\r\n{}")
    assert response.split(b"\r\n", 1)[0].split()[1] == b"400"
    assert console.service.manager.calls == []


def test_truncated_body_cannot_submit_even_when_available_bytes_are_valid_json(console):
    body = '{"action":"preview","count":1}'
    lines = [
        "POST /api/jobs HTTP/1.1", f"Host: {console.host}",
        f"X-App-Token: {console.server.token}", "Content-Type: application/json",
        f"Content-Length: {len(body) + 3}", "Connection: close",
    ]
    response = raw_request(console, "\r\n".join(lines) + "\r\n\r\n" + body)
    assert response.split(b"\r\n", 1)[0].split()[1] == b"400"
    assert console.service.manager.calls == []


def test_worker_start_failure_never_reflects_exception(console, capsys):
    console.service.manager.failure = RuntimeError(SECRET)
    status, _, content = request(console, "POST", "/api/jobs", body={"action": "like", "rows": [7]})
    assert status == 500
    assert SECRET.encode() not in content
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert console.service.manager.calls == []


@pytest.mark.parametrize("path", ["/missing", "/../main.py", "/%2e%2e/main.py", "/api/missing", "/api/reports?name=../accounts.sqlite3"])
def test_unknown_routes_and_traversal_are_not_served(console, path):
    status, _, _ = request(console, "GET", path)
    assert status == 404


def test_options_never_grants_cross_origin_access(console):
    status, headers, _ = request(console, "OPTIONS", "/api/jobs", headers={"Origin": "https://evil.example"})
    assert status in {403, 405, 501}
    assert "Access-Control-Allow-Origin" not in headers
    assert "Access-Control-Allow-Methods" not in headers


def test_malformed_absolute_url_has_fixed_error_instead_of_disconnecting(console):
    response = raw_request(console, "\r\n".join([
        "GET http://[invalid HTTP/1.1", f"Host: {console.host}",
        f"X-App-Token: {console.server.token}", "Connection: close", "", "",
    ]))
    assert response.split(b"\r\n", 1)[0].split()[1] == b"400"
    assert b"http://[invalid" not in response
    assert console.service.manager.calls == []


def test_rejected_request_bodies_consistently_return_http_errors(console):
    # On Windows, closing a socket with an unread body can reset the connection
    # before the error response arrives. Exercise that boundary repeatedly.
    for _ in range(10):
        status, _, _ = request(console, "POST", "/api/jobs", body=b"{}")
        assert status == 415
        status, _, _ = request(console, "POST", "/api/jobs", body=b" " * (ui_server.MAX_BODY + 1), headers={"Content-Type": "application/json"})
        assert status == 413
    assert console.service.manager.calls == []


def test_state_uses_existing_vault_without_exposing_private_metadata(real_console):
    status, _, content = request(real_console, "GET", "/api/state")
    assert status == 200
    payload = json.loads(content)
    assert payload["vault"]["records"] == 10
    assert payload["cohort"]["ready_rows"] == [1, 7]
    assert payload["proxy"]["configured"] is False
    assert payload["limits"] == {"accounts": 5, "test_accounts": 2, "tests_per_account": 5}
    assert SECRET.encode() not in content
    assert b"encrypted_source_backup" not in content
    assert b"source_sha256" not in content


def test_find_returns_only_rows_and_does_not_echo_email(real_console):
    status, _, content = request(real_console, "POST", "/api/find", body={"email": EMAIL})
    assert status == 200
    assert json.loads(content) == {"source_rows": [7]}
    assert real_console.vault_calls == [EMAIL]
    assert EMAIL.encode() not in content


@pytest.mark.parametrize("payload", [{}, {"email": 1}, {"email": EMAIL, "password": SECRET}, {"email": "x" * 255}])
def test_find_rejects_invalid_fields_without_accessing_vault(real_console, payload):
    status, _, content = request(real_console, "POST", "/api/find", body=payload)
    assert status == 400
    assert real_console.vault_calls == []
    assert SECRET.encode() not in content


def test_proxy_save_keeps_key_out_of_response(real_console):
    status, _, content = request(real_console, "POST", "/api/proxy", body={"username": "synthetic-user", "auth_key": SECRET})
    assert status == 200
    assert json.loads(content) == {"configured": True, "provider": "PacketStream", "country": "EG"}
    assert real_console.proxy_calls == [("synthetic-user", SECRET, real_console.folder / "packetstream.dpapi")]
    assert SECRET.encode() not in content
    assert b"synthetic-user" not in content


@pytest.mark.parametrize("payload", [
    {}, {"username": "user"}, {"username": "user", "auth_key": SECRET, "url": "http://unused.invalid"},
    {"username": 1, "auth_key": SECRET}, {"username": "user", "auth_key": "x" * 257},
    {"username": "user", "auth_key": "contains whitespace"},
])
def test_invalid_proxy_credentials_are_not_saved_or_reflected(real_console, payload):
    status, _, content = request(real_console, "POST", "/api/proxy", body=payload)
    assert status == 400
    assert real_console.proxy_calls == []
    assert SECRET.encode() not in content


def test_proxy_changes_are_blocked_while_job_is_running(real_console):
    real_console.service.manager.current = {"id": "synthetic-job", "status": "running"}
    status, _, content = request(real_console, "POST", "/api/proxy", body={"username": "synthetic-user", "auth_key": SECRET})
    assert status == 409
    assert real_console.proxy_calls == []
    assert SECRET.encode() not in content


def test_song_edit_persists_and_reloaded_console_uses_it(real_console):
    status, _, content = request(real_console, "POST", "/api/test-song", body={"song_id": "1280677978"})
    assert status == 200
    assert json.loads(content) == {"test_song_id": "1280677978"}
    path = real_console.folder / "test-settings.json"
    assert test_settings.read_test_song_id(path) == "1280677978"
    status, _, content = request(real_console, "GET", "/api/state")
    assert status == 200 and json.loads(content)["test_song_id"] == "1280677978"
    reloaded = ui_server.ConsoleService(vault_path=real_console.folder / "accounts.sqlite3", manager=FakeManager())
    assert reloaded.state()["test_song_id"] == "1280677978"
    assert real_console.service.manager.calls == []


@pytest.mark.parametrize("payload", [
    {}, {"song_id": "1280677978", "extra": True}, {"test_song_id": "1280677978"},
    {"song_id": "0"}, {"song_id": "01"}, {"song_id": True}, {"song_id": 1.0},
    {"song_id": "9223372036854775808"}, {"song_id": SECRET},
])
def test_invalid_song_edit_preserves_previous_setting_and_starts_no_job(real_console, payload):
    path = real_console.folder / "test-settings.json"
    test_settings.write_test_song_id("1280677978", path)
    before = path.read_bytes()
    status, _, content = request(real_console, "POST", "/api/test-song", body=payload)
    assert status == 400
    assert path.read_bytes() == before
    assert SECRET.encode() not in content
    assert real_console.service.manager.calls == []


@pytest.mark.parametrize("headers, with_token", [
    ({}, False), ({"X-App-Token": "wrong-token"}, False),
    ({"Origin": "https://evil.example"}, True),
])
def test_song_edit_requires_console_token_and_origin(real_console, headers, with_token):
    status, _, _ = request(real_console, "POST", "/api/test-song", body={"song_id": "1280677978"}, token=with_token, headers=headers)
    assert status == 403
    assert not (real_console.folder / "test-settings.json").exists()
    assert real_console.service.manager.calls == []


@pytest.mark.parametrize("busy_status", ["queued", "running"])
def test_song_edit_is_blocked_during_queued_or_running_job(real_console, busy_status):
    path = real_console.folder / "test-settings.json"
    test_settings.write_test_song_id("1263607749", path)
    before = path.read_bytes()
    real_console.service.manager.current = {"id": "synthetic-job", "status": busy_status}
    status, _, _ = request(real_console, "POST", "/api/test-song", body={"song_id": "1280677978"})
    assert status == 409
    assert path.read_bytes() == before
    assert real_console.service.manager.calls == []


def test_malformed_existing_song_setting_fails_closed_in_state(real_console):
    path = real_console.folder / "test-settings.json"
    path.write_text('{"test_song_id":"01"}', encoding="utf-8")
    status, _, content = request(real_console, "GET", "/api/state")
    assert status == 500
    assert b"1263607749" not in content
    assert real_console.service.manager.calls == []


def test_report_serves_only_safe_outcomes_and_filters_private_data(real_console):
    name = "account-7.test-like-report.json"
    report = {
        "source_row": 7, "song_id": "1263607749", "passed": True,
        "phase": "state_after", "mutation_result": "accepted", "liked_after": True,
        "auth_key": SECRET, "password": SECRET, "username": EMAIL,
        "headers": {"Cookie": SECRET}, "sid": SECRET,
        "raw_response": {"private": SECRET}, "url": "https://unused.invalid/" + SECRET,
        "error": {"code": "mutation_unknown", "message": SECRET, "request": SECRET},
        "results": [{"passed": True, "cookies": SECRET, "title": SECRET}],
        "proxy": {"provider": "PacketStream", "country": "EG", "endpoint": "http://user:" + SECRET + "@unused.invalid"},
    }
    (real_console.folder / name).write_text(json.dumps(report), encoding="utf-8")
    status, _, content = request(real_console, "GET", "/api/reports?name=" + name)
    assert status == 200
    payload = json.loads(content)
    assert payload["name"] == name
    assert payload["report"]["passed"] is True
    assert payload["report"]["mutation_result"] == "accepted"
    assert payload["report"]["error"]["message"] == "[redacted]"
    assert SECRET.encode() not in content
    assert EMAIL.encode() not in content
    for field in (b'"password"', b'"auth_key"', b'"headers"', b'"cookies"', b'"sid"', b'"url"', b'"raw_response"'):
        assert field not in content


def test_report_listing_ignores_other_files_and_oversized_reports(real_console):
    allowed = "account-7.test-like-report.json"
    (real_console.folder / allowed).write_text("{}", encoding="utf-8")
    (real_console.folder / "credentials.json").write_text(json.dumps({"password": SECRET}), encoding="utf-8")
    (real_console.folder / "ui-last-job.json").write_bytes(b" " * (ui_server.MAX_REPORT + 1))
    status, _, content = request(real_console, "GET", "/api/state")
    assert status == 200
    assert [report["name"] for report in json.loads(content)["reports"]] == [allowed]
    assert SECRET.encode() not in content


@pytest.mark.parametrize("query", [
    "name=../account-7.test-like-report.json", "name=%2e%2e%2faccount-7.test-like-report.json",
    "name=accounts.sqlite3", "name=packetstream.dpapi", "name=credentials.json",
    "name=account-7.test-like-report.json&name=ui-last-job.json", "", "name=",
])
def test_report_paths_and_ambiguous_selection_are_rejected(real_console, query):
    status, _, content = request(real_console, "GET", "/api/reports?" + query)
    assert status == 404
    assert SECRET.encode() not in content


def test_report_symlink_escape_is_not_listed_or_read(real_console, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"message": SECRET}), encoding="utf-8")
    name = "account-7.test-like-report.json"
    try:
        (real_console.folder / name).symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Creating file symlinks is not enabled on this Windows host")
    assert real_console.service.reports() == []
    status, _, content = request(real_console, "GET", "/api/reports?name=" + name)
    assert status == 404
    assert SECRET.encode() not in content


def test_report_parse_errors_do_not_reflect_file_contents(real_console):
    name = "ui-last-job.json"
    (real_console.folder / name).write_text(SECRET, encoding="utf-8")
    status, _, content = request(real_console, "GET", "/api/reports?name=" + name)
    assert status == 500
    assert SECRET.encode() not in content


def test_existing_console_recognizes_live_loopback_server(console):
    status, headers, _ = request(console, "GET", "/", token=False)
    assert status == 200
    assert headers["X-Anghami-Test-Console"] == "1"
    assert ui_server._existing_console(console.port) is True
    assert console.service.manager.calls == []


def test_existing_console_returns_false_for_closed_loopback_port():
    with socket.socket() as temporary:
        temporary.bind(("127.0.0.1", 0))
        port = temporary.getsockname()[1]
    assert ui_server._existing_console(port) is False


@pytest.mark.parametrize("status, marker", [(200, None), (200, "0"), (404, "1"), (503, "1")])
def test_existing_console_does_not_reuse_unrecognized_listener(monkeypatch, status, marker):
    events = []

    class Response:
        def __init__(self):
            self.status = status

        def getheader(self, name):
            assert name == "X-Anghami-Test-Console"
            return marker

    class Connection:
        def __init__(self, host, port, *, timeout):
            events.append(("connection", host, port, timeout))

        def request(self, method, path):
            events.append(("request", method, path))

        def getresponse(self):
            return Response()

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(ui_server, "HTTPConnection", Connection)
    assert ui_server._existing_console(8765) is False
    assert events == [("connection", "127.0.0.1", 8765, 1), ("request", "GET", "/"), ("close",)]


@pytest.mark.parametrize("open_browser", [False, True])
def test_main_reuses_existing_console_without_creating_service_or_worker(monkeypatch, capsys, open_browser):
    opened = []
    checked = []

    def existing(port):
        checked.append(port)
        return True

    def forbid_creation(**_):
        raise AssertionError("An existing console must not create another server or manager")

    monkeypatch.setattr(ui_server, "_existing_console", existing)
    monkeypatch.setattr(ui_server, "create_server", forbid_creation)
    monkeypatch.setattr(ui_server.webbrowser, "open", lambda url: opened.append(url))
    args = ["--port", "8765"]
    if not open_browser:
        args.append("--no-browser")
    assert ui_server.main(args) == 0
    assert checked == [8765]
    assert opened == (["http://127.0.0.1:8765"] if open_browser else [])
    output = capsys.readouterr().out
    assert "http://127.0.0.1:8765" in output
    assert "Using the console already running." in output


def test_random_port_startup_skips_existing_console_probe(monkeypatch):
    events = []

    def forbid_probe(*_, **__):
        raise AssertionError("Port zero should never probe a listener")

    class Server:
        base_url = "http://127.0.0.1:12345"

        def serve_forever(self):
            events.append("serve")
            raise KeyboardInterrupt

        def server_close(self):
            events.append("close")

    def create(*, port):
        assert port == 0
        events.append("create")
        return Server()

    monkeypatch.setattr(ui_server, "HTTPConnection", forbid_probe)
    monkeypatch.setattr(ui_server, "create_server", create)
    assert ui_server.main(["--port", "0", "--no-browser"]) == 0
    assert events == ["create", "serve", "close"]
