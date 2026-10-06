"""Single-account like checks use synthetic sessions and offline HTTP responses."""

from copy import deepcopy
import json
from pathlib import Path
import traceback
from types import SimpleNamespace

import pytest

from anghami_session import accounts, like_test, media_gateway, play_record, vault as vault_module
from anghami_session.errors import RequestFailure, SessionError, safe_request_failure


@pytest.mark.parametrize("code", ["request_transport_failed", "request_rate_limited", "session_authentication_rejected"])
def test_typed_like_preflight_failure_is_preserved_without_renewal_or_append(saved_session, bootstrap, tmp_path, code):
    failure = RequestFailure(code, stage="relations", http_status=429 if code == "request_rate_limited" else None)
    saved_session.check = lambda **options: (_ for _ in ()).throw(failure)
    path = tmp_path / "synthetic-like-failure.json"
    with pytest.raises(RequestFailure) as caught:
        like_test.run_like_test(saved_session, play_record.TEST_SONG_ID, report_path=path)
    assert caught.value is failure
    report = json.loads(path.read_text())
    assert report["session_failure"] == safe_request_failure(failure)
    assert report["renewal_attempted"] is False and report["mutation_attempted"] is False
    assert saved_session._http.calls == [] and bootstrap == []


def test_like_renewal_intent_is_durable_before_media_bootstrap(saved_session, tmp_path, monkeypatch):
    path = tmp_path / "synthetic-like-renewal-failure.json"
    def bootstrap(gateway):
        report = json.loads(path.read_text())
        assert report["renewal_attempted"] is True and report["renewal_completed"] is False
        assert report["mutation_attempted"] is False
        raise RequestFailure("request_transport_failed", stage="identity", retry_safe=False)
    monkeypatch.setattr(media_gateway.PlaybackGateway, "bootstrap", bootstrap)
    with pytest.raises(RequestFailure):
        like_test.run_like_test(saved_session, play_record.TEST_SONG_ID, report_path=path)
    assert saved_session._http.calls == []


@pytest.mark.parametrize("phase", ["identity", "read", "mutation"])
def test_like_shared_cooldown_refusal_prevents_request_before_renewal_or_mutation_intent(saved_session, bootstrap, tmp_path, monkeypatch, phase):
    calls = []
    def gate(stage):
        calls.append(stage)
        if phase == "identity":
            return stage != "identity"
        return calls.count("likes_read") < (1 if phase == "read" else 3)
    monkeypatch.setattr(like_test, "wait_before_provider_request", gate)
    add_state(saved_session._http, ["42"])
    path = tmp_path / "synthetic-like-cooldown.json"
    with pytest.raises(RequestFailure):
        like_test.run_like_test(saved_session, play_record.TEST_SONG_ID, report_path=path)
    report = json.loads(path.read_text())
    assert report["mutation_attempted"] is False and report["mutation_attempts"] == 0
    assert len(saved_session._http.calls) == (2 if phase == "mutation" else 0)
    assert report["renewal_attempted"] is (phase != "identity")
    assert len(bootstrap) == (0 if phase == "identity" else 1)


@pytest.mark.parametrize("operation", ["read", "mutation"])
def test_actual_like_rate_limit_observes_retry_after_without_replaying_append(saved_session, bootstrap, tmp_path, monkeypatch, operation):
    observed = []
    monkeypatch.setattr(like_test, "observe_provider_failure", lambda failure: observed.append(safe_request_failure(failure)))
    response = Reply({}, status=429)
    response.headers = {"retry-after": "90"}
    if operation == "mutation":
        add_state(saved_session._http, ["42"])
    saved_session._http.replies.append(response)
    if operation == "mutation":
        add_state(saved_session._http, ["42"])
    path = tmp_path / "synthetic-like-actual-rate.json"
    with pytest.raises(SessionError):
        like_test.run_like_test(saved_session, play_record.TEST_SONG_ID, report_path=path)
    assert len(observed) == 1 and observed[0]["retry_after_seconds"] == 90
    assert observed[0]["http_status"] == 429 and observed[0]["retryable"] is False
    report = json.loads(path.read_text())
    assert report["mutation_attempted"] is (operation == "mutation")
    if operation == "mutation":
        assert report["mutation_result"] == "unknown" and report["mutation_attempts"] == 1


