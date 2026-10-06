"""Workbench sticky route pools are saved and exercised only with synthetic data."""

import json
from pathlib import Path
import threading

import pytest

from anghami_session import proxy, proxy_pool, ui_jobs, ui_server, proxy_test_route
from anghami_session.errors import SessionError
from test_proxy_pool import protected_store
from test_ui_jobs import FakeVault, finish
from test_ui_server import FakeManager, request


PRIVATE_USER = "synthetic-workbench-user"
PRIVATE_KEY = "synthetic-workbench-key"
PRIVATE_LABEL = "syntheticworkbenchsession"
ROUTE = f"{PRIVATE_USER}:{PRIVATE_KEY}_country-EG_session-{PRIVATE_LABEL}:proxy.packetstream.io:31112"


def assert_safe(value):
    raw = json.dumps(value)
    for secret in (PRIVATE_USER, PRIVATE_KEY, PRIVATE_LABEL, "_session-", "proxy_auth", "example.invalid"):
        assert secret not in raw


def route_number(number):
    return ROUTE.replace(PRIVATE_LABEL, PRIVATE_LABEL + str(number))


def synthetic_pool(size):
    return proxy_pool.StickyProxyPool(tuple(proxy_pool.parse_lines("\n".join(route_number(number) for number in range(size)))))


def test_multiline_test_route_store_preserves_order_and_deduplicates_only_exact_routes(tmp_path, protected_store):
    path = tmp_path / "packetstream-test-route.dpapi"
    routes = "\n\n" + route_number(3) + "\n" + route_number(1) + "\n" + route_number(3) + "\n" + route_number(2) + "\n"
    summary = proxy_test_route.save_test_route(routes, path)
    pool = proxy_test_route.load_test_pool(path)
    expected = proxy_pool.parse_lines(routes)
    assert summary["pool_size"] == len(pool) == 3
    assert [pool.proxy_for_index(i).transport_options() for i in range(6)] == [item.transport_options() for item in expected] * 2
    assert len(protected_store["saved"]) == 1 and PRIVATE_KEY.encode() not in path.read_bytes()
    assert_safe(summary)


@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_one_bad_pool_line_rejects_the_entire_replacement_without_a_partial_save(tmp_path, protected_store, position):
    path = tmp_path / "packetstream-test-route.dpapi"
    proxy_test_route.save_test_route(ROUTE, path)
    before, count = path.read_bytes(), len(protected_store["saved"])
    lines = [route_number(0), route_number(1)]
    lines.insert({"first": 0, "middle": 1, "last": 2}[position], route_number(2).replace("_country-EG", "_country-LB"))
    with pytest.raises(SessionError) as error:
        proxy_test_route.save_test_route("\n".join(lines), path)
    assert path.read_bytes() == before and len(protected_store["saved"]) == count
    assert PRIVATE_KEY not in str(error.value) and PRIVATE_LABEL not in str(error.value)


def test_workbench_route_pool_limit_counts_raw_nonblank_routes_before_dedup(tmp_path, protected_store):
    path = tmp_path / "packetstream-test-route.dpapi"
    short = "u:k_country-EG_session-r:proxy.packetstream.io:31112"
    summary = proxy_test_route.save_test_route("\n".join([short] * 10000), path)
    assert summary["pool_size"] == 1 and len(proxy_test_route.load_test_pool(path)) == 1
    before, count = path.read_bytes(), len(protected_store["saved"])
    with pytest.raises(SessionError):
        proxy_test_route.save_test_route("\n".join([short] * 10001), path)
    assert path.read_bytes() == before and len(protected_store["saved"]) == count


