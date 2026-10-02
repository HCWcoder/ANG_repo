"""PacketStream tests use synthetic credentials and an offline HTTP double."""

from dataclasses import FrozenInstanceError
import json

import pytest
from curl_cffi.const import CurlInfo, CurlOpt

from anghami_session import proxy, store
from anghami_session.errors import SessionError

USERNAME = "synthetic-private-proxy-user"
AUTH_KEY = "synthetic-private-proxy-key"
PRIVATE_URL = "https://private.invalid/?sid=synthetic-session-secret"


def assert_redacted(value):
    text = str(value)
    assert USERNAME not in text
    assert AUTH_KEY not in text
    assert PRIVATE_URL not in text


def test_dataclass_hides_credentials_and_fixes_country(monkeypatch):
    monkeypatch.setattr(proxy, "token_hex", lambda count: "0123456789abcdef")
    config = proxy.PacketStreamProxy(USERNAME, AUTH_KEY)
    assert repr(config) == "PacketStreamProxy(country='EG')"
    assert_redacted(repr(config))
    assert "0123456789abcdef" not in repr(config)
    assert config.country == "EG"
    with pytest.raises(FrozenInstanceError):
        config.country = "US"


def test_transport_options_bind_one_sticky_egypt_tunnel(monkeypatch):
    labels = iter(("0123456789abcdef", "fedcba9876543210"))
    monkeypatch.setattr(proxy, "token_hex", lambda count: next(labels))
    config = proxy.PacketStreamProxy(USERNAME, AUTH_KEY + "_country-EG")
    assert config.auth_key == AUTH_KEY
    first = config.transport_options()
    second = config.transport_options()
    assert first == second
    assert first == {
        "impersonate": "chrome", "proxy": "https://proxy.packetstream.io:31111",
        "proxy_auth": (USERNAME, AUTH_KEY + "_country-EG_session-0123456789abcdef"),
        "retry": 0, "verify": True, "debug": False,
        "curl_options": {CurlOpt.NOPROXY: ""},
        "curl_infos": [CurlInfo.USED_PROXY, CurlInfo.HTTP_CONNECTCODE],
    }
    assert USERNAME not in first["proxy"] and AUTH_KEY not in first["proxy"]
    assert proxy.PacketStreamProxy(USERNAME, AUTH_KEY).transport_options()["proxy_auth"] != first["proxy_auth"]
    assert config.summary() == {
        "provider": "PacketStream", "country": "EG",
        "endpoint": "https://proxy.packetstream.io:31111", "sticky": True,
    }
    assert_redacted(json.dumps(config.summary()))
    assert "0123456789abcdef" not in json.dumps(config.summary())


@pytest.mark.parametrize("username,key", [
    ("", AUTH_KEY), (USERNAME, ""), (None, AUTH_KEY), (USERNAME, None),
    (USERNAME + " ", AUTH_KEY), (USERNAME, " " + AUTH_KEY),
    (USERNAME, AUTH_KEY + "\n"), (USERNAME + "\t", AUTH_KEY),
    (USERNAME, AUTH_KEY + "\x00"), (USERNAME, AUTH_KEY + "\u200b"),
    (USERNAME, AUTH_KEY + "\u00a0"), (USERNAME + ":suffix", AUTH_KEY),
    (USERNAME, "_country-EG"), (USERNAME, AUTH_KEY + "_country-US"),
    (USERNAME, AUTH_KEY + "_country-Egypt"), (USERNAME, AUTH_KEY + "_country-eg"),
    (USERNAME, AUTH_KEY + "_country-EG_country-EG"),
    (USERNAME, AUTH_KEY + "_country-EG_session-old"),
    (USERNAME, AUTH_KEY + "_session-old"), (USERNAME + "_country-EG", AUTH_KEY),
    (USERNAME, AUTH_KEY + "_country_EG"), (USERNAME, AUTH_KEY + "_SESSION-old"),
])
def test_invalid_credentials_fail_without_saving_or_network(username, key, monkeypatch):
    monkeypatch.setattr(proxy.store, "save_protected_bytes", lambda *_: pytest.fail("Invalid input reached storage"))
    monkeypatch.setattr(proxy.requests, "Session", lambda **_: pytest.fail("Invalid input reached HTTP"))
    with pytest.raises(SessionError) as error:
        proxy.save_packetstream_credentials(username, key)
    assert str(error.value) == proxy._INVALID_CREDENTIALS
    assert_redacted(error.value)


