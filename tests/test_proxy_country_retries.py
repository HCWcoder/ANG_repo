"""Bounded PacketStream preflight recovery uses synthetic transports and credentials."""

from collections import deque
import json

import pytest
from curl_cffi.const import CurlECode, CurlInfo
from curl_cffi.requests.exceptions import RequestException

from anghami_session import proxy
from anghami_session import country_preparation as country
from anghami_session import session_recovery as recovery
from anghami_session.errors import SessionError
from test_country_preparation_workers import imported, install_recovery


PRIVATE_USER = "synthetic-country-retry-user"
PRIVATE_KEY = "synthetic-country-retry-key"
PRIVATE_TEXT = "synthetic-country-retry-secret https://private.invalid/?sid=synthetic-session"


class Reply:
    def __init__(self, *, status=200, payload=None, infos=None):
        self.status_code = status
        self.payload = {"country": "EG", "private": PRIVATE_KEY} if payload is None else payload
        self.infos = {CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 200} if infos is None else infos
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self):
        self.closed = True


@pytest.fixture
def offline_http(monkeypatch):
    state = {"outcomes": deque(), "instances": [], "calls": [], "backoffs": []}

    class Transport:
        def __init__(self, **options):
            self.options, self.closed, self.calls = options, False, []
            state["instances"].append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.closed = True

        def get(self, url, **options):
            assert url == proxy.GEOLOCATION_URL
            assert "headers" not in options and "cookies" not in options
            assert options == {"timeout": 25, "allow_redirects": False}
            self.calls.append((url, options))
            state["calls"].append((self, url, options))
            assert len(self.calls) == 1
            assert state["outcomes"], "Preflight exceeded the explicit synthetic request budget"
            outcome = state["outcomes"].popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def post(self, *_args, **_options):
            pytest.fail("Country preflight attempted an account or mutation request")

    monkeypatch.setattr(proxy.requests, "Session", Transport)
    monkeypatch.setattr(proxy, "wait_country_lookup_start", lambda: True)
    monkeypatch.setattr(proxy.time, "sleep", lambda seconds: state["backoffs"].append(seconds))
    return state


def assert_private_safe(value):
    rendered = json.dumps(value) if isinstance(value, dict) else str(value)
    for secret in (PRIVATE_USER, PRIVATE_KEY, PRIVATE_TEXT, "private.invalid", "synthetic-session"):
        assert secret not in rendered


def assert_bound_closed(state, config):
    assert state["instances"] and all(instance.closed for instance in state["instances"])
    options = config.transport_options()
    assert all(instance.options == options for instance in state["instances"])
    assert all(instance.options["retry"] == 0 and instance.options["verify"] is True and instance.options["debug"] is False for instance in state["instances"])
    assert all("cookies" not in instance.options and "headers" not in instance.options for instance in state["instances"])
    assert len({id(instance) for instance in state["instances"]}) == len(state["instances"])


@pytest.mark.parametrize("code", [5, 6, 7, 18, 28, 35, 52, 55, 56, CurlECode.OPERATION_TIMEDOUT])
def test_known_transient_curl_failure_retries_cookie_free_on_same_sticky_route(code, offline_http, capsys):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    attached = Reply(status=0, infos={CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 200})
    success = Reply()
    offline_http["outcomes"].extend([RequestException(PRIVATE_TEXT, code=code, response=attached), success])
    proof = config.verify_country()
    assert proof["country"] == "EG" and proof["country_verified"] is True and proof["proxy_used"] is True
    assert proof["country_check_attempts"] == 2
    assert len(offline_http["calls"]) == 2 and offline_http["backoffs"] == [0.5]
    assert attached.closed and success.closed
    assert_bound_closed(offline_http, config)
    assert_private_safe(proof)
    assert capsys.readouterr().out == ""


def test_us_route_verifies_us_and_keeps_country_in_sticky_auth(offline_http):
    config = proxy.PacketStreamProxy.from_route(
        PRIVATE_USER, PRIVATE_KEY, "syntheticusroute", country="US",
    )
    offline_http["outcomes"].append(Reply(payload={"country": "US"}))
    proof = config.verify_country()
    assert proof["country"] == "US" and proof["country_verified"] is True
    assert proof["proxy_used"] is True
    assert offline_http["instances"][0].options["proxy_auth"] == (
        PRIVATE_USER, PRIVATE_KEY + "_country-US_session-syntheticusroute",
    )