def test_workbench_route_pool_accepts_and_loads_ten_thousand_distinct_routes(tmp_path, protected_store):
    path = tmp_path / "packetstream-test-route.dpapi"
    text = "\n".join(f"u:k_country-EG_session-r{number}:proxy.packetstream.io:31112" for number in range(10000))
    assert len(text.encode("utf-8")) < 1024 * 1024
    assert proxy_test_route.save_test_route(text, path)["pool_size"] == 10000
    pool = proxy_test_route.load_test_pool(path)
    assert len(pool) == 10000
    assert pool.proxy_for_index(0).transport_options()["proxy_auth"][1].endswith("_session-r0")
    assert pool.proxy_for_index(9999).transport_options()["proxy_auth"][1].endswith("_session-r9999")
    assert pool.proxy_for_index(10000) is pool.proxy_for_index(0)


def test_workbench_pool_loader_rejects_oversized_protected_pool_without_fallback(tmp_path, protected_store):
    path = tmp_path / "packetstream-test-route.dpapi"
    proxies = [proxy.PacketStreamProxy.from_route("u", "k", f"r{number}", "http://proxy.packetstream.io:31112")
               for number in range(10001)]
    proxy_pool.save_pool(proxies, path)
    with pytest.raises(SessionError):
        proxy_test_route.load_test_pool(path)


