"""Manual, one-account Egypt browser playback with a redacted request trace.

Preview is offline. --run prepares one new exact source row using CloakBrowser,
then plays only the fixed test song once in that same browser context. It does
not replay browser requests, send Python play records, or change likes.
"""

import argparse
from collections import Counter
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import hashlib
from http.cookies import SimpleCookie
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from anghami_session import capture
from anghami_session.browser import launch_browser as normal_browser_launch
from anghami_session.client import GATEWAY_URL
from anghami_session.country_preparation import direct_runtime
from anghami_session.errors import SessionError, safe_login_failure
from anghami_session.media_gateway import PlaybackGateway, _decrypt, _derive_key
from anghami_session.play_record import _journal, _load_legacy_functions, _selected_session
from anghami_session.preparation import prepare_test_accounts
from anghami_session.proxy import load_packetstream_proxy
from anghami_session.test_settings import read_test_song_id
from anghami_session.vault import AccountVault

SONG_ID = "1263607749"
SONG_URL = "https://play.anghami.com/song/" + SONG_ID
SETTINGS_PATH = ROOT / ".anghami" / "test-settings.json"
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")
PUBLIC_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,4}\Z")
KNOWN_OPERATIONS = frozenset({
    "authenticate", "GETsong", "GETdownload", "REGISTERwebplay", "REGISTERplay",
    "GETuserrelations", "GETplaylists", "GETplaylist", "GETprofile", "GETuser",
    "GETuserprofile", "GETrecents", "GETrecommendations", "GEThome", "GETfeeds",
    "GETsearch", "GETartist", "GETalbum", "GETnotifications", "GETqueue",
    "POSTfingerprint", "GETfingerprint", "PUTplaylist", "POSTplaylist", "DELETEplaylist",
})
API_STATUSES = frozenset({"ok", "failed", "fail", "error", "success"})
AUTH_STATUSES = API_STATUSES | frozenset({"verification_required", "captcha_required", "challenge_required", "reauth_required"})
MEDIA_HOSTS = frozenset({"d3nhk3h83d1umo.cloudfront.net"})
MEDIA_DOMAINS = ("anghami.com", "angcdn.com")
MAX_TRACE = 1500

# Media is silent under the browser's default mute flag. No JS request payloads
# or account storage are collected. The guard rejects auto-play before arming,
# additional media elements, and any play after the primary media has ended.
MEDIA_INIT_SCRIPT = """(() => {
  const state = {armed: false, started: false, ended: false, primary: null,
    playCalls: 0, blockedCalls: 0, scopeViolation: false, source: null,
    maxTime: 0, endedSnapshot: null, expectedDuration: null, lastTarget: null,
    completionMode: null, scopeReason: null};
  const observed = new Set();
  const originalPlay = HTMLMediaElement.prototype.play;
  const snapshot = media => ({current_time_seconds: media.currentTime,
    duration_seconds: Number.isFinite(media.duration) ? media.duration : null,
    decoded_audio_bytes: media.webkitAudioDecodedByteCount ?? null,
    ready_state: media.readyState, media_error_code: media.error?.code ?? null});
  const finish = (media, mode) => {
    state.endedSnapshot = mode === 'natural_end' ? snapshot(media) : state.lastTarget;
    state.ended = true; state.armed = false; state.completionMode = mode;
    for (const x of observed) x.pause();
  };
  const fail = (media, reason) => {
    state.scopeViolation = true; state.scopeReason = reason; state.armed = false;
    media.pause(); return false;
  };
  const guardPrimary = (media, eventName) => {
    if (media !== state.primary || !state.armed || state.ended) {
      media.pause(); state.blockedCalls++; return false;
    }
    const source = media.src || media.currentSrc || null;
    if (source && !state.source) state.source = source;
    const wrongSource = source && state.source !== source;
    const rewind = media.currentTime < state.maxTime - 1;
    const wrongDuration = Number.isFinite(media.duration) && media.duration > 0
      && state.expectedDuration !== null && Math.abs(media.duration - state.expectedDuration) > 2;
    if (wrongSource || rewind || wrongDuration) {
      // Gapless players can replace src before dispatching ended. Preserve the
      // last verified target clock, stop here, and do not call originalPlay.
      if (state.expectedDuration !== null && state.lastTarget
          && state.maxTime >= state.expectedDuration - 2) {
        finish(media, 'target_boundary'); return false;
      }
      return fail(media, wrongSource ? 'source_changed' : rewind ? 'time_rewound' : 'duration_changed');
    }
    state.maxTime = Math.max(state.maxTime, media.currentTime);
    state.lastTarget = snapshot(media);
    media.loop = false;
    if (eventName === 'ended' || (state.started && media.ended)) {
      finish(media, 'natural_end'); return false;
    }
    return true;
  };
  HTMLMediaElement.prototype.play = function(...args) {
    if (!state.armed || state.ended || (state.started && state.primary?.ended)
        || (state.primary && state.primary !== this)) {
      this.pause();
      state.blockedCalls++;
      return Promise.reject(new DOMException('Bounded playback scope', 'NotAllowedError'));
    }
    if (!state.primary) {
      state.primary = this;
      this.loop = false;
      observed.add(this);
      // SDK Audio() objects are often detached from the document. These direct
      // listeners enforce the boundary even when document capture sees nothing.
      for (const name of ['play', 'playing', 'timeupdate', 'seeking', 'loadstart', 'durationchange', 'loadedmetadata', 'ended']) {
        this.addEventListener(name, () => guardPrimary(this, name), {capture: true});
      }
    }
    if (!guardPrimary(this, 'play_call')) {
      state.blockedCalls++;
      return Promise.reject(new DOMException('Bounded playback scope', 'NotAllowedError'));
    }
    state.started = true; state.playCalls++;
    return originalPlay.apply(this, args);
  };
  const guard = event => {
    const media = event.target;
    if (!(media instanceof HTMLMediaElement)) return;
    if (!state.armed || state.ended || media !== state.primary) {
      media.pause(); state.blockedCalls++; return;
    }
    guardPrimary(media, event.type);
  };
  for (const event of ['play', 'playing', 'timeupdate']) document.addEventListener(event, guard, true);
  Object.defineProperty(window, '__boundedPlaybackState', {value: state});
  Object.defineProperty(window, '__observedPlaybackMedia', {value: observed});
})();"""

