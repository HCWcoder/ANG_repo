"""Accepted writes survive provider readback outages without being replayed."""

import json

import pytest

from anghami_session import play_record, ui_jobs, ui_server
from anghami_session.errors import RequestFailure, SessionError, safe_request_failure
from test_ui_jobs_concurrency import finish, manager_for


def accepted_unverified(*, failure=None):
    return {
        "passed": False, "phase": "failed", "failed_phase": "state_after",
        "error_code": "state_read_failed", "renewal_attempted": True, "renewal_completed": True,
        "mutation_attempted": True, "mutation_attempts": 1, "mutation_accepted": True,
        "mutation_result": "accepted", "mutation_http_status": 200,
        "liked_before": False, "liked_after": None, "persisted_state_verified": False,
        "verification_read_attempts": 3, "verification_read_retries": 2,
        "verification_read_retryable": True,
        "session_failure": safe_request_failure(failure or RequestFailure(
            "request_transport_failed", stage="likes_read", curl_code=7, retry_safe=False)),
    }


def verified_like():
    return {**accepted_unverified(), "passed": True, "phase": "complete",
            "liked_after": True, "persisted_state_verified": True}


def run(manager, factory, *, workers=8, maximum=20, count=1):
    manager.submit({"action": "like", "rows": list(factory.rows), "count": count,
                    "workers": workers, "max_consecutive_failures": maximum})
    return finish(manager)


@pytest.mark.parametrize("workers,maximum", [(1, 1), (1, 20), (8, 20)])
@pytest.mark.parametrize("raises", [False, True])
def test_accepted_like_readback_outage_continues_other_accounts(tmp_path, workers, maximum, raises):
    def handler(action, row, number, options):
        if row != 1:
            return verified_like()
        report = accepted_unverified()
        if raises:
            play_record._journal(report, factory.path.parent / "account-1.test-like-report.json")
            raise SessionError("Synthetic verification connection failed.")
        return report

    manager, factory = manager_for(tmp_path, rows=[1, 2, 3], handler=handler)
    job = run(manager, factory, workers=workers, maximum=maximum)
    assert job["status"] == "completed_with_pending" and job["stop_reason"] is None
    assert (job["attempted"], job["completed_tests"], job["succeeded"], job["skipped"]) == (3, 3, 2, 0)
    assert job["failed"] == job["account_failed"] == job["consecutive_failures"] == 0
    assert job["connection_pending"] == 0 and job["verification_pending"] == 1
    assert job["writes_attempted"] == job["writes_accepted"] == 3 and job["new_likes_verified"] == 2
    assert factory.attempts == {1: 1, 2: 1, 3: 1}
    pending = next(item for item in job["results"] if item["source_row"] == 1)
    assert pending["outcome"] == "verification_pending" and pending["result_unknown"] is False
    assert pending["error_code"] == "state_read_failed" and pending["mutation_attempts"] == 1
    assert pending["attempt_history"][0]["outcome"] == "verification_pending"
    assert "1 accepted likes awaiting verification" in job["message"]
    safe = ui_server.safe_report(job)
    assert safe["verification_pending"] == 1
    assert next(item for item in safe["results"] if item["source_row"] == 1)["outcome"] == "verification_pending"
    saved = json.loads((factory.path.parent / "ui-test-reports" / job["id"] / "account-1.like-1.redacted.json").read_text())
    assert saved["outcome"] == "verification_pending" and saved["mutation_accepted"] is True


@pytest.mark.parametrize("workers,maximum", [(1, 1), (8, 20)])
def test_pending_like_never_repeats_or_runs_an_alias_in_same_job(tmp_path, workers, maximum):
    manager, factory = manager_for(tmp_path, rows=[1, 2, 3], handler=lambda action, row, *_: (
        accepted_unverified() if row == 1 else verified_like()))
    factory.identities[3] = factory.identities[1]
    job = run(manager, factory, workers=workers, maximum=maximum, count=3)
    assert factory.attempts == {1: 1, 2: 3}
    assert job["status"] == "completed_with_pending" and job["verification_pending"] == 1
    assert job["new_likes_verified"] == 3 and job["writes_accepted"] == 4
    assert job["attempted"] == 4 and job["skipped"] == 5


def test_more_than_twenty_pending_verifications_do_not_consume_failure_limit(tmp_path):
    manager, factory = manager_for(tmp_path, rows=range(1, 65), handler=lambda *_: accepted_unverified())
    job = run(manager, factory)
    assert job["status"] == "completed_with_pending" and job["stop_reason"] is None
    assert job["attempted"] == job["completed_tests"] == job["verification_pending"] == 64
    assert job["writes_accepted"] == 64 and job["new_likes_verified"] == 0
    assert job["failed"] == job["consecutive_failures"] == job["skipped"] == 0
    assert all(number == 1 for number in factory.attempts.values())


@pytest.mark.parametrize("failure,error_code", [
    (RequestFailure("request_rate_limited", stage="likes_read", http_status=429, retry_safe=False), "state_http_failed"),
    (RequestFailure("request_http_failed", stage="likes_read", http_status=503, retry_safe=False), "state_http_failed"),
    (RequestFailure("request_rate_limited", stage="likes_read", http_status=429,
                    retry_after_seconds=121, retry_safe=False), "request_rate_limited"),
])
def test_postwrite_typed_http_outage_is_pending_without_whole_test_retry(tmp_path, failure, error_code):
    report = {**accepted_unverified(failure=failure), "error_code": error_code}
    manager, factory = manager_for(tmp_path, rows=[1, 2], handler=lambda action, row, *_: report if row == 1 else verified_like())
    job = run(manager, factory, workers=1)
    assert job["verification_pending"] == 1 and job["new_likes_verified"] == 1
    assert factory.attempts == {1: 1, 2: 1} and job["provider_retries"] == 0