SONG_ID = play_record.TEST_SONG_ID
WRONG_SONG_ID = str(int(SONG_ID) + 1)
SECRET = "synthetic-like-test-secret-must-not-appear"
FINGERPRINT = "synthetic-like-fingerprint"
SOCKET = "synthetic-socket"
PLAYLIST_ID = "9876543"
LIKED_NAME = "$1234567890LIKED#"


class Reply:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.content = b"invalid JSON" if isinstance(payload, Exception) else json.dumps(payload).encode()
        self.request_size = 120
        self.upload_size = 0
        self.download_size = len(self.content)
        self.header_size = 55
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)

    def close(self):
        self.closed = True


class TransportFailure(RuntimeError):
    def __init__(self, code):
        super().__init__(SECRET)
        self.code = code


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.replies = []
        self.retry = SimpleNamespace(count=4)
        self.on_post = None

    def _request(self, method, url, options):
        assert self.retry.count == 0
        operation = options.get("params", {}).get("type")
        if method == "POST":
            operation = options.get("data", {}).get("type")
            assert operation == "PUTplaylist"
            if self.on_post:
                self.on_post()
        else:
            assert operation in {"GETplaylists", "GETplaylistdata"}
        self.calls.append((method, operation, url, options))
        assert self.replies, "Unexpected request"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def get(self, url, **options):
        return self._request("GET", url, options)

    def post(self, url, **options):
        return self._request("POST", url, options)


def discovery(song_ids):
    return {"status": "ok", "sections": [{
        "name": "playlists", "data": [{
            "name": LIKED_NAME, "id": PLAYLIST_ID, "count": len(song_ids),
        }],
    }]}


def playlist(song_ids):
    return {
        "status": "ok", "id": PLAYLIST_ID, "playlistid": int(PLAYLIST_ID),
        "name": "Translated liked songs", "PlaylistName": LIKED_NAME,
        # A hidden or unavailable member is absent from the visible metric.
        "PlaylistCount": max(0, len(song_ids) - 1),
        "responsemode": "buffered", "responsetype": "GETplaylistdata",
        "songorder": ",".join(song_ids), "songbuffers": [],
        "limit_number_songs": 5000,
        "sections": [{
            "type": "song", "displaytype": "list", "buffered": True, "data": [],
        }],
    }


@pytest.fixture
def saved_session():
    template = {
        "method": "GET",
        "url": (
            media_gateway.GATEWAY_URL + "?type=GETuserrelations&sid=" + SECRET
            + "&appsid=" + SECRET + "&fingerprint=" + FINGERPRINT
            + "&language=en&lang=en&web2=true"
        ),
        "headers": {
            "cookie": "appsidsave=" + SECRET + "; fingerprint=" + FINGERPRINT,
            "user-agent": "Synthetic Chrome/152.0.0.0 agent",
            "origin": "https://play.anghami.com",
        },
    }
    saved = {
        "format_version": 1, "created_at_utc": "2026-10-01T00:00:00+00:00",
        "origin": "https://play.anghami.com", "account_email": "synthetic@example.com",
        "requests": {"relations": template},
    }
    checks = []

    def check(*, negative_control):
        checks.append(negative_control)
        return {"authenticated": True, "without_session": {"authentication_rejected": True}}

    return SimpleNamespace(
        _saved=saved, _http=FakeHTTP(), checks=checks, check=check,
        _template=lambda name: saved["requests"][name],
        song=lambda song_id: {"status": 1, "id": str(song_id), "duration": "114.99"},
    )


@pytest.fixture
def bootstrap(monkeypatch):
    calls = []

    def post(self, operation, payload, *, authenticate=False):
        assert operation == "authenticate" and authenticate is True
        calls.append(operation)
        return {"status": "ok", "authenticate": {
            "email": "synthetic@example.com", "reqkey": "R" * 32,
            "reskey": "S" * 32, "socketsessionid": SOCKET,
            "signingkey": "synthetic-signing-key",
        }}

    monkeypatch.setattr(media_gateway.PlaybackGateway, "_post", post)
    return calls


@pytest.mark.parametrize("row", [0, 6, 100, 38599])
def test_like_account_guard_prevents_saved_session_loading(row):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    selected.session = lambda *_: pytest.fail("Out-of-cohort account session loaded")
    with pytest.raises(SessionError, match="selected test accounts"):
        selected.test_like(row, SONG_ID)


