"""Proxy wiring and bounded repetition use synthetic fixtures and no network."""

import json
from types import SimpleNamespace

import pytest
from curl_cffi.const import CurlInfo

from anghami_session import accounts, client, like_test, play_record, vault as vault_module
from anghami_session.errors import SessionError


SONG_ID = play_record.TEST_SONG_ID
SECRET = "synthetic-proxy-and-session-secret-must-not-appear"


class FakeProxy:
    def __init__(self, *, failure=None, events=None):
        self.failure = failure
        self.events = [] if events is None else events
        self.verifications = 0

    def transport_options(self):
        return {
            "impersonate": "chrome",
            "proxy": "https://synthetic-user:" + SECRET + "@proxy.packetstream.io:31111",
        }

    def summary(self):
        return {
            "provider": "PacketStream", "country": "EG",
            "endpoint": "https://proxy.packetstream.io:31111", "sticky": True,
        }

    def verify_country(self):
        self.verifications += 1
        self.events.append("proxy_check")
        if self.failure:
            raise self.failure
        return {**self.summary(), "country_verified": True, "proxy_used": True}


def command_args(command, selected_path, *extra):
    result = [command, "--row", "7", "--vault", str(selected_path)]
    if command in {"test-like", "test-play-record"}:
        result.extend(["--song-id", SONG_ID])
    return result + list(extra)


def fake_cli_vault(monkeypatch, *, fail_at=None):
    calls = []

    class FakeVault:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def record(self, row):
            assert row == 7
            return {"email": "synthetic@example.com", "password": SECRET}

        def execute(self, name, row, selected, options):
            calls.append((name, row, selected, options))
            if fail_at is not None and len(calls) == fail_at:
                raise SessionError("Synthetic test failure; do not retry")
            return {"passed": True, "source_row": row, "operation": name}

        def test_like(self, row, song_id, **options):
            return self.execute("test-like", row, song_id, options)

        def test_play_record(self, row, song_id, **options):
            return self.execute("test-play-record", row, song_id, options)

        def request(self, row, operation, **options):
            return self.execute(operation, row, operation, options)

    monkeypatch.setattr(accounts, "AccountVault", FakeVault)
    return calls


@pytest.mark.parametrize("command", ["test-like", "test-play-record", "relations"])
def test_default_cli_keeps_original_vault_call_signature(monkeypatch, tmp_path, capsys, command):
    calls = fake_cli_vault(monkeypatch)
    assert accounts.main(command_args(command, tmp_path / "synthetic.sqlite3")) == 0
    selected = command if command == "relations" else SONG_ID
    assert calls == [(command, 7, selected, {})]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out)["passed"] is True


@pytest.mark.parametrize("command", ["test-like", "test-play-record", "relations"])
def test_proxy_cli_loads_local_profile_and_passes_exact_instance(monkeypatch, tmp_path, capsys, command):
    calls = fake_cli_vault(monkeypatch)
    proxy = FakeProxy()
    loads = []

    def load(path):
        loads.append(path)
        return proxy

    monkeypatch.setattr(accounts, "load_packetstream_proxy", load)
    path = tmp_path / "synthetic.sqlite3"
    assert accounts.main(command_args(command, path, "--proxy-egypt")) == 0
    selected = command if command == "relations" else SONG_ID
    assert calls == [(command, 7, selected, {"proxy": proxy})]
    assert loads == [tmp_path / "packetstream.dpapi"]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err


def test_proxy_configuration_reads_hidden_key_without_opening_account_vault(monkeypatch, tmp_path, capsys):
    saved = []

    def no_vault(*_args, **_kwargs):
        pytest.fail("Proxy configuration opened the account vault")

    def save(username, auth_key, *, path):
        saved.append((username, auth_key, path))
        return FakeProxy().summary()

    monkeypatch.setattr(accounts, "AccountVault", no_vault)
    monkeypatch.setattr(accounts.getpass, "getpass", lambda _prompt: SECRET)
    monkeypatch.setattr(accounts, "save_packetstream_credentials", save)
    assert accounts.main([
        "proxy-configure", "--username", "synthetic-user", "--vault", str(tmp_path / "synthetic.sqlite3"),
    ]) == 0
    assert saved == [("synthetic-user", SECRET, tmp_path / "packetstream.dpapi")]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out)["country"] == "EG"


