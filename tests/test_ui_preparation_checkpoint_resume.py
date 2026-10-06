"""Worker changes preserve a drained synthetic country's durable checkpoint."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from anghami_session import country_preparation as country, ui_preparation as bridge
from anghami_session.errors import LoginCaptureError, RequestFailure, SessionError, safe_login_failure, safe_request_failure
from test_country_preparation_workers import imported

OLD_JOB = "a" * 32
NEW_JOB = "b" * 32
POOL_HASH = "c" * 64
PRIVATE_CANDIDATE = b"synthetic-private-issued-session-candidate"


class Pool:
    def __init__(self, *, fingerprint=POOL_HASH, count=2):
        self.value, self.count = fingerprint, count
    def __len__(self):
        return self.count
    def fingerprint(self):
        return self.value
    def summary(self):
        return {"endpoint": "http://proxy.packetstream.io:31112"}


@pytest.fixture
def drained(imported, monkeypatch):
    vault, _all_plan, source, factory = imported
    with vault._db:
        vault._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=1", (b"synthetic",))
        vault._db.execute("INSERT INTO test_accounts VALUES(1)")
        vault._db.execute("CREATE TABLE synthetic_pending_candidates(source_row INTEGER PRIMARY KEY, payload BLOB)")
        vault._db.execute("INSERT INTO synthetic_pending_candidates VALUES(2,?)", (PRIVATE_CANDIDATE,))
    rows = list(range(1, 9))
    plan = country.build_selected_plan(vault, source, rows, country="EG")
    progress = country._new_progress(plan)
    progress.update(status="paused", pause_reason="stop_requested", workers=10, no_browser=True,
                    reduce_browser_data=False, max_consecutive_failures=20, unknown_acknowledged=True,
                    connection="proxy_egypt", consecutive_failures=2)
    by_row = {item["source_row"]: item for item in progress["rows"]}
    by_row[1].update(state="ready", attempts=1, phase="complete", connection="proxy_egypt")
    for row, stage, code, retries in [(2, "session_recovery_validation", 7, 2), (3, "country_check", 56, 1)]:
        by_row[row].update(state="connection_pending", attempts=1, phase="connection_pending",
                           error_code="provider_unavailable", connection="proxy_egypt",
                           provider_failure=safe_request_failure(RequestFailure("request_transport_failed", stage=stage, curl_code=code, retry_safe=False)),
                           provider_retry_count=retries)
    by_row[4].update(state="failed", attempts=1, phase="complete", error_code="account_failed", connection="proxy_egypt",
                     login_failure=safe_login_failure(LoginCaptureError("login_rejected", stage="submit", auth_http_status=200, authentication_result="failed")))
    by_row[5].update(state="unknown", attempts=1, phase="stopped", error_code="interrupted_unknown", connection="proxy_egypt")
    manifest = {"format_version": 1, "source_job_id": OLD_JOB, "checkpoint_job_id": OLD_JOB,
                "count": len(rows), "country": "EG", "selected_rows": rows, "held_rows": [5],
                "source_sha256": vault.metadata("source_sha256"), "resumed_by": None}
    base = vault.path.parent
    old_path = base / f"ui-preparation-progress-{OLD_JOB}.json"
    manifest_path = base / "ui-preparation-resume.json"
    country._atomic_json(old_path, progress)
    country._atomic_json(manifest_path, manifest)
    monkeypatch.setattr(bridge, "__file__", str(source.parent / "anghami_session" / "ui_preparation.py"))
    dispatch = []
    def no_account_dispatch(selected_vault, current_plan, path, **call):
        assert selected_vault is vault
        current = country.load_progress(path, current_plan)
        dispatch.append((deepcopy(current), current_plan, path, call))
        summary = country.summarize(current)
        call["progress_callback"](summary)
        return summary
    monkeypatch.setattr(country, "run_plan", no_account_dispatch)
    return SimpleNamespace(vault=vault, plan=plan, progress=progress, manifest=manifest, base=base,
                           old_path=old_path, manifest_path=manifest_path, dispatch=dispatch,
                           published=[], factory=factory)


def save(fixture):
    country._atomic_json(fixture.old_path, fixture.progress)
    country._atomic_json(fixture.manifest_path, fixture.manifest)


def invoke(fixture, *, pool=None, **overrides):
    options = {"action": "prepare", "no_browser": True, "workers": 50, "account_country": "EG",
               "count": 8, "resume_preparation": True, "proxy_egypt": True, "proxy_sticky_pool": False,
               **overrides}
    return bridge.run_parallel_ui_preparation(fixture.vault, options, job_id=NEW_JOB,
                                              progress=fixture.published.append,
                                              pool_loader=lambda _path: pool or Pool())


def test_worker_change_preserves_all_rows_retry_evidence_pending_candidate_and_held_status(drained):
    fixture = drained
    old = country.load_progress(fixture.old_path, fixture.plan)
    result = invoke(fixture)
    assert len(fixture.dispatch) == 1
    current, _plan, _path, call = fixture.dispatch[0]
    assert current["rows"] == old["rows"]
    assert current["workers"] == 50 and current["run_id"] != old["run_id"]
    assert current["status"] == "not_started" and current["pause_reason"] is None
    assert current["unknown_acknowledged"] is True and current["consecutive_failures"] == 2
    assert call["workers"] == 50 and call["owned_ui_job_id"] == NEW_JOB
    assert result["prepared_rows"] == [1] and result["connection_pending_rows"] == [2, 3]
    assert result["account_failed_rows"] == [4] and result["attention_required_rows"] == [5]
    assert result["prepared_account_count"] == 1 and result["connection_pending_count"] == 2
    assert result["account_failed_count"] == result["preparation_held"] == 1
    assert fixture.vault._db.execute("SELECT payload FROM synthetic_pending_candidates WHERE source_row=2").fetchone()[0] == PRIVATE_CANDIDATE
    assert country.load_progress(fixture.old_path, fixture.plan) == old
    assert json.loads(fixture.manifest_path.read_text())["resumed_by"] == NEW_JOB
    assert PRIVATE_CANDIDATE.decode() not in json.dumps(fixture.published)
    assert not any(event[0] in {"recover", "attach", "enroll"} for event in fixture.factory.events)


def enable_pool(fixture):
    fixture.progress.update(proxy_pool_active=True, proxy_pool_count=2, proxy_pool_fingerprint=POOL_HASH,
                            proxy_pool_cursor=17, proxy_pool_endpoint="http://proxy.packetstream.io:31112")
    save(fixture)
    country._atomic_json(fixture.base / "ui-sticky-pool-position.json", {"fingerprint": POOL_HASH, "cursor": 17})


def test_worker_change_preserves_and_mirrors_exact_drained_sticky_cursor(drained):
    fixture = drained
    enable_pool(fixture)
    result = invoke(fixture, proxy_sticky_pool=True)
    current = fixture.dispatch[0][0]
    assert current["proxy_pool_cursor"] == 17 and current["proxy_pool_fingerprint"] == POOL_HASH
    assert current["proxy_pool_count"] == 2 and current["proxy_pool_active"] is True
    assert json.loads((fixture.base / "ui-sticky-pool-position.json").read_text()) == {"fingerprint": POOL_HASH, "cursor": 17}
    assert result["connection"] == "proxy_egypt"


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(status="running", pause_reason=None),
    lambda p: p.update(pause_reason="limit_reached"),
    lambda p: p.update(unknown_acknowledged=False),
    lambda p: p.update(failure_hold="repeated_failures"),
    lambda p: p.update(consecutive_failures=20),
    lambda p: (p["rows"][5].update(state="in_progress", attempts=1, phase="preparing"), p.update(active_row=6, active_rows=[6])),
    lambda p: p["rows"][5].update(state="in_progress", attempts=1, phase="preparing"),
])
def test_worker_change_rejects_undrained_or_unreviewed_checkpoint_before_claim_and_dispatch(drained, mutation):
    fixture = drained
    mutation(fixture.progress)
    save(fixture)
    with pytest.raises(SessionError):
        invoke(fixture)
    assert fixture.dispatch == [] and json.loads(fixture.manifest_path.read_text())["resumed_by"] is None
    assert not (fixture.base / f"ui-preparation-progress-{NEW_JOB}.json").exists()


@pytest.mark.parametrize("changes", [
    {"checkpoint_job_id": "../escape"},
    {"checkpoint_job_id": "d" * 32},
    {"checkpoint_job_id": NEW_JOB, "source_job_id": NEW_JOB},
    {"checkpoint_job_id": "d" * 32, "source_job_id": "d" * 32},
    {"held_rows": []},
    {"held_rows": [2]},
])
def test_worker_change_rejects_rebound_checkpoint_path_or_held_set_before_claim(drained, changes):
    fixture = drained
    fixture.manifest.update(changes)
    save(fixture)
    with pytest.raises(SessionError):
        invoke(fixture)
    assert fixture.dispatch == [] and json.loads(fixture.manifest_path.read_text())["resumed_by"] is None


@pytest.mark.parametrize("change", ["cursor", "fingerprint", "count", "disable"])
def test_worker_change_rejects_changed_pool_binding_without_claiming_manifest(drained, change):
    fixture = drained
    enable_pool(fixture)
    selected = Pool()
    overrides = {"proxy_sticky_pool": True}
    if change == "cursor":
        country._atomic_json(fixture.base / "ui-sticky-pool-position.json", {"fingerprint": POOL_HASH, "cursor": 18})
    elif change == "fingerprint":
        selected = Pool(fingerprint="e" * 64)
    elif change == "count":
        selected = Pool(count=3)
    elif change == "disable":
        overrides["proxy_sticky_pool"] = False
    with pytest.raises(SessionError):
        invoke(fixture, pool=selected, **overrides)
    assert fixture.dispatch == [] and json.loads(fixture.manifest_path.read_text())["resumed_by"] is None


@pytest.mark.parametrize("previous_proxy,new_proxy", [(True, False), (False, True)])
def test_worker_change_cannot_change_drained_connection_mode(drained, previous_proxy, new_proxy):
    fixture = drained
    fixture.progress["connection"] = "proxy_egypt" if previous_proxy else "direct"
    save(fixture)
    with pytest.raises(SessionError, match="connection"):
        invoke(fixture, proxy_egypt=new_proxy)
    assert fixture.dispatch == [] and json.loads(fixture.manifest_path.read_text())["resumed_by"] is None