@pytest.mark.parametrize("song_id", ["", WRONG_SONG_ID, SONG_ID + "&songid=42"])
def test_like_track_guard_prevents_saved_session_loading(song_id):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    selected.session = lambda *_: pytest.fail("Session loaded for an undeclared track")
    with pytest.raises(SessionError, match="declared test song"):
        selected.test_like(7, song_id)


def test_like_cli_routes_selected_row_and_declared_song(monkeypatch, tmp_path, capsys):
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
            return {"email": "synthetic@example.com", "password": SECRET}

        def test_like(self, row, song_id):
            calls.append(("test_like", row, song_id))
            return {"source_row": row, "song_id": song_id, "liked_after": True}

    monkeypatch.setattr(accounts, "AccountVault", FakeVault)
    selected_path = tmp_path / "synthetic.sqlite3"
    assert accounts.main([
        "test-like", "--row", "7", "--song-id", SONG_ID,
        "--vault", str(selected_path),
    ]) == 0
    assert calls == [("vault", selected_path), ("record", 7), ("test_like", 7, SONG_ID)]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out) == {"source_row": 7, "song_id": SONG_ID, "liked_after": True}


def add_state(http, song_ids):
    replies = [Reply(discovery(song_ids)), Reply(playlist(song_ids))]
    http.replies.extend(replies)
    return replies


def safe_report(path):
    text = path.read_text(encoding="utf-8")
    for secret in (SECRET, FINGERPRINT, "synthetic@example.com", "synthetic-socket", "synthetic-signing-key"):
        assert secret not in text
    return json.loads(text)


def test_alternate_declared_like_ignores_existing_default_like_and_appends_selected_song(saved_session, bootstrap, tmp_path):
    alternate = "1280677978"
    metadata_calls = []

    def song(song_id):
        metadata_calls.append(song_id)
        return {"status": 1, "id": song_id, "duration": "114.99"}

    saved_session.song = song
    add_state(saved_session._http, [SONG_ID])
    saved_session._http.replies.append(Reply({"status": "ok"}))
    add_state(saved_session._http, [SONG_ID, alternate])
    path = tmp_path / "alternate-like-audit.json"
    report = like_test.run_like_test(saved_session, alternate, declared_song_id=alternate, report_path=path)
    assert metadata_calls == [alternate]
    assert report["passed"] and report["song_id"] == alternate
    assert report["liked_before"] is False and report["liked_after"] is True
    posts = [options for method, _, _, options in saved_session._http.calls if method == "POST"]
    assert len(posts) == 1 and posts[0]["data"]["songid"] == alternate
    assert safe_report(path)["song_id"] == alternate
    assert play_record.TEST_SONG_ID == SONG_ID


def test_alternate_declared_like_skips_selected_membership_with_default_song_absent(saved_session, bootstrap, tmp_path):
    alternate = "1280677978"
    add_state(saved_session._http, [alternate])
    report = like_test.run_like_test(saved_session, alternate, declared_song_id=alternate, report_path=tmp_path / "audit.json")
    assert report["passed"] and report["song_id"] == alternate
    assert report["liked_before"] is report["liked_after"] is True
    assert report["mutation_result"] == "skipped_already_liked"
    assert [method for method, _, _, _ in saved_session._http.calls] == ["GET", "GET"]


def test_alternate_like_rejects_old_metadata_before_membership_reads(saved_session, bootstrap, tmp_path):
    alternate = "1280677978"
    saved_session.song = lambda *_args: {"status": 1, "id": SONG_ID, "duration": "114.99"}
    with pytest.raises(SessionError, match="requested song"):
        like_test.run_like_test(saved_session, alternate, declared_song_id=alternate, report_path=tmp_path / "audit.json")
    assert saved_session._http.calls == []


def test_alternate_like_adapter_rejects_old_song_from_original_like_function(saved_session):
    alternate = "1280677978"
    functions = like_test._load_legacy_functions(include_like=True)
    adapter = like_test._LikeAdapter(
        saved_session._http, {}, SOCKET, FINGERPRINT, {"liked_before": False}, None,
        functions["build_gateway_params"], song_id=alternate,
    )
    adapter.playlist_id = PLAYLIST_ID
    with pytest.raises(SessionError, match="outside the selected song"):
        functions["like_song"](adapter, SONG_ID, FINGERPRINT, SOCKET)
    assert adapter.mutation_attempts == 0 and saved_session._http.calls == []
    default_adapter = like_test._LikeAdapter(saved_session._http, {}, SOCKET, FINGERPRINT, {}, None, functions["build_gateway_params"])
    assert adapter.song_id == alternate and default_adapter.song_id == SONG_ID


