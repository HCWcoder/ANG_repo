"""Bounded HTTP media delivery and silent audio decoding using a saved session."""

from datetime import datetime, timezone
from io import BytesIO
import math
import struct
import time
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests

from .errors import SessionError

MAX_MEDIA_BYTES = 1024 * 1024
MEDIA_DOMAINS = ("anghami.com", "angcdn.com")
MEDIA_HOSTS = {"d3nhk3h83d1umo.cloudfront.net"}  # Observed Anghami audio distribution.


def validate_seconds(seconds: float) -> float:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        raise SessionError("Probe duration must be a number between 1 and 30 seconds.") from None
    if not math.isfinite(seconds) or not 1 <= seconds <= 30:
        raise SessionError("Probe duration must be between 1 and 30 seconds.")
    return seconds


def song_summary(song: dict) -> dict:
    try:
        duration = float(song["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError
        return {
            "song_id": str(song["id"]), "title": song.get("title"),
            "artist": song.get("artist"), "song_duration_seconds": duration,
        }
    except (KeyError, TypeError, ValueError):
        raise SessionError("The song metadata has an invalid duration or identity.") from None


def _validate_media_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https" and parts.hostname
            and not parts.username and not parts.password and not parts.fragment
            and parts.port in (None, 443)
            and (parts.hostname in MEDIA_HOSTS or any(
                parts.hostname == domain or parts.hostname.endswith("." + domain) for domain in MEDIA_DOMAINS
            ))
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise SessionError("Anghami returned an unsupported media URL.")


def _fetch_media(url: str, user_agent: str) -> tuple[bytes, dict]:
    """Keep gateway cookies/session IDs out of requests to the media service."""
    headers = {
        "user-agent": user_agent,
        "origin": "https://play.anghami.com",
        "referer": "https://play.anghami.com/",
        "range": f"bytes=0-{MAX_MEDIA_BYTES - 1}",
    }
    with requests.Session(impersonate="chrome") as media:
        for hop in range(4):
            _validate_media_url(url)
            response = None
            try:
                media.cookies.clear()
                response = media.get(url, headers=headers, timeout=25, allow_redirects=False, stream=True)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location or hop == 3:
                        raise SessionError("The media service returned an unsupported redirect.")
                    url = urljoin(url, location)
                    continue
                if response.status_code not in (200, 206):
                    raise SessionError(f"Media request returned HTTP {response.status_code}.")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if not content_type.startswith("audio/") and content_type not in ("video/mp4", "application/octet-stream"):
                    raise SessionError("The media service did not return audio data.")
                payload = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    remaining = MAX_MEDIA_BYTES - len(payload)
                    payload.extend(chunk[:remaining])
                    if len(payload) == MAX_MEDIA_BYTES:
                        break
                if not payload:
                    raise SessionError("The media service returned no audio bytes.")
                return bytes(payload), {
                    "http_status": response.status_code,
                    "content_type": content_type,
                    "bytes_received": len(payload),
                    "maximum_bytes": MAX_MEDIA_BYTES,
                    "range_requested": True,
                    "gateway_credentials_forwarded": False,
                }
            except SessionError:
                raise
            except Exception as exc:
                raise SessionError(f"Media delivery failed ({type(exc).__name__}).") from None
            finally:
                if response is not None:
                    response.close()
    raise SessionError("The media service did not return audio data.")


def _decode_audio(payload: bytes, seconds: float) -> dict:
    try:
        import av
    except ImportError:
        raise SessionError("Install requirements-playback.txt before running the playback probe.") from None
    started = time.monotonic()
    decoded_seconds = 0.0
    frames = 0
    pcm_bytes = 0
    peak = 0
    try:
        with av.open(BytesIO(payload)) as container:
            if not container.streams.audio:
                raise SessionError("The received media has no audio stream.")
            stream = container.streams.audio[0]
            codec = stream.codec_context.name
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in container.decode(audio=0):
                if not frame.sample_rate or frame.samples < 1:
                    continue
                frames += 1
                decoded_seconds += frame.samples / frame.sample_rate
                for converted in resampler.resample(frame):
                    samples = bytes(converted.planes[0])[:converted.samples * 2]
                    pcm_bytes += len(samples)
                    peak = max(peak, max((abs(value[0]) for value in struct.iter_unpack("<h", samples)), default=0))
                # Silent consumption is paced by actual decoded sample duration.
                while True:
                    remaining = min(decoded_seconds, seconds) - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, 0.25))
                if decoded_seconds >= seconds:
                    break
        if decoded_seconds < seconds or not frames or not pcm_bytes:
            raise SessionError("The received audio could not cover the requested probe duration.")
        return {
            "engine": "PyAV/FFmpeg", "codec": codec,
            "requested_seconds": seconds, "decoded_seconds": round(decoded_seconds, 6),
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "decoded_frames": frames, "pcm_bytes": pcm_bytes,
            "nonzero_audio_samples": peak > 0,
            "audio_output": "silent", "paced_in_real_time": True,
        }
    except SessionError:
        raise
    except Exception as exc:
        raise SessionError(f"Audio decoding failed ({type(exc).__name__}).") from None


def probe_playback(session, song_id: str | int, *, seconds: float = 5) -> dict:
    seconds = validate_seconds(seconds)
    try:
        import av  # Check the optional dependency before making any network request.
    except ImportError:
        raise SessionError("Install requirements-playback.txt before running the playback probe.") from None
    authentication = session.check(negative_control=True)
    song = session.song(song_id)
    metadata = song_summary(song)
    if metadata["song_duration_seconds"] < seconds:
        raise SessionError("The song is shorter than the requested probe duration.")
    source = session.media_source(song_id)
    template = session._template("relations")
    payload, delivery = _fetch_media(source["location"], template["headers"].get("user-agent", ""))
    decoded = _decode_audio(payload, seconds)
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "passed": True, **metadata,
        "authenticated": authentication["authenticated"],
        "without_session": authentication["without_session"],
        "browser_required": False, "password_required": False,
        "media_delivery": delivery, "audio_decoding": decoded,
        "listening_statistics_submitted": False,
    }
