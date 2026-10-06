"""Post-renewal recovery retries only mocked GET validation requests."""

import json
from urllib.parse import parse_qs, urlsplit

from curl_cffi.curl import CurlError
import pytest

from anghami_session import provider_recovery, session_recovery as recovery
from anghami_session.errors import RequestFailure, SessionError, safe_request_failure
from anghami_session.proxy import ProxyCountryError
from test_session_recovery import (
    FINGERPRINT, NEW_SID, PASSWORD, SERVER_COOKIE, SID,
    Proxy, Reply, record, transport,
)


def valid_check():
    return [Reply({"status": "ok"}), Reply({"status": "ok"}), Reply({"status": "failed"})]


def assert_one_renewal_same_candidate(calls, instances, proxy):
    assert sum(method == "POST" for _, method, _, _ in calls) == 1
    assert sum(parse_qs(urlsplit(url).query).get("type") == ["GETprofile"] for _, method, url, _ in calls if method == "GET") == 1
    main = instances[0]
    for instance, method, url, options in calls[5:]:
        assert method == "GET"
        query = parse_qs(urlsplit(url).query)
        assert query["fingerprint"] == [FINGERPRINT]
        if instance is main:
            assert query["sid"] == query["appsid"] == [NEW_SID]
            assert options["headers"]["x-angh-session"] == NEW_SID
        else:
            assert "sid" not in query and "appsid" not in query
            assert "cookie" not in options["headers"] and "x-angh-session" not in options["headers"]
    if proxy is not None:
        assert proxy.verified == 1
        assert all(instance.options == proxy.transport_options() for instance in instances)
    assert all(instance.closed for instance in instances)


@pytest.mark.parametrize("failed_read", [0, 1, 2])
@pytest.mark.parametrize("use_proxy", [False, True])
def test_transient_postrenewal_read_rechecks_same_issued_candidate_without_another_post(record, transport, monkeypatch, failed_read, use_proxy):
    replies, instances, calls = transport
    sleeps = []
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: sleeps.append(seconds))
    replies[5:] = valid_check()[:failed_read] + [CurlError("private connection diagnostic", code=7)] + valid_check()
    proxy = Proxy() if use_proxy else None
    saved, metadata = recovery.recover_legacy_session(record, proxy=proxy)
    assert not replies
    assert metadata["session_validation_attempts"] == 2 and metadata["session_validation_retries"] == 1
    assert metadata["authenticated"] and metadata["identity_verified"] and metadata["session_renewed"]
    assert sleeps == [0.5]
    for template in saved["requests"].values():
        assert parse_qs(urlsplit(template["url"]).query)["sid"] == [NEW_SID]
    assert_one_renewal_same_candidate(calls, instances, proxy)
    rendered = json.dumps(metadata)
    assert all(secret not in rendered for secret in (SID, NEW_SID, FINGERPRINT, PASSWORD, SERVER_COOKIE))


def test_postrenewal_negative_control_429_honors_shared_cooldown_on_same_proxy(record, transport, monkeypatch):
    replies, instances, calls = transport
    clock = [100.0]
    monkeypatch.setattr(provider_recovery, "_clock", lambda: clock[0])
    monkeypatch.setattr(provider_recovery.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    limited = Reply({}, status=429)
    limited.headers = {"Retry-After": "30"}
    replies[7:] = [limited] + valid_check()
    proxy = Proxy()
    _, metadata = recovery.recover_legacy_session(record, proxy=proxy)
    assert not replies and clock[0] == 130.0
    assert metadata["session_validation_attempts"] == 2 and metadata["session_validation_retries"] == 1
    assert provider_recovery._deadlines["anghami"] == 130.0
    assert_one_renewal_same_candidate(calls, instances, proxy)


def test_postrenewal_validation_exhaustion_retains_private_issued_candidate_and_safe_attempt_counts(record, transport, monkeypatch):
    replies, instances, calls = transport
    sleeps = []
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: sleeps.append(seconds))
    replies[5:] = [CurlError("private retry diagnostic", code=7) for _ in range(3)]
    proxy = Proxy()
    with pytest.raises(RequestFailure) as caught:
        recovery.recover_legacy_session(record, proxy=proxy)
    assert not replies and len(calls) == 8
    assert getattr(caught.value, "renewal_unknown", False) is False
    assert caught.value.renewal_completed is True and caught.value.validation_pending is True
    for template in caught.value.validation_candidate["requests"].values():
        assert parse_qs(urlsplit(template["url"]).query)["sid"] == [NEW_SID]
    assert caught.value.validation_read_attempts == 3 and caught.value.validation_read_retries == 2
    assert safe_request_failure(caught.value)["retryable"] is False
    safe = json.dumps(safe_request_failure(caught.value)) + str(caught.value)
    assert all(secret not in safe for secret in (SID, NEW_SID, FINGERPRINT, PASSWORD, SERVER_COOKIE))
    assert sleeps == [0.5, 1.0]
    assert_one_renewal_same_candidate(calls, instances, proxy)


