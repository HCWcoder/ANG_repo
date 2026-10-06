"""Account preparation and selected-cohort CLI tests are entirely offline."""

import builtins
from copy import deepcopy
import json
import sys
from types import SimpleNamespace

import pytest

from anghami_session import accounts, preparation
from anghami_session.errors import LoginCaptureError, SessionError
from anghami_session.play_record import TEST_SONG_ID


SYNTHETIC_PASSWORD = "synthetic-only-account-password"
SYNTHETIC_EMAIL = "synthetic-only@example.invalid"
SAVED = {"synthetic_session": "synthetic-only-session-secret"}


class FakeProxy:
    def summary(self):
        return {"provider": "PacketStream", "country": "EG", "sticky": True}


class PreparationVault:
    def __init__(self, path, rows=(8, 9, 10), *, sessions=None, failure=None):
        self.path = path
        self.rows = list(rows)
        self.sessions = {} if sessions is None else sessions
        self.failure = failure
        self.events = []
        self.reviewed = {}
        self.pending_candidates = {}

    def pending_session(self, row):
        return deepcopy(self.pending_candidates.get(row))

    def save_pending_session(self, row, saved):
        self.pending_candidates[row] = deepcopy(saved)

    def select_test_candidates(self, count, *, start_row=1):
        self.events.append(("select", count, start_row))
        return [row for row in self.rows if row >= start_row][:count]

    def session(self, row):
        self.events.append(("session", row))
        if row not in self.sessions:
            raise SessionError("No synthetic saved session")
        return self.sessions[row]

    def record(self, row):
        self.events.append(("record", row))
        return {"email": SYNTHETIC_EMAIL, "password": SYNTHETIC_PASSWORD}

    def attach(self, row, saved, **options):
        self.events.append(("attach", row, saved, options))
        if self.failure is not None and row == self.failure[0]:
            raise self.failure[1]
        self.pending_candidates.pop(row, None)
        return {"source_row": row, "verified": True}

    def enable_test_account(self, row):
        self.events.append(("enable", row))

    def record_account_failure(self, row, failure):
        self.reviewed[row] = dict(failure)
        self.events.append(("review", row))

    def failure_review(self):
        return {"failed_rows": sorted(self.reviewed), "accounts": [], "total": len(self.reviewed)}


def forbid_browser_imports(monkeypatch):
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name not in {"capture", "browser", "anghami_session.capture", "anghami_session.browser"}
        assert not name.startswith(("playwright", "cloakbrowser"))
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


def fake_capture(monkeypatch, vault, *, failure=None):
    def capture_login(**options):
        vault.events.append(("capture", dict(options)))
        if failure is not None:
            raise failure
        return SAVED, [{"operation": "authenticate", "password_captured": False}]

    monkeypatch.setitem(sys.modules, "anghami_session.capture", SimpleNamespace(capture_login=capture_login))


def fake_recovery(monkeypatch, vault, *, failure=None):
    def recover_legacy_session(record, *, proxy=None):
        vault.events.append(("recover", dict(record), proxy))
        if failure is not None:
            raise failure
        return SAVED, {"preparation_method": "http", "server_verified": True}

    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=recover_legacy_session))


def safe_report(vault):
    text = (vault.path.parent / "accounts-prepare-tests-report.json").read_text(encoding="utf-8")
    for secret in (SYNTHETIC_PASSWORD, SYNTHETIC_EMAIL, SAVED["synthetic_session"]):
        assert secret not in text
    return json.loads(text)


@pytest.mark.parametrize("count", [0, -1, True, 1.0, "1", None])
def test_invalid_count_rejected_before_selection(tmp_path, count):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    with pytest.raises(SessionError, match="positive integer"):
        preparation.prepare_test_accounts(vault, count=count)
    assert vault.events == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("row", [0, -1, True, 1.0, "1", None])
def test_invalid_start_rejected_before_selection(tmp_path, row):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    with pytest.raises(SessionError, match="positive integer"):
        preparation.prepare_test_accounts(vault, count=1, start_row=row)
    assert vault.events == [] and list(tmp_path.iterdir()) == []