def test_complete_likes_membership_checks_requested_song_without_changing_default():
    alternate = "1280677978"
    payload = playlist([alternate])
    assert like_test._stored_order_like(payload, PLAYLIST_ID, 1, song_id=alternate) is True
    assert like_test._stored_order_like(payload, PLAYLIST_ID, 1) is False
    payload = playlist([SONG_ID])
    assert like_test._stored_order_like(payload, PLAYLIST_ID, 1, song_id=alternate) is False
    assert like_test._stored_order_like(payload, PLAYLIST_ID, 1) is True


@pytest.mark.parametrize("requested,declared", [
    (SONG_ID, "1280677978"), ("1280677978", SONG_ID), (SONG_ID, "0"),
    (SONG_ID, "0123"), (SONG_ID, True), (SONG_ID, 2**63),
    (True, SONG_ID), ("١٢٣", SONG_ID), (SONG_ID + "&sid=" + SECRET, SONG_ID),
])
def test_invalid_or_mismatched_like_declaration_prevents_all_authentication(saved_session, bootstrap, tmp_path, requested, declared):
    with pytest.raises(SessionError, match="declared test song") as exc:
        like_test.run_like_test(saved_session, requested, declared_song_id=declared, report_path=tmp_path / "audit.json")
    assert SECRET not in str(exc.value)
    assert saved_session.checks == [] and bootstrap == [] and saved_session._http.calls == []
    assert list(tmp_path.iterdir()) == []


def test_original_like_appends_once_and_independently_verifies_persisted_state(saved_session, bootstrap, tmp_path, monkeypatch):
    path = tmp_path / "like-audit.json"
    before = add_state(saved_session._http, ["42"])
    append = Reply({"status": "ok", "unused_private_field": SECRET})
    saved_session._http.replies.append(append)
    after = add_state(saved_session._http, ["42", SONG_ID])
    original_retry = saved_session._http.retry
    at_send = []
    saved_session._http.on_post = lambda: at_send.append(safe_report(path))
    load = like_test._load_legacy_functions
    original_calls = []

    def load_with_spy(*, include_like):
        assert include_like is True
        functions = load(include_like=True)
        original = functions["like_song"]
        assert Path(original.__code__.co_filename).name == "send_vote.py"

        def called(*args):
            original_calls.append(args[1:])
            return original(*args)

        functions["like_song"] = called
        return functions

    monkeypatch.setattr(like_test, "_load_legacy_functions", load_with_spy)
    report = like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    assert original_calls == [(SONG_ID, FINGERPRINT, SOCKET)]
    assert bootstrap == ["authenticate"] and saved_session.checks == [True]
    assert saved_session._http.retry is original_retry
    assert [(method, operation) for method, operation, _, _ in saved_session._http.calls] == [
        ("GET", "GETplaylists"), ("GET", "GETplaylistdata"), ("POST", "PUTplaylist"),
        ("GET", "GETplaylists"), ("GET", "GETplaylistdata"),
    ]
    assert len(at_send) == 1
    assert at_send[0]["phase"] == "mutation"
    assert at_send[0]["liked_before"] is False
    assert at_send[0]["mutation_attempts"] == 1
    assert at_send[0]["mutation_accepted"] is None
    assert at_send[0]["mutation_result"] == "unknown"
    for method, operation, url, options in saved_session._http.calls:
        assert url == media_gateway.GATEWAY_URL
        assert options["timeout"] == 25 and options["allow_redirects"] is False
        assert options["params"]["sid"] == SOCKET
        assert options["params"]["fingerprint"] == FINGERPRINT
        expected_headers = dict(saved_session._template("relations")["headers"])
        if method == "POST":
            expected_headers["content-type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            assert options["data"]["songid"] == SONG_ID
            assert options["data"]["action"] == "append"
            assert options["data"]["name"] == LIKED_NAME
        if operation == "GETplaylistdata":
            assert options["params"]["nopaging"] == "1"
            assert options["params"]["buffered"] == "1"
            assert options["params"]["playlistid"] == PLAYLIST_ID
        assert options["headers"] == expected_headers
    assert report == safe_report(path)
    assert report["passed"] and report["persisted_state_verified"]
    assert report["liked_before"] is False and report["liked_after"] is True
    assert report["mutation_accepted"] is True and report["mutation_result"] == "accepted"
    assert report["mutation_attempts"] == 1 and report["api_status"] == "ok"
    assert report["automatic_retry"] is report["browser_required"] is report["password_required"] is False
    assert report["audio_bytes"] == 0
    assert all(reply.closed for reply in (*before, append, *after))


def test_already_liked_song_is_verified_without_any_append(saved_session, bootstrap, tmp_path):
    add_state(saved_session._http, ["42", SONG_ID])
    report = like_test.run_like_test(saved_session, SONG_ID, report_path=tmp_path / "audit.json")
    assert report["passed"] and report["persisted_state_verified"]
    assert report["liked_before"] is report["liked_after"] is True
    assert report["mutation_attempted"] is False and report["mutation_attempts"] == 0
    assert report["mutation_result"] == "skipped_already_liked"
    assert [method for method, _, _, _ in saved_session._http.calls] == ["GET", "GET"]


def test_complete_24_member_order_proves_hidden_like_despite_23_visible_items(saved_session, bootstrap, tmp_path):
    ids = [str(value) for value in range(1000, 1023)] + [SONG_ID]
    responses = add_state(saved_session._http, ids)
    assert responses[0].payload["sections"][0]["data"][0]["count"] == 24
    assert responses[1].payload["PlaylistCount"] == 23
    assert responses[1].payload["sections"][0]["data"] == []
    assert responses[1].payload["name"] != LIKED_NAME
    report = like_test.run_like_test(saved_session, SONG_ID, report_path=tmp_path / "audit.json")
    assert report["passed"] and report["persisted_state_verified"]
    assert report["liked_before"] is report["liked_after"] is True
    assert report["mutation_result"] == "skipped_already_liked"
    assert report["mutation_attempts"] == 0
    assert [method for method, _, _, _ in saved_session._http.calls] == ["GET", "GET"]


def test_partial_23_member_order_against_24_member_card_prevents_append(saved_session, bootstrap, tmp_path):
    ids = [str(value) for value in range(1000, 1024)]
    saved_session._http.replies.extend([Reply(discovery(ids)), Reply(playlist(ids[:-1]))])
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError):
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    report = safe_report(path)
    assert report["mutation_attempts"] == 0 and report["liked_before"] is None
    assert [method for method, _, _, _ in saved_session._http.calls] == ["GET", "GET"]


