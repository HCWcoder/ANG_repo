"""Offline public-only fixtures: no accounts, browsing or network requests."""

from copy import deepcopy
import csv
import importlib.util
import io
import json
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location("chart_snapshot", Path(__file__).resolve().parents[1] / "tools" / "chart_snapshot.py")
chart = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(chart)


def song(identifier="1001", rank=1, count=100, precision="exact", label=None):
    if label is None and precision == "exact":
        label = f"{count:,} PLAYS"
    return {"rank": rank, "song_id": identifier, "title": "Public test title", "artist": "Public test artist",
            "url": f"https://play.anghami.com/song/{identifier}", "public_play_count": count,
            "public_play_count_text": label,
            "count_scope": "not_available" if precision == "unavailable" else "site_public_total",
            "public_play_count_precision": precision}


def snapshot(observed="2026-10-05T12:00:00+03:00", songs=None, **fields):
    songs = [song()] if songs is None else songs
    return {"chart_id": "5890838", "observed_at": observed, "timezone": "Asia/Beirut",
            "complete": True, "declared_song_count": len(songs), "songs": songs, **fields}


def latest(directory):
    return json.loads((directory / "latest.json").read_text(encoding="utf-8"))


def test_initial_baseline_and_exact_zero(tmp_path):
    result = chart.record_snapshot(snapshot(songs=[song(count=0)]), tmp_path)
    assert result["exact_counts"] == 1
    saved = latest(tmp_path)
    assert saved["monitor_date"] == "2026-10-05"
    assert saved["songs"][0]["public_play_count"] == 0
    assert saved["songs"][0]["public_play_count_change"] is None
    assert saved["songs"][0]["comparison_status"] == "first_seen"
    assert (tmp_path / "snapshots" / "2026-10-05.json").exists()
    rows = list(csv.DictReader(io.StringIO((tmp_path / "latest.csv").read_text(encoding="utf-8"))))
    assert rows[0]["public_play_count"] == "0"
    assert rows[0]["public_play_count_change"] == ""


def test_daily_compare_rank_and_exact_counts_same_song(tmp_path):
    chart.record_snapshot(snapshot(songs=[song("1001", 1, 100), song("1002", 2, 20)]), tmp_path)
    chart.record_snapshot(snapshot("2026-10-06T12:00:00+03:00", [song("1002", 1, 30), song("1001", 2, 101)]), tmp_path)
    first, second = latest(tmp_path)["songs"]
    assert (first["song_id"], first["rank_change"], first["public_play_count_change"]) == ("1002", 1, 10)
    assert (second["rank_change"], second["public_play_count_change"]) == (-1, 1)
    assert first["comparison_status"] == "compared"
    assert first["previous_observed_at"] == "2026-10-05T12:00:00+03:00"
    assert len(list(csv.DictReader(io.StringIO((tmp_path / "history.csv").read_text(encoding="utf-8"))))) == 4


def test_rounded_labels_are_kept_but_never_inferred(tmp_path):
    chart.record_snapshot(snapshot(songs=[song(count=None, precision="rounded", label="3.8M PLAYS")]), tmp_path)
    chart.record_snapshot(snapshot("2026-10-06T12:00:00+03:00", [song(count=None, precision="rounded", label="3.9M PLAYS")]), tmp_path)
    current = latest(tmp_path)["songs"][0]
    assert current["public_play_count"] is None
    assert current["public_play_count_change"] is None
    assert current["previous_public_play_count_text"] == "3.8M PLAYS"
    assert current["public_play_count_text_changed"] is True
    assert current["comparison_status"] == "count_not_comparable"


@pytest.mark.parametrize("left,right", [(song(count=None, precision="unavailable"), song(count=0)), (song(count=0), song(count=None, precision="unavailable")), (song(count=None, precision="rounded", label="0K PLAYS"), song(count=0))])
def test_missing_or_rounded_cannot_be_compared_with_zero(tmp_path, left, right):
    chart.record_snapshot(snapshot(songs=[left]), tmp_path)
    chart.record_snapshot(snapshot("2026-10-06T12:00:00+03:00", [right]), tmp_path)
    assert latest(tmp_path)["songs"][0]["public_play_count_change"] is None


def test_withdrawn_song_has_no_delta_and_reintroduced_song_is_first_seen(tmp_path):
    chart.record_snapshot(snapshot(songs=[song("1001", count=100)]), tmp_path)
    chart.record_snapshot(snapshot("2026-10-06T12:00:00+03:00", [song("1002", count=30)]), tmp_path)
    current = latest(tmp_path)["songs"][0]
    assert current["song_id"] == "1002" and current["previous_rank"] is None
    chart.record_snapshot(snapshot("2026-10-07T12:00:00+03:00", [song("1001", count=150)]), tmp_path)
    current = latest(tmp_path)["songs"][0]
    assert current["public_play_count_change"] is None
    assert current["comparison_status"] == "first_seen"


