"""Normal media protocol checks with synthetic sessions and a fake HTTP transport."""

from base64 import b64encode
from copy import deepcopy
from gzip import compress, decompress
from hashlib import md5
import json
from types import SimpleNamespace

import pytest
from pysodium import crypto_aead_chacha20poly1305_decrypt, crypto_aead_chacha20poly1305_encrypt

from anghami_session import media_gateway as gateway
from anghami_session.errors import SessionError


TIMESTAMP = 1700000000
RAW_FINGERPRINT = "AbC%2Fraw+fingerprint=tail"
CAPTURED_FINGERPRINT = "captured-uuid-fingerprint"
STALE_SESSION = "stale-query-session"
COOKIE_SESSION = "private-cookie-session"
REQUEST_KEY = "R" * 32
RESPONSE_KEY = "S" * 32
SIGNING_KEY = "private-synthetic-signing-key"
SOCKET_ID = "private-issued-socket-session"
PASSWORD = "private-synthetic-password"
AGENT = "Mozilla/5.0 Chrome/152.0.0.0 Safari/537.36"
MEDIA_URL = "https://media.anghami.com/synthetic-song.mp3"


def encrypted_response(payload, key):
    """Build a real SDK response envelope independently of gateway._encrypt."""
    nonce, associated = bytes(range(8)), bytes(range(12))
    plaintext = compress(json.dumps(payload).encode("utf-8"))
    ciphertext = crypto_aead_chacha20poly1305_encrypt(plaintext, associated, nonce, key)
    return b64encode(b"##" + nonce + associated + ciphertext).decode("ascii")


def decrypt_request(body, key):
    assert body.startswith(b"##")
    decoded = crypto_aead_chacha20poly1305_decrypt(body[22:], body[10:22], body[2:10], key)
    return decompress(decoded).decode("utf-8")


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.replies = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.replies:
            raise AssertionError("No unplanned network request is permitted")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def session():
    template = {
        "url": (
            gateway.GATEWAY_URL + "?type=GETuserrelations&lang=en&language=en"
            "&userlanguageprod=en&web2=true&sid=" + STALE_SESSION
            + "&appsid=" + STALE_SESSION + "&fingerprint=" + CAPTURED_FINGERPRINT
        ),
        "headers": {
            "cookie": "appsidsave=" + COOKIE_SESSION + "; fingerprint=" + RAW_FINGERPRINT + "; ssss=synthetic",
            "user-agent": AGENT,
            "origin": "https://play.anghami.com",
            "x-angh-session": STALE_SESSION,
        },
    }
    return SimpleNamespace(
        _saved={"account_email": "owned@example.com"},
        _template=lambda operation: template if operation == "relations" else None,
        _http=FakeHTTP(),
        _require_proxy_route=lambda response: None,
    )


@pytest.fixture
def authentication():
    return {
        "email": "OWNED@example.com ", "reqkey": REQUEST_KEY,
        "reskey": RESPONSE_KEY, "signingkey": SIGNING_KEY,
        "socketsessionid": SOCKET_ID,
        "password": PASSWORD,
    }


@pytest.fixture(autouse=True)
def frozen_time(monkeypatch):
    monkeypatch.setattr(gateway.time, "time", lambda: TIMESTAMP + 0.123)


@pytest.mark.parametrize("fingerprint,is_request,expected", [
    ("ABC-Test", True, "80f94722809dfcd9c92bc25e539df8d7"),
    ("ABC-Test", False, "5883e02b91fa0e42e4c2f463a69f159c"),
    ("\U0001d504BC-Test", True, "52d1ee38347d1e7fb9a48bc2e743e309"),
    ("\U0001d504BC-Test", False, "0d7c54206ef4df5b4d84a2e309d9bb6a"),
])
def test_timestamp_key_vectors_match_sdk_hex_bytes_and_utf16_units(fingerprint, is_request, expected):
    key = gateway._derive_key(fingerprint, TIMESTAMP, request=is_request)
    assert key == expected.encode("ascii")
    assert len(key) == 32
    assert key != bytes.fromhex(expected)
    assert gateway._derive_key(fingerprint, TIMESTAMP + 1, request=is_request) != key


