"""Existing-session recovery uses synthetic accounts and never makes live calls."""

from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, quote_plus, urlsplit
import json

from curl_cffi import CurlInfo, CurlOpt
import pytest

from anghami_session import session_recovery as recovery
from anghami_session import media_gateway as gateway
from anghami_session.errors import SessionError
from test_media_gateway import decrypt_request

SID = "private-source-session"
NEW_SID = "private-server-issued-session"
FINGERPRINT = "private-raw%2Fdevice+fingerprint=tail"
PASSWORD = "private-password-never-used"
SERVER_COOKIE = "private-server-cookie"


@pytest.fixture
def record():
    return {
        "source_row": 25, "country": "LB", "email": "owned@example.com", "password": PASSWORD,
        "legacy_metadata": {"appsidsave": SID, "session_fingerprint": FINGERPRINT, "session_uuid": "private-uuid"},
        "legacy_cookies": {"appsidsave": SID, "fingerprint": FINGERPRINT, "ssss": "private-old-cookie"},
    }


class Reply:
    def __init__(self, payload, *, route=True, status=200, cookie_updates=()):
        self.payload = payload
        self.status_code = status
        self.infos = {CurlInfo.USED_PROXY: 1 if route else 0, CurlInfo.HTTP_CONNECTCODE: 200 if route else 0}
        self.cookie_updates = cookie_updates

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)


def cookie(name="ssss", value=SERVER_COOKIE, domain=".anghami.com", path="/", expires=None):
    return SimpleNamespace(name=name, value=value, domain=domain, path=path, expires=expires)


@pytest.fixture
def transport(monkeypatch):
    replies = [
        Reply({"status": "ok"}), Reply({"status": "ok"}), Reply({"status": "failed"}),
        Reply({"status": "ok", "email": "OWNED@example.com "}, cookie_updates=[cookie()]),
        Reply({"status": "ok", "authenticate": {
            "email": "owned@example.com", "reqkey": "R" * 32, "reskey": "S" * 32,
            "signingkey": "private-signing-key", "socketsessionid": NEW_SID,
            "password": PASSWORD,
        }}, cookie_updates=[cookie("appsidsave", "private-renewed-server-cookie")]),
        Reply({"status": "ok"}), Reply({"status": "ok"}), Reply({"status": "failed"}),
    ]
    instances, calls = [], []

    class FakeTransport:
        def __init__(self, **options):
            self.options, self.closed = options, False
            self.cookies = SimpleNamespace(jar=[])
            instances.append(self)

        def _request(self, method, url, options):
            calls.append((self, method, url, deepcopy(options)))
            assert replies, "No extra request is authorized"
            response = replies.pop(0)
            if isinstance(response, Exception):
                raise response
            for item in response.cookie_updates:
                self.cookies.jar = [prior for prior in self.cookies.jar if (prior.name, prior.domain, prior.path) != (item.name, item.domain, item.path)] + [item]
            return response

        def get(self, url, **options):
            return self._request("GET", url, options)

        def post(self, url, **options):
            return self._request("POST", url, options)

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(recovery.requests, "Session", FakeTransport)
    monkeypatch.setattr(gateway.time, "time", lambda: 1700000000)
    return replies, instances, calls


class Proxy:
    country = "EG"

    def __init__(self):
        self.verified = 0

    def verify_country(self):
        self.verified += 1
        return {"country": "EG", "country_verified": True, "proxy_used": True}

    def transport_options(self):
        return {"proxy": "https://synthetic-proxy.invalid", "proxy_auth": ("synthetic-user", "private-proxy-secret")}

    def summary(self):
        return {"country": "EG"}


