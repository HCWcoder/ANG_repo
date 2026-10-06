"""A completed test is distinct from a new verified like or an attempted write."""

import json

import pytest

from anghami_session import proxy, provider_recovery, ui_jobs, ui_server
from test_action_provider_retries import make_manager, run
from test_ui_jobs_concurrency import finish, manager_for
from test_ui_provider_review import browser_handler


def new_like():
    return {"passed": True, "mutation_attempted": True, "mutation_accepted": True,
            "mutation_result": "accepted", "liked_before": False, "liked_after": True,
            "persisted_state_verified": True}


def existing_like():
    return {"passed": True, "mutation_attempted": False, "mutation_accepted": False,
            "mutation_result": "skipped_already_liked", "liked_before": True,
            "liked_after": True, "persisted_state_verified": True}


@pytest.mark.parametrize("workers,maximum", [(1, 1), (8, 20)])
def test_repeated_checks_count_only_one_new_like_per_account(tmp_path, workers, maximum):
    manager, factory = manager_for(tmp_path, rows=[1, 2],
        handler=lambda action, row, number, options: new_like() if number == 1 else existing_like())
    manager.submit({"action": "like", "rows": [1, 2], "count": 3,
                    "workers": workers, "max_consecutive_failures": maximum})
    job = finish(manager)
    assert job["status"] == "succeeded" and job["succeeded"] == job["completed_tests"] == 6
    assert job["writes_attempted"] == job["writes_accepted"] == job["new_likes_verified"] == 2
    assert job["already_liked_verified"] == 4
    assert "2 new likes verified; 4 already liked" in job["message"]
    saved = json.loads(manager._report_path.read_text(encoding="utf-8"))
    assert saved["new_likes_verified"] == 2 and saved["already_liked_verified"] == 4


def test_sixteen_rate_limited_country_checks_finish_without_claiming_likes(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_recovery, "wait_for_provider", lambda failure, stop=None: True)
    manager, factory, _pool = make_manager(tmp_path,
        lambda *args: (_ for _ in ()).throw(proxy.ProxyCountryError("http_failure", http_status=429)))
    job = run(manager, factory, "like")
    assert job["completed_tests"] == job["connection_pending"] == 16
    assert job["succeeded"] == job["failed"] == 0
    assert all(job[key] == 0 for key in ui_jobs._WRITE_COUNTERS)
    assert job["message"] == "0 likes sent; 16 connections pending."
    public = ui_server.safe_report(job)
    assert all(public[key] == 0 for key in ui_jobs._WRITE_COUNTERS)


@pytest.mark.parametrize("action", ["play", "like"])
def test_uncertain_write_counts_attempt_without_claiming_acceptance_or_new_like(tmp_path, action):
    prefix = "event" if action == "play" else "mutation"
    manager, _factory = manager_for(tmp_path, rows=[1, 2], handler=lambda *args: {
        "passed": False, f"{prefix}_attempted": True, f"{prefix}_accepted": False,
        f"{prefix}_result": "unknown", "error_code": "transport_failed"})
    manager.submit({"action": action, "rows": [1, 2], "workers": 1, "max_consecutive_failures": 20})
    job = finish(manager)
    assert job["status"] == "failed" and job["writes_attempted"] == 1
    assert job["writes_accepted"] == job["new_likes_verified"] == job["already_liked_verified"] == 0
    assert "1 " + action + " request(s) attempted" in job["message"]


@pytest.mark.parametrize("changed", [
    {"passed": False}, {"mutation_attempted": False}, {"mutation_accepted": False},
    {"mutation_result": "unknown"}, {"liked_before": True}, {"liked_before": None},
    {"liked_after": False}, {"persisted_state_verified": False},
])
def test_new_like_counter_requires_the_complete_before_write_and_readback_evidence(changed):
    counts = ui_jobs._write_counts("like", {**new_like(), **changed})
    assert counts["new_likes_verified"] == 0
    assert counts["already_liked_verified"] == 0


@pytest.mark.parametrize("key", ui_jobs._WRITE_COUNTERS)
@pytest.mark.parametrize("bad", [True, -1, 2**31, "16", None])
def test_public_write_counters_reject_invalid_values(key, bad):
    assert ui_server.safe_report({key: bad}) == {key: None}


