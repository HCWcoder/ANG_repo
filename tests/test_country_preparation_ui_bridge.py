"""The UI bridge keeps exact synthetic rows and owns its coordinator guards."""

from copy import deepcopy
import json
import threading

import pytest

from anghami_session import country_preparation as country
from anghami_session.errors import SessionError
from test_country_preparation_workers import (
    PRIVATE_ERROR, PRIVATE_PASSWORD, SyntheticVault, assert_safe, imported, install_recovery,
)


OWNED = "a" * 32
OTHER = "b" * 32


def running_ui(path, job_id=OWNED, status="running"):
    path.write_text(json.dumps({"id": job_id, "status": status}), encoding="utf-8")


def test_selected_plan_preserves_exact_rows_order_and_canonical_identity_bindings(imported):
    parent, full, source, factory = imported
    before = list(parent._db.execute("SELECT source_row,state,session FROM accounts ORDER BY source_row"))
    selected = country.build_selected_plan(parent, source, [8, 2, 5], country="EG")
    assert selected["rows"] == [{"source_row": row, "state": "pending"} for row in (8, 2, 5)]
    assert selected["identity_bindings"] == {str(row): full["identity_bindings"][str(row)] for row in (8, 2, 5)}
    assert selected["source_sha256"] == full["source_sha256"]
    assert selected["tagged_rows"] == 3 and selected["duplicate_rows"] == 0
    assert selected["plan_id"] != full["plan_id"]
    assert list(parent._db.execute("SELECT source_row,state,session FROM accounts ORDER BY source_row")) == before
    assert_safe(selected, factory)


@pytest.mark.parametrize("rows", [[], None, {}, [1, 1], [True], [0], [-1], [1.0], ["1"]])
def test_invalid_selected_rows_reject_before_source_or_vault_access(rows):
    with pytest.raises(SessionError, match="distinct positive"):
        country.build_selected_plan(object(), "unavailable-source", rows, country="EG")


@pytest.mark.parametrize("case", ["source_changed", "country", "record_country", "missing_vault", "missing_source", "duplicate_identity"])
def test_selected_plan_rejects_mismatch_without_any_preparation(imported, case):
    parent, _full, source, factory = imported
    rows, tag = [1, 2], "EG"
    if case == "source_changed":
        source.write_bytes(source.read_bytes() + b"\n")
    elif case == "country":
        tag = "LB"
    elif case == "record_country":
        factory.records[1]["country"] = "LB"
    elif case == "missing_vault":
        with parent._db:
            parent._db.execute("DELETE FROM accounts WHERE source_row=1")
    elif case == "missing_source":
        rows = [9]
    elif case == "duplicate_identity":
        with parent._db:
            parent._db.execute("UPDATE accounts SET email_key=(SELECT email_key FROM accounts WHERE source_row=1) WHERE source_row=2")
    with pytest.raises(SessionError):
        country.build_selected_plan(parent, source, rows, country=tag)
    assert not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)


def test_frozen_pending_delegation_checks_row_and_identity_before_parent(imported, monkeypatch):
    parent, plan, _source, _factory = imported
    calls, saved = [], {"synthetic_saved_row": 1}
    monkeypatch.setattr(parent, "pending_session", lambda row: calls.append(("load", row)) or deepcopy(saved), raising=False)
    monkeypatch.setattr(parent, "save_pending_session", lambda row, candidate: calls.append(("save", row, candidate)) or {"source_row": row}, raising=False)
    selected = country.SelectedRowVault(parent, 1, country="EG", identity_binding=plan["identity_bindings"]["1"])
    assert selected.pending_session(1) == saved
    assert selected.save_pending_session(1, saved) == {"source_row": 1}
    for method in (lambda: selected.pending_session(2), lambda: selected.save_pending_session(2, saved)):
        with pytest.raises(SessionError, match="cannot change"):
            method()
    assert calls == [("load", 1), ("save", 1, saved)]
    with parent._db:
        parent._db.execute("UPDATE accounts SET email_key=? WHERE source_row=1", (b"synthetic-changed-identity",))
    with pytest.raises(SessionError, match="identity changed"):
        selected.pending_session(1)
    with pytest.raises(SessionError, match="identity changed"):
        selected.save_pending_session(1, saved)
    assert len(calls) == 2


def test_frozen_vault_without_optional_pending_store_reads_none_but_cannot_fake_save(imported):
    parent, _plan, _source, _factory = imported
    selected = country.SelectedRowVault(parent, 1, country="EG")
    assert selected.pending_session(1) is None
    with pytest.raises(SessionError, match="saved securely"):
        selected.save_pending_session(1, {"synthetic_saved_row": 1})


