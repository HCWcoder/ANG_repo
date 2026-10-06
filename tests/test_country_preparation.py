"""Country preparation is exercised offline with a synthetic import and vault."""

from copy import deepcopy
import builtins
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from anghami_session import country_preparation as country
from anghami_session.errors import LoginCaptureError, SessionError


PASSWORD = "synthetic-country-password-never-expose"
RAW_ERROR = "synthetic-country-private-error https://example.invalid/?sid=private"


class SyntheticVault:
    def __init__(self, path, records, source_hash, *, ready=(), enrolled=()):
        self.path = Path(path)
        self.records = deepcopy(records)
        self.source_hash = source_hash
        self.ready = set(ready)
        self.enrolled = set(enrolled)
        self.calls = []
        self._db = sqlite3.connect(":memory:")
        self._db.execute("CREATE TABLE accounts(source_row INTEGER PRIMARY KEY, email_key BLOB, record BLOB, state TEXT, session BLOB)")
        self._db.execute("CREATE TABLE test_accounts(source_row INTEGER PRIMARY KEY)")
        for row, record in records.items():
            identity = hashlib.sha256(record["email"].strip().casefold().encode()).digest()
            self._db.execute("INSERT INTO accounts VALUES(?,?,?,?,?)", (
                row, identity, b"synthetic-record", "ready" if row in self.ready else "login_required",
                b"synthetic-session" if row in self.ready else None,
            ))
        self._db.executemany("INSERT INTO test_accounts VALUES(?)", [(row,) for row in self.enrolled])
        self._db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def metadata(self, name):
        assert name == "source_sha256"
        return self.source_hash

    def enrolled_test_rows(self):
        return frozenset(self.enrolled)

    def test_accounts(self):
        return {"ready_rows": sorted(self.ready), "test_rows": sorted(self.enrolled), "accounts": []}

    def record(self, row):
        self.calls.append(("record", row))
        return deepcopy(self.records[row])

    def select_test_candidates(self, *_args, **_options):
        raise AssertionError("A fixed country row must never invoke the drifting general selector")

    def session(self, row):
        if row not in self.ready:
            raise SessionError("No synthetic session")
        return {"synthetic_saved_session": True}

    def enable_test_account(self, row):
        self.calls.append(("enable", row))
        self.enrolled.add(row)
        self.ready.add(row)
        self._db.execute("INSERT OR IGNORE INTO test_accounts VALUES(?)", (row,))
        self._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=?", (b"synthetic-session", row))
        self._db.commit()

    def test_play_record(self, *_args, **_options):
        raise AssertionError("Country preparation must never send a play")

    def test_like(self, *_args, **_options):
        raise AssertionError("Country preparation must never send a like")


@pytest.fixture
def imported(tmp_path):
    entries = [
        ("LB", "ready-country@example.invalid"),
        ("EG", "enrolled-country@example.invalid"),
        ("LB", "enrolled-country@example.invalid"),
        ("LB", "fresh-a-country@example.invalid"),
        ("LB", "FRESH-A-COUNTRY@example.invalid"),
        ("lb", "lowercase-country@example.invalid"),
        ("LB", "fresh-b-country@example.invalid"),
        ("EG", "other-country@example.invalid"),
        (" LB ", "fresh-c-country@example.invalid"),
        ("LBR", "long-code-country@example.invalid"),
    ]
    records = {row: {"country": code.strip(), "email": email, "password": PASSWORD}
               for row, (code, email) in enumerate(entries, 1)}
    raw = "\n".join(f"{code}~{email}~{PASSWORD}~k=synthetic~cookie=synthetic" for code, email in entries).encode()
    source = tmp_path / "registered.txt"
    source.write_bytes(raw)
    vault = SyntheticVault(tmp_path / "accounts.sqlite3", records, hashlib.sha256(raw).hexdigest(), ready=(1,), enrolled=(2,))
    yield vault, source
    vault._db.close()


def assert_private_safe(value, vault):
    encoded = json.dumps(value)
    assert PASSWORD not in encoded and RAW_ERROR not in encoded
    assert "sid=private" not in encoded
    for record in vault.records.values():
        assert record["email"] not in encoded


def successful_prepare(calls, *, reduce_browser_data=True):
    def prepare(vault, **options):
        row = options["start_row"]
        assert options["count"] == 1
        assert options["proxy"] is None
        assert options["browser_backend"] == "cloakbrowser"
        assert "no_browser" not in options
        assert options["headless"] is True
        assert options["reduce_browser_data"] is reduce_browser_data
        assert vault.select_test_candidates(1, start_row=row) == [row]
        assert vault.record(row)["country"] == "LB"
        calls.append(row)
        vault.enable_test_account(row)
        report = {"passed": True, "selected_rows": [row], "prepared_rows": [row], "prepared_account_count": 1, "phase": "complete"}
        if options.get("progress"):
            options["progress"](report)
        return report
    return prepare