def test_core_song_guard_prevents_authentication_and_like_reads(saved_session, bootstrap, tmp_path):
    with pytest.raises(SessionError, match="restricted to declared test song"):
        like_test.run_like_test(saved_session, WRONG_SONG_ID, report_path=tmp_path / "audit.json")
    assert saved_session.checks == [] and bootstrap == [] and saved_session._http.calls == []


def test_failed_authentication_prevents_like_reads_and_append(saved_session, bootstrap, tmp_path):
    saved_session.check = lambda **_kwargs: {"authenticated": False}
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError):
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    assert bootstrap == [] and saved_session._http.calls == []
    assert safe_report(path)["mutation_attempts"] == 0


@pytest.mark.parametrize("metadata", [
    {"status": "failed", "message": SECRET},
    {"status": 1, "id": WRONG_SONG_ID},
])
def test_failed_or_mismatched_metadata_prevents_state_reads_and_append(saved_session, bootstrap, tmp_path, metadata):
    saved_session.song = lambda _song_id: metadata
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError) as error:
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    assert SECRET not in str(error.value)
    report = safe_report(path)
    assert report["metadata_verified"] is False and report["mutation_attempts"] == 0
    assert saved_session._http.calls == []


@pytest.mark.parametrize("problem", [
    "count_mismatch", "wrong_playlist", "missing_order", "duplicate_order",
    "invalid_order", "filtered", "cap",
])
def test_unproven_initial_playlist_state_cannot_trigger_append(saved_session, bootstrap, tmp_path, problem):
    saved_session._http.replies.append(Reply(discovery(["42"])))
    state = playlist(["42"])
    if problem == "count_mismatch":
        state = playlist(["42", SONG_ID])
    elif problem == "wrong_playlist":
        state["id"] = str(int(PLAYLIST_ID) + 1)
    elif problem == "missing_order":
        del state["songorder"]
    elif problem == "duplicate_order":
        state["songorder"] = "42,42"
    elif problem == "invalid_order":
        state["songorder"] = "not-a-song-id"
    elif problem == "filtered":
        state["numFiltered"] = 1
    else:
        state["limit_number_songs"] = 1
    saved_session._http.replies.append(Reply(state))
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError):
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    report = safe_report(path)
    assert report["mutation_attempts"] == 0 and report["liked_before"] is None
    assert [method for method, _, _, _ in saved_session._http.calls] == ["GET", "GET"]


