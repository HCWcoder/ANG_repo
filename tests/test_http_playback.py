"""Offline media-delivery and audio-decoding checks with synthetic data only."""

from io import BytesIO
import json
from pathlib import Path
import struct
import subprocess
import sys
import traceback
import wave

import pytest

from anghami_session import SessionError
from anghami_session import playback


ROOT = Path(__file__).resolve().parents[1]
SONG_ID = "1263607749"
SECRET = "synthetic-session-and-url-secret"
MEDIA_URL = "https://media.angcdn.com/test.wav?signature=" + SECRET


def pcm_wav(seconds=1.5, *, silent=False):
    """Build ordinary PCM locally rather than relying on downloaded fixtures."""
    sample_rate = 16000
    samples = int(sample_rate * seconds)
    payload = BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"".join(
            struct.pack("<h", 0 if silent else (8000 if index % 2 else -8000))
            for index in range(samples)
        ))
    return payload.getvalue()


class Reply:
    def __init__(self, *, status=206, headers=None, chunks=None, error=None):
        self.status_code = status
        self.headers = {"content-type": "audio/wav"} if headers is None else headers
        self.chunks = [b"synthetic audio bytes"] if chunks is None else chunks
        self.error = error
        self.closed = False
        self.iterations = 0

    def iter_content(self, chunk_size):
        assert chunk_size == 65536
        for chunk in self.chunks:
            self.iterations += 1
            yield chunk
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True


@pytest.fixture
def media_transport(monkeypatch):
    replies = []
    instances = []

    class Cookies:
        def __init__(self):
            self.values = {"gateway-cookie": SECRET}
            self.clear_count = 0

        def clear(self):
            self.values.clear()
            self.clear_count += 1

    class Transport:
        def __init__(self, **options):
            self.options = options
            self.calls = []
            self.cookies = Cookies()
            self.closed = False
            instances.append(self)

        def get(self, url, **options):
            assert self.cookies.values == {}
            self.calls.append((url, options))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            # A server-set cookie must not accompany even the next media hop.
            self.cookies.values["server-cookie"] = SECRET
            return reply

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(playback.requests, "Session", Transport)
    return instances, replies


def test_media_requests_use_fresh_transports_and_only_media_headers(media_transport):
    instances, replies = media_transport
    responses = [Reply(), Reply()]
    replies.extend(responses)
    for _ in range(2):
        payload, report = playback._fetch_media(MEDIA_URL, "Synthetic UA")
        assert payload == b"synthetic audio bytes"
        assert report["gateway_credentials_forwarded"] is False
        assert SECRET not in json.dumps(report)

    assert len(instances) == 2
    assert all(response.closed for response in responses)
    for instance in instances:
        assert instance.options == {"impersonate": "chrome"}
        assert instance.closed
        assert instance.cookies.clear_count == 1
        assert len(instance.calls) == 1
        url, options = instance.calls[0]
        assert url == MEDIA_URL
        assert options == {
            "headers": {
                "user-agent": "Synthetic UA",
                "origin": "https://play.anghami.com",
                "referer": "https://play.anghami.com/",
                "range": f"bytes=0-{playback.MAX_MEDIA_BYTES - 1}",
            },
            "timeout": 25,
            "allow_redirects": False,
            "stream": True,
        }
        assert not {"cookie", "authorization", "proxy-authorization", "x-angh-session"} & options["headers"].keys()


def test_media_byte_cap_stops_consumption_and_closes_resources(media_transport, monkeypatch):
    instances, replies = media_transport
    monkeypatch.setattr(playback, "MAX_MEDIA_BYTES", 12)
    response = Reply(chunks=[b"a" * 8, b"b" * 8, b"must-not-be-consumed"])
    replies.append(response)
    payload, report = playback._fetch_media(MEDIA_URL, "UA")
    assert payload == b"a" * 8 + b"b" * 4
    assert report["bytes_received"] == report["maximum_bytes"] == 12
    assert response.iterations == 2
    assert response.closed and instances[0].closed
    assert instances[0].calls[0][1]["headers"]["range"] == "bytes=0-11"


@pytest.mark.parametrize("url", [
    "http://media.angcdn.com/audio", "https://angcdn.com.attacker.example/audio",
    "https://notangcdn.com/audio", "https://evil.example/audio",
    "https://user:password@anghami.com/audio", "https://angcdn.com:444/audio",
    "https://angcdn.com/audio#fragment", "/relative/audio", None,
    "https://angcdn.com:bad/audio",
])
def test_unapproved_media_urls_are_rejected_before_network(media_transport, url):
    instances, _ = media_transport
    with pytest.raises(SessionError, match="unsupported media URL"):
        playback._fetch_media(url, "UA")
    assert all(instance.calls == [] and instance.closed for instance in instances)


