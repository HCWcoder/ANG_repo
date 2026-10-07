"""One synthetic legacy play record for the explicitly designated test track.

This is an API integration test, not audio playback. The original function
claims the metadata's full duration without fetching or decoding any audio.
Authentication checks are read-only; the adapter permits exactly one event.
The event requests carry the same device and socket headers the real web
client sends. On demand, a best-effort downstream check compares the song's
exact public play count before and after the accepted event; an unavailable
or unchanged count never fails the test and is reported as unverified.
"""

import ast
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from random import random
import re
import tempfile
import time
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from curl_cffi.const import CurlInfo
from curl_cffi.requests.session import RetryStrategy

from .bandwidth import measured_request
from .client import GATEWAY_URL, validate_session
from .errors import RequestFailure, SessionError, safe_request_failure
from .provider_recovery import observe_provider_failure, retry_after_seconds, wait_before_provider_request
from .media_gateway import PlaybackGateway
from .test_settings import DEFAULT_TEST_SONG_ID, validate_test_song_id

TEST_SONG_ID = DEFAULT_TEST_SONG_ID
TEST_ACCOUNT_ROWS = frozenset({1, 2, 3, 4, 5, 7})
_SOURCE = Path(__file__).resolve().parents[1] / "send_vote.py"
_FUNCTIONS = {"get_song", "play_song", "build_gateway_params", "assert_response_ok"}
_COUNTERS = {
    "request_bytes": (CurlInfo.REQUEST_SIZE, "request_size"),
    "upload_body_bytes": (CurlInfo.SIZE_UPLOAD_T, "upload_size"),
    "download_body_bytes": (CurlInfo.SIZE_DOWNLOAD_T, "download_size"),
    "response_header_bytes": (CurlInfo.HEADER_SIZE, "header_size"),
}


class _Failure(SessionError):
    """Only fixed messages/codes from this module can enter the safe report."""

    def __init__(self, code: str, message: str, *, request_failure=None):
        super().__init__(message)
        self.code = code
        self.request_failure = request_failure


def _validated_test_song(song_id, declared_song_id, *, message="This test command is limited to the declared test song.") -> str:
    """Bind a canonical requested ID to this run's immutable declared ID."""
    try:
        declared = validate_test_song_id(declared_song_id)
        requested = validate_test_song_id(song_id)
    except SessionError:
        raise SessionError(message) from None
    if requested != declared:
        raise SessionError(message)
    return requested


def _load_legacy_functions(*, include_like: bool = False) -> dict:
    """Execute selected definitions only, never legacy imports or setup."""
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"), filename=str(_SOURCE))
    selected = _FUNCTIONS | {"like_song"} if include_like else _FUNCTIONS
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in selected]
    if len(functions) != len(selected) or {node.name for node in functions} != selected:
        raise _Failure("legacy_source_invalid", "The original play-function dependencies were not found.")
    endpoint_values = [
        node.value for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "GATEWAY_URL" for target in node.targets)
    ]
    if len(endpoint_values) != 1 or not isinstance(endpoint_values[0], ast.Constant) or endpoint_values[0].value != GATEWAY_URL:
        raise _Failure("legacy_endpoint_invalid", "The original play function uses an unsupported endpoint.")
    for node in functions:
        annotations = [arg.annotation for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)]
        annotations.extend(arg.annotation for arg in (node.args.vararg, node.args.kwarg) if arg is not None)
        if node.decorator_list or node.returns is not None or any(value is not None for value in annotations):
            raise _Failure("legacy_source_invalid", "The original play-function definitions are unsupported.")
        # Default expressions execute when a definition loads. Permit literals only.
        for value in (*node.args.defaults, *node.args.kw_defaults):
            if value is not None:
                ast.literal_eval(value)
    namespace = {
        "__builtins__": {
            "getattr": getattr, "int": int, "str": str, "float": float,
            "round": round, "AssertionError": AssertionError,
        },
        "dt": datetime, "random": random,
        "GATEWAY_URL": endpoint_values[0].value,
    }
    if include_like:
        namespace["uuid4"] = lambda: str(uuid4())
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(_SOURCE), "exec"), namespace)
    return namespace


