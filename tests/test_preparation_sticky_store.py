"""Preparation route replacement uses a synthetic DPAPI boundary, offline."""

import hashlib
import json

import pytest

from anghami_session import proxy, proxy_pool
from anghami_session.errors import SessionError


PRIVATE_USER = "synthetic-preparation-user"
PRIVATE_KEY = "synthetic-preparation-key"
PRIVATE_LABELS = ("syntheticold", "syntheticfirst", "syntheticsecond")


def route(label=PRIVATE_LABELS[1], *, port=31112):
    return f"{PRIVATE_USER}:{PRIVATE_KEY}_country-EG_session-{label}:proxy.packetstream.io:{port}"


def assert_safe(value):
    serialized = json.dumps(value) if isinstance(value, dict) else str(value)
    assert PRIVATE_USER not in serialized and PRIVATE_KEY not in serialized
    assert not any(label in serialized for label in PRIVATE_LABELS)
    assert "_session-" not in serialized


@pytest.fixture
def protected_store(monkeypatch):
    state = {"encrypted": [], "plaintext": {}}

    def crypt(raw, *, decrypt=False):
        raw = bytes(raw)
        if decrypt:
            return state["plaintext"][raw]
        protected = b"synthetic-protected:" + hashlib.sha256(raw).digest()
        state["plaintext"][protected] = raw
        state["encrypted"].append(protected)
        return protected

    monkeypatch.setattr(proxy_pool.store, "_crypt", crypt)
    monkeypatch.setattr(proxy.requests, "Session", lambda **_: pytest.fail("Route storage attempted a live request"))
    return state


def test_preparation_routes_are_protected_deduplicated_and_preserve_supplied_order(tmp_path, protected_store):
    path = tmp_path / "pool.dpapi"
    lines = [route(), "", "https://" + route(PRIVATE_LABELS[2], port=31111), route(), "  "]
    summary = proxy_pool.save_preparation_routes("\n".join(lines), path)
    assert summary == {"provider": "PacketStream", "country": "EG", "sticky": True, "pool_size": 2, "endpoint": "mixed"}
    assert_safe(summary)
    encrypted = path.read_bytes()
    assert PRIVATE_USER.encode() not in encrypted and PRIVATE_KEY.encode() not in encrypted
    pool = proxy_pool.StickyProxyPool.load(path)
    assert pool.proxy_for_index(0)._session_label == PRIVATE_LABELS[1]
    assert pool.proxy_for_index(1)._session_label == PRIVATE_LABELS[2]
    assert pool.proxy_for_index(2) == pool.proxy_for_index(0)
    assert len(protected_store["encrypted"]) == 1
    assert list(tmp_path.iterdir()) == [path]


def test_successful_replacement_changes_pool_atomically_and_keeps_only_protected_file(tmp_path, protected_store):
    path = tmp_path / "pool.dpapi"
    proxy_pool.save_preparation_routes(route(PRIVATE_LABELS[0]), path)
    original = path.read_bytes()
    original_fingerprint = proxy_pool.StickyProxyPool.load(path).fingerprint()
    result = proxy_pool.save_preparation_routes(route() + "\n" + route(PRIVATE_LABELS[2]), path)
    assert result["pool_size"] == 2 and path.read_bytes() != original
    assert proxy_pool.StickyProxyPool.load(path).fingerprint() != original_fingerprint
    assert list(tmp_path.iterdir()) == [path]
    assert_safe(result)


@pytest.mark.parametrize("routes", [
    None, True, 1, [], {}, b"not-text", "", "\n \t\n",
    route() + "\n" + PRIVATE_USER + ":" + PRIVATE_KEY + "@bad.invalid:31112",
    route() + "\n" + route().replace("_country-EG", "_country-LB"),
    route() + "\n" + "https://" + route(),
    "\ud800",
])
def test_invalid_replacement_preserves_existing_protected_pool(tmp_path, protected_store, routes):
    path = tmp_path / "pool.dpapi"
    proxy_pool.save_preparation_routes(route(PRIVATE_LABELS[0]), path)
    original = path.read_bytes()
    fingerprint = proxy_pool.StickyProxyPool.load(path).fingerprint()
    with pytest.raises(SessionError) as failure:
        proxy_pool.save_preparation_routes(routes, path)
    assert_safe(failure.value)
    assert path.read_bytes() == original
    assert proxy_pool.StickyProxyPool.load(path).fingerprint() == fingerprint
    assert len(protected_store["encrypted"]) == 1
    assert list(tmp_path.iterdir()) == [path]


