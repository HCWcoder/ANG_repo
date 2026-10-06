"""Frozen UI preparation cohorts/checkpoints use a fully offline country runner."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from anghami_session import ui_preparation as bridge
from anghami_session.errors import SessionError
from test_country_preparation_workers import imported

JOB_ID = "a" * 32
OLD_JOB_ID = "b" * 32
SOURCE_HASH = "c" * 64
POOL_HASH = "d" * 64
SECRET = "synthetic-bridge-private-material"


class FakeVault:
    def __init__(self, path, rows):
        self.path = Path(path)
        self.rows = list(rows)
        self.selection_calls = []
        self.initial_states = {}
        self.enrolled = set()
    def select_test_candidates(self, count, *, country, randomize):
        self.selection_calls.append((count, country, randomize))
        return list(self.rows)
    def metadata(self, name):
        assert name == "source_sha256"
        return SOURCE_HASH
    def enrolled_test_rows(self):
        return frozenset(self.enrolled)


class FakePool:
    def __len__(self):
        return 2
    def fingerprint(self):
        return POOL_HASH
    def summary(self):
        return {"country": getattr(self, "country", "EG"), "endpoint": "http://proxy.packetstream.io:31112"}


@pytest.fixture
def offline_bridge(tmp_path, monkeypatch):
    rows = [43, 41, 47, 45, 49, 51, 53, 55, 57, 59]
    vault = FakeVault(tmp_path / "accounts.sqlite3", rows)
    plan_calls, run_calls, attempts, published = [], [], [], []
    outcomes = {}
    def build_selected_plan(selected_vault, source_path, selected_rows, *, country):
        assert selected_vault is vault
        assert source_path == Path(bridge.__file__).resolve().parents[1] / "registered.txt"
        plan_calls.append((list(selected_rows), country))
        return {"country": country, "selected_rows": list(selected_rows),
                "rows": [{"source_row": row, "state": vault.initial_states.get(row, "pending"), "attempts": 0}
                         for row in selected_rows]}
    def new_progress(plan):
        return {"rows": deepcopy(plan["rows"]), "connection": "direct"}
    def summary(checkpoint, *, status, active=(), cursor=None):
        counts = {}
        for item in checkpoint["rows"]:
            counts[item["state"]] = counts.get(item["state"], 0) + 1
        value = {"counts": counts, "active_rows": list(active), "workers": checkpoint["workers"],
                 "connection_pending_rows": [item["source_row"] for item in checkpoint["rows"] if item["state"] == "connection_pending"],
                 "attempted_accounts": sum(item["attempts"] > 0 for item in checkpoint["rows"]),
                 "status": status, "pause_reason": None, "connection": checkpoint["connection"],
                 "consecutive_failures": 0, "private": SECRET}
        if cursor is not None:
            value["proxy_pool"] = {"fingerprint": POOL_HASH, "next_ordinal": cursor}
        return value
    def run_plan(selected_vault, plan, checkpoint_path, **options):
        assert selected_vault is vault
        checkpoint = json.loads(checkpoint_path.read_text())
        run_calls.append((deepcopy(checkpoint), dict(options)))
        todo = [item for item in checkpoint["rows"] if item["state"] == "pending"]
        cursor = checkpoint.get("proxy_pool_cursor")
        options["progress_callback"](summary(checkpoint, status="running", active=[item["source_row"] for item in todo[:2]], cursor=cursor))
        for item in todo:
            attempts.append(item["source_row"])
            item.update(state=outcomes.get(item["source_row"], "ready"), attempts=1)
        if cursor is not None:
            cursor += len(todo)
        bridge.country._atomic_json(checkpoint_path, checkpoint)
        terminal = "completed_with_pending" if any(item["state"] in {"unknown", "connection_pending"} for item in checkpoint["rows"]) else "completed"
        final = summary(checkpoint, status=terminal, cursor=cursor)
        options["progress_callback"](final)
        return final
    monkeypatch.setattr(bridge.country, "build_selected_plan", build_selected_plan)
    monkeypatch.setattr(bridge.country, "_new_progress", new_progress)
    monkeypatch.setattr(bridge.country, "run_plan", run_plan)
    monkeypatch.setattr(bridge.country, "load_progress", lambda path, _plan: json.loads(path.read_text()))
    return SimpleNamespace(vault=vault, rows=rows, plan_calls=plan_calls, run_calls=run_calls,
                           attempts=attempts, published=published, outcomes=outcomes)


def options(count, **extra):
    return {"action": "prepare", "no_browser": True, "workers": 10, "account_country": "EG",
            "count": count, "proxy_egypt": False, "proxy_sticky_pool": False, **extra}


def manifest(rows, *, held=()):
    return {"format_version": 1, "source_job_id": OLD_JOB_ID, "country": "EG", "count": len(rows),
            "selected_rows": list(rows), "held_rows": list(held), "source_sha256": SOURCE_HASH, "resumed_by": None}


def invoke(fixture, **extra):
    return bridge.run_parallel_ui_preparation(fixture.vault, options(len(fixture.rows), **extra),
                                              job_id=JOB_ID, progress=fixture.published.append)


def test_parallel_bridge_freezes_exact_ten_selected_rows_and_publishes_public_progress(offline_bridge):
    fixture = offline_bridge
    result = invoke(fixture)
    assert fixture.vault.selection_calls == [(10, "EG", True)]
    assert fixture.plan_calls == [(fixture.rows, "EG")] and fixture.attempts == fixture.rows
    initial, call = fixture.run_calls[0]
    assert initial["workers"] == 10 and initial["no_browser"] is True
    assert initial["max_consecutive_failures"] == 20 and initial["reduce_browser_data"] is False
    assert call["workers"] == 10 and call["owned_ui_job_id"] == JOB_ID
    assert call["ui_path"] == fixture.vault.path.parent / "ui-last-job.json"
    assert result["passed"] and result["selected_rows"] == result["prepared_rows"] == fixture.rows
    assert result["prepared_account_count"] == result["attempted_accounts"] == 10
    assert fixture.published[0]["active_workers"] == 2 and fixture.published[0]["active_rows"] == fixture.rows[:2]
    assert result["active_workers"] == 0 and result["active_rows"] == []
    assert result["like_events_sent"] == result["play_events_sent"] == 0
    assert SECRET not in json.dumps(fixture.published)
    assert SECRET not in (fixture.vault.path.parent / "accounts-prepare-tests-report.json").read_text()


def test_resume_uses_exact_manifest_rows_skips_held_and_preserves_already_ready_attempts(offline_bridge):
    fixture = offline_bridge
    rows = fixture.rows[:4]
    fixture.vault.initial_states[rows[0]] = "already_ready"
    fixture.vault.enrolled.add(rows[0])
    fixture.vault.path.with_name("ui-preparation-resume.json").write_text(json.dumps(manifest(rows, held=(rows[2],))))
    result = bridge.run_parallel_ui_preparation(fixture.vault, options(4, resume_preparation=True),
                                              job_id=JOB_ID, progress=fixture.published.append)
    assert fixture.vault.selection_calls == [] and fixture.plan_calls == [(rows, "EG")]
    initial, _call = fixture.run_calls[0]
    by_row = {item["source_row"]: item for item in initial["rows"]}
    assert initial["unknown_acknowledged"] is True
    assert by_row[rows[2]]["state"] == "unknown" and by_row[rows[2]]["attempts"] == 1
    assert by_row[rows[0]]["attempts"] == 1
    assert fixture.attempts == [rows[1], rows[3]]
    assert result["prepared_account_count"] == 3 and result["preparation_held"] == 1
    assert result["attention_required_rows"] == [rows[2]] and result["passed"] is False
    claimed = json.loads(fixture.vault.path.with_name("ui-preparation-resume.json").read_text())
    assert claimed["resumed_by"] == JOB_ID


def test_parallel_bridge_carries_ready_failed_connection_pending_and_held_counts(offline_bridge):
    fixture = offline_bridge
    fixture.outcomes.update({fixture.rows[0]: "connection_pending", fixture.rows[1]: "failed", fixture.rows[2]: "unknown"})
    result = invoke(fixture)
    assert result["prepared_account_count"] == 7
    assert result["connection_pending_count"] == 1 and result["connection_pending_rows"] == [fixture.rows[0]]
    assert result["account_failed_count"] == 1 and result["account_failed_rows"] == [fixture.rows[1]]
    assert result["preparation_held"] == 1 and result["attention_required_rows"] == [fixture.rows[2]]


@pytest.mark.parametrize("state,enrolled", [("already_ready", False), ("already_enrolled", True)])
def test_parallel_bridge_revalidates_saved_or_enrolled_accounts_without_ready_enrollment(offline_bridge, state, enrolled):
    fixture = offline_bridge
    row = fixture.rows[0]
    fixture.vault.initial_states[row] = state
    if enrolled:
        fixture.vault.enrolled.add(row)
    result = invoke(fixture)
    initial, _call = fixture.run_calls[0]
    assert initial["rows"][0]["state"] == "pending" and row in fixture.attempts
    assert result["prepared_account_count"] == 10 and result["passed"] is True


def test_parallel_bridge_mirrors_sticky_pool_cursor_without_credentials(offline_bridge):
    fixture = offline_bridge
    pool = FakePool()
    base = fixture.vault.path.parent
    (base / "ui-sticky-pool-position.json").write_text(json.dumps({"fingerprint": POOL_HASH, "cursor": 7}))
    loads = []
    def load(path):
        loads.append(path)
        return pool
    result = bridge.run_parallel_ui_preparation(fixture.vault, options(10, proxy_egypt=True, proxy_sticky_pool=True),
                                              job_id=JOB_ID, progress=fixture.published.append, pool_loader=load)
    initial, call = fixture.run_calls[0]
    assert loads == [base / "packetstream-sticky-pool.dpapi"]
    assert initial["proxy_pool_cursor"] == 7 and initial["proxy_pool_count"] == 2
    assert call["proxy_sticky_pool"] == base / "packetstream-sticky-pool.dpapi"
    assert json.loads((base / "ui-sticky-pool-position.json").read_text()) == {"fingerprint": POOL_HASH, "cursor": 17}
    assert result["connection"] == "proxy_egypt" and result["proxy_sticky_pool"] is True


def test_parallel_bridge_records_us_sticky_pool_country(offline_bridge):
    fixture = offline_bridge
    pool = FakePool()
    pool.country = "US"
    base = fixture.vault.path.parent
    result = bridge.run_parallel_ui_preparation(
        fixture.vault, options(10, proxy_egypt=True, proxy_sticky_pool=True),
        job_id=JOB_ID, progress=fixture.published.append, pool_loader=lambda _path: pool,
    )
    initial, _call = fixture.run_calls[0]
    assert initial["proxy_pool_country"] == "US"
    assert result["connection"] == "proxy_egypt"


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(format_version=True),
    lambda value: value.update(source_job_id="unsafe/path"),
    lambda value: value.update(country="LB"),
    lambda value: value.update(count=True),
    lambda value: value.update(count=9),
    lambda value: value.update(selected_rows=[43] * 10),
    lambda value: value.update(selected_rows=[True] + value["selected_rows"][1:]),
    lambda value: value.update(held_rows=[999]),
    lambda value: value.update(held_rows=[43, 43]),
    lambda value: value.update(source_sha256="e" * 64),
    lambda value: value.update(resumed_by="f" * 32),
])
def test_resume_manifest_tamper_is_rejected_before_country_runner_or_new_selection(offline_bridge, mutation):
    fixture = offline_bridge
    value = manifest(fixture.rows)
    mutation(value)
    fixture.vault.path.with_name("ui-preparation-resume.json").write_text(json.dumps(value))
    with pytest.raises(SessionError, match="continuation"):
        invoke(fixture, resume_preparation=True)
    assert fixture.vault.selection_calls == [] and fixture.run_calls == [] and fixture.attempts == []


def test_duplicate_job_checkpoint_never_starts_country_runner(offline_bridge):
    fixture = offline_bridge
    checkpoint = fixture.vault.path.parent / f"ui-preparation-progress-{JOB_ID}.json"
    checkpoint.write_text("{}")
    with pytest.raises(SessionError, match="already has a checkpoint"):
        invoke(fixture)
    assert fixture.run_calls == [] and fixture.attempts == [] and checkpoint.read_text() == "{}"


@pytest.mark.parametrize("use_proxy", [False, True])
def test_resumed_seed_roundtrips_real_country_schema_with_ready_held_and_pending_rows(imported, monkeypatch, use_proxy):
    """Use real plan/progress validators while replacing only account dispatch."""
    vault, _all_country_plan, source, factory = imported
    with vault._db:
        vault._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=1", (b"synthetic",))
        vault._db.execute("INSERT INTO test_accounts VALUES(1)")
    rows = [1, 2, 3, 4]
    resume = manifest(rows, held=(2,))
    resume["source_sha256"] = vault.metadata("source_sha256")
    (vault.path.parent / "ui-preparation-resume.json").write_text(json.dumps(resume))
    # Point the bridge's fixed source location at this synthetic import.
    monkeypatch.setattr(bridge, "__file__", str(source.parent / "anghami_session" / "ui_preparation.py"))
    captured = []
    def no_dispatch(selected_vault, plan, checkpoint_path, **call):
        assert selected_vault is vault
        # Deliberately retain the production validator, unlike the lightweight
        # bridge fixture: this is the pre-dispatch boundary that failed live.
        seeded = bridge.country.load_progress(checkpoint_path, plan)
        captured.append((deepcopy(seeded), deepcopy(plan), checkpoint_path))
        return bridge.country.summarize(seeded)
    monkeypatch.setattr(bridge.country, "run_plan", no_dispatch)
    published = []
    result = bridge.run_parallel_ui_preparation(vault, options(4, resume_preparation=True, proxy_egypt=use_proxy),
                                              job_id=JOB_ID, progress=published.append)
    assert len(captured) == 1
    seed, plan, checkpoint = captured[0]
    assert plan["source_sha256"] == vault.metadata("source_sha256") and plan["identity_bindings"]
    by_row = {item["source_row"]: item for item in seed["rows"]}
    assert by_row[1]["state"] == "ready" and by_row[1]["attempts"] == 1 and by_row[1]["phase"] == "complete"
    assert by_row[1]["connection"] == ("proxy_egypt" if use_proxy else "direct")
    assert by_row[2]["state"] == "unknown" and by_row[2]["attempts"] == 1
    assert by_row[2]["phase"] == "stopped" and by_row[2]["error_code"] == "interrupted_unknown"
    assert [(by_row[row]["state"], by_row[row]["attempts"]) for row in (3, 4)] == [("pending", 0), ("pending", 0)]
    assert seed["unknown_acknowledged"] is True and seed["workers"] == 10
    assert result["prepared_account_count"] == 1 and result["preparation_held"] == 1
    assert result["attempted_accounts"] == 2 and result["attention_required_rows"] == [2]
    assert not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)
    # Reproduce the old incompatible seed to prove the validator is exercised.
    invalid = deepcopy(seed)
    invalid["rows"][0]["state"] = "already_ready"
    bridge.country._atomic_json(checkpoint, invalid)
    with pytest.raises(SessionError, match="progress file is invalid"):
        bridge.country.load_progress(checkpoint, plan)
