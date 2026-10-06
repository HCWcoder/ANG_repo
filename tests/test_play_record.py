"""Bounded play-record checks use synthetic identities and local responses only."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import traceback
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from anghami_session import accounts, media_gateway, play_record, vault as vault_module
from anghami_session.errors import RequestFailure, SessionError, safe_request_failure


@pytest.mark.parametrize("code", ["request_transport_failed", "request_rate_limited", "session_authentication_rejected"])
def test_typed_preflight_failure_is_preserved_without_renewal_or_event(saved_session, bootstrap, tmp_path, code):
    failure = RequestFailure(code, stage="relations", http_status=429 if code == "request_rate_limited" else None)
    saved_session.check = lambda **options: (_ for _ in ()).throw(failure)
    path = tmp_path / "synthetic-play-failure.json"
    with pytest.raises(RequestFailure) as caught:
        play_record.run_play_record_test(saved_session, play_record.TEST_SONG_ID, report_path=path)
    assert caught.value is failure
    report = json.loads(path.read_text())
    assert report["session_failure"] == safe_request_failure(failure)
    assert report["renewal_attempted"] is False and report["event_attempted"] is False
    assert saved_session._http.calls == [] and bootstrap == []


def test_renewal_intent_is_durable_before_media_bootstrap(saved_session, tmp_path, monkeypatch):
    path = tmp_path / "synthetic-renewal-failure.json"
    def bootstrap(gateway):
        report = json.loads(path.read_text())
        assert report["renewal_attempted"] is True and report["renewal_completed"] is False
        assert report["event_attempted"] is False
        raise RequestFailure("request_transport_failed", stage="identity", retry_safe=False)
    monkeypatch.setattr(media_gateway.PlaybackGateway, "bootstrap", bootstrap)
    with pytest.raises(RequestFailure):
        play_record.run_play_record_test(saved_session, play_record.TEST_SONG_ID, report_path=path)
    assert saved_session._http.calls == []


@pytest.mark.parametrize("phase", ["identity", "metadata", "event"])
def test_shared_cooldown_refusal_prevents_request_before_renewal_or_event_intent(saved_session, bootstrap, tmp_path, monkeypatch, phase):
    calls = []
    def gate(stage):
        calls.append(stage)
        if phase == "identity":
            return stage != "identity"
        return calls.count("song_metadata") < (1 if phase == "metadata" else 2)
    monkeypatch.setattr(play_record, "wait_before_provider_request", gate)
    saved_session._http.replies.append(Reply(metadata()))
    path = tmp_path / "synthetic-cooldown.json"
    with pytest.raises(RequestFailure):
        play_record.run_play_record_test(saved_session, play_record.TEST_SONG_ID, report_path=path)
    report = json.loads(path.read_text())
    assert report["event_attempted"] is False and report["event_attempts"] == 0
    assert len(saved_session._http.calls) == (1 if phase == "event" else 0)
    assert report["renewal_attempted"] is (phase != "identity")
    assert len(bootstrap) == (0 if phase == "identity" else 1)


@pytest.mark.parametrize("operation", ["metadata", "event"])
def test_actual_rate_limit_observes_retry_after_without_replaying_event(saved_session, bootstrap, tmp_path, monkeypatch, operation):
    observed = []
    monkeypatch.setattr(play_record, "observe_provider_failure", lambda failure: observed.append(safe_request_failure(failure)))
    response = Reply({}, status=429)
    response.headers = {"retry-after": "90"}
    if operation == "event":
        saved_session._http.replies.append(Reply(metadata()))
    saved_session._http.replies.append(response)
    path = tmp_path / "synthetic-actual-rate.json"
    with pytest.raises(SessionError):
        play_record.run_play_record_test(saved_session, play_record.TEST_SONG_ID, report_path=path)
    assert len(observed) == 1 and observed[0]["retry_after_seconds"] == 90
    assert observed[0]["http_status"] == 429 and observed[0]["retryable"] is False
    report = json.loads(path.read_text())
    assert report["event_attempted"] is (operation == "event")
    if operation == "event":
        assert report["event_result"] == "unknown" and report["event_attempts"] == 1


SONG_ID = "1263607749"
WRONG_SONG_ID = str(int(SONG_ID) + 1)
SECRET = "synthetic-session-secret-must-not-appear"
FINGERPRINT = "synthetic-fingerprint-secret"
COOKIE = "synthetic-cookie-secret"
SOCKET = "synthetic-issued-socket-secret"


class Reply:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = status == 200
        self.reason = "OK" if self.ok else "Synthetic failure"
        self.content = b"invalid synthetic JSON" if isinstance(payload, Exception) else json.dumps(payload).encode()
        self.text = self.content.decode()
        self.request_size = 321
        self.upload_size = 0
        self.download_size = len(self.content)
        self.header_size = 67
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)

    def close(self):
        self.closed = True


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.replies = []
        self.on_event = None
        self.retry = SimpleNamespace(count=4)

    def get(self, url, **options):
        params = dict(options.get("params", {}))
        if not params:
            params = {name: values[-1] for name, values in parse_qs(urlsplit(url).query).items()}
        operation = params.get("type")
        assert operation in {"GETsong", "REGISTERwebplay"}
        assert self.retry.count == 0, "Automatic retries must be disabled"
        self.calls.append((url, params, options))
        if operation == "REGISTERwebplay" and self.on_event:
            self.on_event()
        assert self.replies, "Unexpected additional request"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def post(self, *_args, **_kwargs):
        pytest.fail("The bootstrap is mocked; no unplanned POST is allowed")


@pytest.fixture
def saved_session():
    template = {
        "url": (
            media_gateway.GATEWAY_URL + "?type=GETuserrelations&sid=" + SECRET
            + "&appsid=" + SECRET + "&fingerprint=" + FINGERPRINT
            + "&language=en&lang=en&web2=true"
        ),
        "headers": {
            "cookie": "appsidsave=" + COOKIE + "; fingerprint=" + FINGERPRINT,
            "user-agent": "Synthetic Chrome/152.0.0.0 agent",
            "origin": "https://play.anghami.com",
        },
    }
    checks = []

    def check(*, negative_control):
        checks.append(negative_control)
        return {
            "authenticated": True,
            "without_session": {"authentication_rejected": True},
            "operations": {"relations": "ok", "playlists": "ok"},
        }

    return SimpleNamespace(
        _saved={
            "format_version": 1, "created_at_utc": "2026-09-30T00:00:00+00:00",
            "origin": "https://play.anghami.com", "account_email": "test@example.com",
            "requests": {"relations": {"method": "GET", **template}},
        },
        _template=lambda operation: template if operation == "relations" else None,
        _http=FakeHTTP(), check=check, checks=checks,
    )


@pytest.fixture
def authentication():
    return {
        "email": "TEST@example.com ", "reqkey": "R" * 32, "reskey": "S" * 32,
        "signingkey": "synthetic-signing-secret", "socketsessionid": SOCKET,
    }


@pytest.fixture
def bootstrap(monkeypatch, authentication):
    calls = []

    def post(self, operation, payload, *, authenticate=False):
        assert operation == "authenticate" and authenticate is True
        calls.append(operation)
        return {"status": "ok", "authenticate": deepcopy(authentication)}

    monkeypatch.setattr(media_gateway.PlaybackGateway, "_post", post)
    return calls


@pytest.mark.parametrize("row", [-1, 0, 6, 100, 38599])
def test_vault_cohort_guard_runs_before_loading_session(row):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    selected.session = lambda *_: pytest.fail("Out-of-cohort account session was loaded")
    with pytest.raises(SessionError, match="selected test accounts"):
        selected.test_play_record(row, SONG_ID)


def test_declared_test_cohort_contains_only_original_five_and_explicit_row_seven():
    assert play_record.TEST_ACCOUNT_ROWS == frozenset({1, 2, 3, 4, 5, 7})
    assert isinstance(play_record.TEST_ACCOUNT_ROWS, frozenset)


def test_vault_accepts_explicit_row_seven_with_its_saved_session(monkeypatch, tmp_path):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    selected.path = tmp_path / "synthetic.sqlite3"
    bundle = {"synthetic": "saved session"}
    calls = []
    closed = []

    def load_session(row):
        calls.append(("session", row))
        return bundle

    class FakeSession:
        def __init__(self, *, saved):
            assert saved is bundle

        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.append(True)

    def run_test(session, song_id, *, report_path):
        assert isinstance(session, FakeSession)
        calls.append(("run", song_id, report_path))
        return {"song_id": song_id, "event_accepted": True, "synthetic": True}

    selected.session = load_session
    monkeypatch.setattr(vault_module, "AnghamiSession", FakeSession)
    monkeypatch.setattr(play_record, "run_play_record_test", run_test)
    result = selected.test_play_record(7, SONG_ID)
    path = tmp_path / "account-7.test-play-record-report.json"
    assert SONG_ID == play_record.TEST_SONG_ID
    assert calls == [("session", 7), ("run", SONG_ID, path)]
    assert closed == [True]
    assert result == {
        "source_row": 7, "song_id": SONG_ID, "event_accepted": True, "synthetic": True,
    }
    assert json.loads(path.read_text(encoding="utf-8")) == result


@pytest.mark.parametrize("song_id", ["42", "", WRONG_SONG_ID, SONG_ID + "&songid=42"])
def test_vault_song_guard_runs_before_loading_session(song_id):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    selected.session = lambda *_: pytest.fail("Session loaded for another track")
    with pytest.raises(SessionError, match="declared test song"):
        selected.test_play_record(1, song_id)


@pytest.mark.parametrize("row", [5, 7])
def test_account_cli_routes_only_selected_row_and_song(monkeypatch, tmp_path, capsys, row):
    calls = []

    class FakeVault:
        def __init__(self, path):
            calls.append(("vault", path))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def record(self, row):
            calls.append(("record", row))
            return {"email": "test@example.com", "password": SECRET}

        def test_play_record(self, row, song_id):
            calls.append(("test_play_record", row, song_id))
            return {"source_row": row, "song_id": song_id, "api_accepted": True}

    monkeypatch.setattr(accounts, "AccountVault", FakeVault)
    selected_path = tmp_path / "synthetic.sqlite3"
    assert accounts.main([
        "test-play-record", "--row", str(row), "--song-id", SONG_ID,
        "--vault", str(selected_path),
    ]) == 0
    assert calls == [
        ("vault", selected_path), ("record", row), ("test_play_record", row, SONG_ID),
    ]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out)["source_row"] == row


def metadata():
    return {"status": 1, "id": SONG_ID, "duration": "114.99", "title": SECRET}


def safe_report(path):
    text = path.read_text(encoding="utf-8")
    for secret in (SECRET, FINGERPRINT, COOKIE, SOCKET, "synthetic-signing-secret", "test@example.com"):
        assert secret not in text
    return json.loads(text)


def test_original_function_runs_once_with_saved_auth_and_pre_attempt_audit(saved_session, bootstrap, tmp_path, monkeypatch):
    path = tmp_path / "audit.json"
    replies = [Reply(metadata()), Reply({"status": "ok", "unused_private_field": SECRET})]
    saved_session._http.replies.extend(replies)
    original_retry = saved_session._http.retry
    journal_at_send = []
    saved_session._http.on_event = lambda: journal_at_send.append(safe_report(path))
    load = play_record._load_legacy_functions
    original_calls = []

    def load_with_spy():
        functions = load()
        original = functions["play_song"]
        assert Path(original.__code__.co_filename).name == "send_vote.py"
        assert original.__globals__ is functions

        def called(*args):
            original_calls.append(args[1:])
            return original(*args)

        functions["play_song"] = called
        return functions

    monkeypatch.setattr(play_record, "_load_legacy_functions", load_with_spy)
    report = play_record.run_play_record_test(saved_session, SONG_ID, report_path=path)
    assert original_calls == [(SONG_ID, FINGERPRINT, SECRET)]
    assert saved_session.checks == [True]
    assert bootstrap == ["authenticate"]
    assert saved_session._http.retry is original_retry
    assert len(journal_at_send) == 1
    before = journal_at_send[0]
    assert before["phase"] == "event"
    assert before["event_attempts"] == 1 and before["event_attempted"] is True
    assert before["event_accepted"] is None and before["event_result"] == "unknown"
    assert before["server_account_identity_verified"] and before["metadata_verified"]
    assert [params["type"] for _, params, _ in saved_session._http.calls] == ["GETsong", "REGISTERwebplay"]
    for url, params, options in saved_session._http.calls:
        assert url == media_gateway.GATEWAY_URL
        assert params["sid"] == params["appsid"] == SECRET
        assert params["fingerprint"] == FINGERPRINT
        assert options["headers"] == saved_session._template("relations")["headers"]
        assert options["timeout"] == 25
        assert options["allow_redirects"] is False
    event = saved_session._http.calls[1][1]
    assert event["songid"] == SONG_ID
    assert 114.99 - 0.000001 <= float(event["playsecs"]) <= 115.0
    assert event["playper"] == "1"
    assert str(int(event["localtimestamp"])) == event["localtimestamp"]
    assert report == safe_report(path)
    assert report["passed"] and report["synthetic"]
    assert report["event_accepted"] is True and report["event_result"] == "accepted"
    assert report["api_status"] == "ok"
    assert report["event_attempts"] == 1
    assert report["downstream_statistics_verified"] is False
    assert report["browser_required"] is report["password_required"] is False
    assert report["audio_bytes"] == 0
    assert report["automatic_retry"] is False
    bandwidth = report["bandwidth"]
    for function, reply in zip(("get_song", "play_song"), replies):
        assert bandwidth[function] == {
            "request_count": 1, "request_bytes": 321, "upload_body_bytes": 0,
            "download_body_bytes": len(reply.content), "response_header_bytes": 67,
            "measurement_complete": True,
        }
    assert bandwidth["total"]["request_count"] == 2
    assert bandwidth["total"]["request_bytes"] == 642
    assert bandwidth["total"]["download_body_bytes"] == sum(len(reply.content) for reply in replies)
    assert bandwidth["total"]["response_header_bytes"] == 134
    assert bandwidth["total"]["measurement_complete"] is True
    assert all(reply.closed for reply in replies)


@pytest.mark.parametrize("song_id", ["42", "", WRONG_SONG_ID, SONG_ID + "&songid=42"])
def test_core_track_guard_prevents_all_authentication(saved_session, bootstrap, tmp_path, song_id):
    with pytest.raises(SessionError, match="restricted to test song"):
        play_record.run_play_record_test(saved_session, song_id, report_path=tmp_path / "audit.json")
    assert saved_session.checks == [] and bootstrap == [] and saved_session._http.calls == []


@pytest.mark.parametrize("identity", [None, "", "other@example.com"])
def test_server_identity_mismatch_prevents_metadata_and_event(saved_session, bootstrap, authentication, tmp_path, identity):
    authentication["email"] = identity
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError):
        play_record.run_play_record_test(saved_session, SONG_ID, report_path=path)
    report = safe_report(path)
    assert bootstrap == ["authenticate"]
    assert report["failed_phase"] == "account_identity"
    assert report["server_account_identity_verified"] is False
    assert report["event_attempted"] is False and report["event_attempts"] == 0
    assert saved_session._http.calls == []


@pytest.mark.parametrize("checked", [
    None, {"authenticated": False},
    {"authenticated": True, "without_session": {"authentication_rejected": False}},
    {"authenticated": True, "without_session": {}},
])
def test_preflight_requires_positive_auth_and_rejected_negative_control(saved_session, bootstrap, tmp_path, checked):
    saved_session.check = lambda **_kwargs: checked
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError):
        play_record.run_play_record_test(saved_session, SONG_ID, report_path=path)
    assert safe_report(path)["event_attempts"] == 0
    assert bootstrap == [] and saved_session._http.calls == []


@pytest.mark.parametrize("change", ["identity", "fingerprint", "duplicate_sid", "different_appsid"])
def test_ambiguous_or_unbound_saved_credentials_fail_before_preflight(saved_session, bootstrap, tmp_path, change):
    saved = saved_session._saved
    template = saved["requests"]["relations"]
    if change == "identity":
        del saved["account_email"]
    elif change == "fingerprint":
        template["url"] = template["url"].replace("&fingerprint=" + FINGERPRINT, "")
    elif change == "duplicate_sid":
        template["url"] += "&sid=another-synthetic-session"
    else:
        template["url"] = template["url"].replace("&appsid=" + SECRET, "&appsid=another-synthetic-session")
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError):
        play_record.run_play_record_test(saved_session, SONG_ID, report_path=path)
    assert safe_report(path)["event_attempts"] == 0
    assert saved_session.checks == [] and bootstrap == [] and saved_session._http.calls == []


@pytest.mark.parametrize("payload", [
    {"status": "failed", "id": SONG_ID, "duration": "114.99"},
    {"status": True, "id": SONG_ID, "duration": "114.99"},
    {"status": 1, "id": "42", "duration": "114.99"},
    {"status": 1, "id": SONG_ID},
    {"status": 1, "id": SONG_ID, "duration": 0},
    {"status": 1, "id": SONG_ID, "duration": True},
    {"status": 1, "id": SONG_ID, "duration": "nan"},
    {"status": 1, "id": SONG_ID, "duration": "inf"},
    {"status": 1, "id": SONG_ID, "duration": "-1"},
    {"status": 1, "id": SONG_ID, "duration": "114.99", "error": SECRET},
])
def test_failed_or_mismatched_metadata_cannot_send_a_record(saved_session, bootstrap, tmp_path, payload):
    saved_session._http.replies.append(Reply(payload))
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError):
        play_record.run_play_record_test(saved_session, SONG_ID, report_path=path)
    assert [params["type"] for _, params, _ in saved_session._http.calls] == ["GETsong"]
    report = safe_report(path)
    assert report["metadata_verified"] is False
    assert report["event_attempts"] == 0 and report["event_attempted"] is False
    assert report["event_result"] == "not_attempted"


@pytest.mark.parametrize("message", [
    "You cannot play this song in the country because of license rights.",
    "YOU CANNOT PLAY THIS SONG IN THE COUNTRY BECAUSE OF LICENSE RIGHTS.",
])
def test_regional_license_rejection_has_fixed_safe_error_and_sends_no_event(saved_session, bootstrap, tmp_path, message):
    response = Reply({"status": "failed", "message": message + " Private diagnostic: " + SECRET})
    saved_session._http.replies.append(response)
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError) as error:
        play_record.run_play_record_test(saved_session, SONG_ID, report_path=path)
    assert str(error.value) == (
        "Anghami reports that this song is unavailable in the current country "
        "because of licensing rights. No play event was sent."
    )
    assert SECRET not in "".join(traceback.format_exception(error.value))
    report = safe_report(path)
    assert report["error_code"] == "metadata_region_unavailable"
    assert report["failed_phase"] == "metadata"
    assert report["metadata_verified"] is False
    assert report["event_attempts"] == 0 and report["event_attempted"] is False
    assert report["event_result"] == "not_attempted"
    assert [params["type"] for _, params, _ in saved_session._http.calls] == ["GETsong"]
    assert report["bandwidth"]["play_song"]["request_count"] == 0
    assert response.closed


@pytest.mark.parametrize("message", [
    "You cannot play this song in the country.",
    "The account cannot fetch license rights.",
])
def test_other_metadata_errors_keep_generic_classification(saved_session, bootstrap, tmp_path, message):
    saved_session._http.replies.append(Reply({"status": "failed", "message": message + SECRET}))
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError) as error:
        play_record.run_play_record_test(saved_session, SONG_ID, report_path=path)
    assert SECRET not in str(error.value)
    report = safe_report(path)
    assert report["error_code"] == "metadata_invalid"
    assert report["metadata_verified"] is False and report["event_attempts"] == 0
    assert [params["type"] for _, params, _ in saved_session._http.calls] == ["GETsong"]


@pytest.mark.parametrize("event_response", [
    RuntimeError("Timeout carrying " + SECRET),
    Reply({"status": "ok", "private": SECRET}, status=500),
    Reply({"status": "ok"}, status=302),
    Reply(ValueError("JSON failed for " + SECRET)),
    Reply(["invalid", SECRET]),
    Reply({"message": SECRET}),
    Reply({"status": "unexpected-status", "message": SECRET}),
])
def test_uncertain_event_response_is_unknown_and_never_retried(saved_session, bootstrap, tmp_path, event_response):
    saved_session._http.replies.extend([Reply(metadata()), event_response])
    path = tmp_path / "audit.json"
    original_retry = saved_session._http.retry
    with pytest.raises(SessionError) as error:
        play_record.run_play_record_test(saved_session, SONG_ID, report_path=path)
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert saved_session._http.retry is original_retry
    assert [params["type"] for _, params, _ in saved_session._http.calls] == ["GETsong", "REGISTERwebplay"]
    report = safe_report(path)
    assert report["passed"] is False
    assert report["event_attempted"] is True and report["event_attempts"] == 1
    assert report["event_accepted"] is None
    assert report["event_result"] == report["api_status"] == "unknown"
    assert report["automatic_retry"] is False and report["downstream_statistics_verified"] is False
    assert report["bandwidth"]["play_song"]["request_count"] == 1
    if isinstance(event_response, Exception):
        assert report["bandwidth"]["play_song"]["measurement_complete"] is False
        assert report["bandwidth"]["play_song"]["request_bytes"] is None
        assert report["bandwidth"]["total"]["request_bytes"] is None


def test_explicit_event_rejection_is_recorded_without_retry(saved_session, bootstrap, tmp_path):
    saved_session._http.replies.extend([Reply(metadata()), Reply({"status": "failed", "error": SECRET})])
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError):
        play_record.run_play_record_test(saved_session, SONG_ID, report_path=path)
    report = safe_report(path)
    assert report["event_attempts"] == 1
    assert report["event_result"] == "rejected" and report["event_accepted"] is False
    assert report["api_status"] == "failed"
    assert len(saved_session._http.calls) == 2


def test_failed_pre_attempt_journal_prevents_event_transport(saved_session, bootstrap, tmp_path, monkeypatch):
    saved_session._http.replies.append(Reply(metadata()))
    journal = play_record._journal

    def fail_when_event_is_armed(report, path):
        if report["phase"] == "event":
            raise SessionError("Synthetic journal write failure")
        journal(report, path)

    monkeypatch.setattr(play_record, "_journal", fail_when_event_is_armed)
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError):
        play_record.run_play_record_test(saved_session, SONG_ID, report_path=path)
    assert [params["type"] for _, params, _ in saved_session._http.calls] == ["GETsong"]
    assert safe_report(path)["passed"] is False


def test_adapter_rejects_second_event_after_first_unknown_attempt(saved_session, tmp_path):
    report = {
        "bandwidth": {name: play_record._empty_counters() for name in ("get_song", "play_song", "total")},
    }
    adapter = play_record._LegacyAdapter(
        saved_session._http, saved_session._template("relations")["headers"], SECRET,
        FINGERPRINT, report, tmp_path / "audit.json",
    )
    adapter.duration = 114.99
    params = {
        "type": "REGISTERwebplay", "angh_type": "REGISTERwebplay", "sid": SECRET,
        "appsid": SECRET, "fingerprint": FINGERPRINT, "songid": SONG_ID,
        "playsecs": "114.99", "playper": "1",
    }
    saved_session._http.retry = SimpleNamespace(count=0)
    saved_session._http.replies.append(RuntimeError("Unknown network outcome " + SECRET))
    with pytest.raises(SessionError, match="transport"):
        adapter.get(media_gateway.GATEWAY_URL, params=params, headers={})
    with pytest.raises(SessionError, match="one event"):
        adapter.get(media_gateway.GATEWAY_URL, params=params, headers={})
    assert len(saved_session._http.calls) == 1 and report["event_attempts"] == 1


def test_original_loader_does_not_require_audio_browser_or_legacy_module_imports():
    script = r'''
import importlib.abc
import sys

blocked = {"av", "cloakbrowser", "playwright", "send_vote", "register"}
class RejectExtraImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise AssertionError("Unexpected module import: " + fullname)

sys.meta_path.insert(0, RejectExtraImports())
from anghami_session import play_record
functions = play_record._load_legacy_functions()
assert set(functions) == {"__builtins__", "dt", "random", "GATEWAY_URL",
                         "get_song", "play_song", "build_gateway_params", "assert_response_ok"}
assert functions["play_song"].__code__.co_filename.endswith("send_vote.py")
assert not blocked.intersection(sys.modules)
'''
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_explicit_alternate_declared_song_runs_original_function_with_only_that_id(saved_session, bootstrap, tmp_path):
    alternate = "1280677978"
    saved_session._http.replies.extend([
        Reply({"status": 1, "id": alternate, "duration": "114.99"}), Reply({"status": "ok"}),
    ])
    path = tmp_path / "alternate-song-audit.json"
    report = play_record.run_play_record_test(saved_session, alternate, declared_song_id=alternate, report_path=path)
    assert report["passed"] is True and report["song_id"] == alternate
    calls = saved_session._http.calls
    assert [params["type"] for _, params, _ in calls] == ["GETsong", "REGISTERwebplay"]
    assert calls[0][1]["songId"] == calls[1][1]["songid"] == alternate
    assert safe_report(path)["song_id"] == alternate
    assert play_record.TEST_SONG_ID == SONG_ID


def test_alternate_song_cannot_accept_default_song_metadata(saved_session, bootstrap, tmp_path):
    alternate = "1280677978"
    saved_session._http.replies.append(Reply(metadata()))
    path = tmp_path / "alternate-metadata-audit.json"
    with pytest.raises(SessionError, match="expected identity"):
        play_record.run_play_record_test(saved_session, alternate, declared_song_id=alternate, report_path=path)
    assert [params["type"] for _, params, _ in saved_session._http.calls] == ["GETsong"]
    report = safe_report(path)
    assert report["song_id"] == alternate and report["event_attempted"] is False


@pytest.mark.parametrize("operation", ["GETsong", "REGISTERwebplay"])
def test_alternate_play_adapter_rejects_old_song_before_transport(saved_session, tmp_path, operation):
    alternate = "1280677978"
    report = {"bandwidth": {name: play_record._empty_counters() for name in ("get_song", "play_song", "total")}}
    adapter = play_record._LegacyAdapter(
        saved_session._http, {}, SECRET, FINGERPRINT, report, tmp_path / "adapter-audit.json", song_id=alternate,
    )
    adapter.duration = 114.99
    params = {
        "type": operation, "angh_type": operation, "sid": SECRET, "appsid": SECRET,
        "fingerprint": FINGERPRINT, "songId" if operation == "GETsong" else "songid": SONG_ID,
        "playsecs": "114.99", "playper": "1",
    }
    with pytest.raises(SessionError, match="different song"):
        adapter.get(media_gateway.GATEWAY_URL, params=params, headers={})
    assert saved_session._http.calls == [] and adapter.event_attempts == adapter.metadata_requests == 0
    assert not (tmp_path / "adapter-audit.json").exists()
    default_adapter = play_record._LegacyAdapter(saved_session._http, {}, SECRET, FINGERPRINT, {}, None)
    assert adapter.song_id == alternate and default_adapter.song_id == SONG_ID


@pytest.mark.parametrize("requested,declared", [
    (SONG_ID, "1280677978"), ("1280677978", SONG_ID), (SONG_ID, "0"),
    (SONG_ID, "0123"), (SONG_ID, True), (SONG_ID, 2**63),
    (True, SONG_ID), ("١٢٣", SONG_ID), (SONG_ID + "&sid=" + SECRET, SONG_ID),
])
def test_invalid_or_mismatched_declaration_prevents_core_authentication(saved_session, bootstrap, tmp_path, requested, declared):
    with pytest.raises(SessionError, match="restricted to test song") as exc:
        play_record.run_play_record_test(saved_session, requested, declared_song_id=declared, report_path=tmp_path / "audit.json")
    assert SECRET not in str(exc.value)
    assert saved_session.checks == [] and bootstrap == [] and saved_session._http.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("method_name", ["test_play_record", "test_like"])
@pytest.mark.parametrize("requested,declared", [(SONG_ID, "1280677978"), (SONG_ID, True), (SONG_ID, "0123"), ("0", SONG_ID)])
def test_vault_declared_guard_precedes_cohort_session_and_proxy_loading(method_name, requested, declared):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    selected.enrolled_test_rows = lambda: pytest.fail("Invalid declared song reached cohort lookup")
    selected._http_session = lambda *_args, **_kwargs: pytest.fail("Invalid declared song reached a saved session or proxy")
    with pytest.raises(SessionError, match="declared test song"):
        getattr(selected, method_name)(7, requested, declared_song_id=declared, proxy=object())


@pytest.mark.parametrize("method_name,module_name,function_name", [
    ("test_play_record", "play_record", "run_play_record_test"),
    ("test_like", "like_test", "run_like_test"),
])
def test_vault_passes_canonical_alternate_declaration_to_core(tmp_path, monkeypatch, initialize_like_history_binding, method_name, module_name, function_name):
    from anghami_session import like_test

    alternate = "1280677978"
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    selected.path = tmp_path / "synthetic.sqlite3"
    if method_name == "test_like":
        initialize_like_history_binding(selected)
    selected.enrolled_test_rows = lambda: frozenset({7})
    calls = []

    class BoundSession:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            calls.append(("close",))

    def http_session(row, proxy, **options):
        calls.append(("session", row, proxy, options["song_id"]))
        return BoundSession()

    def run(session, song_id, *, report_path, declared_song_id):
        assert isinstance(session, BoundSession)
        calls.append(("core", song_id, declared_song_id))
        return {"passed": True, "song_id": song_id}

    selected._http_session = http_session
    monkeypatch.setattr(play_record if module_name == "play_record" else like_test, function_name, run)
    proxy = object()
    result = getattr(selected, method_name)(7, int(alternate), declared_song_id=int(alternate), proxy=proxy)
    assert result == {"source_row": 7, "passed": True, "song_id": alternate}
    assert calls == [("session", 7, proxy, alternate), ("core", alternate, alternate), ("close",)]
