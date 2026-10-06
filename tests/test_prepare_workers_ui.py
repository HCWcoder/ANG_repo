"""Preparation worker controls exercise real UI handlers with mocked local jobs."""

import pytest

from test_prepare_country_ui import Controls, INDEX, browser


def test_preparation_worker_control_is_accessible_and_default_one_with_separate_limit():
    source = INDEX.read_text(encoding="utf-8")
    document = Controls()
    document.feed(source)
    control = document.controls["prepare-workers"]
    assert control["min"] == control["value"] == "1"
    assert "max" not in control and "disabled" in control
    assert control["aria-describedby"] == "prepare-workers-hint"
    assert '<label for="prepare-workers">Preparation workers</label>' in source
    assert "max" not in document.controls["test-workers"]


@pytest.mark.parametrize("workers", [1, 10, 16, 20, 21, 50, 1000, 9007199254740991])
@pytest.mark.parametrize("country", ["EG", "LB"])
@pytest.mark.parametrize("connection", ["direct", "egypt", "sticky"])
def test_http_country_preparation_sends_selected_workers_without_changing_route(workers, country, connection):
    result = browser(no_browser=True, workers=workers, country=country, connection=connection, submit=True)
    payload = result["calls"][0]["payload"]
    assert payload["action"] == "prepare" and payload["workers"] == workers
    assert payload["account_country"] == country and payload["no_browser"] is True
    assert payload["proxy_egypt"] is (connection != "direct")
    assert payload["proxy_sticky_pool"] is (connection == "sticky")
    assert result["before"]["workerDisabled"] is False
    assert result["before"]["workerMax"] is None


@pytest.mark.parametrize("workers", [0, -1, 9007199254740992, 1.5, "", "abc", "1e1"])
def test_invalid_http_preparation_workers_send_no_request(workers):
    result = browser(no_browser=True, workers=workers, submit=True)
    assert result["calls"] == []
    assert "Preparation workers must be a whole number" in result["error"]


def test_stale_server_fixed_preparation_limit_does_not_restore_a_worker_cap():
    result = browser(no_browser=True, workers=1000, worker_limit=16, submit=True)
    assert result["before"]["workerMax"] is None
    assert result["calls"][0]["payload"]["workers"] == 1000


def test_missing_server_preparation_limit_does_not_add_a_worker_cap():
    result = browser(no_browser=True, workers=1000, omit_worker_limit=True, submit=True)
    assert result["before"]["workerMax"] is None
    assert result["calls"][0]["payload"]["workers"] == 1000


@pytest.mark.parametrize("workers", [21, 50])
def test_above_twenty_workers_explains_stop_dispatch_and_drain(workers):
    result = browser(no_browser=True, workers=workers)
    assert "After 20 consecutive failures, no new accounts start; accounts already active finish." in result["before"]["workerHint"]


def test_worker_input_change_updates_drain_hint_without_sending_a_request():
    result = browser(no_browser=True, workers=1, changed_workers=50)
    assert "accounts already active finish" in result["before"]["workerHint"]
    assert result["before"]["workerValue"] == "50" and result["calls"] == []
    restored = browser(no_browser=True, workers=50, changed_workers=20)
    assert "accounts already active finish" not in restored["before"]["workerHint"]


@pytest.mark.parametrize("workers", [10, 50])
def test_browser_preparation_forces_one_and_omits_workers_from_payload(workers):
    result = browser(workers=workers, submit=True)
    assert result["before"]["workerValue"] == "1" and result["before"]["workerDisabled"] is True
    assert "Browser login prepares one account at a time" in result["before"]["workerHint"]
    assert "workers" not in result["calls"][0]["payload"]


@pytest.mark.parametrize("workers", [10, 50])
def test_any_country_row_order_forces_one_and_explains_country_requirement(workers):
    result = browser(no_browser=True, country="", workers=workers, submit=True)
    assert result["before"]["workerValue"] == "1" and result["before"]["workerDisabled"] is True
    assert "Choose Egypt or Lebanon" in result["before"]["workerHint"]
    assert result["calls"][0]["payload"]["workers"] == 1
    assert "account_country" not in result["calls"][0]["payload"]


