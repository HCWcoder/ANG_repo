"""Migrate the account list offline and manage a selected account's normal login."""

import argparse
import ctypes
from concurrent.futures import ThreadPoolExecutor, as_completed
import getpass
import json
from pathlib import Path
import sys
import threading

from .errors import SessionError


def _title(text: str) -> None:
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass
from .proxy import load_packetstream_proxy, save_packetstream_credentials
from .store import load_session
from .vault import AccountVault, DEFAULT_VAULT_PATH, ROOT, migrate_registered


def _load_proxy_file(path) -> list:
    import re
    from secrets import token_hex
    from urllib.parse import urlparse
    from .proxy import PACKETSTREAM_ENDPOINT, PacketStreamProxy
    proxies = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "@" in line:
            parsed = urlparse(line if "://" in line else "http://" + line)
            username = parsed.username or ""
            password = parsed.password or ""
            endpoint = PACKETSTREAM_ENDPOINT
        else:
            parts = line.split(":")
            if len(parts) != 4:
                continue
            if parts[1].isdigit() and not parts[3].isdigit():
                host, port, username, password = parts  # host:port:user:pass
            else:
                username, password, host, port = parts  # user:pass:host:port
            endpoint = f"https://{host}:{port}" if port == "31111" else f"http://{host}:{port}"
        modifiers = re.search(r"_country-([A-Za-z]{2})(?:_session-([A-Za-z0-9]{1,64}))?$", password)
        country, session_label = "EG", None
        if modifiers:
            country = modifiers.group(1).upper()
            session_label = modifiers.group(2)
            password = password[:modifiers.start()]
        if not username or not password:
            continue
        try:
            proxies.append(PacketStreamProxy.from_route(
                username, password, session_label or token_hex(8), endpoint, country=country,
            ))
        except SessionError:
            raise SessionError(f"Invalid PacketStream route on line {lineno} of {path}.") from None
    if not proxies:
        raise SessionError(f"No valid proxy URLs found in {path}.")
    return proxies


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


def _declared_song_id(args):
    from .test_settings import read_test_song_id
    return read_test_song_id(args.vault.parent / "test-settings.json")


def _select_test_rows(vault, args):
    declared = _declared_song_id(args)
    if args.song_id != declared:
        raise SessionError(f"This test command is limited to the declared test song {declared}.")
    country = getattr(args, "country", None)
    if country is not None:
        enrolled = vault.enrolled_test_rows()
        tags = vault.preparation_country_tags()
        rows = sorted(row for row in enrolled if tags.get(row) == country)
        if not rows:
            raise SessionError(f"No enrolled test accounts are tagged as {country}.")
        skip = getattr(args, "skip", 0) or 0
        if skip:
            rows = rows[skip:]
        if not rows:
            raise SessionError(f"No {country} accounts remain after skipping {skip}.")
        return rows
    rows = getattr(args, "rows", None)
    if rows is None:
        return [_select(vault, args)]
    if not 1 <= len(rows) <= 5 or len(set(rows)) != len(rows) or any(row < 1 for row in rows):
        raise SessionError("Choose 1-5 distinct positive source rows for this test run.")
    for row in rows:
        vault.record(row)
    if not set(rows).issubset(vault.enrolled_test_rows()):
        raise SessionError("One or more rows are not prepared for testing. Use accounts prepare-tests and accounts test-accounts.")
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
    if args.command == "test-play-record" and getattr(args, "with_heartbeats", False):
        options["with_heartbeats"] = True
    if args.command == "test-play-record":
        declared = _declared_song_id(args)
        from .test_settings import DEFAULT_TEST_SONG_ID
        if declared != DEFAULT_TEST_SONG_ID:
            options["declared_song_id"] = declared
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
            _title(f"{number}/{args.count}")
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


