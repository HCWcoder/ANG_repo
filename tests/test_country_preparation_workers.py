"""Concurrent country recovery uses isolated synthetic vaults, never live accounts."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest

from anghami_session import country_preparation as country
from anghami_session import capture
from anghami_session import preparation
from anghami_session.errors import SessionError


PRIVATE_PASSWORD = "synthetic-parallel-password"
PRIVATE_ERROR = "synthetic-parallel-private https://private.invalid/?sid=synthetic"


class VaultFactory:
    """Open real SQLite connections on their owning threads over synthetic data."""

    def __init__(self, path, records, source_hash):
        self.path = Path(path)
        self.records = records
        self.source_hash = source_hash
        self.events = []
        self.instances = []
        self.lock = threading.Lock()
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE accounts(source_row INTEGER PRIMARY KEY, email_key BLOB,
                                      record BLOB, state TEXT, session BLOB);
                CREATE TABLE test_accounts(source_row INTEGER PRIMARY KEY);
            """)
            for row, record in records.items():
                identity = hashlib.sha256(record["email"].strip().casefold().encode()).digest()
                db.execute("INSERT INTO accounts VALUES(?,?,?,?,?)", (
                    row, identity, b"synthetic", "login_required", None,
                ))

    def log(self, *event):
        with self.lock:
            self.events.append((*event, threading.get_ident()))

    def __call__(self, path):
        assert Path(path) == self.path
        vault = SyntheticVault(self)
        with self.lock:
            self.instances.append(vault)
        return vault


class SyntheticVault:
    def __init__(self, factory):
        self.factory = factory
        self.path = factory.path
        self.owner = threading.get_ident()
        self.closed = False
        # Deliberately retain SQLite's default thread check. Sharing a parent
        # connection with workers must fail this integration-style fixture.
        self._db = sqlite3.connect(self.path, timeout=5)
        factory.log("open", id(self))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        assert self.owner == threading.get_ident()
        self._db.close()
        self.closed = True
        self.factory.log("close", id(self))

    def metadata(self, name):
        assert name == "source_sha256"
        return self.factory.source_hash

    def enrolled_test_rows(self):
        return frozenset(row[0] for row in self._db.execute("SELECT source_row FROM test_accounts"))

    def record(self, row):
        assert self.owner == threading.get_ident()
        self.factory.log("record", row)
        return deepcopy(self.factory.records[row])

    def session(self, row):
        assert self.owner == threading.get_ident()
        state, saved = self._db.execute("SELECT state,session FROM accounts WHERE source_row=?", (row,)).fetchone()
        if state != "ready" or saved is None:
            raise SessionError("No synthetic session")
        return {"synthetic_saved_row": row}

    def attach(self, row, saved, **options):
        assert self.owner == threading.get_ident()
        assert saved == {"synthetic_saved_row": row}
        self.factory.log("attach", row, options.get("proxy"))
        with self._db:
            self._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=?", (b"synthetic", row))
        return {"source_row": row, "verified": True}

    def enable_test_account(self, row):
        assert self.owner == threading.get_ident()
        assert self.session(row) == {"synthetic_saved_row": row}
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO test_accounts VALUES(?)", (row,))
        self.factory.log("enroll", row)

    def select_test_candidates(self, *_args, **_options):
        pytest.fail("Parallel country work must stay on its frozen source row")

    def test_play_record(self, *_args, **_options):
        pytest.fail("Account preparation sent a play")

    def test_like(self, *_args, **_options):
        pytest.fail("Account preparation sent a like")


@pytest.fixture
def imported(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Offline recovery opened a browser login"))
    monkeypatch.setattr(capture, "launch_browser", lambda **_: pytest.fail("Offline recovery started a browser"))
    records = {row: {
        "country": "EG", "email": f"synthetic-parallel-{row}@example.invalid",
        "password": PRIVATE_PASSWORD,
    } for row in range(1, 9)}
    raw = "\n".join(f"EG~{record['email']}~{PRIVATE_PASSWORD}~k=synthetic~cookie=synthetic" for record in records.values()).encode()
    source = tmp_path / "registered.txt"
    source.write_bytes(raw)
    factory = VaultFactory(tmp_path / "accounts.sqlite3", records, hashlib.sha256(raw).hexdigest())
    parent = factory(factory.path)
    monkeypatch.setattr(country, "AccountVault", factory)
    plan = country.build_plan(parent, source, country="EG")
    yield parent, plan, source, factory
    parent.__exit__()


def row_from(record):
    return int(record["email"].split("@")[0].rsplit("-", 1)[1])


def install_recovery(monkeypatch, factory, hook=None):
    def recover(record, *, proxy=None):
        row = row_from(record)
        factory.log("recover", row, proxy)
        if hook is not None:
            hook(row, proxy)
        return {"synthetic_saved_row": row}, {"preparation_method": "http", "server_verified": True}

    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=recover))


