"""Preparation counts use actual availability and mocked handlers, never real jobs."""

import pytest

from test_prepare_country_ui import Controls, INDEX, browser


def test_accounts_to_add_has_minimum_one_and_waits_for_dynamic_availability():
    document = Controls()
    source = INDEX.read_text(encoding="utf-8")
    document.feed(source)
    control = document.controls["prepare-count"]
    assert control["min"] == "1" and "max" not in control
    assert control["aria-describedby"] == "prepare-count-hint"
    assert "Checking how many accounts are left to prepare" in source


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("count", [6, 1000, 5000])
@pytest.mark.parametrize("country", ["EG", "LB", ""])
@pytest.mark.parametrize("connection", ["direct", "egypt", "sticky"])
def test_prepare_and_preview_accept_large_counts_without_country_or_connection_changes(action, count, country, connection):
    result = browser(action=action, count=count, country=country, connection=connection,
                     no_browser=connection == "sticky", submit=True)
    assert len(result["calls"]) == 1
    payload = result["calls"][0]["payload"]
    assert payload["action"] == action and payload["count"] == count
    assert payload["proxy_egypt"] is (connection != "direct")
    assert payload["proxy_sticky_pool"] is (connection == "sticky")
    if country:
        assert payload["account_country"] == country and "start_row" not in payload
    else:
        assert "account_country" not in payload and payload["start_row"] == 777
    assert result["error"] == ""


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "abc", "NaN", "Infinity", "1e3", "9007199254740992"])
def test_preparation_count_rejects_invalid_or_unsafe_values_before_submission(action, value):
    result = browser(action=action, count=value, submit=True)
    assert result["calls"] == []
    assert "Accounts to add must be a whole number" in result["error"]


@pytest.mark.parametrize("value", [1, 9007199254740991])
def test_preparation_accepts_minimum_and_largest_exact_safe_integer(value):
    result = browser(count=value, submit=True)
    assert result["calls"][0]["payload"]["count"] == value


def test_state_refresh_uses_availability_maximum_and_preserves_in_range_count():
    result = browser(count=5000, render=True)
    assert result["before"]["prepareMax"] == "9007199254740991"
    assert result["before"]["prepareCount"] == "5000"
    assert result["before"]["testMax"] == "5"
    assert result["calls"] == []


def test_explicit_failed_account_review_keeps_its_five_account_limit():
    result = browser(count=5000, review_rows=list(range(31, 37)), submit="review")
    assert result["calls"] == []
    assert "Choose 1–5 accounts" in result["reviewError"]


def test_session_refresh_keeps_its_five_account_limit():
    result = browser(count=5000, login_rows="1,2,3,4,5,6", submit="login")
    assert result["calls"] == []
    assert "Select up to 5 rows" in result["loginError"]
