"""Run play tests over a list of source rows using the job manager.

Same code path as the Test Console: sequential per account, N workers,
Direct connection by default. Reads rows from .anghami/lb-rows.txt.
"""

import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from anghami_session import ui_jobs
from anghami_session.vault import DEFAULT_VAULT_PATH

WORKERS = 8
ROWS_FILE = ROOT / ".anghami" / "lb-rows.txt"
REPORT = ROOT / ".anghami" / "lb-direct-play-run.json"


def main() -> int:
    rows = [int(line) for line in ROWS_FILE.read_text(encoding="utf-8").split() if line.strip()]
    print(f"running play over {len(rows)} LB accounts, workers={WORKERS}, direct", flush=True)
    manager = ui_jobs.JobManager(DEFAULT_VAULT_PATH)
    started = time.monotonic()
    manager.submit({"action": "play", "rows": rows, "count": 1, "workers": WORKERS,
                    "max_consecutive_failures": 1000, "proxy_egypt": False})
    while True:
        snap = manager.snapshot()
        if snap["status"] not in ("queued", "running"):
            break
        done = snap["progress"]["completed"]
        total = snap["progress"]["total"]
        rate = done / ((time.monotonic() - started) / 60)
        print(f"{done}/{total} done · {rate:.0f}/min", flush=True)
        time.sleep(30)
    minutes = (time.monotonic() - started) / 60
    outcomes = {}
    for result in snap.get("results", []):
        outcomes[result.get("outcome", "?")] = outcomes.get(result.get("outcome", "?"), 0) + 1
    summary = {"status": snap["status"], "tests": snap["progress"]["completed"],
               "minutes": round(minutes, 1), "tests_per_min": round(snap["progress"]["completed"] / minutes, 1),
               "outcomes": outcomes}
    REPORT.write_text(json.dumps({"summary": summary, "job": snap}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary), flush=True)
    return 0 if snap["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