def test_workbench_route_pool_limit_measures_utf8_bytes_and_accepts_exact_boundary(tmp_path, protected_store):
    path = tmp_path / "packetstream-test-route.dpapi"
    maximum = 1024 * 1024
    valid = ROUTE + "\n" + " " * (maximum - len(ROUTE.encode("utf-8")) - 1)
    assert len(valid.encode("utf-8")) == maximum
    assert proxy_test_route.save_test_route(valid, path)["pool_size"] == 1
    before, count = path.read_bytes(), len(protected_store["saved"])
    invalid = ROUTE + "\n" + "\u2003" * (maximum // 3)
    assert len(invalid) < maximum < len(invalid.encode("utf-8"))
    with pytest.raises(SessionError):
        proxy_test_route.save_test_route(invalid, path)
    assert path.read_bytes() == before and len(protected_store["saved"]) == count


@pytest.mark.parametrize("route,endpoint", [
    (ROUTE, "http://proxy.packetstream.io:31112"),
    ("http://" + ROUTE.replace(":proxy.packetstream.io", "@proxy.packetstream.io"), "http://proxy.packetstream.io:31112"),
    ("https://" + ROUTE.replace(":31112", ":31111"), "https://proxy.packetstream.io:31111"),
])
def test_saved_test_route_crosses_only_protected_boundary_and_loads_exact_binding(tmp_path, protected_store, route, endpoint):
    path = tmp_path / "packetstream-test-route.dpapi"
    result = proxy_test_route.save_test_route(route, path)
    assert result == {"provider": "PacketStream", "country": "EG", "endpoint": endpoint, "sticky": True, "pool_size": 1}
    assert len(protected_store["saved"]) == 1
    assert PRIVATE_KEY.encode() not in path.read_bytes() and PRIVATE_LABEL.encode() not in path.read_bytes()
    profile = proxy_test_route.load_test_route(path)
    options = profile.transport_options()
    assert options["proxy"] == endpoint
    assert options["proxy_auth"] == (PRIVATE_USER, PRIVATE_KEY + "_country-EG_session-" + PRIVATE_LABEL)
    assert options["retry"] == 0 and options["verify"] is True
    assert_safe(result)
    assert PRIVATE_KEY not in repr(profile)


@pytest.mark.parametrize("route", [
    None, True, 1, [], {}, "", "\n\n", "not-a-route",
    pytest.param("x" * (1024 * 1024 + 1), id="oversized-input"),
    ROUTE.replace("_country-EG", "_country-LB"), ROUTE.replace("_country-EG", "_country-eg"),
    ROUTE.replace("proxy.packetstream.io", "private.invalid"), ROUTE.replace(":31112", ":8080"),
    ROUTE + "/path", ROUTE + "?secret=" + PRIVATE_KEY,
    ROUTE.replace("_session-", "_country-EG_session-"),
])
def test_invalid_single_route_rejects_without_overwriting_saved_profile(tmp_path, protected_store, route):
    path = tmp_path / "packetstream-test-route.dpapi"
    proxy_test_route.save_test_route(ROUTE, path)
    before, calls = path.read_bytes(), len(protected_store["saved"])
    with pytest.raises(SessionError) as error:
        proxy_test_route.save_test_route(route, path)
    assert path.read_bytes() == before and len(protected_store["saved"]) == calls
    assert PRIVATE_KEY not in str(error.value) and PRIVATE_LABEL not in str(error.value)


def test_test_route_loader_rejects_multi_entry_store_without_fallback(tmp_path, protected_store):
    path = tmp_path / "packetstream-test-route.dpapi"
    profiles = proxy_pool.parse_lines(ROUTE + "\n" + ROUTE.replace(PRIVATE_LABEL, "syntheticothersession"))
    proxy_pool.save_pool(profiles, path)
    with pytest.raises(SessionError):
        proxy_test_route.load_test_route(path)
    pool = proxy_test_route.load_test_pool(path)
    assert len(pool) == 2 and pool.proxy_for_index(0).transport_options() == profiles[0].transport_options()
    assert pool.proxy_for_index(1).transport_options() == profiles[1].transport_options()


def test_secure_save_failure_preserves_existing_test_route_and_fixed_error(tmp_path, protected_store, monkeypatch):
    path = tmp_path / "packetstream-test-route.dpapi"
    proxy_test_route.save_test_route(ROUTE, path)
    before = path.read_bytes()

    def save(_raw, _path):
        raise OSError("synthetic private storage error " + PRIVATE_KEY)

    monkeypatch.setattr(proxy_pool.store, "save_protected_bytes", save)
    with pytest.raises(SessionError) as error:
        proxy_test_route.save_test_route(ROUTE.replace(PRIVATE_LABEL, "syntheticreplacement"), path)
    assert path.read_bytes() == before and PRIVATE_KEY not in str(error.value)


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_test_session_flag_requires_boolean_without_configuration_or_worker(tmp_path, value):
    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", test_proxy_loader=lambda _path: pytest.fail("Invalid flag unlocked test route"))
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit({"action": "play", "rows": [7], "proxy_test_session": value})
    assert manager._thread is None and manager.snapshot() is None


@pytest.mark.parametrize("action", ["prepare", "preview", "login", "proxy-check"])
@pytest.mark.parametrize("value", [False, True])
def test_test_session_flag_is_rejected_for_non_workbench_actions(tmp_path, action, value):
    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", test_proxy_loader=lambda _path: pytest.fail("Invalid action unlocked test route"))
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit({"action": action, "rows": [7], "proxy_test_session": value})
    assert manager._thread is None and not list(tmp_path.iterdir())


def test_workbench_test_session_and_preparation_pool_cannot_be_combined(tmp_path):
    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", test_proxy_loader=lambda _path: pytest.fail("Invalid route combination unlocked credentials"))
    with pytest.raises(ui_jobs.JobValidationError):
        manager.submit({"action": "play", "rows": [7], "proxy_test_session": True, "proxy_sticky_pool": True})
    assert manager._thread is None and not list(tmp_path.iterdir())


@pytest.mark.parametrize("action", ["play", "like", "check", "song"])
@pytest.mark.parametrize("egypt", [False, True])
def test_exact_test_route_is_frozen_for_workbench_job_and_normal_proxy_is_not_loaded(tmp_path, action, egypt):
    profile, = proxy_pool.parse_lines(ROUTE)
    profiles, loaded, instances = [profile], [], []

    def loader(path):
        loaded.append(Path(path))
        return profiles[0]

    def factory(path):
        vault = FakeVault(path)
        instances.append(vault)
        return vault

    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", vault_factory=factory, test_proxy_loader=loader,
                                proxy_loader=lambda _path: pytest.fail("Test mode loaded general proxy"))
    queued = manager.submit({"action": action, "rows": [7, 8], "count": 2, "proxy_test_session": True, "proxy_egypt": egypt})
    job = finish(manager)
    assert len(loaded) == 1 and loaded[0] == tmp_path / "packetstream-test-route.dpapi"
    assert job["status"] == "succeeded" and queued["proxy_test_session"] is True
    assert job["proxy_egypt"] is True and job["proxy_mode"] == "test_session"
    calls = [call for vault in instances for call in vault.calls if call[0] == action]
    assert calls
    for call in calls:
        chosen = call[3].get("proxy") if action in {"play", "like"} else call[3]
        assert chosen is profile and chosen.transport_options()["proxy_auth"][1].endswith("_session-" + PRIVATE_LABEL)
    assert all(item["proxy_test_session"] is True and item["proxy_mode"] == "test_session" for item in job["results"])
    assert_safe(job)