MEDIA_SAMPLE_SCRIPT = """() => {
  const s = window.__boundedPlaybackState;
  const m = s?.primary;
  const final = s?.endedSnapshot;
  return {started: !!s?.started, ended: !!s?.ended || !!(s?.started && m?.ended),
    scope_violation: !!s?.scopeViolation, completion_mode: s?.completionMode ?? null,
    scope_reason: s?.scopeReason ?? null,
    play_calls: s?.playCalls ?? 0, blocked_calls: s?.blockedCalls ?? 0,
    current_time_seconds: final?.current_time_seconds ?? m?.currentTime ?? 0,
    duration_seconds: final?.duration_seconds ?? (Number.isFinite(m?.duration) ? m.duration : null),
    paused: m?.paused ?? true, ready_state: m?.readyState ?? 0,
    media_error_code: final?.media_error_code ?? m?.error?.code ?? null,
    decoded_audio_bytes: final?.decoded_audio_bytes ?? m?.webkitAudioDecodedByteCount ?? null};
}"""


class ProbeFailure(SessionError):
    def __init__(self, code):
        self.code = code
        super().__init__("The bounded browser request probe stopped.")


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_names(values):
    return sorted({value for value in values if isinstance(value, str) and NAME.fullmatch(value)})[:100]


def numeric(value, maximum=10**12):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum else None


def public_version(value):
    return value if isinstance(value, str) and PUBLIC_VERSION.fullmatch(value) else None


def endpoint_parts(url):
    """Do not retain an arbitrary URL, host, path, or query value."""
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != "coussa.anghami.com"
                or parts.path != "/gateway.php" or parts.username or parts.password
                or parts.port not in (None, 443)):
            return None
        return parse_qsl(parts.query, keep_blank_values=True, max_num_fields=150)
    except (TypeError, ValueError):
        return None


def failure_bucket(value):
    # Incoming error strings are used for local classification only.
    lowered = value.lower() if isinstance(value, str) else ""
    for fragment, code in (("timed_out", "timeout"), ("timeout", "timeout"),
                           ("proxy", "proxy"), ("aborted", "aborted"),
                           ("name_not_resolved", "dns"), ("cert", "tls"),
                           ("connection", "connection")):
        if fragment in lowered:
            return code
    return "transport"


def body_summary(request, headers):
    encrypted = "x-angh-encpayload" in headers
    content_type = headers.get("content-type", "").split(";", 1)[0].lower()
    result = {"format": "none", "field_names": [], "encrypted": encrypted}
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return result
    if encrypted or content_type == "application/octet-stream":
        result["format"] = "opaque_binary"
        return result
    data = request.post_data
    if not isinstance(data, str) or len(data) > 100_000:
        result["format"] = "opaque"
        return result
    try:
        if content_type == "application/json":
            parsed = json.loads(data)
            result["format"] = "json"
            result["field_names"] = safe_names(parsed) if isinstance(parsed, dict) else []
        elif content_type == "application/x-www-form-urlencoded":
            result["format"] = "form"
            result["field_names"] = safe_names(name for name, _ in parse_qsl(data, max_num_fields=150))
        else:
            result["format"] = "opaque"
    except (TypeError, ValueError):
        result["format"] = "opaque"
    return result


def fixed_status(value, *, authentication=False):
    allowed = AUTH_STATUSES if authentication else API_STATUSES
    return value if type(value) is str and value in allowed else value if type(value) is int and value in {0, 1} else None


def auth_response_summary(payload, request, expected_email=None):
    """Passively inspect the normal SDK reply; never collect issued keys.

    The existing normal scheme-3 decoder runs in memory only. This callback does
    not make a request, change browser state, solve a challenge, or replay auth.
    """
    result = {
        "outer_field_names": safe_names(payload) if isinstance(payload, dict) else [],
        "decode_result": "not_encrypted", "semantic_status": None,
        "verification_required": None, "verification_signal_source": "none",
        "authentication_record_present": False, "selected_account_identity_matched": None,
        "playback_keys_present": False,
    }
    if not isinstance(payload, dict):
        result["decode_result"] = "unsupported_response"
        return result
    semantic = payload
    if isinstance(payload.get("reply"), str):
        result["decode_result"] = "unsupported_headers"
        try:
            headers = request.all_headers()
            timestamp = headers.get("x-angh-ts")
            if (headers.get("x-angh-encpayload") != "3" or not isinstance(timestamp, str)
                    or not timestamp.isascii() or not timestamp.isdecimal() or len(timestamp) > 12):
                return result
            fingerprint = next((piece.strip().split("=", 1)[1]
                                for piece in headers.get("cookie", "").split(";")
                                if piece.strip().startswith("fingerprint=")), "")
            if not fingerprint or len(fingerprint) > 4096:
                return result
            key = _derive_key(fingerprint, int(timestamp), request=False)
            semantic = _decrypt(payload["reply"], key)
            result["decode_result"] = "decoded"
        except Exception:
            result["decode_result"] = "decode_failed"
            return result
    if not isinstance(semantic, dict):
        result["decode_result"] = "unsupported_response"
        return result
    result["semantic_status"] = fixed_status(semantic.get("status"), authentication=True)
    authentication = semantic.get("authenticate")
    result["authentication_record_present"] = isinstance(authentication, dict)
    if isinstance(authentication, dict):
        identity = authentication.get("email")
        if expected_email is not None:
            result["selected_account_identity_matched"] = isinstance(identity, str) and identity.strip().casefold() == expected_email
        result["playback_keys_present"] = all(isinstance(authentication.get(key), str) and bool(authentication[key])
                                               for key in ("reqkey", "reskey", "socketsessionid", "signingkey"))
    flags = ("verification_required", "requires_verification", "captcha_required", "requires_captcha", "challenge_required")
    if any(semantic.get(flag) is True for flag in flags):
        result.update({"verification_required": True, "verification_signal_source": "explicit_flag"})
    elif result["semantic_status"] in {"verification_required", "captcha_required", "challenge_required"}:
        result.update({"verification_required": True, "verification_signal_source": "status"})
    else:
        # Classification is an inference from message text. The message itself,
        # its nested fields, account identity, and returned keys never escape.
        messages = [semantic.get("message"), semantic.get("error_description")]
        error = semantic.get("error")
        messages.append(error.get("message") if isinstance(error, dict) else error)
        if any(isinstance(value, str) and any(marker in value.casefold() for marker in ("captcha", "verification required", "verify your", "human verification", "challenge required")) for value in messages):
            result.update({"verification_required": True, "verification_signal_source": "message_classification"})
        elif result["semantic_status"] in {"ok", "success", 1}:
            result["verification_required"] = False
    return result


