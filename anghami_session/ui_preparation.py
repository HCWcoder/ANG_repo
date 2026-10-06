"""Bridge a country-selected UI batch to the existing HTTP worker coordinator."""

import json
from datetime import datetime, timezone
from pathlib import Path
import re
from time import sleep
from uuid import uuid4

from . import country_preparation as country
from .errors import SessionError, safe_session_storage_failure
from .proxy_pool import StickyProxyPool


def _read_resume(vault, options, job_id):
    path = vault.path.parent / "ui-preparation-resume.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (type(manifest) is not dict or type(manifest.get("format_version")) is not int or manifest["format_version"] != 1
                or type(manifest.get("source_job_id")) is not str
                or re.fullmatch(r"[0-9a-f]{32}", manifest["source_job_id"]) is None
                or manifest.get("country") != options["account_country"]
                or type(manifest.get("count")) is not int or manifest["count"] != options["count"]
                or manifest.get("resumed_by") not in {None, job_id}):
            raise ValueError
        rows, held = manifest.get("selected_rows"), manifest.get("held_rows")
        if (type(rows) is not list or len(rows) != options["count"]
                or any(type(row) is not int or row < 1 for row in rows) or len(set(rows)) != len(rows)
                or type(held) is not list or any(type(row) is not int or row < 1 for row in held)
                or len(set(held)) != len(held) or not set(held).issubset(rows)
                or type(manifest.get("source_sha256")) is not str
                or re.fullmatch(r"[0-9a-f]{64}", manifest["source_sha256"]) is None):
            raise ValueError
        if vault.metadata("source_sha256") != manifest["source_sha256"]:
            raise ValueError
        previous = manifest.get("checkpoint_job_id")
        if previous is not None and (
            type(previous) is not str or re.fullmatch(r"[0-9a-f]{32}", previous) is None
            or previous != manifest["source_job_id"] or previous == job_id
        ):
            raise ValueError
        if "resume_not_before_utc" in manifest:
            _cooldown_deadline(manifest["resume_not_before_utc"])
        return rows, held, manifest
    except Exception:
        raise SessionError("The saved preparation continuation does not match this job or source import.") from None


def _cooldown_deadline(value):
    """Validate one persisted UTC deadline without extending it on restart."""
    if type(value) is not str or len(value) > 64:
        raise ValueError
    deadline = datetime.fromisoformat(value)
    if (deadline.tzinfo is None or deadline.utcoffset().total_seconds() != 0
            or (deadline - datetime.now(timezone.utc)).total_seconds() > 86405):
        raise ValueError
    return deadline


def _wait_resume_cooldown(base, job_id, deadline, on_wait):
    """Wait before all account/proxy requests; retain stop and ownership gates."""
    stop_path = base / f"ui-preparation-progress-{job_id}.stop"
    while (deadline - datetime.now(timezone.utc)).total_seconds() > 0:
        owner, reason = country._owned_ui_state(base / "ui-last-job.json", job_id)
        if stop_path.exists() or reason is not None or owner != job_id:
            return False
        on_wait()
        sleep(min(1.0, max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())))
    owner, reason = country._owned_ui_state(base / "ui-last-job.json", job_id)
    return not stop_path.exists() and reason is None and owner == job_id