def states(path, plan):
    return {item["source_row"]: item for item in country.load_progress(path, plan)["rows"]}


def assert_safe(value, factory):
    encoded = json.dumps(value)
    assert PRIVATE_PASSWORD not in encoded and PRIVATE_ERROR not in encoded
    assert "private.invalid" not in encoded and "sid=synthetic" not in encoded
    assert all(record["email"] not in encoded for record in factory.records.values())


def test_workers_use_isolated_thread_connections_bounded_concurrency_and_hard_attempt_limit(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    barrier = threading.Barrier(3, timeout=5)
    guard = threading.Lock()
    active = peak = 0
    parent_thread = threading.get_ident()
    checkpoint_threads = []
    durable_snapshots = []
    journal_paths = []
    original_atomic, original_journal = country._atomic_json, preparation._journal

    def atomic(destination, value):
        if Path(destination) == path:
            checkpoint_threads.append(threading.get_ident())
        result = original_atomic(destination, value)
        if Path(destination) == path:
            with guard:
                durable_snapshots.append(deepcopy(value))
        return result

    def journal(report, destination):
        if "requested_accounts" in report:
            with guard:
                journal_paths.append((report["selected_rows"][0], Path(destination)))
        return original_journal(report, destination)

    def hook(row, proxy):
        nonlocal active, peak
        assert proxy is None
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            if row <= 3:
                barrier.wait()
            with guard:
                durable = durable_snapshots[-1]
            item = next(item for item in durable["rows"] if item["source_row"] == row)
            assert item["attempts"] == 1 and item["state"] == "in_progress"
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(country, "_atomic_json", atomic)
    monkeypatch.setattr(preparation, "_journal", journal)
    install_recovery(monkeypatch, factory, hook)
    result = country.run_plan(parent, plan, path, workers=3, no_browser=True, limit=5)
    recovered = [event[1] for event in factory.events if event[0] == "recover"]
    assert sorted(recovered) == [1, 2, 3, 4, 5] and len(recovered) == 5
    assert peak == 3
    assert set(checkpoint_threads) == {parent_thread}
    worker_vaults = factory.instances[1:]
    assert worker_vaults and all(v.owner != parent_thread and v.closed for v in worker_vaults)
    assert len({id(v._db) for v in worker_vaults}) == len(worker_vaults)
    paths_by_row = {row: {destination for number, destination in journal_paths if number == row} for row in recovered}
    assert all(len(paths) == 1 for paths in paths_by_row.values())
    assert len({next(iter(paths)) for paths in paths_by_row.values()}) == 5
    assert all(destination.name != "accounts-prepare-tests-report.json" for _, destination in journal_paths)
    for destination in {destination for _, destination in journal_paths}:
        assert_safe(json.loads(destination.read_text()), factory)
    assert result["workers"] == 3 and result["active_rows"] == [] and result["active_row"] is None
    assert result["pause_reason"] == "limit_reached"
    assert sum(item["attempts"] for item in states(path, plan).values()) == 5
    assert_safe(result, factory)
    assert_safe(json.loads(path.read_text()), factory)


@pytest.mark.parametrize("value", [None, False, True, 0, -1, country.MAX_WORKER_REQUEST + 1, 1.5, "2", [], {}])
def test_invalid_worker_selection_is_rejected_before_any_mutation(imported, tmp_path, value):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    before = list(factory.events)
    with pytest.raises(SessionError):
        country.run_plan(parent, plan, path, workers=value, no_browser=True,
                         prepare=lambda *_a, **_k: pytest.fail("Invalid selection started recovery"))
    assert factory.events == before and not path.exists()
    assert not (tmp_path / "country-preparation.lock").exists()


def test_browser_preparation_cannot_start_multiple_workers(imported, tmp_path):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    before = list(factory.events)
    with pytest.raises(SessionError):
        country.run_plan(parent, plan, path, workers=2,
                         prepare=lambda *_a, **_k: pytest.fail("Concurrent browser login started"))
    assert factory.events == before and not path.exists()


def test_stop_requested_by_active_worker_drains_only_already_dispatched_rows(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path, stop = tmp_path / "progress.json", tmp_path / "progress.stop"
    barrier = threading.Barrier(2, timeout=5)
    stopped = threading.Event()

    def hook(row, _proxy):
        assert row in {1, 2}
        barrier.wait()
        if row == 1:
            stop.write_text("stop", encoding="utf-8")
            stopped.set()
        else:
            assert stopped.wait(5)

    install_recovery(monkeypatch, factory, hook)
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True, stop_path=stop)
    rows = states(path, plan)
    assert result["pause_reason"] == "stop_requested" and result["active_rows"] == []
    assert {row for row, item in rows.items() if item["state"] == "ready"} == {1, 2}
    assert all(rows[row]["state"] == "pending" and rows[row]["attempts"] == 0 for row in range(3, 9))
    assert all(v.closed for v in factory.instances[1:])


def test_ordinary_failure_drains_existing_batch_before_replacements_and_never_retries(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    barrier = threading.Barrier(2, timeout=5)
    failed_checkpoint = threading.Event()
    original_atomic = country._atomic_json

    def atomic(destination, value):
        result = original_atomic(destination, value)
        if Path(destination) == path and any(item["source_row"] == 1 and item["state"] == "failed" for item in value["rows"]):
            failed_checkpoint.set()
        return result

    def hook(row, _proxy):
        if row <= 2:
            barrier.wait()
            if row == 1:
                raise SessionError(PRIVATE_ERROR)
            assert failed_checkpoint.wait(5)
        else:
            with sqlite3.connect(factory.path) as db:
                assert db.execute("SELECT state FROM accounts WHERE source_row=2").fetchone()[0] == "ready"

    monkeypatch.setattr(country, "_atomic_json", atomic)
    install_recovery(monkeypatch, factory, hook)
    first = country.run_plan(parent, plan, path, workers=2, no_browser=True)
    rows = states(path, plan)
    assert first["status"] == "completed" and first["pause_reason"] is None
    assert rows[1]["state"] == "failed" and rows[2]["state"] == "ready"
    assert all(rows[row]["state"] == "ready" for row in range(3, 9))
    assert first["active_rows"] == []
    install_recovery(monkeypatch, factory)
    second = country.run_plan(parent, plan, path, workers=2, no_browser=True, limit=2)
    recovered = [event[1] for event in factory.events if event[0] == "recover"]
    assert sorted(recovered) == list(range(1, 9))
    assert states(path, plan)[1]["attempts"] == 1
    assert second["counts"]["ready"] == 7
    assert_safe(first, factory)
    assert_safe(json.loads(path.read_text()), factory)


def test_failure_threshold_is_latched_while_successful_peer_drains_and_review_is_one_attempt(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    barrier = threading.Barrier(4, timeout=5)
    threshold_checkpoint = threading.Event()
    original_atomic = country._atomic_json

    def atomic(destination, value):
        result = original_atomic(destination, value)
        if Path(destination) == path and value["consecutive_failures"] == 3:
            threshold_checkpoint.set()
        return result

    def hook(row, _proxy):
        assert row <= 4
        barrier.wait()
        if row <= 3:
            raise SessionError(PRIVATE_ERROR)
        assert threshold_checkpoint.wait(5)

    monkeypatch.setattr(country, "_atomic_json", atomic)
    install_recovery(monkeypatch, factory, hook)
    first = country.run_plan(parent, plan, path, workers=4, no_browser=True)
    rows = states(path, plan)
    assert first["pause_reason"] == first["failure_hold"] == "repeated_failures"
    assert first["consecutive_failures"] == 3
    assert all(rows[row]["state"] == "failed" for row in (1, 2, 3))
    assert rows[4]["state"] == "ready"
    assert all(rows[row]["state"] == "pending" for row in range(5, 9))
    before = list(factory.events)
    install_recovery(monkeypatch, factory)
    held = country.run_plan(parent, plan, path, workers=4, no_browser=True, fresh_plan=True)
    assert held["pause_reason"] == "repeated_failures" and factory.events == before
    reviewed = country.run_plan(parent, plan, path, workers=4, no_browser=True, limit=1, resume_after_review=True)
    assert reviewed["counts"]["ready"] == 2 and reviewed["counts"]["failed"] == 3
    assert states(path, plan)[5]["state"] == "ready"
    assert sorted(event[1] for event in factory.events if event[0] == "recover") == [1, 2, 3, 4, 5]


def test_interrupted_worker_is_quarantined_with_completed_peer_preserved(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    barrier = threading.Barrier(2, timeout=5)

    def hook(row, _proxy):
        assert row in {1, 2}
        barrier.wait()
        if row == 1:
            raise KeyboardInterrupt()

    install_recovery(monkeypatch, factory, hook)
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True)
    rows = states(path, plan)
    assert result["status"] == "attention_required" and result["pause_reason"] == "unknown_attempt"
    assert rows[1]["state"] == "unknown" and rows[1]["error_code"] == "interrupted_unknown"
    assert rows[2]["state"] == "ready"
    before = list(factory.events)
    install_recovery(monkeypatch, factory)
    held = country.run_plan(parent, plan, path, workers=2, no_browser=True)
    assert held["pause_reason"] == "unknown_attempt"
    assert factory.events == before
    continued = country.run_plan(parent, plan, path, workers=2, no_browser=True, fresh_plan=True, limit=1)
    assert continued["counts"]["unknown"] == 1 and states(path, plan)[3]["state"] == "ready"
    assert [event[1] for event in factory.events if event[0] == "recover"].count(1) == 1


class SyntheticProxy:
    country = "EG"

    def __init__(self, factory, *, hook=None, verification=None):
        self.factory, self.hook, self.verification = factory, hook, verification

    def summary(self):
        return {"provider": "PacketStream", "country": self.country, "sticky": True}

    def verify_country(self):
        self.factory.log("verify_proxy", self)
        if self.hook is not None:
            self.hook()
        if self.verification is not None:
            return self.verification
        return {**self.summary(), "country_verified": True, "proxy_used": True}


def test_each_parallel_account_uses_its_own_verified_egypt_sticky_profile(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    profiles = []
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5)

    def load(destination):
        assert Path(destination) == factory.path.parent / "packetstream.dpapi"
        profile = SyntheticProxy(factory)
        with lock:
            profiles.append(profile)
        return profile

    def hook(row, profile):
        assert profile in profiles and profile.country == "EG"
        assert profile.summary()["sticky"] is True
        assert any(event[0] == "verify_proxy" and event[1] is profile for event in factory.events)
        if row <= 2:
            barrier.wait()

    monkeypatch.setattr(country, "load_packetstream_proxy", load)
    install_recovery(monkeypatch, factory, hook)
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True, proxy_egypt=True, limit=4)
    recoveries = [event for event in factory.events if event[0] == "recover"]
    attachments = [event for event in factory.events if event[0] == "attach"]
    assert len(profiles) == len(recoveries) == len(attachments) == 4
    assert len({id(profile) for profile in profiles}) == 4
    for recovery in recoveries:
        row, profile = recovery[1:3]
        attachment = next(event for event in attachments if event[1] == row)
        assert attachment[2] is profile and attachment[-1] == recovery[-1]
        assert next(index for index, event in enumerate(factory.events) if event[0] == "verify_proxy" and event[1] is profile) < factory.events.index(recovery)
    assert result["connection"] == "proxy_egypt" and result["proxy"]["country"] == "EG"
    assert result["play_events_sent"] == result["like_events_sent"] == 0
    assert_safe(result, factory)


def test_failed_parallel_proxy_preflight_stops_dispatch_drains_peer_and_never_falls_back(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    peer_entered, failure_persisted = threading.Event(), threading.Event()
    lock = threading.Lock()
    profiles = []
    original_atomic = country._atomic_json

    def atomic(destination, value):
        result = original_atomic(destination, value)
        if Path(destination) == path and value.get("proxy_failure") is not None:
            failure_persisted.set()
        return result

    def fail():
        assert peer_entered.wait(5)
        raise SessionError("The proxy country check did not verify Egypt.")

    def load(_destination):
        with lock:
            profile = SyntheticProxy(factory, hook=fail if not profiles else None)
            profiles.append(profile)
            return profile

    def hook(_row, profile):
        assert profile is profiles[1]
        peer_entered.set()
        assert failure_persisted.wait(5)

    monkeypatch.setattr(country, "_atomic_json", atomic)
    monkeypatch.setattr(country, "load_packetstream_proxy", load)
    install_recovery(monkeypatch, factory, hook)
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True, proxy_egypt=True)
    rows = states(path, plan)
    failed_row = result["proxy_failure"]["source_row"]
    assert result["pause_reason"] == "proxy_preflight_failed"
    assert result["proxy_failure"]["code"] == "egypt_unverified"
    # A parallel dispatch consumes one limit slot even if proxy preflight fails;
    # account credentials still remain untouched and there is no direct retry.
    assert rows[failed_row]["state"] == "failed" and rows[failed_row]["attempts"] == 1
    assert not any(event[0] in {"record", "recover", "attach", "enroll"} and event[1] == failed_row for event in factory.events)
    assert len(profiles) == 2 and result["counts"]["ready"] == result["counts"]["failed"] == 1
    assert all(rows[row]["state"] == "pending" for row in range(3, 9))
    assert all(v.closed for v in factory.instances[1:])


@pytest.mark.parametrize("verification", [
    {"country": "LB", "country_verified": True, "proxy_used": True},
    {"country": "EG", "country_verified": False, "proxy_used": True},
    {"country": "EG", "country_verified": True, "proxy_used": False},
    {"country": "EG", "country_verified": 1, "proxy_used": True},
])
def test_parallel_route_must_prove_egypt_before_any_account_work(imported, tmp_path, monkeypatch, verification):
    parent, plan, _source, factory = imported
    profile = SyntheticProxy(factory, verification=verification)
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda _path: profile)
    install_recovery(monkeypatch, factory, lambda *_: pytest.fail("Unverified route reached account recovery"))
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=2, no_browser=True, proxy_egypt=True, limit=1)
    assert result["pause_reason"] == "proxy_preflight_failed"
    assert not any(event[0] in {"record", "recover", "attach", "enroll"} for event in factory.events)