def test_us_route_rejects_an_exit_in_the_wrong_country_with_safe_message(offline_http):
    config = proxy.PacketStreamProxy.from_route(
        PRIVATE_USER, PRIVATE_KEY, "syntheticusroute", country="US",
    )
    offline_http["outcomes"].append(Reply(payload={"country": "EG"}))
    with pytest.raises(proxy.ProxyCountryError, match="configured country"):
        config.verify_country()


@pytest.mark.parametrize("status", [502, 503, 504])
def test_transient_gateway_http_response_retries_without_following_redirects_or_changing_route(status, offline_http):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    failure, success = Reply(status=status, payload={"secret": PRIVATE_KEY}), Reply()
    offline_http["outcomes"].extend([failure, success])
    proof = config.verify_country()
    assert proof["country_verified"] is True
    assert proof["country_check_attempts"] == 2
    assert len(offline_http["calls"]) == 2 and offline_http["backoffs"] == [0.5]
    assert failure.closed and success.closed
    assert_bound_closed(offline_http, config)


def test_preflight_has_three_total_attempts_and_fixed_exhaustion_diagnostics(offline_http, capsys):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    responses = [Reply(status=0, infos={CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 200}) for _ in range(3)]
    offline_http["outcomes"].extend(RequestException(PRIVATE_TEXT, code=28, response=response) for response in responses)
    with pytest.raises(proxy.ProxyCountryError) as failure:
        config.verify_country()
    assert len(offline_http["calls"]) == 3 and offline_http["backoffs"] == [0.5, 1.0]
    assert not offline_http["outcomes"] and all(response.closed for response in responses)
    diagnostics = failure.value.diagnostics
    assert diagnostics["curl_code"] == 28
    assert diagnostics["proxy_connect_http_status"] == 200
    assert diagnostics["country_check_attempts"] == 3 and diagnostics["failure_kind"] == "transport_error"
    assert_private_safe(failure.value)
    assert_private_safe(diagnostics)
    assert_bound_closed(offline_http, config)
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_third_attempt_success_closes_both_failures_and_has_exactly_two_backoffs(offline_http):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    first, second, success = Reply(status=502), Reply(status=504), Reply()
    offline_http["outcomes"].extend([first, second, success])
    proof = config.verify_country()
    assert proof["country_verified"] is True
    assert proof["country_check_attempts"] == 3
    assert len(offline_http["calls"]) == 3 and offline_http["backoffs"] == [0.5, 1.0]
    assert first.closed and second.closed and success.closed
    assert_bound_closed(offline_http, config)


@pytest.mark.parametrize("code", [51, 60, 77, 83, 90, 0, 1, 999, True, "28", None])
def test_certificate_unknown_or_invalid_curl_codes_never_retry(code, offline_http):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    failure = RequestException(PRIVATE_TEXT, code=code)
    offline_http["outcomes"].append(failure)
    with pytest.raises(proxy.ProxyCountryError) as raised:
        config.verify_country()
    assert len(offline_http["calls"]) == 1 and offline_http["backoffs"] == []
    assert_private_safe(raised.value)
    assert_private_safe(raised.value.diagnostics)
    assert_bound_closed(offline_http, config)


@pytest.mark.parametrize("response", [
    Reply(status=302), Reply(status=403), Reply(status=429), Reply(status=500),
    Reply(status=200.0), Reply(infos={}),
    Reply(infos={CurlInfo.USED_PROXY: 0, CurlInfo.HTTP_CONNECTCODE: 200}),
    Reply(infos={CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 0}),
    Reply(payload={"country": "US"}), Reply(payload={"country": "eg"}),
    Reply(payload={"country": "EG", "error": PRIVATE_KEY}),
    Reply(payload=[]), Reply(payload={}), Reply(payload=ValueError(PRIVATE_TEXT)),
])
def test_definitive_http_country_tunnel_or_json_failures_never_retry(response, offline_http):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    offline_http["outcomes"].append(response)
    with pytest.raises(proxy.ProxyCountryError) as failure:
        config.verify_country()
    assert len(offline_http["calls"]) == 1 and offline_http["backoffs"] == []
    assert response.closed
    assert_private_safe(failure.value)
    assert_private_safe(failure.value.diagnostics)
    assert_bound_closed(offline_http, config)