def page_error_bucket(error):
    text = str(error).casefold()
    for marker, code in (("referenceerror", "javascript_reference"), ("typeerror", "javascript_type"),
                         ("syntaxerror", "javascript_syntax"), ("service worker", "service_worker"),
                         ("securityerror", "browser_security"), ("storage", "browser_storage"),
                         ("network", "network")):
        if marker in text:
            return code
    return "javascript_other"


class FlowRecorder:
    """Only names, fixed labels, numbers, and binding booleans reach disk."""
    def __init__(self, report):
        self.report = report
        self.started = time.monotonic()
        self.private = {}
        self.request_indices = {}
        self.sid = self.fingerprint = None
        self.expected_email = None
        self.playback_armed = False
        self.finished = False

    def bind(self, saved):
        headers, self.sid, self.fingerprint = _selected_session(saved)
        self.expected_email = saved.get("account_email")
        self.report["python_effective_header_names"] = safe_names(headers)
        for index, bindings in self.private.items():
            self._bind_event(self.report["requests"][index], bindings)

    def _bind_event(self, event, bindings):
        for key in ("sid", "appsid"):
            vals = bindings.get(key, [])
            event[key + "_matches_prepared_session"] = (
                len(vals) == 1 and vals[0] == self.sid if vals and self.sid is not None else None
            )
        vals = bindings.get("fingerprint", [])
        event["fingerprint_matches_prepared_session"] = (
            len(vals) == 1 and vals[0] == self.fingerprint
            if vals and self.fingerprint is not None else None
        )

    def on_request(self, request):
        try:
            pairs = endpoint_parts(request.url)
            if pairs is None:
                return
            if len(self.report["requests"]) >= MAX_TRACE:
                self.report["trace_limit_reached"] = True
                return
            query = {}
            for key, value in pairs:
                query.setdefault(key, []).append(value)
            operation = (query.get("type") or query.get("angh_type") or [""])[0]
            label = operation if operation in KNOWN_OPERATIONS else "other_gateway_operation"
            headers = request.headers
            event = {
                "index": len(self.report["requests"]),
                "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "phase": self.report.get("preparation_phase", self.report["phase"]) if self.report["phase"] == "preparation" else self.report["phase"], "endpoint": GATEWAY_URL,
                "operation": label, "method": request.method if request.method in {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"} else "OTHER",
                "query_parameter_names": safe_names(query),
                "duplicate_query_parameter_names": safe_names(key for key, values in query.items() if len(values) > 1),
                "request_header_names": safe_names(headers), "body": body_summary(request, headers),
                "song_matches_test_song": None, "http_status": None, "api_status": None,
                "encrypted_reply_present": None, "failed": False,
            }
            ids = [v for k, vals in query.items() if k.lower() in {"songid", "fileid"} for v in vals]
            if ids:
                event["song_matches_test_song"] = all(value == SONG_ID for value in ids)
            if operation.startswith("REGISTER"):
                modes = query.get("RetrievalMode", [])
                event["retrieval_mode"] = modes[0] if len(modes) == 1 and modes[0] in {"Streamed", "Downloaded"} else None
                for field, destination, maximum in (("playsecs", "reported_play_seconds", 600), ("playper", "reported_play_fraction", 1)):
                    values = query.get(field, [])
                    try:
                        event[destination] = numeric(float(values[0]), maximum) if len(values) == 1 else None
                    except (TypeError, ValueError):
                        event[destination] = None
            bindings = {key: query.get(key, []) for key in ("sid", "appsid", "fingerprint")}
            self.private[event["index"]] = bindings
            self._bind_event(event, bindings)
            self.request_indices[id(request)] = event["index"]
            self.report["requests"].append(event)
        except Exception:
            self.report["trace_observer_errors"] += 1

    def on_response(self, response):
        try:
            index = self.request_indices.get(id(response.request))
            if index is not None:
                event = self.report["requests"][index]
                event["http_status"] = response.status if type(response.status) is int and 100 <= response.status <= 599 else None
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        status = payload.get("status")
                        event["api_status"] = fixed_status(status)
                        event["encrypted_reply_present"] = isinstance(payload.get("reply"), str)
                        event["response_field_names"] = safe_names(payload)
                        if event["operation"] == "authenticate":
                            event["authentication_diagnostic"] = auth_response_summary(payload, response.request, self.expected_email)
                except Exception:
                    pass
            parts = urlsplit(response.url)
            media_host = parts.hostname in MEDIA_HOSTS or any(
                parts.hostname == domain or (parts.hostname or "").endswith("." + domain)
                for domain in MEDIA_DOMAINS
            )
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if media_host and (content_type.startswith("audio/") or response.request.resource_type == "media"):
                self.report["media_response_count"] += 1
                self.report["media_success_response_count"] += response.status in {200, 206}
            elif content_type.startswith("audio/") or response.request.resource_type == "media":
                self.report["unknown_host_media_response_count"] = self.report.get("unknown_host_media_response_count", 0) + 1
        except Exception:
            self.report["trace_observer_errors"] += 1

    def on_failure(self, request):
        try:
            resource = request.resource_type
            if resource not in {"document", "script", "stylesheet", "image", "font", "media", "xhr", "fetch", "websocket", "manifest"}:
                resource = "other"
            key = resource + ":" + failure_bucket(request.failure)
            counts = self.report.setdefault("request_failure_counts", {})
            counts[key] = counts.get(key, 0) + 1
            index = self.request_indices.get(id(request))
            if index is not None:
                self.report["requests"][index].update({"failed": True, "failure_code": failure_bucket(request.failure)})
        except Exception:
            self.report["trace_observer_errors"] += 1

    def on_page_error(self, error):
        try:
            events = self.report.setdefault("page_error_events", [])
            if len(events) < 50:
                events.append({"phase": self.report["phase"], "code": page_error_bucket(error)})
        except Exception:
            self.report["trace_observer_errors"] += 1

    def guard_route(self, route):
        pairs = endpoint_parts(route.request.url)
        query = {}
        for key, value in pairs or []:
            query.setdefault(key, []).append(value)
        operations = query.get("type", []) + query.get("angh_type", [])
        operation = operations[0] if operations else ""
        blocked = (len(set(operations)) > 1 or any(len(query.get(key, [])) > 1 for key in ("type", "angh_type"))
                   or operation in {"PUTplaylist", "POSTplaylist", "DELETEplaylist"})
        targets = [value for name, values in query.items() if name.lower() in {"songid", "fileid"} for value in values]
        target_wrong = bool(targets) and (len(set(targets)) > 1 or any(value != SONG_ID for value in targets)
                                         or any(len(query.get(key, [])) > 1 for key in query if key.lower() in {"songid", "fileid"}))
        if operation.startswith("REGISTER"):
            # Opaque encrypted payloads can omit a query song ID. The exact
            # browser page and one-media guard retain the scope in that case.
            blocked = blocked or not self.playback_armed or target_wrong
        if self.playback_armed and operation == "GETdownload":
            blocked = blocked or target_wrong or self.finished
        if blocked:
            self.report["blocked_out_of_scope_requests"] += 1
            route.abort("blockedbyclient")
        else:
            route.continue_()


class RetainedBrowser:
    def __init__(self, browser, recorder):
        self.browser, self.recorder = browser, recorder
        self.context = None
        self.close_deferred = False

    def new_context(self, **options):
        if self.context is not None:
            raise ProbeFailure("multiple_contexts_blocked")
        # Playwright routing cannot govern service-worker-owned requests.
        # Document this context change so comparisons do not imply an identical
        # service-worker/cache path to a normal persistent browser profile.
        self.context = self.browser.new_context(**{**options, "service_workers": "block"})
        self.context.add_init_script(MEDIA_INIT_SCRIPT)
        self.context.on("request", self.recorder.on_request)
        self.context.on("response", self.recorder.on_response)
        self.context.on("requestfailed", self.recorder.on_failure)
        self.context.on("page", lambda page: page.on("pageerror", self.recorder.on_page_error))
        self.context.route("**/gateway.php*", self.recorder.guard_route)
        return self.context

    def close(self):
        # capture_login's normal finally requests closure; ownership transfers
        # only to this bounded probe, whose outer finally closes the real handle.
        self.close_deferred = True

    def real_close(self):
        self.browser.close()


@contextmanager
def retain_login_browser(recorder, holders, *, direct=False):
    original = capture.launch_browser

    def launch(**options):
        if holders or options.get("backend") != "cloakbrowser" or options.get("headless") is not True:
            raise ProbeFailure("browser_scope_invalid")
        holder = RetainedBrowser(original(**{**options, **({"direct": True} if direct else {})}), recorder)
        holders.append(holder)
        return holder

    capture.launch_browser = launch
    try:
        yield
    finally:
        capture.launch_browser = original


def require_new_egypt_account(vault, row, source):
    if type(row) is not int or row < 1:
        raise ProbeFailure("row_invalid")
    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    if digest != vault.metadata("source_sha256"):
        raise ProbeFailure("source_changed")
    record = vault.record(row)
    if record.get("source_row") != row or record.get("country") != "EG":
        raise ProbeFailure("country_or_row_invalid")
    state = vault._db.execute("SELECT email_key,state,session IS NOT NULL FROM accounts WHERE source_row=?", (row,)).fetchone()
    if state is None or state[1] == "ready" or state[2]:
        raise ProbeFailure("account_not_new")
    enrolled = vault.enrolled_test_rows()
    copies = vault._db.execute("SELECT source_row,state,session IS NOT NULL FROM accounts WHERE email_key=?", (state[0],)).fetchall()
    if any(copy[0] in enrolled or copy[1] == "ready" or copy[2] for copy in copies):
        raise ProbeFailure("account_identity_not_new")
    preview = prepare_test_accounts(vault, count=1, start_row=row, browser_backend="cloakbrowser", headless=True, dry_run=True)
    if preview.get("selected_rows") != [row]:
        raise ProbeFailure("selected_row_changed")
    return digest


def require_prepared_egypt_account(vault, row, source):
    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    if digest != vault.metadata("source_sha256"):
        raise ProbeFailure("source_changed")
    record = vault.record(row)
    state = vault._db.execute("SELECT state,session IS NOT NULL FROM accounts WHERE source_row=?", (row,)).fetchone()
    if (record.get("source_row") != row or record.get("country") != "EG"
            or state is None or state[0] != "ready" or not state[1]
            or row not in vault.enrolled_test_rows()):
        raise ProbeFailure("prepared_account_scope_invalid")
    vault.session(row)  # Validates format and exact record identity.
    return digest


def require_no_playback_in_prior_proxy_attempt(row):
    path = ROOT / ".anghami" / f"browser-request-flow-row-{row}-{SONG_ID}.redacted.json"
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ProbeFailure("prior_proxy_attempt_unconfirmed") from None
    playback = prior.get("playback", {}) if isinstance(prior, dict) else {}
    requests = prior.get("requests", []) if isinstance(prior, dict) else []
    if (not isinstance(prior, dict) or prior.get("source_row") != row or prior.get("song_id") != SONG_ID
            or prior.get("connection") != "proxy_egypt" or prior.get("prepared") is not True
            or prior.get("phase") != "failed" or prior.get("error_code") != "browser_playback_timeout"
            or prior.get("cleanup_verified") is not True or prior.get("play_click_count") != 1
            or type(prior.get("media_response_count")) is not int or prior["media_response_count"] != 0
            or prior.get("trace_observer_errors") != 0 or prior.get("trace_limit_reached") is not False
            or not isinstance(playback, dict) or playback.get("started") is not False
            or playback.get("play_calls") != 0 or playback.get("current_time_seconds") != 0
            or not isinstance(requests, list) or any(not isinstance(event, dict) or event.get("operation") in {"GETdownload", "REGISTERwebplay", "REGISTERplay"} for event in requests)):
        raise ProbeFailure("prior_proxy_attempt_uncertain")
    return True


def require_no_playback_in_rejected_direct_login(row):
    path = ROOT / ".anghami" / f"browser-request-flow-row-{row}-{SONG_ID}-direct.redacted.json"
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ProbeFailure("prior_direct_login_unconfirmed") from None
    diagnosis = prior.get("login_failure", {}) if isinstance(prior, dict) else {}
    requests = prior.get("requests", []) if isinstance(prior, dict) else []
    if (not isinstance(prior, dict) or prior.get("source_row") != row or prior.get("song_id") != SONG_ID
            or prior.get("connection") != "direct" or prior.get("phase") != "failed"
            or prior.get("cleanup_verified") is not True or prior.get("play_click_attempted") is not False
            or prior.get("play_click_count") != 0 or prior.get("media_response_count") != 0
            or prior.get("trace_observer_errors") != 0 or prior.get("trace_limit_reached") is not False
            or not isinstance(diagnosis, dict) or diagnosis.get("code") != "login_rejected"
            or diagnosis.get("stage") != "home" or diagnosis.get("auth_http_status") != 200
            or diagnosis.get("authentication_result") != "failed" or not isinstance(requests, list)
            or any(not isinstance(event, dict) or event.get("operation") in {"GETdownload", "REGISTERwebplay", "REGISTERplay"} for event in requests)):
        raise ProbeFailure("prior_direct_login_uncertain")
    return True


def restore_saved_browser(saved, recorder, holders):
    """Restore only this bound saved cookie set; no password or new fingerprint."""
    recorder.bind(saved)
    browser = normal_browser_launch(headless=True, backend="cloakbrowser", direct=True)
    holder = RetainedBrowser(browser, recorder)
    holders.append(holder)  # Real browser cleanup is owned before context creation.
    context = holder.new_context(no_viewport=True)
    parsed = SimpleCookie()
    parsed.load(saved["requests"]["relations"]["headers"].get("cookie", ""))
    if not parsed:
        raise ProbeFailure("saved_browser_cookies_missing")
    context.add_cookies([{"name": name, "value": item.value, "domain": ".anghami.com",
                         "path": "/", "secure": True} for name, item in parsed.items()])
    return context.new_page()


class ExactRowPreparationVault:
    """Check the real selection before the helper reads any credentials."""
    def __init__(self, vault, row, digest):
        self.vault, self.row, self.digest = vault, row, digest
        self.path = vault.path

    def _row(self, row):
        if row != self.row:
            raise ProbeFailure("selected_row_changed")
        if hashlib.sha256((ROOT / "registered.txt").read_bytes()).hexdigest() != self.digest:
            raise ProbeFailure("source_changed")

    def select_test_candidates(self, count, *, start_row):
        if count != 1 or start_row != self.row:
            raise ProbeFailure("selected_row_changed")
        if require_new_egypt_account(self.vault, self.row, ROOT / "registered.txt") != self.digest:
            raise ProbeFailure("source_changed")
        return [self.row]

    def session(self, row):
        self._row(row)
        return self.vault.session(row)

    def record(self, row):
        self._row(row)
        return self.vault.record(row)

    def attach(self, row, saved, **options):
        self._row(row)
        return self.vault.attach(row, saved, **options)

    def enable_test_account(self, row):
        self._row(row)
        return self.vault.enable_test_account(row)


def offline_python_shape(duration):
    """Run only the selected legacy definitions against dummy local replies."""
    duration = numeric(duration, 600)
    if duration is None or duration <= 0:
        raise ProbeFailure("song_duration_invalid")
    calls = []

    class Reply:
        ok, status_code = True, 200
        def __init__(self, payload):
            self.payload = payload
        def json(self):
            return self.payload

    class LocalTransport:
        def get(self, url, *, params, headers):
            operation = params.get("type")
            if url != GATEWAY_URL or operation not in {"GETsong", "REGISTERwebplay"}:
                raise ProbeFailure("offline_shape_scope_invalid")
            if str(params.get("songId" if operation == "GETsong" else "songid")) != SONG_ID:
                raise ProbeFailure("offline_shape_song_invalid")
            event = {"operation": operation, "method": "GET", "query_parameter_names": safe_names(params),
                     "legacy_function_header_names": safe_names(headers), "body": {"format": "none", "field_names": []}}
            if operation == "REGISTERwebplay":
                event.update({"reported_play_seconds": float(params["playsecs"]), "reported_play_fraction": float(params["playper"])})
            calls.append(event)
            return Reply({"status": 1, "id": SONG_ID, "duration": duration} if operation == "GETsong" else {"status": "ok"})

    functions = _load_legacy_functions()
    functions["random"] = lambda: 0.0
    functions["play_song"](LocalTransport(), SONG_ID, "offline-fingerprint", "offline-sid")
    if [call["operation"] for call in calls] != ["GETsong", "REGISTERwebplay"]:
        raise ProbeFailure("offline_shape_sequence_invalid")
    return {"external_requests": 0, "actual_events_sent": 0, "audio_bytes": 0,
            "synthetic_full_duration": True, "requests": calls,
            "effective_header_source": "captured session headers; legacy function headers ignored by live adapter"}


def media_sample(page):
    raw = page.evaluate(MEDIA_SAMPLE_SCRIPT)
    if not isinstance(raw, dict):
        raise ProbeFailure("media_sample_invalid")
    result = {name: raw.get(name) is True for name in ("started", "ended", "paused", "scope_violation")}
    for name in ("play_calls", "blocked_calls", "current_time_seconds", "duration_seconds", "ready_state", "decoded_audio_bytes"):
        result[name] = numeric(raw.get(name))
    error = raw.get("media_error_code")
    result["media_error_code"] = error if type(error) is int and 1 <= error <= 4 else None
    result["completion_mode"] = raw.get("completion_mode") if raw.get("completion_mode") in {"natural_end", "target_boundary"} else None
    result["scope_reason"] = raw.get("scope_reason") if raw.get("scope_reason") in {"source_changed", "time_rewound", "duration_changed"} else None
    return result


def finish_playback(page, recorder, duration, checkpoint):
    started = time.monotonic()
    next_progress = 0
    maximum = min(600, duration + 60)
    previous = 0.0
    observed_progress = False
    while time.monotonic() - started <= maximum:
        parts = urlsplit(page.url)
        if parts.hostname != "play.anghami.com" or parts.path != "/song/" + SONG_ID:
            raise ProbeFailure("browser_song_scope_changed")
        sample = media_sample(page)
        recorder.report["playback"] = sample
        current = sample["current_time_seconds"] or 0
        if sample["scope_violation"]:
            raise ProbeFailure("browser_media_scope_changed")
        actual_duration = sample["duration_seconds"]
        if (current < previous - 1 or (actual_duration is not None and actual_duration > 0
                                      and abs(actual_duration - duration) > 2)):
            raise ProbeFailure("browser_media_scope_changed")
        if current > previous + 0.2 and sample["ready_state"] is not None and sample["ready_state"] >= 3:
            observed_progress = True
        previous = max(previous, current)
        if sample["media_error_code"] is not None:
            raise ProbeFailure("browser_media_error")
        if sample["ended"]:
            recorder.finished = True
            actual_duration = sample["duration_seconds"]
            decoded = sample["decoded_audio_bytes"]
            if (not observed_progress or not sample["started"] or current < duration - 2
                    or actual_duration is None or abs(actual_duration - duration) > 2
                    or (decoded is not None and decoded <= 0)):
                raise ProbeFailure("browser_playback_incomplete")
            recorder.report["playback"]["natural_end_verified"] = sample["completion_mode"] == "natural_end"
            recorder.report["playback"]["completed_test_song_verified"] = True
            recorder.report["playback"]["observed_progress_verified"] = observed_progress
            recorder.report["playback"]["decoded_audio_verified"] = decoded is not None and decoded > 0
            checkpoint()
            return
        elapsed = time.monotonic() - started
        if elapsed >= next_progress:
            checkpoint()
            print(json.dumps({"source_row": recorder.report["source_row"], "phase": "playback", "elapsed_seconds": round(elapsed, 1), "current_time_seconds": current}), flush=True)
            next_progress = elapsed + 30
        page.wait_for_timeout(1000)
    raise ProbeFailure("browser_playback_timeout")


def compare_shapes(report):
    expected = report.get("python_request_shape", {}).get("requests", [])
    comparisons = []
    for item in expected:
        observed = [event for event in report["requests"] if event["operation"] == item["operation"]
                    and (event["song_matches_test_song"] is True if item["operation"] == "GETsong"
                         else event["song_matches_test_song"] is not False)]
        python_names = set(item["query_parameter_names"])
        comparisons.append({
            "operation": item["operation"], "browser_request_count": len(observed),
            "python_method": item["method"], "browser_methods": sorted({event["method"] for event in observed}),
            "browser_only_query_names": sorted(set().union(*(set(event["query_parameter_names"]) for event in observed)) - python_names),
            "python_only_query_names": sorted(python_names - set().union(*(set(event["query_parameter_names"]) for event in observed))) if observed else [],
            "browser_only_header_names": sorted(set().union(*(set(event["request_header_names"]) for event in observed)) - set(report.get("python_effective_header_names", []))),
            "browser_body_formats": sorted({event["body"]["format"] for event in observed}),
            "opaque_song_target_count": sum(event["song_matches_test_song"] is None for event in observed),
        })
    accounting = [event for event in report["requests"] if event["operation"] in {"REGISTERwebplay", "REGISTERplay"}
                  and event["phase"] in {"playback", "finishing"} and event["song_matches_test_song"] is not False]
    accounting_observed = bool(accounting)
    unknown_operations = sum(event["operation"] == "other_gateway_operation" for event in report["requests"])
    coverage = (accounting_observed and report["trace_observer_errors"] == 0 and not report["trace_limit_reached"] and unknown_operations == 0)
    playback = report.get("playback", {})
    decoded_delivery = (numeric(playback.get("decoded_audio_bytes")) is not None and playback["decoded_audio_bytes"] > 0
                        and numeric(playback.get("ready_state")) is not None and playback["ready_state"] >= 3
                        and playback.get("observed_progress_verified") is True)
    report["comparison"] = {
        "scope": "request names and transport shape; response acceptance is not proof of processed statistics",
        "operations": comparisons,
        "browser_gateway_operation_counts": dict(Counter(event["operation"] for event in report["requests"])),
        "python_performs_audio_delivery": False,
        "browser_performs_audio_delivery": decoded_delivery or report["media_response_count"] > 0,
        "browser_audio_delivery_evidence": "decoded_media_progress" if decoded_delivery else "classified_network_response" if report["media_response_count"] > 0 else "unverified",
        "unknown_host_media_response_count": report.get("unknown_host_media_response_count", 0),
        "unknown_gateway_operation_count": unknown_operations,
        "coverage_partial": unknown_operations > 0 or report["trace_limit_reached"] or report["trace_observer_errors"] > 0,
        "completed_browser_playback_verified": report.get("playback", {}).get("natural_end_verified") is True,
        "completion_accounting_observed": accounting_observed,
        "accounting_api_acceptance_verified": any(event["api_status"] == "ok" and event["http_status"] == 200 for event in accounting),
        "request_flow_comparison_conclusive": coverage,
        "payload_semantics_comparison_complete": coverage and all(event.get("reported_play_seconds") is not None and event.get("reported_play_fraction") is not None for event in accounting),
        "encrypted_accounting_payload_observed": any(event["body"]["encrypted"] for event in accounting),
        "downstream_statistics_verified": False,
    }


def browser_dom_state(page):
    """Return state booleans only; DOM text stays inside the browser."""
    raw = page.evaluate("""() => {
      const visible = x => !!x && !!(x.offsetWidth || x.offsetHeight || x.getClientRects().length);
      const buttons = [...document.querySelectorAll('button')];
      const text = document.body?.innerText ?? '';
      return {play_button_visible: buttons.some(x => visible(x) && (x.getAttribute('aria-label') === 'Play' || x.innerText.trim() === 'Play')),
        pause_button_visible: buttons.some(x => visible(x) && (x.getAttribute('aria-label') === 'Pause' || x.innerText.trim() === 'Pause')),
        dialog_count: [...document.querySelectorAll('[role="dialog"]')].filter(visible).length,
        verification_text_visible: /captcha|human verification|verification required|verify your|challenge required/i.test(text),
        license_text_visible: /license rights|not available in your country|unavailable in your country/i.test(text)};
    }""")
    if not isinstance(raw, dict):
        return {}
    return {**{key: raw.get(key) is True for key in ("play_button_visible", "pause_button_visible", "verification_text_visible", "license_text_visible")},
            "dialog_count": numeric(raw.get("dialog_count"), 100)}


def settle_browser_auth(page, recorder, checkpoint):
    # Navigating in the retained browser triggers normal SDK reauthentication.
    # A visible Play button can appear before that request has finished.
    page.wait_for_timeout(5000)
    for _ in range(30):
        events = [event for event in recorder.report["requests"]
                  if event["phase"] == "song_page" and event["operation"] == "authenticate"]
        latest = events[-1] if events else None
        if latest is None or latest.get("http_status") is not None or latest.get("failed"):
            recorder.report["browser_dom_state"] = browser_dom_state(page)
            checkpoint()
            if latest is not None:
                diagnostic = latest.get("authentication_diagnostic", {})
                if diagnostic.get("verification_required") is True:
                    raise ProbeFailure("browser_verification_required")
                if diagnostic.get("selected_account_identity_matched") is False:
                    raise ProbeFailure("browser_identity_mismatch")
                if diagnostic.get("semantic_status") in {"failed", "fail", "error", 0}:
                    raise ProbeFailure("browser_authentication_rejected")
                if latest.get("failed") or latest.get("http_status") != 200:
                    raise ProbeFailure("browser_authentication_transport_failed")
            if recorder.report["browser_dom_state"].get("verification_text_visible"):
                raise ProbeFailure("browser_verification_required")
            if recorder.report["browser_dom_state"].get("license_text_visible"):
                raise ProbeFailure("browser_song_unavailable")
            return
        page.wait_for_timeout(1000)
    raise ProbeFailure("browser_authentication_timeout")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--row", type=int, required=True, help="One exact new EG source row")
    parser.add_argument("--run", action="store_true", help="Prepare this row and play the test song once")
    parser.add_argument("--direct", action="store_true", help="Use local internet for the approved prepared-row follow-up")
    parser.add_argument("--use-prepared", action="store_true", help="Refresh the same prepared EG account after a confirmed proxy attempt with no playback")
    parser.add_argument("--saved-session", action="store_true", help="Restore the validated saved session after a confirmed rejected Direct login with no playback")
    args = parser.parse_args(argv)
    if args.row < 1:
        parser.error("Choose one positive source row")
    if args.direct != args.use_prepared:
        parser.error("The local follow-up requires both --direct and --use-prepared")
    if args.saved_session and not (args.direct and args.use_prepared):
        parser.error("Saved-session restoration requires --direct and --use-prepared")
    holders = []
    report = {
        "started_at_utc": now(), "source_row": args.row, "song_id": SONG_ID,
        "phase": "preview", "passed": False, "browser": "cloakbrowser", "headless": True,
        "reduce_browser_data": False, "connection": "direct" if args.direct else "proxy_egypt", "proxy_country": None if args.direct else "EG",
        "account_source_country": "EG", "reuse_prepared_account": args.use_prepared,
        "saved_session_restored": False, "password_required": not args.saved_session,
        "service_workers_blocked": True, "browser_cache_disabled_by_request_guard": True,
        "source_verified": False, "prepared": False, "server_account_identity_verified": False,
        "song_metadata_verified": False, "play_click_attempted": False, "play_click_count": 0,
        "python_play_events_sent": 0, "like_events_sent": 0, "automatic_retry": False,
        "requests": [], "media_response_count": 0, "media_success_response_count": 0,
        "unknown_host_media_response_count": 0,
        "trace_observer_errors": 0, "trace_limit_reached": False, "blocked_out_of_scope_requests": 0,
        "request_failure_counts": {}, "page_error_events": [],
        "cleanup_verified": False, "downstream_statistics_verified": False,
    }
    suffix = "-direct-saved" if args.saved_session else "-direct" if args.direct else ""
    path = ROOT / ".anghami" / f"browser-request-flow-row-{args.row}-{SONG_ID}{suffix}.redacted.json"
    recorder = FlowRecorder(report)
    can_write_report = False
    runtime = ExitStack()
    restored_page = None

    def checkpoint():
        if args.run and can_write_report:
            _journal(report, path)

    def stage(value):
        report["phase"] = value
        checkpoint()
        print(json.dumps({"source_row": args.row, "phase": value, "prepared": report["prepared"], "gateway_requests_observed": len(report["requests"])}), flush=True)

    try:
        if args.run and path.exists():
            prior = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(prior, dict) or prior.get("play_click_attempted"):
                raise ProbeFailure("previous_playback_attempt_exists")
        can_write_report = True
        with AccountVault() as vault:
            if args.use_prepared:
                digest = require_prepared_egypt_account(vault, args.row, ROOT / "registered.txt")
                require_no_playback_in_prior_proxy_attempt(args.row)
                report["prior_proxy_attempt_confirmed_no_playback"] = True
                if args.saved_session:
                    require_no_playback_in_rejected_direct_login(args.row)
                    report["prior_direct_login_confirmed_no_playback"] = True
            else:
                digest = require_new_egypt_account(vault, args.row, ROOT / "registered.txt")
        if read_test_song_id(SETTINGS_PATH) != SONG_ID:
            raise ProbeFailure("declared_test_song_changed")
        report["source_verified"] = True
        if not args.run:
            print(json.dumps({"dry_run": True, "source_row": args.row, "country": "EG", "song_id": SONG_ID, "browser": "cloakbrowser", "connection": report["connection"], "external_requests": 0}), flush=True)
            return 0
        if args.direct:
            runtime.enter_context(direct_runtime(browser=False))
        try:
            report["cloakbrowser_python_version"] = public_version(version("cloakbrowser"))
        except PackageNotFoundError:
            raise ProbeFailure("cloakbrowser_not_installed") from None
        proxy = None
        if not args.direct:
            stage("proxy_preflight")
            proxy = load_packetstream_proxy()
            proof = proxy.verify_country()
            if proxy.country != "EG" or proof.get("country_verified") is not True or proof.get("proxy_used") is not True:
                raise ProbeFailure("proxy_unverified")
            report["proxy"] = proxy.summary()
            report["proxy_country_verified"] = True
        stage("preparation")
        def preparation_progress(current):
            value = current.get("phase")
            if value in {"session_lookup", "login", "validation", "complete", "stopped"}:
                changed = report.get("preparation_phase") != value
                report["preparation_phase"] = value
                checkpoint()
                if changed:
                    print(json.dumps({"source_row": args.row, "phase": "preparation", "preparation_phase": value}), flush=True)
        with AccountVault() as vault, retain_login_browser(recorder, holders, direct=args.direct):
            if args.use_prepared:
                if require_prepared_egypt_account(vault, args.row, ROOT / "registered.txt") != digest:
                    raise ProbeFailure("source_changed")
                if args.saved_session:
                    saved = vault.session(args.row)
                    report["preparation_phase"] = "saved_session_restoration"
                    checkpoint()
                    restored_page = restore_saved_browser(saved, recorder, holders)
                    report["saved_session_restored"] = True
                else:
                    report["preparation_phase"] = "login"
                    checkpoint()
                    record = vault.record(args.row)
                    try:
                        saved, _metadata = capture.capture_login(email=record["email"], password=record["password"],
                                                                browser_backend="cloakbrowser", headless=True,
                                                                reduce_browser_data=False)
                    finally:
                        record.clear()
                    vault.attach(args.row, saved)
                    vault.enable_test_account(args.row)
            else:
                if require_new_egypt_account(vault, args.row, ROOT / "registered.txt") != digest:
                    raise ProbeFailure("source_changed")
                prepared = prepare_test_accounts(ExactRowPreparationVault(vault, args.row, digest), count=1, start_row=args.row, proxy=proxy,
                                                browser_backend="cloakbrowser", headless=True,
                                                reduce_browser_data=False,
                                                progress=preparation_progress)
                if prepared.get("selected_rows") != [args.row] or prepared.get("prepared_rows") != [args.row] or prepared.get("passed") is not True:
                    raise ProbeFailure("preparation_incomplete")
            saved = vault.session(args.row)
            recorder.bind(saved)
        report["prepared"] = True
        if len(holders) != 1 or holders[0].context is None:
            raise ProbeFailure("login_context_missing")
        report["browser_version"] = public_version(holders[0].browser.version)
        stage("identity_and_song_validation")
        with AccountVault() as vault, vault._http_session(args.row, proxy) as session:
            checked = session.check(negative_control=True)
            if checked.get("authenticated") is not True or checked.get("without_session", {}).get("authentication_rejected") is not True:
                raise ProbeFailure("session_validation_failed")
            PlaybackGateway(session).bootstrap()
            report["server_account_identity_verified"] = True
            song = session.song(SONG_ID)
            raw_duration = song.get("duration")
            duration = numeric(float(raw_duration), 600) if type(raw_duration) in (int, float, str) else None
            if str(song.get("id")) != SONG_ID or song.get("error") or duration is None or duration <= 0:
                raise ProbeFailure("song_metadata_invalid")
            report["song_metadata_verified"] = True
            report["song_duration_seconds"] = duration
            report["python_request_shape"] = offline_python_shape(duration)
        stage("song_page")
        if restored_page is not None:
            page = restored_page
        else:
            pages = [page for page in holders[0].context.pages if urlsplit(page.url).hostname == "play.anghami.com"]
            if len(pages) != 1:
                raise ProbeFailure("login_page_ambiguous")
            page = pages[0]
        page.goto(SONG_URL, wait_until="domcontentloaded", timeout=45000)
        page.get_by_role("button", name="Play", exact=True).wait_for(state="visible", timeout=30000)
        settle_browser_auth(page, recorder, checkpoint)
        if hashlib.sha256((ROOT / "registered.txt").read_bytes()).hexdigest() != digest or read_test_song_id(SETTINGS_PATH) != SONG_ID:
            raise ProbeFailure("source_or_song_changed")
        stage("playback")
        recorder.playback_armed = True
        page.evaluate("duration => { window.__boundedPlaybackState.expectedDuration = duration; window.__boundedPlaybackState.armed = true; }", duration)
        report["play_click_attempted"] = True
        report["play_click_count"] = 1
        checkpoint()
        page.get_by_role("button", name="Play", exact=True).click()
        finish_playback(page, recorder, duration, checkpoint)
        report["playback_passed"] = True
        stage("finishing")
        # Permit the normal completion accounting response to settle, while
        # the media guard and request guard prevent a following song playback.
        page.wait_for_timeout(5000)
        compare_shapes(report)
        report["completion_accounting_observed"] = report["comparison"]["completion_accounting_observed"]
        report["accounting_api_acceptance_verified"] = report["comparison"]["accounting_api_acceptance_verified"]
        report["browser_play_record_requests_seen"] = sum(event["operation"] in {"REGISTERwebplay", "REGISTERplay"} for event in report["requests"])
        report["passed"] = True
        stage("complete")
    except BaseException as exc:
        report["failed_phase"] = report["phase"]
        report["phase"] = "failed"
        report["passed"] = False
        report["error_code"] = exc.code if isinstance(exc, ProbeFailure) else "cancelled" if isinstance(exc, (KeyboardInterrupt, EOFError)) else "probe_failed"
        diagnosis = safe_login_failure(exc)
        if diagnosis:
            report["login_failure"] = diagnosis
        checkpoint()
    finally:
        cleanup_ok = True
        for holder in holders:
            try:
                for page in holder.context.pages if holder.context is not None else []:
                    try:
                        page.evaluate("() => { const s = window.__boundedPlaybackState; if (s) s.armed = false; for (const x of window.__observedPlaybackMedia ?? []) x.pause(); }")
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                holder.real_close()
            except Exception:
                cleanup_ok = False
        report["cleanup_verified"] = cleanup_ok
        if not cleanup_ok:
            report["passed"] = False
            report["error_code"] = "browser_cleanup_failed"
        try:
            runtime.close()
        except Exception:
            report["passed"] = False
            report["error_code"] = "direct_runtime_cleanup_failed"
        report["finished_at_utc"] = now()
        recorder.private.clear()
        recorder.sid = recorder.fingerprint = recorder.expected_email = None
        checkpoint()
    print(json.dumps({key: report.get(key) for key in (
        "source_row", "song_id", "passed", "phase", "error_code", "prepared",
        "server_account_identity_verified", "song_metadata_verified", "play_click_count",
        "media_response_count", "browser_play_record_requests_seen", "cleanup_verified",
    )}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
