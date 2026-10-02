"""Offline account migration; all network/browser behavior is replaced by fakes."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from anghami_session import accounts, client, vault
from anghami_session import __main__ as root_cli
from anghami_session.errors import SessionError
from anghami_session.store import load_protected_bytes

pytestmark = pytest.mark.skipif(os.name != "nt", reason="DPAPI account storage requires Windows")


@pytest.fixture
def account_files(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not contact Anghami")
    monkeypatch.setattr(client.requests, "Session", forbidden)
    raw = (
        "\ufeffEG~first@example.com~private-password-one~appsidsave=old-one;session_uuid=uuid-one~fingerprint=fp-one;cookie=value=with=equals\r\n"
        "\r\n"
        "LB~second@example.com~private-password-two~appsidsave=old-two~fingerprint=fp-two\r\n"
        "EG~FIRST@example.com~private-duplicate-password~appsidsave=old-duplicate~fingerprint=fp-duplicate\r\n"
    ).encode("utf-8")
    source = tmp_path / "registered.txt"
    source.write_bytes(raw)
    return source, tmp_path / ".anghami" / "accounts.sqlite3", raw


@pytest.fixture
def saved_session():
    return {
        "format_version": 1, "created_at_utc": "2026-09-30T00:00:00+00:00",
        "origin": "https://play.anghami.com", "account_email": "second@example.com",
        "requests": {
            "relations": {
                "method": "GET",
                "url": client.GATEWAY_URL + "?type=GETuserrelations&sid=private-new-session",
                "headers": {"cookie": "appsidsave=private-new-session"},
            },
        },
    }


@pytest.fixture
def fake_session(monkeypatch):
    state = {"fail": False, "calls": []}

    class Session:
        def __init__(self, *, saved):
            state["calls"].append(deepcopy(saved))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def check(self, *, negative_control):
            assert negative_control is True
            if state["fail"]:
                raise SessionError("The saved session was not accepted.")
            return {"authenticated": True, "checked_at_utc": "2026-09-30T01:00:00+00:00"}

        def request(self, operation):
            return {"status": "ok", "operation": operation}

    monkeypatch.setattr(vault, "AnghamiSession", Session)
    return state


def test_offline_import_preserves_duplicates_line_numbers_and_encrypted_recovery(account_files):
    source, destination, raw = account_files
    progress = []
    result = vault.migrate_registered(source, destination, progress=lambda *args: progress.append(args))
    assert result["records"] == 3
    assert result["unique_accounts"] == 2
    assert result["duplicate_rows"] == 1
    assert result["states"] == {"login_required": 3}
    assert result["sessions_saved"] == 0
    assert progress == [(3, 3)]
    assert source.read_bytes() == raw
    backup = Path(result["encrypted_source_backup"])
    assert load_protected_bytes(backup) == raw
    for private in [b"first@example.com", b"private-password-one", b"old-two", b"fp-duplicate"]:
        assert private not in destination.read_bytes()
        assert private not in backup.read_bytes()
    with vault.AccountVault(destination) as saved:
        assert saved.find(" FIRST@example.com ") == [1, 4]
        assert saved.find("second@example.com") == [3]
        assert saved.record(1)["legacy_cookies"]["cookie"] == "value=with=equals"
        assert saved.record(3)["password"] == "private-password-two"
        output = destination.parent / "recovered.txt"
        saved.restore_source(output)
        assert output.read_bytes() == raw
        with pytest.raises(FileExistsError):
            saved.restore_source(source)
        assert source.read_bytes() == raw


def test_reimport_is_idempotent_and_preserves_new_sessions(account_files, saved_session, fake_session):
    source, destination, _ = account_files
    vault.migrate_registered(source, destination)
    with vault.AccountVault(destination) as saved:
        saved.attach(3, saved_session)
    before = destination.read_bytes()
    result = vault.migrate_registered(source, destination)
    assert result["already_imported"] is True
    assert result["sessions_saved"] == 1
    assert destination.read_bytes() == before


def test_changed_source_and_invalid_rows_do_not_overwrite_existing_data(account_files):
    source, destination, raw = account_files
    vault.migrate_registered(source, destination)
    previous = destination.read_bytes()
    source.write_bytes(raw + b"EG~added@example.com~password~sid=x~fingerprint=y\n")
    with pytest.raises(SessionError, match="different import"):
        vault.migrate_registered(source, destination)
    assert destination.read_bytes() == previous
    invalid = source.parent / "invalid.txt"
    invalid.write_text("do-not-print-this-secret", encoding="utf-8")
    new_destination = source.parent / "new" / "accounts.sqlite3"
    with pytest.raises(SessionError, match="source line 1") as error:
        vault.migrate_registered(invalid, new_destination)
    assert "do-not-print-this-secret" not in str(error.value)
    assert not new_destination.exists()
    assert not new_destination.parent.exists()


def test_partial_encryption_failure_never_publishes_a_partial_vault(account_files, monkeypatch):
    source, destination, raw = account_files
    original_pack = vault._pack

    def fail_second(record):
        if record["source_row"] == 3:
            raise OSError("simulated encryption error")
        return original_pack(record)

    monkeypatch.setattr(vault, "_pack", fail_second)
    with pytest.raises(OSError):
        vault.migrate_registered(source, destination)
    assert not destination.exists()
    assert not list(destination.parent.glob("*.tmp*"))
    assert source.read_bytes() == raw
    backups = list((destination.parent / "backups").glob("*.dpapi"))
    assert len(backups) == 1
    assert load_protected_bytes(backups[0]) == raw


def test_session_cannot_be_assigned_to_another_account_or_unnamed_capture(account_files, saved_session, fake_session):
    source, destination, _ = account_files
    vault.migrate_registered(source, destination)
    with vault.AccountVault(destination) as saved:
        with pytest.raises(SessionError, match="does not identify"):
            saved.attach(1, saved_session)
        unnamed = deepcopy(saved_session)
        del unnamed["account_email"]
        with pytest.raises(SessionError, match="does not identify"):
            saved.attach(3, unnamed)
        assert saved.summary()["sessions_saved"] == 0
    assert not fake_session["calls"]


def test_selected_session_is_isolated_and_failed_renewal_keeps_previous(account_files, saved_session, fake_session):
    source, destination, _ = account_files
    vault.migrate_registered(source, destination)
    with vault.AccountVault(destination) as saved:
        report = saved.attach(3, saved_session)
        assert report["source_row"] == 3
        assert saved.session(3) == saved_session
        with pytest.raises(SessionError, match="needs a normal login"):
            saved.session(1)
        fake_session["fail"] = True
        replacement = deepcopy(saved_session)
        replacement["created_at_utc"] = "2026-10-01T00:00:00+00:00"
        with pytest.raises(SessionError):
            saved.attach(3, replacement)
        assert saved.session(3) == saved_session
        assert saved.summary()["states"] == {"login_required": 2, "ready": 1}
        with pytest.raises(SessionError):
            saved.check(3)
        assert saved.summary()["states"] == {"check_failed": 1, "login_required": 2}
        assert saved.session(3) == saved_session
        fake_session["fail"] = False
        saved.check(3)
        assert saved.summary()["states"] == {"login_required": 2, "ready": 1}


def test_cli_selects_a_single_account_and_sends_credentials_only_to_login(account_files, saved_session, fake_session, monkeypatch, capsys):
    from anghami_session import capture
    source, destination, _ = account_files
    assert root_cli.main(["accounts", "import", "--source", str(source), "--vault", str(destination)]) == 0
    received = []

    def fake_capture(**kwargs):
        received.append(kwargs)
        return deepcopy(saved_session), [{"request_header_names": ["accept"]}]

    monkeypatch.setattr(capture, "capture_login", fake_capture)
    assert accounts.main(["login", "--row", "3", "--vault", str(destination)]) == 0
    assert received == [{"email": "second@example.com", "password": "private-password-two"}]
    assert accounts.main(["find", "--email", "FIRST@example.com", "--vault", str(destination)]) == 0
    output = capsys.readouterr()
    for private in ["private-password", "private-new-session", "second@example.com"]:
        assert private not in output.out + output.err
    with vault.AccountVault(destination) as saved:
        assert saved.summary()["sessions_saved"] == 1
        assert saved.request(3, "playlists") == {"status": "ok", "operation": "playlists"}


def test_duplicate_email_requires_explicit_row_and_never_starts_login(account_files, monkeypatch, capsys):
    from anghami_session import capture
    source, destination, _ = account_files
    vault.migrate_registered(source, destination)

    def forbidden(**kwargs):
        raise AssertionError("Ambiguous account must not start login")

    monkeypatch.setattr(capture, "capture_login", forbidden)
    assert accounts.main(["login", "--email", "first@example.com", "--vault", str(destination)]) == 1
    assert "Select --row from: 1, 4" in capsys.readouterr().err


def test_failed_cli_login_preserves_saved_session_and_redacts_browser_errors(account_files, saved_session, fake_session, monkeypatch, capsys):
    from anghami_session import capture
    source, destination, _ = account_files
    vault.migrate_registered(source, destination)
    with vault.AccountVault(destination) as saved:
        saved.attach(3, saved_session)

    def fail(**kwargs):
        raise RuntimeError("browser exception containing " + kwargs["password"])

    monkeypatch.setattr(capture, "capture_login", fail)
    assert accounts.main(["login", "--row", "3", "--vault", str(destination)]) == 1
    output = capsys.readouterr()
    assert "private-password" not in output.out + output.err
    assert "RuntimeError" in output.err
    with vault.AccountVault(destination) as saved:
        assert saved.session(3) == saved_session


def test_all_imported_records_match_source_and_backup_checksum(account_files):
    source, destination, raw = account_files
    report = vault.migrate_registered(source, destination)
    assert report["source_sha256"] == hashlib.sha256(raw).hexdigest()
    original_records = vault.parse_registered(raw)
    with vault.AccountVault(destination) as saved:
        assert [saved.record(record["source_row"]) for record in original_records] == original_records
        with pytest.raises(SessionError, match="No account exists"):
            saved.record(2)


def test_password_update_is_hidden_and_saved_only_after_verified_login(account_files, saved_session, fake_session, monkeypatch, capsys):
    from anghami_session import capture
    source, destination, raw = account_files
    vault.migrate_registered(source, destination)
    monkeypatch.setattr(accounts.getpass, "getpass", lambda prompt: "new-private-password")

    def fake_capture(**kwargs):
        assert kwargs == {"email": "second@example.com", "password": "new-private-password"}
        return deepcopy(saved_session), []

    monkeypatch.setattr(capture, "capture_login", fake_capture)
    command = ["login", "--row", "3", "--prompt-password", "--vault", str(destination)]
    fake_session["fail"] = True
    assert accounts.main(command) == 1
    with vault.AccountVault(destination) as saved:
        assert saved.record(3)["password"] == "private-password-two"
    fake_session["fail"] = False
    assert accounts.main(command) == 0
    with vault.AccountVault(destination) as saved:
        assert saved.record(3)["password"] == "new-private-password"
        assert saved.record(1)["password"] == "private-password-one"
        assert saved.session(3)["account_email"] == "second@example.com"
    assert source.read_bytes() == raw
    assert b"new-private-password" not in destination.read_bytes()
    output = capsys.readouterr()
    assert "new-private-password" not in output.out + output.err


def test_headless_login_flag_uses_only_selected_account(account_files, saved_session, fake_session, monkeypatch):
    from anghami_session import capture
    source, destination, _ = account_files
    vault.migrate_registered(source, destination)
    received = []

    def fake_capture(**kwargs):
        received.append(kwargs)
        return deepcopy(saved_session), []

    monkeypatch.setattr(capture, "capture_login", fake_capture)
    assert accounts.main(["login", "--row", "3", "--headless", "--vault", str(destination)]) == 0
    assert received == [{"email": "second@example.com", "password": "private-password-two", "headless": True}]


@pytest.mark.parametrize("backend", ["cloakbrowser", "chrome"])
def test_explicit_login_browser_preserves_default_call_and_selects_chrome(account_files, saved_session, fake_session, monkeypatch, backend):
    from anghami_session import capture
    source, destination, _ = account_files
    vault.migrate_registered(source, destination)
    received = []

    def fake_capture(**kwargs):
        received.append(kwargs)
        return deepcopy(saved_session), []

    monkeypatch.setattr(capture, "capture_login", fake_capture)
    assert accounts.main([
        "login", "--row", "3", "--headless", "--browser", backend,
        "--vault", str(destination),
    ]) == 0
    expected = {"email": "second@example.com", "password": "private-password-two", "headless": True}
    if backend == "chrome":
        expected["browser_backend"] = "chrome"
    assert received == [expected]
