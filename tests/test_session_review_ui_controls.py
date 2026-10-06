"""Saved-session review UI regressions with mocked local requests only.

This file is deliberately separate from the backend quarantine contract tests.
The shared Node driver executes the complete application and real handlers;
its fetch implementation records synthetic requests without any network I/O.
"""

from html.parser import HTMLParser
import json

import pytest

from test_prepare_country_ui import INDEX, browser


STAMP = "2026-10-04T10:00:00+00:00"


def held_account(row, **changes):
    return {
        "source_row": row,
        "state": "session_review_pending",
        "failure_code": "request_transport_failed",
        "failed_stage": "identity",
        "held_at": STAMP,
        "job_id": "a" * 32,
        "session_failure": {
            "code": "request_transport_failed", "stage": "identity",
            "failure_category": "provider", "curl_code": 28,
        },
        **changes,
    }


def session_review(rows=(175, 197, 208), **changes):
    return {
        "accounts": [held_account(row) for row in rows], "total": len(rows),
        "held_rows": list(rows), "session_review_rows": list(rows), **changes,
    }


def ready_account(row):
    return {"source_row": row, "state": "ready", "session_saved": True, "registered_country": "EG"}


def pending_job(**changes):
    return {
        "id": "fixture-job", "action": "play", "status": "completed_with_pending",
        "phase": "completed", "progress": {"completed": 2, "total": 2},
        "count": 1, "attempted": 2, "completed_tests": 2, "succeeded": 1,
        "failed": 0, "account_failed": 0, "session_review_pending": 1,
        "connection_pending": 0, "verification_pending": 0, "skipped": 0,
        "workers": 8, "requested_workers": 8, "effective_workers": 2,
        "active_workers": 0, "active_rows": [], "consecutive_failures": 0,
        "max_consecutive_failures": 20, "provider_retries": 0,
        "writes_attempted": 1, "writes_accepted": 1,
        "new_likes_verified": 0, "already_liked_verified": 0,
        "stop_reason": "completed_with_pending", "elapsed_seconds": 1,
        "results": [
            {"source_row": 175, "test_number": 1, "outcome": "session_review_pending", "passed": False,
             "event_attempted": False, "event_attempts": 0, "event_accepted": False,
             "event_result": "not_attempted", "renewal_attempted": True, "renewal_completed": False},
            {"source_row": 1, "test_number": 1, "passed": True, "event_accepted": True,
             "event_result": "accepted"},
        ],
        **changes,
    }