@pytest.mark.parametrize("use_proxy", [False, True])
def test_normal_recovery_binds_server_identity_renews_existing_sid_and_checks_final_templates(record, transport, use_proxy, capsys):
    replies, instances, calls = transport
    original = deepcopy(record)
    proxy = Proxy() if use_proxy else None
    saved, metadata = recovery.recover_legacy_session(record, proxy=proxy)
    assert record == original
    assert not replies
    assert all(item.closed for item in instances)
    assert len(instances) == 3  # Main action plus two isolated anonymous controls.
    assert saved["renewal_method"] == "saved_sid"
    assert saved["account_email"] == "owned@example.com"
    for template in saved["requests"].values():
        query = parse_qs(urlsplit(template["url"]).query)
        assert query["sid"] == query["appsid"] == [NEW_SID]
        assert query["fingerprint"] == [FINGERPRINT]
        assert "appsidsave=private-renewed-server-cookie" in template["headers"]["cookie"]
        assert "ssss=" + SERVER_COOKIE in template["headers"]["cookie"]
        assert "fingerprint=" + FINGERPRINT in template["headers"]["cookie"]
        assert template["headers"]["x-angh-session"] == NEW_SID
    _, _, profile_url, profile_options = calls[3]
    profile_query = parse_qs(urlsplit(profile_url).query)
    assert profile_query["type"] == ["GETprofile"] and "id" not in profile_query
    assert profile_query["sid"] == [SID]
    _, method, auth_url, auth = calls[4]
    assert method == "POST" and auth_url == recovery.GATEWAY_URL
    assert auth["params"]["type"] == "authenticate"
    assert auth["params"]["sid"] == auth["params"]["appsid"] == SID
    assert "re_token" not in auth["params"]
    assert auth["headers"]["x-angh-session"] == SID
    assert decrypt_request(auth["data"], gateway._derive_key(FINGERPRINT, 1700000000, request=True)) == (
        "reauthenticate=true&sid=" + SID + "&output=jsonhp&devicename=Chrome 103&re_token=undefined"
    )
    for index in (2, 7):
        instance, _, url, options = calls[index]
        assert instance is not instances[0]
        assert not instance.cookies.jar
        assert "sid" not in parse_qs(urlsplit(url).query)
        assert "appsid" not in parse_qs(urlsplit(url).query)
        assert "cookie" not in options["headers"] and "x-angh-session" not in options["headers"]
    for _, _, _, options in calls:
        assert options["timeout"] == 25 and options["allow_redirects"] is False
        assert PASSWORD not in str(options)
    if proxy is not None:
        assert proxy.verified == 1
        assert all(item.options == proxy.transport_options() for item in instances)
    else:
        assert all(item.options["curl_options"] == {CurlOpt.PROXY: "", CurlOpt.NOPROXY: "*"} for item in instances)
    assert metadata["authenticated"] and metadata["identity_verified"] and metadata["session_renewed"]
    assert not metadata["automatic_retry"] and not metadata["browser_required"] and not metadata["password_required"]
    output = capsys.readouterr()
    rendered = json.dumps(metadata) + output.out + output.err
    for secret in (SID, NEW_SID, FINGERPRINT, PASSWORD, SERVER_COOKIE, "private-signing-key", "private-proxy-secret", "owned@example.com"):
        assert secret not in rendered


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(source_row=True),
    lambda value: value["legacy_metadata"].update(appsidsave="other-private-session"),
    lambda value: value["legacy_metadata"].update(session_fingerprint="other-private-device"),
    lambda value: value["legacy_cookies"].update(fingerprint="undefined"),
    lambda value: value["legacy_cookies"].update(ssss="private\r\ninjected"),
    lambda value: value["legacy_cookies"].update(ssss="private; injected=yes"),
    lambda value: value["legacy_cookies"].update({"bad cookie": "private"}),
    lambda value: value.update(legacy_metadata={}),
    lambda value: value.update(legacy_cookies={}),
    lambda value: value.update(email="owned@example.com\nprivate"),
    lambda value: value["legacy_metadata"].update(appsidsave="private&u=injected"),
])
def test_invalid_or_mismatched_source_stops_before_transport_or_proxy(record, transport, mutation):
    _, instances, calls = transport
    mutation(record)
    proxy = Proxy()
    with pytest.raises(SessionError) as error:
        recovery.recover_legacy_session(record, proxy=proxy)
    assert not instances and not calls and not proxy.verified
    assert "private" not in str(error.value)


