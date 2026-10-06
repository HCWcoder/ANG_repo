"""Preparation route import is local, protected, and isolated from test routes."""

import json
from pathlib import Path

import pytest

from anghami_session import proxy_pool, ui_jobs, ui_server
from test_preparation_pool_ui import Vault, pool
from test_proxy_pool import protected_store
from test_ui_server import request
from test_ui_test_proxy import ROUTE, assert_safe, route_number, test_console


def test_preparation_routes_endpoint_saves_full_list_without_touching_other_routes(test_console, protected_store):
    folder = test_console.folder
    general, test_routes = folder / "packetstream.dpapi", folder / "packetstream-test-route.dpapi"
    general.write_bytes(b"synthetic-existing-general-store")
    test_routes.write_bytes(b"synthetic-existing-test-store")
    before = general.read_bytes(), test_routes.read_bytes()
    text = "\n".join([route_number(3), route_number(1), route_number(3), route_number(2)])
    status, _headers, body = request(test_console, "POST", "/api/preparation-proxy", body={"routes": text})
    summary = json.loads(body)
    assert status == 200 and summary["configured"] is True and summary["pool_size"] == 3
    assert protected_store["saved"][0][0] == folder / "packetstream-sticky-pool.dpapi"
    assert (general.read_bytes(), test_routes.read_bytes()) == before
    saved = proxy_pool.StickyProxyPool.load(folder / "packetstream-sticky-pool.dpapi")
    assert list(saved._proxies) == proxy_pool.parse_lines(text)
    status, _headers, body = request(test_console, "GET", "/api/state")
    state = json.loads(body)
    assert status == 200 and state["sticky_pool"]["configured"] is True and state["sticky_pool"]["pool_size"] == 3
    assert_safe(summary)
    assert_safe(state)


def test_preparation_routes_endpoint_accepts_us_sticky_link_and_reports_country(test_console, protected_store):
    route = ROUTE.replace("_country-EG", "_country-US")
    status, _headers, body = request(
        test_console, "POST", "/api/preparation-proxy", body={"routes": route},
    )
    summary = json.loads(body)
    assert status == 200 and summary["configured"] is True
    assert summary["country"] == "US" and summary["pool_size"] == 1
    saved = proxy_pool.StickyProxyPool.load(
        test_console.folder / "packetstream-sticky-pool.dpapi",
    )
    assert saved.summary()["country"] == "US"
    assert_safe(summary)


@pytest.mark.parametrize("payload", [None, [], {}, {"routes": None}, {"routes": True},
    {"route": ROUTE}, {"routes": ROUTE, "extra": "synthetic"},
    {"routes": ""}, {"routes": ROUTE + "\ninvalid-private-line"},
    {"routes": ROUTE.replace("_country-EG", "_country-LB")}])
def test_invalid_preparation_import_keeps_saved_pool(test_console, protected_store, payload):
    path = test_console.folder / "packetstream-sticky-pool.dpapi"
    proxy_pool.save_pool(proxy_pool.parse_lines(ROUTE), path)
    before, count = path.read_bytes(), len(protected_store["saved"])
    status, _headers, body = request(test_console, "POST", "/api/preparation-proxy", body=payload if payload is not None else b"null",
                                    headers={"Content-Type": "application/json"})
    assert status == 400 and path.read_bytes() == before and len(protected_store["saved"]) == count
    assert_safe(json.loads(body))


@pytest.mark.parametrize("status", ["queued", "running"])
def test_preparation_pool_cannot_be_replaced_during_active_job(test_console, protected_store, status):
    test_console.service.manager.current = {"status": status}
    code, _headers, body = request(test_console, "POST", "/api/preparation-proxy", body={"routes": ROUTE})
    assert code == 409 and protected_store["saved"] == []
    assert_safe(json.loads(body))


@pytest.mark.parametrize("headers", [{"Origin": "https://example.invalid"}, {"X-App-Token": "wrong-token"}])
def test_preparation_import_enforces_existing_local_request_controls(test_console, protected_store, headers):
    code, _headers, _body = request(test_console, "POST", "/api/preparation-proxy", body={"routes": ROUTE}, headers=headers)
    assert code == 403 and protected_store["saved"] == []


def test_large_paste_has_its_own_body_limit_and_still_saves_all_routes(test_console, protected_store):
    routes = "\n".join(route_number(i) for i in range(5000))
    routes += "\n" + " " * max(0, ui_server.MAX_BODY + 100 - len(routes.encode()))
    size = len(json.dumps({"routes": routes}).encode())
    assert ui_server.MAX_BODY < size < ui_server.MAX_PREPARATION_PROXY_BODY
    status, _headers, body = request(test_console, "POST", "/api/preparation-proxy", body={"routes": routes})
    assert status == 200 and json.loads(body)["pool_size"] == 5000
    assert_safe(json.loads(body))


def test_oversized_preparation_body_rejected_before_saving(test_console, protected_store):
    code, _headers, body = request(test_console, "POST", "/api/preparation-proxy", body={
        "routes": "x" * (ui_server.MAX_PREPARATION_PROXY_BODY + 1)})
    assert code == 413 and protected_store["saved"] == []
    assert_safe(json.loads(body))