def test_exact_raw_line_limit_is_accepted_before_deduplicating(tmp_path, protected_store):
    path = tmp_path / "pool.dpapi"
    short_route = "u:k_country-EG_session-a:proxy.packetstream.io:31112"
    text = "\n".join([short_route] * proxy_pool.PREPARATION_MAX_ROUTES)
    assert len(text.encode()) < proxy_pool.PREPARATION_MAX_INPUT_BYTES
    assert proxy_pool.save_preparation_routes(text, path)["pool_size"] == 1


def test_duplicate_lines_above_raw_limit_reject_before_parser_or_save(tmp_path, protected_store, monkeypatch):
    path = tmp_path / "pool.dpapi"
    proxy_pool.save_preparation_routes(route(PRIVATE_LABELS[0]), path)
    original = path.read_bytes()
    short_route = "u:k_country-EG_session-a:proxy.packetstream.io:31112"
    text = "\n".join([short_route] * (proxy_pool.PREPARATION_MAX_ROUTES + 1))
    assert len(text.encode()) < proxy_pool.PREPARATION_MAX_INPUT_BYTES
    monkeypatch.setattr(proxy_pool, "parse_lines", lambda _: pytest.fail("Over-limit raw lines reached deduplication"))
    with pytest.raises(SessionError) as failure:
        proxy_pool.save_preparation_routes(text, path)
    assert_safe(failure.value)
    assert path.read_bytes() == original and len(protected_store["encrypted"]) == 1


def test_exact_utf8_byte_limit_is_accepted(tmp_path, protected_store):
    path = tmp_path / "pool.dpapi"
    text = route()
    text += " " * (proxy_pool.PREPARATION_MAX_INPUT_BYTES - len(text.encode("utf-8")))
    assert len(text.encode("utf-8")) == proxy_pool.PREPARATION_MAX_INPUT_BYTES
    assert proxy_pool.save_preparation_routes(text, path)["pool_size"] == 1


@pytest.mark.parametrize("padding", [" ", "\u2003"])
def test_oversized_utf8_input_rejects_before_parse_and_preserves_pool(tmp_path, protected_store, monkeypatch, padding):
    path = tmp_path / "pool.dpapi"
    proxy_pool.save_preparation_routes(route(PRIVATE_LABELS[0]), path)
    original = path.read_bytes()
    text = route() + "\n" + padding * proxy_pool.PREPARATION_MAX_INPUT_BYTES
    assert len(text.encode("utf-8")) > proxy_pool.PREPARATION_MAX_INPUT_BYTES
    monkeypatch.setattr(proxy_pool, "parse_lines", lambda _: pytest.fail("Over-limit bytes reached parsing"))
    with pytest.raises(SessionError) as failure:
        proxy_pool.save_preparation_routes(text, path)
    assert_safe(failure.value)
    assert path.read_bytes() == original and len(protected_store["encrypted"]) == 1


def test_failed_atomic_replacement_keeps_existing_pool_and_removes_temporary(tmp_path, protected_store, monkeypatch):
    path = tmp_path / "pool.dpapi"
    proxy_pool.save_preparation_routes(route(PRIVATE_LABELS[0]), path)
    original = path.read_bytes()

    def fail(*_args):
        raise OSError(PRIVATE_USER + PRIVATE_KEY)

    with monkeypatch.context() as isolated:
        isolated.setattr(proxy_pool.store.os, "replace", fail)
        with pytest.raises(SessionError) as failure:
            proxy_pool.save_preparation_routes(route(), path)
    assert_safe(failure.value)
    assert path.read_bytes() == original and list(tmp_path.iterdir()) == [path]
    assert proxy_pool.StickyProxyPool.load(path).proxy_for_index(0)._session_label == PRIVATE_LABELS[0]


def test_existing_cli_parser_retains_larger_input_bounds(tmp_path, protected_store):
    assert proxy_pool._MAX_ENTRIES == 100_000 and proxy_pool._MAX_INPUT_BYTES == 16 * 1024 * 1024
    short_route = "u:k_country-EG_session-a:proxy.packetstream.io:31112"
    text = "\n".join([short_route] * (proxy_pool.PREPARATION_MAX_ROUTES + 1))
    assert len(proxy_pool.parse_lines(text)) == 1
    source = tmp_path / "routes.txt"
    source.write_text(text, encoding="utf-8")
    original = source.read_bytes()
    assert proxy_pool.import_files([source], tmp_path / "pool.dpapi")["pool_size"] == 1
    assert source.read_bytes() == original