def test_saved_route_changes_do_not_change_an_inflight_job_binding(tmp_path):
    first, = proxy_pool.parse_lines(ROUTE)
    second, = proxy_pool.parse_lines(ROUTE.replace(PRIVATE_LABEL, "syntheticreplacement"))
    started, release = threading.Event(), threading.Event()
    selected, loads, instances = [first], [], []

    def loader(path):
        loads.append(Path(path))
        return selected[0]

    def factory(path):
        vault = FakeVault(path, gate=(started, release))
        instances.append(vault)
        return vault

    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", vault_factory=factory, test_proxy_loader=loader,
                                proxy_loader=lambda _path: pytest.fail("Custom route fell back to general proxy"))
    manager.submit({"action": "play", "rows": [7, 8], "count": 3, "workers": 2,
                    "max_consecutive_failures": 20, "proxy_test_session": True})
    try:
        assert started.wait(3)
        selected[0] = second
    finally:
        release.set()
    assert finish(manager)["status"] == "succeeded"
    assert len(loads) == 1
    calls = [call for vault in instances for call in vault.calls if call[0] == "play"]
    assert len(calls) == 6 and all(call[3]["proxy"] is first for call in calls)
    manager.submit({"action": "play", "rows": [7], "proxy_test_session": True})
    assert finish(manager)["status"] == "succeeded" and len(loads) == 2
    assert next(call for call in instances[-1].calls if call[0] == "play")[3]["proxy"] is second


def test_missing_custom_profile_rejects_submit_without_any_fallback_or_account_call(tmp_path):
    calls = []

    def loader(_path):
        raise SessionError(PRIVATE_KEY)

    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", vault_factory=lambda path: calls.append(path), test_proxy_loader=loader,
                                proxy_loader=lambda _path: pytest.fail("Missing test route used general route"))
    with pytest.raises(ui_jobs.JobValidationError) as error:
        manager.submit({"action": "play", "rows": [7], "proxy_test_session": True})
    assert calls == [] and manager._thread is None and manager.snapshot() is None
    assert PRIVATE_KEY not in str(error.value)


def test_direct_and_general_proxy_jobs_do_not_unlock_test_profile(tmp_path):
    general, = proxy_pool.parse_lines(ROUTE.replace(PRIVATE_LABEL, "syntheticgeneral"))
    loads, instances = [], []

    def factory(path):
        vault = FakeVault(path)
        instances.append(vault)
        return vault

    def general_loader(path):
        loads.append(Path(path))
        return general

    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", vault_factory=factory, proxy_loader=general_loader,
                                test_proxy_loader=lambda _path: pytest.fail("Other mode unlocked separate test route"))
    for egypt in (False, True):
        manager.submit({"action": "play", "rows": [7], "proxy_egypt": egypt, "proxy_test_session": False})
        job = finish(manager)
        assert job["status"] == "succeeded" and job["proxy_test_session"] is False
        call = next(call for call in instances[-1].calls if call[0] == "play")
        assert call[3] == ({"proxy": general} if egypt else {})
    assert loads == [tmp_path / "packetstream.dpapi"]


