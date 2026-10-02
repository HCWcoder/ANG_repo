r"""Serve the real console against in-memory fixtures for browser verification.

Run from the repository with:
    .\.venv\Scripts\python.exe tests\ui_browser_fixture.py --port 8766

All accounts, credentials, outcomes and reports are synthetic. This fixture
does not open a vault, save a credential, launch a browser, or contact a service.
Play on synthetic row 2 deliberately fails and stops the remaining batch.
"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anghami_session.play_record import TEST_SONG_ID
from anghami_session.proxy import PacketStreamProxy
from anghami_session.test_settings import validate_test_song_id
from anghami_session.ui_jobs import JobBusyError, JobValidationError, _validate
from anghami_session.ui_server import create_server, safe_report


def now():
    return datetime.now(timezone.utc).isoformat()


class FixtureManager:
    def __init__(self, service, *, delay):
        self.service = service
        self.delay = delay
        self.lock = threading.RLock()
        self.latest = None

    def snapshot(self):
        with self.lock:
            return deepcopy(self.latest)

    def submit(self, payload):
        options = _validate(payload)
        rows = options["rows"]
        action = options["action"]
        if any(row not in range(1, 13) for row in rows):
            raise JobValidationError("Choose an imported fixture row from 1 to 12.")
        if action in {"play", "like", "check", "song"} and not set(rows).issubset(self.service.ready):
            raise JobValidationError("Choose a ready fixture account.")
        if action != "preview" and (options["proxy_egypt"] or action == "proxy-check") and not self.service.proxy_configured:
            raise JobValidationError("Save synthetic proxy credentials first.")
        total = (
            len(rows) * options["count"] if action in {"play", "like"}
            else options["count"] if action in {"prepare", "preview"}
            else len(rows) if rows else 1
        )
        with self.lock:
            if self.latest and self.latest["status"] in {"queued", "running"}:
                raise JobBusyError("An offline fixture job is already running.")
            if "song_id" in options and options["song_id"] != self.service.test_song_id:
                raise JobValidationError("The configured fixture song changed. Refresh before testing.")
            options["song_id"] = self.service.test_song_id
            self.latest = {
                "id": uuid4().hex, "action": action, "status": "queued",
                "phase": "queued", "message": "Offline browser fixture queued.",
                "progress": {"completed": 0, "total": total}, "results": [],
                "rows": rows, "count": options["count"], "error": None,
                "proxy_egypt": options["proxy_egypt"] or action == "proxy-check",
                "song_id": options["song_id"], "reduce_browser_data": options["reduce_browser_data"],
                "started_at": None, "finished_at": None,
            }
            queued = deepcopy(self.latest)
            threading.Thread(target=self._run, args=(options,), daemon=True).start()
            return queued

    def _update(self, **fields):
        with self.lock:
            self.latest.update(deepcopy(fields))

    def _append(self, result, *, completed):
        with self.lock:
            self.latest["results"].append(deepcopy(result))
            self.latest["progress"]["completed"] = completed

    def _finish(self, *, error=None):
        self._update(
            status="failed" if error else "succeeded", phase="failed" if error else "complete",
            finished_at=now(), error=error,
            message="Offline fixture stopped on its first failure. No live event was sent." if error
            else "Offline fixture completed. No live event was sent.",
        )
        self.service.record_report("ui-last-job.json", self.snapshot())

    def _run(self, options):
        action = options["action"]
        self._update(status="running", phase="executing", started_at=now(), message="Offline fixture is running; controls should be disabled.")
        time.sleep(self.delay)
        proxy = {"provider": "PacketStream", "country": "EG", "country_verified": True, "proxy_used": True, "sticky": True}
        if action in {"prepare", "preview"}:
            with self.service.lock:
                selected = [row for row in self.service.candidates if row >= options["start_row"] and row not in self.service.ready][:options["count"]]
                if action == "prepare":
                    self.service.ready.update(selected)
            result = {
                "passed": True, "dry_run": action == "preview", "selected_rows": selected,
                "prepared_rows": selected if action == "prepare" else [],
                "requested_accounts": options["count"], "prepared_account_count": len(selected) if action == "prepare" else 0,
                "browser": options["browser"], "headless": options["headless"],
                "reduce_browser_data": options["reduce_browser_data"],
            }
            self._append(result, completed=len(selected))
            self.service.record_report("accounts-prepare-tests-report.json", result)
            self._finish()
            return
        if action == "proxy-check":
            self._append({"passed": True, **proxy, "http_status": 200, "proxy_connect_http_status": 200}, completed=1)
            self.service.record_report("packetstream-egypt-report.json", {"passed": True, **proxy})
            self._finish()
            return
        completed = 0
        for row in options["rows"]:
            repetitions = options["count"] if action in {"play", "like"} else 1
            for number in range(1, repetitions + 1):
                self._update(phase="executing", message=f"Offline row {row}: {action} run {number} of {repetitions}.")
                time.sleep(0.35)
                result = {"source_row": row, "test_number": number, "song_id": options["song_id"], "passed": True, "automatic_retry": False}
                if options["proxy_egypt"]:
                    result["proxy"] = proxy
                if action == "play":
                    if row == 2:
                        result.update(passed=False, failed_phase="metadata", event_attempted=False, event_result="not_attempted")
                        self._append(result, completed=completed)
                        self.service.record_report(f"account-{row}.test-play-record-report.json", result)
                        self._finish(error={
                            "code": "test_failed", "message": "Synthetic row 2 failed its metadata check. Remaining runs stopped; no live request was sent.",
                            "source_row": row, "test_number": number, "failed_phase": "metadata", "result_unknown": False,
                        })
                        return
                    result.update(event_accepted=True, event_attempted=True, event_attempts=1, event_result="accepted", event_http_status=200)
                    self.service.record_report(f"account-{row}.test-play-record-report.json", result)
                elif action == "like":
                    with self.service.lock:
                        already_liked = (row, options["song_id"]) in self.service.liked
                        self.service.liked.add((row, options["song_id"]))
                    result.update(
                        liked_before=already_liked, liked_after=True, persisted_state_verified=True,
                        mutation_attempted=not already_liked, mutation_accepted=not already_liked,
                        mutation_attempts=0 if already_liked else 1,
                        mutation_result="skipped_already_liked" if already_liked else "accepted",
                        skipped_already_liked=already_liked,
                    )
                    self.service.record_report(f"account-{row}.test-like-report.json", result)
                elif action == "login":
                    with self.service.lock:
                        self.service.ready.add(row)
                    result["session_saved"] = True
                    result["reduce_browser_data"] = options["reduce_browser_data"]
                elif action == "check":
                    result.update(authenticated=True, negative_control_passed=True, operations={"relations": "ok", "playlists": "ok"})
                elif action == "song":
                    result.update(metadata_verified=True, metadata_duration_seconds=167)
                completed += 1
                self._append(result, completed=completed)
        self._finish()


class FixtureService:
    def __init__(self, *, delay=2.5):
        self.lock = threading.RLock()
        self.ready = {1, 2, 7}
        self.liked = {(7, TEST_SONG_ID)}
        self.test_song_id = TEST_SONG_ID
        self.candidates = [6, 8, 9, 10, 11, 12]
        self.proxy_configured = True
        self.saved_reports = {}
        self.manager = FixtureManager(self, delay=delay)
        self.record_report("account-7.test-like-report.json", {
            "source_row": 7, "song_id": TEST_SONG_ID, "passed": True,
            "mutation_result": "skipped_already_liked", "liked_before": True, "liked_after": True,
            "mutation_attempted": False, "persisted_state_verified": True,
        })

    def record_report(self, name, report):
        with self.lock:
            self.saved_reports[name] = {"modified_at": now(), "report": deepcopy(report)}

    def reports(self):
        with self.lock:
            return [{"name": name, "size": len(json.dumps(value["report"])), "modified_at": value["modified_at"]}
                    for name, value in sorted(self.saved_reports.items(), key=lambda item: item[1]["modified_at"], reverse=True)]

    def read_report(self, name):
        with self.lock:
            if name not in self.saved_reports:
                raise FileNotFoundError
            return {"name": name, "report": safe_report(deepcopy(self.saved_reports[name]["report"]))}

    def state(self):
        with self.lock:
            ready = sorted(self.ready)
            return {
                "vault": {"records": 12, "unique_accounts": 11, "sessions_saved": len(ready), "duplicate_rows": 1, "states": {"ready": len(ready), "login_required": 12 - len(ready)}},
                "vault_available": True,
                "cohort": {"test_rows": ready, "ready_rows": ready, "accounts": [{"source_row": row, "state": "ready", "session_saved": True} for row in ready]},
                "proxy": {"configured": self.proxy_configured, "provider": "PacketStream", "country": "EG", "sticky": True},
                "test_song_id": self.test_song_id, "limits": {"accounts": 5, "test_accounts": len(ready), "tests_per_account": 5},
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
                raise JobBusyError("An offline fixture job is already running.")
            self.test_song_id = validate_test_song_id(payload["song_id"])
            return {"test_song_id": self.test_song_id}

    def find(self, payload):
        if set(payload) != {"email"} or not isinstance(payload["email"], str) or len(payload["email"]) > 254:
            raise ValueError
        return {"source_rows": [7] if payload["email"].casefold() == "fixture7@example.invalid" else []}

    def save_proxy(self, payload):
        if set(payload) != {"username", "auth_key"} or any(not isinstance(value, str) or len(value) > 256 for value in payload.values()):
            raise ValueError
        PacketStreamProxy(payload["username"], payload["auth_key"])
        with self.lock:
            job = self.manager.snapshot()
            if job and job["status"] in {"queued", "running"}:
                raise JobBusyError("A fixture job is running.")
            self.proxy_configured = True
        # Deliberately retain neither input value, even in fixture memory.
        return {"configured": True, "provider": "PacketStream", "country": "EG"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--delay", type=float, default=2.5)
    parser.add_argument("--no-proxy", action="store_true", help="Start with no synthetic proxy configured.")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535 or not 0 <= args.delay <= 30:
        parser.error("Use a valid local port and delay from 0 to 30 seconds.")
    service = FixtureService(delay=args.delay)
    service.proxy_configured = not args.no_proxy
    server = create_server(port=args.port, service=service)
    print(f"OFFLINE browser fixture: {server.base_url}", flush=True)
    print("Synthetic data only. Row 2 play fails; row 7 starts liked. Ctrl+C stops.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