def test_changing_to_row_order_disables_and_resets_workers():
    result = browser(no_browser=True, country="EG", workers=10, changed_country="", submit=True)
    assert result["before"]["workerDisabled"] is True and result["before"]["workerValue"] == "1"
    assert result["calls"][0]["payload"]["workers"] == 1


@pytest.mark.parametrize("country", ["EG", "LB"])
def test_changing_from_row_order_to_country_reenables_workers(country):
    result = browser(no_browser=True, country="", changed_country=country, submit=True)
    assert result["before"]["workerDisabled"] is False
    assert result["calls"][0]["payload"]["workers"] == 1


def test_switching_to_browser_login_resets_worker_count():
    result = browser(no_browser=True, workers=10, changed_method="browser", submit=True)
    assert result["before"]["workerDisabled"] is True and result["before"]["workerValue"] == "1"
    assert "workers" not in result["calls"][0]["payload"]


def test_switching_to_http_enables_country_preparation_workers():
    result = browser(workers=10, changed_method="http", submit=True)
    assert result["before"]["workerDisabled"] is False and result["before"]["workerValue"] == "1"
    assert result["calls"][0]["payload"]["workers"] == 1


@pytest.mark.parametrize("no_browser", [False, True])
def test_preview_omits_workers_even_when_http_preparation_has_a_parallel_count(no_browser):
    result = browser(action="preview", no_browser=no_browser, workers=10, submit=True)
    assert result["calls"][0]["payload"]["action"] == "preview"
    assert "workers" not in result["calls"][0]["payload"]


def test_invalid_workers_do_not_block_offline_selection_preview():
    result = browser(action="preview", no_browser=True, workers="", submit=True)
    assert result["calls"][0]["payload"]["action"] == "preview"
    assert "workers" not in result["calls"][0]["payload"]


@pytest.mark.parametrize("fields", [{"busy": True}, {"pending": True}, {"no_state": True}])
def test_worker_control_locks_during_job_request_and_initial_loading(fields):
    result = browser(no_browser=True, workers=10, **fields)
    assert result["before"]["workerDisabled"] is True


def test_state_refresh_keeps_chosen_workers_and_does_not_restore_any_worker_cap():
    result = browser(no_browser=True, workers=50, render=True)
    assert result["before"]["workerValue"] == "50" and result["before"]["workerMax"] is None
    assert result["before"]["workerDisabled"] is False
    assert result["before"]["testWorkerMax"] is None


def test_explicit_failed_review_still_omits_parallel_worker_option():
    result = browser(no_browser=True, workers=10, submit="review")
    payload = result["calls"][0]["payload"]
    assert payload["review_rows"] == [31] and "workers" not in payload


def test_login_session_refresh_still_omits_preparation_worker_option():
    result = browser(no_browser=True, workers=10, login_rows="7,8", submit="login")
    assert result["calls"][0]["payload"]["action"] == "login"
    assert "workers" not in result["calls"][0]["payload"]


def preparation_job(**fields):
    return {"action": "prepare", "status": "running", "phase": "preparing", "progress": {"completed": 25, "total": 1000},
            "connection_pending": 2, "account_failed": 0, "results": [], **fields}


@pytest.mark.parametrize("workers,active", [(10, 7), (50, 35), (1000, 70)])
def test_ongoing_status_shows_real_configured_and_active_worker_counts(workers, active):
    result = browser(no_browser=True, job=preparation_job(workers=workers, active_workers=active))
    assert f"{workers:,} workers" in result["before"]["jobMetrics"]
    assert f"{active:,} active" in result["before"]["jobMetrics"]
    assert "25 prepared" in result["before"]["jobMetrics"]


@pytest.mark.parametrize("fields", [{}, {"workers": 10}, {"workers": 10, "active_workers": None},
                                  {"workers": 10, "active_workers": 11},
                                  {"workers": 9007199254740992, "active_workers": 50}])