def test_http_storage_failure_keeps_previous_pool_and_does_not_expose_error(test_console, protected_store, monkeypatch):
    path = test_console.folder / "packetstream-sticky-pool.dpapi"
    proxy_pool.save_pool(proxy_pool.parse_lines(ROUTE), path)
    before = path.read_bytes()
    monkeypatch.setattr(proxy_pool.store, "save_protected_bytes", lambda *_: (_ for _ in ()).throw(
        OSError("synthetic private storage exception")))
    code, _headers, body = request(test_console, "POST", "/api/preparation-proxy", body={"routes": route_number(2)})
    assert code == 400 and path.read_bytes() == before
    assert b"synthetic private" not in body
    assert_safe(json.loads(body))


def test_saved_replacement_pool_starts_at_first_route_and_keeps_position_for_next_job(tmp_path):
    first = pool()
    second = proxy_pool.StickyProxyPool(tuple(proxy_pool.parse_lines(
        "\n".join(route_number(number) for number in range(3)))))
    current, instances = [first], []
    def factory(path):
        selected = Vault(path, saved=True)
        instances.append(selected)
        return selected
    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", vault_factory=factory, pool_loader=lambda _: current[0])
    for routes in [first, second, second]:
        current[0] = routes
        manager.submit({"action": "prepare", "count": 1, "no_browser": True, "proxy_sticky_pool": True})
        manager._thread.join(3)
        assert not manager._thread.is_alive() and manager.snapshot()["status"] == "succeeded"
    used = [event[2] for vault in instances for event in vault.events if event[0] == "attach"]
    assert used == [first.proxy_for_index(0), second.proxy_for_index(0), second.proxy_for_index(1)]
    cursor = json.loads((tmp_path / "ui-sticky-pool-position.json").read_text())
    assert cursor == {"fingerprint": second.fingerprint(), "cursor": 2}


def test_remove_preparation_pool_only_removes_its_fixed_file_and_state_is_unconfigured(test_console, protected_store, monkeypatch):
    folder = test_console.folder
    path = folder / "packetstream-sticky-pool.dpapi"
    proxy_pool.save_pool(proxy_pool.parse_lines(ROUTE), path)
    others = [folder / "packetstream-test-route.dpapi", folder / "packetstream.dpapi", folder / "ui-sticky-pool-position.json"]
    for other in others:
        other.write_bytes(b"synthetic-unchanged-store")
    before = [other.read_bytes() for other in others]
    original = proxy_pool.store.load_protected_bytes
    def load_existing(path):
        if not Path(path).exists():
            raise FileNotFoundError
        return original(path)
    monkeypatch.setattr(proxy_pool.store, "load_protected_bytes", load_existing)
    for _ in range(2):
        status, _headers, body = request(test_console, "POST", "/api/preparation-proxy/remove", body={})
        assert status == 200 and json.loads(body) == {"configured": False, "pool_size": 0}
        assert not path.exists() and [other.read_bytes() for other in others] == before
    status, _headers, body = request(test_console, "GET", "/api/state")
    assert status == 200 and json.loads(body)["sticky_pool"] == {"configured": False, "pool_size": 0}


@pytest.mark.parametrize("status", ["queued", "running"])
def test_remove_preparation_pool_rejects_active_job(test_console, protected_store, status):
    path = test_console.folder / "packetstream-sticky-pool.dpapi"
    proxy_pool.save_pool(proxy_pool.parse_lines(ROUTE), path)
    before = path.read_bytes()
    test_console.service.manager.current = {"status": status}
    code, _headers, body = request(test_console, "POST", "/api/preparation-proxy/remove", body={})
    assert code == 409 and path.read_bytes() == before
    assert_safe(json.loads(body))


@pytest.mark.parametrize("payload", [{"path": "packetstream-test-route.dpapi"}, {"routes": ROUTE}, []])
def test_remove_payload_cannot_choose_other_files(test_console, protected_store, payload):
    path = test_console.folder / "packetstream-sticky-pool.dpapi"
    proxy_pool.save_pool(proxy_pool.parse_lines(ROUTE), path)
    before = path.read_bytes()
    code, _headers, body = request(test_console, "POST", "/api/preparation-proxy/remove", body=payload)
    assert code == 400 and path.read_bytes() == before
    assert_safe(json.loads(body))


def test_remove_preparation_pool_requires_local_request_token(test_console, protected_store):
    path = test_console.folder / "packetstream-sticky-pool.dpapi"
    proxy_pool.save_pool(proxy_pool.parse_lines(ROUTE), path)
    before = path.read_bytes()
    code, _headers, _body = request(test_console, "POST", "/api/preparation-proxy/remove", body={}, token=False)
    assert code == 403 and path.read_bytes() == before


def test_remove_storage_failure_preserves_saved_pool_and_uses_fixed_error(test_console, protected_store, monkeypatch):
    path = test_console.folder / "packetstream-sticky-pool.dpapi"
    proxy_pool.save_pool(proxy_pool.parse_lines(ROUTE), path)
    before = path.read_bytes()
    original = Path.unlink
    def unlink(selected, *args, **kwargs):
        if selected == path:
            raise OSError("synthetic-private-delete-error")
        return original(selected, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    code, _headers, body = request(test_console, "POST", "/api/preparation-proxy/remove", body={})
    assert code == 400 and path.read_bytes() == before and b"synthetic-private" not in body
