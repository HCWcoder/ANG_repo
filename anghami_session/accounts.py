"""Migrate the account list offline and manage a selected account's normal login."""

import argparse
import getpass
import json
from pathlib import Path
import sys

from .errors import SessionError
from .proxy import load_packetstream_proxy, save_packetstream_credentials
from .store import load_session
from .vault import AccountVault, DEFAULT_VAULT_PATH, ROOT, migrate_registered


def _select(vault: AccountVault, args) -> int:
    if args.row is not None:
        if args.row < 1:
            raise SessionError("Source row must be a positive integer.")
        vault.record(args.row)
        return args.row
    email = args.email
    if email is None:
        email = input("Account email from registered.txt: ").strip()
    rows = vault.find(email)
    if not rows:
        raise SessionError("No imported account matches that email.")
    if len(rows) > 1:
        raise SessionError("That email occurs more than once. Select --row from: " + ", ".join(map(str, rows)))
    return rows[0]


def _select_test_rows(vault, args):
    rows = getattr(args, "rows", None)
    if rows is None:
        return [_select(vault, args)]
    if not 1 <= len(rows) <= 5 or len(set(rows)) != len(rows) or any(row < 1 for row in rows):
        raise SessionError("Choose 1-5 distinct positive source rows for this test run.")
    for row in rows:
        vault.record(row)
    if not set(rows).issubset(vault.enrolled_test_rows()):
        raise SessionError("One or more rows are not prepared for testing. Use accounts prepare-tests and accounts test-accounts.")
    from .play_record import TEST_SONG_ID
    if args.song_id != TEST_SONG_ID:
        raise SessionError(f"This test command is limited to the declared test song {TEST_SONG_ID}.")
    return rows


def _test_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Test count must be an integer from 1 to 5.") from None
    if not 1 <= count <= 5:
        raise argparse.ArgumentTypeError("Test count must be from 1 to 5 for this test setup.")
    return count


def _preparation_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Preparation count must be a positive integer.") from None
    if count < 1:
        raise argparse.ArgumentTypeError("Preparation count must be a positive integer.")
    return count


def _run_repeated_test(vault, args, row, proxy):
    runner = vault.test_play_record if args.command == "test-play-record" else vault.test_like
    options = {} if proxy is None else {"proxy": proxy}
    if args.count == 1:
        return runner(row, args.song_id, **options)
    from .play_record import _journal
    report_path = args.vault.parent / f"account-{row}.{args.command}-batch-report.json"
    batch = {
        "source_row": row, "song_id": args.song_id, "operation": args.command,
        "requested_tests": args.count, "attempted_tests": 0, "completed_tests": 0,
        "passed": False, "phase": "running", "automatic_retry": False,
        "results": [],
    }
    if proxy is not None:
        batch["proxy"] = proxy.summary()
    _journal(batch, report_path)
    try:
        for number in range(1, args.count + 1):
            batch.update({"attempted_tests": number, "active_test": number})
            _journal(batch, report_path)
            result = runner(row, args.song_id, **options)
            batch["results"].append(result)
            batch["completed_tests"] = number
            _journal(batch, report_path)
        batch.update({"passed": True, "phase": "complete"})
        _journal(batch, report_path)
        return batch
    except Exception:
        batch.update({"passed": False, "phase": "stopped", "failed_test": batch["attempted_tests"]})
        _journal(batch, report_path)
        raise


