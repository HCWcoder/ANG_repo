"""UI preparation accepts positive counts without changing test limits."""

import json

import pytest

from anghami_session import preparation, ui_jobs
from test_ui_jobs import finish, make_manager
from test_ui_server import console, request


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("count", [6, 100, 1000, 5000, 2**31, 2**53])
def test_preparation_job_validation_has_no_fixed_upper_count(action, count):
    options = ui_jobs._validate({"action": action, "count": count, "account_country": "EG"})
    assert options["count"] == count


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("count", [None, True, False, 0, -1, 1.5, "10", [], {}])
def test_preparation_count_still_requires_positive_whole_integer(tmp_path, action, count):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit({"action": action, "count": count})
    assert manager._thread is None and factories == loads == []


@pytest.mark.parametrize("country", ["EG", "LB"])
@pytest.mark.parametrize("action", ["prepare", "preview"])
def test_large_ui_job_preserves_requested_count_through_backend_and_progress(tmp_path, monkeypatch, country, action):
    seen = []
    rows = list(range(1001, 2001))
    def prepare(vault, **options):
        seen.append(options)
        if not options["dry_run"]:
            options["progress"]({"phase": "validation", "prepared_account_count": 777, "active_row": 1777})
            assert manager.snapshot()["progress"] == {"completed": 777, "total": 1000}
        return {"passed": True, "phase": "preview" if options["dry_run"] else "complete",
                "requested_accounts": 1000, "selected_rows": rows,
                "prepared_rows": [] if options["dry_run"] else rows,
                "prepared_account_count": 0 if options["dry_run"] else 1000,
                "account_country": country, "selection_mode": "random_country"}
    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    manager, *_ = make_manager(tmp_path)
    manager.submit({"action": action, "count": 1000, "account_country": country, "no_browser": True})
    job = finish(manager)
    assert seen[0]["count"] == job["count"] == 1000
    assert job["status"] == "succeeded" and job["progress"] == {"completed": 1000, "total": 1000}
    assert len(job["results"][0]["selected_rows"]) == 1000


@pytest.mark.parametrize("action", ["play", "like", "login", "check"])
def test_accounts_to_add_change_does_not_uncap_unrelated_actions(action):
    with pytest.raises(ui_jobs.JobValidationError):
        ui_jobs._validate({"action": action, "rows": [7], "count": 6})


def test_explicit_failed_review_keeps_its_separate_selection_limit():
    with pytest.raises(ui_jobs.JobValidationError):
        ui_jobs._validate({"action": "prepare", "count": 6, "review_rows": [7, 8, 9, 10, 11, 12]})


@pytest.mark.parametrize("action", ["prepare", "preview"])
def test_loopback_http_allows_large_normal_preparation_request(console, action):
    payload = {"action": action, "count": 5000, "no_browser": True, "account_country": "EG"}
    status, _headers, body = request(console, "POST", "/api/jobs", body=payload)
    assert status == 202 and json.loads(body)["job"]["status"] == "queued"
    assert console.service.manager.calls == [payload]
