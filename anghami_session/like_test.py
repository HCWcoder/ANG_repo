"""A single-account, idempotent check of the original like_song function."""

from datetime import datetime, timezone
import time

from curl_cffi.requests.session import RetryStrategy

from .bandwidth import measured_request

from .client import GATEWAY_URL
from .errors import RequestFailure, SessionError, safe_request_failure
from .provider_recovery import observe_provider_failure, retry_after_seconds, wait_before_provider_request
from .media_gateway import PlaybackGateway
from .play_record import (
    TEST_SONG_ID, _Failure, _SafeResponse, _journal,
    _load_legacy_functions, _selected_session, _validated_test_song,
)

LIKED_PLAYLIST = "$1234567890LIKED#"
MAX_VERIFICATION_READ_ATTEMPTS = 3
_TRANSIENT_VERIFICATION_CURL_CODES = frozenset({5, 6, 7, 18, 28, 35, 52, 55, 56, 81, 92, 95, 96})
_TRANSIENT_VERIFICATION_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _history_journal(report, report_path, history_record=None):
    """Persist intent/results before forwarding safe facts to the vault ledger."""
    _journal(report, report_path)
    if history_record is not None:
        try:
            history_record(report)
        except Exception:
            raise _Failure("like_history_failed", "The durable like history could not be saved. Do not automatically resend an append.") from None


def retryable_like_verification_failure(value) -> bool:
    """Identify safe GET-only recovery without permitting a whole-test replay."""
    failure = safe_request_failure(value)
    if failure.get("stage") != "likes_read" or failure.get("failure_category") != "provider":
        return False
    if failure.get("http_status") in {401, 403, 407} or failure.get("curl_code") in {51, 58, 60, 77, 82, 83, 90, 91, 98}:
        return False
    if failure.get("http_status") in {401, 403, 407} or failure.get("curl_code") in {51, 58, 60, 77, 82, 83, 90, 91, 98}:
        return False
    code = failure.get("code")
    if code == "request_transport_failed":
        return failure.get("curl_code") in _TRANSIENT_VERIFICATION_CURL_CODES
    if code in {"request_http_failed", "request_rate_limited"}:
        return failure.get("http_status") in _TRANSIENT_VERIFICATION_HTTP_STATUSES
    return False


def _decimal_id(value) -> str:
    result = str(value)
    if isinstance(value, bool) or not result.isascii() or not result.isdecimal() or not 1 <= len(result) <= 20:
        raise _Failure("playlist_invalid", "The likes response contained an invalid identity.")
    return result


def _semantic_success(payload) -> bool:
    return (
        isinstance(payload, dict) and not isinstance(payload.get("status"), bool)
        and payload.get("status") in (1, "1", "ok") and not payload.get("error")
    )


def _incomplete(payload: dict, *, allow_buffers: bool = False) -> bool:
    if not allow_buffers and (payload.get("responsemode") == "buffered" or payload.get("songbuffers")):
        return True
    for name in ("moreData", "moredata", "has_more", "hasMore", "nextpage", "next_page", "nextPage"):
        if payload.get(name) not in (None, False, 0, "0", "false", ""):
            return True
    return not allow_buffers and payload.get("buffered") not in (None, False, 0, "0", "false", "")


def _playlist_identity(payload: dict) -> tuple[str, int]:
    """Bind the special playlist to an account-local ID and complete count."""
    if not _semantic_success(payload) or _incomplete(payload) or not isinstance(payload.get("sections"), list):
        raise _Failure("playlist_discovery_invalid", "The saved likes playlist could not be identified completely.")
    groups = [section for section in payload["sections"] if isinstance(section, dict) and section.get("name") == "playlists"]
    if len(groups) != 1 or _incomplete(groups[0]) or not isinstance(groups[0].get("data"), list):
        raise _Failure("playlist_discovery_invalid", "The saved likes playlist could not be identified completely.")
    candidates = [item for item in groups[0]["data"] if isinstance(item, dict) and item.get("name") == LIKED_PLAYLIST]
    if len(candidates) != 1:
        raise _Failure("playlist_identity_missing", "The selected account has no unambiguous likes playlist.")
    item = candidates[0]
    playlist_id = _decimal_id(item.get("id"))
    count = item.get("count")
    if isinstance(count, bool):
        raise _Failure("playlist_count_invalid", "The likes playlist did not provide a valid complete song count.")
    try:
        count = int(str(count))
        if count < 0 or str(item.get("count")) != str(count):
            raise ValueError
    except (TypeError, ValueError):
        raise _Failure("playlist_count_invalid", "The likes playlist did not provide a valid complete song count.") from None
    return playlist_id, count