@pytest.mark.parametrize("change", [
    {"mutation_result": "unknown", "mutation_accepted": None},
    {"mutation_attempts": 2}, {"mutation_attempts": True},
    {"mutation_http_status": 201}, {"mutation_accepted": False},
    {"liked_after": False, "error_code": "readback_not_liked"},
    {"renewal_completed": False}, {"failed_phase": "state_before"},
    {"error_code": "journal_failed"},
    {"session_failure": safe_request_failure(RequestFailure("session_response_invalid", stage="likes_read"))},
    {"session_failure": safe_request_failure(RequestFailure("request_transport_failed", stage="likes_read", curl_code=60))},
    {"session_failure": safe_request_failure(RequestFailure("request_http_failed", stage="likes_read", http_status=401))},
    {"session_failure": safe_request_failure(RequestFailure("request_transport_failed", stage="likes_read"))},
    {"session_failure": safe_request_failure(RequestFailure("request_proxy_unverified", stage="likes_read"))},
    {"session_failure": safe_request_failure(RequestFailure("request_transport_failed", stage="identity", curl_code=7))},
])
def test_verification_pending_does_not_hide_uncertain_or_invalid_results(tmp_path, change):
    manager, factory = manager_for(tmp_path, rows=[1, 2], handler=lambda action, row, *_: {
        **accepted_unverified(), **change})
    job = run(manager, factory, workers=1)
    assert job["status"] == "failed" and job["verification_pending"] == 0
    assert factory.attempts == {1: 1} and job["new_likes_verified"] == 0


@pytest.mark.parametrize("name,valid,bad", [
    ("verification_pending", 2, True),
    ("verification_read_attempts", 3, 4),
    ("verification_read_retries", 2, 3),
    ("verification_read_retryable", True, "synthetic-private-secret"),
])
def test_pending_readback_facts_are_strictly_sanitized(name, valid, bad):
    assert ui_server.safe_report({name: valid})[name] == valid
    assert ui_server.safe_report({name: bad})[name] is None
    if name != "verification_pending":
        assert ui_jobs._public_report({name: bad}) == {}


@pytest.mark.parametrize("scope", [{"source_row": 2}, {"song_id": str(int(play_record.TEST_SONG_ID) + 1)}])
def test_recovered_pending_report_with_wrong_scope_is_fatal(tmp_path, scope):
    def handler(*_):
        play_record._journal({**accepted_unverified(), **scope}, factory.path.parent / "account-1.test-like-report.json")
        raise SessionError("Synthetic readback unavailable.")
    manager, factory = manager_for(tmp_path, rows=[1, 2], handler=handler)
    job = run(manager, factory, workers=1)
    assert job["status"] == "failed" and job["error"]["code"] == "request_scope_invalid"
    assert job["verification_pending"] == 0 and factory.attempts == {1: 1}


@pytest.mark.parametrize("exception", [
    ui_jobs._JobFailure({"code": "account_scope_invalid", "message": "Synthetic scope hold."}),
    play_record._Failure("journal_failed", "Synthetic journal unavailable."),
    KeyboardInterrupt(),
])
def test_pending_report_cannot_bypass_local_scope_journal_or_interrupt_guard(tmp_path, exception):
    def handler(*_):
        play_record._journal(accepted_unverified(), factory.path.parent / "account-1.test-like-report.json")
        raise exception
    manager, factory = manager_for(tmp_path, rows=[1, 2], handler=handler)
    job = run(manager, factory, workers=1)
    assert job["status"] == "failed" and job["verification_pending"] == 0
    assert factory.attempts == {1: 1}


def test_pending_account_report_journal_failure_stops_before_next_dispatch(tmp_path, monkeypatch):
    manager, factory = manager_for(tmp_path, rows=[1, 2], handler=lambda *_: accepted_unverified())
    original = ui_jobs._journal
    def journal(value, path):
        if "ui-test-reports" in path.parts:
            raise OSError("Synthetic pending report unavailable.")
        return original(value, path)
    monkeypatch.setattr(ui_jobs, "_journal", journal)
    job = run(manager, factory, workers=1)
    assert job["status"] == "failed" and job["error"]["code"] == "journal_failed"
    assert factory.attempts == {1: 1}


def test_verification_pending_keeps_twenty_style_failure_budget_neutral(tmp_path):
    def handler(action, row, *_):
        if row == 2:
            return accepted_unverified()
        return {"passed": False, "mutation_attempted": True, "mutation_result": "rejected",
                "mutation_accepted": False, "error_code": "mutation_rejected"}
    manager, factory = manager_for(tmp_path, rows=[1, 2, 3, 4], handler=handler)
    job = run(manager, factory, workers=1, maximum=2)
    assert job["stop_reason"] == "consecutive_failure_limit"
    assert job["failed"] == job["consecutive_failures"] == 2 and job["verification_pending"] == 1
    assert job["attempted"] == 3 and job["skipped"] == 1


@pytest.mark.parametrize("failure", [
    RequestFailure("request_transport_failed", stage="likes_read", curl_code=7, http_status=401),
    RequestFailure("request_transport_failed", stage="likes_read", curl_code=7, http_status=403),
    RequestFailure("request_transport_failed", stage="likes_read", curl_code=7, http_status=407),
    RequestFailure("request_http_failed", stage="likes_read", http_status=503, curl_code=60),
])
def test_authentication_or_certificate_evidence_prevents_read_only_provider_retry(failure):
    from anghami_session.like_test import retryable_like_verification_failure
    assert retryable_like_verification_failure(failure) is False
    assert ui_jobs.JobManager._accepted_like_verification_pending("like", accepted_unverified(failure=failure)) is False