def test_source_change_during_preflight_aborts_without_account_request(imported, tmp_path, monkeypatch):
    parent, plan, source, factory = imported
    path = tmp_path / "progress.json"

    def change_source():
        source.write_bytes(source.read_bytes() + b"\n")

    monkeypatch.setattr(country, "load_packetstream_proxy", lambda _path: SyntheticProxy(factory, hook=change_source))
    install_recovery(monkeypatch, factory, lambda *_: pytest.fail("Changed source reached recovery"))
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True, proxy_egypt=True, limit=1)
    assert result["status"] == "attention_required" and result["pause_reason"] == "source_changed"
    assert all(item["attempts"] == 0 and item["state"] == "pending" for item in states(path, plan).values())
    assert not any(event[0] in {"record", "recover", "attach", "enroll"} for event in factory.events)


def test_changed_identity_stops_before_dispatch_or_credentials(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    with parent._db:
        parent._db.execute("UPDATE accounts SET email_key=? WHERE source_row=1", (b"changed-synthetic-identity",))
    install_recovery(monkeypatch, factory, lambda *_: pytest.fail("Changed identity reached recovery"))
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True)
    assert result["status"] == "attention_required" and result["pause_reason"] == "scope_mismatch"
    assert len(factory.instances) == 1
    assert all(item["attempts"] == 0 for item in states(path, plan).values())