def test_unsupported_browser_rejected_before_selection(tmp_path):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    with pytest.raises(SessionError, match="supported browser"):
        preparation.prepare_test_accounts(vault, count=1, browser_backend="unsupported")
    assert vault.events == []


@pytest.mark.parametrize("value", [None, 0, 1, 1.0, "true", [], {}])
@pytest.mark.parametrize("use_proxy", [False, True])
def test_invalid_browser_data_selection_stops_before_account_selection(tmp_path, value, use_proxy):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    with pytest.raises(SessionError, match="true or false"):
        preparation.prepare_test_accounts(
            vault, count=1, reduce_browser_data=value,
            proxy=FakeProxy() if use_proxy else None,
        )
    assert vault.events == [] and list(tmp_path.iterdir()) == []


def test_shortage_rejected_before_journal_or_capture(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3", rows=(9,))
    forbid_browser_imports(monkeypatch)
    monkeypatch.setattr(preparation, "_journal", lambda *_: pytest.fail("Shortage reached journal"))
    with pytest.raises(SessionError, match="Only 1 additional unique accounts"):
        preparation.prepare_test_accounts(vault, count=2, start_row=9)
    assert vault.events == [("select", 2, 9)]


@pytest.mark.parametrize("value", [None, 0, 1, 1.0, "true", [], {}])
def test_invalid_no_browser_selection_stops_before_account_selection(tmp_path, value):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    with pytest.raises(SessionError, match="true or false"):
        preparation.prepare_test_accounts(vault, count=1, no_browser=value)
    assert vault.events == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("use_proxy", [False, True])
def test_no_browser_preview_normalizes_browser_options_without_recovery_or_writes(tmp_path, monkeypatch, use_proxy):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    forbid_browser_imports(monkeypatch)
    monkeypatch.setattr(preparation, "_journal", lambda *_: pytest.fail("Preview wrote a report"))
    fake_recovery(monkeypatch, vault, failure=AssertionError("Preview attempted recovery"))
    report = preparation.prepare_test_accounts(
        vault, count=1, no_browser=True, dry_run=True,
        headless=True, reduce_browser_data=True, proxy=FakeProxy() if use_proxy else None,
    )
    assert report["browser"] == "none" and report["preparation_method"] == "http"
    assert report["no_browser"] is True and report["browser_required"] is False
    assert report["headless"] is report["reduce_browser_data"] is False
    assert report["attempted_accounts"] == 0 and report["selected_rows"] == [8]
    assert vault.events == [("select", 1, 1)] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("use_proxy", [False, True])
def test_no_browser_reuses_existing_session_without_recovery_and_keeps_validation(tmp_path, monkeypatch, use_proxy):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3", sessions={8: SAVED})
    forbid_browser_imports(monkeypatch)
    fake_recovery(monkeypatch, vault, failure=AssertionError("Existing session was unnecessarily recovered"))
    proxy = FakeProxy() if use_proxy else None
    report = preparation.prepare_test_accounts(vault, count=1, no_browser=True, proxy=proxy)
    assert vault.events == [
        ("select", 1, 1), ("session", 8), ("attach", 8, SAVED, {} if proxy is None else {"proxy": proxy}), ("enable", 8),
    ]
    assert report["passed"] is True and report["browser"] == "none"
    assert safe_report(vault) == report


@pytest.mark.parametrize("use_proxy", [False, True])
def test_no_browser_missing_session_recovers_exact_record_then_attaches_and_enrolls(tmp_path, monkeypatch, use_proxy):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    forbid_browser_imports(monkeypatch)
    fake_recovery(monkeypatch, vault)
    proxy = FakeProxy() if use_proxy else None
    reports = []
    result = preparation.prepare_test_accounts(vault, count=1, no_browser=True, proxy=proxy, progress=reports.append)
    assert vault.events == [
        ("select", 1, 1), ("session", 8), ("record", 8),
        ("recover", {"email": SYNTHETIC_EMAIL, "password": SYNTHETIC_PASSWORD}, proxy),
        ("attach", 8, SAVED, {} if proxy is None else {"proxy": proxy}), ("enable", 8),
    ]
    if proxy is not None:
        assert vault.events[3][2] is vault.events[4][3]["proxy"] is proxy
    assert "session_recovery" in [report["phase"] for report in reports]
    assert "login" not in [report["phase"] for report in reports]
    assert result["passed"] is True and result["prepared_rows"] == [8]
    assert result["browser"] == "none" and result["browser_required"] is False
    assert result["headless"] is result["reduce_browser_data"] is False
    assert safe_report(vault) == result
    metadata_path = tmp_path / "account-8.session-recovery.redacted.json"
    assert json.loads(metadata_path.read_text()) == {"preparation_method": "http", "server_verified": True}
    assert not (tmp_path / "account-8.login-request.redacted.json").exists()


@pytest.mark.parametrize("failure,error_code", [
    (SessionError(SYNTHETIC_PASSWORD), "preparation_failed"),
    (KeyboardInterrupt(), "cancelled"), (EOFError(), "cancelled"),
])
def test_failed_http_recovery_stops_without_browser_fallback_attach_or_enrollment(tmp_path, monkeypatch, failure, error_code):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    forbid_browser_imports(monkeypatch)
    fake_recovery(monkeypatch, vault, failure=failure)
    with pytest.raises(type(failure)):
        preparation.prepare_test_accounts(vault, count=2, no_browser=True)
    assert [event[0] for event in vault.events] == ["select", "session", "record", "recover"]
    report = safe_report(vault)
    assert report["failed_phase"] == "session_recovery" and report["error_code"] == error_code
    assert report["prepared_rows"] == [] and report["attempted_accounts"] == 1
    assert report["no_browser"] is True and report["browser"] == "none"


def test_recovered_http_session_still_requires_normal_attach_validation(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3", failure=(8, SessionError(SYNTHETIC_PASSWORD)))
    forbid_browser_imports(monkeypatch)
    fake_recovery(monkeypatch, vault)
    with pytest.raises(SessionError):
        preparation.prepare_test_accounts(vault, count=1, no_browser=True)
    assert [event[0] for event in vault.events] == ["select", "session", "record", "recover", "attach"]
    report = safe_report(vault)
    assert report["failed_phase"] == "validation" and report["prepared_rows"] == []
    assert not (tmp_path / "account-8.session-recovery.redacted.json").exists()


@pytest.mark.parametrize("use_proxy", [False, True])
@pytest.mark.parametrize("reduce_browser_data", [False, True])
def test_dry_run_has_no_login_import_validation_enrollment_or_writes(tmp_path, monkeypatch, use_proxy, reduce_browser_data):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    forbid_browser_imports(monkeypatch)
    monkeypatch.setattr(preparation, "_journal", lambda *_: pytest.fail("Preview wrote a report"))
    proxy = FakeProxy() if use_proxy else None
    result = preparation.prepare_test_accounts(vault, count=2, start_row=9, proxy=proxy, headless=True, dry_run=True, reduce_browser_data=reduce_browser_data)
    assert vault.events == [("select", 2, 9)]
    assert result["selected_rows"] == [9, 10]
    assert result["prepared_rows"] == [] and result["prepared_account_count"] == 0
    assert result["attempted_accounts"] == 0 and result["dry_run"] is True
    assert result["passed"] is False and result["phase"] == "preview"
    assert result["headless"] is True and result["browser"] == "chrome"
    assert result["reduce_browser_data"] is reduce_browser_data
    assert result["like_events_sent"] == result["play_events_sent"] == 0
    assert list(tmp_path.iterdir()) == []
    if proxy is not None:
        assert result["proxy"] == proxy.summary()
    else:
        assert result["connection"] == "direct"


def test_reuses_existing_sessions_and_enables_only_after_validation(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3", sessions={8: SAVED, 9: SAVED})
    forbid_browser_imports(monkeypatch)
    result = preparation.prepare_test_accounts(vault, count=2)
    assert vault.events == [
        ("select", 2, 1), ("session", 8), ("attach", 8, SAVED, {}), ("enable", 8),
        ("session", 9), ("attach", 9, SAVED, {}), ("enable", 9),
    ]
    assert result["passed"] is True and result["prepared_rows"] == [8, 9]
    assert result["prepared_account_count"] == result["attempted_accounts"] == 2
    assert safe_report(vault) == result


@pytest.mark.parametrize("backend,headless", [("chrome", False), ("chrome", True), ("cloakbrowser", True)])
@pytest.mark.parametrize("use_proxy", [False, True])
@pytest.mark.parametrize("reduce_browser_data", [False, True])
def test_missing_session_normal_login_uses_record_and_bound_proxy(tmp_path, monkeypatch, backend, headless, use_proxy, reduce_browser_data):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    fake_capture(monkeypatch, vault)
    proxy = FakeProxy() if use_proxy else None
    result = preparation.prepare_test_accounts(vault, count=1, proxy=proxy, browser_backend=backend, headless=headless, reduce_browser_data=reduce_browser_data)
    capture_options = {
        "email": SYNTHETIC_EMAIL, "password": SYNTHETIC_PASSWORD,
        "browser_backend": backend, "headless": headless,
    }
    attach_options = {}
    if reduce_browser_data:
        capture_options["reduce_browser_data"] = True
    if proxy is not None:
        capture_options["proxy"] = proxy
        attach_options["proxy"] = proxy
    assert vault.events == [
        ("select", 1, 1), ("session", 8), ("record", 8), ("capture", capture_options),
        ("attach", 8, SAVED, attach_options), ("enable", 8),
    ]
    if proxy is not None:
        assert vault.events[3][1]["proxy"] is vault.events[4][3]["proxy"] is proxy
    assert result["passed"] is True and result["prepared_rows"] == [8]
    assert result["reduce_browser_data"] is reduce_browser_data
    assert safe_report(vault) == result
    assert json.loads((tmp_path / "account-8.login-request.redacted.json").read_text()) == [
        {"operation": "authenticate", "password_captured": False},
    ]


@pytest.mark.parametrize("failure,error_code", [
    (SessionError(SYNTHETIC_PASSWORD), "preparation_failed"),
    (KeyboardInterrupt(), "cancelled"),
    (EOFError(), "cancelled"),
])
def test_second_validation_failure_preserves_prepared_first_row_and_stops(tmp_path, monkeypatch, failure, error_code):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3", sessions={8: SAVED, 9: SAVED, 10: SAVED}, failure=(9, failure))
    forbid_browser_imports(monkeypatch)
    with pytest.raises(type(failure)):
        preparation.prepare_test_accounts(vault, count=3)
    assert vault.events == [
        ("select", 3, 1), ("session", 8), ("attach", 8, SAVED, {}), ("enable", 8),
        ("session", 9), ("attach", 9, SAVED, {}),
    ]
    report = safe_report(vault)
    assert report["prepared_rows"] == [8] and report["prepared_account_count"] == 1
    assert report["attempted_accounts"] == 2 and report["failed_row"] == 9
    assert report["phase"] == "stopped" and report["failed_phase"] == "validation"
    assert report["passed"] is False and report["error_code"] == error_code


def test_cancelled_login_does_not_attach_or_enable(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    fake_capture(monkeypatch, vault, failure=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        preparation.prepare_test_accounts(vault, count=2)
    assert [event[0] for event in vault.events] == ["select", "session", "record", "capture"]
    report = safe_report(vault)
    assert report["prepared_rows"] == [] and report["failed_row"] == 8
    assert report["failed_phase"] == "login" and report["error_code"] == "cancelled"


def test_login_rejection_enters_review_and_keeps_preparing_the_next_account(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3", sessions={9: SAVED})
    error = LoginCaptureError(
        "login_rejected", stage="home", page_http_status=200, auth_http_status=200,
        authentication_result="failed",
    )
    error.args = (SYNTHETIC_PASSWORD,)
    error.headers = {"cookie": SAVED["synthetic_session"]}
    error.email = SYNTHETIC_EMAIL
    fake_capture(monkeypatch, vault, failure=error)
    reports = []
    preparation.prepare_test_accounts(vault, count=2, progress=reports.append)
    assert [event[0] for event in vault.events] == ["select", "session", "record", "capture", "review", "session", "attach", "enable"]
    assert vault.sessions == {9: SAVED}
    report = safe_report(vault)
    assert report["prepared_rows"] == [9] and report["attempted_accounts"] == 2
    assert report["account_failed_rows"] == [8] and report["connection_pending_rows"] == []
    assert report["passed"] is False and report["phase"] == "complete"
    assert vault.reviewed[8]["code"] == "session_authentication_rejected"
    assert report["login_failure"] == {
        "code": "login_rejected", "stage": "home", "page_http_status": 200,
        "auth_http_status": 200, "authentication_result": "failed",
    }
    assert reports[-1] == report
    assert not (tmp_path / "account-8.login-request.redacted.json").exists()


def test_unvalidated_login_failure_enum_falls_back_to_generic_report(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    error = LoginCaptureError("login_timeout", stage="home")
    error.stage = SYNTHETIC_PASSWORD
    fake_capture(monkeypatch, vault, failure=error)
    with pytest.raises(LoginCaptureError):
        preparation.prepare_test_accounts(vault, count=1)
    report = safe_report(vault)
    assert report["error_code"] == "preparation_failed"
    assert "login_failure" not in report


def test_final_journal_failure_never_returns_or_persists_success(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3", sessions={8: SAVED})
    original_journal = preparation._journal
    rejected = []

    def journal(report, path):
        if report.get("passed"):
            rejected.append(True)
            raise SessionError("Synthetic final journal failure")
        original_journal(report, path)

    monkeypatch.setattr(preparation, "_journal", journal)
    with pytest.raises(SessionError, match="final journal failure"):
        preparation.prepare_test_accounts(vault, count=1)
    assert rejected == [True]
    report = safe_report(vault)
    assert report["passed"] is False and report["phase"] == "stopped"
    assert report["prepared_rows"] == [8] and report["prepared_account_count"] == 1


class CLIVault:
    def __init__(self, path):
        self.path = path
        self.events = []
        self.enrolled = {7, 8, 9, 10, 11}
        self.failure_at = None
        self.runs = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def record(self, row):
        self.events.append(("record", row))
        return {"email": SYNTHETIC_EMAIL, "password": SYNTHETIC_PASSWORD}

    def enrolled_test_rows(self):
        self.events.append(("enrolled",))
        return set(self.enrolled)

    def test_accounts(self):
        self.events.append(("test_accounts",))
        return {"test_rows": sorted(self.enrolled)}

    def find(self, email):
        self.events.append(("find", email))
        return [7]

    def run(self, operation, row, song_id, options):
        self.events.append((operation, row, song_id, options))
        self.runs += 1
        if self.failure_at == self.runs:
            raise SessionError("Synthetic runner failure")
        return {"passed": True, "source_row": row, "operation": operation}

    def test_like(self, row, song_id, **options):
        return self.run("test-like", row, song_id, options)

    def test_play_record(self, row, song_id, **options):
        return self.run("test-play-record", row, song_id, options)


@pytest.fixture
def cli_vault(tmp_path, monkeypatch):
    vault = CLIVault(tmp_path / "synthetic.sqlite3")
    monkeypatch.setattr(accounts, "AccountVault", lambda path: vault)
    return vault


@pytest.mark.parametrize("use_proxy", [False, True])
@pytest.mark.parametrize("reduce_browser_data", [False, True])
def test_prepare_cli_forwards_count_start_browser_headless_preview_and_proxy(cli_vault, monkeypatch, capsys, use_proxy, reduce_browser_data):
    calls = []
    proxy = FakeProxy()
    monkeypatch.setattr(accounts, "load_packetstream_proxy", lambda path: calls.append(("proxy", path)) or proxy)
    monkeypatch.setattr(preparation, "prepare_test_accounts", lambda vault, **options: calls.append(("prepare", vault, options)) or {"selected_rows": [8, 9]})
    args = ["prepare-tests", "--vault", str(cli_vault.path), "--count", "2", "--start-row", "8", "--browser", "chrome", "--headless", "--dry-run"]
    if use_proxy:
        args.append("--proxy-egypt")
    if reduce_browser_data:
        args.append("--reduce-browser-data")
    assert accounts.main(args) == 0
    assert calls[-1] == ("prepare", cli_vault, {
        "count": 2, "start_row": 8, "proxy": proxy if use_proxy else None,
        "browser_backend": "chrome", "headless": True, "dry_run": True,
        **({"reduce_browser_data": True} if reduce_browser_data else {}),
    })
    if use_proxy:
        assert calls[0] == ("proxy", cli_vault.path.parent / "packetstream.dpapi")
    else:
        assert len(calls) == 1
    assert cli_vault.events == [] and json.loads(capsys.readouterr().out)["selected_rows"] == [8, 9]


@pytest.mark.parametrize("use_proxy", [False, True])
def test_prepare_cli_no_browser_forwards_http_mode_without_browser_download_options(cli_vault, monkeypatch, capsys, use_proxy):
    calls = []
    proxy = FakeProxy()
    monkeypatch.setattr(accounts, "load_packetstream_proxy", lambda path: calls.append(("proxy", path)) or proxy)
    monkeypatch.setattr(preparation, "prepare_test_accounts", lambda vault, **options: calls.append(("prepare", vault, options)) or {"selected_rows": [8]})
    args = ["prepare-tests", "--vault", str(cli_vault.path), "--count", "1", "--start-row", "8",
            "--no-browser", "--headless", "--reduce-browser-data", "--dry-run"]
    if use_proxy:
        args.append("--proxy-egypt")
    assert accounts.main(args) == 0
    assert calls[-1] == ("prepare", cli_vault, {
        "count": 1, "start_row": 8, "proxy": proxy if use_proxy else None,
        "browser_backend": "chrome", "headless": False, "dry_run": True, "no_browser": True,
    })
    assert cli_vault.events == [] and json.loads(capsys.readouterr().out)["selected_rows"] == [8]


@pytest.mark.parametrize("use_proxy", [False, True])
@pytest.mark.parametrize("reduce_browser_data", [False, True])
def test_login_cli_browser_data_choice_keeps_proxy_and_account_validation(cli_vault, monkeypatch, capsys, use_proxy, reduce_browser_data):
    proxy = FakeProxy()
    captures = []
    attachments = []
    proxy_loads = []

    def capture_login(**options):
        captures.append(dict(options))
        return SAVED, [{"operation": "authenticate"}]

    def attach(row, saved, **options):
        attachments.append((row, saved, options))
        return {"source_row": row, "authenticated": True}

    monkeypatch.setitem(sys.modules, "anghami_session.capture", SimpleNamespace(capture_login=capture_login))
    monkeypatch.setattr(cli_vault, "attach", attach, raising=False)
    monkeypatch.setattr(accounts, "load_packetstream_proxy", lambda path: proxy_loads.append(path) or proxy)
    args = ["login", "--row", "7", "--vault", str(cli_vault.path), "--headless", "--browser", "chrome"]
    if use_proxy:
        args.append("--proxy-egypt")
    if reduce_browser_data:
        args.append("--reduce-browser-data")
    assert accounts.main(args) == 0
    assert captures == [{
        "email": SYNTHETIC_EMAIL, "password": SYNTHETIC_PASSWORD,
        "headless": True, "browser_backend": "chrome",
        **({"proxy": proxy} if use_proxy else {}),
        **({"reduce_browser_data": True} if reduce_browser_data else {}),
    }]
    assert attachments == [(7, SAVED, {"new_password": None, "review_session": True,
                                     **({"proxy": proxy} if use_proxy else {})})]
    assert proxy_loads == ([cli_vault.path.parent / "packetstream.dpapi"] if use_proxy else [])
    output = capsys.readouterr().out
    assert SYNTHETIC_PASSWORD not in output and SYNTHETIC_EMAIL not in output
    report = json.loads(output)
    assert report["reduce_browser_data"] is reduce_browser_data
    assert json.loads((cli_vault.path.parent / "accounts-last-operation.json").read_text()) == report


def test_test_accounts_cli_is_pure_listing(cli_vault, monkeypatch, capsys):
    forbid_browser_imports(monkeypatch)
    monkeypatch.setattr(accounts, "load_packetstream_proxy", lambda *_: pytest.fail("Listing loaded proxy"))
    assert accounts.main(["test-accounts", "--vault", str(cli_vault.path)]) == 0
    assert cli_vault.events == [("test_accounts",)] and cli_vault.runs == 0
    assert json.loads(capsys.readouterr().out) == {"test_rows": [7, 8, 9, 10, 11]}
    assert list(cli_vault.path.parent.iterdir()) == []


@pytest.mark.parametrize("command", ["test-like", "test-play-record"])
@pytest.mark.parametrize("selector", [["--row", "7"], ["--email", SYNTHETIC_EMAIL]])
def test_rows_selector_is_mutually_exclusive(command, selector):
    with pytest.raises(SystemExit) as exc:
        accounts.build_parser().parse_args([command, "--rows", "7", "8", *selector, "--song-id", TEST_SONG_ID])
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["test-like", "test-play-record"])
@pytest.mark.parametrize("rows,song_id", [
    ([0, 8], TEST_SONG_ID), ([-1, 8], TEST_SONG_ID), ([7, 7], TEST_SONG_ID),
    ([7, 8, 9, 10, 11, 12], TEST_SONG_ID), ([7, 12], TEST_SONG_ID),
    ([7, 8], "synthetic-other-song"),
])
def test_all_multiaccount_scope_checks_precede_proxy_and_runner(cli_vault, monkeypatch, capsys, command, rows, song_id):
    monkeypatch.setattr(accounts, "load_packetstream_proxy", lambda *_: pytest.fail("Bad cohort reached proxy"))
    args = [command, "--vault", str(cli_vault.path), "--rows", *map(str, rows), "--song-id", song_id, "--proxy-egypt"]
    assert accounts.main(args) == 1
    assert cli_vault.runs == 0
    assert capsys.readouterr().out == ""
    assert list(cli_vault.path.parent.iterdir()) == []


@pytest.mark.parametrize("command", ["test-like", "test-play-record"])
def test_multiaccount_tests_are_sequential_for_each_row_and_count(cli_vault, monkeypatch, capsys, command):
    proxy = FakeProxy()
    monkeypatch.setattr(accounts, "load_packetstream_proxy", lambda *_: proxy)
    assert accounts.main([
        command, "--vault", str(cli_vault.path), "--rows", "9", "7", "8",
        "--song-id", TEST_SONG_ID, "--count", "2", "--proxy-egypt",
    ]) == 0
    assert cli_vault.events[:4] == [("record", 9), ("record", 7), ("record", 8), ("enrolled",)]
    assert cli_vault.events[4:] == [(command, row, TEST_SONG_ID, {"proxy": proxy}) for row in (9, 9, 7, 7, 8, 8)]
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True and report["selected_rows"] == [9, 7, 8]
    assert report["completed_accounts"] == report["requested_accounts"] == 3
    assert report["tests_per_account"] == 2 and report["automatic_retry"] is False
    assert all(result["completed_tests"] == 2 for result in report["results"])


@pytest.mark.parametrize("command", ["test-like", "test-play-record"])
def test_multiaccount_failure_stops_all_remaining_tests(cli_vault, capsys, command):
    cli_vault.failure_at = 3
    assert accounts.main([
        command, "--vault", str(cli_vault.path), "--rows", "7", "8", "9",
        "--song-id", TEST_SONG_ID, "--count", "2",
    ]) == 1
    assert cli_vault.events[4:] == [(command, row, TEST_SONG_ID, {}) for row in (7, 7, 8)]
    report = json.loads((cli_vault.path.parent / f"{command}.accounts-batch-report.json").read_text())
    assert report["passed"] is False and report["phase"] == "stopped"
    assert report["completed_accounts"] == 1 and report["attempted_accounts"] == 2
    assert report["failed_row"] == 8 and len(report["results"]) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", ["test-like", "test-play-record"])
@pytest.mark.parametrize("selector", ["row", "rows", "email"])
def test_default_single_account_keeps_one_legacy_runner_call(cli_vault, capsys, command, selector):
    value = SYNTHETIC_EMAIL if selector == "email" else "7"
    assert accounts.main([
        command, "--vault", str(cli_vault.path), "--" + selector, value, "--song-id", TEST_SONG_ID,
    ]) == 0
    assert cli_vault.runs == 1
    assert cli_vault.events[-1] == (command, 7, TEST_SONG_ID, {})
    assert json.loads(capsys.readouterr().out) == {"passed": True, "source_row": 7, "operation": command}
    assert list(cli_vault.path.parent.iterdir()) == []