def test_owned_ui_and_coordinator_callback_support_ten_workers_on_exact_subset(imported, tmp_path, monkeypatch):
    parent, _full, source, factory = imported
    plan = country.build_selected_plan(parent, source, [8, 2, 5], country="EG")
    install_recovery(monkeypatch, factory)
    ui_path = tmp_path / "ui.json"
    running_ui(ui_path)
    owner = threading.get_ident()
    callbacks = []

    def progress(summary):
        assert threading.get_ident() == owner
        assert_safe(summary, factory)
        assert "identity_bindings" not in summary and "source_path" not in summary
        callbacks.append(deepcopy(summary))
        summary["counts"]["ready"] = -100  # A callback cannot mutate the checkpoint.

    result = country.run_plan(
        parent, plan, tmp_path / "progress.json", workers=10, no_browser=True,
        max_consecutive_failures=20, owned_ui_job_id=OWNED, ui_path=ui_path, progress_callback=progress,
    )
    assert result["status"] == "completed" and result["workers"] == 10
    assert result["counts"]["ready"] == 3
    recovered = sorted(event[1] for event in factory.events if event[0] == "recover")
    assert recovered == [2, 5, 8]
    assert callbacks and callbacks[-1]["counts"]["ready"] == 3
    assert all(summary["workers"] == 10 for summary in callbacks)
    assert_safe(result, factory)


def test_pending_candidate_crosses_frozen_worker_wrapper_without_renewal_replay(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    pending = {1: {"synthetic_saved_row": 1}}
    original_attach = SyntheticVault.attach

    def load(self, row):
        self.factory.log("pending_load", row)
        return deepcopy(pending.get(row))

    def save(self, row, candidate):
        self.factory.log("pending_save", row)
        pending[row] = deepcopy(candidate)

    def attach(self, row, saved, **options):
        result = original_attach(self, row, saved, **options)
        pending.pop(row, None)
        return result

    monkeypatch.setattr(SyntheticVault, "pending_session", load, raising=False)
    monkeypatch.setattr(SyntheticVault, "save_pending_session", save, raising=False)
    monkeypatch.setattr(SyntheticVault, "attach", attach)
    install_recovery(monkeypatch, factory)
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=10, no_browser=True, max_consecutive_failures=20)
    assert result["counts"]["ready"] == 8 and pending == {}
    assert not any(event[0] == "recover" and event[1] == 1 for event in factory.events)
    assert sum(event[0] == "attach" and event[1] == 1 for event in factory.events) == 1
    assert sum(event[0] == "enroll" and event[1] == 1 for event in factory.events) == 1


@pytest.mark.parametrize("status", ["running", "succeeded"])
def test_owned_ui_bridge_stops_when_another_job_replaces_owner_before_worker_reads(imported, tmp_path, monkeypatch, status):
    parent, plan, _source, factory = imported
    install_recovery(monkeypatch, factory)
    ui_path = tmp_path / "ui.json"
    running_ui(ui_path)

    def progress(summary):
        if summary["active_rows"]:
            running_ui(ui_path, job_id=OTHER, status=status)

    result = country.run_plan(
        parent, plan, tmp_path / "progress.json", workers=10, no_browser=True,
        max_consecutive_failures=20, ui_path=ui_path, owned_ui_job_id=OWNED, progress_callback=progress,
    )
    assert result["status"] == "paused" and result["pause_reason"] in {"ui_busy", "ui_activity"}
    assert not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)


def test_owned_job_finished_status_stops_instead_of_accepting_stale_owner(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    install_recovery(monkeypatch, factory)
    ui_path = tmp_path / "ui.json"
    running_ui(ui_path, status="failed")
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=10, no_browser=True,
                              ui_path=ui_path, owned_ui_job_id=OWNED)
    assert result["status"] == "paused" and result["pause_reason"] == "ui_activity"
    assert not any(event[0] == "recover" for event in factory.events)


def test_callback_failure_is_safe_and_starts_no_account_work(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    install_recovery(monkeypatch, factory)

    def fail(_summary):
        raise RuntimeError(PRIVATE_ERROR + PRIVATE_PASSWORD)

    with pytest.raises(SessionError, match="progress could not be reported") as failure:
        country.run_plan(parent, plan, tmp_path / "progress.json", workers=10, no_browser=True, progress_callback=fail)
    assert PRIVATE_ERROR not in str(failure.value) and PRIVATE_PASSWORD not in str(failure.value)
    assert not any(event[0] == "recover" for event in factory.events)


@pytest.mark.parametrize("options", [
    {"owned_ui_job_id": "wrong"}, {"owned_ui_job_id": "A" * 32},
    {"owned_ui_job_id": 123}, {"progress_callback": True}, {"progress_callback": {}},
])
def test_invalid_ui_bridge_options_reject_before_vault_access(options):
    with pytest.raises(SessionError):
        country.run_plan(object(), {}, "unused", workers=10, no_browser=True, **options)


def test_previously_reviewed_unknown_row_remains_quarantined_during_other_rows(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    install_recovery(monkeypatch, factory)
    path = tmp_path / "progress.json"
    progress = country._new_progress(plan)
    progress["rows"][0].update(state="unknown", attempts=1, phase="stopped", error_code="interrupted_unknown")
    progress["unknown_acknowledged"] = True
    country._atomic_json(path, progress)
    result = country.run_plan(parent, plan, path, workers=10, no_browser=True, max_consecutive_failures=20)
    assert result["counts"]["ready"] == 7 and result["counts"]["unknown"] == 1
    assert not any(event[0] == "recover" and event[1] == 1 for event in factory.events)
    held = country.load_progress(path, plan)["rows"][0]
    assert held["state"] == "unknown" and held["attempts"] == 1
