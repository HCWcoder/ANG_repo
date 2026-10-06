"""Registered-country selection uses disposable accounts and never the network."""

import json
import sqlite3

import pytest

from anghami_session import client, preparation, vault as vault_module
from anghami_session.errors import SessionError


@pytest.fixture
def country_vault(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Country selection must never create an HTTP transport")

    monkeypatch.setattr(client.requests, "Session", forbidden)
    monkeypatch.setattr(vault_module, "_crypt", lambda value, decrypt=False: value)
    path = tmp_path / "synthetic.sqlite3"
    key = b"synthetic-index-key"
    # Aliases deliberately cross country tags and differ in email case.
    records = [
        (1, "EG", "enrolled@example.invalid"),
        (101, "EG", "eg-one@example.invalid"),
        (102, "LB", "lb-one@example.invalid"),
        (103, "EG", "eg-two@example.invalid"),
        (104, "EG", "EG-ONE@example.invalid"),
        (105, "LB", "lb-two@example.invalid"),
        (106, "EG", "eg-three@example.invalid"),
        (107, "eg", "lowercase-tag@example.invalid"),
        (108, "EG ", "spaced-tag@example.invalid"),
        (109, "US", "other-country@example.invalid"),
        (110, "LB", "lb-three@example.invalid"),
        (111, "EG", "eg-four@example.invalid"),
        (112, "EG", "ENROLLED@example.invalid"),
        (113, "LB", "EG-TWO@example.invalid"),
        (114, "EG", "LB-TWO@example.invalid"),
    ]
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
        for row, country, email in records:
            record = {
                "source_row": row, "country": country, "email": email,
                "password": "synthetic-secret", "legacy_metadata": {}, "legacy_cookies": {},
            }
            database.execute(
                "INSERT INTO accounts(source_row, email_key, record) VALUES (?, ?, ?)",
                (row, vault_module._email_key(key, email), vault_module._pack(record)),
            )
    with vault_module.AccountVault(path) as selected:
        yield selected


@pytest.mark.parametrize("country,pool", [
    ("EG", [101, 103, 106, 111, 114]),
    ("LB", [102, 105, 110, 113]),
])
def test_randomizes_distinct_identities_only_inside_exact_country(country_vault, monkeypatch, country, pool):
    calls = []

    def choose(population, count):
        calls.append((list(population), count))
        return list(reversed(population))[:count]

    monkeypatch.setattr(vault_module.random, "sample", choose)
    selected = country_vault.select_test_candidates(3, country=country, randomize=True)
    assert calls == [(pool, 3)]
    assert selected == list(reversed(pool))[:3]
    assert len({country_vault.record(row)["email"].casefold() for row in selected}) == 3
    assert all(country_vault.record(row)["country"] == country for row in selected)
    assert 104 not in selected and 112 not in selected


def test_excludes_all_aliases_of_managed_enrolled_identities(country_vault, monkeypatch):
    with country_vault._db:
        country_vault._db.execute("CREATE TABLE test_accounts (source_row INTEGER PRIMARY KEY)")
        country_vault._db.execute("INSERT INTO test_accounts VALUES (101)")
    monkeypatch.setattr(vault_module.random, "sample", lambda population, count: population[:count])
    assert country_vault.select_test_candidates(5, country="EG", randomize=True) == [103, 106, 111, 114]


def test_excludes_failed_identity_and_review_identity_across_country_aliases(country_vault, monkeypatch):
    with country_vault._db:
        # One alias is stale; exclusion must apply by identity, not row state.
        country_vault._db.execute("UPDATE accounts SET state='account_failed' WHERE source_row=113")
        country_vault._db.execute("CREATE TABLE account_failure_review (source_row INTEGER PRIMARY KEY)")
        country_vault._db.execute("INSERT INTO account_failure_review VALUES (105)")
    monkeypatch.setattr(vault_module.random, "sample", lambda population, count: population[:count])
    assert country_vault.select_test_candidates(5, country="EG", randomize=True) == [101, 106, 111]
    assert country_vault.select_test_candidates(5, country="LB", randomize=True) == [102, 110]


def test_candidate_preview_does_not_write_or_read_source_file(country_vault, monkeypatch):
    original = country_vault.path.read_bytes()
    traced = []
    country_vault._db.set_trace_callback(traced.append)
    monkeypatch.setattr(preparation, "_journal", lambda *_: pytest.fail("Country preview wrote a journal"))
    report = preparation.prepare_test_accounts(
        country_vault, count=3, account_country="EG", dry_run=True, no_browser=True,
    )
    assert report["account_country"] == "EG" and report["selection_mode"] == "random_country"
    assert len(report["selected_rows"]) == 3
    assert set(report["selected_rows"]) <= {101, 103, 106, 111, 114}
    assert report["attempted_accounts"] == 0
    assert report["play_events_sent"] == report["like_events_sent"] == 0
    assert country_vault.path.read_bytes() == original
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in traced)
    assert "example.invalid" not in json.dumps(report)
    assert "synthetic-secret" not in json.dumps(report)