def test_worker_rechecks_exact_country_before_recovering_credentials(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    factory.records[1]["country"] = "LB"
    install_recovery(monkeypatch, factory, lambda *_: pytest.fail("Wrong country reached recovery"))
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=2, no_browser=True, limit=1)
    assert result["status"] == "attention_required" and result["pause_reason"] == "scope_mismatch"
    assert result["counts"]["unknown"] == 1 and result["counts"]["ready"] == 0
    assert not any(event[0] in {"recover", "attach", "enroll"} for event in factory.events)


def test_parallel_plan_skips_ready_enrolled_duplicates_and_other_country(imported, tmp_path, monkeypatch):
    parent, _old_plan, source, factory = imported
    factory.records[7]["email"] = factory.records[1]["email"].upper()
    factory.records[8]["country"] = "LB"
    identity = parent._db.execute("SELECT email_key FROM accounts WHERE source_row=1").fetchone()[0]
    with parent._db:
        parent._db.execute("UPDATE accounts SET email_key=? WHERE source_row=7", (identity,))
        parent._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=1", (b"synthetic",))
        parent._db.execute("INSERT INTO test_accounts VALUES(2)")
    raw = "\n".join(f"{record['country']}~{record['email']}~{PRIVATE_PASSWORD}~k=synthetic~cookie=synthetic" for record in factory.records.values()).encode()
    source.write_bytes(raw)
    factory.source_hash = hashlib.sha256(raw).hexdigest()
    plan = country.build_plan(parent, source, country="EG")
    assert plan["tagged_rows"] == 7 and plan["duplicate_rows"] == 1
    install_recovery(monkeypatch, factory)
    result = country.run_plan(parent, plan, tmp_path / "progress.json", workers=2, no_browser=True)
    assert sorted(event[1] for event in factory.events if event[0] == "recover") == [3, 4, 5, 6]
    assert result["counts"]["already_ready"] == result["counts"]["already_enrolled"] == 1
    assert result["counts"]["ready"] == 4 and result["status"] == "completed"


