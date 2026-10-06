"""Ready-account country filters and requested worker counts stay local until submit."""

import pytest

from test_prepare_country_ui import Controls, INDEX, browser


READY = [1, 2, 3, 4, 5, 6, 7]
ACCOUNTS = [
    {"source_row": 1, "registered_country": "EG", "session_saved": True, "state": "ready"},
    {"source_row": 2, "registered_country": "LB", "session_saved": True, "state": "ready"},
    {"source_row": 3, "registered_country": "EG", "session_saved": True, "state": "ready"},
    {"source_row": 4, "registered_country": "LB", "session_saved": True, "state": "ready"},
    {"source_row": 5, "registered_country": "Other", "session_saved": True, "state": "ready"},
    {"source_row": 6, "session_saved": True, "state": "ready"},
    {"source_row": 7, "registered_country": "untrusted-country", "session_saved": True, "state": "ready"},
    {"source_row": 8, "registered_country": "EG", "session_saved": False, "state": "login_required"},
]


def workbench(**options):
    return browser(ready_rows=READY, ready_accounts=ACCOUNTS, test_account_limit=7,
                   render_picker=True, selected_rows=options.pop("selected_rows", []), **options)


def test_registered_country_filter_is_accessible_and_defaults_to_all():
    source = INDEX.read_text(encoding="utf-8")
    document = Controls()
    document.feed(source)
    control = document.controls["test-account-country"]
    assert control["aria-describedby"] == "test-account-country-hint"
    assert '<label for="test-account-country">Registered country for selected accounts</label>' in source
    assert '<option value="" selected>All countries</option><option value="EG">EG · Egypt</option><option value="LB">LB · Lebanon</option>' in source
    assert "max" not in document.controls["test-workers"]
    assert "max" not in document.controls["prepare-workers"]


@pytest.mark.parametrize("country,expected", [("", READY), ("EG", [1, 3]), ("LB", [2, 4])])
def test_filter_select_all_uses_only_matching_ready_registered_accounts(country, expected):
    result = workbench(test_country=country, selection_operations=[{"type": "all"}])
    before = result["before"]
    assert before["pickerRows"] == before["selectedRows"] == expected
    assert int(before["randomMax"]) == len(expected)
    assert "connection choice is independent" in before["testCountryHint"]
    assert "untrusted-country" not in before["cohortText"]
    assert "Row 1 · EG" in before["cohortText"] and "Row 2 · LB" in before["cohortText"]
    assert result["calls"] == []


@pytest.mark.parametrize("country,pool", [("EG", {1, 3}), ("LB", {2, 4})])
def test_random_selection_uses_exact_count_from_country_filtered_pool(country, pool):
    result = workbench(test_country=country, selection_operations=[{"type": "random", "count": 2}])
    selected = result["before"]["selectedRows"]
    assert len(selected) == len(set(selected)) == 2 and set(selected) == pool
    assert result["calls"] == []


def test_filter_change_removes_out_of_filter_selection_and_keeps_matching_rows():
    result = workbench(selected_rows=[1, 2, 3, 5], random_count=6,
                       selection_operations=[{"type": "country", "value": "EG"}])
    before = result["before"]
    assert before["selectedRows"] == before["pickerRows"] == [1, 3]
    assert before["randomValue"] == "2" and before["randomMax"] == "2"
    assert result["calls"] == []


def test_switching_between_countries_does_not_auto_select_another_account():
    result = workbench(test_country="EG", selected_rows=[1, 3],
                       selection_operations=[{"type": "country", "value": "LB"}])
    assert result["before"]["selectedRows"] == []
    assert result["before"]["pickerRows"] == [2, 4]


def test_manual_selection_and_clear_keep_country_scope():
    result = workbench(test_country="LB", selection_operations=[
        {"type": "manual", "row": 2, "checked": True},
        {"type": "manual", "row": 4, "checked": True},
        {"type": "manual", "row": 2, "checked": False}])
    assert result["before"]["selectedRows"] == [4]
    cleared = workbench(test_country="LB", selected_rows=[2, 4],
                        selection_operations=[{"type": "clear"}])
    assert cleared["before"]["selectedRows"] == []


@pytest.mark.parametrize("country", ["EG", "LB"])
@pytest.mark.parametrize("connection", ["direct", "egypt", "session"])
@pytest.mark.parametrize("action", ["play", "like"])
def test_country_filtered_action_payload_keeps_connection_independent(country, connection, action):
    result = workbench(test_country=country, test_connection=connection, test_action=action,
                       test_workers=1000, selection_operations=[{"type": "all"}], submit="test")
    payload = result["calls"][0]["payload"]
    assert payload["rows"] == ([1, 3] if country == "EG" else [2, 4])
    assert payload["workers"] == 1000 and payload["max_consecutive_failures"] == 20
    assert payload["proxy_egypt"] is (connection != "direct")
    assert payload.get("proxy_test_session", False) is (connection == "session")
    assert "account_country" not in payload and "registered_country" not in payload
    assert "up to 2 workers" in result["before"]["runSummary"]
    assert "tests already active finish" in result["before"]["testWorkerHint"]
    assert result["workbenchError"] == ""


