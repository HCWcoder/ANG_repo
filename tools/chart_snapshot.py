"""Persist public chart observations supplied by a human or browser collector.

This utility has no network client and reads no account, proxy or session files.
Counts are retained only when the collector explicitly labels them exact. A
rounded public label remains text; it never becomes a fabricated exact count.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CHART_ID = "5890838"
MONITOR_TIMEZONE = "Asia/Beirut"
DEFAULT_DIRECTORY = Path(__file__).resolve().parents[1] / ".anghami" / "chart-monitor"
MAX_INTEGER = 2**53 - 1
_ROOT_KEYS = {"chart_id", "observed_at", "timezone", "complete", "declared_song_count", "songs"}
_SONG_KEYS = {"rank", "song_id", "title", "artist", "url", "public_play_count",
              "public_play_count_text", "count_scope", "public_play_count_precision"}
CSV_FIELDS = (
    "monitor_date", "observed_at", "rank", "song_id", "title", "artist", "url",
    "public_play_count", "public_play_count_text", "count_scope", "public_play_count_precision",
    "previous_observed_at", "previous_rank", "rank_change", "previous_public_play_count",
    "previous_public_play_count_text", "public_play_count_text_changed",
    "public_play_count_change", "comparison_status",
)


class SnapshotError(ValueError):
    """A fixed error code avoids echoing untrusted input or file contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _integer(value, *, minimum=0):
    return type(value) is int and minimum <= value <= MAX_INTEGER


def _identifier(value):
    if type(value) is int and _integer(value, minimum=1):
        value = str(value)
    if type(value) is not str or not re.fullmatch(r"[1-9][0-9]{0,15}", value):
        raise SnapshotError("invalid_identifier")
    if int(value) > MAX_INTEGER:
        raise SnapshotError("invalid_identifier")
    return value