def test_real_request_envelope_preserves_literal_serialization_and_insertion_order():
    payload = {"first": "A+B%2F=x", "reauthenticate": "true", "re_token": "undefined", "last": "space value"}
    body = gateway._encrypt(payload, REQUEST_KEY.encode())
    assert body.startswith(b"##")
    assert len(body[2:10]) == 8
    assert len(body[10:22]) == 12
    assert decrypt_request(body, REQUEST_KEY.encode()) == (
        "first=A+B%2F=x&reauthenticate=true&re_token=undefined&last=space value"
    )


def test_real_response_envelope_decrypts_and_rejects_tampering_or_wrong_key():
    payload = {"status": "ok", "requestedfileid": "42", "sections": [{"data": [{"location": MEDIA_URL}]}]}
    reply = encrypted_response(payload, RESPONSE_KEY.encode())
    assert gateway._decrypt(reply, RESPONSE_KEY.encode()) == payload
    with pytest.raises((ValueError, RuntimeError)):
        gateway._decrypt(reply, REQUEST_KEY.encode())
    from base64 import b64decode
    damaged = bytearray(b64decode(reply))
    damaged[-1] ^= 1
    with pytest.raises((ValueError, RuntimeError)):
        gateway._decrypt(b64encode(damaged).decode(), RESPONSE_KEY.encode())


def test_bootstrap_and_media_use_separate_normal_keys_and_no_stale_explicit_session(session, authentication, capsys):
    response_key = gateway._derive_key(RAW_FINGERPRINT, TIMESTAMP, request=False)
    session._http.replies.extend([
        Response({"reply": encrypted_response({"status": "ok", "authenticate": authentication}, response_key)}),
        Response({"reply": encrypted_response({
            "status": "ok", "requestedfileid": "42", "sections": [{"data": [{"location": MEDIA_URL}]}],
        }, RESPONSE_KEY.encode())}),
    ])
    media = gateway.PlaybackGateway(session)
    assert media.fingerprint == RAW_FINGERPRINT
    result = media.media_source(42)
    assert result == {"location": MEDIA_URL, "song_id": "42"}
    assert len(session._http.calls) == 2
    bootstrap_url, bootstrap = session._http.calls[0]
    assert bootstrap_url == gateway.GATEWAY_URL
    assert bootstrap["params"]["fingerprint"] == RAW_FINGERPRINT
    assert bootstrap["params"]["type"] == "authenticate"
    assert "sid" not in bootstrap["params"]
    assert "appsid" not in bootstrap["params"]
    assert "re_token" not in bootstrap["params"]
    assert bootstrap["headers"]["x-angh-session"] != STALE_SESSION
    assert bootstrap["headers"]["x-angh-udid"] == RAW_FINGERPRINT.lower()
    assert bootstrap["headers"]["cookie"] == session._template("relations")["headers"]["cookie"]
    assert bootstrap["headers"]["x-angh-ts"] == str(TIMESTAMP)
    assert bootstrap["headers"]["x-angh-encpayload"] == "3"
    request_key = gateway._derive_key(RAW_FINGERPRINT, TIMESTAMP, request=True)
    assert decrypt_request(bootstrap["data"], request_key) == (
        "reauthenticate=true&output=jsonhp&devicename=Chrome 152&re_token=undefined"
    )
    _, download = session._http.calls[1]
    assert download["params"]["type"] == "GETdownload"
    assert download["params"]["sid"] == SOCKET_ID
    assert download["params"]["appsid"] == SOCKET_ID
    assert download["headers"]["x-angh-session"] == SOCKET_ID
    timestamp_ms = str(int((TIMESTAMP + 0.123) * 1000))
    expected_signature = md5((timestamp_ms + b64encode(AGENT.encode("latin-1")).decode() + SIGNING_KEY).encode()).hexdigest()
    assert download["params"]["ts"] == timestamp_ms
    assert download["params"]["ts_hashed"] == expected_signature
    assert decrypt_request(download["data"], REQUEST_KEY.encode()) == (
        "fileid=42&HQ=64&output=jsonhp&retry=0&ts=" + timestamp_ms + "&ts_hashed=" + expected_signature
    )
    for _, request in session._http.calls:
        assert request["timeout"] == 25
        assert request["allow_redirects"] is False
    assert set(media.tokens) == {"reqkey", "reskey", "signingkey", "socketsessionid"}
    assert "password" not in media.tokens
    output = capsys.readouterr()
    rendered = json.dumps(result) + output.out + output.err
    for secret in (SIGNING_KEY, PASSWORD, REQUEST_KEY, RESPONSE_KEY, SOCKET_ID, COOKIE_SESSION):
        assert secret not in rendered