def test_plan_is_exact_country_bound_to_source_and_deduplicated_without_mutation(imported):
    vault, source = imported
    before = list(vault._db.execute("SELECT source_row,state,session FROM accounts ORDER BY source_row"))
    plan = country.build_plan(vault, source, country="LB")
    assert plan["country"] == "LB" and plan["source_sha256"] == vault.source_hash
    assert plan["tagged_rows"] == 6 and plan["duplicate_rows"] == 1
    assert {item["source_row"]: item["state"] for item in plan["rows"]} == {
        1: "already_ready", 3: "already_enrolled", 4: "pending", 7: "pending", 9: "pending",
    }
    assert list(vault._db.execute("SELECT source_row,state,session FROM accounts ORDER BY source_row")) == before
    assert_private_safe(plan, vault)


def test_source_hash_mismatch_fails_before_creating_plan_or_account_work(imported):
    vault, source = imported
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(SessionError):
        country.build_plan(vault, source, country="LB")
    assert vault.calls == []


def test_fixed_row_selector_never_drifts_to_another_account(imported):
    vault, _source = imported
    fixed = country.SelectedRowVault(vault, 4, country="LB")
    assert fixed.select_test_candidates(1, start_row=4) == [4]
    for count, start in [(2, 4), (1, 5), (1, 1)]:
        with pytest.raises(SessionError):
            fixed.select_test_candidates(count, start_row=start)


