"""Remaining-account controls exercise real UI with synthetic local requests only."""

import pytest

from test_prepare_country_ui import Controls, INDEX, browser


def availability(eg=120, lb=7, total=140, *, start_row=1):
    return {"available": True, "counts": {"EG": eg, "LB": lb, "all": total}, "start_row": start_row}


def test_count_keeps_accessible_minimum_one_and_waits_for_server_availability():
    document = Controls()
    document.feed(INDEX.read_text(encoding="utf-8"))
    assert document.controls["prepare-count"]["min"] == "1"
    assert "max" not in document.controls["prepare-count"]
    assert document.controls["prepare-count"]["aria-describedby"] == "prepare-count-hint"


@pytest.mark.parametrize("country,maximum", [("EG", 120), ("LB", 7), ("", 140)])
def test_country_selection_shows_exact_remaining_unique_count_and_input_maximum(country, maximum):
    result = browser(country=country, start_row="", availability=availability())
    assert result["before"]["prepareMax"] == str(maximum)
    assert f"{maximum} unique" in result["before"]["prepareCountHint"]
    assert f"maximum {maximum}" in result["before"]["prepareCountHint"]
    assert result["before"]["prepareCountDisabled"] is False
    assert result["before"]["previewDisabled"] is False
    assert result["before"]["prepareDisabled"] is False
    assert result["calls"] == result["availabilityCalls"] == []


def test_country_change_clamps_previous_count_without_changing_method_route_or_start_row():
    result = browser(country="EG", changed_country="LB", count=100, connection="sticky", no_browser=True,
                     workers=8, availability=availability())
    assert result["before"]["prepareCount"] == "7"
    assert result["before"]["prepareMax"] == "7"
    assert result["before"]["country"] == "LB"
    assert result["before"]["connection"] == "sticky"
    assert result["before"]["startRow"] == "777"
    assert result["before"]["workerValue"] == "8"
    assert result["calls"] == []


def test_country_with_more_accounts_preserves_the_entered_in_range_count():
    result = browser(country="LB", changed_country="EG", count=5, availability=availability())
    assert result["before"]["prepareCount"] == "5"
    assert result["before"]["prepareMax"] == "120"


@pytest.mark.parametrize("action", ["prepare", "preview"])
def test_no_remaining_accounts_disables_count_and_actions_without_minimum_maximum_conflict(action):
    result = browser(availability=availability(eg=0), action=action, submit=True)
    assert result["before"]["prepareMax"] is None
    assert result["before"]["prepareCountDisabled"] is True
    assert result["before"]["previewDisabled"] is True
    assert result["before"]["prepareDisabled"] is True
    assert "No eligible EG accounts" in result["before"]["prepareCountHint"]
    assert "No eligible accounts" in result["error"]
    assert result["calls"] == []


@pytest.mark.parametrize("fields", [
    {"omit_availability": True},
    {"availability": {"available": False}},
    {"availability": {**availability(), "available": "true"}},
    {"availability": availability(eg=-1)},
    {"availability": availability(eg=1.5)},
    {"availability": availability(eg=True)},
    {"availability": availability(eg="120")},
    {"availability": availability(eg=9007199254740992)},
    {"availability": {**availability(), "start_row": 3}},
    {"availability": {"available": True, "counts": {"EG": 120}, "start_row": 1}},
])
def test_unavailable_or_malformed_counts_fail_closed_without_claiming_zero(fields):
    result = browser(submit=True, **fields)
    assert result["before"]["prepareCountDisabled"] is True
    assert result["before"]["previewDisabled"] is True
    assert result["before"]["prepareDisabled"] is True
    assert result["before"]["prepareMax"] is None
    assert "unavailable" in result["before"]["prepareCountHint"]
    assert "No eligible" not in result["before"]["prepareCountHint"]
    assert result["calls"] == []


def test_refreshed_post_preparation_count_decreases_maximum_and_clamps_previous_count():
    result = browser(count=100, availability=availability(), updated_availability=availability(eg=4), render=True)
    assert result["before"]["prepareMax"] == result["before"]["prepareCount"] == "4"
    assert "4 unique EG accounts left" in result["before"]["prepareCountHint"]
    assert result["calls"] == []


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("country", ["EG", "LB"])
def test_count_over_current_available_maximum_is_rejected_before_submission(action, country):
    result = browser(country=country, availability=availability(eg=3, lb=3), changed_prepare_count=4,
                     action=action, submit=True)
    assert result["before"]["prepareCount"] == "4"
    assert result["before"]["prepareMax"] == "3"
    assert result["calls"] == []
    assert "whole number between 1 and 3" in result["error"]


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "1e2", "abc"])
def test_invalid_count_is_not_silently_replaced_or_submitted(value):
    result = browser(availability=availability(), changed_prepare_count=value, submit=True)
    assert result["before"]["prepareCount"] == value
    assert result["calls"] == []
    assert "Accounts to add must be a whole number" in result["error"]


@pytest.mark.parametrize("action", ["prepare", "preview"])
def test_minimum_one_and_exact_remaining_maximum_are_accepted(action):
    result = browser(availability=availability(eg=1), count=1, action=action, submit=True)
    assert result["calls"][0]["payload"]["count"] == 1
    assert result["calls"][0]["payload"]["account_country"] == "EG"