def test_coordinator_interrupt_durably_quarantines_and_drains_completed_work(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    recovery_started, interruption_saved = threading.Event(), threading.Event()
    original_wait, original_atomic = country.wait, country._atomic_json
    interrupted = False

    def hook(_row, _proxy):
        recovery_started.set()
        assert interruption_saved.wait(5)

    def wait(futures, **options):
        nonlocal interrupted
        if not interrupted:
            assert recovery_started.wait(5)
            interrupted = True
            raise KeyboardInterrupt()
        return original_wait(futures, **options)

    def atomic(destination, value):
        result = original_atomic(destination, value)
        if Path(destination) == path and any(item["state"] == "unknown" for item in value["rows"]):
            interruption_saved.set()
        return result

    monkeypatch.setattr(country, "wait", wait)
    monkeypatch.setattr(country, "_atomic_json", atomic)
    install_recovery(monkeypatch, factory, hook)
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True, limit=1)
    assert interrupted and interruption_saved.is_set()
    assert result["pause_reason"] == "stop_requested" and result["active_rows"] == []
    assert states(path, plan)[1]["state"] == "ready"
    assert all(states(path, plan)[row]["state"] == "pending" for row in range(2, 9))
    assert all(v.closed for v in factory.instances[1:])


@pytest.mark.parametrize("field,value", [
    ("workers", 0), ("workers", country.MAX_WORKER_REQUEST + 1), ("workers", True),
    ("active_rows", [1, 1]), ("active_rows", [9]),
    ("active_rows", [True]), ("active_rows", "1"),
    ("active_rows", [1]), ("active_rows", [1, 2, 3]),
    ("active_row", 1),
])
def test_malformed_parallel_checkpoint_rejected_without_rewrite(imported, tmp_path, monkeypatch, field, value):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    progress = country._new_progress(plan)
    progress.update(no_browser=True, workers=2)
    progress[field] = value
    path.write_text(json.dumps(progress), encoding="utf-8")
    before, events = path.read_bytes(), list(factory.events)
    install_recovery(monkeypatch, factory, lambda *_: pytest.fail("Invalid checkpoint reached recovery"))
    with pytest.raises(SessionError):
        country.run_plan(parent, plan, path, workers=2, no_browser=True)
    assert path.read_bytes() == before and factory.events == events