@pytest.mark.parametrize("action", ["play", "like", "check", "song"])
def test_serial_workbench_pool_binding_follows_selected_identity_order_and_keeps_aliases_on_the_same_route(tmp_path, action):
    pool = synthetic_pool(2)
    selected = [9, 7, 8, 10]
    expected = {9: 1, 7: 2, 8: 1, 10: 1}
    instances, loads = [], []

    class DistinctIdentityVault(FakeVault):
        def __init__(self, path):
            super().__init__(path, rows=selected, cohort=selected)
            instances.append(self)

        def record(self, row):
            record = super().record(row)
            identity = 9 if row == 10 else row
            return {**record, "email": f"synthetic-route-{identity}@example.invalid"}

    def load(path):
        loads.append(Path(path))
        return pool

    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", vault_factory=DistinctIdentityVault,
                                test_proxy_loader=load,
                                proxy_loader=lambda _path: pytest.fail("Pool test fell back to general proxy"))
    manager.submit({"action": action, "rows": selected, "count": 3, "proxy_test_session": True})
    job = finish(manager)
    assert job["status"] == "succeeded" and len(loads) == 1
    assert job["proxy_pool_size"] == 2
    assert job["proxy_route_assignments"] == [{"source_row": row, "proxy_route_number": expected[row]} for row in selected]
    calls = [call for vault in instances for call in vault.calls if call[0] == action]
    assert [call[1] for call in calls] == [row for row in selected for _ in range(3 if action in {"play", "like"} else 1)]
    for call in calls:
        selected_proxy = call[3]["proxy"] if action in {"play", "like"} else call[3]
        assert selected_proxy is pool.proxy_for_index(expected[call[1]] - 1)
    assert all(item["proxy_route_number"] == expected[item["source_row"]] and item["proxy_pool_size"] == 2 for item in job["results"])
    archived = ui_server.safe_report(json.loads(manager._report_path.read_text()))
    assert archived["proxy_route_assignments"] == job["proxy_route_assignments"]
    assert all(item["proxy_route_number"] == expected[item["source_row"]] for item in archived["results"])
    assert_safe(job)
    assert_safe(archived)


@pytest.fixture
def test_console(tmp_path, protected_store, monkeypatch):
    manager = FakeManager()
    service = ui_server.ConsoleService(tmp_path / "a.sqlite3", manager=manager)

    def unavailable_vault(_path):
        raise SessionError("Synthetic vault is intentionally unavailable.")

    monkeypatch.setattr(ui_server, "AccountVault", unavailable_vault)
    server = ui_server.create_server(port=0, service=service)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    from types import SimpleNamespace
    yield SimpleNamespace(server=server, service=service, port=server.server_address[1], host=f"127.0.0.1:{server.server_address[1]}", folder=tmp_path)
    server.shutdown()
    server.server_close()
    thread.join(2)
    assert not thread.is_alive()


def test_test_proxy_endpoint_saves_only_separate_protected_route_and_state_is_redacted(test_console, protected_store):
    folder = test_console.folder
    general, pool = folder / "packetstream.dpapi", folder / "packetstream-sticky-pool.dpapi"
    general.write_bytes(b"synthetic-existing-general-protected")
    pool.write_bytes(b"synthetic-existing-preparation-protected")
    before = general.read_bytes(), pool.read_bytes()
    status, _headers, raw = request(test_console, "POST", "/api/test-proxy", body={"route": ROUTE})
    assert status == 200
    report = json.loads(raw)
    assert report["configured"] is True and report["country"] == "EG" and report["sticky"] is True
    assert protected_store["saved"][0][0] == folder / "packetstream-test-route.dpapi"
    assert (general.read_bytes(), pool.read_bytes()) == before
    status, _headers, raw = request(test_console, "GET", "/api/state")
    assert status == 200
    state = json.loads(raw)
    assert state["test_proxy"]["configured"] is True and state["test_proxy"]["sticky"] is True
    assert_safe(report)
    assert_safe(state)