def test_direct_runner_prepares_one_exact_row_at_a_time_and_resumes_remaining(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    progress_path = tmp_path / "country-progress.json"
    calls = []
    country.run_plan(vault, plan, progress_path, limit=1, prepare=successful_prepare(calls))
    assert calls == [4]
    progress = country.load_progress(progress_path, plan)
    assert {item["source_row"]: item["state"] for item in progress["rows"]}[4] == "ready"
    country.run_plan(vault, plan, progress_path, prepare=successful_prepare(calls))
    assert calls == [4, 7, 9]
    progress = country.load_progress(progress_path, plan)
    assert all(item["state"] != "pending" for item in progress["rows"])
    assert_private_safe(progress, vault)
    assert_private_safe(country.summarize(progress), vault)
    assert_private_safe(json.loads(progress_path.read_text(encoding="utf-8")), vault)


def test_failed_account_is_sanitized_and_not_automatically_replayed(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    progress_path = tmp_path / "country-progress.json"
    calls = []

    def fail(vault, **options):
        calls.append(options["start_row"])
        raise SessionError(RAW_ERROR + " " + PASSWORD)

    country.run_plan(vault, plan, progress_path, limit=1, prepare=fail)
    assert calls == [4]
    progress = country.load_progress(progress_path, plan)
    assert {item["source_row"]: item["state"] for item in progress["rows"]}[4] == "failed"
    country.run_plan(vault, plan, progress_path, prepare=successful_prepare(calls))
    assert calls == [4, 7, 9]
    progress = country.load_progress(progress_path, plan)
    assert_private_safe(progress, vault)
    assert_private_safe(country.summarize(progress), vault)


def test_duplicate_lock_is_rejected_and_releases_after_context(tmp_path):
    path = tmp_path / "country.lock"
    with country.CountryJobLock(path):
        with pytest.raises(SessionError):
            with country.CountryJobLock(path):
                raise AssertionError("A duplicate worker acquired the country lock")
    with country.CountryJobLock(path):
        pass


def test_interrupted_row_is_not_replayed_when_resuming(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    progress_path = tmp_path / "country-progress.json"
    calls = []
    country.run_plan(vault, plan, progress_path, limit=1, prepare=successful_prepare(calls))
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    interrupted = next(item for item in progress["rows"] if item["source_row"] == 7)
    interrupted.update(state="in_progress", attempts=1, phase="login")
    progress["status"] = "running"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    country.run_plan(vault, plan, progress_path, prepare=successful_prepare(calls))
    assert calls == [4]
    result = country.load_progress(progress_path, plan)
    state = next(item["state"] for item in result["rows"] if item["source_row"] == 7)
    assert state not in {"pending", "in_progress", "ready"}
    assert next(item["state"] for item in result["rows"] if item["source_row"] == 9) == "pending"
    assert_private_safe(result, vault)


def test_changed_vault_source_hash_stops_resume_without_touching_checkpoint(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    progress_path = tmp_path / "country-progress.json"
    calls = []
    country.run_plan(vault, plan, progress_path, limit=1, prepare=successful_prepare(calls))
    before = progress_path.read_bytes()
    vault.source_hash = "a" * 64
    with pytest.raises(SessionError):
        country.run_plan(vault, plan, progress_path, prepare=successful_prepare(calls))
    assert calls == [4]
    assert progress_path.read_bytes() == before


def test_stop_request_during_active_preparation_finishes_only_that_account(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    progress_path = tmp_path / "country-progress.json"
    stop_path = tmp_path / "country.stop"
    calls = []
    prepare = successful_prepare(calls)

    def stop_after_active(vault, **options):
        stop_path.write_text("stop", encoding="utf-8")
        return prepare(vault, **options)

    country.run_plan(vault, plan, progress_path, prepare=stop_after_active, stop_path=stop_path)
    assert calls == [4]
    result = country.load_progress(progress_path, plan)
    states = {item["source_row"]: item["state"] for item in result["rows"]}
    assert states[4] == "ready" and states[7] == states[9] == "pending"
    assert_private_safe(result, vault)


def test_checkpoint_from_different_plan_fails_closed(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    progress_path = tmp_path / "country-progress.json"
    country.run_plan(vault, plan, progress_path, limit=1, prepare=successful_prepare([]))
    other = deepcopy(plan)
    other["plan_id"] = "different-synthetic-plan"
    with pytest.raises(SessionError):
        country.load_progress(progress_path, other)


@pytest.mark.parametrize("status", ["queued", "running"])
def test_active_ui_job_pauses_before_any_account_preparation(imported, tmp_path, status):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    ui_path = tmp_path / "ui-last-job.json"
    ui_path.write_text(json.dumps({"id": "synthetic-ui-job", "status": status, "action": "prepare"}), encoding="utf-8")
    progress_path = tmp_path / "country-progress.json"
    calls = []
    summary = country.run_plan(vault, plan, progress_path, prepare=successful_prepare(calls), ui_path=ui_path)
    assert calls == []
    assert summary["pause_reason"] == "ui_busy"
    progress = country.load_progress(progress_path, plan)
    assert all(item["state"] == "pending" for item in progress["rows"] if item["source_row"] in {4, 7, 9})
    assert_private_safe(progress, vault)


@pytest.mark.parametrize("status", ["succeeded", "failed", "completed_with_failures"])
def test_finished_ui_test_job_does_not_block_country_preparation(imported, tmp_path, status):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    ui_path = tmp_path / "ui-last-job.json"
    ui_path.write_text(json.dumps({"id": "synthetic-finished-test", "status": status, "action": "play"}), encoding="utf-8")
    calls = []
    summary = country.run_plan(vault, plan, tmp_path / "country-progress.json", limit=1,
                               prepare=successful_prepare(calls), ui_path=ui_path)
    assert calls == [4]
    assert summary["pause_reason"] not in {"ui_busy", "ui_unavailable"}
    assert_private_safe(summary, vault)


@pytest.mark.parametrize("failed", [False, True])
def test_direct_runtime_ignores_then_restores_proxy_environment_and_launch_function(imported, tmp_path, monkeypatch, failed):
    import anghami_session

    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    for name in proxy_names:
        monkeypatch.setenv(name, f"http://synthetic-{name.lower()}.invalid")
    monkeypatch.setenv("NO_PROXY", "synthetic-exempt.invalid")
    original_environment = {name: os.environ.get(name) for name in (*proxy_names, "NO_PROXY")}
    launched = []

    def launch(*_args, **options):
        launched.append(options)

    capture = SimpleNamespace(launch_browser=launch)
    monkeypatch.setitem(sys.modules, "anghami_session.capture", capture)
    monkeypatch.setattr(anghami_session, "capture", capture, raising=False)
    calls = []
    success = successful_prepare(calls)

    def prepare(vault, **options):
        assert not any(name.casefold() in {"http_proxy", "https_proxy", "all_proxy"} for name in os.environ)
        assert os.environ.get("NO_PROXY") == "*"
        capture.launch_browser("cloakbrowser", headless=True)
        if failed:
            raise SessionError(RAW_ERROR)
        return success(vault, **options)

    country.run_plan(vault, plan, tmp_path / "country-progress.json", limit=1, prepare=prepare)
    assert launched == [{"headless": True, "direct": True}]
    assert capture.launch_browser is launch
    assert {name: os.environ.get(name) for name in original_environment} == original_environment


def test_country_plans_with_different_progress_paths_share_one_worker_lock(imported, tmp_path):
    vault, source = imported
    lb = country.build_plan(vault, source, country="LB")
    eg = country.build_plan(vault, source, country="EG")
    calls, competing_calls = [], []
    prepare = successful_prepare(calls)

    def competing_attempt(vault, **options):
        with pytest.raises(SessionError):
            country.run_plan(vault._vault, eg, tmp_path / "eg-progress.json", prepare=successful_prepare(competing_calls))
        return prepare(vault, **options)

    country.run_plan(vault, lb, tmp_path / "lb-progress.json", limit=1, prepare=competing_attempt)
    assert calls == [4] and competing_calls == []
    assert not (tmp_path / "eg-progress.json").exists()


def test_changed_opaque_identity_blocks_frozen_row_before_preparation(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    vault._db.execute("UPDATE accounts SET email_key=? WHERE source_row=4", (b"different-synthetic-identity",))
    vault._db.commit()
    calls = []
    result = country.run_plan(vault, plan, tmp_path / "country-progress.json", prepare=successful_prepare(calls))
    assert calls == []
    assert result["status"] == "attention_required" and result["pause_reason"] == "scope_mismatch"


def test_wrong_country_or_wrong_row_cannot_pass_fixed_wrapper(imported):
    vault, _source = imported
    fixed = country.SelectedRowVault(vault, 4, country="LB")
    with pytest.raises(SessionError):
        fixed.record(7)
    vault.records[4]["country"] = "EG"
    with pytest.raises(SessionError):
        fixed.select_test_candidates(1, start_row=4)


def test_failed_intent_checkpoint_prevents_account_work_and_keeps_pending_rows(imported, tmp_path, monkeypatch):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    atomic = country._atomic_json

    def fail_intent(destination, progress):
        if Path(destination) == path and any(row["state"] == "in_progress" for row in progress["rows"]):
            raise OSError(RAW_ERROR)
        return atomic(destination, progress)

    monkeypatch.setattr(country, "_atomic_json", fail_intent)
    calls = []
    with pytest.raises(OSError):
        country.run_plan(vault, plan, path, prepare=successful_prepare(calls))
    assert calls == []
    progress = country.load_progress(path, plan)
    assert all(row["state"] == "pending" for row in progress["rows"] if row["source_row"] in {4, 7, 9})
    assert list(tmp_path.glob("*.tmp")) == []
    assert_private_safe(progress, vault)


@pytest.mark.parametrize("action", [None, "--dry-run", "--status"])
def test_cli_preview_and_status_are_offline_and_do_not_write_checkpoint_or_vault(imported, tmp_path, monkeypatch, capsys, action):
    vault, source = imported
    before = list(vault._db.execute("SELECT source_row,state,session FROM accounts ORDER BY source_row"))
    monkeypatch.setattr(country, "AccountVault", lambda path: vault)
    monkeypatch.setattr(country, "run_plan", lambda *_args, **_options: pytest.fail("An offline command attempted account preparation"))
    args = ["--country", "LB", "--source", str(source), "--vault", str(vault.path), "--limit", "1"]
    if action is not None:
        args.append(action)
    assert country.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is (action != "--status")
    assert result["next_rows"] == [4, 7, 9]
    assert result["connection"] == "direct" and result["play_events_sent"] == result["like_events_sent"] == 0
    assert not (tmp_path / "country-LB-preparation-progress.json").exists()
    assert not (tmp_path / "country-preparation.lock").exists()
    assert list(vault._db.execute("SELECT source_row,state,session FROM accounts ORDER BY source_row")) == before
    assert_private_safe(result, vault)


def test_cli_stop_creates_only_stop_flag_without_loading_account_vault(imported, tmp_path, monkeypatch, capsys):
    vault, source = imported
    monkeypatch.setattr(country, "AccountVault", lambda _path: pytest.fail("Stop must not open or authenticate accounts"))
    assert country.main(["--country", "LB", "--vault", str(vault.path), "--source", str(source), "--stop"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["stop_requested"] is True
    assert json.loads((tmp_path / "country-LB-preparation-progress.stop").read_text(encoding="utf-8")) == {"stop": True}
    assert not (tmp_path / "country-LB-preparation-progress.json").exists()
    assert_private_safe(result, vault)


def test_interrupted_row_with_exact_saved_ready_session_is_enrolled_offline_not_reauthenticated(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    calls = []
    country.run_plan(vault, plan, path, limit=1, prepare=successful_prepare(calls))
    progress = json.loads(path.read_text(encoding="utf-8"))
    next(item for item in progress["rows"] if item["source_row"] == 7).update(state="in_progress", attempts=1, phase="validation")
    progress["status"] = "running"
    path.write_text(json.dumps(progress), encoding="utf-8")
    vault.ready.add(7)
    vault._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=7", (b"synthetic-saved-session",))
    vault._db.commit()
    assert 7 not in vault.enrolled
    country.run_plan(vault, plan, path, prepare=successful_prepare(calls))
    assert calls == [4, 9] and 7 in vault.enrolled
    recovered = next(item for item in country.load_progress(path, plan)["rows"] if item["source_row"] == 7)
    assert recovered["state"] == "ready" and recovered["attempts"] == 1


def test_acknowledged_unknown_row_stays_quarantined_while_other_pending_row_continues(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    calls = []
    country.run_plan(vault, plan, path, limit=1, prepare=successful_prepare(calls))
    progress = json.loads(path.read_text(encoding="utf-8"))
    next(item for item in progress["rows"] if item["source_row"] == 7).update(state="in_progress", attempts=1, phase="login")
    progress["status"] = "running"
    path.write_text(json.dumps(progress), encoding="utf-8")
    paused = country.run_plan(vault, plan, path, prepare=successful_prepare(calls))
    assert paused["status"] == "attention_required" and calls == [4]
    country.run_plan(vault, plan, path, fresh_plan=True, prepare=successful_prepare(calls))
    assert calls == [4, 9]
    final = country.load_progress(path, plan)
    unknown = next(item for item in final["rows"] if item["source_row"] == 7)
    assert unknown["state"] == "unknown" and unknown["attempts"] == 1
    assert final["unknown_acknowledged"] is True
    assert_private_safe(final, vault)


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_atomic_checkpoint_io_failure_preserves_old_file_and_removes_temporary(tmp_path, monkeypatch, operation):
    path = tmp_path / "country-progress.json"
    old = {"synthetic_previous_checkpoint": True}
    country._atomic_json(path, old)
    before = path.read_bytes()

    def fail(*_args, **_options):
        raise OSError(RAW_ERROR)

    monkeypatch.setattr(country.os, operation, fail)
    with pytest.raises(OSError):
        country._atomic_json(path, {"synthetic_new_checkpoint": True})
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_third_consecutive_failure_checkpoint_loads_and_resume_never_replays_failed_rows(imported, tmp_path):
    vault, source = imported
    record = {"country": "LB", "email": "fourth-country@example.invalid", "password": PASSWORD}
    vault.records[11] = record
    identity = hashlib.sha256(record["email"].encode()).digest()
    vault._db.execute("INSERT INTO accounts VALUES(?,?,?,?,?)", (11, identity, b"synthetic-record", "login_required", None))
    vault._db.commit()
    source.write_bytes(source.read_bytes() + f"\nLB~{record['email']}~{PASSWORD}~k=synthetic~cookie=synthetic".encode())
    vault.source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    calls = []

    def fail(_vault, **options):
        calls.append(options["start_row"])
        raise SessionError(RAW_ERROR)

    paused = country.run_plan(vault, plan, path, prepare=fail)
    assert calls == [4, 7, 9]
    assert paused["status"] == "paused" and paused["pause_reason"] == "repeated_failures"
    progress = country.load_progress(path, plan)
    assert progress["consecutive_failures"] == 3
    assert next(item["state"] for item in progress["rows"] if item["source_row"] == 11) == "pending"
    before = path.read_bytes()
    for options in ({}, {"fresh_plan": True}):
        held = country.run_plan(vault, plan, path, prepare=successful_prepare(calls), **options)
        assert calls == [4, 7, 9]
        assert held["status"] == "paused" and held["pause_reason"] == "repeated_failures"
        assert path.read_bytes() == before
    completed = country.run_plan(vault, plan, path, limit=1, resume_after_review=True, prepare=successful_prepare(calls))
    assert calls == [4, 7, 9, 11]
    assert completed["status"] == "completed" and completed["consecutive_failures"] == 0
    assert_private_safe(country.load_progress(path, plan), vault)


@pytest.mark.parametrize("failure,reason", [
    ("The login browser could not close cleanly. Check its processes before retrying.", "browser_cleanup_failed"),
    ("CloakBrowser could not start because its license check failed. Check its key.", "browser_unavailable"),
])
def test_global_failure_pause_is_sticky_and_review_probe_failure_pauses_immediately(imported, tmp_path, failure, reason):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    calls = []
    def fail(_vault, **options):
        calls.append(options["start_row"])
        raise SessionError(failure)
    paused = country.run_plan(vault, plan, path, prepare=fail)
    assert calls == [4] and paused["pause_reason"] == reason
    before = path.read_bytes()
    held = country.run_plan(vault, plan, path, fresh_plan=True, prepare=successful_prepare(calls))
    assert calls == [4] and held["pause_reason"] == reason and path.read_bytes() == before
    def bad_account(_vault, **options):
        calls.append(options["start_row"])
        raise SessionError(RAW_ERROR)
    probe = country.run_plan(vault, plan, path, limit=1, resume_after_review=True, prepare=bad_account)
    assert calls == [4, 7] and probe["status"] == "paused" and probe["pause_reason"] == "repeated_failures"
    assert probe["consecutive_failures"] == 2
    country.run_plan(vault, plan, path, prepare=successful_prepare(calls))
    assert calls == [4, 7]


def test_review_probe_requires_exact_one_account_limit_before_account_work(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    calls = []
    for limit in (None, 2, True):
        with pytest.raises(SessionError):
            country.run_plan(vault, plan, tmp_path / "country-progress.json", limit=limit, resume_after_review=True, prepare=successful_prepare(calls))
    assert calls == []


@pytest.mark.parametrize("flags", [
    ["--resume-after-review"], ["--resume-after-review", "--run"],
    ["--resume-after-review", "--run", "--limit", "2"],
    ["--resume-after-review", "--status", "--limit", "1"],
])
def test_cli_review_acknowledgment_requires_run_and_one_account_limit(monkeypatch, flags):
    monkeypatch.setattr(country, "AccountVault", lambda *_: pytest.fail("Invalid review flags opened the vault"))
    with pytest.raises(SystemExit) as error:
        country.main(["--country", "LB", *flags])
    assert error.value.code == 2


def test_failed_login_stores_only_fixed_diagnostic_evidence(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    class PrivateDiagnosticError(LoginCaptureError):
        @property
        def diagnostics(self):
            return {"password": PASSWORD, "raw_error": RAW_ERROR}
    def fail(_vault, **_options):
        error = PrivateDiagnosticError("login_timeout", stage="home", page_http_status=200, auth_http_status=200)
        error.args = (RAW_ERROR, PASSWORD)
        error.headers = {"cookie": PASSWORD}
        raise error
    summary = country.run_plan(vault, plan, path, limit=1, prepare=fail)
    expected = {"code": "login_timeout", "stage": "home", "page_http_status": 200, "auth_http_status": 200}
    progress = country.load_progress(path, plan)
    failed = next(row for row in progress["rows"] if row["source_row"] == 4)
    assert failed["login_failure"] == expected and failed["error_code"] == "account_failed"
    assert summary["recent_failures"] == [{
        "source_row": 4, "error_code": "account_failed", "login_failure": expected, "connection": "direct",
    }]
    assert_private_safe(summary, vault)
    assert_private_safe(json.loads(path.read_text()), vault)


def test_stored_diagnostics_strip_unknown_fields_and_invalid_statuses(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    country.run_plan(vault, plan, path, limit=1, prepare=successful_prepare([]))
    progress = json.loads(path.read_text())
    progress["rows"][-1]["login_failure"] = {
        "code": "login_timeout", "stage": "home", "page_http_status": "200", "auth_http_status": True,
        "authentication_result": PASSWORD,
        "headers": {"cookie": PASSWORD}, "raw_error": RAW_ERROR, "email": vault.records[4]["email"],
    }
    path.write_text(json.dumps(progress))
    loaded = country.load_progress(path, plan)
    assert loaded["rows"][-1]["login_failure"] == {"code": "login_timeout", "stage": "home"}
    assert_private_safe(loaded, vault)


@pytest.mark.parametrize("diagnostics", [
    RAW_ERROR, {"code": RAW_ERROR, "stage": "home"},
    {"code": "login_timeout", "stage": PASSWORD}, {"code": "login_timeout"},
])
def test_malformed_diagnostic_enum_is_rejected_before_any_account_attempt(imported, tmp_path, diagnostics):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    calls = []
    country.run_plan(vault, plan, path, limit=1, prepare=successful_prepare(calls))
    progress = json.loads(path.read_text())
    progress["rows"][-1]["login_failure"] = diagnostics
    path.write_text(json.dumps(progress))
    before = path.read_bytes()
    with pytest.raises(SessionError):
        country.run_plan(vault, plan, path, prepare=successful_prepare(calls))
    assert calls == [4] and path.read_bytes() == before


def test_legacy_checkpoint_without_login_diagnostics_still_loads(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    country.run_plan(vault, plan, path, limit=1, prepare=successful_prepare([]))
    progress = json.loads(path.read_text())
    for row in progress["rows"]:
        row.pop("login_failure", None)
    path.write_text(json.dumps(progress))
    loaded = country.load_progress(path, plan)
    assert all(row["login_failure"] is None for row in loaded["rows"])


def test_rejected_login_retains_only_allowlisted_authentication_result(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    def fail(_vault, **_options):
        error = LoginCaptureError("login_rejected", stage="home", page_http_status=200,
                                  auth_http_status=200, authentication_result="failed")
        error.authentication_body = {"password": PASSWORD, "url": RAW_ERROR}
        raise error
    result = country.run_plan(vault, plan, path, limit=1, prepare=fail)
    evidence = result["recent_failures"][0]["login_failure"]
    assert evidence == {"code": "login_rejected", "stage": "home", "page_http_status": 200,
                        "auth_http_status": 200, "authentication_result": "failed"}
    assert country.load_progress(path, plan)["rows"][2]["login_failure"] == evidence
    assert_private_safe(result, vault)


def test_full_browser_data_selection_is_forwarded_and_persisted(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    calls = []
    result = country.run_plan(vault, plan, path, limit=1, reduce_browser_data=False,
                              prepare=successful_prepare(calls, reduce_browser_data=False))
    assert calls == [4] and result["reduce_browser_data"] is False
    assert country.load_progress(path, plan)["reduce_browser_data"] is False


def test_no_browser_country_mode_is_persisted_uses_fixed_row_and_never_imports_browser(imported, tmp_path, monkeypatch):
    vault, source = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    calls = []
    monkeypatch.setenv("HTTPS_PROXY", "https://synthetic-environment.invalid")
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name not in {"capture", "browser", "anghami_session.capture", "anghami_session.browser"}
        assert not name.startswith(("playwright", "cloakbrowser"))
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)

    def prepare(selected, **options):
        assert options["no_browser"] is True
        assert options["headless"] is options["reduce_browser_data"] is False
        assert options["proxy"] is None and options["count"] == 1
        assert "HTTPS_PROXY" not in os.environ and os.environ["NO_PROXY"] == "*"
        row = options["start_row"]
        assert selected.select_test_candidates(1, start_row=row) == [row]
        options["progress"]({"phase": "session_recovery"})
        current = country.load_progress(path, plan)
        item = next(item for item in current["rows"] if item["source_row"] == row)
        assert item["phase"] == "session_recovery" and item["attempts"] == 1
        calls.append(row)
        selected.enable_test_account(row)
        return {"passed": True, "selected_rows": [row], "prepared_rows": [row], "prepared_account_count": 1}

    summary = country.run_plan(vault, plan, path, limit=1, no_browser=True, prepare=prepare)
    assert calls == [4]
    assert summary["no_browser"] is True and summary["browser"] == "none"
    assert summary["headless"] is summary["reduce_browser_data"] is summary["browser_required"] is False
    assert summary["preparation_method"] == "http"
    assert country.load_progress(path, plan)["no_browser"] is True
    assert os.environ["HTTPS_PROXY"] == "https://synthetic-environment.invalid"
    assert_private_safe(summary, vault)


@pytest.mark.parametrize("value", [None, 0, 1, 1.0, "true", [], {}])
def test_no_browser_country_mode_requires_exact_boolean_before_work(imported, tmp_path, value):
    vault, source = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    calls = []
    with pytest.raises(SessionError):
        country.run_plan(vault, plan, path, no_browser=value, prepare=successful_prepare(calls))
    assert not path.exists() and calls == [] and vault.calls == []


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_malformed_stored_no_browser_mode_rejected_before_work_or_rewrite(imported, tmp_path, value):
    vault, source = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    progress = country._new_progress(plan)
    progress["no_browser"] = value
    path.write_text(json.dumps(progress))
    before = path.read_bytes()
    with pytest.raises(SessionError):
        country.run_plan(vault, plan, path, no_browser=True, prepare=lambda *_args, **_kwargs: pytest.fail("Invalid checkpoint reached HTTP recovery"))
    assert path.read_bytes() == before and vault.calls == []


def test_legacy_country_checkpoint_defaults_to_browser_mode(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    progress = country._new_progress(plan)
    progress.pop("no_browser")
    path.write_text(json.dumps(progress))
    summary = country.summarize(country.load_progress(path, plan))
    assert summary["no_browser"] is False and summary["browser"] == "cloakbrowser"
    assert summary["preparation_method"] == "browser" and summary["browser_required"] is True


def test_no_browser_mode_cannot_bypass_existing_browser_failure_hold(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"

    def fail(*_args, **_options):
        raise SessionError("The login browser could not close cleanly. Check its processes before retrying.")

    country.run_plan(vault, plan, path, prepare=fail)
    before = path.read_bytes()
    held = country.run_plan(vault, plan, path, no_browser=True, fresh_plan=True,
                            prepare=lambda *_args, **_kwargs: pytest.fail("HTTP mode bypassed failure hold"))
    assert held["failure_hold"] == held["pause_reason"] == "browser_cleanup_failed"
    assert held["no_browser"] is False and path.read_bytes() == before


@pytest.mark.parametrize("action", [[], ["--status"]])
def test_no_browser_country_preview_is_offline_and_status_preserves_stored_mode(imported, monkeypatch, capsys, action):
    vault, source = imported
    monkeypatch.setattr(country, "AccountVault", lambda _: vault)
    monkeypatch.setattr(country, "run_plan", lambda *_args, **_options: pytest.fail("Offline CLI started recovery"))
    before = sorted(vault.path.parent.iterdir())
    assert country.main(["--country", "LB", "--source", str(source), "--vault", str(vault.path), "--no-browser", *action]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["no_browser"] is (not action)
    assert summary["browser"] == ("cloakbrowser" if action else "none")
    assert sorted(vault.path.parent.iterdir()) == before and vault.calls == []


def test_cli_no_browser_country_mode_forwards_only_when_enabled(imported, monkeypatch, capsys):
    vault, source = imported
    calls = []
    monkeypatch.setattr(country, "AccountVault", lambda _: vault)

    def run(_vault, plan, _path, **options):
        calls.append(options)
        progress = country._new_progress(plan)
        progress["no_browser"] = options.get("no_browser", False)
        return country.summarize(progress)

    monkeypatch.setattr(country, "run_plan", run)
    args = ["--country", "LB", "--source", str(source), "--vault", str(vault.path), "--run", "--limit", "1"]
    assert country.main(args + ["--no-browser"]) == 0
    assert calls[-1]["no_browser"] is True
    assert json.loads(capsys.readouterr().out)["browser"] == "none"
    assert country.main(args) == 0
    assert "no_browser" not in calls[-1]
    assert json.loads(capsys.readouterr().out)["browser"] == "cloakbrowser"


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
def test_browser_data_selection_is_strict_boolean_before_account_work(imported, tmp_path, value):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    calls = []
    with pytest.raises(SessionError):
        country.run_plan(vault, plan, tmp_path / "country-progress.json", reduce_browser_data=value,
                         prepare=successful_prepare(calls))
    assert calls == []


def test_legacy_progress_defaults_to_previous_reduced_browser_selection(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    country.run_plan(vault, plan, path, limit=1, prepare=successful_prepare([]))
    progress = json.loads(path.read_text())
    progress.pop("reduce_browser_data")
    path.write_text(json.dumps(progress))
    assert country.summarize(country.load_progress(path, plan))["reduce_browser_data"] is True


def test_cli_full_browser_data_forwards_only_the_chosen_normal_login_option(imported, monkeypatch, capsys):
    vault, source = imported
    calls = []
    monkeypatch.setattr(country, "AccountVault", lambda _: vault)
    def run(_vault, plan, path, **options):
        calls.append(options)
        progress = country._new_progress(plan)
        progress["reduce_browser_data"] = options["reduce_browser_data"]
        return country.summarize(progress)
    monkeypatch.setattr(country, "run_plan", run)
    result = country.main(["--country", "LB", "--source", str(source), "--vault", str(vault.path),
                           "--run", "--resume-after-review", "--limit", "1", "--full-browser-data"])
    assert result == 0 and calls[0]["reduce_browser_data"] is False
    assert calls[0]["resume_after_review"] is True and calls[0]["limit"] == 1
    assert json.loads(capsys.readouterr().out)["reduce_browser_data"] is False


@pytest.mark.parametrize("damage", ["non_object_row", "attempted_pending", "unattempted_ready", "duplicate_row"])
def test_damaged_checkpoint_cannot_replay_or_create_an_account_attempt(imported, tmp_path, damage):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    calls = []
    country.run_plan(vault, plan, path, limit=1, prepare=successful_prepare(calls))
    progress = json.loads(path.read_text(encoding="utf-8"))
    if damage == "non_object_row":
        progress["rows"].append(RAW_ERROR)
    elif damage == "attempted_pending":
        next(item for item in progress["rows"] if item["source_row"] == 7)["attempts"] = 1
    elif damage == "unattempted_ready":
        next(item for item in progress["rows"] if item["source_row"] == 4)["attempts"] = 0
    else:
        progress["rows"].append(deepcopy(progress["rows"][-1]))
    path.write_text(json.dumps(progress), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SessionError):
        country.run_plan(vault, plan, path, prepare=successful_prepare(calls))
    assert calls == [4] and path.read_bytes() == before


def test_loaded_checkpoint_removes_unrecognized_private_fields(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    country.run_plan(vault, plan, path, limit=1, prepare=successful_prepare([]))
    progress = json.loads(path.read_text(encoding="utf-8"))
    progress.update(password=PASSWORD, raw_error=RAW_ERROR, email=vault.records[4]["email"])
    progress["rows"][-1].update(headers={"Cookie": PASSWORD}, url=RAW_ERROR)
    path.write_text(json.dumps(progress), encoding="utf-8")
    loaded = country.load_progress(path, plan)
    assert_private_safe(loaded, vault)
    assert_private_safe(country.summarize(loaded), vault)


def test_source_change_after_active_account_persists_attention_before_next_login(imported, tmp_path):
    vault, source = imported
    plan = country.build_plan(vault, source, country="LB")
    path = tmp_path / "country-progress.json"
    calls = []
    prepare = successful_prepare(calls)

    def change_after_active(vault, **options):
        result = prepare(vault, **options)
        source.write_bytes(source.read_bytes() + b"\n")
        return result

    paused = country.run_plan(vault, plan, path, prepare=change_after_active)
    assert calls == [4]
    assert paused["status"] == "attention_required" and paused["pause_reason"] == "source_changed"
    progress = country.load_progress(path, plan)
    assert progress["status"] == "attention_required" and progress["active_row"] is None
    assert next(item["state"] for item in progress["rows"] if item["source_row"] == 7) == "pending"