@pytest.mark.parametrize("cookie", ["", "appsidsave=synthetic", "fingerprint=undefined"])
def test_missing_media_fingerprint_fails_before_transport(session, cookie):
    session._template("relations")["headers"]["cookie"] = cookie
    with pytest.raises(SessionError, match="fingerprint cookie"):
        gateway.PlaybackGateway(session)
    assert not session._http.calls


@pytest.mark.parametrize("identity", [None, "", "other@example.com"])
def test_bootstrap_requires_matching_selected_account(session, authentication, monkeypatch, identity):
    authentication["email"] = identity
    media = gateway.PlaybackGateway(session)
    monkeypatch.setattr(media, "_post", lambda *args, **kwargs: {"status": "ok", "authenticate": authentication})
    with pytest.raises(SessionError, match="identify the selected account"):
        media.bootstrap()
    assert media.tokens is None
    assert not session._http.calls


@pytest.mark.parametrize("key", ["reqkey", "reskey", "signingkey", "socketsessionid"])
def test_bootstrap_rejects_missing_required_key(session, authentication, monkeypatch, key):
    del authentication[key]
    media = gateway.PlaybackGateway(session)
    monkeypatch.setattr(media, "_post", lambda *args, **kwargs: {"status": "ok", "authenticate": authentication})
    with pytest.raises(SessionError, match="required playback keys"):
        media.bootstrap()
    assert media.tokens is None


@pytest.mark.parametrize("key", ["reqkey", "reskey"])
def test_bootstrap_rejects_unsupported_key_length(session, authentication, monkeypatch, key):
    authentication[key] = "short-private-key"
    media = gateway.PlaybackGateway(session)
    monkeypatch.setattr(media, "_post", lambda *args, **kwargs: {"status": "ok", "authenticate": authentication})
    with pytest.raises(SessionError, match="unsupported playback keys") as error:
        media.bootstrap()
    assert "short-private-key" not in str(error.value)
    assert media.tokens is None


@pytest.mark.parametrize("identification", [{"song": {"id": 42}}, {"requestedfileid": "42"}])
@pytest.mark.parametrize("nested", [False, True])
def test_matching_song_or_requested_file_id_accepts_real_response_layouts(session, authentication, monkeypatch, identification, nested):
    media = gateway.PlaybackGateway(session)
    media.tokens = authentication
    payload = {"status": "ok", **identification}
    payload.update({"sections": [{"data": [{"location": MEDIA_URL}]}]} if nested else {"location": MEDIA_URL})
    monkeypatch.setattr(media, "_post", lambda *args, **kwargs: payload)
    assert media.media_source("42") == {"song_id": "42", "location": MEDIA_URL}


@pytest.mark.parametrize("identification", [
    {}, {"song": {"id": "43"}}, {"requestedfileid": "43"},
    {"song": {"id": "43"}, "requestedfileid": "42"},
])
def test_media_response_cannot_be_assigned_to_another_song(session, authentication, monkeypatch, identification):
    media = gateway.PlaybackGateway(session)
    media.tokens = authentication
    monkeypatch.setattr(media, "_post", lambda *args, **kwargs: {"status": "ok", "location": MEDIA_URL, **identification})
    with pytest.raises(SessionError, match="match the requested song"):
        media.media_source("42")


@pytest.mark.parametrize("song_id", ["", "-1", "1&sid=other", "abc", "\u0661", "1" * 21])
def test_invalid_song_id_is_rejected_before_authentication(session, monkeypatch, song_id):
    media = gateway.PlaybackGateway(session)

    def forbidden():
        raise AssertionError("An invalid song must not authenticate")

    monkeypatch.setattr(media, "bootstrap", forbidden)
    with pytest.raises(SessionError, match="decimal digits"):
        media.media_source(song_id)
    assert not session._http.calls


@pytest.mark.parametrize("reply", [
    RuntimeError("transport log with " + SIGNING_KEY + " and " + COOKIE_SESSION),
    Response(ValueError("JSON log with " + PASSWORD)),
    Response({"status": "failed", "error": {"message": SIGNING_KEY + PASSWORD}}),
    Response({"status": "ok", "error": {"message": SIGNING_KEY}}),
    Response({"reply": "not-a-base64-reply-with-private-tokens"}),
    Response({"status": "ok"}, status=403),
])
def test_protocol_and_transport_errors_never_expose_secrets(session, reply, capsys):
    session._http.replies.append(reply)
    media = gateway.PlaybackGateway(session)
    with pytest.raises(SessionError) as error:
        media._post("authenticate", {"reauthenticate": "true"}, authenticate=True)
    output = capsys.readouterr()
    rendered = str(error.value) + output.out + output.err
    for secret in (SIGNING_KEY, PASSWORD, COOKIE_SESSION, RAW_FINGERPRINT, STALE_SESSION):
        assert secret not in rendered