def _journal(report: dict, report_path) -> None:
    if report_path is None:
        return
    temporary = None
    try:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, suffix=".json.tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(report, output, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        replacement_deadline = time.monotonic() + 2
        while True:
            try:
                os.replace(temporary, target)
                break
            except OSError as exc:
                windows_contention = os.name == "nt" and (
                    isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
                )
                if not windows_contention or time.monotonic() >= replacement_deadline:
                    raise
                # Retry only the local atomic rename, never an account request.
                time.sleep(0.1)
    except Exception as exc:
        failure = _Failure("journal_failed", "The test report could not be saved. Check the report path before any further test.")
        failure.local_diagnostics = {"error_kind": "filesystem_error" if isinstance(exc, OSError) else "report_error"}
        if isinstance(exc, OSError):
            for name in ("errno", "winerror"):
                try:
                    value = getattr(exc, name, None)
                except Exception:
                    continue
                if type(value) is int and 0 <= value <= 65535:
                    failure.local_diagnostics[name] = value
        raise failure from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _empty_counters() -> dict:
    return {"request_count": 0, **{name: 0 for name in _COUNTERS}, "measurement_complete": True}


PUBLIC_SONG_PAGE_URL = "https://play.anghami.com/song/"
_PUBLIC_PLAYS_PATTERN = re.compile(r"(?:&q;|\"|&quot;)plays(?:&q;|\"|&quot;)\s*:\s*([0-9]{1,15})")
_PUBLIC_PAGE_MAX_CHARS = 2_000_000
_PUBLIC_PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}
_DEVICE_COOKIE_PATTERN = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")


def _public_play_count(http, song_id) -> int | None:
    """Best-effort exact public play count from the unauthenticated song page.

    None means the count is unavailable; that is never a test failure.
    """
    response = None
    try:
        response = http.get(
            PUBLIC_SONG_PAGE_URL + song_id,
            headers=dict(_PUBLIC_PAGE_HEADERS), timeout=25, allow_redirects=False,
        )
        if getattr(response, "status_code", None) != 200:
            return None
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text or len(text) > _PUBLIC_PAGE_MAX_CHARS:
            return None
        match = _PUBLIC_PLAYS_PATTERN.search(text)
        return int(match.group(1)) if match else None
    except Exception:
        return None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _device_id_from_headers(headers: dict) -> str | None:
    """The web client sends its stored device UUID as the x-angh-udid header."""
    cookie = headers.get("cookie") if isinstance(headers, dict) else None
    if not isinstance(cookie, str):
        return None
    for part in cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name == "xxlfingerprint" and _DEVICE_COOKIE_PATTERN.fullmatch(value):
            return value
    return None