def test_partial_observation_preserves_healthy_baseline(tmp_path):
    chart.record_snapshot(snapshot(), tmp_path)
    before = {name: (tmp_path / name).read_bytes() for name in ("latest.json", "latest.csv", "history.csv")}
    result = chart.record_snapshot(snapshot("2026-10-06T12:00:00+03:00", [], complete=False, declared_song_count=50), tmp_path)
    assert result["status"] == "partial_recorded" and result["healthy_baseline_preserved"] is True
    assert len(list((tmp_path / "partials").glob("*.json"))) == 1
    assert not (tmp_path / "snapshots" / "2026-10-06.json").exists()
    assert all((tmp_path / name).read_bytes() == contents for name, contents in before.items())


def test_partial_first_fetch_does_not_create_baseline(tmp_path):
    result = chart.record_snapshot(snapshot(songs=[], complete=False, declared_song_count=50), tmp_path)
    assert result["healthy_baseline_preserved"] is False
    assert not (tmp_path / "latest.json").exists()


def test_same_day_newest_only_and_older_snapshot_ignored(tmp_path):
    chart.record_snapshot(snapshot("2026-10-04T12:00:00+03:00", [song(count=90)]), tmp_path)
    chart.record_snapshot(snapshot("2026-10-05T12:00:00+03:00", [song(count=100)]), tmp_path)
    chart.record_snapshot(snapshot("2026-10-05T13:00:00+03:00", [song(count=110)]), tmp_path)
    result = chart.record_snapshot(snapshot("2026-10-05T11:00:00+03:00", [song(count=95)]), tmp_path)
    assert result["status"] == "older_observation_ignored"
    assert latest(tmp_path)["songs"][0]["public_play_count_change"] == 20
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 2


def test_days_use_beirut_dst_not_utc_date(tmp_path):
    chart.record_snapshot(snapshot("2026-10-05T22:30:00+00:00"), tmp_path)
    assert latest(tmp_path)["monitor_date"] == "2026-10-06"


@pytest.mark.parametrize("patch,error", [({"chart_id": "1"}, "wrong_chart"), ({"observed_at": "2026-10-05T12:00:00"}, "observation_timezone_required"), ({"complete": 1}, "invalid_completeness"), ({"declared_song_count": 2}, "incomplete_chart"), ({"timezone": "UTC"}, "wrong_monitor_timezone"), ({"cookie": "do-not-store"}, "invalid_snapshot_fields")])
def test_invalid_roots_rejected_without_storage(tmp_path, patch, error):
    with pytest.raises(chart.SnapshotError, match=error):
        chart.record_snapshot(snapshot(**patch), tmp_path)
    assert not (tmp_path / "latest.json").exists()


@pytest.mark.parametrize("field,value,error", [("song_id", "001", "invalid_identifier"), ("song_id", True, "invalid_identifier"), ("rank", 0, "invalid_rank"), ("public_play_count", -1, "invalid_exact_count"), ("public_play_count", True, "invalid_exact_count"), ("public_play_count_text", "3.8M PLAYS", "rounded_count_cannot_be_exact"), ("count_scope", "egypt_daily", "invalid_exact_count"), ("url", "https://example.invalid/1001", "invalid_public_song_url")])
def test_invalid_song_fields_rejected(tmp_path, field, value, error):
    selected = song()
    selected[field] = value
    with pytest.raises(chart.SnapshotError, match=error):
        chart.record_snapshot(snapshot(songs=[selected]), tmp_path)


@pytest.mark.parametrize("songs", [[song("1001", 1), song("1001", 2)], [song("1001", 1), song("1002", 1)]])
def test_duplicate_songs_or_ranks_rejected_without_double_count(tmp_path, songs):
    with pytest.raises(chart.SnapshotError, match="duplicate_chart_song"):
        chart.record_snapshot(snapshot(songs=songs), tmp_path)


def test_csv_public_text_is_not_formula_and_json_preserves_it(tmp_path):
    selected = song()
    selected["title"] = "=PUBLIC_FORMULA()"
    chart.record_snapshot(snapshot(songs=[selected]), tmp_path)
    assert latest(tmp_path)["songs"][0]["title"] == "=PUBLIC_FORMULA()"
    rows = list(csv.DictReader(io.StringIO((tmp_path / "latest.csv").read_text(encoding="utf-8"))))
    assert rows[0]["title"] == "'=PUBLIC_FORMULA()"


def test_cli_stdin_and_bad_input_emit_safe_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(chart.sys, "stdin", io.StringIO(json.dumps(snapshot())))
    assert chart.main(["-", "--directory", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "recorded"
    monkeypatch.setattr(chart.sys, "stdin", io.StringIO('{"secret":"never-echo-this"}'))
    assert chart.main(["-", "--directory", str(tmp_path)]) == 2
    assert "never-echo-this" not in capsys.readouterr().out


def test_numeric_decrease_is_recorded_without_claiming_new_plays(tmp_path):
    chart.record_snapshot(snapshot(songs=[song(count=100)]), tmp_path)
    chart.record_snapshot(snapshot("2026-10-06T12:00:00+03:00", [song(count=90)]), tmp_path)
    assert latest(tmp_path)["songs"][0]["public_play_count_change"] == -10