def test_media_gateway_rejects_engagement_operations_and_unvalidated_media_calls(session):
    media = gateway.PlaybackGateway(session)
    for operation in ("REGISTERwebplay", "PUTplaylist", "followARTIST"):
        with pytest.raises(SessionError, match="Unsupported"):
            media._post(operation, {})
    with pytest.raises(SessionError, match="not been validated"):
        media._post("GETdownload", {})
    assert not session._http.calls


def test_marked_existing_sid_bootstrap_uses_standard_explicit_sid_in_body_query_and_header(session, authentication):
    session._saved["renewal_method"] = "saved_sid"
    response_key = gateway._derive_key(RAW_FINGERPRINT, TIMESTAMP, request=False)
    session._http.replies.append(Response({"reply": encrypted_response({"status": "ok", "authenticate": authentication}, response_key)}))
    media = gateway.PlaybackGateway(session)
    media.bootstrap()
    assert len(session._http.calls) == 1
    _, options = session._http.calls[0]
    assert options["params"]["sid"] == options["params"]["appsid"] == STALE_SESSION
    assert options["headers"]["x-angh-session"] == STALE_SESSION
    assert "re_token" not in options["params"]
    assert decrypt_request(options["data"], gateway._derive_key(RAW_FINGERPRINT, TIMESTAMP, request=True)) == (
        "reauthenticate=true&sid=" + STALE_SESSION + "&output=jsonhp&devicename=Chrome 152&re_token=undefined"
    )
    assert media.tokens["socketsessionid"] == SOCKET_ID


@pytest.mark.parametrize("query", [
    "sid=one&appsid=other", "sid=one&sid=one&appsid=one", "sid=&appsid=", "sid=undefined&appsid=undefined",
    "sid=private%26injected&appsid=private%26injected", "sid=private%0D%0Asession&appsid=private%0D%0Asession",
    "sid=" + "x" * 4097 + "&appsid=" + "x" * 4097,
])
def test_marked_bootstrap_requires_one_bounded_consistent_session_id_before_transport(session, query):
    session._saved["renewal_method"] = "saved_sid"
    session._template("relations")["url"] = gateway.GATEWAY_URL + "?type=GETuserrelations&" + query
    with pytest.raises(SessionError, match="renewal session identifier") as error:
        gateway.PlaybackGateway(session)
    assert not session._http.calls
    assert "private" not in str(error.value)


@pytest.mark.parametrize("key,value", [
    ("signingkey", "private\r\ninjected"), ("socketsessionid", "private\x00session"),
    ("signingkey", "x" * 4097), ("socketsessionid", "x" * 4097),
])
def test_bootstrap_rejects_control_characters_and_unbounded_tokens(session, authentication, monkeypatch, key, value):
    authentication[key] = value
    media = gateway.PlaybackGateway(session)
    monkeypatch.setattr(media, "_post", lambda *args, **kwargs: {"status": "ok", "authenticate": authentication})
    with pytest.raises(SessionError, match="required playback keys") as error:
        media.bootstrap()
    assert "private" not in str(error.value)
    assert media.tokens is None


@pytest.mark.parametrize("operation,authenticate", [("authenticate", True), ("GETdownload", False)])
def test_proxy_route_is_required_for_bootstrap_and_media_requests(session, authentication, operation, authenticate):
    from curl_cffi import CurlInfo
    from anghami_session.client import AnghamiSession
    session._proxy = object()
    session._require_proxy_route = lambda response: AnghamiSession._require_proxy_route(session, response)
    reply = Response({"status": "ok", "authenticate": authentication})
    reply.infos = {CurlInfo.USED_PROXY: 0, CurlInfo.HTTP_CONNECTCODE: 0}
    session._http.replies.append(reply)
    media = gateway.PlaybackGateway(session)
    if not authenticate:
        media.tokens = authentication
    with pytest.raises(SessionError, match="configured proxy route"):
        media._post(operation, {"reauthenticate": "true"} if authenticate else {}, authenticate=authenticate)
    assert len(session._http.calls) == 1