def _record_bandwidth(report: dict, function: str, response=None) -> None:
    record = report["bandwidth"][function]
    record["request_count"] += 1
    infos = getattr(response, "infos", {}) if response is not None else {}
    for name, (info, attribute) in _COUNTERS.items():
        value = infos.get(info) if isinstance(infos, dict) else None
        if value is None and response is not None:
            value = getattr(response, attribute, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            record[name] = None
            record["measurement_complete"] = False
        elif record[name] is not None:
            record[name] += int(value)
    total = report["bandwidth"]["total"]
    for name in ("request_count", *_COUNTERS):
        values = [report["bandwidth"][function][name] for function in ("get_song", "play_song")]
        total[name] = None if any(value is None for value in values) else sum(values)
    total["measurement_complete"] = all(report["bandwidth"][function]["measurement_complete"] for function in ("get_song", "play_song"))


class _SafeResponse:
    ok = True
    status_code = 200
    reason = "OK"
    text = ""

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


class _LegacyAdapter:
    def __init__(self, http, headers: dict, sid: str, fingerprint: str, report: dict, report_path, *, song_id=TEST_SONG_ID, event_sid=None, socket_id=None):
        self.song_id = _validated_test_song(song_id, song_id)
        self.http = http
        self.headers = headers
        self.device_id = _device_id_from_headers(headers)
        # The page issues one socket identifier; the play event carries it.
        # With a renewed session it matches the bootstrap's socket session id,
        # exactly like the real client's rotated session flow.
        self.socket_id = socket_id or str(uuid4())
        self.sid = sid
        self.event_sid = event_sid or sid
        self.fingerprint = fingerprint
        self.report = report
        self.report_path = report_path
        self.metadata_requests = 0
        self.event_attempts = 0
        self.duration = None
        self.claimed_override = None

    def get(self, url, *, params, headers):
        # The original function's obsolete browser headers are intentionally ignored.
        operation = params.get("type") if isinstance(params, dict) else None
        expected_sid = self.sid if operation == "GETsong" else self.event_sid
        if (
            url != GATEWAY_URL or operation not in {"GETsong", "REGISTERwebplay"}
            or params.get("angh_type") != operation
            or params.get("fingerprint") != self.fingerprint
            or (operation == "GETsong" and (params.get("sid") != self.sid or params.get("appsid") != self.sid))
        ):
            raise _Failure("request_scope_invalid", "The original play function attempted a request outside the selected session.")
        identity = "songId" if operation == "GETsong" else "songid"
        if str(params.get(identity)) != self.song_id:
            raise _Failure("song_scope_invalid", "The original play function attempted a different song.")
        function = "get_song" if operation == "GETsong" else "play_song"
        if operation == "GETsong":
            if self.metadata_requests or self.event_attempts:
                raise _Failure("metadata_repeat_blocked", "The test permits one metadata request only.")
            self.report["phase"] = "metadata"
            if not wait_before_provider_request("song_metadata"):
                raise RequestFailure("request_rate_limited", stage="song_metadata", http_status=429, retry_after_seconds=121, retry_safe=False)
            self.metadata_requests += 1
        else:
            if self.event_attempts or self.duration is None:
                raise _Failure("event_repeat_blocked", "The test permits one event after validated song metadata only.")
            try:
                fraction = float(params["playper"])
                valid_fraction = fraction == 1
                if not valid_fraction:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise _Failure("legacy_claim_invalid", "The original play function produced an unsupported synthetic duration.") from None
            if self.claimed_override is not None:
                claimed_seconds = self.claimed_override
                if not math.isfinite(claimed_seconds) or not 0 < claimed_seconds <= self.duration + 0.01:
                    raise _Failure("legacy_claim_invalid", "The original play function produced an unsupported synthetic duration.")
            else:
                try:
                    claimed_seconds = float(params["playsecs"])
                except (KeyError, TypeError, ValueError):
                    raise _Failure("legacy_claim_invalid", "The original play function produced an unsupported synthetic duration.") from None
                if not math.isfinite(claimed_seconds) or not self.duration - 0.000001 <= claimed_seconds <= self.duration + 0.01:
                    raise _Failure("legacy_claim_invalid", "The original play function produced an unsupported synthetic duration.")
            if not wait_before_provider_request("song_metadata"):
                raise RequestFailure("request_rate_limited", stage="song_metadata", http_status=429, retry_after_seconds=121, retry_safe=False)
            self.event_attempts += 1
            self.report.update({
                "phase": "event", "event_attempted": True, "event_attempts": 1,
                "event_accepted": None, "event_result": "unknown", "api_status": "unknown",
                "reported_play_seconds": claimed_seconds, "reported_play_fraction": fraction,
            })
            # Persist the ambiguity before transport: a timeout must never cause a resend.
            _journal(self.report, self.report_path)
        response = None
        try:
            # Match the observed web client: it sends no web_medium parameter,
            # carries the device UUID on both requests, and adds the page's
            # socket identifier to the play event.
            outgoing = {name: value for name, value in params.items() if name != "web_medium"}
            if operation == "REGISTERwebplay":
                if self.claimed_override is not None:
                    outgoing["playsecs"] = str(round(self.claimed_override, 6))
                if self.event_sid != self.sid:
                    outgoing["sid"] = outgoing["appsid"] = self.event_sid
            request_headers = dict(self.headers)
            if self.device_id is not None:
                request_headers["x-angh-udid"] = self.device_id
            if operation == "REGISTERwebplay":
                request_headers["x-socket-id"] = self.socket_id
            response = measured_request(self.http, "get",
                GATEWAY_URL, params=outgoing, headers=request_headers,
                timeout=25, allow_redirects=False,
            )
        except Exception as exc:
            _record_bandwidth(self.report, function)
            failure = RequestFailure("request_transport_failed", stage="song_metadata", curl_code=getattr(exc, "code", None), retry_safe=False) if operation == "GETsong" else None
            raise _Failure("transport_failed", "The test request failed in transport. An attempted event has an unknown result; do not automatically resend it.", request_failure=failure) from None
        _record_bandwidth(self.report, function, response)
        try:
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and not isinstance(status, bool):
                self.report["metadata_http_status" if operation == "GETsong" else "event_http_status"] = status
            if status != 200:
                failure = RequestFailure("request_rate_limited" if status == 429 else "request_http_failed", stage="song_metadata", http_status=status,
                    retry_after_seconds=retry_after_seconds(getattr(response, "headers", None)), retry_safe=False)
                observe_provider_failure(failure)
                if operation != "GETsong":
                    failure = None
                raise _Failure("http_rejected", "The test gateway did not return a successful HTTP response.", request_failure=failure)
            try:
                payload = response.json()
            except Exception:
                raise _Failure("response_invalid", "The test gateway returned an unexpected response format.") from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        if operation == "GETsong":
            if isinstance(payload, dict) and payload.get("status") == "failed":
                message = payload.get("message")
                if (
                    isinstance(message, str)
                    and "cannot play this song in the country" in message.casefold()
                    and "license rights" in message.casefold()
                ):
                    raise _Failure(
                        "metadata_region_unavailable",
                        "Anghami reports that this song is unavailable in the current country "
                        "because of licensing rights. No play event was sent.",
                    )
            try:
                if (
                    not isinstance(payload, dict) or isinstance(payload.get("status"), bool)
                    or payload.get("status") not in (1, "1", "ok") or payload.get("error")
                    or str(payload.get("id")) != self.song_id or isinstance(payload.get("duration"), bool)
                ):
                    raise ValueError
                duration = float(payload["duration"])
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise _Failure("metadata_invalid", "The test song metadata did not contain the expected identity and a valid duration.") from None
            self.duration = duration
            self.report.update({"metadata_verified": True, "metadata_duration_seconds": duration})
            # Only the fields required by the original function leave this adapter.
            return _SafeResponse({"status": 1, "id": self.song_id, "duration": duration})
        if not isinstance(payload, dict):
            raise _Failure("response_invalid", "The event response was not valid JSON metadata.")
        if payload.get("status") == "failed" or payload.get("error"):
            self.report.update({"event_accepted": False, "event_result": "rejected", "api_status": "failed"})
            raise _Failure("event_rejected", "The test gateway did not accept the synthetic event.")
        if payload.get("status") != "ok":
            raise _Failure("response_invalid", "The event response did not confirm acceptance or rejection. Do not automatically resend it.")
        self.report.update({"event_accepted": True, "event_result": "accepted", "api_status": "ok"})
        return _SafeResponse({"status": "ok"})


def _selected_session(saved: dict) -> tuple[dict, str, str]:
    saved = validate_session(saved)
    if not saved.get("account_email"):
        raise _Failure("account_identity_missing", "The selected session has no saved account identity.")
    template = saved["requests"]["relations"]
    query = parse_qsl(urlsplit(template["url"]).query, keep_blank_values=True)
    values = {name: [value for key, value in query if key == name] for name in ("sid", "appsid", "fingerprint")}
    if any(len(items) > 1 for items in values.values()):
        raise _Failure("session_ambiguous", "The selected session has ambiguous request credentials.")
    sid = (values["sid"] or values["appsid"] or [""])[0]
    fingerprint = (values["fingerprint"] or [""])[0]
    if (
        not sid or sid == "undefined" or not fingerprint or fingerprint == "undefined"
        or (values["sid"] and values["appsid"] and values["sid"] != values["appsid"])
    ):
        raise _Failure("session_credentials_missing", "The selected session has no unambiguous request SID and fingerprint.")
    return dict(template["headers"]), sid, fingerprint


def run_play_record_test(session, song_id, *, report_path=None, declared_song_id=TEST_SONG_ID,
                         verify_downstream: bool = False, verify_timeout: float = 120, verify_interval: float = 20,
                         with_audio: bool = False) -> dict:
    """Submit at most one synthetic record for the declared test track.

    The caller enforces the authorized source-row cohort. This function binds
    the operation to one validated session, checks its server account identity,
    and restricts the song and legacy operations itself. No media is requested.
    With verify_downstream, the song's exact public play count is sampled
    before the event and polled afterwards; the result is reported but never
    changes the pass/fail outcome.
    """
    if verify_downstream:
        try:
            verify_timeout = float(verify_timeout)
            verify_interval = float(verify_interval)
        except (TypeError, ValueError):
            raise SessionError("Invalid downstream verification timing.") from None
        if not math.isfinite(verify_timeout) or not math.isfinite(verify_interval) \
                or not 1 <= verify_timeout <= 600 or not 0.1 <= verify_interval <= verify_timeout:
            raise SessionError("Invalid downstream verification timing.")
    song_id = _validated_test_song(
        song_id, declared_song_id,
        message="Synthetic play-record tests are restricted to test song configured for this run.",
    )
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "song_id": song_id, "passed": False, "synthetic": True,
        "audio_bytes": 0, "browser_required": False, "password_required": False,
        "downstream_statistics_verified": False, "automatic_retry": False,
        "public_play_count_before": None, "public_play_count_after": None,
        "public_play_count_change": None, "downstream_observation_seconds": None,
        "audio_requested": bool(with_audio), "audio_decoded_seconds": None,
        "audio_codec": None, "media_http_status": None,
        "authenticated": False, "negative_control_passed": False,
        "server_account_identity_verified": False, "metadata_verified": False,
        "renewal_attempted": False, "renewal_completed": False,
        "metadata_duration_seconds": None, "reported_play_seconds": None,
        "reported_play_fraction": None, "event_attempted": False,
        "event_attempts": 0, "event_accepted": False,
        "event_result": "not_attempted", "api_status": "not_attempted",
        "phase": "session_validation",
        "bandwidth": {
            "measurement": "libcurl CurlInfo; numeric byte counts only",
            "scope": "get_song metadata and play_song event requests; authentication preflight and public count verification excluded",
            "get_song": _empty_counters(), "play_song": _empty_counters(), "total": _empty_counters(),
        },
    }
    proxy_summary = getattr(session, "proxy_summary", None)
    if proxy_summary is not None:
        report["proxy"] = proxy_summary
    started = time.perf_counter()
    http = None
    previous_retry = None
    retry_overridden = False
    try:
        _journal(report, report_path)
        headers, sid, fingerprint = _selected_session(session._saved)
        report["phase"] = "legacy_source"
        functions = _load_legacy_functions()
        http = session._http
        if hasattr(http, "retry"):
            previous_retry = http.retry
            http.retry = RetryStrategy(count=0)
            retry_overridden = True
        report["phase"] = "preflight"
        checked = session.check(negative_control=True)
        if (
            not isinstance(checked, dict) or checked.get("authenticated") is not True
            or not isinstance(checked.get("without_session"), dict)
            or checked["without_session"].get("authentication_rejected") is not True
        ):
            raise _Failure("preflight_failed", "The selected session did not pass authentication and its negative control.")
        report.update({"authenticated": True, "negative_control_passed": True, "phase": "account_identity"})
        gateway = PlaybackGateway(session)
        if not wait_before_provider_request("identity"):
            raise RequestFailure("request_rate_limited", stage="identity", http_status=429, retry_after_seconds=121, retry_safe=False)
        report["renewal_attempted"] = True
        _journal(report, report_path)
        gateway.bootstrap()
        report["renewal_completed"] = True
        report["server_account_identity_verified"] = True
        socket = gateway.tokens.get("socketsessionid") if isinstance(gateway.tokens, dict) else None
        if not isinstance(socket, str) or not socket:
            raise _Failure("session_credentials_missing", "The session renewal did not provide a socket session identifier.")
        adapter = _LegacyAdapter(http, headers, sid, fingerprint, report, report_path, song_id=song_id,
                                 event_sid=socket, socket_id=socket)
        baseline = _public_play_count(http, song_id) if verify_downstream else None
        if baseline is not None:
            report["public_play_count_before"] = baseline
        play_secs = None
        if with_audio:
            from .playback import _decode_audio, _fetch_media, validate_seconds
            report["phase"] = "audio"
            _journal(report, report_path)
            try:
                validate_seconds(1)
            except SessionError:
                raise _Failure("audio_probe_invalid", "The audio delivery option is not supported by this playback module.") from None
            source = gateway.media_source(song_id)
            location = source.get("location") if isinstance(source, dict) else None
            if not isinstance(location, str) or not location:
                raise _Failure("audio_source_missing", "Anghami did not provide a playable media source for the test song.")
            payload, delivery = _fetch_media(location, headers.get("user-agent", ""))
            decoded = _decode_audio(payload, 1)
            play_secs = min(adapter.duration if adapter.duration is not None else 1.0,
                            float(decoded["decoded_seconds"]))
            adapter.claimed_override = play_secs
            report.update({
                "audio_requested": True,
                "audio_bytes": int(delivery["bytes_received"]),
                "audio_decoded_seconds": float(decoded["decoded_seconds"]),
                "audio_codec": decoded.get("codec") if type(decoded.get("codec")) is str else None,
                "media_http_status": int(delivery["http_status"]),
            })
        functions["play_song"](adapter, song_id, fingerprint, sid)
        if adapter.event_attempts != 1 or report["event_accepted"] is not True:
            raise _Failure("event_incomplete", "The original play function did not complete the expected single test event.")
        if baseline is not None:
            report["phase"] = "verification"
            _journal(report, report_path)
            observation_started = time.perf_counter()
            deadline = time.monotonic() + verify_timeout
            current = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(verify_interval, remaining))
                current = _public_play_count(http, song_id)
                if current is not None and current > baseline:
                    break
            report["downstream_observation_seconds"] = round(time.perf_counter() - observation_started, 6)
            if current is not None:
                report["public_play_count_after"] = current
                report["public_play_count_change"] = current - baseline
                report["downstream_statistics_verified"] = current > baseline
        report.update({"phase": "complete", "passed": True, "elapsed_seconds": round(time.perf_counter() - started, 6)})
        _journal(report, report_path)
        return report
    except Exception as exc:
        failure = safe_request_failure(exc) or safe_request_failure(getattr(exc, "request_failure", None))
        report.update({
            "failed_phase": report["phase"], "phase": "failed",
            "error_code": exc.code if isinstance(exc, (_Failure, RequestFailure)) else "test_failed",
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        })
        if failure:
            report["session_failure"] = failure
        try:
            _journal(report, report_path)
        except _Failure:
            raise SessionError("The test report could not be saved. An attempted event may have an unknown result; do not automatically resend it.") from None
        if isinstance(exc, RequestFailure):
            raise
        message = str(exc) if isinstance(exc, _Failure) else "The synthetic play-record test failed. Check the safe report; do not automatically resend an attempted event."
        raise SessionError(message) from None
    finally:
        if retry_overridden:
            http.retry = previous_retry
