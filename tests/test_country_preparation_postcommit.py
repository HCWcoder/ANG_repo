"""Local report failures never replay authentication or invent committed readiness."""

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from anghami_session import country_preparation as country, play_record, preparation
from anghami_session.errors import SessionError
from test_country_preparation_sticky_pool import imported, install_pool, run, assert_safe, bound_progress
from test_country_preparation_workers import SyntheticVault, install_recovery, states


def account_events(factory, name, row=1):
    return [event for event in factory.events if event[0] == name and event[1] == row]


def test_scope_invalid_result_remains_quarantined_even_with_committed_session_and_enrollment(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    install_pool(monkeypatch, factory)
    install_recovery(monkeypatch, factory)

    def invalid_result(vault, **options):
        result = preparation.prepare_test_accounts(vault, **options)
        return {**result, "selected_rows": [2]}

    result = run(parent, plan, path, prepare=invalid_result, limit=1, max_consecutive_failures=20)
    assert parent._db.execute("SELECT state,session IS NOT NULL FROM accounts WHERE source_row=1").fetchone() == ("ready", 1)
    assert 1 in parent.enrolled_test_rows()
    assert result["pause_reason"] == "scope_mismatch" and result["counts"]["ready"] == 0
    assert states(path, plan)[1]["state"] == "unknown"
    assert all(len(account_events(factory, name)) == 1 for name in ("recover", "attach", "enroll"))


def test_validation_phase_failure_before_enrollment_stays_failed_with_original_report(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    install_pool(monkeypatch, factory)
    install_recovery(monkeypatch, factory)

    def fail_enrollment(self, row):
        self.factory.log("enrollment_attempt", row)
        raise SessionError("The synthetic enrollment did not commit.")

    monkeypatch.setattr(SyntheticVault, "enable_test_account", fail_enrollment)
    result = run(parent, plan, path, limit=1, max_consecutive_failures=20)
    assert parent._db.execute("SELECT state,session IS NOT NULL FROM accounts WHERE source_row=1").fetchone() == ("ready", 1)
    assert 1 not in parent.enrolled_test_rows()
    assert result["counts"]["failed"] == 1 and result["counts"]["ready"] == 0
    assert states(path, plan)[1]["state"] == "failed"
    reports = list((tmp_path / "country-preparation-reports").rglob("account-1.redacted.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["failed_phase"] == "validation" and report["prepared_rows"] == []
    assert len(account_events(factory, "recover")) == len(account_events(factory, "attach")) == 1
    assert len(account_events(factory, "enrollment_attempt")) == 1


@pytest.mark.parametrize("failed_phase", ["validation", "complete"])
@pytest.mark.parametrize("failure_report_writable", [False, True])
def test_genuine_final_report_failure_reconciles_only_acknowledged_committed_account(imported, tmp_path, monkeypatch, failed_phase, failure_report_writable):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    install_pool(monkeypatch, factory)
    install_recovery(monkeypatch, factory)
    original_journal, failures = preparation._journal, []

    def journal(report, destination):
        if report.get("prepared_rows") == [1] and report.get("phase") == failed_phase:
            failures.append(deepcopy(report))
            raise play_record._Failure("journal_failed", "The synthetic local report could not be saved.")
        if not failure_report_writable and report.get("phase") == "stopped" and report.get("local_finalization_failure"):
            raise play_record._Failure("journal_failed", "The synthetic failure report also could not be saved.")
        return original_journal(report, destination)

    monkeypatch.setattr(preparation, "_journal", journal)
    result = run(parent, plan, path, limit=1, max_consecutive_failures=20)
    assert len(failures) == 1
    assert result["counts"]["ready"] == 1 and result["counts"]["failed"] == 0
    assert result["pause_reason"] == "limit_reached" and result["consecutive_failures"] == 0
    row = states(path, plan)[1]
    assert row["state"] == "ready" and row["attempts"] == 1
    evidence = row["local_finalization_failure"]
    assert evidence["code"] == "journal_failed" and evidence["failed_phase"] == failed_phase
    assert evidence["prior_error_code"] == "account_failed" and evidence["reconciled"] == "worker_committed"
    assert 1 in parent.enrolled_test_rows() and parent.session(1) == {"synthetic_saved_row": 1}
    assert all(len(account_events(factory, name)) == 1 for name in ("recover", "attach", "enroll"))
    report_path = next((tmp_path / "country-preparation-reports").rglob("account-1.redacted.json"))
    report = json.loads(report_path.read_text())
    assert report["passed"] is False
    if failure_report_writable:
        assert report["failed_phase"] == failed_phase
        assert report["prepared_rows"] == [1] and report["failed_row"] == 1
    else:
        assert report["phase"] == "validation"
    assert_safe(row, factory)
    assert_safe(report, factory)
    before = list(factory.events)
    run(parent, plan, path, limit=1, max_consecutive_failures=20)
    assert all(len(account_events(factory, name)) == 1 for name in ("recover", "attach", "enroll"))
    assert len(factory.events) > len(before)
    assert states(path, plan)[1]["local_finalization_failure"] == evidence


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_local_report_rename_contention_recovers_without_replaying_any_account_work(tmp_path, monkeypatch, winerror):
    path = tmp_path / "report.json"
    path.write_text('{"version":"old"}', encoding="utf-8")
    original_replace, attempts, sleeps = os.replace, [], []

    def replace(temporary, destination):
        attempts.append(Path(temporary))
        if len(attempts) <= 2:
            assert json.loads(path.read_text()) == {"version": "old"}
            error = OSError(13, "synthetic private file path and account credential")
            error.winerror = winerror
            raise error
        return original_replace(temporary, destination)

    monkeypatch.setattr(play_record.os, "replace", replace)
    monkeypatch.setattr(play_record.time, "sleep", sleeps.append)
    play_record._journal({"version": "new", "prepared_rows": [1]}, path)
    assert len(attempts) == 3 and len(set(attempts)) == 1 and sleeps == [0.1, 0.1]
    assert json.loads(path.read_text()) == {"version": "new", "prepared_rows": [1]}
    assert not list(tmp_path.glob("*.tmp"))


def test_local_report_rename_exhaustion_is_bounded_preserves_old_report_and_safe_diagnostics(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    before = b'{"version":"old"}'
    path.write_bytes(before)
    clock, attempts, sleeps = [0.0], [], []

    def replace(_temporary, _destination):
        attempts.append(clock[0])
        error = PermissionError(13, "synthetic private report path and proxy authentication")
        error.winerror = 32
        raise error

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(play_record.os, "replace", replace)
    monkeypatch.setattr(play_record.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(play_record.time, "sleep", sleep)
    with pytest.raises(play_record._Failure) as captured:
        play_record._journal({"version": "new"}, path)
    error = captured.value
    assert error.code == "journal_failed"
    assert error.local_diagnostics == {"error_kind": "filesystem_error", "errno": 13, "winerror": 32}
    assert "synthetic private" not in str(error)
    assert 1.99 <= sum(sleeps) <= 2.11 and len(attempts) <= 23
    assert path.read_bytes() == before and not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("error", [OSError(28, "synthetic private disk path"), KeyboardInterrupt()])
def test_report_writer_does_not_retry_unrelated_storage_faults_or_interrupts(tmp_path, monkeypatch, error):
    path = tmp_path / "report.json"
    attempts = []

    def replace(_temporary, _destination):
        attempts.append(1)
        raise error

    monkeypatch.setattr(play_record.os, "replace", replace)
    monkeypatch.setattr(play_record.time, "sleep", lambda _seconds: pytest.fail("Unrelated local failure was retried"))
    with pytest.raises(KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else play_record._Failure):
        play_record._journal({"passed": False}, path)
    assert attempts == [1] and not path.exists() and not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("kwargs", [
    {"row": True, "failed_phase": "complete"}, {"row": 0, "failed_phase": "complete"},
    {"row": 1, "failed_phase": "session_recovery"},
    {"row": 1, "failed_phase": []},
    {"row": 1, "failed_phase": "complete", "diagnostics": {"error_kind": []}},
    {"row": 1, "failed_phase": "complete", "diagnostics": {"error_kind": "report_error", "errno": 13}},
    {"row": 1, "failed_phase": "complete", "diagnostics": {"error_kind": "filesystem_error", "errno": True}},
    {"row": 1, "failed_phase": "complete", "diagnostics": {"error_kind": "filesystem_error", "winerror": "32"}},
    {"row": 1, "failed_phase": "complete", "diagnostics": {"error_kind": "filesystem_error", "errno": 65536}},
    {"row": 1, "failed_phase": "complete", "diagnostics": {"error_kind": "report_error", "message": "synthetic secret"}},
])
def test_finalization_error_rejects_unproved_phase_row_or_private_diagnostics(kwargs):
    with pytest.raises(ValueError):
        preparation.PreparationFinalizationError(**kwargs)


@pytest.mark.parametrize("fault", ["no_acknowledgment", "other_row", "missing_enrollment", "bad_session", "changed_identity", "changed_source", "changed_country", "ordinary_exception"])
def test_finalization_reconciliation_cannot_promote_incomplete_or_wrong_account_proof(imported, tmp_path, monkeypatch, fault):
    parent, plan, source, factory = imported
    path = tmp_path / "progress.json"
    install_pool(monkeypatch, factory)
    install_recovery(monkeypatch, factory)
    original_session = SyntheticVault.session

    def prepare(vault, **options):
        if fault == "no_acknowledgment":
            with vault._vault._db:
                vault._vault._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=1", (b"synthetic",))
                vault._vault._db.execute("INSERT INTO test_accounts VALUES(1)")
        else:
            preparation.prepare_test_accounts(vault, **options)
        if fault == "missing_enrollment":
            with vault._vault._db:
                vault._vault._db.execute("DELETE FROM test_accounts WHERE source_row=1")
        elif fault == "changed_identity":
            with vault._vault._db:
                vault._vault._db.execute("UPDATE accounts SET email_key=? WHERE source_row=1", (b"changed-synthetic-identity",))
        elif fault == "changed_source":
            source.write_bytes(source.read_bytes() + b"\nEG~synthetic-source-change")
        elif fault == "changed_country":
            factory.records[1]["country"] = "LB"
        elif fault == "bad_session":
            def session(self, row):
                if row == 1:
                    raise SessionError("The synthetic stored session failed local validation.")
                return original_session(self, row)
            monkeypatch.setattr(SyntheticVault, "session", session)
        if fault == "ordinary_exception":
            raise RuntimeError("synthetic unrelated postcommit exception")
        raise preparation.PreparationFinalizationError(2 if fault == "other_row" else 1, failed_phase="complete")

    result = run(parent, plan, path, prepare=prepare, limit=1, max_consecutive_failures=20)
    row = states(path, plan)[1]
    assert result["counts"]["ready"] == 0 and row["state"] in {"failed", "unknown"}
    assert row.get("local_finalization_failure") is None
    for name in ("recover", "attach", "enroll"):
        assert len(account_events(factory, name)) <= 1


def write_post_run_evidence(parent, plan, path, pool, *, held=False):
    progress = bound_progress(plan, pool, cursor=4)
    progress.update(consecutive_failures=20 if held else 2, infrastructure_failures=7 if held else 1,
                    failure_hold="repeated_failures" if held else None,
                    pause_reason="repeated_failures" if held else "limit_reached")
    for row in (1, 2, 4):
        progress["rows"][row - 1].update(state="failed", phase="stopped", attempts=1,
                                         error_code="account_failed", connection="proxy_egypt")
        parent.attach(row, {"synthetic_saved_row": row})
        parent.enable_test_account(row)
    path.write_text(json.dumps(progress), encoding="utf-8")
    reports = {}
    for row in (1, 2):
        report_path = path.parent / "country-preparation-reports" / progress["run_id"] / f"account-{row}.redacted.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "requested_accounts": 1, "selected_rows": [row], "prepared_rows": [row], "prepared_account_count": 1,
            "attempted_accounts": 1, "passed": False, "dry_run": False, "phase": "stopped",
            "failed_phase": "complete", "failed_row": row, "error_code": "preparation_failed",
            "active_row": row, "checked_at_utc": progress["created_at_utc"],
            "browser": "none", "no_browser": True, "preparation_method": "http",
            "like_events_sent": 0, "play_events_sent": 0,
        }
        report_path.write_text(json.dumps(report), encoding="utf-8")
        metadata_path = path.parent / f"account-{row}.session-recovery.redacted.json"
        metadata = {
            "source_row": row, "authenticated": True, "identity_verified": True, "session_renewed": True,
            "operations": {"relations": "ok", "playlists": "ok"},
            "preparation_method": "http", "server_verified": True,
            "checked_at_utc": progress["created_at_utc"],
        }
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        reports[row] = (report_path, metadata_path)
    return progress, reports


@pytest.mark.parametrize("held", [False, True])
def test_post_run_reconciliation_requires_new_attempt_and_keeps_reports_counters_and_holds(imported, tmp_path, monkeypatch, held):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    pool, _selected = install_pool(monkeypatch, factory)
    before, reports = write_post_run_evidence(parent, plan, path, pool, held=held)
    original_reports = {file: file.read_bytes() for pair in reports.values() for file in pair}
    baseline = {row: 0 for row in range(1, 31)}
    baseline[2] = 1
    events = list(factory.events)
    country.reconcile_finalization_failures(parent, plan, path, baseline_attempts=baseline)
    saved = country.load_progress(path, plan)
    assert saved["rows"][0]["state"] == "ready" and saved["rows"][0]["attempts"] == 1
    evidence = saved["rows"][0]["local_finalization_failure"]
    assert evidence == {
        "code": "postcommit_reporting_failed", "failed_phase": "complete", "error_kind": "report_error",
        "prior_error_code": "account_failed", "report_error_code": "preparation_failed", "reconciled": "post_run_committed",
    }
    assert saved["rows"][1]["state"] == saved["rows"][3]["state"] == "failed"
    assert saved["rows"][2]["state"] == "pending"
    for field in ("consecutive_failures", "infrastructure_failures", "failure_hold", "status", "pause_reason",
                  "run_id", "created_at_utc", "proxy_pool_cursor", "unknown_acknowledged"):
        assert saved[field] == before[field]
    assert all(file.read_bytes() == content for file, content in original_reports.items())
    assert all(sum(event[0] == name for event in factory.events) == sum(event[0] == name for event in events)
               for name in ("recover", "attach", "enroll", "verify_pool"))
    assert_safe(saved, factory)
    snapshot = path.read_bytes()
    country.reconcile_finalization_failures(parent, plan, path, baseline_attempts=baseline)
    assert path.read_bytes() == snapshot


@pytest.mark.parametrize("fault", [
    "baseline_missing", "baseline_already_attempted", "report_missing", "report_wrong_row", "report_no_prepared_row",
    "report_bad_count", "report_early_phase", "report_passed", "metadata_missing", "metadata_wrong_row",
    "metadata_not_authenticated", "metadata_identity_unproved", "metadata_not_renewed", "metadata_operation_failed",
    "metadata_boolean_strings", "metadata_stale", "metadata_naive_time", "no_enrollment", "bad_session",
    "wrong_identity", "wrong_country", "source_changed",
])
def test_post_run_reconciliation_leaves_unproved_failure_untouched_without_account_calls(imported, tmp_path, monkeypatch, fault):
    parent, plan, source, factory = imported
    path = tmp_path / "progress.json"
    pool, _selected = install_pool(monkeypatch, factory)
    _progress, files = write_post_run_evidence(parent, plan, path, pool)
    report_path, metadata_path = files[1]
    baseline = {1: 0}
    if fault == "baseline_missing":
        baseline = {}
    elif fault == "baseline_already_attempted":
        baseline = {1: 1}
    elif fault == "report_missing":
        report_path.unlink()
    elif fault.startswith("report_"):
        report = json.loads(report_path.read_text())
        field, value = {
            "report_wrong_row": ("selected_rows", [2]), "report_no_prepared_row": ("prepared_rows", []),
            "report_bad_count": ("prepared_account_count", 0), "report_early_phase": ("failed_phase", "session_recovery"),
            "report_passed": ("passed", True),
        }[fault]
        report[field] = value
        report_path.write_text(json.dumps(report), encoding="utf-8")
    elif fault == "metadata_missing":
        metadata_path.unlink()
    elif fault.startswith("metadata_"):
        metadata = json.loads(metadata_path.read_text())
        if fault == "metadata_operation_failed":
            metadata["operations"]["playlists"] = "failed"
        elif fault == "metadata_stale":
            metadata["checked_at_utc"] = "2000-01-01T00:00:00+00:00"
        elif fault == "metadata_naive_time":
            metadata["checked_at_utc"] = "2099-01-01T00:00:00"
        else:
            field, value = {
                "metadata_wrong_row": ("source_row", 2), "metadata_not_authenticated": ("authenticated", False),
                "metadata_identity_unproved": ("identity_verified", False), "metadata_not_renewed": ("session_renewed", False),
                "metadata_boolean_strings": ("authenticated", "true"),
            }[fault]
            metadata[field] = value
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif fault == "no_enrollment":
        with parent._db:
            parent._db.execute("DELETE FROM test_accounts WHERE source_row=1")
    elif fault == "bad_session":
        monkeypatch.setattr(parent, "session", lambda _row: (_ for _ in ()).throw(SessionError("Synthetic invalid session")))
    elif fault == "wrong_identity":
        with parent._db:
            parent._db.execute("UPDATE accounts SET email_key=? WHERE source_row=1", (b"different-synthetic-identity",))
    elif fault == "wrong_country":
        factory.records[1]["country"] = "LB"
    elif fault == "source_changed":
        source.write_bytes(source.read_bytes() + b"\nEG~synthetic-source-change")
    snapshot, events = path.read_bytes(), list(factory.events)
    if fault == "source_changed":
        with pytest.raises(SessionError):
            country.reconcile_finalization_failures(parent, plan, path, baseline_attempts=baseline)
    else:
        country.reconcile_finalization_failures(parent, plan, path, baseline_attempts=baseline)
    assert path.read_bytes() == snapshot and states(path, plan)[1]["state"] == "failed"
    assert all(sum(event[0] == name for event in factory.events) == sum(event[0] == name for event in events)
               for name in ("recover", "attach", "enroll", "verify_pool"))


@pytest.mark.parametrize("baseline", [None, [], {True: 0}, {0: 0}, {31: 0}, {1: True}, {1: 2}, {"1": 0}])
def test_post_run_reconciliation_rejects_untrusted_baseline_without_writes(imported, tmp_path, monkeypatch, baseline):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    pool, _selected = install_pool(monkeypatch, factory)
    write_post_run_evidence(parent, plan, path, pool)
    snapshot, events = path.read_bytes(), list(factory.events)
    with pytest.raises(SessionError):
        country.reconcile_finalization_failures(parent, plan, path, baseline_attempts=baseline)
    assert path.read_bytes() == snapshot and factory.events == events


def test_post_run_reconciliation_refuses_still_running_country_job(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    pool, _selected = install_pool(monkeypatch, factory)
    progress, _files = write_post_run_evidence(parent, plan, path, pool)
    progress.update(status="running", pause_reason=None, active_row=3, active_rows=[3])
    progress["rows"][2].update(state="in_progress", attempts=1, phase="proxy_preflight", connection="proxy_egypt")
    path.write_text(json.dumps(progress), encoding="utf-8")
    snapshot, events = path.read_bytes(), list(factory.events)
    with pytest.raises(SessionError):
        country.reconcile_finalization_failures(parent, plan, path, baseline_attempts={1: 0})
    assert path.read_bytes() == snapshot and factory.events == events


def test_post_run_source_change_during_proof_prevents_any_ready_checkpoint_publish(imported, tmp_path, monkeypatch):
    parent, plan, source, factory = imported
    path = tmp_path / "progress.json"
    pool, _selected = install_pool(monkeypatch, factory)
    write_post_run_evidence(parent, plan, path, pool)
    original_session = parent.session

    def session(row):
        saved = original_session(row)
        source.write_bytes(source.read_bytes() + b"\nEG~synthetic-source-change-during-proof")
        return saved

    monkeypatch.setattr(parent, "session", session)
    snapshot, events = path.read_bytes(), list(factory.events)
    with pytest.raises(SessionError):
        country.reconcile_finalization_failures(parent, plan, path, baseline_attempts={1: 0})
    assert path.read_bytes() == snapshot and states(path, plan)[1]["state"] == "failed"
    assert not any(event[0] in {"recover", "attach", "enroll", "verify_pool"} for event in factory.events[len(events):])


@pytest.mark.parametrize("field,value", [
    ("code", "private-error"), ("failed_phase", "session_recovery"), ("error_kind", "network_error"),
    ("errno", True), ("winerror", "32"), ("prior_error_code", "proxy_preflight_failed"),
    ("reconciled", "unchecked"), ("report_error_code", "synthetic private error"),
])
def test_checkpoint_rejects_malformed_finalization_audit_instead_of_publishing_private_data(imported, tmp_path, monkeypatch, field, value):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    pool, _selected = install_pool(monkeypatch, factory)
    progress, _files = write_post_run_evidence(parent, plan, path, pool)
    progress["rows"][0].update(state="ready", phase="complete", error_code=None, local_finalization_failure={
        "code": "postcommit_reporting_failed", "failed_phase": "complete", "error_kind": "report_error",
        "prior_error_code": "account_failed", "report_error_code": "preparation_failed", "reconciled": "post_run_committed",
    })
    progress["rows"][0]["local_finalization_failure"][field] = value
    path.write_text(json.dumps(progress), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SessionError):
        country.load_progress(path, plan)
    assert path.read_bytes() == before


@pytest.mark.parametrize("ui", [
    {"id": "synthetic-ui-job", "status": "queued"},
    {"id": "synthetic-ui-job", "status": "running"},
    {"id": "synthetic-ui-job", "status": "untrusted"},
    {"status": "succeeded"},
])
def test_post_run_reconciliation_refuses_busy_or_unavailable_ui_state(imported, tmp_path, monkeypatch, ui):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    pool, _selected = install_pool(monkeypatch, factory)
    write_post_run_evidence(parent, plan, path, pool)
    (tmp_path / "ui-last-job.json").write_text(json.dumps(ui), encoding="utf-8")
    before, events = path.read_bytes(), list(factory.events)
    with pytest.raises(SessionError):
        country.reconcile_finalization_failures(parent, plan, path, baseline_attempts={1: 0})
    assert path.read_bytes() == before and factory.events == events


@pytest.mark.parametrize("field", ["selected_rows", "prepared_rows", "active_row", "failed_row"])
def test_post_run_report_boolean_row_cannot_stand_in_for_exact_account_identity(imported, tmp_path, monkeypatch, field):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    pool, _selected = install_pool(monkeypatch, factory)
    _progress, files = write_post_run_evidence(parent, plan, path, pool)
    report_path = files[1][0]
    report = json.loads(report_path.read_text())
    report[field] = [True] if field.endswith("_rows") else True
    report_path.write_text(json.dumps(report), encoding="utf-8")
    before = path.read_bytes()
    result = country.reconcile_finalization_failures(parent, plan, path, baseline_attempts={1: 0})
    assert result["reconciled_rows"] == [] and path.read_bytes() == before


@pytest.mark.parametrize("fault", ["identity", "country", "session", "enrollment"])
def test_final_local_proof_recheck_blocks_mutation_after_initial_reconciliation_proof(imported, tmp_path, monkeypatch, fault):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    pool, _selected = install_pool(monkeypatch, factory)
    write_post_run_evidence(parent, plan, path, pool)
    original_proof, calls = country._local_committed_ready, []

    def proof(selected):
        calls.append(selected.row)
        valid = original_proof(selected)
        if len(calls) == 1:
            assert valid is True
            if fault == "identity":
                with parent._db:
                    parent._db.execute("UPDATE accounts SET email_key=? WHERE source_row=1", (b"synthetic-changed-after-proof",))
            elif fault == "country":
                factory.records[1]["country"] = "LB"
            elif fault == "session":
                with parent._db:
                    parent._db.execute("UPDATE accounts SET session=NULL WHERE source_row=1")
            else:
                with parent._db:
                    parent._db.execute("DELETE FROM test_accounts WHERE source_row=1")
        return valid

    monkeypatch.setattr(country, "_local_committed_ready", proof)
    before, events = path.read_bytes(), list(factory.events)
    with pytest.raises(SessionError):
        country.reconcile_finalization_failures(parent, plan, path, baseline_attempts={1: 0})
    assert path.read_bytes() == before and states(path, plan)[1]["state"] == "failed"
    assert not any(event[0] in {"recover", "attach", "enroll", "verify_pool"} for event in factory.events[len(events):])


