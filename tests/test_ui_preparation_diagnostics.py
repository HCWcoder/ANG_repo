"""Preparation errors expose typed evidence and never renewed session candidates."""

import json

import pytest

from anghami_session import preparation, ui_jobs, ui_server
from anghami_session.errors import RequestFailure, SessionError, safe_request_failure
from anghami_session.proxy import ProxyCountryError, safe_proxy_country_failure
from test_ui_jobs import finish, make_manager


PRIVATE = "synthetic-private-renewed-session-do-not-expose"


def request_failure(code="request_transport_failed", **fields):
    error = RequestFailure(code, **fields)
    error.args = (PRIVATE,)
    error.saved_session_candidate = {"sid": PRIVATE, "cookies": PRIVATE}
    error.raw_response = PRIVATE
    return error


@pytest.mark.parametrize("no_browser", [False, True])
def test_preparation_connection_error_retains_safe_code_stage_and_curl_without_account_rejection(no_browser):
    error = request_failure(stage="negative_control", curl_code=7, retry_safe=False)
    result = ui_jobs.JobManager._safe_error("prepare", error, no_browser=no_browser)
    assert result["code"] == "request_transport_failed"
    assert result["session_failure"] == safe_request_failure(error)
    assert result["session_failure"]["stage"] == "negative_control"
    assert result["session_failure"]["curl_code"] == 7
    assert "connection failed" in result["message"]
    assert "does not prove the account is invalid" in result["message"]
    assert "renewal_unknown" not in result
    assert ("No browser login was attempted." in result["message"]) is no_browser
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("code", ["request_rate_limited", "request_http_failed"])
def test_preparation_429_message_requires_cooldown_without_claiming_automatic_retry(code):
    error = request_failure(code, stage="relations", http_status=429, retry_after_seconds=31)
    result = ui_jobs.JobManager._safe_error("prepare", error, no_browser=True)
    assert result["code"] == code
    assert result["session_failure"]["http_status"] == 429
    assert result["session_failure"]["retry_after_seconds"] == 31
    assert "cooldown is required" in result["message"]
    assert "will retry" not in result["message"]
    assert "renewal_unknown" not in result and PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_recovery_stage_does_not_infer_unknown_renewal_without_exact_flag(flag):
    error = request_failure(stage="session_recovery_validation", curl_code=56, retry_safe=False)
    error.renewal_unknown = flag
    result = ui_jobs.JobManager._safe_error("prepare", error, no_browser=True)
    assert result["code"] == "request_transport_failed"
    assert "renewal_unknown" not in result


def test_explicit_unknown_renewal_is_reported_as_unknown_with_typed_underlying_failure():
    error = request_failure(stage="session_recovery_renewal", curl_code=28, retry_safe=False)
    error.renewal_unknown = True
    result = ui_jobs.JobManager._safe_error("prepare", error, no_browser=True)
    assert result["code"] == "session_renewal_unknown" and result["renewal_unknown"] is True
    assert result["session_failure"]["code"] == "request_transport_failed"
    assert "Review this account before retrying" in result["message"]
    assert "renewal was not repeated" in result["message"]
    assert PRIVATE not in json.dumps(result)


def test_fixed_generic_unknown_renewal_does_not_expose_exception_text_or_candidate():
    error = SessionError(PRIVATE)
    error.renewal_unknown = True
    error.saved_session_candidate = {"sid": PRIVATE}
    result = ui_jobs.JobManager._safe_error("prepare", error, no_browser=True)
    assert result["code"] == "session_renewal_unknown" and result["renewal_unknown"] is True
    assert "session_failure" not in result and PRIVATE not in json.dumps(result)


def test_known_renewal_validation_pending_does_not_claim_renewal_unknown():
    error = request_failure(stage="negative_control", curl_code=7, retry_safe=False)
    error.renewal_completed = True
    error.validation_pending = True
    error.session_validation_attempts = 3
    error.session_validation_retries = 2
    result = ui_jobs.JobManager._safe_error("prepare", error, no_browser=True)
    assert result["code"] == "request_transport_failed"
    assert result["renewal_completed"] is True and result["validation_pending"] is True
    assert result["session_validation_attempts"] == 3 and result["session_validation_retries"] == 2
    assert "renewal_unknown" not in result and PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("code", ["session_authentication_rejected", "session_identity_mismatch"])
def test_confirmed_account_errors_are_not_presented_as_proxy_connection_failures(code):
    error = request_failure(code, stage="identity", retry_safe=False)
    result = ui_jobs.JobManager._safe_error("prepare", error, no_browser=True)
    assert result["code"] == code
    assert result["session_failure"]["failure_category"] == "account"
    assert "needs review" in result["message"]
    assert "connection failed" not in result["message"]


@pytest.mark.parametrize("fields", [{"failure_kind": "transport_error", "curl_code": 7},
                                    {"failure_kind": "http_failure", "http_status": 429, "retry_after_seconds": 15},
                                    {"failure_kind": "authentication_rejected", "http_status": 407}])