def test_ongoing_status_does_not_fabricate_missing_or_invalid_active_counts(fields):
    result = browser(no_browser=True, job=preparation_job(**fields))
    assert " active" not in result["before"]["jobMetrics"]


@pytest.mark.parametrize("held", [0, 2, 10])
def test_preparation_status_reports_known_held_count_without_counting_account_failure(held):
    result = browser(no_browser=True, job=preparation_job(workers=10, preparation_held=held))
    metrics = result["before"]["jobMetrics"]
    assert f"{held} held for review" in metrics
    assert "0 account failures" in metrics
    assert "10 workers" in metrics


@pytest.mark.parametrize("fields", [{}, {"preparation_held": None}, {"preparation_held": -1},
                                  {"preparation_held": "2"}, {"preparation_held": True},
                                  {"preparation_held": 1.5}])
def test_preparation_status_omits_unknown_or_invalid_held_counts(fields):
    result = browser(no_browser=True, job=preparation_job(**fields))
    assert "held for review" not in result["before"]["jobMetrics"]


@pytest.mark.parametrize("infrastructure", [0, 3])
@pytest.mark.parametrize("account_failed", [0, 2])
def test_preparation_status_reports_infrastructure_errors_separately_from_account_failures(infrastructure, account_failed):
    result = browser(no_browser=True, job=preparation_job(
        preparation_infrastructure_failed=infrastructure, account_failed=account_failed))
    metrics = result["before"]["jobMetrics"]
    assert f"{infrastructure} infrastructure errors" in metrics
    assert f"{account_failed} account failures" in metrics
    assert "2 connection pending" in metrics


@pytest.mark.parametrize("fields", [{}, {"preparation_infrastructure_failed": None},
                                  {"preparation_infrastructure_failed": -1},
                                  {"preparation_infrastructure_failed": "3"},
                                  {"preparation_infrastructure_failed": True},
                                  {"preparation_infrastructure_failed": 1.5},
                                  {"preparation_infrastructure_failed": 9007199254740992}])
def test_preparation_status_omits_missing_or_invalid_infrastructure_count(fields):
    result = browser(no_browser=True, job=preparation_job(**fields))
    metrics = result["before"]["jobMetrics"]
    assert "infrastructure errors" not in metrics
    assert "0 account failures" in metrics


def test_legacy_positive_preparation_error_count_is_not_called_account_failure():
    result = browser(no_browser=True, job=preparation_job(account_failed=3))
    metrics = result["before"]["jobMetrics"]
    assert "3 preparation errors" in metrics
    assert "account failures" not in metrics and "infrastructure errors" not in metrics


def test_classified_report_keeps_account_and_infrastructure_counts_explicit():
    result = browser(no_browser=True, job=preparation_job(
        account_failed=3, preparation_infrastructure_failed=2))
    metrics = result["before"]["jobMetrics"]
    assert "3 account failures" in metrics and "2 infrastructure errors" in metrics
    assert "preparation errors" not in metrics


def test_preparation_status_distinguishes_requested_effective_and_active_workers():
    result = browser(no_browser=True, job=preparation_job(
        workers=1000, requested_workers=1000, effective_workers=7, active_workers=3))
    metrics = result["before"]["jobMetrics"]
    assert "1,000 workers requested" in metrics
    assert "7 effective" in metrics and "3 active" in metrics


def test_preparation_status_never_invents_effective_workers_for_legacy_report():
    result = browser(no_browser=True, job=preparation_job(workers=1000, active_workers=3))
    assert "1,000 workers requested" in result["before"]["jobMetrics"]
    assert "effective" not in result["before"]["jobMetrics"]


def test_preparation_status_omits_active_count_above_actual_effective_workers():
    result = browser(no_browser=True, job=preparation_job(
        workers=1000, effective_workers=7, active_workers=8))
    assert "7 effective" in result["before"]["jobMetrics"]
    assert " active" not in result["before"]["jobMetrics"]
