"""Regression tests use synthetic sessions and never contact Anghami."""

from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import json
import os
import subprocess
import sys

import pytest

from anghami_session import AnghamiSession, SessionError
from anghami_session import client, store
from anghami_session import __main__ as cli

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-session-do-not-log"


@pytest.fixture
def bundle():
    return {
        "format_version": 1,
        "created_at_utc": "2026-09-29T00:00:00+00:00",
        "origin": "https://play.anghami.com",
        "requests": {
            name: {
                "method": "GET",
                "url": f"{client.GATEWAY_URL}?type={operation}&angh_type={operation}&sid={TOKEN}&fingerprint=test-device",
                "headers": {
                    "Cookie": f"appsidsave={TOKEN}; fingerprint=test-device",
                    "User-Agent": "Captured Chrome agent",
                    "Origin": "https://play.anghami.com",
                },
            }
            for name, operation in client.OPERATIONS.items()
        },
    }


class Reply:
    def __init__(self, payload=None, status=200):
        self.payload = {"status": "ok"} if payload is None else payload
        self.status_code = status

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture
def transport(monkeypatch):
    instances = []
    replies = []

    class FakeTransport:
        def __init__(self, **options):
            self.options = options
            self.calls = []
            self.closed = False
            instances.append(self)

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(client.requests, "Session", FakeTransport)
    return instances, replies


def test_replays_authenticated_reads_and_uses_an_isolated_negative_control(bundle, transport):
    instances, replies = transport
    bundle["requests"]["relations"]["url"] += f"&appsid={TOKEN}"
    headers = bundle["requests"]["relations"]["headers"]
    headers.update({":authority": "ignored", "Host": "ignored", "Content-Length": "999", "X-ANGH-SESSION": TOKEN})
    replies.extend([Reply(), Reply(), Reply({"status": "failed"})])
    with AnghamiSession(saved=bundle) as session:
        report = session.check(negative_control=True)
    assert report["authenticated"] is True
    assert report["operations"] == {"relations": "ok", "playlists": "ok"}
    assert report["without_session"]["authentication_rejected"] is True
    assert TOKEN not in json.dumps(report)
    assert len(instances) == 2
    assert all(instance.closed for instance in instances)
    assert all(instance.options == {"impersonate": "chrome"} for instance in instances)
    url, options = instances[0].calls[0]
    assert parse_qs(urlsplit(url).query)["sid"] == [TOKEN]
    assert options["headers"]["cookie"].startswith("appsidsave=")
    assert options["headers"]["user-agent"] == "Captured Chrome agent"
    assert not any(k.startswith(":") or k in client.IGNORED_HEADERS for k in options["headers"])
    anonymous_url, anonymous_options = instances[1].calls[0]
    assert TOKEN not in anonymous_url + json.dumps(anonymous_options)
    for instance in instances:
        for _, options in instance.calls:
            assert options["allow_redirects"] is False
            assert options["timeout"] == 25


@pytest.mark.parametrize("change", [
    {"method": "POST"},
    {"url": "https://example.com/gateway.php?type=GETuserrelations&sid=test"},
    {"url": "https://coussa.anghami.com:444/gateway.php?type=GETuserrelations&sid=test"},
    {"url": "https://user@coussa.anghami.com/gateway.php?type=GETuserrelations&sid=test"},
    {"url": client.GATEWAY_URL + "?type=authenticate&sid=test"},
    {"url": client.GATEWAY_URL + "?type=GETuserrelations&type=authenticate&sid=test"},
    {"url": client.GATEWAY_URL + "?type=GETuserrelations&angh_type=REGISTERwebplay&sid=test"},
    {"url": client.GATEWAY_URL + "?type=GETuserrelations&sid=test&p=secret"},
    {"url": client.GATEWAY_URL + "?type=GETuserrelations"},
    {"headers": {"Cookie": "value\r\nInjected: yes"}},
])
def test_invalid_templates_are_rejected_before_network(bundle, transport, change):
    instances, _ = transport
    bundle["requests"]["relations"].update(change)
    with pytest.raises(SessionError):
        AnghamiSession(saved=bundle)
    assert instances == []


@pytest.mark.parametrize("reply", [
    Reply({"status": "failed"}),
    Reply(status=403),
    Reply(status=302),
    Reply(["unexpected"]),
    Reply(ValueError("bad JSON " + TOKEN)),
    RuntimeError("network exception with " + TOKEN),
])
def test_failed_requests_are_not_mistaken_for_login_or_leaked(bundle, transport, reply):
    _, replies = transport
    replies.append(reply)
    with AnghamiSession(saved=bundle) as session:
        with pytest.raises(SessionError) as error:
            session.request()
    assert TOKEN not in str(error.value)


