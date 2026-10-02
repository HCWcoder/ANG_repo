"""Song reads reuse synthetic saved sessions; these tests make no live requests."""

from pathlib import Path
import subprocess
import sys
import traceback
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from anghami_session import AnghamiSession, SessionError
from anghami_session import client


ROOT = Path(__file__).resolve().parents[1]
SONG_ID = "1280677978"
SECRET = "synthetic-session-secret-must-not-appear"


@pytest.fixture
def saved_session():
    query = {
        "type": "GETuserrelations",
        "angh_type": "GETuserrelations",
        "sid": SECRET,
        "appsid": "synthetic-app-session",
        "fingerprint": "synthetic-device",
        "output": "jsonhp",
        "web2": "true",
        "language": "en",
        "lang": "en",
        "userlanguageprod": "en",
        # These belong to the captured read and must not alter the song request.
        "songId": "111",
        "playsecs": "999",
        "playper": "1",
        "unrelated": "captured-only",
    }
    return {
        "format_version": 1,
        "created_at_utc": "2026-09-30T00:00:00+00:00",
        "origin": "https://play.anghami.com",
        "requests": {
            "relations": {
                "method": "GET",
                "url": client.GATEWAY_URL + "?" + urlencode(query),
                "headers": {
                    "Cookie": "appsidsave=" + SECRET,
                    "Authorization": "Bearer " + SECRET,
                    "X-ANGH-SESSION": SECRET,
                    "User-Agent": "Synthetic captured browser agent",
                    "Origin": "https://play.anghami.com",
                    "Host": "discarded.example",
                },
            },
        },
    }


class Reply:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture
def fake_transport(monkeypatch):
    instances = []
    replies = []

    class Transport:
        def __init__(self, **options):
            self.options = options
            self.calls = []
            self.closed = False
            instances.append(self)

        def get(self, url, **options):
            self.calls.append((url, options))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        def close(self):
            self.closed = True

    monkeypatch.setattr(client.requests, "Session", Transport)
    return instances, replies


def test_song_read_reuses_selected_saved_authentication_without_play_events(saved_session, fake_transport):
    instances, replies = fake_transport
    payload = {"status": 1, "id": SONG_ID, "title": "Synthetic song", "duration": "115"}
    replies.append(Reply(payload))

    with AnghamiSession(saved=saved_session) as session:
        assert session.song(SONG_ID) is payload

    assert len(instances) == 1
    transport = instances[0]
    assert transport.options == {"impersonate": "chrome"}
    assert transport.closed
    assert len(transport.calls) == 1
    url, options = transport.calls[0]
    assert urlsplit(url)._replace(query="").geturl() == client.GATEWAY_URL
    assert parse_qs(urlsplit(url).query) == {
        "sid": [SECRET],
        "appsid": ["synthetic-app-session"],
        "fingerprint": ["synthetic-device"],
        "output": ["jsonhp"],
        "web2": ["true"],
        "language": ["en"],
        "lang": ["en"],
        "userlanguageprod": ["en"],
        "type": ["GETsong"],
        "angh_type": ["GETsong"],
        "songId": [SONG_ID],
    }
    assert options == {
        "headers": {
            "cookie": "appsidsave=" + SECRET,
            "authorization": "Bearer " + SECRET,
            "x-angh-session": SECRET,
            "user-agent": "Synthetic captured browser agent",
            "origin": "https://play.anghami.com",
        },
        "timeout": 25,
        "allow_redirects": False,
    }


@pytest.mark.parametrize("status", [1, "1", "ok"])
@pytest.mark.parametrize("response_id", [SONG_ID, int(SONG_ID)])
def test_song_accepts_success_only_for_matching_song(saved_session, fake_transport, status, response_id):
    _, replies = fake_transport
    payload = {"status": status, "id": response_id}
    replies.append(Reply(payload))
    with AnghamiSession(saved=saved_session) as session:
        assert session.song(int(SONG_ID)) == payload


@pytest.mark.parametrize("payload", [
    {"status": "failed", "id": SONG_ID},
    {"status": 0, "id": SONG_ID},
    {"status": True, "id": SONG_ID},
    {"status": "success", "id": SONG_ID},
    {"id": SONG_ID},
    {"status": "ok"},
    {"status": "ok", "id": "111"},
    {"status": "ok", "id": None},
    {"status": "ok", "id": [SONG_ID]},
    ["ok", SONG_ID],
    None,
])
def test_http_200_is_not_enough_to_identify_a_successful_song(saved_session, fake_transport, payload):
    _, replies = fake_transport
    replies.append(Reply(payload))
    with AnghamiSession(saved=saved_session) as session:
        with pytest.raises(SessionError, match="requested song"):
            session.song(SONG_ID)


@pytest.mark.parametrize("song_id", [
    "", "-1", -1, "1.5", 1.5, " 12", "12 ", "1&sid=other", "123\n",
    "١٢٣", "１２３", "1" * 21, None, True,
])
def test_invalid_song_ids_are_rejected_before_any_request(saved_session, fake_transport, song_id):
    instances, _ = fake_transport
    with AnghamiSession(saved=saved_session) as session:
        with pytest.raises(SessionError, match="Song ID"):
            session.song(song_id)
    assert instances[0].calls == []


@pytest.mark.parametrize("reply", [
    Reply({"status": "failed", "id": SONG_ID, "error": SECRET}, status=403),
    Reply({"redirect": SECRET}, status=302),
    Reply(ValueError("JSON decoding failed for " + client.GATEWAY_URL + "?sid=" + SECRET)),
    RuntimeError("Connection failed for " + client.GATEWAY_URL + "?sid=" + SECRET),
])
def test_http_json_and_transport_failures_do_not_disclose_session_secrets(saved_session, fake_transport, reply):
    _, replies = fake_transport
    replies.append(reply)
    with AnghamiSession(saved=saved_session) as session:
        with pytest.raises(SessionError) as error:
            session.song(SONG_ID)
    rendered = "".join(traceback.format_exception(error.value))
    assert SECRET not in str(error.value)
    assert SECRET not in rendered
    assert client.GATEWAY_URL + "?sid=" not in rendered


def test_song_read_does_not_require_browser_packages():
    # A fresh process makes the check independent of the suite's import order.
    script = r'''
import importlib.abc
import sys

class RejectBrowserImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"cloakbrowser", "playwright"}:
            raise AssertionError("Browser import attempted: " + fullname)

sys.meta_path.insert(0, RejectBrowserImports())
from anghami_session import AnghamiSession
from anghami_session import client

class Reply:
    status_code = 200
    def json(self):
        return {"status": 1, "id": "1280677978"}

class Http:
    def __init__(self, **options):
        pass
    def get(self, url, **options):
        assert "type=GETsong" in url
        return Reply()
    def close(self):
        pass

client.requests.Session = Http
saved = {
    "format_version": 1,
    "created_at_utc": "2026-09-30T00:00:00+00:00",
    "origin": "https://play.anghami.com",
    "requests": {"relations": {
        "method": "GET",
        "url": client.GATEWAY_URL + "?type=GETuserrelations&sid=synthetic",
        "headers": {"Cookie": "synthetic=value"},
    }},
}
with AnghamiSession(saved=saved) as session:
    assert session.song("1280677978")["id"] == "1280677978"
assert "cloakbrowser" not in sys.modules
assert "playwright" not in sys.modules
'''
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