def _text(value, *, maximum=500, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise SnapshotError("invalid_public_text")
    if any(ord(character) < 32 for character in value):
        raise SnapshotError("invalid_public_text")
    return value.strip()


def _timestamp(value):
    if type(value) is not str or len(value) > 64:
        raise SnapshotError("invalid_observation_time")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SnapshotError("invalid_observation_time") from None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise SnapshotError("observation_timezone_required")
    return timestamp


def _monitor_timezone():
    try:
        return ZoneInfo(MONITOR_TIMEZONE)
    except ZoneInfoNotFoundError:
        raise SnapshotError("timezone_data_unavailable") from None


def validate_snapshot(value):
    """Rebuild the allowlisted, public-only input schema without inference."""
    if type(value) is not dict or set(value) - _ROOT_KEYS:
        raise SnapshotError("invalid_snapshot_fields")
    if _identifier(value.get("chart_id")) != CHART_ID:
        raise SnapshotError("wrong_chart")
    if value.get("timezone", MONITOR_TIMEZONE) != MONITOR_TIMEZONE:
        raise SnapshotError("wrong_monitor_timezone")
    observed = _timestamp(value.get("observed_at"))
    complete = value.get("complete", True)
    if type(complete) is not bool:
        raise SnapshotError("invalid_completeness")
    songs = value.get("songs")
    if type(songs) is not list or len(songs) > 1000:
        raise SnapshotError("invalid_songs")
    declared = value.get("declared_song_count", len(songs))
    if not _integer(declared, minimum=1) or len(songs) > declared:
        raise SnapshotError("invalid_declared_song_count")
    if complete and len(songs) != declared:
        raise SnapshotError("incomplete_chart")
    normalized, identifiers, ranks = [], set(), set()
    for song in songs:
        if type(song) is not dict or set(song) != _SONG_KEYS:
            raise SnapshotError("invalid_song_fields")
        rank, identifier = song["rank"], _identifier(song["song_id"])
        if not _integer(rank, minimum=1) or rank > declared:
            raise SnapshotError("invalid_rank")
        if rank in ranks or identifier in identifiers:
            raise SnapshotError("duplicate_chart_song")
        ranks.add(rank)
        identifiers.add(identifier)
        title, artist = _text(song["title"]), _text(song["artist"])
        url = song["url"]
        if url != f"https://play.anghami.com/song/{identifier}":
            raise SnapshotError("invalid_public_song_url")
        count, label = song["public_play_count"], _text(song["public_play_count_text"], nullable=True)
        scope, precision = song["count_scope"], song["public_play_count_precision"]
        if precision == "exact":
            if scope != "site_public_total" or not _integer(count) or label is None:
                raise SnapshotError("invalid_exact_count")
            # Reject an explicitly rounded English notation even if a caller
            # incorrectly claims it exact. No number is parsed from the label.
            if re.search(r"(?i)(?:\d\s*[kmb](?:\b|$)|\b(?:thousand|million|billion|about|approximately)\b|[~≈+])", label):
                raise SnapshotError("rounded_count_cannot_be_exact")
        elif precision == "rounded":
            if scope != "site_public_total" or count is not None or label is None:
                raise SnapshotError("invalid_rounded_count")
        elif precision == "unavailable":
            if scope != "not_available" or count is not None or label is not None:
                raise SnapshotError("invalid_unavailable_count")
        else:
            raise SnapshotError("invalid_count_precision")
        normalized.append({"rank": rank, "song_id": identifier, "title": title,
                           "artist": artist, "url": url, "public_play_count": count,
                           "public_play_count_text": label, "count_scope": scope,
                           "public_play_count_precision": precision})
    if complete and ranks != set(range(1, declared + 1)):
        raise SnapshotError("incomplete_ranking")
    return {"chart_id": CHART_ID, "observed_at": observed.isoformat(),
            "timezone": MONITOR_TIMEZONE, "complete": complete,
            "declared_song_count": declared,
            "songs": sorted(normalized, key=lambda item: item["rank"])}


def _comparison(snapshot, previous):
    lookup = {song["song_id"]: song for song in previous["songs"]} if previous else {}
    songs = []
    for current in snapshot["songs"]:
        old = lookup.get(current["song_id"])
        comparable = bool(old and old["public_play_count_precision"] == "exact"
                          and current["public_play_count_precision"] == "exact"
                          and old["count_scope"] == current["count_scope"] == "site_public_total")
        songs.append({**current,
                      "previous_observed_at": previous["observed_at"] if old else None,
                      "previous_rank": old["rank"] if old else None,
                      "rank_change": old["rank"] - current["rank"] if old else None,
                      "previous_public_play_count": old["public_play_count"] if old and old["public_play_count_precision"] == "exact" else None,
                      "previous_public_play_count_text": old["public_play_count_text"] if old else None,
                      "public_play_count_text_changed": old["public_play_count_text"] != current["public_play_count_text"] if old else None,
                      "public_play_count_change": current["public_play_count"] - old["public_play_count"] if comparable else None,
                      "comparison_status": "first_seen" if old is None else "compared" if comparable else "count_not_comparable"})
    observed = _timestamp(snapshot["observed_at"])
    return {"schema_version": 1, **snapshot,
            "monitor_date": observed.astimezone(_monitor_timezone()).date().isoformat(),
            "previous_observed_at": previous["observed_at"] if previous else None,
            "songs": songs}


def _read_snapshot(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        # Persisted comparisons are derived facts; rebuild from input fields.
        value = {key: value[key] for key in _ROOT_KEYS if key in value}
        value["songs"] = [{key: item[key] for key in _SONG_KEYS if key in item} for item in value["songs"]]
        return validate_snapshot(value)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise SnapshotError("invalid_saved_snapshot") from None


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _csv(snapshots):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for snapshot in snapshots:
        for song in snapshot["songs"]:
            row = {key: snapshot[key] if key in {"monitor_date", "observed_at"} else song.get(key) for key in CSV_FIELDS}
            # Public song titles are data, never spreadsheet formula commands.
            row = {key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value for key, value in row.items()}
            writer.writerow(row)
    return stream.getvalue()


def record_snapshot(value, directory=DEFAULT_DIRECTORY):
    """Store daily complete observations; incomplete fetches cannot erase them."""
    snapshot = validate_snapshot(value)
    directory = Path(directory)
    observed = _timestamp(snapshot["observed_at"])
    day = observed.astimezone(_monitor_timezone()).date().isoformat()
    if not snapshot["complete"]:
        stamp = observed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        partial = {"schema_version": 1, **snapshot, "monitor_date": day,
                   "status": "partial_observation", "comparison_available": False}
        _atomic_write(directory / "partials" / f"{stamp}.json", _json(partial))
        _atomic_write(directory / "latest_attempt.json", _json(partial))
        return {"status": "partial_recorded", "monitor_date": day,
                "observed_songs": len(snapshot["songs"]), "declared_song_count": snapshot["declared_song_count"],
                "healthy_baseline_preserved": (directory / "latest.json").exists()}
    history = {}
    for path in sorted((directory / "snapshots").glob("*.json")):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", path.name):
            raise SnapshotError("invalid_saved_snapshot_filename")
        saved = _read_snapshot(path)
        saved_day = _timestamp(saved["observed_at"]).astimezone(_monitor_timezone()).date().isoformat()
        if not saved["complete"] or path.stem != saved_day:
            raise SnapshotError("invalid_saved_snapshot")
        history[saved_day] = saved
    if day in history and observed < _timestamp(history[day]["observed_at"]):
        return {"status": "older_observation_ignored", "monitor_date": day,
                "observed_songs": len(snapshot["songs"])}
    history[day] = snapshot
    comparisons, previous = [], None
    for history_day in sorted(history):
        compared = _comparison(history[history_day], previous)
        comparisons.append(compared)
        _atomic_write(directory / "snapshots" / f"{history_day}.json", _json(compared))
        previous = history[history_day]
    latest = comparisons[-1]
    _atomic_write(directory / "latest.json", _json(latest))
    _atomic_write(directory / "latest.csv", _csv([latest]))
    _atomic_write(directory / "history.csv", _csv(comparisons))
    _atomic_write(directory / "latest_attempt.json", _json({**_comparison(snapshot, next((history[item] for item in sorted(history, reverse=True) if item < day), None)), "status": "complete_observation"}))
    return {"status": "recorded", "monitor_date": day, "observed_songs": len(snapshot["songs"]),
            "complete_days": len(comparisons), "latest_monitor_date": latest["monitor_date"],
            "exact_counts": sum(song["public_play_count_precision"] == "exact" for song in snapshot["songs"]),
            "rounded_counts": sum(song["public_play_count_precision"] == "rounded" for song in snapshot["songs"]),
            "unavailable_counts": sum(song["public_play_count_precision"] == "unavailable" for song in snapshot["songs"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", help="Public-only JSON snapshot file, or - for stdin")
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    arguments = parser.parse_args(argv)
    try:
        contents = sys.stdin.read() if arguments.snapshot == "-" else Path(arguments.snapshot).read_text(encoding="utf-8-sig")
        result = record_snapshot(json.loads(contents), arguments.directory)
    except SnapshotError as exc:
        print(json.dumps({"status": "rejected", "error_code": exc.code}))
        return 2
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(json.dumps({"status": "rejected", "error_code": "snapshot_io_failed"}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