@pytest.mark.parametrize("reply", [
    RuntimeError("Unknown append outcome carrying " + SECRET),
    Reply({"status": "ok", "private": SECRET}, status=500),
    Reply(ValueError("Malformed append reply carrying " + SECRET)),
])
def test_unknown_append_is_not_retried_even_when_readback_confirms_like(saved_session, bootstrap, tmp_path, reply):
    add_state(saved_session._http, ["42"])
    saved_session._http.replies.append(reply)
    add_state(saved_session._http, ["42", SONG_ID])
    original_retry = saved_session._http.retry
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError) as error:
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    assert SECRET not in "".join(traceback.format_exception(error.value))
    report = safe_report(path)
    assert report["passed"] is False
    assert report["mutation_attempts"] == 1 and report["mutation_accepted"] is None
    assert report["mutation_result"] == report["api_status"] == "unknown"
    assert report["liked_after"] is True and report["persisted_state_verified"] is True
    assert report["automatic_retry"] is False
    assert report["verification_read_attempts"] == 1 and report["verification_read_retries"] == 0
    assert saved_session._http.retry is original_retry
    assert [method for method, _, _, _ in saved_session._http.calls].count("POST") == 1


def test_api_acknowledgement_does_not_substitute_for_persisted_like(saved_session, bootstrap, tmp_path):
    add_state(saved_session._http, ["42"])
    saved_session._http.replies.append(Reply({"status": "ok"}))
    add_state(saved_session._http, ["42"])
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError, match="stored like was not verified"):
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    report = safe_report(path)
    assert report["passed"] is False
    assert report["mutation_accepted"] is True and report["mutation_result"] == "accepted"
    assert report["liked_after"] is False and report["persisted_state_verified"] is False
    assert report["mutation_attempts"] == 1
    assert report["error_code"] == "readback_not_liked"
    assert report["verification_read_attempts"] == 1 and report["verification_read_retries"] == 0
    assert report["verification_read_retryable"] is False


@pytest.mark.parametrize("operation", ["GETplaylists", "GETplaylistdata"])
def test_accepted_append_retries_only_readback_on_same_session(saved_session, bootstrap, tmp_path, monkeypatch, operation):
    sleeps = []
    monkeypatch.setattr(like_test.time, "sleep", lambda seconds: sleeps.append(seconds))
    add_state(saved_session._http, ["42"])
    saved_session._http.replies.append(Reply({"status": "ok"}))
    if operation == "GETplaylistdata":
        saved_session._http.replies.append(Reply(discovery(["42", SONG_ID])))
    saved_session._http.replies.append(TransportFailure(7))
    responses = add_state(saved_session._http, ["42", SONG_ID])
    path = tmp_path / "accepted-read-recovery.json"
    report = like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    assert report["passed"] is True and report["persisted_state_verified"] is True
    assert report["liked_before"] is False and report["liked_after"] is True
    assert report["mutation_attempts"] == 1 and report["mutation_accepted"] is True
    assert report["verification_read_attempts"] == 2 and report["verification_read_retries"] == 1
    assert report["verification_read_retryable"] is False
    assert bootstrap == ["authenticate"] and saved_session.checks == [True]
    calls = saved_session._http.calls
    assert sum(method == "POST" for method, *_ in calls) == 1
    assert all(options["params"]["sid"] == SOCKET for _, _, _, options in calls)
    assert all(response.closed for response in responses)
    assert sleeps == [0.5]
    assert safe_report(path)["passed"] is True