@pytest.mark.parametrize("control", [Reply(), Reply({"status": "failed"}, status=403)])
def test_control_must_reject_authentication_not_just_fail_at_http(bundle, transport, control):
    _, replies = transport
    replies.extend([Reply(), Reply(), control])
    with AnghamiSession(saved=bundle) as session:
        with pytest.raises(SessionError, match="unauthenticated control"):
            session.check(negative_control=True)


def test_missing_operation_is_explicit_and_does_not_make_a_request(bundle, transport):
    instances, _ = transport
    del bundle["requests"]["playlists"]
    with AnghamiSession(saved=bundle) as session:
        with pytest.raises(SessionError, match="No captured request"):
            session.request("playlists")
    assert not instances[0].calls


def test_failed_replacement_keeps_previous_file(bundle, transport, tmp_path):
    _, replies = transport
    replies.append(Reply({"status": "failed"}))
    path = tmp_path / "session.dpapi"
    path.write_bytes(b"previous-encrypted-session")
    with pytest.raises(SessionError):
        cli.save_verified_session(bundle, path)
    assert path.read_bytes() == b"previous-encrypted-session"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration")
def test_dpapi_round_trip_has_no_plaintext_and_atomic_failure_preserves_file(bundle, monkeypatch, tmp_path):
    path = tmp_path / "session.dpapi"
    store.save_session(bundle, path)
    assert store.load_session(path) == bundle
    encrypted = path.read_bytes()
    assert TOKEN.encode() not in encrypted
    assert client.GATEWAY_URL.encode() not in encrypted

    def fail_replace(*_):
        raise OSError("simulated file replacement failure")

    monkeypatch.setattr(store.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.save_session({"replacement": True}, path)
    assert path.read_bytes() == encrypted
    assert not list(tmp_path.glob("*.dpapi.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration")
def test_cli_import_validates_and_stores_only_session_fields(bundle, transport, tmp_path, capsys):
    _, replies = transport
    replies.extend([Reply(), Reply(), Reply({"status": "failed"})])
    source = tmp_path / "source.dpapi"
    destination = tmp_path / "local" / "session.dpapi"
    original = deepcopy(bundle)
    original["password"] = "must-not-be-imported"
    store.save_session(original, source)
    assert cli.main(["import", "--source", str(source), "--session", str(destination)]) == 0
    assert store.load_session(destination) == client.validate_session(bundle)
    assert store.load_session(source) == original
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    report = json.loads((destination.parent / "last-check.json").read_text())
    assert report["authenticated"] is True


def test_cli_missing_session_is_actionable_and_does_not_start_transport(tmp_path, transport, capsys):
    instances, _ = transport
    assert cli.main(["check", "--session", str(tmp_path / "missing.dpapi")]) == 1
    assert "No session" in capsys.readouterr().err
    assert not instances


def test_main_entrypoint_uses_session_cli_without_loading_browser(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "check", "--session", str(tmp_path / "missing.dpapi")],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1
    assert "No session" in result.stderr
    # Importing the HTTP client must not require optional browser dependencies.
    result = subprocess.run(
        [sys.executable, "-c", "import anghami_session, sys; assert 'cloakbrowser' not in sys.modules; assert 'playwright' not in sys.modules"],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_existing_sid_renewal_marker_is_retained_without_unrelated_private_fields(bundle, transport):
    bundle.update(account_email="OWNED@example.com", renewal_method="saved_sid", password="private-unused-password")
    saved = client.validate_session(bundle)
    assert saved["renewal_method"] == "saved_sid"
    assert saved["account_email"] == "owned@example.com"
    assert "password" not in saved
    assert "renewal_method" not in client.validate_session({key: value for key, value in bundle.items() if key != "renewal_method"})


@pytest.mark.parametrize("method", [None, True, 1, {}, "password", "cookies", "undefined", "private-secret"])
def test_unsupported_renewal_method_is_rejected_before_transport_without_leaking(bundle, transport, method):
    instances, _ = transport
    bundle.update(account_email="owned@example.com", renewal_method=method)
    with pytest.raises(SessionError, match="renewal method") as error:
        AnghamiSession(saved=bundle)
    assert not instances
    assert "private-secret" not in str(error.value)


def test_renewal_marker_requires_captured_account_identity_before_transport(bundle, transport):
    instances, _ = transport
    bundle["renewal_method"] = "saved_sid"
    with pytest.raises(SessionError, match="renewal method"):
        AnghamiSession(saved=bundle)
    assert not instances
