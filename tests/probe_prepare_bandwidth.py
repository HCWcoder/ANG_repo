"""Measure one normal account preparation; without --run this is offline.

The relay counts the stream inside PacketStream's outer TLS connection. It
forwards opaque bytes and never records URLs, headers, credentials or payloads.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import select
import socket
import ssl
import sys
from threading import Event, RLock, Thread, current_thread
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UPSTREAM_HOST = "proxy.packetstream.io"
UPSTREAM_PORT = 31111
BYTE_LIMIT = 200_000_000
REPORT_PATH = ROOT / ".anghami" / "preparation-bandwidth-report.json"
BUFFER_SIZE = 65_536


class MeteredRelay:
    """Private, fixed-upstream proxy relay with integer byte counters."""

    def __init__(self, *, byte_limit=BYTE_LIMIT, time_limit=180.0, idle_timeout=45.0):
        if type(byte_limit) is not int or byte_limit < 1:
            raise ValueError("The byte limit must be positive.")
        if time_limit <= 0 or idle_timeout <= 0:
            raise ValueError("The time limits must be positive.")
        self.byte_limit = byte_limit
        self.time_limit = time_limit
        self.idle_timeout = idle_timeout
        self._lock = RLock()
        self._stopping = Event()
        self._sockets = set()
        self._workers = set()
        self._listener = None
        self._accept_thread = None
        self._started = None
        self._finished = None
        self._upload = self._download = self._connections = self._failures = 0
        self._limited = False

    @property
    def proxy_url(self):
        if self._listener is None:
            raise RuntimeError("Start the relay first.")
        return f"http://127.0.0.1:{self._port}"

    def start(self):
        if self._started is not None:
            raise RuntimeError("The relay cannot be restarted.")
        self._started = time.monotonic()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._port = self._listener.getsockname()[1]
        self._listener.listen(32)
        self._listener.settimeout(0.2)
        self._accept_thread = Thread(target=self._accept, name="bandwidth-relay", daemon=True)
        self._accept_thread.start()
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

    def snapshot(self):
        with self._lock:
            end = self._finished if self._finished is not None else time.monotonic()
            return {
                "upload_bytes": self._upload, "download_bytes": self._download,
                "total_bytes": self._upload + self._download,
                "connections": self._connections, "failures": self._failures,
                "duration_seconds": round(0.0 if self._started is None else end - self._started, 3),
                "limited": self._limited,
            }

    def _track(self, connection):
        with self._lock:
            if self._stopping.is_set():
                connection.close()
                raise OSError("Relay closed.")
            self._sockets.add(connection)
        return connection

    def _disconnect(self, connection):
        if connection is None:
            return
        with self._lock:
            self._sockets.discard(connection)
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

    def _stop(self, *, limited=False):
        with self._lock:
            self._limited |= limited
            self._stopping.set()
            sockets = tuple(self._sockets)
        if self._listener is not None:
            self._listener.close()
        for connection in sockets:
            self._disconnect(connection)

    def close(self):
        self._stop()
        deadline = time.monotonic() + 3.0
        with self._lock:
            threads = [self._accept_thread, *self._workers]
        for thread in threads:
            if thread is not None and thread is not current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            if any(thread is not None and thread.is_alive() for thread in threads):
                self._failures += 1
            if self._finished is None:
                self._finished = time.monotonic()

    def _accept(self):
        while not self._stopping.is_set():
            if time.monotonic() - self._started >= self.time_limit:
                self._stop(limited=True)
                return
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._track(client)
            except OSError:
                return
            with self._lock:
                if len(self._workers) >= 64:
                    self._failures += 1
                    self._disconnect(client)
                    continue
                self._connections += 1
                worker = Thread(target=self._forward, args=(client,), name="bandwidth-tunnel", daemon=True)
                self._workers.add(worker)
                worker.start()

    def _connect_upstream(self):
        # All destinations are tunneled through this one verified provider.
        family, kind, protocol, _, address = socket.getaddrinfo(
            UPSTREAM_HOST, UPSTREAM_PORT, type=socket.SOCK_STREAM,
        )[0]
        raw = self._track(socket.socket(family, kind, protocol))
        raw.settimeout(10)
        raw.connect(address)
        context = ssl.create_default_context()
        context.set_alpn_protocols(["http/1.1"])
        upstream = context.wrap_socket(raw, server_hostname=UPSTREAM_HOST, do_handshake_on_connect=False)
        with self._lock:
            self._sockets.discard(raw)
        self._track(upstream)
        upstream.do_handshake()
        return upstream

    def _send_upstream(self, upstream, buffer):
        # Serialize the counter and nonblocking operation to keep a global cap.
        with self._lock:
            remaining = self.byte_limit - self._upload - self._download
            if remaining <= 0:
                self._stop(limited=True)
                return 0
            sent = upstream.send(memoryview(buffer)[:remaining])
            self._upload += sent
            if self._upload + self._download >= self.byte_limit:
                self._stop(limited=True)
            return sent

    def _receive_upstream(self, upstream):
        with self._lock:
            remaining = self.byte_limit - self._upload - self._download
            if remaining <= 0:
                self._stop(limited=True)
                return b""
            data = upstream.recv(min(BUFFER_SIZE, remaining))
            # Count received bytes even if the local browser has disconnected.
            self._download += len(data)
            if self._upload + self._download >= self.byte_limit:
                self._stop(limited=True)
            return data

    def _forward(self, client):
        upstream = None
        established = False
        try:
            upstream = self._connect_upstream()
            self._track(upstream)  # Also supports the offline socket test double.
            established = True
            client.setblocking(False)
            upstream.setblocking(False)
            upload = bytearray()
            download = bytearray()
            client_eof = upstream_eof = False
            last_activity = time.monotonic()
            client_closed_at = None
            while not self._stopping.is_set():
                now = time.monotonic()
                if now - last_activity >= self.idle_timeout:
                    break
                if client_eof and not upload and client_closed_at is not None and now - last_activity >= 0.25:
                    break
                if upstream_eof and not download:
                    break
                reads = []
                writes = []
                if not client_eof and len(upload) < BUFFER_SIZE:
                    reads.append(client)
                if not upstream_eof and (client_eof or len(download) < BUFFER_SIZE):
                    reads.append(upstream)
                if upload and not upstream_eof:
                    writes.append(upstream)
                if download and not client_eof:
                    writes.append(client)
                readable, writable, _ = select.select(reads, writes, [], 0.1)
                pending = getattr(upstream, "pending", None)
                if not upstream_eof and callable(pending) and pending():
                    readable.append(upstream)
                if upstream in writable and upload:
                    try:
                        sent = self._send_upstream(upstream, upload)
                        del upload[:sent]
                        if sent:
                            last_activity = time.monotonic()
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                        pass
                if upstream in readable:
                    try:
                        data = self._receive_upstream(upstream)
                        if data:
                            last_activity = time.monotonic()
                            if not client_eof:
                                download.extend(data)
                        else:
                            upstream_eof = True
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                        pass
                if client in readable:
                    try:
                        data = client.recv(BUFFER_SIZE)
                        if data:
                            upload.extend(data)
                            last_activity = time.monotonic()
                        else:
                            client_eof = True
                            client_closed_at = time.monotonic()
                            download.clear()
                    except BlockingIOError:
                        pass
                    except (ConnectionResetError, ConnectionAbortedError):
                        client_eof = True
                        client_closed_at = time.monotonic()
                        download.clear()
                if client in writable and download:
                    try:
                        sent = client.send(download)
                        del download[:sent]
                        if sent:
                            last_activity = time.monotonic()
                    except BlockingIOError:
                        pass
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        client_eof = True
                        client_closed_at = time.monotonic()
                        download.clear()
        except (ssl.SSLEOFError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # Browser cancellation and normal TLS/connection EOF are expected.
            if not established and not self._stopping.is_set():
                with self._lock:
                    self._failures += 1
        except Exception:
            if not self._stopping.is_set():
                with self._lock:
                    self._failures += 1
        finally:
            self._disconnect(client)
            self._disconnect(upstream)
            with self._lock:
                self._workers.discard(current_thread())


class MeteredProxy:
    def __init__(self, base, relay):
        self._base = base
        self._relay = relay

    def summary(self):
        return self._base.summary()

    def transport_options(self):
        options = dict(self._base.transport_options())
        options["proxy"] = self._relay.proxy_url
        return options

    def browser_options(self):
        options = dict(self._base.browser_options())
        options["server"] = self._relay.proxy_url
        return options

    def verify_country(self):
        from anghami_session.proxy import PacketStreamProxy
        return PacketStreamProxy.verify_country(self)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Authorize the one-account live preparation.")
    parser.add_argument("--start-row", type=int, default=1)
    parser.add_argument("--browser", choices=("chrome", "cloakbrowser"), default="chrome")
    parser.add_argument("--reduce-browser-data", action="store_true")
    args = parser.parse_args(argv)
    if args.start_row < 1:
        parser.error("Choose a positive starting source row.")
    from anghami_session.errors import SessionError
    from anghami_session.preparation import prepare_test_accounts
    from anghami_session.proxy import load_packetstream_proxy
    from anghami_session.vault import AccountVault

    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_row": None, "was_new_login": False, "prepared": False,
        "passed": False, "error_code": None, "live_run": bool(args.run),
        "browser_backend": args.browser, "headless": True,
        "reduce_browser_data": args.reduce_browser_data,
        "proxy_country": "EG", "origin_upstream": f"https://{UPSTREAM_HOST}:{UPSTREAM_PORT}",
        "price_usd_per_gb": 1.0, "gb_bytes": 1_000_000_000,
        "byte_limit": BYTE_LIMIT, "phase_snapshots": [],
        "limitations": [
            "Counts upload and download inside the outer PacketStream TLS connection, including CONNECT and destination TLS traffic.",
            "Excludes outer TLS handshake/record overhead and TCP/IP framing; this is a cost estimate, not a provider billing measurement.",
            "One fresh browser profile sample; assets, login time and failed attempts can change usage.",
        ],
    }
    relay = None
    try:
        with AccountVault() as vault:
            rows = vault.select_test_candidates(1, start_row=args.start_row)
            if len(rows) != 1:
                report["error_code"] = "no_candidate"
                raise SessionError("No candidate.")
            report["source_row"] = rows[0]
            try:
                saved = vault.session(rows[0])
                del saved
            except SessionError:
                report["was_new_login"] = True
            if not args.run:
                print(json.dumps({
                    "live_run": False, "source_row": rows[0],
                    "was_new_login": report["was_new_login"], "network_requests": 0,
                    "browser_backend": args.browser,
                    "reduce_browser_data": args.reduce_browser_data,
                    "message": "Preview only. Use --run to measure one preparation.",
                }, indent=2))
                return 0
            base = load_packetstream_proxy()
            relay = MeteredRelay().start()
            proxy = MeteredProxy(base, relay)

            def progress(current):
                phase = current.get("phase")
                if phase not in {"preparing", "session_lookup", "login", "validation", "complete", "stopped"}:
                    phase = "preparing"
                report["phase_snapshots"].append({"phase": phase, **relay.snapshot()})

            preparation_options = {"reduce_browser_data": True} if args.reduce_browser_data else {}
            prepared = prepare_test_accounts(
                vault, count=1, start_row=args.start_row, proxy=proxy,
                browser_backend=args.browser, headless=True, progress=progress,
                **preparation_options,
            )
            report["prepared"] = rows[0] in prepared.get("prepared_rows", [])
            report["passed"] = prepared.get("passed") is True and report["prepared"]
    except (KeyboardInterrupt, EOFError):
        report["error_code"] = "cancelled"
    except Exception as exc:
        if report["error_code"] is None:
            report["error_code"] = "preparation_failed"
        known_failures = {
            "The login proxy country check failed. No browser was opened.": "http_proxy_preflight",
            "The browser proxy country check did not verify Egypt. No Anghami sign-in was attempted.": "browser_proxy_preflight",
            "Sign-in did not finish. Check the account credentials or any verification required by Anghami, then retry.": "login_timeout_or_verification",
            "The browser could not complete Anghami sign-in. Check that the site is accessible with the selected browser, then retry. The previous session was kept.": "browser_signin",
            "No authenticated session request was observed. The previous session was kept.": "session_capture",
        }
        if isinstance(exc, SessionError) and str(exc) in known_failures:
            report["failure_stage"] = known_failures[str(exc)]
    finally:
        if relay is not None:
            relay.close()
            report.update(relay.snapshot())
        else:
            report.update(upload_bytes=0, download_bytes=0, total_bytes=0,
                          connections=0, failures=0, duration_seconds=0.0, limited=False)
    if not args.run:
        print(json.dumps({"live_run": False, "error_code": report["error_code"], "network_requests": 0}, indent=2))
        return 1
    if report["limited"]:
        report.update(passed=False, error_code="measurement_limit")
    elif report["failures"]:
        report.update(passed=False, error_code="relay_failed")
    report["upload_mb"] = round(report["upload_bytes"] / 1_000_000, 6)
    report["download_mb"] = round(report["download_bytes"] / 1_000_000, 6)
    report["total_mb"] = round(report["total_bytes"] / 1_000_000, 6)
    report["estimated_usd"] = round(report["total_bytes"] / 1_000_000_000, 8)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