def test_multiline_test_proxy_body_can_exceed_job_body_cap_and_keeps_state_summary_redacted(test_console, protected_store):
    payload = {"route": "\n".join(route_number(number) for number in range(6000))}
    body_bytes = len(json.dumps(payload).encode("utf-8"))
    assert ui_server.MAX_BODY < body_bytes < ui_server.MAX_TEST_PROXY_BODY
    status, _headers, raw = request(test_console, "POST", "/api/test-proxy", body=payload)
    assert status == 200 and json.loads(raw)["pool_size"] == 6000
    before, count = (test_console.folder / "packetstream-test-route.dpapi").read_bytes(), len(protected_store["saved"])
    status, _headers, raw = request(test_console, "GET", "/api/state")
    assert status == 200 and json.loads(raw)["test_proxy"]["pool_size"] == 6000
    assert_safe(json.loads(raw))
    status, _headers, _raw = request(test_console, "POST", "/api/jobs", body=payload)
    assert status == 413 and test_console.service.manager.calls == []
    assert (test_console.folder / "packetstream-test-route.dpapi").read_bytes() == before
    assert len(protected_store["saved"]) == count


def test_test_proxy_endpoint_rejects_oversized_body_before_profile_save(test_console, protected_store):
    payload = {"route": "x" * (ui_server.MAX_TEST_PROXY_BODY + 1)}
    status, _headers, raw = request(test_console, "POST", "/api/test-proxy", body=payload)
    assert status == 413 and protected_store["saved"] == []
    assert_safe(json.loads(raw))


def test_json_escaped_route_body_accepts_a_valid_exact_utf8_input_boundary(test_console, protected_store):
    maximum = 1024 * 1024
    prefix, suffix = "u:", "_country-EG_session-r:proxy.packetstream.io:31112"
    key_bytes = maximum - len((prefix + suffix).encode("utf-8"))
    route = prefix + "\u00e9" * (key_bytes // 2) + ("k" if key_bytes % 2 else "") + suffix
    assert len(route.encode("utf-8")) == maximum
    payload = {"route": route}
    encoded_size = len(json.dumps(payload).encode("utf-8"))
    assert 2 * maximum < encoded_size < ui_server.MAX_TEST_PROXY_BODY
    status, _headers, raw = request(test_console, "POST", "/api/test-proxy", body=payload)
    assert status == 200 and json.loads(raw)["pool_size"] == 1
    pool = proxy_test_route.load_test_pool(test_console.folder / "packetstream-test-route.dpapi")
    assert len(pool) == 1
    assert len(pool.proxy_for_index(0).transport_options()["proxy_auth"][1].encode("utf-8")) < maximum
    assert "\u00e9" not in raw.decode("utf-8")


@pytest.mark.parametrize("payload", [{}, {"route": None}, {"route": ROUTE, "username": PRIVATE_USER}, {"route": ROUTE + "\nmalformed-line"}])
def test_test_proxy_endpoint_rejects_invalid_payload_without_any_store_write(test_console, protected_store, payload):
    status, _headers, raw = request(test_console, "POST", "/api/test-proxy", body=payload)
    assert status == 400 and protected_store["saved"] == []
    assert_safe(json.loads(raw))


@pytest.mark.parametrize("status", ["queued", "running"])
def test_busy_ui_job_prevents_route_update_and_preserves_existing_profile(test_console, protected_store, status):
    path = test_console.folder / "packetstream-test-route.dpapi"
    proxy_test_route.save_test_route(ROUTE, path)
    before, count = path.read_bytes(), len(protected_store["saved"])
    test_console.service.manager.current = {"id": "synthetic-busy", "status": status}
    http_status, _headers, raw = request(test_console, "POST", "/api/test-proxy", body={"route": "\n".join([route_number(1), route_number(2)])})
    assert http_status == 409 and path.read_bytes() == before and len(protected_store["saved"]) == count
    assert_safe(json.loads(raw))


@pytest.mark.parametrize("options", [{"token": False}, {"headers": {"Origin": "https://private.invalid"}}])
def test_test_proxy_save_requires_same_local_capability_and_origin_as_other_writes(test_console, protected_store, options):
    status, _headers, raw = request(test_console, "POST", "/api/test-proxy", body={"route": ROUTE}, **options)
    assert status == 403 and protected_store["saved"] == []
    assert_safe(json.loads(raw))


def test_route_save_and_job_submission_share_one_service_lock(tmp_path, protected_store, monkeypatch):
    manager = FakeManager()
    service = ui_server.ConsoleService(tmp_path / "a.sqlite3", manager=manager)
    saving, release, submit_entered = threading.Event(), threading.Event(), threading.Event()
    original_save, order, errors = ui_server.save_test_route, [], []

    class ObservedLock:
        def __init__(self):
            self.lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "synthetic-submit":
                submit_entered.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_):
            self.lock.release()

    def save(route, path):
        saving.set()
        assert release.wait(5)
        result = original_save(route, path)
        order.append("saved")
        return result

    def save_worker():
        try:
            service.save_test_proxy({"route": ROUTE})
        except BaseException as error:
            errors.append(error)

    def submit_worker():
        try:
            service.submit({"action": "play", "rows": [7], "proxy_test_session": True})
            order.append("submitted")
        except BaseException as error:
            errors.append(error)

    service.lock = ObservedLock()
    monkeypatch.setattr(ui_server, "save_test_route", save)
    writer = threading.Thread(target=save_worker, name="synthetic-save")
    submitter = threading.Thread(target=submit_worker, name="synthetic-submit")
    writer.start()
    try:
        assert saving.wait(5)
        submitter.start()
        assert submit_entered.wait(5)
        assert manager.calls == []
    finally:
        release.set()
        writer.join(5)
        if submitter.ident is not None:
            submitter.join(5)
    assert not writer.is_alive() and not submitter.is_alive() and errors == []
    assert order == ["saved", "submitted"] and len(protected_store["saved"]) == 1


