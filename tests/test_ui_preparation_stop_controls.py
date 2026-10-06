"""Exercise preparation Stop controls without touching any live job."""

from html.parser import HTMLParser
from pathlib import Path

import pytest

from test_ui_like_history_controls import APP, browser, job, state


def preparation(**changes):
    return job(action="prepare", progress={"completed": 7, "total": 20},
               active_workers=3, connection_pending=0, workers=3, **changes)


def test_accounts_stop_button_sends_one_bound_request_and_shows_safe_drain():
    shown = browser(state=state(), render_job=preparation(), stop_via_preparation=True)
    assert shown["calls"] == [{"path": "/api/jobs/stop", "payload": {"job_id": "a" * 32}}]
    assert not shown["prepareStopHidden"] and shown["prepareStopDisabled"]
    assert shown["prepareStopText"] == "Stopping…"
    assert shown["status"] == "Running"
    assert shown["prepareStatus"] == "Stopping preparation · 7 prepared · 3 accounts finishing"
    assert "Waiting for active accounts to finish and save their sessions" in shown["prepareStopHint"]
    assert not shown["prepareStopError"]


def test_workbench_stop_supports_preparation_and_duplicate_click_does_not_repeat():
    shown = browser(state=state(), render_job=preparation(), stop=True, stop_twice=True)
    assert len(shown["calls"]) == 1
    assert shown["stopText"] == shown["prepareStopText"] == "Stopping…"
    assert shown["stopDisabled"] and shown["prepareStopDisabled"]


def test_running_preparation_has_stop_in_accounts_and_workbench_without_request():
    shown = browser(state=state(), render_job=preparation())
    assert not shown["prepareStopHidden"] and not shown["prepareStopDisabled"]
    assert shown["prepareStopText"] == shown["stopText"] == "Stop preparation"
    assert shown["prepareStatus"] == "Preparing accounts · 7 / 20 prepared"
    assert not shown["prepareStatusHidden"] and not shown["calls"]


@pytest.mark.parametrize("capability", [None, False, "true", 1])
def test_older_server_explains_restart_and_never_sends_unsupported_stop(capability):
    saved = state()
    if capability is None:
        del saved["job_controls"]
    else:
        saved["job_controls"]["stop_preparation"] = capability
    shown = browser(state=saved, render_job=preparation(), stop=True, stop_via_preparation=True)
    assert not shown["prepareStopHidden"] and shown["prepareStopDisabled"]
    assert shown["stopDisabled"]
    assert "restart to enable Stop preparation" in shown["prepareStopHint"]
    assert "after this run finishes" in shown["prepareStopHint"]
    assert shown["status"] == "Running" and not shown["calls"]


def test_stop_error_is_visible_in_accounts_and_preserves_active_job():
    shown = browser(state=state(), render_job=preparation(), stop_via_preparation=True, stop_error=True)
    assert shown["prepareStopError"] == shown["stopError"] == "The run changed."
    assert shown["status"] == "Running" and not shown["prepareStopDisabled"]
    assert shown["prepareStopText"] == "Stop preparation"


def test_preparation_stop_is_explained_and_does_not_look_like_account_failure():
    shown = browser(state=state(), render_job=preparation(status="stopped", stop_reason="user_stop"))
    assert shown["statusClass"] == "badge warning"
    assert shown["prepareStopHidden"] and not shown["prepareStatusHidden"]
    assert shown["prepareStatus"] == "Preparation stopped · 7 / 20 prepared"
    assert "Active accounts were allowed to finish and save their sessions" in shown["stopReason"]
    assert "no new accounts started" in shown["stopReason"]
    assert not shown["calls"]


@pytest.mark.parametrize("action", ["like", "play", "check", "preview", "login"])
def test_preparation_controls_are_hidden_for_other_actions(action):
    shown = browser(state=state(), render_job=job(action=action))
    assert shown["prepareStopHidden"] and shown["prepareStatusHidden"]


def test_preparation_stop_button_is_accessible_and_available_while_mutations_disabled():
    class Document(HTMLParser):
        def __init__(self):
            super().__init__()
            self.controls = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.controls[attrs["id"]] = attrs

    document = Document()
    document.feed(Path(APP).with_name("index.html").read_text(encoding="utf-8"))
    button = document.controls["stop-prepare"]
    assert button["type"] == "button" and "data-mutation" not in button
    assert button["aria-describedby"] == "prepare-stop-hint"
    assert "let active preparation finish" in button["aria-label"]
    assert document.controls["prepare-run-status"]["role"] == "status"
    assert document.controls["prepare-control-error"]["role"] == "alert"