class ReviewDocument(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = {}
        self.current_select = None
        self.review_options = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.controls[attrs["id"]] = attrs
        if tag == "select":
            self.current_select = attrs.get("id")
        elif tag == "option" and self.current_select == "session-review-connection-mode":
            self.review_options.append(attrs["value"])

    def handle_endtag(self, tag):
        if tag == "select":
            self.current_select = None


def test_panel_names_the_read_only_action_separate_from_login_and_confirmed_account_failures():
    source = INDEX.read_text(encoding="utf-8")
    document = ReviewDocument()
    document.feed(source)
    assert "Session review</h2>" in source
    assert "Check saved sessions (read only)</button>" in source
    assert document.review_options == ["direct", "egypt"]
    assert document.controls["session-review-connection-mode"]["aria-describedby"] == "session-review-proxy-mode-hint"
    assert document.controls["session-review-error"]["role"] == "alert"
    assert "No login, session renewal, play or like request is sent" in source
    assert "unsuccessful checks keep the account here" in source
    assert "Session holds do not advance or reset the failure streak" in source
    assert "Linked rows stay excluded while another saved session for the same account remains held" in source


@pytest.mark.parametrize("connection", ["direct", "egypt"])
@pytest.mark.parametrize("other_connection", ["direct", "sticky"])
def test_read_only_submission_uses_its_own_connection_and_exact_selected_rows(connection, other_connection):
    result = browser(
        session_review=session_review(), render_session_review=True, session_selected_rows=[208, 175],
        session_review_connection=connection, connection=other_connection, test_connection=other_connection,
        workers=999, test_workers=999, failures=999, count=1000, test_count=5,
        submit="session-review",
    )
    assert result["sessionReviewError"] == ""
    assert result["calls"] == [{
        "path": "/api/jobs",
        "payload": {"action": "review-sessions", "rows": [175, 208], "proxy_egypt": connection == "egypt"},
    }]
    assert "session renewal" not in json.dumps(result["calls"])


@pytest.mark.parametrize("rows", [[], [1, 2, 3, 4, 5, 6], [999]], ids=["empty", "over_five", "not_held"])
def test_empty_oversized_or_stale_review_selection_never_submits(rows):
    result = browser(
        session_review=session_review(range(1, 7)), render_session_review=True,
        session_selected_rows=rows, submit="session-review",
    )
    assert result["calls"] == []
    assert result["sessionReviewButtonDisabled"] is True
    assert "Choose 1–5 held accounts" in result["sessionReviewError"]


@pytest.mark.parametrize("connection,configured", [("sticky", True), ("session", True), ("egypt", False)])
def test_review_rejects_pasted_test_routes_and_unconfigured_egypt_proxy(connection, configured):
    result = browser(
        session_review=session_review(), render_session_review=True, session_selected_rows=[175],
        session_review_connection=connection, proxy_configured=configured, submit="session-review",
    )
    assert result["calls"] == []
    assert result["sessionReviewError"]
    if connection != "egypt":
        assert "Choose Direct or Egypt" in result["sessionReviewError"]
    else:
        assert result["sessionReviewButtonDisabled"] is True


@pytest.mark.parametrize("lock", [{"busy": True}, {"pending": True}, {"no_state": True}])
def test_busy_pending_and_initial_state_lock_review_actions_without_a_request(lock):
    result = browser(
        session_review=session_review(), render_session_review=True, session_selected_rows=[175],
        session_operations=[{"type": "all"}], submit="session-review", **lock,
    )
    assert result["calls"] == []
    assert result["sessionReviewButtonDisabled"] is True
    assert all(box["disabled"] for box in result["sessionReviewBoxes"])
    assert result["sessionReviewError"] == ""


def test_select_all_picks_only_five_held_entries_and_disables_additional_checkboxes():
    result = browser(
        session_review=session_review(range(1, 8)), render_session_review=True,
        session_operations=[{"type": "all"}],
    )
    assert result["sessionReviewSelected"] == [1, 2, 3, 4, 5]
    assert result["sessionReviewSelectionText"] == "5 selected · up to 5 per check"
    assert result["sessionReviewCount"] == "7 accounts"
    assert [box["row"] for box in result["sessionReviewBoxes"] if box["disabled"]] == [6, 7]
    assert result["sessionReviewButtonDisabled"] is False
    assert result["calls"] == []


def test_manual_selection_enforces_five_even_when_a_disabled_checkbox_handler_is_called():
    result = browser(
        session_review=session_review(range(1, 7)), render_session_review=True,
        session_operations=[{"type": "manual", "row": row, "checked": True} for row in range(1, 7)],
    )
    assert result["sessionReviewSelected"] == [1, 2, 3, 4, 5]
    assert result["sessionReviewBoxes"][-1] == {"row": 6, "checked": False, "disabled": True}
    assert "Choose up to 5 held accounts" in result["sessionReviewError"]


def test_clear_removes_review_selection_without_preparing_or_checking_any_session():
    result = browser(
        session_review=session_review(), render_session_review=True,
        session_operations=[{"type": "all"}, {"type": "clear"}],
    )
    assert result["sessionReviewSelected"] == []
    assert result["sessionReviewButtonDisabled"] is True
    assert not any(box["checked"] for box in result["sessionReviewBoxes"])
    assert result["calls"] == []


@pytest.mark.parametrize("action", ["play", "like"])
def test_held_accounts_are_removed_from_ready_and_refresh_pickers_before_test_selection(action):
    rows = [1, 175, 197, 208]
    before_submission = browser(
        session_review=session_review(), ready_rows=rows, ready_accounts=[ready_account(row) for row in rows],
        selected_rows=rows, render_picker=True,
    )
    assert {box["row"] for box in before_submission["cohortBoxes"] if box["disabled"]} == {175, 197, 208}
    result = browser(
        session_review=session_review(), ready_rows=rows, ready_accounts=[ready_account(row) for row in rows],
        selected_rows=rows, render_picker=True, submit="test", test_action=action,
    )
    assert result["before"]["pickerRows"] == [1]
    assert result["before"]["selectedRows"] == [1]
    assert result["calls"][0]["payload"]["rows"] == [1]
    assert result["calls"][0]["payload"]["action"] == action
    assert all(box["disabled"] for box in result["cohortBoxes"])
    assert result["workbenchError"] == ""


def test_manually_entering_a_held_row_for_normal_login_is_blocked_without_renewal():
    result = browser(session_review=session_review(), login_rows="1, 175", submit="login")
    assert result["calls"] == []
    assert "Use Check saved sessions in Session review" in result["loginError"]
    assert "A new login is not part of that check" in result["loginError"]


def test_session_holds_do_not_enter_failed_account_preparation_even_with_stale_review_data():
    result = browser(
        session_review=session_review([31]), review_rows=[31, 32], render_picker=True, submit="review",
    )
    assert "Row 31" not in result["failureReviewText"]
    assert "Row 32" in result["failureReviewText"]
    assert "Row 31" in result["sessionReviewText"]
    assert result["calls"] == []
    assert "failed-account review list" in result["reviewError"]


def test_panel_renders_only_typed_diagnostics_and_never_private_fields_or_malformed_values():
    secret = "fixture-private-never-display"
    accounts = [
        held_account(175, email=secret, cookies=secret, sid=secret, route=secret),
        held_account(197, failure_code=secret, failed_stage=secret, held_at=secret,
                     session_failure={"http_status": secret, "curl_code": True}),
        held_account(208, session_failure={"http_status": 429, "curl_code": 7}),
        held_account("209"), held_account(0), held_account(True), held_account(2**31),
        held_account(175),
    ]
    result = browser(
        session_review=session_review(accounts=accounts, total=3, session_review_rows=[175, 197, 208]),
        render_session_review=True,
    )
    assert [box["row"] for box in result["sessionReviewBoxes"]] == [175, 197, 208]
    assert "Request Transport Failed" in result["sessionReviewText"]
    assert "Identity" in result["sessionReviewText"]
    assert "Curl 28" in result["sessionReviewText"]
    assert "HTTP 429" in result["sessionReviewText"] and "Curl 7" in result["sessionReviewText"]
    assert "Saved session needs checking" in result["sessionReviewText"]
    assert secret not in json.dumps(result)


@pytest.mark.parametrize("filter_name", ["all", "pending", "failed"])
def test_session_pending_results_and_counters_are_separate_from_confirmed_account_failures(filter_name):
    result = browser(job=pending_job(), result_filter=filter_name)
    assert "Session review pending 1" in result["before"]["jobOverview"]
    assert "Account failures 0" in result["before"]["jobOverview"]
    assert "Other failures 0" in result["before"]["jobOverview"]
    assert "Failure streak 0 / 20" in result["before"]["jobOverview"]
    assert result["jobStatus"] == "Completed with pending checks"
    if filter_name == "failed":
        assert "Account 175" not in result["jobResults"]
    else:
        assert "Account 175" in result["jobResults"]
        assert "Session review pending" in result["jobResults"]
        assert "no play or like request was sent. Other accounts continued" in result["jobResults"]
    if filter_name == "all":
        specific = browser(job=pending_job(), result_filter="session_review_pending")
        assert "Account 175" in specific["jobResults"] and "Account 1 ·" not in specific["jobResults"]


def test_successful_read_only_review_result_describes_the_cleared_hold_without_claiming_a_login():
    job = {
        "id": "fixture-review-job", "action": "review-sessions", "status": "succeeded",
        "phase": "completed", "progress": {"completed": 1, "total": 1},
        "session_reviews_cleared": 1,
        "results": [{"source_row": 175, "passed": True, "session_review_cleared": True, "cleared_rows": [175]}],
    }
    result = browser(job=job)
    assert "saved session passed read-only checks" in result["jobResults"]
    assert "Its hold was cleared; no login, play or like request was sent" in result["jobResults"]
    assert result["jobStatus"] == "Completed"
    assert result["calls"] == []


def test_state_refresh_clears_a_verified_hold_and_restores_ready_without_auto_selecting_or_running_it():
    result = browser(
        session_review=session_review([175]), ready_rows=[1, 175],
        ready_accounts=[ready_account(1), ready_account(175)], render_picker=True,
        session_selected_rows=[175],
        updated_session_review=session_review([]), updated_ready_rows=[1, 175],
    )
    assert result["before"]["pickerRows"] == [1, 175]
    assert result["before"]["selectedRows"] == [1]
    assert result["sessionReviewSelected"] == [] and result["sessionReviewCount"] == "0 accounts"
    assert result["sessionReviewButtonDisabled"] is True
    assert not any(box["disabled"] for box in result["cohortBoxes"])
    assert result["calls"] == []


def test_review_selection_uses_explicit_entries_not_every_blocked_identity_alias():
    result = browser(
        session_review=session_review(
            [175, 197], held_rows=[175, 197, 208], session_review_rows=[197], total=1,
        ), render_session_review=True, session_operations=[{"type": "all"}], submit="session-review",
    )
    assert [box["row"] for box in result["sessionReviewBoxes"]] == [197]
    assert result["sessionReviewSelected"] == [197]
    assert result["calls"][0]["payload"]["rows"] == [197]


def test_checked_alias_stays_excluded_until_other_distinct_saved_session_holds_are_cleared():
    rows = [1, 175, 197, 208]
    result = browser(
        session_review=session_review([175, 197], held_rows=[175, 197, 208]),
        ready_rows=rows, ready_accounts=[ready_account(row) for row in rows], render_picker=True,
        session_selected_rows=[175, 197],
        updated_session_review=session_review([197], held_rows=[175, 197, 208]), updated_ready_rows=rows,
    )
    assert result["before"]["pickerRows"] == [1]
    assert result["sessionReviewSelected"] == [197]
    assert [box["row"] for box in result["sessionReviewBoxes"]] == [197]
    assert {box["row"] for box in result["cohortBoxes"] if box["disabled"]} == {175, 197, 208}
    assert result["calls"] == []