@pytest.mark.parametrize("command", ["test-like", "test-play-record"])
def test_count_runs_same_selected_account_and_proxy_sequentially(monkeypatch, tmp_path, capsys, command):
    calls = fake_cli_vault(monkeypatch)
    proxy = FakeProxy()
    loads = []
    monkeypatch.setattr(accounts, "load_packetstream_proxy", lambda path: loads.append(path) or proxy)
    assert accounts.main(command_args(
        command, tmp_path / "synthetic.sqlite3", "--proxy-egypt", "--count", "3",
    )) == 0
    assert calls == [(command, 7, SONG_ID, {"proxy": proxy})] * 3
    assert loads == [tmp_path / "packetstream.dpapi"]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    report = json.loads(output.out)
    assert report["requested_tests"] == report["completed_tests"] == report["attempted_tests"] == 3
    assert report["automatic_retry"] is False


@pytest.mark.parametrize("count", ["0", "6"])
def test_count_outside_bound_cannot_invoke_any_test(monkeypatch, tmp_path, count):
    calls = fake_cli_vault(monkeypatch)
    try:
        result = accounts.main(command_args("test-like", tmp_path / "synthetic.sqlite3", "--count", count))
    except SystemExit as error:
        result = error.code
    assert result != 0
    assert calls == []


@pytest.mark.parametrize("command", ["test-like", "test-play-record"])
def test_repeated_tests_stop_at_first_failure_without_retry(monkeypatch, tmp_path, capsys, command):
    calls = fake_cli_vault(monkeypatch, fail_at=2)
    assert accounts.main(command_args(
        command, tmp_path / "synthetic.sqlite3", "--count", "5",
    )) == 1
    assert calls == [(command, 7, SONG_ID, {})] * 2
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    report = tmp_path / f"account-7.{command}-batch-report.json"
    text = report.read_text(encoding="utf-8")
    assert SECRET not in text
    report = json.loads(text)
    assert report["source_row"] == 7
    assert report["requested_tests"] == 5 and report["attempted_tests"] == 2
    assert report["completed_tests"] == 1 and report["failed_test"] == 2
    assert report["phase"] == "stopped" and report["automatic_retry"] is False


@pytest.mark.parametrize("command", ["test-like", "test-play-record"])
def test_final_batch_journal_failure_records_failed_batch_without_resending(monkeypatch, tmp_path, capsys, command):
    calls = fake_cli_vault(monkeypatch)
    journal = play_record._journal
    failures = []

    def fail_final_write_once(report, path):
        if report["phase"] == "complete" and not failures:
            failures.append(True)
            raise SessionError("Synthetic final batch journal failure")
        journal(report, path)

    monkeypatch.setattr(play_record, "_journal", fail_final_write_once)
    assert accounts.main(command_args(
        command, tmp_path / "synthetic.sqlite3", "--count", "2",
    )) == 1
    assert failures == [True]
    assert calls == [(command, 7, SONG_ID, {})] * 2
    path = tmp_path / f"account-7.{command}-batch-report.json"
    text = path.read_text(encoding="utf-8")
    report = json.loads(text)
    assert report["passed"] is False and report["phase"] == "stopped"
    assert report["requested_tests"] == report["attempted_tests"] == report["completed_tests"] == 2
    assert report["results"] == [
        {"passed": True, "source_row": 7, "operation": command},
        {"passed": True, "source_row": 7, "operation": command},
    ]
    assert report["automatic_retry"] is False
    output = capsys.readouterr()
    assert SECRET not in text + output.out + output.err