def _run_selected_tests(vault, args, rows, proxy):
    if len(rows) == 1:
        return _run_repeated_test(vault, args, rows[0], proxy)
    from .play_record import _journal
    report_path = args.vault.parent / f"{args.command}.accounts-batch-report.json"
    report = {
        "operation": args.command, "song_id": args.song_id, "selected_rows": rows,
        "requested_accounts": len(rows), "tests_per_account": args.count,
        "attempted_accounts": 0, "completed_accounts": 0, "results": [],
        "passed": False, "phase": "running", "automatic_retry": False,
    }
    if proxy is not None:
        report["proxy"] = proxy.summary()
    _journal(report, report_path)
    try:
        for number, row in enumerate(rows, 1):
            report.update({"active_row": row, "attempted_accounts": number})
            _journal(report, report_path)
            report["results"].append(_run_repeated_test(vault, args, row, proxy))
            report["completed_accounts"] = number
            _journal(report, report_path)
        report.update({"passed": True, "phase": "complete"})
        _journal(report, report_path)
        return report
    except BaseException:
        report.update({"passed": False, "phase": "stopped", "failed_row": report.get("active_row")})
        _journal(report, report_path)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    help_text = {
        "import": "Import registered.txt offline, preserving an encrypted backup",
        "status": "Show counts and session readiness without exposing credentials",
        "find": "Find source row numbers for an email",
        "login": "Refresh one selected account through its normal browser login",
        "attach": "Validate and attach an account-bound encrypted session",
        "check": "Check one selected session using read-only HTTP requests",
        "relations": "Read one selected account's relations",
        "playlists": "Read one selected account's playlists",
        "song": "Read song metadata using one selected saved session",
        "probe-playback": "Check media delivery and silent audio decoding without a browser",
        "test-play-record": "Send one synthetic play record for the declared test song using a selected test account",
        "test-like": "Like the declared test song once and verify the selected account's saved like state",
        "proxy-configure": "Save the PacketStream proxy key in the local encrypted store",
        "prepare-tests": "Prepare additional unique registered accounts and enable their saved sessions for testing",
        "test-accounts": "List the test account rows and saved session readiness",
        "restore-source": "Restore the exact original file to a new output path",
    }
    for name, help_line in help_text.items():
        command = subcommands.add_parser(name, help=help_line)
        command.add_argument("--vault", type=Path, default=DEFAULT_VAULT_PATH)
        if name == "import":
            command.add_argument("--source", type=Path, default=ROOT / "registered.txt")
        elif name == "find":
            command.add_argument("--email", required=True)
        elif name == "restore-source":
            command.add_argument("--output", type=Path, required=True)
        elif name == "proxy-configure":
            command.add_argument("--username", help="PacketStream proxy username; omitted values prompt locally")
        elif name == "prepare-tests":
            command.add_argument("--count", type=_preparation_count, required=True, help="Positive number of additional unique accounts to prepare")
            command.add_argument("--start-row", type=int, default=1, help="Look for additional accounts from this registered.txt source row onward")
            command.add_argument("--browser", choices=("chrome", "cloakbrowser"), default="chrome")
            command.add_argument("--headless", action="store_true", help="Capture normal logins without visible browser windows")
            command.add_argument("--reduce-browser-data", action="store_true", help="Reduce optional browser downloads during login")
            command.add_argument("--no-browser", action="store_true", help="Reuse a saved session or recover this row's legacy SID through HTTP; never open a browser")
            command.add_argument("--proxy-egypt", action="store_true", help="Use the Egypt proxy for browser login and HTTP validation")
            command.add_argument("--dry-run", action="store_true", help="Preview selected row numbers without login or changes")
        elif name not in {"status", "test-accounts"}:
            selector = command.add_mutually_exclusive_group()
            selector.add_argument("--row", type=int, help="Original line number in registered.txt")
            selector.add_argument("--email", help="Select a unique email; omitted selectors prompt locally")
            if name in {"test-play-record", "test-like"}:
                selector.add_argument("--rows", type=int, nargs="+", help="Test 1-5 prepared source rows sequentially")
            if name in {"song", "probe-playback", "test-play-record", "test-like"}:
                command.add_argument("--song-id", required=True)
            if name in {"login", "relations", "playlists", "test-play-record", "test-like"}:
                command.add_argument("--proxy-egypt", action="store_true", help="Use the encrypted PacketStream credentials and verify an Egypt exit before Anghami requests")
            if name in {"test-play-record", "test-like"}:
                command.add_argument("--count", type=_test_count, default=1, help="Sequential tests of this selected row and song (1-5; default 1)")
            if name == "probe-playback":
                command.add_argument("--seconds", type=float, default=5, help="Seconds of decoded audio to consume silently (1-30; default 5)")
            if name == "attach":
                command.add_argument("--session", type=Path, required=True)
            if name == "login":
                command.add_argument("--prompt-password", action="store_true", help="Enter a changed password locally; save it only after successful login")
                command.add_argument("--headless", action="store_true", help="Sign in without opening a visible browser window")
                command.add_argument("--reduce-browser-data", action="store_true", help="Reduce optional browser downloads during login")
                command.add_argument("--browser", choices=("cloakbrowser", "chrome"), default="cloakbrowser", help="Choose CloakBrowser or installed Chrome for normal login")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "proxy-configure":
            username = args.username or input("PacketStream proxy username: ").strip()
            auth_key = getpass.getpass("PacketStream proxy auth key (hidden): ")
            result = save_packetstream_credentials(username, auth_key, path=args.vault.parent / "packetstream.dpapi")
        elif args.command == "import":
            def progress(done, total):
                print(f"Encrypted {done}/{total} records locally.", file=sys.stderr, flush=True)
            result = migrate_registered(args.source, args.vault, progress=progress)
        else:
            with AccountVault(args.vault) as vault:
                if args.command == "status":
                    result = vault.summary()
                elif args.command == "test-accounts":
                    result = vault.test_accounts()
                elif args.command == "prepare-tests":
                    from .preparation import prepare_test_accounts
                    proxy = load_packetstream_proxy(args.vault.parent / "packetstream.dpapi") if args.proxy_egypt else None
                    result = prepare_test_accounts(
                        vault, count=args.count, start_row=args.start_row, proxy=proxy,
                        browser_backend=args.browser, headless=False if args.no_browser else args.headless, dry_run=args.dry_run,
                        **({"reduce_browser_data": True} if args.reduce_browser_data and not args.no_browser else {}),
                        **({"no_browser": True} if args.no_browser else {}),
                    )
                elif args.command == "find":
                    result = {"source_rows": vault.find(args.email)}
                elif args.command == "restore-source":
                    result = vault.restore_source(args.output)
                else:
                    rows = _select_test_rows(vault, args) if args.command in {"test-play-record", "test-like"} else [_select(vault, args)]
                    row = rows[0]
                    proxy = load_packetstream_proxy(args.vault.parent / "packetstream.dpapi") if getattr(args, "proxy_egypt", False) else None
                    if args.command == "login":
                        from .capture import capture_login
                        record = vault.record(row)
                        password = getpass.getpass("New Anghami password (hidden): ") if args.prompt_password else record["password"]
                        if not password:
                            raise SessionError("The password cannot be empty.")
                        login_options = {"email": record["email"], "password": password}
                        if args.headless:
                            login_options["headless"] = True
                        if args.browser != "cloakbrowser":
                            login_options["browser_backend"] = args.browser
                        if proxy is not None:
                            login_options["proxy"] = proxy
                        if args.reduce_browser_data:
                            login_options["reduce_browser_data"] = True
                        saved, metadata = capture_login(**login_options)
                        options = {} if proxy is None else {"proxy": proxy}
                        result = vault.attach(row, saved, new_password=password if args.prompt_password else None,
                                              review_session=True, **options)
                        result = {**result, "reduce_browser_data": args.reduce_browser_data}
                        metadata_path = args.vault.parent / f"account-{row}.login-request.redacted.json"
                        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
                    elif args.command == "attach":
                        result = vault.attach(row, load_session(args.session), review_session=True)
                    elif args.command == "check":
                        result = vault.check(row)
                    elif args.command == "song":
                        from .playback import song_summary
                        result = {"source_row": row, **song_summary(vault.song(row, args.song_id))}
                    elif args.command == "probe-playback":
                        result = vault.probe_playback(row, args.song_id, seconds=args.seconds)
                    elif args.command == "test-play-record":
                        result = _run_selected_tests(vault, args, rows, proxy)
                    elif args.command == "test-like":
                        result = _run_selected_tests(vault, args, rows, proxy)
                    else:
                        options = {} if proxy is None else {"proxy": proxy}
                        result = vault.request(row, args.command, **options)
        if args.command in {"import", "login", "attach", "check"}:
            report_path = args.vault.parent / "accounts-last-operation.json"
            report_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if args.command == "probe-playback":
            report_path = args.vault.parent / f"account-{result['source_row']}.http-playback-report.json"
            report_path.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except SessionError as exc:
        print(str(exc), file=sys.stderr)
    except FileExistsError:
        print("The destination already exists. It was preserved; choose a new output path.", file=sys.stderr)
    except FileNotFoundError:
        print("The selected source, session, or backup file does not exist.", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("Account operation cancelled. Previously saved records were kept.", file=sys.stderr)
    except Exception as exc:
        # Browser and transport tracebacks can contain passwords and session URLs.
        print(f"Account operation failed ({type(exc).__name__}).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
