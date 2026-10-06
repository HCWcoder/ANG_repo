"""Country preparation request wiring uses synthetic data and no account traffic."""

import json

import pytest

from anghami_session import preparation, ui_jobs, ui_server
from test_ui_jobs import FakeVault, finish, make_manager
from test_ui_server import console, request


@pytest.mark.parametrize("country", ["EG", "LB"])
@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("proxy_egypt", [False, True])
@pytest.mark.parametrize("no_browser", [False, True])
def test_jobs_forward_registered_country_independently_of_connection_and_method(
        tmp_path, monkeypatch, country, action, proxy_egypt, no_browser):
    calls = []
    def prepare(vault, **options):
        calls.append(options)
        return {"passed": True, "phase": "complete" if not options["dry_run"] else "preview",
                "selected_rows": [21, 27], "prepared_rows": [] if options["dry_run"] else [21, 27],
                "prepared_account_count": 0 if options["dry_run"] else 2,
                "account_country": options["account_country"], "selection_mode": "random_country"}
    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    manager, _factories, proxy, loads = make_manager(tmp_path)
    manager.submit({"action": action, "count": 2, "account_country": country,
                    "proxy_egypt": proxy_egypt, "no_browser": no_browser})
    job = finish(manager)
    assert job["status"] == "succeeded" and len(calls) == 1
    assert calls[0]["account_country"] == job["account_country"] == country
    assert calls[0].get("no_browser", False) is no_browser and calls[0]["start_row"] == 1
    assert calls[0]["proxy"] is (proxy if proxy_egypt and action == "prepare" else None)
    assert bool(loads) is (proxy_egypt and action == "prepare")
    assert job["selection_mode"] == "random_country"
    assert job["results"][0]["selected_rows"] == [21, 27]
    safe = ui_server.safe_report(job)
    assert safe["account_country"] == safe["results"][0]["account_country"] == country


@pytest.mark.parametrize("country", [None, "", "US", "eg", "lb", " EG", True, 1, [], {}])
def test_invalid_country_never_starts_worker_or_loads_credentials(tmp_path, country):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit({"action": "prepare", "account_country": country})
    assert manager._thread is None and factories == loads == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("payload", [
    {"action": "like", "rows": [7], "account_country": "EG"},
    {"action": "check", "rows": [7], "account_country": "LB"},
    {"action": "login", "rows": [7], "account_country": "LB"},
    {"action": "prepare", "account_country": "EG", "start_row": 1},
    {"action": "preview", "account_country": "LB", "start_row": 17},
    {"action": "prepare", "account_country": "EG", "review_rows": [7], "count": 1},
])
def test_random_country_is_limited_to_normal_preparation_mode(tmp_path, payload):
    manager, factories, _proxy, loads = make_manager(tmp_path)
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit(payload)
    assert manager._thread is None and factories == loads == []


@pytest.mark.parametrize("payload,expected", [
    ({"action": "preview", "start_row": 15}, {"start_row": 15}),
    ({"action": "prepare", "review_rows": [7]}, {"selected_rows": [7]}),
])
def test_legacy_row_order_and_explicit_review_never_receive_country_filter(tmp_path, monkeypatch, payload, expected):
    calls = []
    monkeypatch.setattr(FakeVault, "failure_review", lambda self: {"failed_rows": [7]}, raising=False)
    def prepare(vault, **options):
        calls.append(options)
        return {"passed": True, "phase": "complete", "prepared_account_count": 1}
    monkeypatch.setattr(preparation, "prepare_test_accounts", prepare)
    manager, *_ = make_manager(tmp_path)
    manager.submit(payload)
    job = finish(manager)
    assert job["status"] == "succeeded" and "account_country" not in job
    assert "account_country" not in calls[0]
    for name, value in expected.items():
        assert calls[0][name] == value


@pytest.mark.parametrize("field,good,bad", [
    ("account_country", "LB", "synthetic-private-value"),
    ("selection_mode", "random_country", "synthetic-private-value"),
])
def test_country_selection_report_facts_use_strict_allowlists(field, good, bad):
    assert ui_jobs._public_report({field: good}) == {field: good}
    assert ui_jobs._public_report({field: bad}) == {}
    assert ui_server.safe_report({field: good}) == {field: good}
    assert ui_server.safe_report({field: bad}) == {field: None}


@pytest.mark.parametrize("country", ["EG", "LB"])
def test_loopback_http_accepts_country_preparation_payload(console, country):
    payload = {"action": "preview", "count": 2, "account_country": country, "no_browser": True}
    status, _headers, body = request(console, "POST", "/api/jobs", body=payload)
    assert status == 202
    assert console.service.manager.calls == [payload]
    assert json.loads(body)["job"]["status"] == "queued"


def test_loopback_http_rejects_country_filter_on_engagement_action(console):
    status, _headers, _body = request(console, "POST", "/api/jobs", body={
        "action": "like", "rows": [7], "account_country": "EG"})
    assert status == 400 and console.service.manager.calls == []