def test_accepted_append_readback_rate_limit_waits_before_retry_get(saved_session, bootstrap, tmp_path, monkeypatch):
    observed = []
    gate_observations = []
    monkeypatch.setattr(like_test, "observe_provider_failure", lambda failure: observed.append(safe_request_failure(failure)))
    monkeypatch.setattr(like_test, "wait_before_provider_request", lambda _stage: gate_observations.append(len(observed)) or True)
    monkeypatch.setattr(like_test.time, "sleep", lambda _seconds: pytest.fail("429 recovery must use the shared cooldown"))
    add_state(saved_session._http, ["42"])
    saved_session._http.replies.append(Reply({"status": "ok"}))
    limited = Reply({}, status=429)
    limited.headers = {"Retry-After": "90"}
    saved_session._http.replies.append(limited)
    add_state(saved_session._http, ["42", SONG_ID])
    report = like_test.run_like_test(saved_session, SONG_ID, report_path=tmp_path / "accepted-rate-recovery.json")
    assert report["passed"] and report["verification_read_retries"] == 1
    assert len(observed) == 1 and observed[0]["retry_after_seconds"] == 90
    assert gate_observations == [0, 0, 0, 0, 0, 1, 1]
    assert sum(method == "POST" for method, *_ in saved_session._http.calls) == 1