def test_insufficient_country_pool_fails_before_journal_or_network(country_vault, monkeypatch):
    monkeypatch.setattr(preparation, "_journal", lambda *_: pytest.fail("Shortage wrote a journal"))
    with pytest.raises(SessionError, match="Only 4 additional unique LB accounts"):
        preparation.prepare_test_accounts(country_vault, count=5, account_country="LB", no_browser=True)
    assert not (country_vault.path.parent / "accounts-prepare-tests-report.json").exists()


def test_legacy_candidate_selection_keeps_order_and_start_row(country_vault):
    assert country_vault.select_test_candidates(5) == [101, 102, 103, 105, 106]
    assert country_vault.select_test_candidates(3, start_row=104) == [104, 105, 106]


def test_preparation_keeps_old_vault_callers_without_new_keywords():
    class LegacyVault:
        def select_test_candidates(self, count, *, start_row):
            assert count == 2 and start_row == 9
            return [9, 11]

    report = preparation.prepare_test_accounts(LegacyVault(), count=2, start_row=9, dry_run=True)
    assert report["selected_rows"] == [9, 11]
    assert "account_country" not in report and "selection_mode" not in report


@pytest.mark.parametrize("country", ["eg", "lb", "US", "", " EG", True, 1, [], {}])
def test_invalid_vault_country_rejected_before_database_access(country):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    with pytest.raises(SessionError, match="EG or LB"):
        selected.select_test_candidates(1, country=country, randomize=True)


@pytest.mark.parametrize("randomize", [None, 0, 1, "true", [], {}])
def test_invalid_randomize_rejected_before_database_access(randomize):
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    with pytest.raises(SessionError, match="true or false"):
        selected.select_test_candidates(1, randomize=randomize)


def test_vault_country_rejects_nondefault_start_before_database_access():
    selected = vault_module.AccountVault.__new__(vault_module.AccountVault)
    with pytest.raises(SessionError, match="starting source row"):
        selected.select_test_candidates(1, start_row=104, country="EG", randomize=True)


@pytest.mark.parametrize("country", ["eg", "US", "", "EG ", True, 1, [], {}])
def test_invalid_preparation_country_rejected_before_selection(country):
    with pytest.raises(SessionError, match="EG or LB"):
        preparation.prepare_test_accounts(object(), count=1, account_country=country)


@pytest.mark.parametrize("options", [{"selected_rows": [101]}, {"start_row": 101}])
def test_preparation_country_rejects_conflicting_selection_before_access(options):
    with pytest.raises(SessionError, match="cannot be combined"):
        preparation.prepare_test_accounts(object(), count=1, account_country="EG", **options)


@pytest.mark.parametrize("count", [0, -1, True, 1.0])
def test_country_mode_requires_positive_integer_count(count):
    with pytest.raises(SessionError, match="positive integer"):
        preparation.prepare_test_accounts(object(), count=count, account_country="EG")
