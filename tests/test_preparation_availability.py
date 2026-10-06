"""Preparation pool counts are exact, offline, source cached and private."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading

import pytest

from anghami_session import client, ui_server, vault as vault_module
from anghami_session.errors import SessionError
from test_ui_server import FakeManager, console, request


PRIVATE = "synthetic-preparation-availability-secret"


@pytest.fixture
def availability_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_module, "_crypt", lambda value, decrypt=False: value)
    monkeypatch.setattr(client.requests, "Session", lambda *_a, **_k: pytest.fail("Pool counting created an HTTP transport"))
    path = tmp_path / "synthetic.sqlite3"
    key = b"availability-index"
    records = [
        (1, "EG", "enrolled"), (2, "LB", "enrolled"),
        (101, "EG", "one"), (102, "EG", "one"),
        (103, "LB", "cross"), (104, "EG", "cross"),
        (105, "EG", "failed"), (106, "LB", "failed"),
        (107, "EG", "failure-review"), (108, "LB", "failure-review"),
        (109, "EG", "session-review"), (110, "LB", "session-review"),
        (111, "EG", "held-state"), (112, "LB", "held-state"),
        (113, "eg", "lowercase"), (114, "EG ", "spaced"),
        (115, "US", "other"), (116, "EG", "saved-unenrolled"),
    ]
    with sqlite3.connect(path) as database:
        database.executescript("""
            CREATE TABLE metadata (name TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE accounts (
                source_row INTEGER PRIMARY KEY, email_key TEXT NOT NULL,
                record BLOB NOT NULL, session BLOB,
                state TEXT NOT NULL DEFAULT 'login_required', checked_at_utc TEXT
            );
            CREATE TABLE account_failure_review (source_row INTEGER PRIMARY KEY);
            CREATE TABLE account_session_review (source_row INTEGER PRIMARY KEY, email_key TEXT);
        """)
        database.executemany("INSERT INTO metadata VALUES (?, ?)", {
            "format_version": "1", "index_key": key, "source_sha256": "a" * 64,
        }.items())
        for row, country, identity in records:
            email = identity + "@example.invalid"
            packed = vault_module._pack({"source_row": row, "country": country, "email": email, "password": PRIVATE})
            database.execute("INSERT INTO accounts(source_row,email_key,record) VALUES (?,?,?)",
                             (row, vault_module._email_key(key, email), packed))
        database.execute("UPDATE accounts SET state='account_failed' WHERE source_row=105")
        database.execute("INSERT INTO account_failure_review VALUES (107)")
        database.execute("INSERT INTO account_session_review SELECT source_row,email_key FROM accounts WHERE source_row=109")
        database.execute("UPDATE accounts SET state='session_review_pending' WHERE source_row=111")
        database.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=116", (b"synthetic-saved-session",))
    with vault_module.AccountVault(path) as selected:
        yield selected


def test_availability_matches_each_selector_pool_and_exact_tags(availability_vault):
    selected = availability_vault
    tags = selected.preparation_country_tags()
    assert tags[113] == tags[114] == "Other"
    counts = selected.preparation_availability(tags)
    assert counts == {"available": True, "start_row": 1, "counts": {"EG": 3, "LB": 1, "all": 8}}
    for country in ("EG", "LB"):
        assert counts["counts"][country] == len(selected.select_test_candidates(1000, country=country, randomize=True))
    assert counts["counts"]["all"] == len(selected.select_test_candidates(1000))
    # A ready but unenrolled identity still requires validation and enrollment.
    assert 116 in selected.select_test_candidates(1000, country="EG", randomize=True)
    assert PRIVATE not in json.dumps(counts)
    assert "@" not in json.dumps(counts)


@pytest.mark.parametrize("start", [1, 102, 104, 107, 116, 117, 2**31, 2**53 - 1])
def test_row_order_count_respects_start_without_changing_country_pools(availability_vault, start):
    selected = availability_vault
    value = selected.preparation_availability(selected.preparation_country_tags(), start_row=start)
    assert value["counts"]["all"] == len(selected.select_test_candidates(1000, start_row=start))
    assert value["counts"]["EG"] == 3 and value["counts"]["LB"] == 1
    assert value["start_row"] == start


def test_counting_refreshes_mutable_enrollment_and_holds_without_decryption(availability_vault, monkeypatch):
    selected = availability_vault
    tags = selected.preparation_country_tags()
    before = selected.path.read_bytes()
    statements = []
    selected._db.set_trace_callback(statements.append)
    monkeypatch.setattr(vault_module, "_unpack", lambda *_: pytest.fail("Cached counting decrypted a record"))
    assert selected.preparation_availability(tags)["counts"] == {"EG": 3, "LB": 1, "all": 8}
    assert selected.path.read_bytes() == before
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    with selected._db:
        selected._db.execute("CREATE TABLE test_accounts (source_row INTEGER PRIMARY KEY)")
        selected._db.execute("INSERT INTO test_accounts VALUES (101)")
        selected._db.execute("UPDATE accounts SET state='session_review_pending' WHERE source_row=104")
    assert selected.preparation_availability(tags)["counts"] == {"EG": 1, "LB": 0, "all": 6}


def test_no_candidates_is_available_zero_and_does_not_write(availability_vault):
    selected = availability_vault
    with selected._db:
        selected._db.execute("CREATE TABLE test_accounts (source_row INTEGER PRIMARY KEY)")
        selected._db.execute("INSERT INTO test_accounts SELECT source_row FROM accounts")
    before = selected.path.read_bytes()
    assert selected.preparation_availability(selected.preparation_country_tags()) == {
        "available": True, "start_row": 1, "counts": {"EG": 0, "LB": 0, "all": 0},
    }
    assert selected.path.read_bytes() == before


@pytest.mark.parametrize("tags", [{}, {1: "EG"}, {True: "EG"}, {1: []}, {1: "eg"}, {1: PRIVATE}])
def test_incomplete_or_unsafe_import_tags_cannot_publish_a_pool(availability_vault, tags):
    with pytest.raises(SessionError):
        availability_vault.preparation_availability(tags)


@pytest.mark.parametrize("start", [0, -1, True, "1", 1.0, 2**53])
def test_invalid_counting_start_rejected_before_database(start):
    selected = object.__new__(vault_module.AccountVault)
    with pytest.raises(SessionError):
        selected.preparation_availability({}, start_row=start)


def test_verified_backup_reads_country_tags_without_per_record_decryption(availability_vault, monkeypatch):
    selected = availability_vault
    raw = b"EG~first@example.invalid~secret~x=y~x=y\n\nLB~second@example.invalid~secret~x=y~x=y\neg~third@example.invalid~secret~x=y~x=y\n"
    with selected._db:
        selected._db.execute("INSERT INTO metadata VALUES ('backup_relative','backups/import.dpapi')")
        selected._db.execute("UPDATE metadata SET value=? WHERE name='source_sha256'", (hashlib.sha256(raw).hexdigest(),))
    paths = []
    monkeypatch.setattr(vault_module, "load_protected_bytes", lambda path: paths.append(path) or raw)
    monkeypatch.setattr(vault_module, "_unpack", lambda *_: pytest.fail("Verified source backup decrypted individual records"))
    assert selected.preparation_country_tags() == {1: "EG", 3: "LB", 4: "Other"}
    assert paths == [selected.path.parent / "backups" / "import.dpapi"]


def test_backup_digest_failure_does_not_fall_back_to_unbound_records(availability_vault, monkeypatch):
    selected = availability_vault
    with selected._db:
        selected._db.execute("INSERT INTO metadata VALUES ('backup_relative','backups/import.dpapi')")
    monkeypatch.setattr(vault_module, "load_protected_bytes", lambda *_: b"tampered source")
    monkeypatch.setattr(vault_module, "_unpack", lambda *_: pytest.fail("Unverified source fell back to records"))
    with pytest.raises(SessionError):
        selected.preparation_country_tags()


@pytest.mark.parametrize("relative", ["../outside.dpapi", "import.dpapi", "../backups/import.dpapi"])
def test_source_backup_cannot_escape_expected_backups_folder(availability_vault, monkeypatch, relative):
    selected = availability_vault
    with selected._db:
        selected._db.execute("INSERT INTO metadata VALUES ('backup_relative',?)", (relative,))
    monkeypatch.setattr(vault_module, "load_protected_bytes", lambda *_: pytest.fail("Unsafe path read"))
    with pytest.raises(SessionError):
        selected.preparation_country_tags()


def synthetic_service(tmp_path, monkeypatch):
    calls, guard = [], threading.Lock()
    source, remaining = {"hash": "a" * 64}, {"EG": 3, "LB": 4, "all": 8}

    class SyntheticVault:
        def __init__(self, _path):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def summary(self):
            return {"records": 10, "unique_accounts": 9, "sessions_saved": 1, "duplicate_rows": 1,
                    "states": {}, "source_sha256": source["hash"], "email": PRIVATE}
        def test_accounts(self):
            return {"test_rows": [], "ready_rows": [], "accounts": []}
        def preparation_country_tags(self):
            with guard:
                calls.append(source["hash"])
            return {101: "EG", 102: "LB", 103: "Other"}
        def preparation_availability(self, tags, *, start_row=1):
            assert tags == {101: "EG", 102: "LB", 103: "Other"}
            return {"available": True, "counts": {**remaining, "all": remaining["all"] if start_row == 1 else 2},
                    "start_row": start_row, "password": PRIVATE, "email": PRIVATE}

    monkeypatch.setattr(ui_server, "AccountVault", SyntheticVault)
    for name in ("load_packetstream_proxy", "load_test_pool"):
        monkeypatch.setattr(ui_server, name, lambda *_: (_ for _ in ()).throw(SessionError("not configured")))
    monkeypatch.setattr(ui_server.StickyProxyPool, "load", lambda *_: (_ for _ in ()).throw(SessionError("not configured")))
    return ui_server.ConsoleService(tmp_path / "synthetic.sqlite3", manager=FakeManager()), calls, source, remaining


def test_state_caches_source_tags_but_refreshes_remaining_counts(tmp_path, monkeypatch):
    service, calls, source, remaining = synthetic_service(tmp_path, monkeypatch)
    first = service.state()
    remaining["EG"] = 2
    second = service.state()
    assert first["preparation_availability"]["counts"]["EG"] == 3
    assert second["preparation_availability"]["counts"]["EG"] == 2
    assert calls == ["a" * 64]
    assert PRIVATE not in json.dumps(second)
    assert "a" * 64 not in json.dumps(second)
    source["hash"] = "b" * 64
    service.state()
    assert calls == ["a" * 64, "b" * 64]
    assert service.preparation_availability(104) == {"available": True, "start_row": 104,
                                                    "counts": {"EG": 2, "LB": 4, "all": 2}}


def test_concurrent_state_requests_initialize_tags_once(tmp_path, monkeypatch):
    service, calls, _source, _remaining = synthetic_service(tmp_path, monkeypatch)
    barrier = threading.Barrier(4)
    def read(_number):
        barrier.wait(timeout=5)
        return service.state()["preparation_availability"]
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(read, range(4)))
    assert results == [results[0]] * 4
    assert calls == ["a" * 64]


@pytest.mark.parametrize("malformed", [None, {}, {"available": True, "start_row": 1, "counts": {"EG": True, "LB": 2, "all": 3}},
                                       {"available": True, "start_row": 1, "counts": {"EG": 11, "LB": 2, "all": 3}},
                                       {"available": True, "start_row": 2, "counts": {"EG": 1, "LB": 2, "all": 3}}])
def test_malformed_capability_is_explicitly_unavailable(tmp_path, monkeypatch, malformed):
    service, _calls, _source, _remaining = synthetic_service(tmp_path, monkeypatch)
    monkeypatch.setattr(ui_server.AccountVault, "preparation_availability", lambda *_a, **_k: malformed)
    assert service.state()["preparation_availability"] == {"available": False, "start_row": 1, "counts": {"EG": 0, "LB": 0, "all": 0}}


def test_missing_capability_or_vault_is_unavailable(tmp_path, monkeypatch):
    service, _calls, _source, _remaining = synthetic_service(tmp_path, monkeypatch)
    monkeypatch.delattr(ui_server.AccountVault, "preparation_availability")
    assert service.state()["preparation_availability"]["available"] is False
    monkeypatch.setattr(ui_server, "AccountVault", lambda *_: (_ for _ in ()).throw(SessionError(PRIVATE)))
    assert service.preparation_availability(7) == {"available": False, "start_row": 7, "counts": {"EG": 0, "LB": 0, "all": 0}}


def test_invalid_source_hash_never_uses_or_publishes_cached_country_data(tmp_path, monkeypatch):
    service, calls, source, _remaining = synthetic_service(tmp_path, monkeypatch)
    assert service.state()["preparation_availability"]["available"] is True
    source["hash"] = PRIVATE
    assert service.state()["preparation_availability"] == {"available": False, "start_row": 1, "counts": {"EG": 0, "LB": 0, "all": 0}}
    assert calls == ["a" * 64]


def test_authenticated_read_only_availability_endpoint(console):
    calls = []
    console.service.preparation_availability = lambda start: calls.append(start) or {"available": True, "start_row": start, "counts": {"EG": 3, "LB": 2, "all": 1}}
    status, _headers, value = request(console, "GET", "/api/preparation-availability?start_row=101")
    assert status == 200 and json.loads(value)["counts"]["all"] == 1 and calls == [101]
    assert request(console, "GET", "/api/preparation-availability?start_row=101", token=False)[0] == 403
    assert calls == [101]


@pytest.mark.parametrize("query", ["", "?start_row=0", "?start_row=-1", "?start_row=01", "?start_row=1.0", "?start_row=true",
                                   "?start_row=9007199254740992", "?start_row=1&start_row=2", "?start_row=1&password=hidden", "?start_row=", "?start_row=%E2%91%A0"])
def test_endpoint_rejects_invalid_queries_without_pool_read(console, query):
    console.service.preparation_availability = lambda *_: pytest.fail("Invalid endpoint query read a pool")
    assert request(console, "GET", "/api/preparation-availability" + query)[0] == 400
