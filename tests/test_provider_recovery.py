"""Typed connection failures and respectful cooldowns; synthetic requests only."""

import json
import threading

import pytest
from curl_cffi.requests.exceptions import RequestException
from curl_cffi.const import CurlECode

from anghami_session import client, provider_recovery as recovery
from anghami_session.errors import RequestFailure, safe_request_failure
from anghami_session.proxy import ProxyCountryError, safe_proxy_country_failure
from test_saved_session import Reply, TOKEN, bundle, transport


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 1000.0, "waited": []}
    monkeypatch.setattr(recovery, "_deadlines", {})
    monkeypatch.setattr(recovery, "_clock", lambda: state["now"])
    def sleep(seconds):
        state["waited"].append(seconds)
        state["now"] += seconds
    monkeypatch.setattr(recovery.time, "sleep", sleep)
    return state


@pytest.mark.parametrize("code", [5, 6, 7, 28, 35, 52, 55, 56])
def test_known_network_failures_are_provider_issues(code):
    failure = RequestFailure("request_transport_failed", stage="relations", curl_code=code)
    safe = recovery.provider_failure(failure)
    assert safe["failure_category"] == "provider"
    assert safe["retryable"] is True and safe["rotate_route"] is True
    assert safe["curl_code"] == code


@pytest.mark.parametrize("code", [51, 58, 60, 77, 82, 83, 90, 91, 98])
def test_certificate_failures_never_rotate_or_retry(code):
    safe = recovery.provider_failure(RequestFailure("request_transport_failed", curl_code=code))
    assert safe["retryable"] is False and safe["rotate_route"] is False


def test_curl_enum_certificate_failure_preserves_no_retry_classification():
    failure = RequestFailure("request_transport_failed", curl_code=CurlECode.PEER_FAILED_VERIFICATION)
    safe = recovery.provider_failure(failure)
    assert safe["curl_code"] == 60
    assert safe["retryable"] is False and safe["rotate_route"] is False


@pytest.mark.parametrize("status", [401, 403, 407])
def test_http_rejection_without_account_evidence_is_not_an_account_failure(status):
    safe = safe_request_failure(RequestFailure("request_http_failed", http_status=status))
    assert safe["failure_category"] == "provider" and safe["retryable"] is False


@pytest.mark.parametrize("code", ["session_authentication_rejected", "session_identity_mismatch"])
def test_confirmed_account_rejections_do_not_enter_provider_retry(code):
    failure = RequestFailure(code, stage="identity")
    assert safe_request_failure(failure)["failure_category"] == "account"
    assert recovery.provider_failure(failure) == {}


def test_safe_diagnostic_reconstructs_classification_and_removes_secrets():
    safe = safe_request_failure({"code": "request_transport_failed", "stage": "relations",
                                 "failure_category": "account", "curl_code": True,
                                 "http_status": True, "url": TOKEN, "message": TOKEN,
                                 "headers": {"Cookie": TOKEN}, "retryable": True})
    assert safe["failure_category"] == "provider"
    assert "curl_code" not in safe and "http_status" not in safe
    assert TOKEN not in json.dumps(safe)
    assert recovery.provider_failure(RuntimeError(TOKEN)) == {}


def test_postrenewal_failure_is_provider_pending_without_replay():
    failure = RequestFailure("request_transport_failed", stage="session_recovery_validation", retry_safe=False)
    assert recovery.provider_failure(failure)["retryable"] is False
    assert recovery.provider_failure(safe_request_failure(failure))["rotate_route"] is False


def test_country_failure_has_safe_cooldown_evidence():
    failure = ProxyCountryError("http_failure", http_status=429,
                                proxy_connect_http_status=200, retry_after_seconds=60)
    safe = safe_proxy_country_failure(failure)
    assert safe["retry_after_seconds"] == 60
    provider = recovery.provider_failure({"proxy_failure": safe})
    assert provider["code"] == "request_rate_limited" and provider["rotate_route"] is False


def test_country_report_retry_boundary_is_preserved():
    failure = {"failure_kind": "transport_error", "curl_code": 56,
               "country_check_attempts": 3, "retryable": False}
    assert recovery.provider_failure(failure)["retryable"] is False


def test_429_waits_on_same_provider_and_other_workers_respect_deadline(clock):
    error = RequestFailure("request_rate_limited", stage="relations", http_status=429, retry_after_seconds=7)
    assert recovery.wait_for_provider(error)
    assert sum(clock["waited"]) == pytest.approx(7)
    assert safe_request_failure(error)["rotate_route"] is False
    recovery._deadlines["anghami"] = clock["now"] + 6
    assert recovery.wait_before_provider_request("playlists")
    assert sum(clock["waited"]) == pytest.approx(13)


def test_long_retry_after_defers_without_sending_early(clock):
    error = RequestFailure("request_rate_limited", http_status=429, retry_after_seconds=3600)
    assert recovery.wait_for_provider(error) is False
    assert recovery.wait_before_provider_request("relations") is False
    assert clock["waited"] == []