def test_real_ui_explains_older_sixteen_pending_run_without_new_counter_fields():
    job = {"id": "synthetic-older-run", "action": "like", "status": "completed_with_pending",
           "phase": "complete", "attempted": 16, "completed_tests": 16,
           "progress": {"completed": 0, "total": 16}, "succeeded": 0, "failed": 0,
           "connection_pending": 16, "results_total": 16,
           "results": [{"source_row": row, "passed": False, "mutation_attempted": False,
                        "outcome": "connection_pending"} for row in range(1, 17)]}
    shown = browser_handler(render_job=job)
    assert shown["progress"] == "16 / 16 checks finished"
    assert "0 likes sent; 16 connections pending." in shown["message"]
    assert "Checks passed 0" in shown["metrics"]
    assert "Like requests attempted 0" in shown["metrics"]
    assert "New likes verified 0" in shown["metrics"]


def test_real_ui_distinguishes_one_new_like_from_repeated_existing_like_checks():
    job = {"id": "synthetic-like-results", "action": "like", "status": "succeeded", "phase": "complete",
           "attempted": 3, "completed_tests": 3, "progress": {"completed": 3, "total": 3},
           "succeeded": 3, "failed": 0, "results_total": 3, "connection_pending": 0,
           "results": [new_like(), existing_like(), existing_like()]}
    shown = browser_handler(render_job=job)
    assert "Checks passed 3" in shown["metrics"]
    assert "Like requests attempted 1" in shown["metrics"]
    assert "New likes verified 1" in shown["metrics"]
    assert "Already liked 2" in shown["metrics"]
    assert "1 new likes verified; 2 already liked" in shown["message"]
    assert "A new like was saved and verified." in shown["results"]


def test_older_truncated_run_never_infers_whole_run_writes_from_partial_results():
    job = {"id": "synthetic-truncated", "action": "like", "status": "succeeded", "phase": "complete",
           "attempted": 501, "completed_tests": 501, "progress": {"completed": 501, "total": 501},
           "succeeded": 501, "results_total": 501, "results": [existing_like()]}
    shown = browser_handler(render_job=job)
    assert "Write totals were not recorded for this older run." in shown["message"]
    assert "Like requests attempted Not recorded" in shown["metrics"]
    assert "0 likes sent" not in shown["message"]


def verification_pending(row=2675):
    return {"source_row": row, "outcome": "verification_pending", "passed": False,
            "mutation_attempted": True, "mutation_accepted": True,
            "mutation_result": "accepted", "mutation_http_status": 200,
            "liked_before": False, "liked_after": None,
            "persisted_state_verified": False, "failed_phase": "state_after",
            "error_code": "state_read_failed"}


def pending_like_job():
    return {"id": "synthetic-pending-verification", "action": "like",
            "status": "completed_with_pending", "phase": "complete",
            "attempted": 3, "completed_tests": 3,
            "progress": {"completed": 1, "total": 3}, "succeeded": 1, "failed": 0,
            "connection_pending": 1, "verification_pending": 1, "results_total": 3,
            "results": [{**new_like(), "source_row": 1}, verification_pending(),
                        {"source_row": 3, "passed": False, "outcome": "connection_pending",
                         "mutation_attempted": False}]}


def test_real_ui_keeps_accepted_unverified_like_distinct_from_new_and_failed_likes():
    shown = browser_handler(render_job=pending_like_job())
    assert shown["progress"] == "3 / 3 checks finished"
    assert shown["status"] == "Completed with pending checks"
    assert shown["statusClass"] == "badge warning"
    assert "Checks passed 1" in shown["metrics"]
    assert "Like requests attempted 2" in shown["metrics"]
    assert "New likes verified 1" in shown["metrics"]
    assert "Other failures 0" in shown["metrics"]
    assert "Connection pending 1" in shown["metrics"]
    assert "Verification pending 1" in shown["metrics"]
    assert "1 connections pending; 1 accepted likes awaiting verification" in shown["message"]
    assert "All planned checks finished" in shown["message"]
    assert "The run stopped" not in shown["message"]
    assert "The server accepted the like, but its saved state could not be verified." in shown["results"]
    assert "The like was not sent again" in shown["results"]
    assert "Verification pending" in shown["results"]
    assert "Failed at State After" not in shown["results"]


@pytest.mark.parametrize("filter,expected,absent", [
    ("pending", ["Account 2675", "Account 3"], ["Account 1"]),
    ("verification_pending", ["Account 2675"], ["Account 1", "Account 3"]),
    ("failed", [], ["Account 1", "Account 2675", "Account 3"]),
    ("passed", ["Account 1"], ["Account 2675", "Account 3"]),
])
def test_real_ui_filters_verification_pending_without_calling_it_a_failure(filter, expected, absent):
    shown = browser_handler(job=pending_like_job(), filter=filter)
    assert all(label in shown["results"] for label in expected)
    assert all(label not in shown["results"] for label in absent)