def test_encrypted_file_roundtrip_uses_store_and_saves_base_key_only(tmp_path, monkeypatch):
    crypt_calls = []

    def fake_dpapi(data, *, decrypt=False):
        crypt_calls.append(decrypt)
        if decrypt:
            assert data.startswith(b"FAKE-DPAPI:")
            return bytes(value ^ 0x55 for value in data[len(b"FAKE-DPAPI:"):])
        return b"FAKE-DPAPI:" + bytes(value ^ 0x55 for value in data)

    monkeypatch.setattr(store, "_crypt", fake_dpapi)
    monkeypatch.setattr(proxy.requests, "Session", lambda **_: pytest.fail("Storage reached HTTP"))
    path = tmp_path / "packetstream.dpapi"
    summary = proxy.save_packetstream_credentials(USERNAME, AUTH_KEY + "_country-EG", path)
    cipher = path.read_bytes()
    assert USERNAME.encode() not in cipher and AUTH_KEY.encode() not in cipher
    assert not list(tmp_path.glob("*.tmp"))
    assert_redacted(json.dumps(summary))
    loaded = proxy.load_packetstream_proxy(path)
    assert loaded.username == USERNAME and loaded.auth_key == AUTH_KEY
    assert crypt_calls == [False, True]
    payload = json.loads(bytes(value ^ 0x55 for value in cipher[len(b"FAKE-DPAPI:"):]))
    assert payload == {
        "format_version": 1, "provider": "PacketStream", "country": "EG",
        "username": USERNAME, "auth_key": AUTH_KEY,
    }
    assert "_session-" not in json.dumps(payload)


def test_load_generates_fresh_label_each_time(monkeypatch):
    payload = json.dumps({
        "format_version": 1, "provider": "PacketStream", "country": "EG",
        "username": USERNAME, "auth_key": AUTH_KEY,
    }).encode()
    monkeypatch.setattr(proxy.store, "load_protected_bytes", lambda _: payload)
    first = proxy.load_packetstream_proxy()
    second = proxy.load_packetstream_proxy()
    assert first.transport_options()["proxy_auth"] != second.transport_options()["proxy_auth"]
    assert first.summary() == second.summary()


def test_missing_config_has_safe_actionable_error(tmp_path):
    with pytest.raises(SessionError, match="not configured") as error:
        proxy.load_packetstream_proxy(tmp_path / "missing.dpapi")
    assert_redacted(error.value)


@pytest.mark.parametrize("function", ["load", "save"])
def test_storage_failures_never_expose_exception_text(function, monkeypatch):
    def fail(*_, **__):
        raise RuntimeError(USERNAME + AUTH_KEY + PRIVATE_URL)

    if function == "load":
        monkeypatch.setattr(proxy.store, "load_protected_bytes", fail)
        call = proxy.load_packetstream_proxy
    else:
        monkeypatch.setattr(proxy.store, "save_protected_bytes", fail)
        call = lambda: proxy.save_packetstream_credentials(USERNAME, AUTH_KEY)
    with pytest.raises(SessionError) as error:
        call()
    assert_redacted(error.value)


@pytest.mark.parametrize("payload", [
    b"not-json", b"\xff", b"[]", b"{}",
    json.dumps({"format_version": True, "provider": "PacketStream", "country": "EG", "username": USERNAME, "auth_key": AUTH_KEY}).encode(),
    json.dumps({"format_version": 1, "provider": "PacketStream", "country": "US", "username": USERNAME, "auth_key": AUTH_KEY}).encode(),
    json.dumps({"format_version": 1, "provider": "Other", "country": "EG", "username": USERNAME, "auth_key": AUTH_KEY}).encode(),
    json.dumps({"format_version": 1, "provider": "PacketStream", "country": "EG", "username": USERNAME, "auth_key": AUTH_KEY, "session_label": "stale"}).encode(),
    json.dumps({"format_version": 1, "provider": "PacketStream", "country": "EG", "username": USERNAME, "auth_key": AUTH_KEY + "_session-old"}).encode(),
])
def test_invalid_saved_configuration_fails_closed(payload, monkeypatch):
    monkeypatch.setattr(proxy.store, "load_protected_bytes", lambda _: payload)
    with pytest.raises(SessionError, match="configuration is invalid") as error:
        proxy.load_packetstream_proxy()
    assert_redacted(error.value)


class Reply:
    def __init__(self, *, status=200, payload=None, infos=None):
        self.status_code = status
        self.payload = {"country": "EG", "private": AUTH_KEY} if payload is None else payload
        self.infos = {CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 200} if infos is None else infos
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self):
        self.closed = True