@pytest.mark.parametrize("fields", [{"busy": True}, {"pending": True}, {"no_state": True}, {"lock": True}])
def test_busy_pending_and_initial_load_keep_preparation_controls_locked(fields):
    result = browser(availability=availability(), **fields)
    assert result["before"]["prepareCountDisabled"] is True
    assert result["before"]["previewDisabled"] is True
    assert result["before"]["prepareDisabled"] is True


def test_missing_proxy_keeps_prepare_disabled_but_allows_offline_preview():
    result = browser(availability=availability(), connection="egypt", proxy_configured=False,
                     action="preview", submit=True)
    assert result["before"]["prepareDisabled"] is True
    assert result["before"]["previewDisabled"] is False
    assert result["calls"][0]["payload"]["action"] == "preview"


def test_no_new_accounts_does_not_block_explicit_failed_account_review():
    result = browser(availability=availability(eg=0), submit="review")
    payload = result["calls"][0]["payload"]
    assert payload["review_rows"] == [31]
    assert "account_country" not in payload


def test_no_new_accounts_does_not_block_selected_ready_play_accounts():
    result = browser(availability=availability(eg=0), ready_rows=[8], selected_rows=[8], submit="test")
    assert result["calls"][0]["payload"]["rows"] == [8]
    assert result["calls"][0]["payload"]["action"] == "play"


def test_row_order_uses_read_only_offset_count_and_preserves_selected_starting_row():
    result = browser(country="", start_row=777, availability=availability(),
                     offset_availability=availability(total=6, start_row=777), count=10, submit=True)
    assert result["availabilityCalls"] == ["/api/preparation-availability?start_row=777"]
    assert result["before"]["prepareMax"] == result["before"]["prepareCount"] == "6"
    assert "from row 777" in result["before"]["prepareCountHint"]
    payload = result["calls"][0]["payload"]
    assert payload["count"] == 6 and payload["start_row"] == 777
    assert "account_country" not in payload


def test_row_order_offset_lookup_temporarily_disables_submission():
    result = browser(country="", start_row=777, availability=availability(), before_availability_response=True)
    assert result["before"]["prepareCountDisabled"] is True
    assert result["before"]["previewDisabled"] is True
    assert result["before"]["prepareDisabled"] is True
    assert "Checking" in result["before"]["prepareCountHint"]
    assert result["calls"] == []


@pytest.mark.parametrize("options", [
    {"availability_error": True},
    {"offset_availability": availability(total=6, start_row=778)},
    {"offset_availability": {"available": False, "start_row": 777}},
])
def test_unavailable_or_wrong_offset_response_cannot_enable_preparation(options):
    result = browser(country="", start_row=777, availability=availability(), submit=True, **options)
    assert result["before"]["prepareCountDisabled"] is True
    assert result["calls"] == []
    assert "unavailable" in result["before"]["prepareCountHint"]


def test_zero_row_offset_count_disables_submission_even_if_other_rows_remain():
    result = browser(country="", start_row=777, availability=availability(),
                     offset_availability=availability(total=0, start_row=777), submit=True)
    assert "No eligible accounts" in result["before"]["prepareCountHint"]
    assert "from row 777" in result["before"]["prepareCountHint"]
    assert result["calls"] == []


def test_late_row_offset_response_cannot_replace_new_country_count():
    result = browser(country="", start_row=777, availability=availability(), changed_country="EG",
                     offset_availability=availability(total=1, start_row=777), count=100)
    assert result["before"]["prepareMax"] == "120"
    assert result["before"]["prepareCount"] == "100"
    assert "120 unique EG accounts" in result["before"]["prepareCountHint"]


def test_late_previous_row_response_cannot_replace_current_row_count():
    result = browser(country="", start_row=7, availability=availability(), changed_start_row=9,
                     offset_availabilities={"7": availability(total=3, start_row=7), "9": availability(total=8, start_row=9)}, count=10)
    assert result["availabilityCalls"] == ["/api/preparation-availability?start_row=7", "/api/preparation-availability?start_row=9"]
    assert result["before"]["prepareMax"] == result["before"]["prepareCount"] == "8"
    assert "from row 9" in result["before"]["prepareCountHint"]


def test_returning_to_previous_row_after_interrupted_lookup_fetches_again():
    result = browser(country="", start_row=7, availability=availability(),
                     offset_availability=availability(total=8, start_row=7),
                     preparation_operations=[{"type": "country", "value": "EG"}, {"type": "country", "value": ""}])
    assert result["availabilityCalls"] == ["/api/preparation-availability?start_row=7"] * 2
    assert result["before"]["prepareMax"] == "8"


@pytest.mark.parametrize("row", ["-1", "0", "1.5", "1e2", "abc"])
def test_invalid_starting_row_cannot_use_global_count_for_submission(row):
    result = browser(country="", start_row=row, availability=availability(), submit=True)
    assert result["availabilityCalls"] == result["calls"] == []
    assert result["before"]["prepareCountDisabled"] is True
    assert "valid starting row" in result["before"]["prepareCountHint"]