def test_accepted_append_exhausted_readback_remains_unverified_with_typed_failure(saved_session, bootstrap, tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(like_test.time, "sleep", lambda seconds: sleeps.append(seconds))
    add_state(saved_session._http, ["42"])
    saved_session._http.replies.extend([Reply({"status": "ok"}), TransportFailure(7), TransportFailure(56), TransportFailure(7)])
    path = tmp_path / "accepted-read-exhausted.json"
    with pytest.raises(SessionError):
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    report = safe_report(path)
    assert report["passed"] is False and report["persisted_state_verified"] is False
    assert report["mutation_accepted"] is True and report["mutation_attempts"] == 1
    assert report["liked_after"] is None and report["failed_phase"] == "state_after"
    assert report["error_code"] == "state_read_failed"
    assert report["session_failure"] == safe_request_failure(RequestFailure("request_transport_failed", stage="likes_read", curl_code=7, retry_safe=False))
    assert report["verification_read_attempts"] == 3 and report["verification_read_retries"] == 2
    assert report["verification_read_retryable"] is True
    assert sleeps == [0.5, 1.0] and bootstrap == ["authenticate"]
    assert [method for method, *_ in saved_session._http.calls] == ["GET", "GET", "POST", "GET", "GET", "GET"]


def test_accepted_append_transient_http_readback_recovers_without_replay(saved_session, bootstrap, tmp_path, monkeypatch):
    monkeypatch.setattr(like_test.time, "sleep", lambda _seconds: None)
    add_state(saved_session._http, ["42"])
    saved_session._http.replies.extend([Reply({"status": "ok"}), Reply({}, status=503)])
    add_state(saved_session._http, ["42", SONG_ID])
    report = like_test.run_like_test(saved_session, SONG_ID, report_path=tmp_path / "accepted-http-recovery.json")
    assert report["passed"] and report["verification_read_retries"] == 1
    assert sum(method == "POST" for method, *_ in saved_session._http.calls) == 1
    assert bootstrap == ["authenticate"]


def test_accepted_append_recovery_still_requires_original_playlist_identity(saved_session, bootstrap, tmp_path, monkeypatch):
    monkeypatch.setattr(like_test.time, "sleep", lambda _seconds: None)
    add_state(saved_session._http, ["42"])
    saved_session._http.replies.extend([Reply({"status": "ok"}), TransportFailure(7)])
    changed = discovery(["42", SONG_ID])
    changed["sections"][0]["data"][0]["id"] = str(int(PLAYLIST_ID) + 1)
    saved_session._http.replies.append(Reply(changed))
    path = tmp_path / "accepted-read-identity-change.json"
    with pytest.raises(SessionError):
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    report = safe_report(path)
    assert report["error_code"] == "playlist_identity_changed"
    assert report["verification_read_attempts"] == 2 and report["verification_read_retryable"] is False
    assert report["liked_after"] is None and report["persisted_state_verified"] is False
    assert [method for method, *_ in saved_session._http.calls] == ["GET", "GET", "POST", "GET", "GET"]


@pytest.mark.parametrize("failure", [TransportFailure(60), TransportFailure(98), TransportFailure(99), RuntimeError(SECRET), Reply({}, status=401), Reply({}, status=403), Reply({}, status=407), Reply({}, status=404), Reply(ValueError(SECRET)), Reply({"status": "failed"})])
def test_accepted_append_does_not_retry_tls_auth_unknown_or_semantic_readback_failure(saved_session, bootstrap, tmp_path, failure):
    add_state(saved_session._http, ["42"])
    saved_session._http.replies.extend([Reply({"status": "ok"}), failure])
    path = tmp_path / "accepted-nonretry-read.json"
    with pytest.raises(SessionError):
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    report = safe_report(path)
    assert report["verification_read_attempts"] == 1 and report["verification_read_retries"] == 0
    assert report["verification_read_retryable"] is False
    assert report["passed"] is False and report["mutation_attempts"] == 1
    assert len(saved_session._http.calls) == 4 and bootstrap == ["authenticate"]


def test_unknown_append_transport_failure_does_not_enable_readback_retries(saved_session, bootstrap, tmp_path):
    add_state(saved_session._http, ["42"])
    saved_session._http.replies.extend([TransportFailure(56), TransportFailure(7)])
    path = tmp_path / "unknown-append-no-retry.json"
    with pytest.raises(SessionError):
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    report = safe_report(path)
    assert report["mutation_accepted"] is None and report["mutation_attempts"] == 1
    assert report["verification_read_attempts"] == 1 and report["verification_read_retries"] == 0
    assert report["verification_read_retryable"] is False
    assert [method for method, *_ in saved_session._http.calls] == ["GET", "GET", "POST", "GET"]


@pytest.mark.parametrize("failure, expected", [
    (RequestFailure("request_transport_failed", stage="likes_read", curl_code=7, retry_safe=False), True),
    (RequestFailure("request_transport_failed", stage="likes_read", curl_code=60, retry_safe=False), False),
    (RequestFailure("request_transport_failed", stage="likes_read", curl_code=7, http_status=401, retry_safe=False), False),
    (RequestFailure("request_http_failed", stage="likes_read", http_status=503, curl_code=60, retry_safe=False), False),
    (RequestFailure("request_transport_failed", stage="identity", curl_code=7), False),
    (RequestFailure("request_transport_failed", stage="likes_read", retry_safe=False), False),
    (RequestFailure("request_http_failed", stage="likes_read", http_status=503, retry_safe=False), True),
    (RequestFailure("request_rate_limited", stage="likes_read", http_status=429, retry_safe=False), True),
    (RequestFailure("request_http_failed", stage="likes_read", http_status=403, retry_safe=False), False),
    (RequestFailure("session_response_invalid", stage="likes_read"), False),
])
def test_readback_recovery_helper_requires_typed_transient_read_failure(failure, expected):
    assert like_test.retryable_like_verification_failure(failure) is expected
    assert like_test.retryable_like_verification_failure(safe_request_failure(failure)) is expected


def test_accepted_append_readback_cooldown_refusal_does_not_repeat_reads(saved_session, bootstrap, tmp_path, monkeypatch):
    stages = []
    def gate(stage):
        stages.append(stage)
        return len(stages) <= 5
    monkeypatch.setattr(like_test, "wait_before_provider_request", gate)
    add_state(saved_session._http, ["42"])
    saved_session._http.replies.extend([Reply({"status": "ok"}), Reply({}, status=429)])
    monkeypatch.setattr(like_test, "observe_provider_failure", lambda _failure: None)
    path = tmp_path / "accepted-cooldown-pending.json"
    with pytest.raises(RequestFailure):
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    report = safe_report(path)
    assert report["verification_read_attempts"] == 2 and report["verification_read_retries"] == 1
    assert report["verification_read_retryable"] is True
    assert report["session_failure"]["http_status"] == 429
    assert [method for method, *_ in saved_session._http.calls] == ["GET", "GET", "POST", "GET"]


def test_pre_append_journal_failure_prevents_post(saved_session, bootstrap, tmp_path, monkeypatch):
    add_state(saved_session._http, ["42"])
    add_state(saved_session._http, ["42"])
    journal = like_test._journal

    def fail_on_mutation(report, path):
        if report["phase"] == "mutation":
            raise SessionError("Synthetic journal failure")
        journal(report, path)

    monkeypatch.setattr(like_test, "_journal", fail_on_mutation)
    path = tmp_path / "audit.json"
    with pytest.raises(SessionError):
        like_test.run_like_test(saved_session, SONG_ID, report_path=path)
    assert all(method == "GET" for method, _, _, _ in saved_session._http.calls)
    assert safe_report(path)["passed"] is False