def _stored_order_like(payload: dict, playlist_id: str, expected_count: int, *, song_id=TEST_SONG_ID) -> bool:
    """Read complete membership from the normal buffered player's song order.

    The player resolves this order against songbuffers and then filters missing
    or unavailable songs. Those visible items are not a complete membership
    proof. The validated original order includes every stored playlist ID.
    """
    song_id = _validated_test_song(song_id, song_id)
    if (
        not _semantic_success(payload) or _incomplete(payload, allow_buffers=True)
        or _decimal_id(payload.get("id")) != playlist_id
        or payload.get("responsemode") != "buffered"
        or payload.get("responsetype") not in (None, "GETplaylistdata")
        or not isinstance(payload.get("sections"), list)
        or not isinstance(payload.get("songbuffers"), list)
        or not isinstance(payload.get("songorder"), str)
    ):
        raise _Failure("likes_order_invalid", "The likes response did not provide a complete original playlist order.")
    if "playlistid" in payload and _decimal_id(payload["playlistid"]) != playlist_id:
        raise _Failure("likes_order_invalid", "The likes response identified a different playlist.")
    if "PlaylistName" in payload and payload["PlaylistName"] != LIKED_PLAYLIST:
        raise _Failure("likes_order_invalid", "The likes response identified a different playlist.")
    sections = [
        section for section in payload["sections"] if isinstance(section, dict)
        and section.get("type") == "song" and section.get("displaytype") == "list"
        and section.get("buffered") in (True, 1, "1", "true")
    ]
    if len(sections) != 1 or _incomplete(sections[0], allow_buffers=True):
        raise _Failure("likes_order_invalid", "The likes response did not identify its buffered song section.")
    songorder = payload["songorder"]
    identities = [] if songorder == "" else [_decimal_id(value) for value in songorder.split(",")]
    if len(identities) != len(set(identities)) or len(identities) != expected_count:
        raise _Failure("likes_order_incomplete", "The original playlist order did not match the complete saved song count.")
    if "limit_number_songs" in payload:
        limit = payload["limit_number_songs"]
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or len(identities) >= limit:
            raise _Failure("likes_order_incomplete", "The original playlist order reached or did not provide a valid song limit.")
    for container in (payload, sections[0]):
        if container.get("numFiltered") not in (None, 0, "0"):
            raise _Failure("likes_order_incomplete", "The original playlist order was filtered.")
        for name in ("totalcount", "total_count", "totalCount"):
            if name in container:
                try:
                    if isinstance(container[name], bool) or int(str(container[name])) != expected_count:
                        raise ValueError
                except (TypeError, ValueError):
                    raise _Failure("likes_order_incomplete", "The original playlist order contained a contradictory total.") from None
    return song_id in identities