@pytest.mark.parametrize("problem", [
    CurlError("private TLS failure", code=60),
    CurlError("private unknown failure", code=99),
    OSError("private transport with unknown numeric code"),
    Reply({}, status=401), Reply({}, status=403), Reply({}, status=407), Reply({}, status=404),
    Reply({"status": "failed"}), Reply({"status": "unknown"}), Reply(ValueError("private malformed JSON")),
])
def test_postrenewal_tls_authentication_unknown_and_semantic_errors_are_not_retried(record, transport, problem):
    replies, instances, calls = transport
    replies[5:] = [problem]
    with pytest.raises(RequestFailure) as caught:
        recovery.recover_legacy_session(record)
    assert not replies and len(calls) == 6
    assert caught.value.validation_read_attempts == 1 and caught.value.validation_read_retries == 0
    assert safe_request_failure(caught.value)["retryable"] is False
    assert not hasattr(caught.value, "validation_candidate")
    assert not hasattr(caught.value, "validation_pending")
    assert_one_renewal_same_candidate(calls, instances, None)


@pytest.mark.parametrize("index", [0, 3, 4])
def test_transient_failures_before_or_during_bootstrap_do_not_enter_validation_retry(record, transport, index):
    replies, instances, calls = transport
    replies[index] = CurlError("private prevalidation diagnostic", code=7)
    with pytest.raises(RequestFailure) as caught:
        recovery.recover_legacy_session(record)
    assert len(calls) == index + 1
    assert not hasattr(caught.value, "validation_read_attempts")
    assert not hasattr(caught.value, "validation_candidate")
    assert sum(method == "POST" for _, method, _, _ in calls) == (index == 4)
    assert all(instance.closed for instance in instances)


def test_postrenewal_long_cooldown_keeps_private_candidate_pending_without_another_validation_read(record, transport):
    replies, instances, calls = transport
    limited = Reply({}, status=429)
    limited.headers = {"Retry-After": "600"}
    replies[5:] = [limited]
    proxy = Proxy()
    with pytest.raises(RequestFailure) as caught:
        recovery.recover_legacy_session(record, proxy=proxy)
    assert not replies and len(calls) == 6
    assert getattr(caught.value, "renewal_unknown", False) is False
    assert caught.value.validation_pending is True and caught.value.renewal_completed is True
    assert caught.value.validation_read_attempts == 1
    assert provider_recovery._deadlines["anghami"] - provider_recovery._clock() > 599
    assert_one_renewal_same_candidate(calls, instances, proxy)


@pytest.mark.parametrize("stage", ["identity", "session_recovery_renewal", "session_recovery_preflight", "preflight", "country_check", "song_metadata", "likes_read"])
def test_reusable_validation_retry_helper_rejects_unsafe_stages(stage):
    calls = []
    error = RequestFailure("request_transport_failed", stage=stage, curl_code=7)
    def check():
        calls.append(True)
        raise error
    with pytest.raises(RequestFailure) as caught:
        recovery.retry_readonly_validation(check)
    assert caught.value is error and calls == [True]
    assert error.validation_read_attempts == 1


def test_reusable_validation_retry_helper_respects_explicit_no_retry():
    calls = []
    error = RequestFailure("request_transport_failed", stage="negative_control", curl_code=7, retry_safe=False)
    def check():
        calls.append(True)
        raise error
    with pytest.raises(RequestFailure) as caught:
        recovery.retry_readonly_validation(check)
    assert caught.value is error and calls == [True]
    assert error.validation_read_attempts == 1


@pytest.mark.parametrize("failure, retryable, pending", [
    (RequestFailure("request_transport_failed", stage="negative_control", curl_code=7), True, True),
    (RequestFailure("request_transport_failed", stage="negative_control", curl_code=7, retry_safe=False), False, False),
    (RequestFailure("request_rate_limited", stage="negative_control", http_status=429, retry_safe=False), False, True),
    (RequestFailure("request_transport_failed", stage="negative_control", curl_code=7, http_status=401), False, False),
    (RequestFailure("request_http_failed", stage="negative_control", http_status=503, curl_code=60), False, False),
    (RequestFailure("request_proxy_unverified", stage="preflight"), False, False),
    (RequestFailure("session_response_invalid", stage="negative_control"), False, False),
    (RequestFailure("request_transport_failed", stage="session_recovery_renewal", curl_code=7), False, False),
])
def test_validation_failure_predicate_distinguishes_read_retry_and_pending_cooldown(failure, retryable, pending):
    assert recovery.read_only_validation_failure(failure) is retryable
    assert recovery.read_only_validation_failure(failure, allow_cooldown_refusal=True) is pending
    assert recovery.read_only_validation_failure(safe_request_failure(failure)) is retryable


def test_semantic_anonymous_control_failure_keeps_unknown_hold_without_pending_candidate(record, transport):
    replies, _, calls = transport
    replies[7] = Reply({"status": "ok"})
    with pytest.raises(RequestFailure) as caught:
        recovery.recover_legacy_session(record)
    assert caught.value.code == "session_control_failed"
    assert caught.value.renewal_unknown is True
    assert not hasattr(caught.value, "validation_candidate")
    assert len(calls) == 8 and sum(method == "POST" for _, method, _, _ in calls) == 1