def test_nonretryable_post_response_still_registers_cooldown(clock):
    failure = RequestFailure("request_rate_limited", stage="identity", http_status=429,
                             retry_after_seconds=3600, retry_safe=False)
    recovery.observe_provider_failure(failure)
    assert recovery.wait_before_provider_request("relations") is False
    assert clock["waited"] == []


def test_country_429_registers_cooldown_without_retry(clock):
    from anghami_session.proxy import _country_error
    reply = Reply(status=429)
    reply.headers = {"retry-after": "3600"}
    failure = _country_error("http_failure", reply, attempt=3)
    assert failure.retry_after_seconds == 3600
    assert recovery.wait_before_provider_request("country_check") is False
    assert recovery.wait_before_provider_request("relations") is True


def test_country_and_account_provider_buckets_are_independent(clock):
    recovery._deadlines["country_check"] = clock["now"] + 3600
    assert recovery.wait_before_provider_request("country_check") is False
    assert recovery.wait_before_provider_request("relations") is True


def test_cancelled_cooldown_does_not_start_another_attempt(clock):
    stop = threading.Event()
    stop.set()
    failure = RequestFailure("request_rate_limited", http_status=429)
    assert recovery.wait_for_provider(failure, stop=stop) is False
    assert clock["waited"] == []


@pytest.mark.parametrize("raw,expected", [("10", 10), ("0", 0), ("90000", 86400), ("garbage", None), (None, None)])
def test_retry_after_parses_only_supported_values(raw, expected):
    assert recovery.retry_after_seconds({"Retry-After": raw}) == expected


def test_retry_after_accepts_lowercase_browser_headers():
    assert recovery.retry_after_seconds({"retry-after": "60"}) == 60


def test_actual_429_blocks_next_account_without_caller_retry(bundle, transport, clock):
    instances, replies = transport
    reply = Reply(status=429)
    reply.headers = {"Retry-After": "3600"}
    replies.append(reply)
    with client.AnghamiSession(saved=bundle) as session:
        with pytest.raises(RequestFailure):
            session.request()
    with client.AnghamiSession(saved=bundle) as next_session:
        with pytest.raises(RequestFailure):
            next_session.request()
    assert len(instances[0].calls) == 1
    assert instances[1].calls == []


@pytest.mark.parametrize("reply,code,category", [
    (Reply({"status": "failed"}), "session_authentication_rejected", "account"),
    (Reply(status=429), "request_rate_limited", "provider"),
    (Reply(status=503), "request_http_failed", "provider"),
    (Reply(status=403), "request_http_failed", "provider"),
    (Reply(ValueError(TOKEN)), "session_response_invalid", "unknown"),
    (RequestException(TOKEN, code=28), "request_transport_failed", "provider"),
    (RequestException(TOKEN, code=56), "request_transport_failed", "provider"),
], ids=["auth", "rate", "unavailable", "http-forbidden", "bad-json", "timeout", "receive"])
def test_live_health_errors_retain_safe_cause_before_any_event(bundle, transport, reply, code, category):
    instances, replies = transport
    replies.append(reply)
    with client.AnghamiSession(saved=bundle) as session:
        with pytest.raises(RequestFailure) as caught:
            session.check(negative_control=True)
    safe = safe_request_failure(caught.value)
    assert safe["code"] == code and safe["failure_category"] == category
    assert TOKEN not in str(caught.value) + json.dumps(safe)
    assert len(instances[0].calls) == 1


def test_negative_control_network_failure_is_not_rejected_account(bundle, transport):
    _instances, replies = transport
    replies.extend([Reply(), Reply(), RequestException(TOKEN, code=56)])
    with client.AnghamiSession(saved=bundle) as session:
        with pytest.raises(RequestFailure) as error:
            session.check(negative_control=True)
    safe = safe_request_failure(error.value)
    assert safe["stage"] == "negative_control" and safe["failure_category"] == "provider"


def test_shared_long_cooldown_blocks_new_authenticated_request(bundle, transport, clock):
    instances, _replies = transport
    recovery._deadlines["anghami"] = clock["now"] + 3600
    with client.AnghamiSession(saved=bundle) as session:
        with pytest.raises(RequestFailure) as error:
            session.request()
    assert error.value.code == "request_rate_limited"
    assert instances[0].calls == []
    deadline = recovery._deadlines["anghami"]
    assert recovery.wait_for_provider(error.value) is False
    assert recovery._deadlines["anghami"] == deadline


def test_country_cooldown_refusal_does_not_extend_shared_deadline(clock):
    recovery._deadlines["country_check"] = clock["now"] + 3600
    failure = ProxyCountryError("http_failure", http_status=429,
                                retry_after_seconds=121, retry_safe=False)
    assert recovery.provider_failure(failure)["retryable"] is False
    assert recovery.provider_failure(safe_proxy_country_failure(failure))["retryable"] is False
    assert recovery.wait_for_provider(failure) is False
    assert recovery._deadlines["country_check"] == clock["now"] + 3600