@pytest.fixture
def offline_http(monkeypatch):
    state = {"instances": [], "reply": Reply(), "error": None}

    class FakeSession:
        def __init__(self, **options):
            self.options = options
            self.calls = []
            self.closed = False
            state["instances"].append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.closed = True

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            assert len(self.calls) == 1
            if state["error"] is not None:
                raise state["error"]
            return state["reply"]

    monkeypatch.setattr(proxy.requests, "Session", FakeSession)
    return state


def test_country_verification_is_one_fresh_cookie_free_verified_proxy_request(offline_http, capsys):
    config = proxy.PacketStreamProxy(USERNAME, AUTH_KEY)
    report = config.verify_country()
    assert report == {
        **config.summary(), "country_verified": True, "proxy_used": True,
        "http_status": 200, "proxy_connect_http_status": 200,
    }
    assert_redacted(json.dumps(report))
    assert "private" not in report
    assert capsys.readouterr().out == ""
    transport, = offline_http["instances"]
    assert transport.options == config.transport_options()
    assert "cookies" not in transport.options and "headers" not in transport.options
    assert transport.calls == [("https://ipinfo.io/json", {"timeout": 25, "allow_redirects": False})]
    assert transport.closed and offline_http["reply"].closed


@pytest.mark.parametrize("reply", [
    Reply(status=302), Reply(status=403), Reply(status=500), Reply(status=200.0),
    Reply(infos={}), Reply(infos={CurlInfo.USED_PROXY: 0, CurlInfo.HTTP_CONNECTCODE: 200}),
    Reply(infos={CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 0}),
    Reply(infos={CurlInfo.USED_PROXY: True, CurlInfo.HTTP_CONNECTCODE: 200}),
    Reply(infos={CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 200.0}),
    Reply(payload={"country": "US"}), Reply(payload={"country": "eg"}),
    Reply(payload={}), Reply(payload=[]), Reply(payload={"country": AUTH_KEY}),
    Reply(payload={"country": "EG", "error": {"private": AUTH_KEY}}),
    Reply(payload=ValueError(AUTH_KEY + PRIVATE_URL)),
])
def test_country_or_route_failure_never_falls_back_or_leaks(reply, offline_http):
    offline_http["reply"] = reply
    with pytest.raises(SessionError) as error:
        proxy.PacketStreamProxy(USERNAME, AUTH_KEY).verify_country()
    assert_redacted(error.value)
    transport, = offline_http["instances"]
    assert len(transport.calls) == 1
    assert transport.options["retry"] == 0
    assert transport.closed and reply.closed


@pytest.mark.parametrize("error", [
    RuntimeError(USERNAME + AUTH_KEY + PRIVATE_URL),
    SessionError(USERNAME + AUTH_KEY + PRIVATE_URL),
])
def test_transport_exceptions_are_redacted_even_if_they_use_session_error(error, offline_http):
    offline_http["error"] = error
    with pytest.raises(SessionError) as failure:
        proxy.PacketStreamProxy(USERNAME, AUTH_KEY).verify_country()
    assert_redacted(failure.value)
    assert "No direct connection" in str(failure.value)
    transport, = offline_http["instances"]
    assert transport.closed and len(transport.calls) == 1


@pytest.mark.parametrize("kind", ["http_status", "connect_status", "exception_status", "exception_text"])
def test_proxy_authentication_407_is_classified_without_secrets(kind, offline_http):
    if kind == "http_status":
        offline_http["reply"] = Reply(status=407, payload={"secret": AUTH_KEY})
    elif kind == "connect_status":
        offline_http["reply"] = Reply(infos={CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 407})
    elif kind == "exception_status":
        failure = RuntimeError(USERNAME + AUTH_KEY + PRIVATE_URL)
        failure.response = Reply(status=0, infos={CurlInfo.HTTP_CONNECTCODE: 407})
        offline_http["error"] = failure
    else:
        offline_http["error"] = RuntimeError("CONNECT tunnel failed, response 407. " + USERNAME + AUTH_KEY + PRIVATE_URL)
    with pytest.raises(SessionError, match="authentication was rejected.*407") as error:
        proxy.PacketStreamProxy(USERNAME, AUTH_KEY).verify_country()
    assert_redacted(error.value)
    transport, = offline_http["instances"]
    assert len(transport.calls) == 1 and transport.closed
    if kind == "exception_status":
        assert offline_http["error"].response.closed