def test_normal_postrenewal_validation_metadata_records_one_attempt(record, transport):
    _, metadata = recovery.recover_legacy_session(record)
    assert metadata["session_validation_attempts"] == 1 and metadata["session_validation_retries"] == 0


@pytest.mark.parametrize("error", [
    ProxyCountryError("transport_error", curl_code=7),
    ProxyCountryError("transport_error", curl_code=28),
    ProxyCountryError("transport_error", curl_code=56),
    ProxyCountryError("http_failure", http_status=503),
])
def test_same_candidate_country_read_retries_on_same_proxy_without_renewal(monkeypatch, error):
    sleeps = []
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: sleeps.append(seconds))
    candidate = object()
    proxy = object()
    calls = []
    def attach():
        calls.append((candidate, proxy))
        if len(calls) == 1:
            raise error
        return {"authenticated": True}
    result, attempts = recovery.retry_readonly_validation(attach)
    assert result == {"authenticated": True} and attempts == 2
    assert calls == [(candidate, proxy), (candidate, proxy)]
    assert sleeps == [0.5]


@pytest.mark.parametrize("kind,field", [("http_failure", "http_status"), ("transport_error", "proxy_connect_http_status")])
def test_same_candidate_country_429_uses_shared_country_cooldown_without_rotation(monkeypatch, kind, field):
    clock = [100.0]
    monkeypatch.setattr(provider_recovery, "_clock", lambda: clock[0])
    monkeypatch.setattr(provider_recovery.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    error = ProxyCountryError(kind, **{field: 429}, retry_after_seconds=30)
    calls = []
    same_proxy = object()
    def attach():
        calls.append(same_proxy)
        if len(calls) == 1:
            raise error
        return True
    result, attempts = recovery.retry_readonly_validation(attach)
    assert result is True and attempts == 2 and calls == [same_proxy, same_proxy]
    assert clock[0] == 130.0 and provider_recovery._deadlines["country_check"] == 130.0
    assert "anghami" not in provider_recovery._deadlines


def test_same_candidate_country_read_exhaustion_is_bounded_and_remains_pending_eligible(monkeypatch):
    monkeypatch.setattr(recovery.time, "sleep", lambda _seconds: None)
    calls = []
    error = ProxyCountryError("transport_error", curl_code=7)
    def attach():
        calls.append(True)
        raise error
    with pytest.raises(ProxyCountryError) as caught:
        recovery.retry_readonly_validation(attach)
    assert caught.value is error and calls == [True] * 3
    assert error.validation_read_attempts == 3 and error.validation_read_retries == 2
    assert recovery.read_only_validation_failure(error, allow_cooldown_refusal=True) is True


def test_same_candidate_country_long429_defers_without_further_get(monkeypatch):
    monkeypatch.setattr(provider_recovery.time, "sleep", lambda _seconds: pytest.fail("Long cooldown must defer immediately"))
    calls = []
    error = ProxyCountryError("http_failure", http_status=429, retry_after_seconds=600)
    def attach():
        calls.append(True)
        raise error
    with pytest.raises(ProxyCountryError):
        recovery.retry_readonly_validation(attach)
    assert calls == [True] and error.validation_read_attempts == 1
    assert recovery.read_only_validation_failure(error, allow_cooldown_refusal=True) is True


@pytest.mark.parametrize("error", [
    ProxyCountryError("country_unverified"), ProxyCountryError("route_unverified"),
    ProxyCountryError("response_invalid"), ProxyCountryError("authentication_rejected", proxy_connect_http_status=407),
    ProxyCountryError("transport_error", curl_code=60),
    ProxyCountryError("transport_error", curl_code=7, http_status=403),
    ProxyCountryError("transport_error", curl_code=7, proxy_connect_http_status=407),
    ProxyCountryError("transport_error"), ProxyCountryError("http_failure", http_status=404),
    ProxyCountryError("transport_error", curl_code=7, retry_safe=False),
])
def test_country_validation_wrong_route_auth_tls_unknown_errors_are_not_retried(error):
    calls = []
    def attach():
        calls.append(True)
        raise error
    with pytest.raises(ProxyCountryError) as caught:
        recovery.retry_readonly_validation(attach)
    assert caught.value is error and calls == [True]
    assert recovery.read_only_validation_failure(error, allow_cooldown_refusal=True) is False


def test_country_cooldown_refusal_is_pending_eligible_but_never_retried():
    error = ProxyCountryError("http_failure", http_status=429, retry_safe=False)
    assert recovery.read_only_validation_failure(error) is False
    assert recovery.read_only_validation_failure(error, allow_cooldown_refusal=True) is True
    # Country proof cannot be replaced with an untyped request-stage claim.
    claim = RequestFailure("request_transport_failed", stage="country_check", curl_code=7)
    assert recovery.read_only_validation_failure(claim, allow_cooldown_refusal=True) is False