@pytest.mark.parametrize("method", ["test_like", "test_play_record"])
@pytest.mark.parametrize("bad_selection", ["row", "song"])
def test_scope_guards_precede_session_loading_and_proxy_check(method, bad_selection):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    selected.session = lambda *_: pytest.fail("Guard loaded an unselected session")
    proxy = FakeProxy()
    row, song_id = (6, SONG_ID) if bad_selection == "row" else (7, str(int(SONG_ID) + 1))
    with pytest.raises(SessionError):
        getattr(selected, method)(row, song_id, proxy=proxy)
    assert proxy.verifications == 0


@pytest.mark.parametrize("method", ["test_like", "test_play_record", "request"])
def test_proxy_country_failure_prevents_origin_transport_and_test_runner(monkeypatch, tmp_path, method):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    selected.path = tmp_path / "synthetic.sqlite3"
    selected.session = lambda _row: {"synthetic": "saved"}
    proxy = FakeProxy(failure=SessionError("Synthetic proxy country verification failed"))
    report_path = None
    if method != "request":
        command = "test-like" if method == "test_like" else "test-play-record"
        report_path = tmp_path / f"account-7.{command}-report.json"
        report_path.write_text(json.dumps({
            "passed": True, "event_attempted": True, "event_attempts": 1,
            "mutation_attempted": True, "mutation_attempts": 1, "stale_success": True,
        }), encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Origin transport or test runner was created before country verification")

    monkeypatch.setattr(vault_module, "AnghamiSession", forbidden)
    monkeypatch.setattr(like_test, "run_like_test", forbidden)
    monkeypatch.setattr(play_record, "run_play_record_test", forbidden)
    selected_value = "relations" if method == "request" else SONG_ID
    with pytest.raises(SessionError, match="proxy country verification failed"):
        getattr(selected, method)(7, selected_value, proxy=proxy)
    assert proxy.verifications == 1
    if report_path is not None:
        text = report_path.read_text(encoding="utf-8")
        report = json.loads(text)
        assert report["passed"] is False
        assert report["phase"] == "failed" and report["failed_phase"] == "proxy_preflight"
        assert report["event_attempted"] is False and report["event_attempts"] == 0
        assert report["mutation_attempted"] is False and report["mutation_attempts"] == 0
        assert "stale_success" not in report and SECRET not in text


@pytest.mark.parametrize("method", ["test_like", "test_play_record", "request"])
def test_vault_verifies_route_once_before_constructing_bound_transport(monkeypatch, tmp_path, method):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    selected.path = tmp_path / "synthetic.sqlite3"
    events = []
    bundle = {"synthetic": "saved"}
    proxy = FakeProxy(events=events)
    selected.session = lambda row: events.append(("saved", row)) or bundle

    class FakeSession:
        def __init__(self, *, saved, proxy):
            assert saved is bundle
            assert proxy.verifications == 1
            self._proxy = proxy
            events.append("origin_transport")

        @property
        def proxy_summary(self):
            return {**self._proxy.summary(), "exit_check": self._proxy_check}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            events.append("closed")

        def request(self, operation):
            assert operation == "relations"
            events.append("origin_request")
            return {"status": "ok"}

    def run(session, song_id, *, report_path):
        assert isinstance(session, FakeSession) and song_id == SONG_ID
        events.append("runner")
        return {"passed": True, "song_id": song_id, "proxy": session.proxy_summary}

    monkeypatch.setattr(vault_module, "AnghamiSession", FakeSession)
    monkeypatch.setattr(like_test, "run_like_test", run)
    monkeypatch.setattr(play_record, "run_play_record_test", run)
    selected_value = "relations" if method == "request" else SONG_ID
    result = getattr(selected, method)(7, selected_value, proxy=proxy)
    assert proxy.verifications == 1
    assert events == [
        ("saved", 7), "proxy_check", "origin_transport",
        "origin_request" if method == "request" else "runner", "closed",
    ]
    assert SECRET not in json.dumps(result)
    assert result["proxy"]["country"] == "EG"
    assert result["proxy"]["exit_check"]["country_verified"] is True


def test_fresh_and_reused_proxy_tunnels_keep_anonymous_control_isolated(monkeypatch):
    instances = []
    proxy = FakeProxy()
    saved = {
        "format_version": 1, "created_at_utc": "2026-10-01T00:00:00+00:00",
        "origin": "https://play.anghami.com",
        "requests": {"relations": {
            "method": "GET", "url": client.GATEWAY_URL + "?type=GETuserrelations&sid=" + SECRET,
            "headers": {"cookie": "synthetic=" + SECRET, "authorization": "Bearer " + SECRET},
        }},
    }
    saved["requests"]["playlists"] = {
        "method": "GET", "url": client.GATEWAY_URL + "?type=GETplaylists&sid=" + SECRET,
        "headers": {"cookie": "synthetic=" + SECRET},
    }

    class Reply:
        status_code = 200

        def __init__(self, status, connect_code=200):
            self.status = status
            self.infos = {CurlInfo.USED_PROXY: 1, CurlInfo.HTTP_CONNECTCODE: connect_code}

        def json(self):
            return {"status": self.status}

    class FakeTransport:
        def __init__(self, **options):
            self.options = options
            self.cookies = {}
            self.calls = []
            self.closed = False
            instances.append(self)

        def get(self, url, **options):
            self.calls.append((url, options))
            if len(instances) == 1:
                self.cookies["positive-only-cookie"] = SECRET
                # libcurl reports no new CONNECT for a reused HTTPS tunnel.
                return Reply("ok", connect_code=200 if len(self.calls) == 1 else 0)
            assert self.cookies == {}
            assert SECRET not in url + json.dumps(options)
            return Reply("failed")

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(client.requests, "Session", FakeTransport)
    with client.AnghamiSession(saved=saved, proxy=proxy) as session:
        assert session._proxy is proxy
        session._proxy_check = proxy.verify_country()
        report = session.check(negative_control=True)
    assert report["operations"] == {"relations": "ok", "playlists": "ok"}
    assert report["proxy"]["exit_check"]["country_verified"] is True
    assert proxy.verifications == 1
    assert report["without_session"]["authentication_rejected"] is True
    assert SECRET not in json.dumps(report)
    assert len(instances) == 2
    assert all(instance.closed for instance in instances)
    assert all(instance.options == {"impersonate": "chrome", **proxy.transport_options()} for instance in instances)
    assert instances[0].cookies == {"positive-only-cookie": SECRET}
    assert instances[1].cookies == {}
    assert len(instances[0].calls) == 2 and len(instances[1].calls) == 1


@pytest.mark.parametrize("used_proxy,connect_code", [(0, 0), (0, 200), (1, 407)])
def test_successful_http_cannot_hide_direct_route_or_failed_proxy_tunnel(monkeypatch, used_proxy, connect_code):
    calls = []
    closed = []
    saved = {
        "format_version": 1, "created_at_utc": "2026-10-01T00:00:00+00:00",
        "origin": "https://play.anghami.com",
        "requests": {"relations": {
            "method": "GET", "url": client.GATEWAY_URL + "?type=GETuserrelations&sid=" + SECRET,
            "headers": {"cookie": "synthetic=" + SECRET},
        }},
    }

    class FakeTransport:
        def __init__(self, **options):
            assert options == FakeProxy().transport_options()

        def get(self, url, **options):
            calls.append((url, options))
            return SimpleNamespace(
                status_code=200,
                infos={CurlInfo.USED_PROXY: used_proxy, CurlInfo.HTTP_CONNECTCODE: connect_code},
                json=lambda: {"status": "ok"},
            )

        def close(self):
            closed.append(True)

    monkeypatch.setattr(client.requests, "Session", FakeTransport)
    with client.AnghamiSession(saved=saved, proxy=FakeProxy()) as session:
        with pytest.raises(SessionError, match="proxy route was not confirmed") as error:
            session.request("relations")
    assert SECRET not in str(error.value)
    assert len(calls) == 1 and closed == [True]
