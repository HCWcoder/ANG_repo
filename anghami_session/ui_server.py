"""A loopback-only console for the existing saved-session test workflow."""

import argparse
from datetime import datetime, timezone
import hmac
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .errors import SessionError
from .proxy import load_packetstream_proxy, save_packetstream_credentials
from .test_settings import read_test_song_id, write_test_song_id
from .ui_jobs import JobBusyError, JobManager, JobValidationError
from .vault import AccountVault, DEFAULT_VAULT_PATH

ASSETS = Path(__file__).with_name("ui")
MAX_BODY = 16 * 1024
MAX_REPORT = 2 * 1024 * 1024
REPORT_NAME = re.compile(
    r"(?:account-[1-9][0-9]*\.(?:test-(?:like|play-record)(?:-batch)?|http-playback|like-readiness)-report|"
    r"test-(?:like|play-record)\.accounts-batch-report|accounts-prepare-tests-report|"
    r"accounts-last-operation|packetstream-(?:egypt|browser-proxy)-report|ui-last-job)\.json\Z"
)
# Existing reports come from several probes. Expose outcome fields only, never
# arbitrary request/response data or secrets from an older probe.
REPORT_KEYS = frozenset("""
    checked_at_utc tested_at_utc started_at finished_at created_at_utc id action
    source_row account_row song_id passed phase failed_phase failed_row failed_test
    selected_rows prepared_rows requested_accounts prepared_account_count attempted_accounts
    completed_accounts requested_tests completed_tests attempted_tests tests_per_account
    active_row active_test dry_run browser headless reduce_browser_data no_browser preparation_method connection automatic_retry automatic_retries
    results progress completed total status message error code error_code error_type
    provider country requested_country observed_country endpoint sticky sticky_session_requested
    configured country_verified proxy_used used_proxy proxy_connect_http_status proxy_connect_status
    http_status api_status operations without_session authentication_rejected authenticated
    server_account_identity_verified song_metadata_verified metadata_verified metadata_duration_seconds
    negative_control_passed browser_required password_required browser_used audio_requested audio_bytes
    play_events_sent like_events_sent browser_country_verified http_country_check proxy exit_check
    event_attempted event_attempts event_accepted event_result event_http_status mutation_attempted
    mutation_attempts mutation_accepted mutation_result mutation_http_status persisted_state_verified
    liked_before liked_after state_read_method reported_play_seconds reported_play_fraction
    downstream_statistics_verified elapsed_seconds bandwidth measurement scope get_song play_song total
    request_count request_bytes request_body_bytes response_body_bytes response_header_bytes
    measurement_complete download_bytes upload_bytes header_bytes http_transfer_bytes balance_verified
    proxy_requests_attempted origin_requests_sent saved_session_read_attempted saved_session_read_worked
    egypt_route_verified proxy_access_worked basic_proxy_auth_worked rejection_also_without_country_selection
    checks operation account_metadata readiness transport test_rows ready_rows accounts state session_saved
    records unique_accounts duplicate_rows sessions_saved title artist duration_seconds
    rows count proxy_egypt report_code test_number result_unknown upload_body_bytes download_body_bytes
    song_duration_seconds
""".split())
SAFE_WORDS = frozenset("""
    ok failed succeeded queued running stopped complete preview preparing ready login_required check_failed
    direct proxy_egypt EG chrome cloakbrowser none browser http PacketStream accepted rejected unknown not_attempted
    skipped_already_liked session_validation legacy_source preflight account_identity metadata event
    mutation state_before state_after session_lookup session_recovery login validation proxy_preflight proxy_preflight_failed
    test_failed preparation_failed cancelled metadata_region_unavailable metadata_invalid event_incomplete
    event_rejected event_unknown state_http_failed mutation_failed mutation_incomplete mutation_unknown
    readback_not_liked journal_failed request_scope_invalid song_scope_invalid session_invalid
    relations playlists prepare play like check song proxy-check proxy_authentication_rejected
    test-like test-play-record GETuserrelations GETplaylists curl_cffi
""".split())


