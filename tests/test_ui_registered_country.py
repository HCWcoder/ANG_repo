"""Imported country filters publish cached labels, never account credentials."""

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from anghami_session import ui_server, vault
from test_ui_server import FakeManager


PRIVATE = "synthetic-country-metadata-secret"


def test_registered_country_reader_batches_rows_and_returns_labels_only(monkeypatch):
    database = sqlite3.connect(":memory:")
    database.execute("CREATE TABLE accounts (source_row INTEGER PRIMARY KEY, record BLOB)")
    database.executemany("INSERT INTO accounts VALUES (?, ?)", [
        (row, json.dumps({"country": " EG " if row % 3 == 1 else "lb" if row % 3 == 2 else PRIVATE,
                          "email": PRIVATE, "password": PRIVATE, "legacy_cookies": {"sid": PRIVATE}}).encode())
        for row in range(1, 1002)
    ])
    monkeypatch.setattr(vault, "_unpack", lambda encrypted: json.loads(encrypted))
    saved = object.__new__(vault.AccountVault)
    saved._db = database
    statements = []
    database.set_trace_callback(statements.append)
    try:
        countries = saved.registered_countries([*range(1, 1002), 1, True, -1, 0, "1"])
    finally:
        database.close()
    assert len(countries) == 1001
    assert countries[1] == "EG" and countries[2] == "LB" and countries[3] == "Other"
    assert len([sql for sql in statements if sql.startswith("SELECT source_row, record")]) == 2
    assert PRIVATE not in json.dumps(countries)
    assert set(countries.values()) == {"EG", "LB", "Other"}


def synthetic_service(tmp_path, monkeypatch, *, countries=None):
    calls, calls_lock = [], threading.Lock()
    source = {"hash": "a" * 64}
    class SyntheticVault:
        def __init__(self, _path):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def summary(self):
            return {"records": 4, "unique_accounts": 4, "sessions_saved": 3,
                    "duplicate_rows": 0, "states": {"ready": 3, "login_required": 1},
                    "source_sha256": source["hash"], "password": PRIVATE}
        def test_accounts(self):
            return {"test_rows": [1, 2, 3, 4], "ready_rows": [1, 2, 3], "password": PRIVATE,
                    "accounts": [{"source_row": row, "state": "ready" if row < 4 else "login_required",
                                  "session_saved": row < 4, "email": PRIVATE, "sid": PRIVATE,
                                  "registered_country": PRIVATE} for row in range(1, 5)]}
        def registered_countries(self, rows):
            with calls_lock:
                calls.append(frozenset(rows))
            return countries or {1: "EG", 2: "LB", 3: PRIVATE, 4: "LB", "password": PRIVATE}
    monkeypatch.setattr(ui_server, "AccountVault", SyntheticVault)
    service = ui_server.ConsoleService(tmp_path / "accounts.sqlite3", manager=FakeManager())
    return service, calls, source


def test_ready_cohort_country_labels_are_safe_cached_and_invalidate_with_source(tmp_path, monkeypatch):
    service, calls, source = synthetic_service(tmp_path, monkeypatch)
    first = service.state()
    second = service.state()
    assert first["cohort"] == second["cohort"]
    assert calls == [frozenset({1, 2, 3})]
    assert [item["registered_country"] for item in first["cohort"]["accounts"]] == ["EG", "LB", "Other", "Other"]
    assert all(set(item) == {"source_row", "state", "session_saved", "registered_country"} for item in first["cohort"]["accounts"])
    assert PRIVATE not in json.dumps(first)
    assert "a" * 64 not in json.dumps(first)
    assert service._registered_countries == {1: "EG", 2: "LB", 3: "Other"}
    assert first["limits"]["workers"] is None and first["limits"]["preparation_workers"] is None
    assert first["limits"]["worker_integer_max"] == 2**53 - 1
    source["hash"] = "b" * 64
    service.state()
    assert calls == [frozenset({1, 2, 3}), frozenset({1, 2, 3})]


def test_simultaneous_initial_state_requests_populate_country_cache_once(tmp_path, monkeypatch):
    service, calls, _source = synthetic_service(tmp_path, monkeypatch)
    barrier = threading.Barrier(4)
    def state():
        barrier.wait(timeout=5)
        return service.state()["cohort"]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _number: state(), range(4)))
    assert results == [results[0]] * 4 and calls == [frozenset({1, 2, 3})]


@pytest.mark.skipif(os.name != "nt", reason="DPAPI account import requires Windows")
def test_encrypted_import_exposes_registered_country_without_credentials(tmp_path, monkeypatch):
    source = tmp_path / "registered.txt"
    source.write_text("EG~synthetic-eg@example.invalid~synthetic-password~appsidsave=synthetic-sid~fingerprint=synthetic-fingerprint\n"
                      "LB~synthetic-lb@example.invalid~synthetic-password~appsidsave=synthetic-sid~fingerprint=synthetic-fingerprint\n"
                      "US~synthetic-other@example.invalid~synthetic-password~appsidsave=synthetic-sid~fingerprint=synthetic-fingerprint\n", encoding="utf-8")
    path = tmp_path / "imported.sqlite3"
    vault.migrate_registered(source, path)
    with vault.AccountVault(path) as saved:
        countries = saved.registered_countries([1, 2, 3])
    assert countries == {1: "EG", 2: "LB", 3: "Other"}
    assert "example.invalid" not in json.dumps(countries)
    assert b"synthetic-password" not in path.read_bytes()


@pytest.mark.parametrize("country", [PRIVATE, {}, [], 1, True, None, "EG\npassword", "US"])
def test_safe_report_rejects_arbitrary_registered_country_metadata(country):
    assert ui_server.safe_report({"registered_country": country}) == {"registered_country": "Other"}
