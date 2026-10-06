"""Large preparation counts use synthetic vaults and no live account transport."""

import json
import sqlite3

import pytest

from anghami_session import accounts, client, preparation, vault as vault_module
from anghami_session.errors import SessionError


@pytest.fixture
def large_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(client.requests, "Session", lambda *_a, **_k: pytest.fail("Large preview attempted HTTP"))
    monkeypatch.setattr(vault_module, "_crypt", lambda value, decrypt=False: value)
    key = b"synthetic-large-vault-index-key"
    path = tmp_path / "large-synthetic.sqlite3"
    with sqlite3.connect(path) as database:
        database.executescript("""
            CREATE TABLE metadata (name TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE accounts (
                source_row INTEGER PRIMARY KEY, email_key TEXT NOT NULL,
                record BLOB NOT NULL, session BLOB,
                state TEXT NOT NULL DEFAULT 'login_required', checked_at_utc TEXT
            );
        """)
        database.executemany("INSERT INTO metadata VALUES (?, ?)", {
            "format_version": "1", "index_key": key,
        }.items())
        for country, first_row in (("EG", 10_000), ("LB", 20_000)):
            for index in range(1100):
                row = first_row + index
                email = f"synthetic-{country}-{index}@example.invalid"
                record = {"source_row": row, "country": country, "email": email, "password": "synthetic-only-secret"}
                database.execute(
                    "INSERT INTO accounts(source_row, email_key, record) VALUES (?, ?, ?)",
                    (row, vault_module._email_key(key, email), vault_module._pack(record)),
                )
    with vault_module.AccountVault(path) as selected:
        yield selected


@pytest.mark.parametrize("count", [6, 1000])
@pytest.mark.parametrize("country", [None, "EG", "LB"])
def test_large_counts_preview_distinct_available_country_rows_offline(large_vault, monkeypatch, count, country):
    original = large_vault.path.read_bytes()
    monkeypatch.setattr(preparation, "_journal", lambda *_a: pytest.fail("Preview wrote a journal"))
    report = preparation.prepare_test_accounts(
        large_vault, count=count, account_country=country, dry_run=True, no_browser=True,
    )
    rows = report["selected_rows"]
    assert len(rows) == len(set(rows)) == count
    assert len({large_vault.record(row)["email"] for row in rows}) == count
    if country is not None:
        assert all(large_vault.record(row)["country"] == country for row in rows)
        assert report["account_country"] == country and report["selection_mode"] == "random_country"
    else:
        assert rows == list(range(10_000, 10_000 + count))
    assert report["requested_accounts"] == count and report["attempted_accounts"] == 0
    assert report["play_events_sent"] == report["like_events_sent"] == 0
    assert large_vault.path.read_bytes() == original
    assert "example.invalid" not in json.dumps(report) and "synthetic-only-secret" not in json.dumps(report)


@pytest.mark.parametrize("country", [None, "EG", "LB"])
def test_request_above_available_unique_pool_fails_before_journal_and_transport(large_vault, monkeypatch, country):
    monkeypatch.setattr(preparation, "_journal", lambda *_a: pytest.fail("Unavailable pool wrote a journal"))
    available = 2200 if country is None else 1100
    with pytest.raises(SessionError, match=f"Only {available} additional unique"):
        preparation.prepare_test_accounts(large_vault, count=available + 1, account_country=country, no_browser=True)
    assert not (large_vault.path.parent / "accounts-prepare-tests-report.json").exists()


def test_positive_count_has_no_artificial_upper_bound_and_shortage_is_clear(large_vault, monkeypatch):
    monkeypatch.setattr(preparation, "_journal", lambda *_a: pytest.fail("Unavailable large count wrote a journal"))
    with pytest.raises(SessionError, match="Only 1100 additional unique EG accounts"):
        preparation.prepare_test_accounts(large_vault, count=10**20, account_country="EG", no_browser=True)


@pytest.mark.parametrize("count", [6, 1000])
def test_synthetic_preparation_completes_every_selected_account(tmp_path, monkeypatch, count):
    monkeypatch.setattr(client.requests, "Session", lambda *_a, **_k: pytest.fail("Synthetic preparation attempted HTTP"))
    rows = list(range(10_000, 10_000 + count))
    calls = {"session": [], "attach": [], "enable": [], "journal": 0}

    class SyntheticVault:
        path = tmp_path / "synthetic.sqlite3"

        def select_test_candidates(self, requested, *, country, randomize):
            assert requested == count and country == "EG" and randomize is True
            return list(rows)

        def session(self, row):
            calls["session"].append(row)
            return {"synthetic_source_row": row}

        def attach(self, row, saved):
            assert saved == {"synthetic_source_row": row}
            calls["attach"].append(row)

        def enable_test_account(self, row):
            calls["enable"].append(row)

    def journal(report, path):
        assert path == SyntheticVault.path.parent / "accounts-prepare-tests-report.json"
        assert report["selected_rows"] == rows
        calls["journal"] += 1

    monkeypatch.setattr(preparation, "_journal", journal)
    report = preparation.prepare_test_accounts(SyntheticVault(), count=count, account_country="EG", no_browser=True)
    assert report["passed"] is True and report["phase"] == "complete"
    assert report["prepared_rows"] == rows
    assert report["prepared_account_count"] == report["attempted_accounts"] == count
    assert calls["session"] == calls["attach"] == calls["enable"] == rows
    assert calls["journal"] == 3 * count + 2
    assert report["play_events_sent"] == report["like_events_sent"] == 0
    assert list(tmp_path.iterdir()) == []


def test_explicit_failed_review_still_has_five_account_limit():
    with pytest.raises(SessionError, match="1-5 distinct"):
        preparation.prepare_test_accounts(object(), count=6, selected_rows=list(range(10_000, 10_006)))


@pytest.mark.parametrize("count", ["6", "1000", str(10**20)])
@pytest.mark.parametrize("preview", [False, True])
def test_preparation_cli_accepts_large_positive_count(count, preview):
    command = ["prepare-tests", "--count", count]
    if preview:
        command.append("--dry-run")
    parsed = accounts.build_parser().parse_args(command)
    assert parsed.count == int(count) and parsed.dry_run is preview


@pytest.mark.parametrize("count", ["0", "-1", "1.5", "not-a-number"])
def test_preparation_cli_rejects_nonpositive_or_noninteger_count(count):
    with pytest.raises(SystemExit) as failure:
        accounts.build_parser().parse_args(["prepare-tests", "--count", count])
    assert failure.value.code == 2


@pytest.mark.parametrize("command", ["test-like", "test-play-record"])
def test_large_preparation_count_does_not_remove_action_repeat_limit(command):
    with pytest.raises(SystemExit) as failure:
        accounts.build_parser().parse_args([command, "--row", "7", "--song-id", "12345", "--count", "6"])
    assert failure.value.code == 2
