"""Manual browser-free playback checks for at most five saved accounts."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anghami_session.errors import SessionError
from anghami_session.playback import validate_seconds
from anghami_session.vault import AccountVault, DEFAULT_VAULT_PATH


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", nargs="+", type=int, required=True)
    parser.add_argument("--song-id", default="1263607749")
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT_PATH)
    args = parser.parse_args(argv)
    rows = list(dict.fromkeys(args.rows))
    if not 1 <= len(rows) <= 5 or any(row < 1 for row in rows):
        parser.error("Choose one to five positive source rows")
    try:
        seconds = validate_seconds(args.seconds)
        with AccountVault(args.vault) as vault:
            keys = []
            for row in rows:
                vault.session(row)  # All selected sessions must already exist.
                keys.append(vault._db.execute("SELECT email_key FROM accounts WHERE source_row=?", (row,)).fetchone()[0])
            if len(set(keys)) != len(keys):
                raise SessionError("Choose distinct accounts, rather than duplicate source rows.")
    except SessionError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    started = datetime.now(timezone.utc)
    report = {
        "started_at_utc": started.isoformat(), "selected_rows": rows,
        "song_id": args.song_id, "browser_required": False,
        "scope": "saved session authentication, media delivery, silent paced audio decoding",
        "listening_statistics_submitted": False, "accounts": [],
    }
    directory = args.vault.parent
    destinations = [
        directory / "http-five-account-playback-report.json",
        directory / f"http-playback-{started.strftime('%Y%m%dT%H%M%S%fZ')}.json",
    ]

    def checkpoint():
        serialized = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
        for destination in destinations:
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(destination)

    checkpoint()
    with AccountVault(args.vault) as vault:
        for row in rows:
            print(json.dumps({"source_row": row, "stage": "checking_saved_http_playback"}), flush=True)
            try:
                result = vault.probe_playback(row, args.song_id, seconds=seconds)
                (directory / f"account-{row}.http-playback-report.json").write_text(
                    json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8",
                )
            except SessionError as exc:
                result = {"source_row": row, "passed": False, "error": str(exc)}
            except Exception as exc:
                result = {"source_row": row, "passed": False, "error_type": type(exc).__name__}
            report["accounts"].append(result)
            checkpoint()
            print(json.dumps({"source_row": row, "passed": result.get("passed", False)}), flush=True)
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = {
        "accounts_checked": len(report["accounts"]),
        "passed": sum(bool(item.get("passed")) for item in report["accounts"]),
        "failed": sum(not item.get("passed") for item in report["accounts"]),
    }
    checkpoint()
    print(json.dumps(report["summary"]), flush=True)
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