def test_relative_redirect_is_validated_and_cookies_are_cleared_again(media_transport):
    instances, replies = media_transport
    redirect = Reply(status=302, headers={"location": "/actual.wav?signature=" + SECRET})
    audio = Reply()
    replies.extend([redirect, audio])
    playback._fetch_media(MEDIA_URL, "UA")
    assert [call[0] for call in instances[0].calls] == [
        MEDIA_URL, "https://media.angcdn.com/actual.wav?signature=" + SECRET,
    ]
    assert instances[0].cookies.clear_count == 2
    assert redirect.closed and audio.closed and instances[0].closed


@pytest.mark.parametrize("location", [
    "https://evil.example/audio", "http://media.angcdn.com/audio",
    "https://media.angcdn.com:444/audio", "https://user@anghami.com/audio",
])
def test_redirect_cannot_leave_approved_https_media_service(media_transport, location):
    instances, replies = media_transport
    redirect = Reply(status=307, headers={"location": location})
    replies.append(redirect)
    with pytest.raises(SessionError, match="unsupported media URL"):
        playback._fetch_media(MEDIA_URL, "UA")
    assert len(instances[0].calls) == 1
    assert redirect.closed and instances[0].closed


def test_redirect_chain_is_bounded(media_transport):
    instances, replies = media_transport
    redirects = [Reply(status=302, headers={"location": "/again.wav"}) for _ in range(4)]
    replies.extend(redirects)
    with pytest.raises(SessionError, match="unsupported redirect"):
        playback._fetch_media(MEDIA_URL, "UA")
    assert len(instances[0].calls) == 4
    assert all(reply.closed for reply in redirects)
    assert instances[0].closed


@pytest.mark.parametrize("response", [
    Reply(status=302, headers={}),
    Reply(status=403, headers={"content-type": "text/html"}),
    Reply(headers={"content-type": "text/html"}, chunks=[b"<html>" + SECRET.encode()]),
    Reply(headers={}),
    Reply(chunks=[]),
])
def test_redirect_http_html_and_empty_responses_fail_without_leaks(media_transport, response):
    instances, replies = media_transport
    replies.append(response)
    with pytest.raises(SessionError) as error:
        playback._fetch_media(MEDIA_URL, "UA")
    assert SECRET not in str(error.value)
    assert response.closed and instances[0].closed


@pytest.mark.parametrize("failure_stage", ["get", "stream"])
def test_transport_errors_are_sanitized_and_resources_close(media_transport, failure_stage):
    instances, replies = media_transport
    failure = RuntimeError("Failure downloading " + MEDIA_URL)
    response = Reply(chunks=[], error=failure)
    replies.append(failure if failure_stage == "get" else response)
    with pytest.raises(SessionError, match="Media delivery failed") as error:
        playback._fetch_media(MEDIA_URL, "UA")
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert MEDIA_URL not in str(error.value)
    assert instances[0].closed
    if failure_stage == "stream":
        assert response.closed


