"""Read-only transfer summaries from a job's frozen, redacted test reports.

Older workers only recorded metadata and play transfers. These summaries are
therefore partial lower bounds, even if each legacy transfer was measured.
No sessions are opened and no requests or account actions are issued here.
"""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import threading

from .ui_jobs import legacy_network_usage, safe_network_usage, summarize_network_usage
from .errors import SessionError
from .test_settings import validate_test_song_id


_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
_REPORT_NAME = re.compile(r"account-([1-9][0-9]*)\.(play|like|check|song)-([1-5])\.redacted\.json\Z")
_LOCK = threading.RLock()
_CACHE = {}
_MAX_REPORT_BYTES = 2_000_000


def _identity(job):
    if type(job) is not dict or not isinstance(job.get("id"), str) or not _JOB_ID.fullmatch(job["id"]):
        return None
    action, rows, count = job.get("action"), job.get("rows"), job.get("count")
    if type(action) is not str or action not in {"play", "like", "check", "song"} or type(rows) is not list or type(count) is not int or not 1 <= count <= 5:
        return None
    if not rows or any(type(row) is not int or not 1 <= row <= 2**31 - 1 for row in rows) or len(set(rows)) != len(rows):
        return None
    song = job.get("song_id")
    if action in {"play", "like", "song"}:
        try:
            if type(song) is not str or validate_test_song_id(song) != song:
                return None
        except SessionError:
            return None
    completed = job.get("completed_tests")
    total = len(rows) * count
    if type(completed) is not int or not 0 <= completed <= total:
        return None
    return action, frozenset(rows), count, song, completed, total


def _report_matches(report, row, number, song):
    if type(report) is not dict or type(report.get("source_row")) is not int or report["source_row"] != row:
        return False
    if type(report.get("test_number")) is not int or report["test_number"] != number:
        return False
    if song is not None and report.get("song_id") != song:
        return False
    return True


def _report_usage(report, row, number, song, proxy_used):
    if not _report_matches(report, row, number, song):
        return {}
    return safe_network_usage(report.get("network_usage")) or legacy_network_usage(report, proxy_used=proxy_used)


def _scan(job, base, identity):
    action, rows, count, song, _, _ = identity
    directory = base / "ui-test-reports" / job["id"]
    cache_key = (str(directory), action, rows, count, song, job.get("proxy_egypt") is True)
    entry = _CACHE.get(cache_key)
    if entry is None:
        if len(_CACHE) >= 8:
            _CACHE.pop(next(iter(_CACHE)))
        entry = _CACHE[cache_key] = {"files": {}}
    files, seen, pending = entry["files"], set(), []
    def read_report(item):
        key, path, signature = item
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            usage = _report_usage(report, *key, song, job.get("proxy_egypt") is True)
            return key, signature, usage, _report_matches(report, *key, song)
        except (OSError, UnicodeError, ValueError, RecursionError):
            return key, signature, {}, False
    try:
        candidates = directory.iterdir()
        for path in candidates:
            match = _REPORT_NAME.fullmatch(path.name)
            if not match or match[2] != action:
                continue
            row, number = int(match[1]), int(match[3])
            if row not in rows or number > count:
                continue
            key = (row, number)
            seen.add(key)
            try:
                if path.is_symlink():
                    files.pop(key, None)
                    continue
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                if key in files and files[key][0] == signature:
                    continue
                if stat.st_size <= _MAX_REPORT_BYTES:
                    pending.append((key, path, signature))
                else:
                    files[key] = (signature, {}, False)
            except (OSError, UnicodeError, ValueError, RecursionError):
                # An atomic report replacement will have a new signature on
                # the next poll. Incomplete files never become measured zero.
                files.pop(key, None)
        for key in set(files).difference(seen):
            del files[key]
    except OSError:
        return {}, set()
    # Historical files are independent, small local reads. Bounded I/O avoids
    # serial disk latency on the first open of a large completed run.
    if pending:
        with ThreadPoolExecutor(max_workers=min(8, len(pending)), thread_name_prefix="usage-report-read") as executor:
            for key, signature, usage, matched in executor.map(read_report, pending):
                # Cache only safe counters, never the raw report object.
                files[key] = (signature, usage, matched)
    return ({key: value[1] for key, value in files.items() if value[1]},
            {key for key, value in files.items() if value[2]})


def enrich_job_usage(job, state_directory):
    """Copy a snapshot and attach safe totals across all matching test files.

    A new worker's cumulative measurement is authoritative, including setup
    traffic. Legacy files supply only missing usage and per-account details.
    The cache avoids reparsing thousands of finished reports on every poll.
    """
    identity = _identity(job)
    if identity is None:
        return deepcopy(job)
    result = deepcopy(job)
    action, rows, count, song, completed, total = identity
    authoritative = safe_network_usage(result.get("network_usage"))
    if authoritative and count == 1:
        result["network_usage"] = authoritative
        return result
    with _LOCK:
        samples, finished_keys = _scan(job, Path(state_directory).resolve(), identity)
        retained = result.get("results") if type(result.get("results")) is list else []
        for report in retained:
            if type(report) is not dict:
                continue
            row, number = report.get("source_row"), report.get("test_number")
            if type(row) is not int or row not in rows or type(number) is not int or not 1 <= number <= count:
                continue
            if not _report_matches(report, row, number, song):
                continue
            key = (row, number)
            finished_keys.add(key)
            usage = _report_usage(report, row, number, song, job.get("proxy_egypt") is True)
            if usage:
                samples.setdefault(key, usage)
        if authoritative:
            result["network_usage"] = authoritative
        elif samples:
            result["network_usage"] = summarize_network_usage(
                ((row, usage) for (row, _), usage in samples.items()),
                total_tests=total, completed_tests=max(completed, len(finished_keys)),
            )
        visible_rows = {report.get("source_row") for report in retained
                        if type(report) is dict and type(report.get("source_row")) is int}
        per_account = {}
        account_counts = {}
        for row, number in finished_keys:
            account_counts[row] = account_counts.get(row, 0) + 1
        for (row, number), usage in samples.items():
            if row in visible_rows:
                per_account.setdefault(row, []).append(usage)
        account_totals = {
            row: summarize_network_usage(((row, usage) for usage in usages), total_tests=account_counts[row], completed_tests=account_counts[row])
            for row, usages in per_account.items()
        }
        for report in retained:
            if type(report) is not dict:
                continue
            row, number = report.get("source_row"), report.get("test_number")
            if (type(row) is not int or row not in rows or type(number) is not int or not 1 <= number <= count
                or not _report_matches(report, row, number, song)):
                continue
            usage = samples.get((row, number))
            if usage:
                report["network_usage"] = deepcopy(usage)
            if row in account_totals and account_counts[row] > 1:
                previous_count = report.get("account_tests_completed")
                previous_usage = safe_network_usage(report.get("account_network_usage"))
                if not (type(previous_count) is int and account_counts[row] < previous_count <= count and previous_usage):
                    report["account_network_usage"] = deepcopy(account_totals[row])
                    report["account_tests_completed"] = account_counts[row]
    return result