def test_legacy_checkpoint_without_worker_fields_remains_serial_compatible(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    progress = country._new_progress(plan)
    progress.pop("workers")
    progress.pop("active_rows")
    path.write_text(json.dumps(progress), encoding="utf-8")
    loaded = country.load_progress(path, plan)
    assert loaded["workers"] == 1 and loaded["active_rows"] == []
    install_recovery(monkeypatch, factory)
    result = country.run_plan(parent, plan, path, no_browser=True, limit=1)
    assert result["workers"] == 1 and result["counts"]["ready"] == 1
    assert len(factory.instances) == 1


def test_workers_cli_is_offline_for_preview_and_rejects_browser_parallelism(imported, monkeypatch, capsys):
    parent, _plan, source, factory = imported
    monkeypatch.setattr(country, "run_plan", lambda *_a, **_k: pytest.fail("Preview started a recovery"))
    args = ["--country", "EG", "--vault", str(factory.path), "--source", str(source), "--workers", "4"]
    assert country.main(args + ["--no-browser", "--proxy-egypt"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["workers"] == 4 and result["dry_run"] is True and result["browser"] == "none"
    assert len(factory.instances) == 2 and factory.instances[-1].closed
    with pytest.raises(SystemExit):
        country.main(args + ["--run"])
    assert len(factory.instances) == 2


def write_acknowledged_quarantine(path, plan):
    progress = country._new_progress(plan)
    progress.update(no_browser=True, workers=2, unknown_acknowledged=True)
    progress["rows"][0].update(
        state="unknown", attempts=1, phase="stopped", error_code="interrupted_unknown", connection="proxy_egypt",
    )
    path.write_text(json.dumps(progress), encoding="utf-8")


@pytest.mark.parametrize("limit,expected_status,expected_reason", [
    (2, "paused", "limit_reached"),
    (None, "completed", None),
])
def test_acknowledged_old_quarantine_does_not_pause_new_parallel_work(imported, tmp_path, monkeypatch, limit, expected_status, expected_reason):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    write_acknowledged_quarantine(path, plan)
    install_recovery(monkeypatch, factory)
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True, limit=limit)
    assert result["status"] == expected_status and result["pause_reason"] == expected_reason
    assert result["counts"]["unknown"] == 1
    assert result["counts"]["ready"] == (2 if limit else 7)
    progress = country.load_progress(path, plan)
    assert progress["unknown_acknowledged"] is True
    assert progress["rows"][0]["state"] == "unknown" and progress["rows"][0]["attempts"] == 1
    assert all(event[1] != 1 for event in factory.events if event[0] == "recover")


def test_new_unknown_still_pauses_when_an_old_quarantine_was_acknowledged(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    write_acknowledged_quarantine(path, plan)
    barrier = threading.Barrier(2, timeout=5)

    def hook(row, _proxy):
        assert row in {2, 3}
        barrier.wait()
        if row == 2:
            raise EOFError()

    install_recovery(monkeypatch, factory, hook)
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True)
    assert result["status"] == "attention_required" and result["pause_reason"] == "unknown_attempt"
    progress = country.load_progress(path, plan)
    assert progress["unknown_acknowledged"] is False
    assert states(path, plan)[1]["state"] == states(path, plan)[2]["state"] == "unknown"
    assert states(path, plan)[3]["state"] == "ready"
    assert all(states(path, plan)[row]["state"] == "pending" for row in range(4, 9))


def test_proxy_preflight_in_progress_checkpoint_is_reloadable(imported, tmp_path, monkeypatch):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    phase_persisted, reloaded = threading.Event(), threading.Event()
    original_atomic = country._atomic_json

    def atomic(destination, value):
        result = original_atomic(destination, value)
        if Path(destination) == path and value["rows"][0]["phase"] == "proxy_preflight":
            phase_persisted.set()
        return result

    def preflight():
        assert phase_persisted.wait(5)
        progress = country.load_progress(path, plan)
        assert progress["rows"][0]["state"] == "in_progress"
        assert progress["rows"][0]["phase"] == "proxy_preflight"
        assert progress["workers"] == 2 and progress["active_rows"] == [1] and progress["active_row"] == 1
        reloaded.set()

    monkeypatch.setattr(country, "_atomic_json", atomic)
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda _path: SyntheticProxy(factory, hook=preflight))
    install_recovery(monkeypatch, factory)
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True, proxy_egypt=True, limit=1)
    assert reloaded.is_set() and result["counts"]["ready"] == 1


@pytest.mark.parametrize("status", ["running", "paused"])
def test_restart_failure_hold_clears_stale_active_rows_without_replaying_intent(imported, tmp_path, monkeypatch, status):
    parent, plan, _source, factory = imported
    path = tmp_path / "progress.json"
    progress = country._new_progress(plan)
    progress.update(
        no_browser=True, workers=2, status=status, pause_reason="repeated_failures",
        failure_hold="repeated_failures", consecutive_failures=3,
        active_row=2, active_rows=[2],
    )
    progress["rows"][0].update(state="failed", attempts=1, phase="stopped", error_code="account_failed", connection="direct")
    progress["rows"][1].update(state="in_progress", attempts=1, phase="session_recovery", connection="direct")
    path.write_text(json.dumps(progress), encoding="utf-8")
    before = list(factory.events)
    install_recovery(monkeypatch, factory, lambda *_: pytest.fail("Failure hold replayed an interrupted account"))
    result = country.run_plan(parent, plan, path, workers=2, no_browser=True)
    assert result["pause_reason"] == "repeated_failures"
    assert result["active_row"] is None and result["active_rows"] == []
    assert factory.events == before
    assert states(path, plan)[2]["state"] == "in_progress" and states(path, plan)[2]["attempts"] == 1