def _close_response(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class _LikeAdapter:
    def __init__(self, http, headers: dict, sid: str, fingerprint: str, report: dict, report_path, build_params, *, song_id=TEST_SONG_ID, history_record=None):
        self.song_id = _validated_test_song(song_id, song_id)
        self.http = http
        self.headers = headers
        self.sid = sid
        self.fingerprint = fingerprint
        self.report = report
        self.report_path = report_path
        self.build_params = build_params
        self.mutation_attempts = 0
        self.playlist_id = None
        self.history_record = history_record

    def _read(self, operation: str, **extra) -> dict:
        if operation not in {"GETplaylists", "GETplaylistdata"}:
            raise _Failure("read_scope_invalid", "The like test attempted an unsupported read operation.")
        params = self.build_params(
            type=operation, angh_type=operation, output="jsonhp",
            sid=self.sid, appsid=self.sid, fingerprint=self.fingerprint, **extra,
        )
        response = None
        received = False
        try:
            if not wait_before_provider_request("likes_read"):
                raise RequestFailure("request_rate_limited", stage="likes_read", http_status=429, retry_after_seconds=121, retry_safe=False)
            response = measured_request(self.http, "get",
                GATEWAY_URL, params=params, headers=dict(self.headers),
                timeout=25, allow_redirects=False,
            )
            received = True
            if getattr(response, "status_code", None) != 200:
                status = getattr(response, "status_code", None)
                failure = RequestFailure("request_rate_limited" if status == 429 else "request_http_failed", stage="likes_read", http_status=status,
                    retry_after_seconds=retry_after_seconds(getattr(response, "headers", None)), retry_safe=False)
                observe_provider_failure(failure)
                raise _Failure("state_http_failed", "The gateway did not return a successful likes-state response.", request_failure=failure)
            payload = response.json()
            if not _semantic_success(payload):
                raise _Failure("state_api_failed", "The gateway did not accept the likes-state read.")
            return payload
        except (_Failure, RequestFailure):
            raise
        except Exception as exc:
            failure = RequestFailure("session_response_invalid" if received else "request_transport_failed", stage="likes_read", curl_code=getattr(exc, "code", None), retry_safe=False)
            raise _Failure("state_read_failed", "The likes state could not be read safely.", request_failure=failure) from None
        finally:
            if response is not None:
                _close_response(response)

    def stored_like(self) -> bool:
        playlist_id, count = _playlist_identity(self._read("GETplaylists"))
        if self.playlist_id is not None and playlist_id != self.playlist_id:
            raise _Failure("playlist_identity_changed", "The likes playlist identity changed during the test.")
        self.playlist_id = playlist_id
        payload = self._read("GETplaylistdata", nopaging="1", playlistid=playlist_id, buffered="1")
        return _stored_order_like(payload, playlist_id, count, song_id=self.song_id)

    def verify_after_append(self) -> bool:
        """Retry bounded reads of an accepted append on this same session only."""
        accepted = self.report.get("mutation_accepted") is True
        attempts = MAX_VERIFICATION_READ_ATTEMPTS if accepted else 1
        for attempt in range(1, attempts + 1):
            self.report.update({
                "verification_read_attempts": attempt,
                "verification_read_retries": attempt - 1,
                "verification_read_retryable": False,
            })
            _history_journal(self.report, self.report_path, self.history_record)
            try:
                return self.stored_like()
            except (_Failure, RequestFailure) as exc:
                failure = exc if isinstance(exc, RequestFailure) else getattr(exc, "request_failure", None)
                retryable = accepted and retryable_like_verification_failure(failure)
                self.report["verification_read_retryable"] = retryable
                # A direct RequestFailure is a cooldown refusal before a GET.
                # Do not loop or extend that deadline; leave verification pending.
                if not retryable or attempt == attempts or isinstance(exc, RequestFailure):
                    raise
                if safe_request_failure(failure).get("http_status") != 429:
                    time.sleep(0.5 * attempt)
                # _read honors the shared 429 cooldown before each new GET.

    def post(self, url, *, params, data, headers):
        allowed_keys = {"type", "name", "songid", "action", "extras", "x-socket-id"}
        if (
            url != GATEWAY_URL or not isinstance(params, dict) or not isinstance(data, dict)
            or set(data) != allowed_keys or data.get("type") != "PUTplaylist"
            or params.get("angh_type") != "PUTplaylist" or params.get("sid") != self.sid
            or params.get("fingerprint") != self.fingerprint
            or data.get("name") != LIKED_PLAYLIST or str(data.get("songid")) != self.song_id
            or data.get("action") != "append" or data.get("extras") != ""
            or not isinstance(data.get("x-socket-id"), str) or not data["x-socket-id"]
            or ("appsid" in params and params["appsid"] != self.sid)
            or ("type" in params and params["type"] != "PUTplaylist")
        ):
            raise _Failure("mutation_scope_invalid", "The original like function attempted a request outside the selected song and session.")
        if self.mutation_attempts or self.report["liked_before"] is not False or self.playlist_id is None:
            raise _Failure("mutation_repeat_blocked", "The test permits one append after a verified unliked state only.")
        if not wait_before_provider_request("likes_read"):
            raise RequestFailure("request_rate_limited", stage="likes_read", http_status=429, retry_after_seconds=121, retry_safe=False)
        self.mutation_attempts += 1
        self.report.update({
            "phase": "mutation", "mutation_attempted": True, "mutation_attempts": 1,
            "mutation_accepted": None, "mutation_result": "unknown", "api_status": "unknown",
        })
        _history_journal(self.report, self.report_path, self.history_record)
        response = None
        try:
            response = measured_request(self.http, "post",
                GATEWAY_URL, params=dict(params), data=dict(data),
                headers={**self.headers, "content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=25, allow_redirects=False,
            )
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and not isinstance(status, bool):
                self.report["mutation_http_status"] = status
            if status != 200:
                observe_provider_failure(RequestFailure("request_rate_limited" if status == 429 else "request_http_failed", stage="likes_read", http_status=status,
                    retry_after_seconds=retry_after_seconds(getattr(response, "headers", None)), retry_safe=False))
                raise _Failure("mutation_http_failed", "The append response did not confirm its result. Do not automatically resend it.")
            payload = response.json()
            if not isinstance(payload, dict):
                raise _Failure("mutation_reply_invalid", "The append response did not confirm its result. Do not automatically resend it.")
            if payload.get("status") == "failed" or payload.get("error"):
                self.report.update({"mutation_accepted": False, "mutation_result": "rejected", "api_status": "failed"})
                _history_journal(self.report, self.report_path, self.history_record)
                raise _Failure("mutation_rejected", "The gateway did not accept the song append.")
            if payload.get("status") != "ok":
                raise _Failure("mutation_reply_invalid", "The append response did not confirm its result. Do not automatically resend it.")
            self.report.update({"mutation_accepted": True, "mutation_result": "accepted", "api_status": "ok"})
            _history_journal(self.report, self.report_path, self.history_record)
            return _SafeResponse({"status": "ok"})
        except _Failure:
            raise
        except Exception:
            raise _Failure("mutation_transport_failed", "The append result is unknown. Check the safe readback report; do not automatically resend it.") from None
        finally:
            if response is not None:
                _close_response(response)


def run_like_test(session, song_id, *, report_path=None, declared_song_id=TEST_SONG_ID, history_record=None) -> dict:
    """Check one normal append, or skip it if the declared song is liked already.

    The caller enforces the authorized account cohort. API acceptance and the
    independently read stored state remain separate facts in the safe report.
    """
    song_id = _validated_test_song(song_id, declared_song_id, message="Like tests are restricted to declared test song configured for this run.")
    if history_record is None:
        history_record = getattr(session, "_like_history_record", None)
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(), "song_id": song_id,
        "passed": False, "phase": "session_validation", "browser_required": False,
        "password_required": False, "audio_bytes": 0, "automatic_retry": False,
        "authenticated": False, "negative_control_passed": False,
        "server_account_identity_verified": False, "metadata_verified": False,
        "renewal_attempted": False, "renewal_completed": False,
        "state_read_method": "GETplaylistdata complete original songorder",
        "liked_before": None, "liked_after": None, "persisted_state_verified": False,
        "mutation_attempted": False, "mutation_attempts": 0, "mutation_accepted": False,
        "mutation_result": "not_attempted", "api_status": "not_attempted",
        "verification_read_attempts": 0, "verification_read_retries": 0,
        "verification_read_retryable": False,
    }
    proxy_summary = getattr(session, "proxy_summary", None)
    if proxy_summary is not None:
        report["proxy"] = proxy_summary
    started = time.perf_counter()
    http = None
    previous_retry = None
    retry_overridden = False
    try:
        _history_journal(report, report_path, history_record)
        headers, sid, fingerprint = _selected_session(session._saved)
        report["phase"] = "legacy_source"
        functions = _load_legacy_functions(include_like=True)
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
        _history_journal(report, report_path, history_record)
        gateway.bootstrap()
        report["renewal_completed"] = True
        # Plain player requests use the freshly authenticated socket session,
        # while their fingerprint remains the captured decoded UUID.
        sid = gateway.tokens["socketsessionid"]
        report["server_account_identity_verified"] = True
        report["phase"] = "metadata"
        metadata = session.song(song_id)
        if not _semantic_success(metadata) or str(metadata.get("id")) != song_id:
            raise _Failure("metadata_invalid", "The requested song was not returned by the gateway.")
        report["metadata_verified"] = True
        adapter = _LikeAdapter(http, headers, sid, fingerprint, report, report_path, functions["build_gateway_params"], song_id=song_id, history_record=history_record)
        report["phase"] = "state_before"
        report["liked_before"] = adapter.stored_like()
        if report["liked_before"]:
            report.update({
                "liked_after": True, "persisted_state_verified": True,
                "mutation_result": "skipped_already_liked", "passed": True,
                "phase": "complete", "elapsed_seconds": round(time.perf_counter() - started, 6),
            })
            _history_journal(report, report_path, history_record)
            return report
        _history_journal(report, report_path, history_record)
        mutation_error = None
        try:
            functions["like_song"](adapter, song_id, fingerprint, sid)
            if adapter.mutation_attempts != 1 or report["mutation_accepted"] is not True:
                raise _Failure("mutation_incomplete", "The original like function did not complete the single expected append.")
        except Exception as exc:
            mutation_error = exc if isinstance(exc, (_Failure, RequestFailure)) else _Failure("mutation_failed", "The original like function failed. Do not automatically resend an attempted append.")
        # A readback is safe even after a timeout; it never resends the mutation.
        if adapter.mutation_attempts:
            report["phase"] = "state_after"
            report["liked_after"] = adapter.verify_after_append()
            report["persisted_state_verified"] = report["liked_after"] is True
        if mutation_error is not None:
            raise mutation_error
        if report["liked_after"] is not True:
            raise _Failure("readback_not_liked", "The append was acknowledged, but the stored like was not verified. Do not automatically resend it.")
        report.update({"phase": "complete", "passed": True, "elapsed_seconds": round(time.perf_counter() - started, 6)})
        _history_journal(report, report_path, history_record)
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
            _history_journal(report, report_path, history_record)
        except _Failure:
            raise SessionError("The like report could not be saved. An attempted append may have an unknown result; do not automatically resend it.") from None
        if isinstance(exc, RequestFailure):
            raise
        message = str(exc) if isinstance(exc, _Failure) else "The like test failed. Check the safe report; do not automatically resend an attempted append."
        raise SessionError(message) from None
    finally:
        if retry_overridden:
            http.retry = previous_retry