@pytest.mark.parametrize("kind", ["origin", "connect", "exception", "exception_marker"])
def test_proxy_auth_rejection_wins_over_transient_curl_code_and_is_not_retried(kind, offline_http):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    response = Reply(status=407) if kind == "origin" else Reply(infos={CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 407})
    if kind in {"origin", "connect"}:
        outcome = response
    elif kind == "exception":
        outcome = RequestException(PRIVATE_TEXT, code=28, response=response)
    else:
        response = None
        outcome = RequestException("CONNECT tunnel failed, response 407. " + PRIVATE_TEXT, code=28)
    offline_http["outcomes"].append(outcome)
    with pytest.raises(proxy.ProxyCountryError, match="authentication was rejected.*407") as failure:
        config.verify_country()
    assert len(offline_http["calls"]) == 1 and offline_http["backoffs"] == []
    if response is not None:
        assert response.closed
    assert_private_safe(failure.value)
    assert_private_safe(failure.value.diagnostics)
    assert_bound_closed(offline_http, config)


@pytest.mark.parametrize("error", [RuntimeError(PRIVATE_TEXT), SessionError(PRIVATE_TEXT), ValueError(PRIVATE_TEXT)])
def test_arbitrary_exceptions_do_not_authorize_retry_or_expose_raw_text(error, offline_http):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    offline_http["outcomes"].append(error)
    with pytest.raises(proxy.ProxyCountryError) as failure:
        config.verify_country()
    assert len(offline_http["calls"]) == 1 and offline_http["backoffs"] == []
    assert_private_safe(failure.value)
    assert_private_safe(failure.value.diagnostics)
    assert_bound_closed(offline_http, config)


@pytest.mark.parametrize("outcome", [
    RequestException(PRIVATE_TEXT, code=28, response=Reply(status=403)),
    RequestException(PRIVATE_TEXT, code=28, response=Reply(status=429)),
    RequestException(PRIVATE_TEXT, code=60, response=Reply(status=502)),
    Reply(status=502, infos={CurlInfo.USED_PROXY: 0, CurlInfo.HTTP_CONNECTCODE: 200}),
    Reply(status=503, infos={CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: 0}),
])
def test_definitive_status_certificate_or_route_evidence_overrides_transient_metadata(outcome, offline_http):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    offline_http["outcomes"].append(outcome)
    with pytest.raises(proxy.ProxyCountryError) as failure:
        config.verify_country()
    assert len(offline_http["calls"]) == 1 and offline_http["backoffs"] == []
    response = getattr(outcome, "response", outcome)
    assert response.closed
    assert_private_safe(failure.value.diagnostics)
    assert_bound_closed(offline_http, config)


def test_one_successful_preflight_remains_one_request_and_reports_one_attempt(offline_http):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    success = Reply()
    offline_http["outcomes"].append(success)
    proof = config.verify_country()
    assert proof["country_check_attempts"] == 1 and proof["country_verified"] is True
    assert len(offline_http["calls"]) == 1 and offline_http["backoffs"] == [] and success.closed
    assert_private_safe(proof)
    assert_bound_closed(offline_http, config)


