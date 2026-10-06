"""UI ownership reads tolerate sharing faults without accepting stale owners."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from anghami_session import country_preparation as country
from test_country_preparation_workers import imported, install_recovery


OWNER = "a" * 32
OTHER = "b" * 32
PRIVATE = "synthetic-private-owner https://private.invalid/?sid=synthetic-secret"


def ownership_file(tmp_path, *, owner=OWNER, status="running"):
    path = tmp_path / "ui-last-job.json"
    path.write_text(json.dumps({"id": owner, "status": status}), encoding="utf-8")
    return path


def sharing_error():
    error = PermissionError(13, PRIVATE)
    error.winerror = 32
    return error


def inject_reads(monkeypatch, path, responses):
    original = Path.read_text
    calls = []

    def read(selected, *args, **options):
        if selected != path:
            return original(selected, *args, **options)
        calls.append(True)
        response = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(Path, "read_text", read)
    return calls


@pytest.mark.parametrize("failures", [1, 2])
def test_transient_sharing_read_confirms_fresh_owner_before_accepting_it(tmp_path, monkeypatch, failures):
    path = ownership_file(tmp_path)
    good = path.read_text(encoding="utf-8")
    calls = inject_reads(monkeypatch, path, [sharing_error()] * failures + [good])
    waits = []
    monkeypatch.setattr(country, "sleep", waits.append)
    progress = {}

    result = country._owned_ui_state(path, OWNER, progress=progress)
    assert result == (OWNER, None)
    assert len(calls) == failures + 1 and len(waits) == failures
    assert result.failure is None
    assert progress.get("ui_read_failure") is None


def test_permanent_sharing_read_stays_unavailable_with_bounded_safe_diagnostics(tmp_path, monkeypatch):
    path = ownership_file(tmp_path)
    calls = inject_reads(monkeypatch, path, [sharing_error()])
    waits = []
    monkeypatch.setattr(country, "sleep", waits.append)
    progress = {}

    result = country._owned_ui_state(path, OWNER, progress=progress)
    assert result == (None, "ui_unavailable")
    assert len(calls) == 6 and len(waits) == 5
    assert country.safe_ui_read_failure(result.failure) == result.failure
    assert result.failure["attempts"] == 6
    assert result.failure["errno"] == 13 and result.failure["winerror"] == 32
    assert progress["ui_read_failure"] == result.failure
    assert PRIVATE not in json.dumps(result.failure)


def test_prior_success_cannot_be_cached_as_owner_after_read_becomes_unavailable(tmp_path, monkeypatch):
    path = ownership_file(tmp_path)
    assert country._owned_ui_state(path, OWNER) == (OWNER, None)
    calls = inject_reads(monkeypatch, path, [sharing_error()])
    monkeypatch.setattr(country, "sleep", lambda _seconds: None)
    assert country._owned_ui_state(path, OWNER) == (None, "ui_unavailable")
    assert len(calls) == 6


@pytest.mark.parametrize("owner,status,reason", [
    (OTHER, "running", "ui_busy"),
    (OTHER, "succeeded", "ui_activity"),
    (OWNER, "failed", "ui_activity"),
])
def test_recovered_read_never_ignores_changed_or_terminal_owner(tmp_path, monkeypatch, owner, status, reason):
    path = ownership_file(tmp_path)
    calls = inject_reads(monkeypatch, path, [sharing_error(), json.dumps({"id": owner, "status": status})])
    monkeypatch.setattr(country, "sleep", lambda _seconds: None)
    result = country._owned_ui_state(path, OWNER)
    assert result == (owner, reason) and len(calls) == 2
    assert result.failure is None


@pytest.mark.parametrize("content", ["{", "[]", '{}', '{"id":"a","status":"unknown"}'])
def test_malformed_ownership_is_not_retried_or_replaced_with_previous_owner(tmp_path, monkeypatch, content):
    path = ownership_file(tmp_path)
    calls = inject_reads(monkeypatch, path, [content, json.dumps({"id": OWNER, "status": "running"})])
    monkeypatch.setattr(country, "sleep", lambda _seconds: pytest.fail("Invalid ownership retried"))
    result = country._owned_ui_state(path, OWNER)
    assert result == (None, "ui_unavailable") and len(calls) == 1
    assert result.failure["attempts"] == 1
    assert PRIVATE not in json.dumps(result.failure)


def test_nonsharing_io_error_is_fail_closed_without_retry(tmp_path, monkeypatch):
    path = ownership_file(tmp_path)
    calls = inject_reads(monkeypatch, path, [OSError(5, PRIVATE)])
    monkeypatch.setattr(country, "sleep", lambda _seconds: pytest.fail("Non-sharing failure retried"))
    result = country._owned_ui_state(path, OWNER)
    assert result == (None, "ui_unavailable") and len(calls) == 1
    assert result.failure["attempts"] == 1 and result.failure["errno"] == 5
    assert PRIVATE not in json.dumps(result.failure)


def test_non_windows_permission_error_does_not_enter_windows_sharing_retry(tmp_path, monkeypatch):
    path = ownership_file(tmp_path)
    calls = inject_reads(monkeypatch, path, [sharing_error()])
    monkeypatch.setattr(country, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(country, "sleep", lambda _seconds: pytest.fail("Windows retry ran on another platform"))
    result = country._owned_ui_state(path, OWNER)
    assert result == (None, "ui_unavailable") and len(calls) == 1


def test_transient_sharing_stat_is_retried_before_reading_current_owner(tmp_path, monkeypatch):
    path = ownership_file(tmp_path)
    original = Path.stat
    calls = []

    def stat(selected, *args, **options):
        if selected == path:
            calls.append(True)
            if len(calls) == 1:
                raise sharing_error()
        return original(selected, *args, **options)

    monkeypatch.setattr(Path, "stat", stat)
    waits = []
    monkeypatch.setattr(country, "sleep", waits.append)
    result = country._owned_ui_state(path, OWNER)
    assert result == (OWNER, None) and len(calls) == 2 and len(waits) == 1


def test_missing_report_cannot_be_replaced_by_cached_owner_or_retried(tmp_path, monkeypatch):
    path = ownership_file(tmp_path)
    assert country._owned_ui_state(path, OWNER) == (OWNER, None)
    path.unlink()
    monkeypatch.setattr(country, "sleep", lambda _seconds: pytest.fail("Missing ownership report retried"))
    result = country._owned_ui_state(path, OWNER)
    assert result == (None, "ui_activity")
    assert result.failure["code"] == "ui_report_missing" and result.failure["attempts"] == 1


def test_oversized_report_is_rejected_without_loading_or_retrying_it(tmp_path, monkeypatch):
    path = ownership_file(tmp_path)
    path.write_bytes(b"x" * 10_000_001)
    monkeypatch.setattr(country, "sleep", lambda _seconds: pytest.fail("Oversized ownership report retried"))
    inject_reads(monkeypatch, path, [AssertionError("Oversized report was read")])
    result = country._owned_ui_state(path, OWNER)
    assert result == (None, "ui_unavailable")
    assert result.failure == {"code": "ui_report_too_large", "stage": "stat", "attempts": 1}


@pytest.mark.parametrize("value", [
    {"code": PRIVATE, "stage": "read", "attempts": 1},
    {"code": "ui_file_unreadable", "stage": PRIVATE, "attempts": 1},
    {"code": "ui_file_unreadable", "stage": "read", "attempts": True},
    {"code": "ui_file_unreadable", "stage": "read", "attempts": 7},
    {"code": "ui_file_unreadable", "stage": "read", "attempts": 1, "errno": PRIVATE},
    {"code": "ui_file_unreadable", "stage": "read", "attempts": 1, "path": PRIVATE},
    {"code": "ui_report_invalid", "stage": "parse", "attempts": 1, "winerror": 32},
])
def test_ownership_diagnostics_reject_private_or_invalid_fields(value):
    assert country.safe_ui_read_failure(value) == {}


def test_coordinator_starts_no_account_requests_while_owner_read_is_unavailable(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    install_recovery(monkeypatch, factory)
    path = ownership_file(tmp_path)
    calls = inject_reads(monkeypatch, path, [sharing_error()])

    def wait(_seconds):
        assert not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)

    monkeypatch.setattr(country, "sleep", wait)
    result = country.run_plan(
        parent, plan, tmp_path / "progress.json", workers=30, no_browser=True,
        max_consecutive_failures=20, ui_path=path, owned_ui_job_id=OWNER,
    )
    assert result["status"] == "paused" and result["pause_reason"] == "ui_unavailable"
    assert result["counts"]["pending"] == 8 and result["attempted_accounts"] == 0
    assert len(calls) >= 3
    assert not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)


def test_manual_stop_arriving_during_read_retry_prevents_new_account_requests(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    install_recovery(monkeypatch, factory)
    path = ownership_file(tmp_path)
    progress_path = tmp_path / "progress.json"
    good = path.read_text(encoding="utf-8")
    inject_reads(monkeypatch, path, [sharing_error(), good])

    def stop_during_wait(_seconds):
        progress_path.with_suffix(".stop").touch()

    monkeypatch.setattr(country, "sleep", stop_during_wait)
    result = country.run_plan(
        parent, plan, progress_path, workers=30, no_browser=True,
        max_consecutive_failures=20, ui_path=path, owned_ui_job_id=OWNER,
    )
    assert result["status"] == "paused" and result["pause_reason"] == "stop_requested"
    assert not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)