@pytest.mark.parametrize("workers", [9, 51, 1000, 9007199254740991])
def test_requested_test_workers_above_old_caps_are_valid_and_effective_is_selected_count(workers):
    result = workbench(test_country="EG", test_workers=workers,
                       selected_rows=[1, 3], submit="test")
    assert result["calls"][0]["payload"]["workers"] == workers
    assert "up to 2 workers" in result["before"]["runSummary"]
    assert result["before"]["playDisabled"] is False


@pytest.mark.parametrize("workers", [0, -1, 1.5, "", "abc", "1e1", 9007199254740992])
def test_invalid_requested_workers_disable_actions_and_send_no_test(workers):
    result = workbench(test_country="EG", test_workers=workers,
                       selected_rows=[1, 3], submit="test")
    assert result["calls"] == []
    assert result["before"]["playDisabled"] is True and result["before"]["likeDisabled"] is True
    assert "whole number" in result["workbenchError"]


def test_new_filter_does_not_change_overview_ready_total_or_cohort_readiness():
    result = workbench(test_country="EG", render=True)
    assert result["before"]["globalReady"] == "7"
    assert result["before"]["cohortText"].count("Ready to test") == 7
    assert result["before"]["pickerRows"] == [1, 3]


@pytest.mark.parametrize("fields", [{"busy": True}, {"pending": True}, {"no_state": True}])
def test_country_filter_locks_with_existing_mutation_controls(fields):
    result = browser(**fields)
    assert result["before"]["testCountryDisabled"] is True


def test_live_action_overview_separates_requested_and_effective_concurrency():
    job = {"action": "like", "status": "running", "attempted": 2, "progress": {"completed": 0, "total": 7},
           "workers": 1000, "requested_workers": 1000, "effective_workers": 7, "active_workers": 3,
           "completed_tests": 0, "max_consecutive_failures": 20, "consecutive_failures": 0, "results": []}
    result = workbench(job=job)
    assert "Workers requested 1,000" in result["before"]["jobOverview"]
    assert "Workers active 3 / 7" in result["before"]["jobOverview"]


def test_legacy_action_overview_does_not_invent_effective_concurrency():
    job = {"action": "play", "status": "running", "attempted": 2, "progress": {"completed": 0, "total": 7},
           "workers": 1000, "active_workers": 3, "completed_tests": 0, "results": []}
    result = workbench(job=job)
    assert "Workers requested 1,000" in result["before"]["jobOverview"]
    assert "Workers active 3" in result["before"]["jobOverview"]
    assert "Workers active 3 / 1,000" not in result["before"]["jobOverview"]


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("failures", [21, 1000, 9007199254740991])
def test_failure_threshold_above_twenty_is_valid_after_render_and_keeps_other_controls(action, failures):
    result = workbench(test_country="EG", selected_rows=[1, 3], test_action=action,
                       test_workers=1001, failures=failures, test_count=2, render=True, submit="test")
    payload = result["calls"][0]["payload"]
    assert payload["max_consecutive_failures"] == failures
    assert payload["count"] == 2 and payload["workers"] == 1001
    assert payload["rows"] == [1, 3]
    assert result["before"]["failureMax"] is None
    assert result["before"]["failureValue"] == str(failures)
    assert result["before"]["playDisabled"] is False and result["before"]["likeDisabled"] is False


@pytest.mark.parametrize("failures", [0, -1, 1.5, "", "abc", "1e3", 9007199254740992])
def test_invalid_failure_threshold_disables_actions_without_sending_a_test(failures):
    result = workbench(test_country="EG", selected_rows=[1, 3], failures=failures, submit="test")
    assert result["calls"] == []
    assert result["before"]["playDisabled"] is True and result["before"]["likeDisabled"] is True
    assert "whole number" in result["workbenchError"]


def test_failure_threshold_input_updates_drain_hint_to_the_chosen_threshold():
    result = workbench(test_workers=1001, changed_failures=1000)
    assert "After 1000 consecutive failures, no new tests start; tests already active finish" in result["before"]["testWorkerHint"]
    assert "After 20 consecutive failures" not in result["before"]["testWorkerHint"]
    assert result["calls"] == []
    equal_workers = workbench(test_workers=1000, changed_failures=1000)
    assert "tests already active finish" not in equal_workers["before"]["testWorkerHint"]


def test_failure_threshold_default_remains_twenty():
    result = workbench(selected_rows=[1], submit="test")
    assert result["before"]["failureValue"] == "20"
    assert result["calls"][0]["payload"]["max_consecutive_failures"] == 20