def test_keyboard_interrupt_during_backoff_never_starts_another_attempt(offline_http, monkeypatch):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    attached = Reply(status=0, infos={})
    offline_http["outcomes"].append(RequestException(PRIVATE_TEXT, code=28, response=attached))

    def interrupted(_seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(proxy.time, "sleep", interrupted)
    with pytest.raises(KeyboardInterrupt):
        config.verify_country()
    assert len(offline_http["calls"]) == 1 and attached.closed
    assert_bound_closed(offline_http, config)


@pytest.mark.parametrize("kind", [PRIVATE_TEXT, None, True, {}, []])
def test_typed_country_error_rejects_unknown_failure_kind(kind):
    with pytest.raises(ValueError) as error:
        proxy.ProxyCountryError(kind)
    assert_private_safe(error.value)


@pytest.mark.parametrize("attempts", [0, 4, -1, None, True, "3", 1.0])
def test_typed_country_error_rejects_invalid_or_unbounded_attempt_counts(attempts):
    with pytest.raises(ValueError):
        proxy.ProxyCountryError("transport_error", country_check_attempts=attempts)


def test_typed_country_error_reconstructs_only_fixed_validated_diagnostic_fields():
    error = proxy.ProxyCountryError(
        "transport_error", curl_code=CurlECode.OPERATION_TIMEDOUT,
        http_status=504, proxy_connect_http_status=200, country_check_attempts=3,
    )
    error.raw_error = PRIVATE_TEXT
    error.username, error.auth_key = PRIVATE_USER, PRIVATE_KEY
    assert proxy.safe_proxy_country_failure(error) == {
        "failure_kind": "transport_error", "curl_code": 28,
        "http_status": 504, "proxy_connect_http_status": 200, "country_check_attempts": 3,
    }
    assert_private_safe(error.diagnostics)
    assert_private_safe(error)


@pytest.mark.parametrize("field,value", [
    ("curl_code", True), ("curl_code", "28"), ("curl_code", -1), ("curl_code", 999),
    ("http_status", True), ("http_status", "504"), ("http_status", 0), ("http_status", 600),
    ("proxy_connect_http_status", True), ("proxy_connect_http_status", 0),
    ("proxy_connect_http_status", 200.0), ("proxy_connect_http_status", PRIVATE_TEXT),
])
def test_invalid_optional_diagnostic_values_are_discarded(field, value):
    error = proxy.ProxyCountryError("transport_error", **{field: value})
    assert field not in proxy.safe_proxy_country_failure(error)
    # Validate on extraction too, rather than trusting a subsequently mutated exception.
    setattr(error, field, value)
    assert field not in proxy.safe_proxy_country_failure(error)
    assert_private_safe(error.diagnostics)


def test_generic_error_cannot_spoof_typed_country_diagnostics():
    error = SessionError(PRIVATE_TEXT)
    error.failure_kind, error.curl_code, error.country_check_attempts = "transport_error", 28, 3
    assert proxy.safe_proxy_country_failure(error) == {}


def test_recovery_preserves_typed_country_failure_and_never_opens_account_transport(monkeypatch):
    error = proxy.ProxyCountryError("transport_error", curl_code=56, country_check_attempts=3)
    record = {
        "source_row": 1, "country": "EG", "email": "synthetic-retry@example.invalid",
        "password": PRIVATE_KEY,
        "legacy_metadata": {"appsidsave": "synthetic-legacy", "session_fingerprint": "synthetic-device"},
        "legacy_cookies": {"appsidsave": "synthetic-legacy", "fingerprint": "synthetic-device"},
    }

    class FailingProxy:
        country = "EG"

        def verify_country(self):
            raise error

    monkeypatch.setattr(recovery, "_RecoverySession", lambda **_: pytest.fail("Country failure reached account HTTP"))
    with pytest.raises(proxy.ProxyCountryError) as raised:
        recovery.recover_legacy_session(record, proxy=FailingProxy())
    assert raised.value is error
    assert raised.value.diagnostics["curl_code"] == 56


def test_country_checkpoint_keeps_safe_preflight_diagnostics_without_account_retry(imported, tmp_path, monkeypatch, offline_http):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    offline_http["outcomes"].extend(RequestException(PRIVATE_TEXT, code=56) for _ in range(3))
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda _path: config)
    install_recovery(monkeypatch, factory, lambda *_: pytest.fail("Failed preflight reached account recovery"))
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True, proxy_egypt=True, limit=1)
    expected = {
        "source_row": 1, "stage": "country_check", "code": "country_check_failed",
        "failure_kind": "transport_error", "curl_code": 56, "country_check_attempts": 3,
    }
    assert result["proxy_failure"] == expected and result["pause_reason"] == "limit_reached"
    assert result["counts"]["failed"] == result["consecutive_failures"] == 0
    assert result["counts"]["connection_pending"] == 1
    progress = country.load_progress(path, plan)
    assert progress["proxy_failure"] == expected
    assert progress["rows"][0]["attempts"] == 1
    assert not any(event[0] in {"record", "recover", "attach", "enroll"} for event in factory.events)
    assert len(offline_http["calls"]) == 3
    # Extra private fields from an untrusted local report are never propagated.
    raw = json.loads(path.read_text())
    raw["proxy_failure"].update(raw_error=PRIVATE_TEXT, username=PRIVATE_USER, auth_key=PRIVATE_KEY)
    path.write_text(json.dumps(raw), encoding="utf-8")
    reloaded = country.load_progress(path, plan)
    assert reloaded["proxy_failure"] == expected
    assert_private_safe(country.summarize(reloaded))