@pytest.fixture
def fake_clock(monkeypatch):
    class Clock:
        now = 10.0

        def __init__(self):
            self.sleeps = []

        def monotonic(self):
            # Account for call overhead so sub-ULP sleep targets cannot stall
            # a simulated clock, unlike the continually advancing real clock.
            self.now += 1e-9
            return self.now

        def sleep(self, seconds):
            assert 0 < seconds <= 0.25
            self.sleeps.append(seconds)
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(playback.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(playback.time, "sleep", clock.sleep)
    return clock


@pytest.mark.parametrize("silent", [False, True])
def test_real_pcm_decoding_is_paced_by_samples_and_uses_no_audio_output(fake_clock, silent):
    report = playback._decode_audio(pcm_wav(silent=silent), seconds=1)
    assert report["engine"] == "PyAV/FFmpeg"
    assert report["codec"] == "pcm_s16le"
    assert report["requested_seconds"] == 1
    assert report["decoded_seconds"] >= 1
    assert report["decoded_frames"] > 0
    assert report["pcm_bytes"] >= 32000
    assert report["nonzero_audio_samples"] is (not silent)
    assert report["elapsed_seconds"] == pytest.approx(1)
    assert sum(fake_clock.sleeps) == pytest.approx(1)
    assert report["audio_output"] == "silent"
    assert report["paced_in_real_time"] is True


@pytest.mark.parametrize("payload", [
    b"<html>" + SECRET.encode(), b"", pcm_wav()[:40], pcm_wav()[:400],
])
def test_invalid_or_truncated_audio_cannot_report_success(fake_clock, payload):
    with pytest.raises(SessionError) as error:
        playback._decode_audio(payload, seconds=1)
    assert SECRET not in "".join(traceback.format_exception(error.value))


def test_decodable_but_short_audio_fails_requested_duration(fake_clock):
    with pytest.raises(SessionError, match="cover the requested probe duration"):
        playback._decode_audio(pcm_wav(seconds=0.5), seconds=1)
    assert sum(fake_clock.sleeps) == pytest.approx(0.5)


class SavedSession:
    def __init__(self, *, authentication_error=None):
        self.calls = []
        self.authentication_error = authentication_error

    def check(self, *, negative_control):
        assert negative_control is True
        self.calls.append("check")
        if self.authentication_error:
            raise self.authentication_error
        return {
            "authenticated": True,
            "without_session": {"authentication_rejected": True},
        }

    def song(self, song_id):
        self.calls.append("song")
        return {"id": str(song_id), "title": "Synthetic song", "duration": "115"}

    def media_source(self, song_id):
        self.calls.append("media_source")
        return {"location": MEDIA_URL}

    def _template(self, operation):
        assert operation == "relations"
        self.calls.append("template")
        return {"headers": {
            "user-agent": "Synthetic UA", "cookie": SECRET,
            "authorization": "Bearer " + SECRET, "x-angh-session": SECRET,
        }}


def test_failed_authentication_prevents_song_or_media_requests(monkeypatch):
    session = SavedSession(authentication_error=SessionError("Synthetic authentication rejected"))
    monkeypatch.setattr(playback, "_fetch_media", lambda *_: pytest.fail("Media fetched after failed authentication"))
    with pytest.raises(SessionError, match="authentication rejected"):
        playback.probe_playback(session, SONG_ID)
    assert session.calls == ["check"]


@pytest.mark.parametrize("seconds", [0, -1, 30.1, "bad", None, float("nan"), float("inf")])
def test_invalid_probe_duration_fails_before_network(monkeypatch, seconds):
    session = SavedSession()
    monkeypatch.setattr(playback, "_fetch_media", lambda *_: pytest.fail("Media fetched for invalid duration"))
    with pytest.raises(SessionError, match="Probe duration"):
        playback.probe_playback(session, SONG_ID, seconds=seconds)
    assert session.calls == []


def test_probe_uses_authenticated_source_but_returns_no_secrets_or_statistics(monkeypatch):
    session = SavedSession()
    fetch_calls = []
    decode_calls = []

    def fetch(url, user_agent):
        fetch_calls.append((url, user_agent))
        return b"audio", {"bytes_received": 5, "gateway_credentials_forwarded": False}

    def decode(payload, seconds):
        decode_calls.append((payload, seconds))
        return {"decoded_seconds": seconds, "paced_in_real_time": True}

    monkeypatch.setattr(playback, "_fetch_media", fetch)
    monkeypatch.setattr(playback, "_decode_audio", decode)
    report = playback.probe_playback(session, SONG_ID, seconds="5")
    assert session.calls == ["check", "song", "media_source", "template"]
    assert fetch_calls == [(MEDIA_URL, "Synthetic UA")]
    assert decode_calls == [(b"audio", 5.0)]
    assert report["passed"] and report["authenticated"]
    assert report["song_id"] == SONG_ID
    assert report["song_duration_seconds"] == 115.0
    assert report["browser_required"] is False
    assert report["password_required"] is False
    assert report["listening_statistics_submitted"] is False
    serialized = json.dumps(report)
    assert SECRET not in serialized
    assert MEDIA_URL not in serialized
    assert "REGISTERwebplay" not in serialized


def test_audio_decoder_import_and_execution_do_not_require_browser_packages():
    script = r'''
import importlib.abc
from io import BytesIO
import sys
import wave

class RejectBrowserImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"cloakbrowser", "playwright"}:
            raise AssertionError("Browser import attempted: " + fullname)

sys.meta_path.insert(0, RejectBrowserImports())
from anghami_session import playback
payload = BytesIO()
with wave.open(payload, "wb") as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(16000)
    wav.writeframes(b"\x10\x00" * 16000)
clock = [0.0]
def monotonic():
    clock[0] += 1e-9
    return clock[0]
playback.time.monotonic = monotonic
def sleep(seconds):
    clock[0] += seconds
playback.time.sleep = sleep
assert playback._decode_audio(payload.getvalue(), 1)["decoded_seconds"] >= 1
assert "cloakbrowser" not in sys.modules
assert "playwright" not in sys.modules
'''
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