def test_public_proxy_failure_filter_discards_secrets_and_invalid_numeric_fields():
    value = {"failure_kind": "transport_error", "curl_code": True, "http_status": "200",
             "proxy_connect_http_status": 200, "country_check_attempts": 3, "auth_key": PRIVATE_KEY,
             "username": PRIVATE_USER, "session_label": PRIVATE_LABEL, "request_url": "https://private.invalid"}
    expected = {"failure_kind": "transport_error", "proxy_connect_http_status": 200, "country_check_attempts": 3}
    assert ui_jobs._public_proxy_failure(value) == expected
    assert ui_server.safe_report({"proxy_failure": value})["proxy_failure"] == expected
    assert_safe(expected)
    assert ui_jobs._public_proxy_failure({**value, "failure_kind": PRIVATE_USER}) == {}


@pytest.mark.parametrize("change", [
    {"source_row": True}, {"source_row": 0}, {"source_row": 2**31},
    {"test_number": "1"}, {"test_number": 0}, {"test_number": 6},
    {"started_at": "2026-10-03T12:00:00"}, {"started_at": PRIVATE_KEY},
    {"started_at": None}, {"started_at": "x" * 65},
])
def test_archived_active_test_descriptors_reject_malformed_identity_or_time(change):
    descriptor = {"source_row": 7, "test_number": 1, "started_at": "2026-10-03T12:00:00+00:00"}
    report = ui_server.safe_report({"active_tests": [{**descriptor, **change}]})
    assert report == {"active_tests": []}
    assert_safe(report)


def test_archived_active_test_descriptors_are_bounded_and_strip_unrelated_fields():
    descriptors = [{"source_row": row, "test_number": 1, "started_at": "2026-10-03T12:00:00+00:00",
                    "auth_key": PRIVATE_KEY, "endpoint": ROUTE, "elapsed_seconds": 999}
                   for row in range(1, 602)]
    report = ui_server.safe_report({"active_tests": descriptors})
    assert report["active_tests"] == [{"source_row": row, "test_number": 1,
                                       "started_at": "2026-10-03T12:00:00+00:00"}
                                      for row in range(1, 501)]
    assert len(report["active_tests"]) == ui_server.MAX_ACTIVE_TEST_DESCRIPTORS == 500
    assert_safe(report)