def test_legacy_encoded_cookie_sid_is_compared_without_changing_raw_fingerprint(record, transport):
    record["legacy_metadata"]["appsidsave"] = "private/session=tail"
    record["legacy_cookies"]["appsidsave"] = quote_plus("private/session=tail")
    _, _, calls = transport
    recovery.recover_legacy_session(record)
    assert parse_qs(urlsplit(calls[0][2]).query)["sid"] == ["private/session=tail"]
    assert "appsidsave=private%2Fsession%3Dtail" in calls[0][3]["headers"]["cookie"]


@pytest.mark.parametrize("index", range(8))
def test_each_proxied_read_control_profile_and_renewal_requires_route_proof(record, transport, index):
    replies, instances, calls = transport
    replies[index].infos[CurlInfo.USED_PROXY] = 0
    with pytest.raises(SessionError):
        recovery.recover_legacy_session(record, proxy=Proxy())
    assert len(calls) == index + 1
    assert all(item.closed for item in instances)


@pytest.mark.parametrize("index,payload", [
    (0, {"status": "failed"}), (2, {"status": "ok"}),
    (3, {"status": "ok", "email": "other@example.com"}),
    (3, {"status": "failed", "email": "owned@example.com"}),
    (4, {"status": "failed", "error": {"message": PASSWORD}}),
    (4, {"status": "ok", "authenticate": {"email": "other@example.com"}}),
    (5, {"status": "failed"}), (7, {"status": "ok"}),
])
def test_expired_wrong_identity_verification_or_final_check_failure_returns_no_session(record, transport, index, payload, capsys):
    replies, instances, calls = transport
    replies[index].payload = payload
    original = deepcopy(record)
    with pytest.raises(SessionError) as error:
        recovery.recover_legacy_session(record)
    assert len(calls) == index + 1
    assert record == original
    assert all(item.closed for item in instances)
    output = capsys.readouterr()
    assert not any(secret in str(error.value) + output.out + output.err for secret in (SID, NEW_SID, FINGERPRINT, PASSWORD))


@pytest.mark.parametrize("index", [0, 3, 4])
def test_transport_failure_has_no_secret_error_or_retry(record, transport, index):
    replies, instances, calls = transport
    replies[index] = RuntimeError("private transport URL=" + SID + " cookie=" + PASSWORD)
    with pytest.raises(SessionError) as error:
        recovery.recover_legacy_session(record)
    assert SID not in str(error.value) and PASSWORD not in str(error.value)
    assert len(calls) == index + 1 and all(item.closed for item in instances)


def test_proxy_preflight_failure_stops_without_auth_and_does_not_fallback(record, transport):
    _, instances, calls = transport
    proxy = Proxy()
    proxy.verify_country = lambda: {"country": "US", "country_verified": True, "proxy_used": True}
    with pytest.raises(SessionError, match="proxy country check failed"):
        recovery.recover_legacy_session(record, proxy=proxy)
    assert not calls and not instances


@pytest.mark.parametrize("untrusted", [
    cookie(domain="foreign.invalid"), cookie(path="/other"), cookie(value="private; inject=yes"),
    cookie(name="fingerprint", value="different-fingerprint"), cookie(name="authorization", value="private"),
    cookie(expires=1),
])
def test_cookie_merger_ignores_foreign_untrusted_paths_names_values_and_expiry(record, transport, untrusted):
    replies, _, _ = transport
    replies[3].cookie_updates = [untrusted]
    saved, _ = recovery.recover_legacy_session(record)
    for template in saved["requests"].values():
        assert "fingerprint=" + FINGERPRINT in template["headers"]["cookie"]
        assert "ssss=private-old-cookie" in template["headers"]["cookie"]
        assert "different-fingerprint" not in template["headers"]["cookie"]
        assert "authorization=" not in template["headers"]["cookie"]