def safe_report(value, key="", depth=0):
    if depth > 20:
        return None
    if isinstance(value, dict):
        return {name: safe_report(item, name, depth + 1) for name, item in value.items() if name in REPORT_KEYS}
    if isinstance(value, list):
        return [safe_report(item, key, depth + 1) for item in value[:500]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if value in SAFE_WORDS or (key in {"song_id"} and value.isascii() and value.isdecimal()):
            return value
        if key in {"checked_at_utc", "tested_at_utc", "started_at", "finished_at", "created_at_utc"}:
            try:
                return datetime.fromisoformat(value).isoformat()
            except ValueError:
                return None
        if key == "endpoint" and value == "https://proxy.packetstream.io:31111":
            return value
        return "[redacted]"
    return None


class ConsoleService:
    def __init__(self, vault_path=DEFAULT_VAULT_PATH, manager=None):
        self.vault_path = Path(vault_path).resolve()
        self.manager = manager if manager is not None else JobManager(self.vault_path)
        self.lock = threading.RLock()
        self.test_settings_path = self.vault_path.parent / "test-settings.json"

    def reports(self):
        folder = self.vault_path.parent
        result = []
        if folder.is_dir():
            for path in folder.glob("*.json"):
                if not REPORT_NAME.fullmatch(path.name) or path.resolve().parent != folder.resolve():
                    continue
                info = path.stat()
                if info.st_size <= MAX_REPORT:
                    result.append({
                        "name": path.name, "size": info.st_size,
                        "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                    })
        return sorted(result, key=lambda item: item["modified_at"], reverse=True)[:100]

    def read_report(self, name):
        if not isinstance(name, str) or not REPORT_NAME.fullmatch(name):
            raise FileNotFoundError
        path = self.vault_path.parent / name
        if path.resolve().parent != self.vault_path.parent.resolve() or not path.is_file():
            raise FileNotFoundError
        if path.stat().st_size > MAX_REPORT:
            raise ValueError
        return {"name": name, "report": safe_report(json.loads(path.read_text(encoding="utf-8")))}

    def state(self):
        vault_summary = {"records": 0, "unique_accounts": 0, "sessions_saved": 0}
        cohort = {"test_rows": [], "ready_rows": [], "accounts": []}
        vault_available = False
        try:
            with AccountVault(self.vault_path) as vault:
                summary = vault.summary()
                vault_summary = {name: summary[name] for name in ("records", "unique_accounts", "sessions_saved", "duplicate_rows", "states")}
                cohort = vault.test_accounts()
                vault_available = True
        except (SessionError, OSError):
            pass
        proxy = {"configured": False, "provider": "PacketStream", "country": "EG"}
        try:
            profile = load_packetstream_proxy(self.vault_path.parent / "packetstream.dpapi")
            proxy = {"configured": True, **profile.summary()}
        except SessionError:
            pass
        return {
            "vault": vault_summary, "vault_available": vault_available,
            "cohort": cohort, "proxy": proxy, "test_song_id": read_test_song_id(self.test_settings_path),
            "limits": {"accounts": 5, "test_accounts": len(cohort["ready_rows"]), "tests_per_account": 5},
            "job": self.manager.snapshot(), "reports": self.reports(),
        }

    def submit(self, payload):
        with self.lock:
            return self.manager.submit(payload)

    def save_test_song(self, payload):
        if set(payload) != {"song_id"}:
            raise ValueError
        with self.lock:
            job = self.manager.snapshot()
            if job and job["status"] in {"queued", "running"}:
                raise JobBusyError("An operation is running.")
            song_id = write_test_song_id(payload["song_id"], self.test_settings_path)
            return {"test_song_id": song_id}

    def find(self, payload):
        if set(payload) != {"email"} or not isinstance(payload["email"], str) or len(payload["email"]) > 254:
            raise ValueError
        with AccountVault(self.vault_path) as vault:
            return {"source_rows": vault.find(payload["email"])}

    def save_proxy(self, payload):
        if set(payload) != {"username", "auth_key"}:
            raise ValueError
        if not all(isinstance(payload[name], str) and len(payload[name]) <= 256 for name in payload):
            raise ValueError
        with self.lock:
            job = self.manager.snapshot()
            if job and job["status"] in {"queued", "running"}:
                raise JobBusyError("An operation is running.")
            return {"configured": True, **save_packetstream_credentials(
                payload["username"], payload["auth_key"], self.vault_path.parent / "packetstream.dpapi",
            )}


class ConsoleHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        self._body_consumed = False

    def log_message(self, *_):
        # URLs, request bodies and credentials must never appear in access logs.
        pass

    def _send(self, status, value, mime="application/json; charset=utf-8"):
        data = json.dumps(value, allow_nan=False).encode() if mime.startswith("application/json") else value
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Anghami-Test-Console", "1")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, code, message):
        # Drain small rejected POSTs before closing the socket. On Windows,
        # unread request bytes can reset the connection and hide the response.
        if self.command == "POST" and not self._body_consumed:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) == 1 and not self.headers.get("Transfer-Encoding"):
                value = lengths[0]
                if value.isascii() and value.isdecimal() and int(value) <= MAX_BODY * 2:
                    try:
                        self.rfile.read(int(value))
                    except (OSError, TimeoutError):
                        pass
            self._body_consumed = True
        self._send(status, {"error": {"code": code, "message": message}})

    def _allowed_request(self, api=False):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self._error(403, "host_rejected", "Use this console's local address.")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allowed_origins:
            self._error(403, "origin_rejected", "This request did not come from the local console.")
            return False
        if api:
            token = self.headers.get("X-App-Token", "")
            if not token.isascii() or not hmac.compare_digest(token, self.server.token):
                self._error(403, "token_rejected", "Reload the console and try again.")
                return False
        return True

    def do_GET(self):
        try:
            parts = urlsplit(self.path)
        except ValueError:
            self._error(400, "invalid_path", "The requested local address is invalid.")
            return
        if not self._allowed_request(api=parts.path.startswith("/api/")):
            return
        try:
            if parts.path == "/api/state" and not parts.query:
                self._send(200, self.server.service.state())
            elif parts.path == "/api/job" and not parts.query:
                self._send(200, {"job": self.server.service.manager.snapshot()})
            elif parts.path == "/api/reports":
                query = parse_qs(parts.query, keep_blank_values=True)
                if set(query) != {"name"} or len(query["name"]) != 1:
                    raise FileNotFoundError
                self._send(200, self.server.service.read_report(query["name"][0]))
            elif parts.path in {"/", "/index.html", "/styles.css", "/selection.js", "/app.js"} and not parts.query:
                name = "index.html" if parts.path in {"/", "/index.html"} else parts.path[1:]
                raw = (ASSETS / name).read_bytes()
                if name == "index.html":
                    raw = raw.replace(b"__APP_TOKEN__", self.server.token.encode())
                mime = {"index.html": "text/html; charset=utf-8", "styles.css": "text/css; charset=utf-8", "selection.js": "text/javascript; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}[name]
                self._send(200, raw, mime)
            else:
                self._error(404, "not_found", "This item is not available.")
        except FileNotFoundError:
            self._error(404, "not_found", "This item is not available.")
        except Exception:
            self._error(500, "read_failed", "The local data could not be loaded. Refresh and try again.")

    def do_POST(self):
        try:
            parts = urlsplit(self.path)
        except ValueError:
            self._error(400, "invalid_path", "The requested local address is invalid.")
            return
        if not self._allowed_request(api=True):
            return
        if parts.query or parts.path not in {"/api/jobs", "/api/find", "/api/proxy", "/api/test-song"}:
            self._error(404, "not_found", "This action is not available.")
            return
        if self.headers.get_content_type() != "application/json":
            self._error(415, "json_required", "Send this action from the console.")
            return
        try:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError
            length_text = self.headers.get("Content-Length", "")
            if not length_text.isascii() or not length_text.isdecimal():
                raise ValueError
            length = int(length_text)
            if length > MAX_BODY:
                self._error(413, "body_too_large", "This request is too large.")
                return
            raw = self.rfile.read(length)
            self._body_consumed = True
            if len(raw) != length:
                raise ValueError
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except Exception:
            self._error(400, "invalid_json", "The action could not be read. Check its fields and try again.")
            return
        try:
            if parts.path == "/api/jobs":
                self._send(202, {"job": self.server.service.submit(payload)})
            elif parts.path == "/api/find":
                self._send(200, self.server.service.find(payload))
            elif parts.path == "/api/test-song":
                self._send(200, self.server.service.save_test_song(payload))
            else:
                self._send(200, self.server.service.save_proxy(payload))
        except JobBusyError:
            self._error(409, "busy", "Another operation is running. Wait for it to finish.")
        except (JobValidationError, ValueError, SessionError):
            self._error(400, "invalid_action", "Check the selected accounts, counts and settings, then try again.")
        except Exception:
            self._error(500, "action_failed", "The action could not be started. Check the latest report.")


def create_server(*, port=0, service=None):
    server = ThreadingHTTPServer(("127.0.0.1", port), ConsoleHandler)
    server.daemon_threads = True
    server.token = secrets.token_urlsafe(32)
    server.service = ConsoleService() if service is None else service
    actual_port = server.server_address[1]
    server.base_url = f"http://127.0.0.1:{actual_port}"
    server.allowed_hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
    server.allowed_origins = {f"http://{host}" for host in server.allowed_hosts}
    return server


def _existing_console(port):
    if not port:
        return False
    connection = HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        recognized = response.status == 200 and response.getheader("X-Anghami-Test-Console") == "1"
        if recognized:
            response.read(64 * 1024)
        return recognized
    except (OSError, ValueError, HTTPException):
        return False
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("Choose a valid local port.")
    try:
        if _existing_console(args.port):
            url = f"http://127.0.0.1:{args.port}"
            print(f"Anghami Test Console: {url}", flush=True)
            print("Using the console already running.", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        try:
            server = create_server(port=args.port)
        except OSError:
            server = create_server(port=0)
        print(f"Anghami Test Console: {server.base_url}", flush=True)
        print("Keep this process running while using the console.", flush=True)
        if not args.no_browser:
            webbrowser.open(server.base_url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except Exception:
        print("The local console could not start. Check the environment and try again.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