@pytest.mark.parametrize("size,number", [
    (True, 1), (0, 1), (10001, 1), (2.0, 1), (2, True), (2, 0), (2, 3), (2, 1.0), (2, PRIVATE_LABEL),
])
def test_archived_active_route_metadata_rejects_invalid_numeric_pairs(size, number):
    base = {"source_row": 7, "test_number": 1, "started_at": "2026-10-03T12:00:00+00:00"}
    value = {**base, "proxy_pool_size": size, "proxy_route_number": number, "auth_key": PRIVATE_KEY}
    report = ui_server.safe_report({"active_tests": [value]})
    assert report == {"active_tests": [base]}
    assert_safe(report)


@pytest.mark.parametrize("change", [
    {"source_row": True}, {"source_row": 0}, {"source_row": 2**31},
    {"proxy_route_number": True}, {"proxy_route_number": 0}, {"proxy_route_number": 3}, {"proxy_route_number": 1.0},
])
def test_archived_assignments_keep_only_valid_rows_and_ordinals_from_the_enclosing_pool(change):
    valid = {"source_row": 7, "proxy_route_number": 1}
    report = ui_server.safe_report({"proxy_pool_size": 2, "proxy_route_assignments": [
        {**valid, "auth_key": PRIVATE_KEY}, {**valid, **change, "auth_key": PRIVATE_KEY},
    ]})
    assert report == {"proxy_pool_size": 2, "proxy_route_assignments": [valid]}
    assert_safe(report)


@pytest.mark.parametrize("local_size", [3, True, 0, 10001, "2"])
def test_archived_local_route_metadata_cannot_contradict_enclosing_job_pool(local_size):
    base = {"source_row": 7, "test_number": 1, "started_at": "2026-10-03T12:00:00+00:00"}
    local = {**base, "proxy_pool_size": local_size, "proxy_route_number": 1, "auth_key": PRIVATE_KEY}
    report = ui_server.safe_report({"proxy_pool_size": 2, "active_tests": [local], "results": [local]})
    assert report["proxy_pool_size"] == 2 and report["active_tests"] == [base]
    assert "proxy_pool_size" not in report["results"][0] and "proxy_route_number" not in report["results"][0]
    assert_safe(report)


@pytest.mark.parametrize("action", ["check", "song"])
def test_read_only_workbench_proxy_failures_keep_safe_typed_diagnostics_and_exact_binding(tmp_path, action):
    profile, = proxy_pool.parse_lines(ROUTE)
    error = proxy.ProxyCountryError("transport_error", curl_code=28, proxy_connect_http_status=200, country_check_attempts=3)
    error.args = (PRIVATE_KEY,)
    error.session_label = PRIVATE_LABEL
    calls = []

    class FailedReadVault(FakeVault):
        def _http_session(self, row, proxy=None):
            calls.append((row, proxy))
            raise error

    manager = ui_jobs.JobManager(tmp_path / "a.sqlite3", vault_factory=FailedReadVault,
                                test_proxy_loader=lambda _path: profile,
                                proxy_loader=lambda _path: pytest.fail("Read failure fell back to another route"))
    manager.submit({"action": action, "rows": [7, 8], "proxy_test_session": True})
    job = finish(manager)
    assert calls == [(7, profile), (8, profile)]
    assert job["status"] == "completed_with_pending" and job["connection_pending"] == 2
    assert job["proxy_test_session"] is True and job["proxy_mode"] == "test_session"
    expected = {"failure_kind": "transport_error", "curl_code": 28,
                "proxy_connect_http_status": 200, "country_check_attempts": 3}
    assert all(report["proxy_failure"] == expected and report["outcome"] == "connection_pending" for report in job["results"])
    archived = ui_server.safe_report(json.loads(manager._report_path.read_text()))
    assert all(report["proxy_failure"] == expected for report in archived["results"])
    assert_safe(job)
    assert_safe(archived)
