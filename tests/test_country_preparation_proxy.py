"""Country proxy preparation uses synthetic accounts and offline transports."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from anghami_session import capture, country_preparation as country
from anghami_session.errors import SessionError


PRIVATE_PASSWORD = "synthetic-country-proxy-account-password"
PRIVATE_PROXY_USER = "synthetic-country-proxy-user"
PRIVATE_PROXY_KEY = "synthetic-country-proxy-key"
PRIVATE_ERROR = "synthetic-proxy-error https://private.invalid/?sid=synthetic-session"
SAVED = {"synthetic_cookie": "synthetic-session"}


class SyntheticVault:
    def __init__(self, path, records, source_hash, events):
        self.path, self.records = Path(path), deepcopy(records)
        self.source_hash, self.events = source_hash, events
        self.ready, self.enrolled = set(), set()
        self._db = sqlite3.connect(":memory:")
        self._db.execute("CREATE TABLE accounts(source_row INTEGER PRIMARY KEY, email_key BLOB, record BLOB, state TEXT, session BLOB)")
        for row, record in records.items():
            identity = hashlib.sha256(record["email"].encode()).digest()
            self._db.execute("INSERT INTO accounts VALUES(?,?,?,?,?)", (row, identity, b"synthetic", "login_required", None))
        self._db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def metadata(self, name):
        assert name == "source_sha256"
        return self.source_hash

    def enrolled_test_rows(self):
        return frozenset(self.enrolled)

    def record(self, row):
        self.events.append(("credentials", row))
        return deepcopy(self.records[row])

    def session(self, row):
        self.events.append(("session", row))
        if row not in self.ready:
            raise SessionError("No synthetic session")
        return SAVED

    def select_test_candidates(self, *_args, **_options):
        pytest.fail("Country preparation used the general account selector")

    def attach(self, row, saved, **options):
        assert saved == SAVED
        self.events.append(("attach", row, dict(options)))
        self.ready.add(row)
        self._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=?", (b"synthetic", row))
        self._db.commit()
        return {"source_row": row, "verified": True}

    def enable_test_account(self, row):
        assert row in self.ready
        self.events.append(("enroll", row))
        self.enrolled.add(row)

    def test_like(self, *_args, **_options):
        pytest.fail("Preparation sent a like event")

    def test_play_record(self, *_args, **_options):
        pytest.fail("Preparation sent a play event")


class SyntheticProxy:
    username, auth_key = PRIVATE_PROXY_USER, PRIVATE_PROXY_KEY
    country = "EG"

    def __init__(self, events, *, failure=None, result=None, hook=None):
        self.events, self.failure, self.result, self.hook = events, failure, result, hook

    def summary(self):
        return {"provider": "PacketStream", "country": "EG", "sticky": True}

    def verify_country(self):
        self.events.append(("verify_egypt", self))
        if self.failure:
            raise self.failure
        if self.hook:
            self.hook()
        if self.result is not None:
            return self.result
        return {**self.summary(), "country_verified": True, "proxy_used": True, "http_status": 200, "proxy_connect_http_status": 200}


@pytest.fixture
def imported(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Test reached an unconfigured login"))
    monkeypatch.setattr(capture, "launch_browser", lambda **_: pytest.fail("Test started a real browser"))
    records = {row: {
        "country": "LB", "email": f"synthetic-proxy-account-{row}@example.invalid",
        "password": PRIVATE_PASSWORD,
    } for row in (1, 2, 3, 4)}
    raw = "\n".join(f"LB~{record['email']}~{PRIVATE_PASSWORD}" for record in records.values()).encode()
    source = tmp_path / "registered.txt"
    source.write_bytes(raw)
    events = []
    vault = SyntheticVault(tmp_path / "accounts.sqlite3", records, hashlib.sha256(raw).hexdigest(), events)
    yield vault, source, events
    vault._db.close()


def assert_safe(value, vault):
    encoded = json.dumps(value)
    for secret in (PRIVATE_PASSWORD, PRIVATE_PROXY_USER, PRIVATE_PROXY_KEY, PRIVATE_ERROR, "synthetic-session"):
        assert secret not in encoded
    assert "private.invalid" not in encoded
    for record in vault.records.values():
        assert record["email"] not in encoded


def install_capture(monkeypatch, events, *, failure=None):
    def capture_login(**options):
        events.append(("capture", dict(options)))
        if failure:
            raise failure
        return SAVED, []

    monkeypatch.setattr(capture, "capture_login", capture_login)


def install_profile_loader(monkeypatch, events, *, profiles=None, failure=None):
    loaded = []

    def load(path):
        events.append(("load_profile", Path(path)))
        if failure:
            raise failure
        profile = profiles[len(loaded)] if profiles is not None else SyntheticProxy(events)
        loaded.append(profile)
        return profile

    monkeypatch.setattr(country, "load_packetstream_proxy", load)
    return loaded


def checkpoint(path, plan):
    return country.load_progress(path, plan)


def test_fresh_saved_profile_is_verified_before_credentials_and_bound_to_capture_and_validation(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    loaded = install_profile_loader(monkeypatch, events)
    install_capture(monkeypatch, events)
    summary = country.run_plan(vault, plan, path, limit=2, proxy_egypt=True)
    assert len(loaded) == 2 and loaded[0] is not loaded[1]
    assert [event[1] for event in events if event[0] == "load_profile"] == [vault.path.parent / "packetstream.dpapi"] * 2
    captures = [event[1] for event in events if event[0] == "capture"]
    attachments = [event for event in events if event[0] == "attach"]
    assert len(captures) == len(attachments) == 2
    for row, proxy, options, attachment in zip((1, 2), loaded, captures, attachments):
        assert options["proxy"] is proxy and attachment[2]["proxy"] is proxy
        assert options["browser_backend"] == "cloakbrowser" and options["headless"] is True
        assert options["reduce_browser_data"] is True
        assert events.index(("verify_egypt", proxy)) < events.index(("credentials", row))
        assert attachment[1] == row
    assert summary["connection"] == "proxy_egypt"
    assert summary["proxy"]["provider"] == "PacketStream" and summary["proxy"]["country"] == "EG"
    assert summary["proxy"]["sticky"] is True
    assert summary["play_events_sent"] == summary["like_events_sent"] == 0
    rows = checkpoint(path, plan)["rows"]
    assert [(row["state"], row["connection"]) for row in rows] == [
        ("ready", "proxy_egypt"), ("ready", "proxy_egypt"), ("pending", None), ("pending", None),
    ]
    assert_safe(summary, vault)
    assert_safe(json.loads(path.read_text()), vault)


def test_no_browser_proxy_recovery_keeps_same_verified_profile_and_durable_intent(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    loaded = install_profile_loader(monkeypatch, events)

    def recover_legacy_session(record, *, proxy=None):
        assert proxy is loaded[0]
        assert record == vault.records[1]
        progress = checkpoint(path, plan)
        assert progress["active_row"] == 1 and progress["no_browser"] is True
        assert progress["rows"][0]["attempts"] == 1 and progress["rows"][0]["phase"] == "session_recovery"
        events.append(("recover", 1, proxy))
        return SAVED, {"preparation_method": "http", "server_verified": True}

    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=recover_legacy_session))
    summary = country.run_plan(vault, plan, path, limit=1, proxy_egypt=True, no_browser=True)
    assert len(loaded) == 1
    assert events.index(("verify_egypt", loaded[0])) < next(i for i, event in enumerate(events) if event[0] == "credentials")
    attachment = next(event for event in events if event[0] == "attach")
    assert attachment[1] == 1 and attachment[2]["proxy"] is loaded[0]
    assert summary["counts"]["ready"] == 1 and summary["counts"]["failed"] == 0
    assert summary["browser"] == "none" and summary["preparation_method"] == "http"
    assert summary["headless"] is summary["reduce_browser_data"] is summary["browser_required"] is False
    assert summary["play_events_sent"] == summary["like_events_sent"] == 0
    assert not any(event[0] == "capture" for event in events)
    assert_safe(summary, vault)
    assert_safe(json.loads(path.read_text()), vault)


def test_no_browser_proxy_preflight_failure_still_guards_pending_row_and_records_cause(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_profile_loader(monkeypatch, events, failure=SessionError("PacketStream proxy credentials are not configured. Save them before using the Egypt proxy."))
    monkeypatch.setitem(sys.modules, "anghami_session.session_recovery", SimpleNamespace(recover_legacy_session=lambda *_args, **_options: pytest.fail("Preflight failure reached recovery")))
    summary = country.run_plan(vault, plan, path, limit=1, proxy_egypt=True, no_browser=True)
    assert summary["pause_reason"] == "proxy_preflight_failed"
    assert summary["proxy_failure"] == {"source_row": 1, "stage": "profile_load", "code": "credentials_missing"}
    assert summary["no_browser"] is True and summary["browser"] == "none"
    assert [event[0] for event in events] == ["load_profile"]
    assert all(row["state"] == "pending" and row["attempts"] == 0 for row in checkpoint(path, plan)["rows"])


@pytest.mark.parametrize("failure", [SessionError(PRIVATE_ERROR), RuntimeError(PRIVATE_ERROR)])
def test_failed_egypt_preflight_never_reads_credentials_consumes_a_row_or_falls_back(imported, tmp_path, monkeypatch, failure):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    profile = SyntheticProxy(events, failure=failure)
    install_profile_loader(monkeypatch, events, profiles=[profile])
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Preflight failure reached login"))
    summary = country.run_plan(vault, plan, path, limit=1, proxy_egypt=True)
    assert summary["status"] == "paused" and summary["pause_reason"] == "proxy_preflight_failed"
    assert [event[0] for event in events] == ["load_profile", "verify_egypt"]
    progress = checkpoint(path, plan)
    assert progress["consecutive_failures"] == progress["infrastructure_failures"] == 0
    assert all(row["state"] == "pending" and row["attempts"] == 0 and row["connection"] is None for row in progress["rows"])
    assert not vault.ready and not vault.enrolled
    assert_safe(summary, vault)
    assert_safe(json.loads(path.read_text()), vault)


def test_missing_saved_profile_pauses_before_any_account_or_fallback(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_profile_loader(monkeypatch, events, failure=SessionError(PRIVATE_ERROR))
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Missing profile reached login"))
    summary = country.run_plan(vault, plan, path, proxy_egypt=True)
    assert summary["pause_reason"] == "proxy_preflight_failed"
    assert [event[0] for event in events] == ["load_profile"]
    assert all(row["attempts"] == 0 for row in checkpoint(path, plan)["rows"])
    assert_safe(summary, vault)


@pytest.mark.parametrize("result", [
    [], {}, {"country": "EG"},
    {"country": "US", "country_verified": True, "proxy_used": True},
    {"country": "eg", "country_verified": True, "proxy_used": True},
    {"country": "EG", "country_verified": 1, "proxy_used": True},
    {"country": "EG", "country_verified": True, "proxy_used": 1},
    {"country": "EG", "country_verified": "true", "proxy_used": True},
    {"country": "EG", "country_verified": True, "proxy_used": False},
])
def test_unverified_or_wrong_country_preflight_fails_closed_before_credentials(imported, tmp_path, monkeypatch, result):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    profile = SyntheticProxy(events, result=result)
    install_profile_loader(monkeypatch, events, profiles=[profile])
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Unverified route reached login"))
    path = tmp_path / "country-progress.json"
    summary = country.run_plan(vault, plan, path, proxy_egypt=True)
    assert summary["pause_reason"] == "proxy_preflight_failed"
    assert all(row["attempts"] == 0 for row in checkpoint(path, plan)["rows"])
    assert not any(event[0] == "credentials" for event in events)


def test_saved_profile_wrong_country_is_rejected_without_even_preflight_or_login(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    profile = SyntheticProxy(events)
    profile.country = "US"
    install_profile_loader(monkeypatch, events, profiles=[profile])
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Wrong-country profile reached login"))
    summary = country.run_plan(vault, plan, tmp_path / "country-progress.json", proxy_egypt=True)
    assert summary["pause_reason"] == "proxy_preflight_failed"
    assert [event[0] for event in events] == ["load_profile"]


def test_successful_preflight_retry_uses_same_pending_row_without_replaying_an_account(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_profile_loader(monkeypatch, events, profiles=[SyntheticProxy(events, failure=SessionError(PRIVATE_ERROR))])
    country.run_plan(vault, plan, path, limit=1, proxy_egypt=True)
    events.clear()
    loaded = install_profile_loader(monkeypatch, events)
    install_capture(monkeypatch, events)
    summary = country.run_plan(vault, plan, path, limit=1, proxy_egypt=True)
    assert len(loaded) == 1
    assert [event[1] for event in events if event[0] == "attach"] == [1]
    assert summary["counts"]["ready"] == 1 and summary["counts"]["failed"] == 0
    assert checkpoint(path, plan)["rows"][0]["attempts"] == 1


@pytest.mark.parametrize("change,expected_reason", [
    ("stop", "stop_requested"), ("ui", "ui_busy"),
    ("source", "source_changed"), ("identity", "scope_mismatch"),
])
def test_guards_are_rechecked_after_proxy_verification_before_durable_account_intent(imported, tmp_path, monkeypatch, change, expected_reason):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"

    def hook():
        if change == "stop":
            path.with_suffix(".stop").write_text('{"stop":true}')
        elif change == "ui":
            (tmp_path / "ui-last-job.json").write_text('{"status":"queued","id":"synthetic-ui-job"}')
        elif change == "source":
            source.write_bytes(source.read_bytes() + b"\n")
        else:
            vault._db.execute("UPDATE accounts SET email_key=? WHERE source_row=1", (b"synthetic-new-identity",))
            vault._db.commit()

    install_profile_loader(monkeypatch, events, profiles=[SyntheticProxy(events, hook=hook)])
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Changed guard reached login"))
    summary = country.run_plan(vault, plan, path, limit=1, proxy_egypt=True)
    assert summary["pause_reason"] == expected_reason
    assert all(row["attempts"] == 0 and row["state"] == "pending" for row in checkpoint(path, plan)["rows"])
    assert not any(event[0] == "credentials" for event in events)


def test_selected_row_validation_accepts_only_exact_bound_profile(imported):
    vault, _source, events = imported
    profile, other = SyntheticProxy(events), SyntheticProxy(events)
    selected = country.SelectedRowVault(vault, 1, country="LB", proxy=profile)
    for options in ({}, {"proxy": None}, {"proxy": other}):
        with pytest.raises(SessionError):
            selected.attach(1, SAVED, **options)
    assert events == []
    selected.attach(1, SAVED, proxy=profile)
    assert events[-1][2]["proxy"] is profile
    direct = country.SelectedRowVault(vault, 2, country="LB")
    with pytest.raises(SessionError):
        direct.attach(2, SAVED, proxy=profile)
    direct.attach(2, SAVED)
    assert events[-1] == ("attach", 2, {})


def test_direct_default_keeps_no_proxy_profile_and_force_direct_browser_runtime(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Direct mode loaded proxy credentials"))
    monkeypatch.setenv("HTTPS_PROXY", "https://synthetic-environment-proxy.invalid")
    monkeypatch.setenv("NO_PROXY", "synthetic.invalid")
    launches = []
    original = lambda **options: launches.append(options)
    monkeypatch.setattr(capture, "launch_browser", original)

    def capture_login(**options):
        assert options.get("proxy") is None
        assert "HTTPS_PROXY" not in os.environ and os.environ["NO_PROXY"] == "*"
        capture.launch_browser(headless=True, backend="cloakbrowser")
        return SAVED, []

    monkeypatch.setattr(capture, "capture_login", capture_login)
    path = tmp_path / "country-progress.json"
    summary = country.run_plan(vault, plan, path, limit=1)
    assert summary["connection"] == "direct" and "proxy" not in summary
    assert launches == [{"headless": True, "backend": "cloakbrowser", "direct": True}]
    assert capture.launch_browser is original
    assert os.environ["HTTPS_PROXY"] == "https://synthetic-environment-proxy.invalid"
    assert os.environ["NO_PROXY"] == "synthetic.invalid"
    attachment = next(event for event in events if event[0] == "attach")
    assert attachment[2].get("proxy") is None
    assert checkpoint(path, plan)["rows"][0]["connection"] == "direct"


def test_proxy_runtime_preserves_explicit_browser_proxy_routing_instead_of_force_direct(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    loaded = install_profile_loader(monkeypatch, events)
    launches = []
    original = lambda **options: launches.append(options)
    monkeypatch.setattr(capture, "launch_browser", original)

    def capture_login(**options):
        assert options["proxy"] is loaded[0]
        assert capture.launch_browser is original
        capture.launch_browser(headless=True, backend="cloakbrowser")
        return SAVED, []

    monkeypatch.setattr(capture, "capture_login", capture_login)
    country.run_plan(vault, plan, tmp_path / "country-progress.json", limit=1, proxy_egypt=True)
    assert launches == [{"headless": True, "backend": "cloakbrowser"}]
    assert capture.launch_browser is original


def test_switching_route_preserves_per_account_connection_history(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_capture(monkeypatch, events)
    country.run_plan(vault, plan, path, limit=1)
    install_profile_loader(monkeypatch, events)
    summary = country.run_plan(vault, plan, path, limit=1, proxy_egypt=True)
    rows = checkpoint(path, plan)["rows"]
    assert [(row["source_row"], row["connection"]) for row in rows] == [(1, "direct"), (2, "proxy_egypt"), (3, None), (4, None)]
    assert summary["connection"] == "proxy_egypt"
    assert_safe(summary, vault)


@pytest.mark.parametrize("fresh", [False, True])
def test_changing_proxy_mode_cannot_bypass_existing_failure_hold(imported, tmp_path, monkeypatch, fresh):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_capture(monkeypatch, events, failure=SessionError(PRIVATE_ERROR))
    failed = country.run_plan(vault, plan, path)
    assert failed["pause_reason"] == "repeated_failures"
    before = path.read_bytes()
    events.clear()
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Mode change bypassed the failure hold"))
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Mode change tried another account"))
    held = country.run_plan(vault, plan, path, proxy_egypt=True, fresh_plan=fresh)
    assert held["pause_reason"] == "repeated_failures" and held["connection"] == "direct"
    assert events == [] and path.read_bytes() == before
    assert held["counts"]["failed"] == 3 and held["counts"]["pending"] == 1


def test_explicit_review_allows_one_pending_proxy_probe_without_retrying_failed_accounts(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_capture(monkeypatch, events, failure=SessionError(PRIVATE_ERROR))
    country.run_plan(vault, plan, path)
    events.clear()
    loaded = install_profile_loader(monkeypatch, events)
    install_capture(monkeypatch, events)
    summary = country.run_plan(vault, plan, path, proxy_egypt=True, resume_after_review=True, limit=1)
    assert len(loaded) == 1
    assert [event[1] for event in events if event[0] == "attach"] == [4]
    assert summary["counts"]["failed"] == 3 and summary["counts"]["ready"] == 1
    assert [row["attempts"] for row in checkpoint(path, plan)["rows"]] == [1, 1, 1, 1]


def test_preflight_failure_preserves_existing_failure_counters_and_pending_row(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_capture(monkeypatch, events, failure=RuntimeError(PRIVATE_ERROR))
    country.run_plan(vault, plan, path, limit=1)
    before = checkpoint(path, plan)
    assert before["consecutive_failures"] == before["infrastructure_failures"] == 1
    events.clear()
    install_profile_loader(monkeypatch, events, profiles=[SyntheticProxy(events, failure=SessionError(PRIVATE_ERROR))])
    summary = country.run_plan(vault, plan, path, limit=1, proxy_egypt=True)
    progress = checkpoint(path, plan)
    assert summary["pause_reason"] == "proxy_preflight_failed"
    assert progress["consecutive_failures"] == progress["infrastructure_failures"] == 1
    assert progress["rows"][0] == before["rows"][0]
    assert progress["rows"][1]["attempts"] == 0 and progress["rows"][1]["state"] == "pending"
    assert [event[0] for event in events] == ["load_profile", "verify_egypt"]


def test_post_intent_proxy_failure_stops_immediately_without_consuming_remaining_accounts(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    loaded = install_profile_loader(monkeypatch, events)
    install_capture(monkeypatch, events, failure=SessionError("The login proxy country check failed. No browser was opened."))
    summary = country.run_plan(vault, plan, path, proxy_egypt=True)
    assert len(loaded) == 1 and summary["pause_reason"] == "proxy_preflight_failed"
    assert summary["counts"]["failed"] == 1 and summary["counts"]["pending"] == 3
    progress = checkpoint(path, plan)
    assert progress["rows"][0]["attempts"] == 1 and progress["rows"][0]["connection"] == "proxy_egypt"
    assert all(row["attempts"] == 0 for row in progress["rows"][1:])
    assert not any(event[0] in {"attach", "enroll"} for event in events)
    assert summary["recent_failures"][0]["connection"] == "proxy_egypt"
    assert summary["proxy_failure"] == {
        "source_row": 1, "stage": "preparation", "code": "country_check_failed",
    }
    assert progress["proxy_failure"] == summary["proxy_failure"]
    assert_safe(summary, vault)


@pytest.mark.parametrize("guard", ["unknown", "ui_busy"])
def test_existing_unknown_and_ui_guards_precede_profile_loading(imported, tmp_path, monkeypatch, guard):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    if guard == "unknown":
        progress = country._new_progress(plan)
        progress["rows"][0].update(state="in_progress", attempts=1, phase="login", connection="direct")
        path.write_text(json.dumps(progress))
    else:
        (tmp_path / "ui-last-job.json").write_text('{"status":"running","id":"synthetic-ui-job"}')
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Pending guard loaded credentials"))
    summary = country.run_plan(vault, plan, path, proxy_egypt=True)
    assert summary["pause_reason"] == ("unknown_attempt" if guard == "unknown" else "ui_busy")
    assert events == []


def test_legacy_connection_defaults_and_route_report_ignore_private_metadata(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_capture(monkeypatch, events)
    country.run_plan(vault, plan, path, limit=1)
    progress = json.loads(path.read_text())
    progress.pop("connection")
    progress["proxy"] = {"username": PRIVATE_PROXY_USER, "auth_key": PRIVATE_PROXY_KEY, "endpoint": PRIVATE_ERROR}
    for row in progress["rows"]:
        row.pop("connection")
        row["proxy_auth"] = PRIVATE_PROXY_KEY
    path.write_text(json.dumps(progress))
    loaded = checkpoint(path, plan)
    assert loaded["connection"] == "direct"
    assert [row["connection"] for row in loaded["rows"]] == ["direct", None, None, None]
    loaded["connection"] = "proxy_egypt"
    loaded["proxy"] = progress["proxy"]
    summary = country.summarize(loaded)
    assert summary["proxy"] == {
        "provider": "PacketStream", "country": "EG", "endpoint": country.PACKETSTREAM_ENDPOINT, "sticky": True,
    }
    assert_safe(summary, vault)
    loaded.pop("proxy")
    assert_safe(loaded, vault)


@pytest.mark.parametrize("target", ["job", "row"])
def test_unsafe_persisted_connection_rejected_before_profile_loading_or_rewrite(imported, tmp_path, monkeypatch, target):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    progress = country._new_progress(plan)
    if target == "job":
        progress["connection"] = PRIVATE_PROXY_KEY
    else:
        progress["rows"][0]["connection"] = PRIVATE_PROXY_KEY
    path.write_text(json.dumps(progress))
    before = path.read_bytes()
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Invalid checkpoint unlocked credentials"))
    with pytest.raises(SessionError) as error:
        country.run_plan(vault, plan, path, proxy_egypt=True)
    assert PRIVATE_PROXY_KEY not in str(error.value)
    assert events == [] and path.read_bytes() == before


@pytest.mark.parametrize("failure,hold", [
    ("The login browser could not close cleanly. Check its processes before retrying.", "browser_cleanup_failed"),
    ("CloakBrowser could not start because its license check failed. Check the key.", "browser_unavailable"),
])
@pytest.mark.parametrize("preflight", ["verify_error", "wrong_profile_country"])
def test_reviewed_preflight_failure_cannot_erase_prior_cleanup_or_license_hold(imported, tmp_path, monkeypatch, failure, hold, preflight):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_capture(monkeypatch, events, failure=SessionError(failure))
    first = country.run_plan(vault, plan, path)
    assert first["pause_reason"] == hold and first["consecutive_failures"] == 1
    events.clear()
    profile = SyntheticProxy(events, failure=SessionError(PRIVATE_ERROR))
    if preflight == "wrong_profile_country":
        profile.country = "US"
    install_profile_loader(monkeypatch, events, profiles=[profile])
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Reviewed bad preflight reached login"))
    reviewed = country.run_plan(vault, plan, path, proxy_egypt=True, resume_after_review=True, limit=1)
    assert reviewed["pause_reason"] == "proxy_preflight_failed" and reviewed["failure_hold"] == hold
    after = checkpoint(path, plan)
    assert after["failure_hold"] == hold and after["consecutive_failures"] == 1
    assert after["rows"][0]["state"] == "failed" and after["rows"][1]["attempts"] == 0
    events.clear()
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Unreviewed rerun unlocked a profile"))
    for options in ({}, {"fresh_plan": True}, {"proxy_egypt": True}):
        held = country.run_plan(vault, plan, path, **options)
        assert held["pause_reason"] == hold and held["failure_hold"] == hold
        assert events == []
    final = checkpoint(path, plan)
    assert [row["attempts"] for row in final["rows"]] == [1, 0, 0, 0]
    assert final["consecutive_failures"] == 1


@pytest.mark.parametrize("exception", [KeyboardInterrupt, EOFError])
@pytest.mark.parametrize("during", ["load", "verify"])
def test_cancelled_proxy_preflight_pauses_without_consuming_an_account(imported, tmp_path, monkeypatch, exception, during):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    error = exception(PRIVATE_ERROR)
    if during == "load":
        install_profile_loader(monkeypatch, events, failure=error)
    else:
        install_profile_loader(monkeypatch, events, profiles=[SyntheticProxy(events, failure=error)])
    summary = country.run_plan(vault, plan, path, proxy_egypt=True)
    assert summary["status"] == "paused" and summary["pause_reason"] == "stop_requested"
    progress = checkpoint(path, plan)
    assert all(row["state"] == "pending" and row["attempts"] == 0 for row in progress["rows"])
    assert progress["consecutive_failures"] == progress["infrastructure_failures"] == 0
    assert not any(event[0] == "credentials" for event in events)
    assert_safe(summary, vault)


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_proxy_mode_requires_exact_boolean_before_checkpoint_or_account_work(imported, tmp_path, monkeypatch, value):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Invalid flag loaded credentials"))
    with pytest.raises(SessionError):
        country.run_plan(vault, plan, path, proxy_egypt=value)
    assert events == [] and not path.exists()


@pytest.mark.parametrize("flags,expected_connection", [
    (["--proxy-egypt"], "proxy_egypt"),
    (["--dry-run", "--proxy-egypt", "--full-browser-data"], "proxy_egypt"),
    (["--status", "--proxy-egypt"], "direct"),
])
def test_offline_cli_preview_and_status_do_not_unlock_profiles_or_write_progress(imported, tmp_path, monkeypatch, capsys, flags, expected_connection):
    vault, source, events = imported
    monkeypatch.setattr(country, "AccountVault", lambda _: vault)
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Offline mode unlocked proxy credentials"))
    monkeypatch.setattr(capture, "capture_login", lambda **_: pytest.fail("Offline mode authenticated an account"))
    before = sorted(path.name for path in tmp_path.iterdir())
    assert country.main(["--country", "LB", "--source", str(source), "--vault", str(vault.path), *flags]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["connection"] == expected_connection
    assert events == [] and sorted(path.name for path in tmp_path.iterdir()) == before
    assert_safe(report, vault)


def test_status_uses_checkpoint_route_even_when_current_flag_selects_direct(imported, tmp_path, monkeypatch, capsys):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-LB-preparation-progress.json"
    install_profile_loader(monkeypatch, events)
    install_capture(monkeypatch, events)
    country.run_plan(vault, plan, path, limit=1, proxy_egypt=True)
    before = path.read_bytes()
    events.clear()
    monkeypatch.setattr(country, "AccountVault", lambda _: vault)
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Status unlocked credentials"))
    assert country.main(["--country", "LB", "--source", str(source), "--vault", str(vault.path), "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["connection"] == "proxy_egypt"
    assert events == [] and path.read_bytes() == before


def test_stop_with_proxy_flag_only_writes_stop_marker_without_opening_vault(imported, tmp_path, monkeypatch, capsys):
    vault, source, _events = imported
    monkeypatch.setattr(country, "AccountVault", lambda _: pytest.fail("Stop opened account vault"))
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Stop unlocked profile"))
    assert country.main(["--country", "LB", "--source", str(source), "--vault", str(vault.path), "--stop", "--proxy-egypt"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["stop_requested"] is True
    assert json.loads((tmp_path / "country-LB-preparation-progress.stop").read_text()) == {"stop": True}
    assert not (tmp_path / "country-LB-preparation-progress.json").exists()
    assert_safe(report, vault)


@pytest.mark.parametrize("stage,message,code", [
    ("profile_load", "PacketStream proxy credentials are not configured. Save them before using the Egypt proxy.", "credentials_missing"),
    ("profile_load", "The PacketStream proxy configuration could not be unlocked. Use the Windows user who saved it or configure it again.", "configuration_locked"),
    ("profile_load", "The PacketStream proxy configuration is invalid. Configure it again.", "configuration_invalid"),
    ("country_check", "PacketStream proxy authentication was rejected (HTTP 407). Check the configured credentials and available balance.", "authentication_rejected"),
    ("country_check", "The proxy country check did not return HTTP 200.", "country_http_not_ok"),
    ("country_check", "The proxy country check did not prove a successful proxy tunnel.", "proxy_tunnel_unconfirmed"),
    ("country_check", "The proxy country check did not verify Egypt.", "egypt_unverified"),
    ("country_check", "The proxy country check failed. No direct connection was attempted.", "country_check_failed"),
])
def test_fixed_proxy_diagnostic_survives_checkpoint_without_account_attempt(imported, tmp_path, monkeypatch, stage, message, code):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    failure = SessionError(message)
    if stage == "profile_load":
        install_profile_loader(monkeypatch, events, failure=failure)
    else:
        install_profile_loader(monkeypatch, events, profiles=[SyntheticProxy(events, failure=failure)])
    summary = country.run_plan(vault, plan, path, proxy_egypt=True)
    expected = {"source_row": 1, "stage": stage, "code": code}
    assert summary["pause_reason"] == "proxy_preflight_failed"
    assert summary["proxy_failure"] == checkpoint(path, plan)["proxy_failure"] == expected
    assert country.summarize(checkpoint(path, plan))["proxy_failure"] == expected
    assert all(row["state"] == "pending" and row["attempts"] == 0 for row in checkpoint(path, plan)["rows"])
    assert not any(event[0] == "credentials" for event in events)
    assert_safe(json.loads(path.read_text()), vault)


@pytest.mark.parametrize("wrong_profile", [False, True])
def test_unverified_profile_and_route_get_fixed_diagnostics(imported, tmp_path, monkeypatch, wrong_profile):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    profile = SyntheticProxy(events, result={"country": "US", "country_verified": True, "proxy_used": True})
    if wrong_profile:
        profile.country = "US"
    install_profile_loader(monkeypatch, events, profiles=[profile])
    path = tmp_path / "country-progress.json"
    summary = country.run_plan(vault, plan, path, proxy_egypt=True)
    assert summary["proxy_failure"] == {
        "source_row": 1, "stage": "profile_country" if wrong_profile else "country_check",
        "code": "profile_country_mismatch" if wrong_profile else "route_unverified",
    }
    assert all(row["attempts"] == 0 for row in checkpoint(path, plan)["rows"])
    assert not any(event[0] == "credentials" for event in events)


@pytest.mark.parametrize("failure", [
    RuntimeError(PRIVATE_ERROR), SessionError(PRIVATE_ERROR),
    SessionError("The proxy country check did not verify Egypt. " + PRIVATE_ERROR),
])
def test_unknown_proxy_diagnostic_text_never_enters_checkpoint_or_summary(imported, tmp_path, monkeypatch, failure):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_profile_loader(monkeypatch, events, profiles=[SyntheticProxy(events, failure=failure)])
    summary = country.run_plan(vault, plan, path, proxy_egypt=True)
    assert summary["proxy_failure"] == {"source_row": 1, "stage": "country_check", "code": "country_check_failed"}
    assert_safe(summary, vault)
    assert_safe(json.loads(path.read_text()), vault)


def test_proxy_diagnostic_never_reads_exception_response_or_profile_metadata(imported, tmp_path, monkeypatch):
    vault, source, events = imported

    class OpaqueError(SessionError):
        @property
        def response(self):
            pytest.fail("Diagnostic inspected a transport response")

        @property
        def diagnostics(self):
            pytest.fail("Diagnostic inspected arbitrary exception attributes")

        def __str__(self):
            raise ValueError(PRIVATE_ERROR)

    class OpaqueProfile(SyntheticProxy):
        def summary(self):
            pytest.fail("Diagnostic inspected arbitrary profile metadata")

    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_profile_loader(monkeypatch, events, profiles=[OpaqueProfile(events, failure=OpaqueError())])
    summary = country.run_plan(vault, plan, path, proxy_egypt=True)
    assert summary["proxy_failure"]["code"] == "country_check_failed"
    assert_safe(summary, vault)
    assert_safe(json.loads(path.read_text()), vault)


def test_proxy_diagnostic_points_to_next_pending_row_after_prior_successes(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    profiles = [SyntheticProxy(events) for _ in range(3)]
    profiles.append(SyntheticProxy(events, failure=SessionError("The proxy country check did not verify Egypt.")))
    install_profile_loader(monkeypatch, events, profiles=profiles)
    install_capture(monkeypatch, events)
    summary = country.run_plan(vault, plan, path, proxy_egypt=True)
    progress = checkpoint(path, plan)
    assert summary["proxy_failure"] == {"source_row": 4, "stage": "country_check", "code": "egypt_unverified"}
    assert summary["counts"]["ready"] == 3 and summary["counts"]["failed"] == 0
    assert progress["consecutive_failures"] == progress["infrastructure_failures"] == 0
    assert progress["rows"][-1]["state"] == "pending" and progress["rows"][-1]["attempts"] == 0
    assert not any(event[:2] == ("credentials", 4) for event in events)


@pytest.mark.parametrize("use_proxy", [False, True])
def test_successful_retry_or_direct_run_clears_diagnostic_and_uses_same_pending_row(imported, tmp_path, monkeypatch, use_proxy):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    install_profile_loader(monkeypatch, events, profiles=[SyntheticProxy(events, failure=SessionError(PRIVATE_ERROR))])
    failed = country.run_plan(vault, plan, path, proxy_egypt=True)
    assert failed["proxy_failure"] is not None
    events.clear()
    if use_proxy:
        install_profile_loader(monkeypatch, events)
    else:
        monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Direct loaded a profile"))
    install_capture(monkeypatch, events)
    summary = country.run_plan(vault, plan, path, proxy_egypt=use_proxy, limit=1)
    assert summary["proxy_failure"] is None and checkpoint(path, plan)["proxy_failure"] is None
    assert [event[1] for event in events if event[0] == "attach"] == [1]
    assert summary["counts"]["ready"] == 1 and summary["counts"]["failed"] == 0


def test_legacy_checkpoint_without_proxy_diagnostic_remains_offline_and_compatible(imported, tmp_path, monkeypatch):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    progress = country._new_progress(plan)
    progress.pop("proxy_failure")
    path.write_text(json.dumps(progress))
    before = path.read_bytes()
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Status loaded a profile"))
    loaded = checkpoint(path, plan)
    assert loaded["proxy_failure"] is country.summarize(loaded)["proxy_failure"] is None
    assert path.read_bytes() == before and events == []


def test_loaded_proxy_diagnostic_discards_private_fields_and_reconstructs_summary(imported, tmp_path):
    vault, source, _events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    expected = {"source_row": 1, "stage": "country_check", "code": "egypt_unverified"}
    progress = country._new_progress(plan)
    progress["proxy_failure"] = {**expected, "message": PRIVATE_ERROR, "username": PRIVATE_PROXY_USER, "response": {"cookie": "synthetic-session"}}
    path.write_text(json.dumps(progress))
    loaded = checkpoint(path, plan)
    assert loaded["proxy_failure"] == expected
    assert_safe(loaded, vault)
    loaded["proxy_failure"]["auth_key"] = PRIVATE_PROXY_KEY
    summary = country.summarize(loaded)
    assert summary["proxy_failure"] == expected
    assert_safe(summary, vault)


@pytest.mark.parametrize("value", [
    [], "private-diagnostic", {},
    {"source_row": True, "stage": "country_check", "code": "egypt_unverified"},
    {"source_row": "1", "stage": "country_check", "code": "egypt_unverified"},
    {"source_row": 999, "stage": "country_check", "code": "egypt_unverified"},
    {"source_row": 1, "stage": PRIVATE_PROXY_KEY, "code": "egypt_unverified"},
    {"source_row": 1, "stage": [], "code": "egypt_unverified"},
    {"source_row": 1, "stage": "country_check", "code": PRIVATE_PROXY_KEY},
    {"source_row": 1, "stage": "country_check", "code": []},
])
def test_malformed_proxy_diagnostic_rejected_without_exposure_rewrite_or_work(imported, tmp_path, monkeypatch, value):
    vault, source, events = imported
    plan = country.build_plan(vault, source)
    path = tmp_path / "country-progress.json"
    progress = country._new_progress(plan)
    progress["proxy_failure"] = value
    path.write_text(json.dumps(progress))
    before = path.read_bytes()
    monkeypatch.setattr(country, "load_packetstream_proxy", lambda *_: pytest.fail("Malformed checkpoint loaded credentials"))
    with pytest.raises(SessionError) as error:
        country.run_plan(vault, plan, path, proxy_egypt=True)
    assert PRIVATE_PROXY_KEY not in str(error.value)
    assert path.read_bytes() == before and events == []