def test_country_transient_preflight_recovery_prepares_exactly_one_account_attempt(imported, tmp_path, monkeypatch, offline_http):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    offline_http["outcomes"].extend([RequestException(PRIVATE_TEXT, code=35), Reply(status=503), Reply()])
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda _path: config)
    install_recovery(monkeypatch, factory)
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True, proxy_egypt=True, limit=1)
    assert result["counts"]["ready"] == 1 and result["counts"]["failed"] == 0
    progress = country.load_progress(path, plan)
    assert progress["rows"][0]["attempts"] == 1 and progress["rows"][0]["state"] == "ready"
    assert [event[1] for event in factory.events if event[0] == "recover"] == [1]
    assert [event[1] for event in factory.events if event[0] == "attach"] == [1]
    assert len(offline_http["calls"]) == 3 and offline_http["backoffs"] == [0.5, 1.0]
    assert_bound_closed(offline_http, config)


@pytest.mark.parametrize("definitive", [
    Reply(payload={"country": "US"}), Reply(status=403),
    RequestException(PRIVATE_TEXT, code=60), RuntimeError(PRIVATE_TEXT),
])
def test_transient_first_attempt_does_not_allow_retry_of_a_definitive_second_failure(definitive, offline_http):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    offline_http["outcomes"].extend([RequestException(PRIVATE_TEXT, code=28), definitive])
    with pytest.raises(proxy.ProxyCountryError) as failure:
        config.verify_country()
    assert len(offline_http["calls"]) == 2 and offline_http["backoffs"] == [0.5]
    assert failure.value.diagnostics["country_check_attempts"] == 2
    assert_private_safe(failure.value.diagnostics)
    assert_bound_closed(offline_http, config)


def test_transient_gateway_exhaustion_is_bounded_and_reports_final_http_status(offline_http):
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    failures = [Reply(status=503) for _ in range(3)]
    offline_http["outcomes"].extend(failures)
    with pytest.raises(proxy.ProxyCountryError) as raised:
        config.verify_country()
    assert raised.value.diagnostics == {
        "failure_kind": "http_failure", "http_status": 503,
        "proxy_connect_http_status": 200, "country_check_attempts": 3,
    }
    assert len(offline_http["calls"]) == 3 and offline_http["backoffs"] == [0.5, 1.0]
    assert all(failure.closed for failure in failures)
    assert_bound_closed(offline_http, config)


@pytest.mark.parametrize("field,value", [
    ("failure_kind", PRIVATE_TEXT), ("failure_kind", None),
    ("country_check_attempts", True), ("country_check_attempts", 4), ("country_check_attempts", "3"),
    ("curl_code", True), ("curl_code", "28"), ("curl_code", 999),
    ("http_status", True), ("http_status", 0), ("http_status", "503"),
    ("proxy_connect_http_status", 200.0), ("proxy_connect_http_status", PRIVATE_TEXT),
])
def test_malformed_optional_proxy_diagnostics_in_checkpoint_are_rejected_without_rewrite(imported, tmp_path, field, value):
    _parent, plan, _source, _factory = imported
    path = tmp_path / "progress.json"
    progress = country._new_progress(plan)
    progress["proxy_failure"] = {
        "source_row": 1, "stage": "country_check", "code": "country_check_failed",
        "failure_kind": "transport_error", "curl_code": 28, "country_check_attempts": 3,
    }
    progress["proxy_failure"][field] = value
    path.write_text(json.dumps(progress), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SessionError):
        country.load_progress(path, plan)
    assert path.read_bytes() == before


def test_existing_account_recovery_error_is_never_retried_by_country_preflight_policy(imported, tmp_path, monkeypatch, offline_http):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    config = proxy.PacketStreamProxy(PRIVATE_USER, PRIVATE_KEY)
    offline_http["outcomes"].append(Reply())
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda _path: config)

    def failed_recovery(_row, _profile):
        raise RequestException(PRIVATE_TEXT, code=28)

    install_recovery(monkeypatch, factory, failed_recovery)
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True, proxy_egypt=True, limit=1)
    assert result["counts"]["failed"] == 1 and result["counts"]["ready"] == 0
    assert [event[1] for event in factory.events if event[0] == "recover"] == [1]
    assert not any(event[0] in {"attach", "enroll"} for event in factory.events)
    assert len(offline_http["calls"]) == 1 and offline_http["backoffs"] == []
    assert country.load_progress(path, plan)["rows"][0]["attempts"] == 1
    reports = list((factory.path.parent / "country-preparation-reports").rglob("account-1.redacted.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text())["automatic_retry"] is False
    assert_private_safe(result)