def test_proxy_preparation_diagnostic_uses_safe_kind_numbers_and_fixed_message(fields):
    error = ProxyCountryError(**fields)
    error.args = (PRIVATE,)
    error.raw_proxy = PRIVATE
    result = ui_jobs.JobManager._safe_error("prepare", error, no_browser=True)
    assert result["code"] == "proxy_preflight_failed"
    assert result["proxy_failure"] == safe_proxy_country_failure(error)
    assert PRIVATE not in json.dumps(result)
    if fields.get("http_status") == 429:
        assert "cooldown is required" in result["message"]
    if fields.get("http_status") == 407:
        assert "account was not rejected" in result["message"]


def test_mutated_unsupported_typed_fields_do_not_escape_generic_redaction():
    error = request_failure(stage="negative_control", curl_code=7)
    error.code = error.stage = PRIVATE
    result = ui_jobs.JobManager._safe_error("prepare", error, no_browser=True)
    assert result["code"] == "session_preparation_failed"
    assert "session_failure" not in result and PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("action", ["play", "like", "login", "check", "proxy-check", "preview"])
def test_preparation_diagnostics_do_not_change_unrelated_action_safety(action):
    plain = SessionError(PRIVATE)
    typed = request_failure(stage="negative_control", curl_code=7)
    typed.renewal_unknown = True
    assert ui_jobs.JobManager._safe_error(action, typed, no_browser=True) == ui_jobs.JobManager._safe_error(action, plain, no_browser=True)


def test_preparation_job_and_local_journal_retain_only_typed_failure_evidence(tmp_path, monkeypatch):
    error = request_failure(stage="negative_control", curl_code=7, retry_safe=False)
    def fail(_vault, **_options):
        raise error
    monkeypatch.setattr(preparation, "prepare_test_accounts", fail)
    manager, *_ = make_manager(tmp_path)
    manager.submit({"action": "prepare", "count": 1, "no_browser": True})
    job = finish(manager)
    assert job["status"] == "failed"
    assert job["error"]["session_failure"]["curl_code"] == 7
    assert job["error"]["session_failure"]["stage"] == "negative_control"
    assert PRIVATE not in json.dumps(job) and PRIVATE not in manager._report_path.read_text(encoding="utf-8")


def test_completed_preparation_with_known_renewed_pending_validation_is_not_failed(tmp_path, monkeypatch):
    def pending(_vault, **_options):
        return {"phase": "complete", "passed": False, "requested_accounts": 1,
                "selected_rows": [19], "prepared_rows": [], "prepared_account_count": 0,
                "attempted_accounts": 1, "connection_pending_rows": [19], "account_failed_rows": [],
                "validation_pending": True, "renewal_completed": True,
                "session_validation_attempts": 3, "session_validation_retries": 2,
                "attach_validation_attempts": 0, "attach_validation_retries": 0,
                "session_failure": safe_request_failure(request_failure(stage="negative_control", curl_code=7)),
                "saved_session_candidate": {"sid": PRIVATE}}
    monkeypatch.setattr(preparation, "prepare_test_accounts", pending)
    manager, *_ = make_manager(tmp_path)
    manager.submit({"action": "prepare", "count": 1, "no_browser": True})
    job = finish(manager)
    assert job["status"] == "completed_with_pending" and job["connection_pending"] == 1
    assert job["account_failed"] == 0 and job.get("error") is None
    result = job["results"][0]
    assert result["validation_pending"] is True and result["renewal_completed"] is True
    assert result["session_validation_attempts"] == 3 and result["session_validation_retries"] == 2
    assert result["session_failure"]["curl_code"] == 7 and PRIVATE not in json.dumps(job)


@pytest.mark.parametrize("name", ["session_validation_attempts", "attach_validation_attempts"])
@pytest.mark.parametrize("value", [0, 1, 2, 3])
def test_bounded_validation_attempt_counts_survive_both_public_boundaries(name, value):
    assert ui_jobs._public_report({name: value}) == {name: value}
    assert ui_server.safe_report({name: value}) == {name: value}


@pytest.mark.parametrize("name", ["session_validation_retries", "attach_validation_retries"])
@pytest.mark.parametrize("value", [0, 1, 2])
def test_bounded_validation_retry_counts_survive_both_public_boundaries(name, value):
    assert ui_jobs._public_report({name: value}) == {name: value}
    assert ui_server.safe_report({name: value}) == {name: value}


@pytest.mark.parametrize("name", ["session_validation_attempts", "attach_validation_attempts",
                                 "session_validation_retries", "attach_validation_retries"])
@pytest.mark.parametrize("value", [True, -1, 4, 1.5, "1", None, PRIVATE, {}, []])
def test_validation_count_boundaries_drop_invalid_types_and_out_of_range_values(name, value):
    assert ui_jobs._public_report({name: value}) == {}
    assert ui_server.safe_report({name: value}) == {name: None}


@pytest.mark.parametrize("value", [True, False])
def test_validation_pending_is_a_safe_boolean(value):
    assert ui_jobs._public_report({"validation_pending": value}) == {"validation_pending": value}
    assert ui_server.safe_report({"validation_pending": value}) == {"validation_pending": value}


@pytest.mark.parametrize("value", [0, 1, "true", None, PRIVATE, {}, []])
def test_invalid_validation_pending_never_exposes_untyped_payload(value):
    assert ui_jobs._public_report({"validation_pending": value}) == {}
    assert ui_server.safe_report({"validation_pending": value}) == {"validation_pending": None}