def _run_selected_tests(vault, args, rows, proxy, proxies=None):
    if len(rows) == 1:
        return _run_repeated_test(vault, args, rows[0], proxy)
    from .play_record import _journal
    report_path = args.vault.parent / f"{args.command}.accounts-batch-report.json"
    total = len(rows)
    workers = min(getattr(args, "workers", 8), total)
    report = {
        "operation": args.command, "song_id": args.song_id, "selected_rows": rows,
        "requested_accounts": total, "tests_per_account": args.count,
        "workers": workers,
        "attempted_accounts": 0, "completed_accounts": 0, "results": [],
        "passed": False, "phase": "running", "automatic_retry": False,
    }
    if proxy is not None:
        report["proxy"] = proxy.summary()
    _journal(report, report_path)

    # Pre-load all sessions sequentially in the main thread.
    # Windows DPAPI serializes concurrent decrypt calls from the same process,
    # so doing this upfront is faster than having 8+ threads all decrypt at once.
    from .client import AnghamiSession
    from .play_record import run_play_record_test, TEST_SONG_ID as _TEST_SONG_ID, _journal
    from .test_settings import DEFAULT_TEST_SONG_ID
    from tqdm import tqdm
    import colorama
    colorama.init()
    preloaded = {}
    skipped_held = []
    with tqdm(total=len(rows), unit="acc", desc="\033[36mLoading sessions\033[0m", dynamic_ncols=True, colour="cyan") as load_bar:
        for row in rows:
            try:
                preloaded[row] = vault.session(row)
            except Exception:
                skipped_held.append(row)
            load_bar.update(1)
    rows = [row for row in rows if row in preloaded]
    if skipped_held:
        print(f"Skipped {len(skipped_held)} held/invalid accounts: {skipped_held[:10]}{'...' if len(skipped_held) > 10 else ''}")
    if not rows:
        raise SessionError("No valid sessions available after filtering held accounts.")
    total = len(rows)
    workers = min(getattr(args, "workers", 8), total)
    report.update({"selected_rows": rows, "requested_accounts": total, "workers": workers, "skipped_held": skipped_held})
    report_path_for = lambda row: args.vault.parent / f"account-{row}.{args.command}-report.json"
    declared = _declared_song_id(args)

    import time as _time
    _start_time = _time.monotonic()

    lock = threading.Lock()
    counters = {"ok": 0, "fail": 0, "done": 0}
    results_by_row = {}

    bar = tqdm(total=total, unit="acc", desc="\033[32mRunning tests\033[0m", dynamic_ncols=True, colour="green")
    bar.set_postfix(ok=0, fail=0)

    def _elapsed_str():
        secs = int(_time.monotonic() - _start_time)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"

    def _refresh_title():
        _title(f"[{_elapsed_str()}] test-play-record")

    row_index = {row: i for i, row in enumerate(rows)}

    def _pick_proxy(row):
        if proxies:
            return proxies[row_index[row] % len(proxies)]
        return proxy

    def _run_one(row):
        try:
            saved = preloaded[row]
            rpath = report_path_for(row)
            options = {}
            if declared != DEFAULT_TEST_SONG_ID:
                options["declared_song_id"] = declared
            if getattr(args, "with_heartbeats", False):
                options["with_heartbeats"] = True
            _proxy = _pick_proxy(row)
            with AnghamiSession(saved=saved, **({} if _proxy is None else {"proxy": _proxy})) as session:
                import json as _json
                report = run_play_record_test(session, args.song_id, report_path=rpath, **options)
            result = {"source_row": row, **report}
            rpath.write_text(_json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
            passed = result.get("passed", False)
        except Exception as exc:
            result = {"source_row": row, "passed": False, "phase": "stopped", "error": str(exc)}
            passed = False
        with lock:
            counters["done"] += 1
            if passed:
                counters["ok"] += 1
            else:
                counters["fail"] += 1
            results_by_row[row] = result
            bar.set_postfix(
                ok=f"\033[32m{counters['ok']}\033[0m",
                fail=f"\033[31m{counters['fail']}\033[0m",
            )
            bar.update(1)
        _refresh_title()
        return row, result

    _refresh_title()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_one, row): row for row in rows}
        for future in as_completed(futures):
            row, result = future.result()
            with lock:
                report["completed_accounts"] = counters["done"]
                report["attempted_accounts"] = counters["done"]
            _journal(report, report_path)
    bar.close()

    report["results"] = [results_by_row[row] for row in rows]
    all_passed = all(
        (r.get("passed", False) if isinstance(r, dict) else bool(r))
        for r in report["results"]
    )
    with lock:
        o, f = counters["ok"], counters["fail"]
    _title(f"[{_elapsed_str()}] done")
    report.update({"passed": all_passed, "phase": "complete",
                   "completed_accounts": total, "attempted_accounts": total})
    _journal(report, report_path)
    return report


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
                selector.add_argument("--country", choices=("LB", "EG"), help="Run all enrolled accounts tagged with this country (no row limit)")
            if name in {"song", "probe-playback", "test-play-record", "test-like"}:
                command.add_argument("--song-id", required=True)
            if name in {"login", "relations", "playlists", "test-play-record", "test-like"}:
                command.add_argument("--proxy-egypt", action="store_true", help="Use the encrypted PacketStream credentials and verify an Egypt exit before Anghami requests")
            if name in {"test-play-record", "test-like"}:
                command.add_argument("--count", type=_test_count, default=1, help="Sequential tests of this selected row and song (1-5; default 1)")
            if name == "test-play-record":
                command.add_argument("--with-heartbeats", action="store_true", help="Send /playqueue/create and progress beats before the play event (required for chart counting)")
                command.add_argument("--workers", type=int, default=8, help="Parallel workers for multi-account runs (default 8)")
                command.add_argument("--skip", type=int, default=0, help="Skip the first N accounts from the selected country (default 0)")
                command.add_argument("--proxy-file", type=Path, default=None, help="Path to a txt file with one PacketStream proxy per line: user:auth_key@proxy.packetstream.io:31111 or exported user:auth_key[_country-XX_session-YY]:proxy.packetstream.io:31112")
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
                    proxies = _load_proxy_file(args.proxy_file) if getattr(args, "proxy_file", None) else None
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
                        result = _run_selected_tests(vault, args, rows, proxy, proxies=proxies)
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
        import traceback
        traceback.print_exc()
        print(f"Account operation failed ({type(exc).__name__}).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
