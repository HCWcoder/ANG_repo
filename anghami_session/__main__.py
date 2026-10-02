"""Manage and reuse a local Anghami session. The default action only checks login."""

import argparse
import json
import sys
from pathlib import Path

from .client import AnghamiSession, validate_session
from .errors import SessionError
from .store import DEFAULT_SESSION_PATH, load_session, save_session


def save_verified_session(saved: dict, path: Path) -> dict:
    saved = validate_session(saved)
    with AnghamiSession(saved=saved) as session:
        report = session.check(negative_control=True)
    # Do not replace a working session until the new one passes direct HTTP checks.
    save_session(saved, path)
    return report


def _write_report(path: Path, result: dict | list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "accounts":
        from .accounts import main as accounts_main
        return accounts_main(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", nargs="?", default="check",
        choices=["check", "relations", "playlists", "song", "probe-playback", "login", "import"],
    )
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION_PATH, help="Path to an encrypted .dpapi session")
    parser.add_argument("--source", type=Path, help="Existing DPAPI session to import and validate")
    parser.add_argument("--negative-control", action="store_true", help="Also confirm a request without credentials is rejected")
    parser.add_argument("--song-id", help="Song to read or probe using the saved session")
    parser.add_argument("--seconds", type=float, default=5, help="Silent playback probe duration (1-30 seconds)")
    parser.epilog = "For the encrypted account list: python main.py accounts --help"
    args = parser.parse_args(arguments)
    if args.operation == "import" and args.source is None:
        parser.error("import requires --source pointing to an encrypted session")
    if args.source is not None and args.operation != "import":
        parser.error("--source is only valid with import")
    if args.negative_control and args.operation != "check":
        parser.error("--negative-control is only valid with check (login and import always perform it)")
    if args.operation in {"song", "probe-playback"} and not args.song_id:
        parser.error("song and probe-playback require --song-id")
    if args.song_id and args.operation not in {"song", "probe-playback"}:
        parser.error("--song-id is only valid with song or probe-playback")
    try:
        if args.operation == "login":
            from .capture import capture_login
            saved, login_metadata = capture_login()
            result = save_verified_session(saved, args.session)
            _write_report(args.session.parent / "login-request.redacted.json", login_metadata)
        elif args.operation == "import":
            result = save_verified_session(load_session(args.source), args.session)
        else:
            with AnghamiSession(args.session) as session:
                if args.operation == "check":
                    result = session.check(negative_control=args.negative_control)
                elif args.operation == "song":
                    from .playback import song_summary
                    result = song_summary(session.song(args.song_id))
                elif args.operation == "probe-playback":
                    from .playback import probe_playback
                    result = probe_playback(session, args.song_id, seconds=args.seconds)
                else:
                    result = session.request(args.operation)
        if args.operation in {"check", "login", "import"}:
            _write_report(args.session.parent / "last-check.json", result)
        elif args.operation == "probe-playback":
            _write_report(args.session.parent / "http-playback-report.json", result)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except FileNotFoundError:
        print("No session at the selected path. Use login, or import an existing encrypted session.", file=sys.stderr)
    except SessionError as exc:
        print(str(exc), file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print("Session operation cancelled. The saved session was kept.", file=sys.stderr)
    except Exception as exc:
        # Do not print raw HTTP/browser exceptions: these can contain credentials.
        print(f"Session operation failed ({type(exc).__name__}).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
