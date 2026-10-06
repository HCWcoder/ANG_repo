"""Preparation worker controls are validated offline, separately from tests."""

import json

import pytest

from anghami_session import ui_jobs
from test_ui_server import console, request


@pytest.mark.parametrize("workers", [1, 2, 8, 10, 16, 50, 100, 2**53 - 1])
@pytest.mark.parametrize("country", ["EG", "LB"])
@pytest.mark.parametrize("route", [{}, {"proxy_egypt": True}, {"proxy_sticky_pool": True}])
def test_country_http_preparation_accepts_positive_safe_integer_workers(workers, country, route):
    options = ui_jobs._validate({
        "action": "prepare", "count": 100, "no_browser": True,
        "account_country": country, "workers": workers, **route,
    })
    assert options["workers"] == workers
    assert options["account_country"] == country and options["no_browser"] is True
    assert "max_consecutive_failures" not in options


@pytest.mark.parametrize("payload", [
    {"action": "prepare"},
    {"action": "prepare", "no_browser": True},
    {"action": "prepare", "account_country": "EG"},
    {"action": "prepare", "review_rows": [31], "count": 1},
])
def test_preparation_defaults_to_one_worker_without_changing_legacy_scope(payload):
    assert ui_jobs._validate(payload)["workers"] == 1


@pytest.mark.parametrize("workers", [None, True, False, 0, -1, 2**53, 50.0, "50", [], {}])
def test_invalid_preparation_worker_count_rejects_before_job_or_configuration(tmp_path, workers):
    calls = []
    manager = ui_jobs.JobManager(tmp_path / "synthetic.sqlite3", vault_factory=lambda path: calls.append(path))
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit({"action": "prepare", "no_browser": True, "account_country": "EG", "workers": workers})
    assert calls == [] and manager._thread is None and manager.snapshot() is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("workers", [2, 10, 16, 50])
@pytest.mark.parametrize("browser_choice", [{}, {"no_browser": False}])
def test_multiple_preparation_workers_cannot_start_browser_login(workers, browser_choice):
    with pytest.raises(ui_jobs.JobValidationError, match="browser-free"):
        ui_jobs._validate({"action": "prepare", "account_country": "EG", "workers": workers, **browser_choice})


@pytest.mark.parametrize("scope", [{}, {"start_row": 20}, {"review_rows": [31], "count": 1}])
def test_multiple_preparation_workers_require_registered_country_scope(scope):
    with pytest.raises(ui_jobs.JobValidationError, match="registered account country"):
        ui_jobs._validate({"action": "prepare", "no_browser": True, "workers": 10, **scope})


@pytest.mark.parametrize("action", ["preview", "login", "check", "song", "proxy-check"])
def test_preparation_concurrency_option_cannot_change_unrelated_action(action):
    with pytest.raises(ui_jobs.JobValidationError):
        ui_jobs._validate({"action": action, "rows": [7], "workers": 1})


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("workers", [9, 10, 50, 100, 2**53 - 1])
def test_play_and_like_accept_requested_workers_without_operating_cap(action, workers):
    assert ui_jobs._validate({"action": action, "rows": [7], "workers": workers})["workers"] == workers


def test_preparation_cannot_accept_test_failure_limit():
    with pytest.raises(ui_jobs.JobValidationError, match="failure limit only for play or like"):
        ui_jobs._validate({"action": "prepare", "no_browser": True, "workers": 10,
                           "account_country": "EG", "max_consecutive_failures": 20})


@pytest.mark.parametrize("workers", [10, 50, 100, 2**53 - 1])
def test_queued_preparation_persists_worker_count_without_starting_account_work(tmp_path, monkeypatch, workers):
    threads = []
    class QueuedThread:
        def __init__(self, *, target, args, name, daemon):
            self.target, self.args = target, args
            self.started = False
            threads.append(self)
        def start(self):
            self.started = True
    monkeypatch.setattr(ui_jobs.threading, "Thread", QueuedThread)
    manager = ui_jobs.JobManager(tmp_path / "synthetic.sqlite3", vault_factory=lambda _path: pytest.fail("Queued job opened account vault"))
    queued = manager.submit({"action": "prepare", "count": 100, "no_browser": True,
                             "account_country": "EG", "workers": workers})
    assert queued["status"] == "queued" and queued["workers"] == workers
    assert queued["active_workers"] == 0 and queued["active_rows"] == []
    assert queued["progress"] == {"completed": 0, "total": 100}
    assert manager.snapshot()["workers"] == workers
    assert json.loads(manager._report_path.read_text())["workers"] == workers
    assert threads[0].started and threads[0].args[0]["workers"] == workers


@pytest.mark.parametrize("workers", [10, 50, 100, 2**53 - 1])
def test_loopback_api_accepts_parallel_workers_for_country_http_preparation(console, workers):
    payload = {"action": "prepare", "count": 100, "no_browser": True,
               "account_country": "EG", "workers": workers, "proxy_egypt": True}
    status, _, _ = request(console, "POST", "/api/jobs", body=payload)
    assert status == 202 and console.service.manager.calls == [payload]


@pytest.mark.parametrize("payload", [
    {"action": "prepare", "workers": 10, "account_country": "EG"},
    {"action": "prepare", "workers": 10, "no_browser": True},
    {"action": "prepare", "workers": 2**53, "no_browser": True, "account_country": "EG"},
    {"action": "preview", "workers": 1, "no_browser": True, "account_country": "EG"},
])
def test_loopback_api_rejects_invalid_parallel_preparation_without_job(console, payload):
    status, _, _ = request(console, "POST", "/api/jobs", body=payload)
    assert status == 400 and console.service.manager.calls == []