def _pool_position(base, pool):
    path = base / "ui-sticky-pool-position.json"
    if not path.exists():
        return 0
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if (type(saved) is not dict or set(saved) != {"fingerprint", "cursor"}
                or type(saved["fingerprint"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", saved["fingerprint"]) is None
                or type(saved["cursor"]) is not int or not 0 <= saved["cursor"] < 2**63):
            raise ValueError
        return saved["cursor"] if saved["fingerprint"] == pool.fingerprint() else 0
    except Exception:
        raise SessionError("The saved sticky pool position is invalid or belongs to another pool.") from None


def run_parallel_ui_preparation(vault, options, *, job_id, progress, pool_loader=StickyProxyPool.load):
    """Freeze one country cohort; preserve held rows and coordinator checkpoints."""
    if (type(job_id) is not str or re.fullmatch(r"[0-9a-f]{32}", job_id) is None
            or options.get("action") != "prepare" or options.get("no_browser") is not True
            or options.get("account_country") not in {"EG", "LB"}
            or type(options.get("workers")) is not int or not 2 <= options["workers"] <= country.MAX_WORKER_REQUEST
            or options.get("review_rows")):
        raise SessionError("Parallel preparation requires browser-free country-selected accounts.")
    base = vault.path.parent
    held = []
    continuation = None
    if options.get("resume_preparation"):
        rows, held, continuation = _read_resume(vault, options, job_id)
    else:
        rows = vault.select_test_candidates(options["count"], country=options["account_country"], randomize=True)
        if len(rows) != options["count"]:
            raise SessionError("The requested number of additional country accounts is not available.")
    source_path = Path(__file__).resolve().parents[1] / "registered.txt"
    plan = country.build_selected_plan(vault, source_path, rows, country=options["account_country"])
    enrolled = vault.enrolled_test_rows()
    for item in plan["rows"]:
        if item["state"] == "already_enrolled" or (item["state"] == "already_ready" and item["source_row"] not in enrolled):
            # A saved session without ready test enrollment still needs the
            # ordinary account validation and enrollment path.
            item["state"] = "pending"
    checkpoint_path = base / f"ui-preparation-progress-{job_id}.json"
    if checkpoint_path.exists():
        raise SessionError("This preparation job already has a checkpoint. Review it before restarting.")
    if continuation is not None and continuation.get("checkpoint_job_id"):
        previous_path = base / f"ui-preparation-progress-{continuation['checkpoint_job_id']}.json"
        initial = country.load_progress(previous_path, plan)
        if (initial["status"] != "paused" or initial["pause_reason"] != "stop_requested"
                or initial["active_rows"] or any(row["state"] == "in_progress" for row in initial["rows"])
                or set(held) != {row["source_row"] for row in initial["rows"] if row["state"] == "unknown"}
                or (held and initial["unknown_acknowledged"] is not True)
                or initial.get("failure_hold") is not None):
            raise SessionError("The previous preparation checkpoint has not drained or requires review.")
        if (initial["proxy_pool_active"] != bool(options.get("proxy_sticky_pool"))
                or initial["connection"] != ("proxy_egypt" if options.get("proxy_egypt") else "direct")):
            raise SessionError("Keep the drained preparation connection when changing workers.")
        initial.update(run_id=uuid4().hex, status="not_started", pause_reason=None,
                       created_at_utc=country._now(), updated_at_utc=country._now())
    else:
        initial = country._new_progress(plan)
        for row in initial["rows"]:
            if row["source_row"] in held:
                row.update(state="unknown", attempts=1, phase="stopped", error_code="interrupted_unknown")
            elif options.get("resume_preparation") and row["state"] in {"already_ready", "already_enrolled"}:
                # These were completed in the paused UI batch. Keep its cumulative
                # attempt count while skipping every saved account request.
                row.update(state="ready", attempts=1, phase="complete",
                           connection="proxy_egypt" if options.get("proxy_egypt") or options.get("proxy_sticky_pool") else "direct")
        initial["unknown_acknowledged"] = bool(held)
    initial.update(no_browser=True,
                   reduce_browser_data=False, workers=options["workers"], requested_workers=options["workers"],
                   effective_workers=country._effective_workers(initial, options["workers"]), max_consecutive_failures=20)
    pool_path = base / "packetstream-sticky-pool.dpapi" if options.get("proxy_sticky_pool") else None
    if pool_path is not None:
        pool = pool_loader(pool_path)
        cursor = _pool_position(base, pool)
        if continuation is not None and continuation.get("checkpoint_job_id") and (
            initial["proxy_pool_active"] is not True or initial["proxy_pool_fingerprint"] != pool.fingerprint()
            or initial["proxy_pool_count"] != len(pool) or initial["proxy_pool_cursor"] != cursor
        ):
            raise SessionError("The saved sticky pool position does not match the drained preparation checkpoint.")
        initial.update(proxy_pool_active=True, proxy_pool_count=len(pool),
                       proxy_pool_fingerprint=pool.fingerprint(),
                       proxy_pool_cursor=cursor,
                       proxy_pool_endpoint=pool.summary()["endpoint"], connection="proxy_egypt")
    elif options.get("proxy_egypt"):
        initial["connection"] = "proxy_egypt"
    if continuation is not None:
        continuation["resumed_by"] = job_id
        country._atomic_json(base / "ui-preparation-resume.json", continuation)
    country._atomic_json(checkpoint_path, initial)
    report_path = base / "accounts-prepare-tests-report.json"

    def report_for(summary, *, final_rows=None):
        counts = summary["counts"]
        ready = sum(counts.get(state, 0) for state in ("ready", "already_ready", "already_enrolled"))
        active = list(summary.get("active_rows", []))
        pending = list(summary.get("connection_pending_rows", []))
        terminal = summary["status"] in {"completed", "completed_with_pending"}
        report = {
            "requested_accounts": len(rows), "selected_rows": list(rows),
            "prepared_account_count": ready, "attempted_accounts": summary.get("attempted_accounts", 0),
            "workers": summary["workers"], "active_workers": len(active), "active_rows": active,
            "requested_workers": summary.get("requested_workers", summary["workers"]),
            "effective_workers": summary.get("effective_workers", initial["effective_workers"]),
            "connection_pending_count": counts.get("connection_pending", 0), "connection_pending_rows": pending,
            "account_failed_count": summary.get("account_failed_count", counts.get("failed", 0)),
            "infrastructure_failed_count": summary.get("infrastructure_failed_count", 0),
            "preparation_held": counts.get("unknown", 0) + counts.get("session_review_pending", 0),
            "session_review_pending_count": counts.get("session_review_pending", 0),
            "session_review_pending_rows": list(summary.get("session_review_pending_rows", [])),
            "attention_required_rows": sorted(set(held) | set(summary.get("session_review_pending_rows", []))),
            "phase": "complete" if terminal else "preparing" if summary["status"] == "running" else "stopped",
            "status": summary["status"], "pause_reason": summary.get("pause_reason"),
            "passed": ready == len(rows), "no_browser": True, "browser_required": False,
            "preparation_method": "http", "reduce_browser_data": False,
            "connection": summary["connection"], "account_country": plan["country"],
            "selection_mode": "random_country", "proxy_sticky_pool": pool_path is not None,
            "play_events_sent": 0, "like_events_sent": 0,
            "max_consecutive_failures": 20, "consecutive_failures": summary.get("consecutive_failures", 0),
            "failure_limit_scope": summary.get("failure_limit_scope", "active_budget"),
        }
        if final_rows is not None:
            report["prepared_rows"] = [row["source_row"] for row in final_rows
                                       if row["state"] in {"ready", "already_ready", "already_enrolled"}]
            report["account_failed_rows"] = [row["source_row"] for row in final_rows
                                             if row["state"] == "failed" and row.get("error_code", "account_failed") == "account_failed"]
            report["infrastructure_failed_rows"] = [row["source_row"] for row in final_rows
                                                    if row["state"] == "failed" and row.get("error_code", "account_failed") != "account_failed"]
            report["session_review_pending_rows"] = [row["source_row"] for row in final_rows
                                                     if row["state"] == "session_review_pending"]
            report["attention_required_rows"] = [row["source_row"] for row in final_rows
                                                 if row["state"] in {"unknown", "session_review_pending"}]
        storage_failures = []
        for failure in summary.get("recent_session_storage_failures", []):
            if not isinstance(failure, dict) or type(failure.get("source_row")) is not int or failure["source_row"] < 1:
                continue
            diagnostic = safe_session_storage_failure({key: value for key, value in failure.items() if key != "source_row"})
            if diagnostic:
                storage_failures.append({"source_row": failure["source_row"], **diagnostic})
        if storage_failures:
            report["recent_session_storage_failures"] = storage_failures[-5:]
        ui_read_failure = country.safe_ui_read_failure(summary.get("ui_read_failure"))
        if ui_read_failure:
            report["ui_read_failure"] = ui_read_failure
        return report

    def publish(summary):
        report = report_for(summary)
        country._atomic_json(report_path, report)
        saved_pool = summary.get("proxy_pool")
        if saved_pool is not None:
            country._atomic_json(base / "ui-sticky-pool-position.json", {
                "fingerprint": saved_pool["fingerprint"], "cursor": saved_pool["next_ordinal"],
            })
        progress(report)

    if continuation is not None and continuation.get("resume_not_before_utc"):
        def waiting():
            safe = report_for(country.summarize(initial))
            safe["phase"] = "provider_wait"
            country._atomic_json(report_path, safe)
            progress(safe)
        if not _wait_resume_cooldown(base, job_id, _cooldown_deadline(continuation["resume_not_before_utc"]), waiting):
            initial.update(status="paused", pause_reason="stop_requested", active_row=None, active_rows=[])
            country._atomic_json(checkpoint_path, initial)
            stopped = report_for(country.summarize(initial), final_rows=initial["rows"])
            country._atomic_json(report_path, stopped)
            progress(stopped)
            return stopped

    result = country.run_plan(
        vault, plan, checkpoint_path, no_browser=True, workers=options["workers"],
        proxy_egypt=options["proxy_egypt"], proxy_sticky_pool=pool_path,
        max_consecutive_failures=20, owned_ui_job_id=job_id,
        ui_path=base / "ui-last-job.json", progress_callback=publish,
    )
    durable = country.load_progress(checkpoint_path, plan)
    final = report_for(result, final_rows=durable["rows"])
    country._atomic_json(report_path, final)
    progress(final)
    return final
