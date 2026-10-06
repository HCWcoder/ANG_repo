"""Offline per-account routing and UI pool-selection regression checks."""

import json
from pathlib import Path

import pytest

from anghami_session import preparation, session_recovery, ui_jobs
from anghami_session.errors import SessionError
from anghami_session.proxy import PacketStreamProxy
from anghami_session.proxy_pool import StickyProxyPool


class Vault:
    def __init__(self, path, *, saved=False, rejected=None):
        self.path, self.saved, self.rejected = Path(path), saved, rejected
        self.events = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def select_test_candidates(self, count, *, start_row=1):
        return list(range(start_row, start_row + count))

    def session(self, row):
        if not self.saved:
            raise SessionError("Synthetic missing session")
        return {"synthetic": "session"}

    def record(self, row):
        return {"source_row": row}

    def attach(self, row, saved, *, proxy):
        self.events.append(("attach", row, proxy))
        if row == self.rejected:
            raise SessionError("Synthetic rejected session")

    def enable_test_account(self, row):
        self.events.append(("ready", row))


def pool():
    return StickyProxyPool(tuple(PacketStreamProxy.from_route(
        "synthetic", "testPassword", label, "http://proxy.packetstream.io:31112",
    ) for label in ("RouteA", "RouteB")))


@pytest.mark.parametrize("saved", [False, True])
def test_each_row_keeps_one_selected_route_through_recovery_and_validation(tmp_path, monkeypatch, saved):
    routes = pool()
    vault = Vault(tmp_path / "accounts.sqlite3", saved=saved)
    selected = []

    def select(row):
        selected.append(row)
        return routes.proxy_for_index(len(selected) - 1)

    def recover(record, *, proxy):
        vault.events.append(("recover", record["source_row"], proxy))
        return {"synthetic": "session"}, {"preparation_method": "http"}

    monkeypatch.setattr(session_recovery, "recover_legacy_session", recover)
    report = preparation.prepare_test_accounts(vault, count=3, no_browser=True, proxy_factory=select)
    assert report["passed"] and report["prepared_rows"] == [1, 2, 3]
    assert selected == [1, 2, 3]
    for row in selected:
        expected = routes.proxy_for_index(row - 1)
        assert ("attach", row, expected) in vault.events
        if not saved:
            assert ("recover", row, expected) in vault.events
    assert report["play_events_sent"] == report["like_events_sent"] == 0
    assert "testPassword" not in json.dumps(report)


def test_rejected_session_is_never_enrolled_and_later_routes_are_not_consumed(tmp_path):
    vault = Vault(tmp_path / "accounts.sqlite3", saved=True, rejected=2)
    selected = []
    routes = pool()

    def select(row):
        selected.append(row)
        return routes.proxy_for_index(row - 1)

    with pytest.raises(SessionError):
        preparation.prepare_test_accounts(vault, count=3, no_browser=True, proxy_factory=select)
    assert selected == [1, 2]
    assert ("ready", 1) in vault.events
    assert ("ready", 2) not in vault.events
    assert ("ready", 3) not in vault.events


def test_preview_does_not_select_or_unlock_a_route(tmp_path):
    def forbidden(_row):
        pytest.fail("Preview must remain offline")

    report = preparation.prepare_test_accounts(Vault(tmp_path / "a.sqlite3"), count=2, no_browser=True, proxy_factory=forbidden, dry_run=True)
    assert report["attempted_accounts"] == 0 and report["connection"] == "proxy_egypt"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kwargs", [
    {"proxy_factory": 123, "no_browser": True},
    {"proxy_factory": lambda row: None},
    {"proxy_factory": lambda row: None, "no_browser": True, "proxy": object()},
])
def test_invalid_route_factory_cannot_select_accounts(tmp_path, kwargs):
    with pytest.raises(SessionError):
        preparation.prepare_test_accounts(Vault(tmp_path / "a.sqlite3"), count=1, **kwargs)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("action", ["play", "like", "login", "check", "song", "proxy-check"])
def test_ui_cannot_apply_pool_to_other_actions(action):
    with pytest.raises(ui_jobs.JobValidationError):
        ui_jobs._validate({"action": action, "rows": [1], "proxy_sticky_pool": True, "no_browser": True})


@pytest.mark.parametrize("value", [None, 1, 0, "true", [], {}])
def test_ui_pool_choice_requires_boolean(value):
    with pytest.raises(ui_jobs.JobValidationError):
        ui_jobs._validate({"action": "prepare", "no_browser": True, "proxy_sticky_pool": value})


def test_ui_pool_choice_requires_http_preparation():
    with pytest.raises(ui_jobs.JobValidationError):
        ui_jobs._validate({"action": "prepare", "proxy_sticky_pool": True})


def test_ui_pool_cursor_persists_and_wraps_between_jobs(tmp_path):
    routes, vaults, loads = pool(), [], []

    def vault_factory(path):
        vault = Vault(path, saved=True)
        vaults.append(vault)
        return vault

    def pool_loader(path):
        loads.append(path)
        return routes

    def forbidden_base_proxy(path):
        pytest.fail("Pool mode must use the imported credentials, not the base proxy")

    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", vault_factory=vault_factory, pool_loader=pool_loader, proxy_loader=forbidden_base_proxy)
    for count in [1, 2]:
        manager.submit({"action": "prepare", "count": count, "no_browser": True, "proxy_sticky_pool": True})
        manager._thread.join(3)
        assert manager.snapshot()["status"] == "succeeded"
    assert [event[2] for vault in vaults for event in vault.events if event[0] == "attach"] == [routes.proxy_for_index(i) for i in range(3)]
    cursor = json.loads((tmp_path / "ui-sticky-pool-position.json").read_text())
    assert cursor == {"fingerprint": routes.fingerprint(), "cursor": 3}
    assert len(loads) == 2
    assert "testPassword" not in (tmp_path / "ui-last-job.json").read_text()


def test_ui_preview_does_not_unlock_pool(tmp_path):
    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", vault_factory=Vault, pool_loader=lambda path: pytest.fail("Preview unlocked a pool"))
    manager.submit({"action": "preview", "count": 2, "no_browser": True, "proxy_sticky_pool": True})
    manager._thread.join(3)
    assert manager.snapshot()["status"] == "succeeded"
    assert not (tmp_path / "ui-sticky-pool-position.json").exists()


@pytest.mark.parametrize("cursor", [
    {"fingerprint": "other", "cursor": 2},
    {"fingerprint": pool().fingerprint(), "cursor": -1},
    {"fingerprint": pool().fingerprint(), "cursor": True},
])
def test_invalid_ui_pool_cursor_stops_before_account_attempt(tmp_path, cursor):
    (tmp_path / "ui-sticky-pool-position.json").write_text(json.dumps(cursor))
    vault = Vault(tmp_path / "a.sqlite3", saved=True)
    manager = ui_jobs.JobManager(vault.path, vault_factory=lambda _: vault, pool_loader=lambda _: pool())
    manager.submit({"action": "prepare", "count": 1, "no_browser": True, "proxy_sticky_pool": True})
    manager._thread.join(3)
    assert manager.snapshot()["status"] == "failed"
    assert vault.events == []
