"""Sticky proxy import and round-robin selection use only synthetic credentials."""

import hashlib
import json
from pathlib import Path

import pytest

from anghami_session import proxy, proxy_pool
from anghami_session.errors import SessionError


PRIVATE_USER = "synthetic-pool-user"
PRIVATE_KEY = "synthetic-pool-key"
PRIVATE_LABELS = ("syntheticlabelone", "syntheticlabeltwo", "syntheticlabelthree")


def route(label, *, username=PRIVATE_USER, key=PRIVATE_KEY, port=31112, country="EG"):
    return f"{username}:{key}_country-{country}_session-{label}:proxy.packetstream.io:{port}"


def assert_safe(value):
    text = json.dumps(value) if isinstance(value, dict) else str(value)
    assert PRIVATE_USER not in text and PRIVATE_KEY not in text
    assert not any(label in text for label in PRIVATE_LABELS)
    assert "_session-" not in text and "proxy_auth" not in text


@pytest.fixture
def protected_store(monkeypatch):
    state = {"plaintext": {}, "saved": [], "loaded": []}

    def save(raw, path):
        path = Path(path)
        state["saved"].append((path, bytes(raw)))
        state["plaintext"][path] = bytes(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A synthetic protected-store boundary, never a replacement crypto implementation.
        path.write_bytes(b"synthetic-protected:" + hashlib.sha256(raw).digest())

    def load(path):
        path = Path(path)
        state["loaded"].append(path)
        if path not in state["plaintext"]:
            raise FileNotFoundError()
        return state["plaintext"][path]

    monkeypatch.setattr(proxy_pool.store, "save_protected_bytes", save)
    monkeypatch.setattr(proxy_pool.store, "load_protected_bytes", load)
    monkeypatch.setattr(proxy.requests, "Session", lambda **_: pytest.fail("Pool import/selection attempted a live request"))
    return state


def test_supplied_attachment_layout_preserves_order_and_unique_sticky_credentials():
    text = "\n".join([route(PRIVATE_LABELS[0]), "", route(PRIVATE_LABELS[1]), route(PRIVATE_LABELS[0]), route(PRIVATE_LABELS[2])])
    proxies = proxy_pool.parse_lines(text)
    assert len(proxies) == 3
    for label, item in zip(PRIVATE_LABELS, proxies):
        assert item.country == "EG"
        options = item.transport_options()
        assert options["proxy"] == "http://proxy.packetstream.io:31112"
        assert options["proxy_auth"] == (PRIVATE_USER, PRIVATE_KEY + "_country-EG_session-" + label)
        assert options["retry"] == 0 and options["verify"] is True and options["debug"] is False
        assert item.browser_options()["password"] == options["proxy_auth"][1]
        assert_safe(repr(item))
        assert_safe(item.summary())


@pytest.mark.parametrize("scheme,separator,port", [
    ("", ":", 31112), ("http://", ":", 31112), ("http://", "@", 31112),
    ("", "@", 31112), ("https://", ":", 31111), ("https://", "@", 31111),
])
def test_supported_proxy_attachment_formats_map_to_the_same_supplied_route(scheme, separator, port):
    line = scheme + route(PRIVATE_LABELS[0], port=port)
    if separator == "@":
        line = line.replace(":proxy.packetstream.io", "@proxy.packetstream.io")
    selected, = proxy_pool.parse_lines(line)
    expected_scheme = "http" if port == 31112 else "https"
    assert selected.transport_options()["proxy"] == f"{expected_scheme}://proxy.packetstream.io:{port}"
    assert selected.transport_options()["proxy_auth"] == (PRIVATE_USER, PRIVATE_KEY + "_country-EG_session-" + PRIVATE_LABELS[0])


def test_encrypted_pool_roundtrip_round_robin_and_fingerprint_are_stable(tmp_path, protected_store):
    path = tmp_path / "pool.dpapi"
    proxies = proxy_pool.parse_lines("\n".join(route(label) for label in PRIVATE_LABELS))
    summary = proxy_pool.save_pool(proxies, path)
    assert summary["pool_size"] == 3 and summary["country"] == "EG" and summary["sticky"] is True
    assert_safe(summary)
    encrypted = path.read_bytes()
    assert PRIVATE_USER.encode() not in encrypted and PRIVATE_KEY.encode() not in encrypted
    assert all(label.encode() not in encrypted for label in PRIVATE_LABELS)
    pool = proxy_pool.StickyProxyPool.load(path)
    assert len(pool) == 3
    for index in range(8):
        selected = pool.proxy_for_index(index)
        assert selected.transport_options() == proxies[index % 3].transport_options()
    assert pool.proxy_for_index(0).transport_options() == pool.proxy_for_index(3000).transport_options()
    assert len(pool.fingerprint()) == 64 and set(pool.fingerprint()) <= set("0123456789abcdef")
    assert pool.fingerprint() == proxy_pool.StickyProxyPool.load(path).fingerprint()
    assert_safe(pool.summary())
    assert_safe(repr(pool))


def test_import_files_deduplicates_across_attachments_and_preserves_source_files(tmp_path, protected_store):
    first, second, destination = tmp_path / "one.txt", tmp_path / "two.txt", tmp_path / "pool.dpapi"
    first_raw = (route(PRIVATE_LABELS[0]) + "\n" + route(PRIVATE_LABELS[1]) + "\n").encode()
    second_raw = (route(PRIVATE_LABELS[1]) + "\n" + route(PRIVATE_LABELS[2]) + "\n").encode()
    first.write_bytes(first_raw)
    second.write_bytes(second_raw)
    result = proxy_pool.import_files([first, second], destination)
    assert result["pool_size"] == 3 and first.read_bytes() == first_raw and second.read_bytes() == second_raw
    assert len(protected_store["saved"]) == 1
    pool = proxy_pool.StickyProxyPool.load(destination)
    assert [pool.proxy_for_index(i).transport_options()["proxy_auth"][1].rsplit("_session-", 1)[1] for i in range(3)] == list(PRIVATE_LABELS)
    assert_safe(result)


def test_pool_fingerprint_binds_order_endpoint_and_credentials(tmp_path, protected_store):
    rows = [route(label) for label in PRIVATE_LABELS]

    def fingerprint(text, name):
        destination = tmp_path / name
        proxy_pool.save_pool(proxy_pool.parse_lines(text), destination)
        return proxy_pool.StickyProxyPool.load(destination).fingerprint()

    base = fingerprint("\n".join(rows), "base.dpapi")
    reordered = fingerprint("\n".join(reversed(rows)), "order.dpapi")
    changed_key = fingerprint("\n".join([route(PRIVATE_LABELS[0], key="different-synthetic-key"), *rows[1:]]), "key.dpapi")
    changed_endpoint = fingerprint("\n".join([route(PRIVATE_LABELS[0], port=31111), *rows[1:]]), "endpoint.dpapi")
    assert len({base, reordered, changed_key, changed_endpoint}) == 4


def test_us_sticky_routes_preserve_country_through_transport_and_encrypted_pool(tmp_path, protected_store):
    path = tmp_path / "us-pool.dpapi"
    supplied, = proxy_pool.parse_lines(route(PRIVATE_LABELS[0], country="US"))
    assert supplied.country == "US"
    assert supplied.transport_options()["proxy_auth"] == (
        PRIVATE_USER, PRIVATE_KEY + "_country-US_session-" + PRIVATE_LABELS[0],
    )
    assert supplied.browser_options()["password"] == PRIVATE_KEY + "_country-US_session-" + PRIVATE_LABELS[0]
    summary = proxy_pool.save_pool([supplied], path)
    loaded = proxy_pool.StickyProxyPool.load(path)
    assert summary["country"] == loaded.summary()["country"] == "US"
    assert loaded.proxy_for_index(0).transport_options() == supplied.transport_options()
    assert json.loads(protected_store["saved"][-1][1])["country"] == "US"


def test_sticky_pool_rejects_mixed_countries_and_preparation_accepts_us_pool(tmp_path, protected_store):
    egypt, = proxy_pool.parse_lines(route(PRIVATE_LABELS[0]))
    us, = proxy_pool.parse_lines(route(PRIVATE_LABELS[1], country="US"))
    with pytest.raises(SessionError):
        proxy_pool.StickyProxyPool((egypt, us))
    result = proxy_pool.save_preparation_routes(
        route(PRIVATE_LABELS[0], country="US"), tmp_path / "preparation.dpapi",
    )
    assert result["country"] == "US"
    assert json.loads(protected_store["saved"][-1][1])["country"] == "US"


@pytest.mark.parametrize("value", [None, False, True, -1, 1.0, "1", [], {}])
def test_pool_selection_requires_nonnegative_integer(tmp_path, protected_store, value):
    destination = tmp_path / "pool.dpapi"
    proxy_pool.save_pool(proxy_pool.parse_lines(route(PRIVATE_LABELS[0])), destination)
    pool = proxy_pool.StickyProxyPool.load(destination)
    with pytest.raises(SessionError):
        pool.proxy_for_index(value)


@pytest.mark.parametrize("text", [
    "", "\n\n", "# comment", "not-a-proxy",
    route(PRIVATE_LABELS[0]).replace("_country-EG", "_country-LB"),
    route(PRIVATE_LABELS[0]).replace("_country-EG", "_country-eg"),
    route(PRIVATE_LABELS[0]).replace("_session-", "_session- "),
    route(PRIVATE_LABELS[0]).replace("_session-", "_session-" + "x" * 1000),
    route(PRIVATE_LABELS[0]).replace("proxy.packetstream.io", "evil.invalid"),
    route(PRIVATE_LABELS[0]).replace(":31112", ":8080"),
    route(PRIVATE_LABELS[0]).replace(":31112", ":31112/path"),
    route(PRIVATE_LABELS[0]).replace(":31112", ":31112?secret=" + PRIVATE_KEY),
    route(PRIVATE_LABELS[0]).replace(":31112", ":31112#fragment"),
    "https://" + route(PRIVATE_LABELS[0]),
    route(PRIVATE_LABELS[0]).replace(PRIVATE_USER, "bad user"),
    route(PRIVATE_LABELS[0]).replace(PRIVATE_USER, "bad\x00user"),
    route(PRIVATE_LABELS[0]).replace(PRIVATE_KEY, PRIVATE_KEY + "_country-US"),
    route(PRIVATE_LABELS[0]).replace(PRIVATE_KEY, PRIVATE_KEY + "_session-stale"),
    route(PRIVATE_LABELS[0]) + ":extra",
    route(PRIVATE_LABELS[0]).replace("_session-" + PRIVATE_LABELS[0], "_session-"),
    route(PRIVATE_LABELS[0]).replace(PRIVATE_USER, ""),
    route(PRIVATE_LABELS[0]).replace(PRIVATE_KEY, ""),
])
def test_malformed_or_unsafe_attachment_line_rejects_entire_pool_with_safe_error(text):
    with pytest.raises(SessionError) as failure:
        proxy_pool.parse_lines(text)
    assert_safe(failure.value)


def test_import_rejects_invalid_later_file_before_publishing_any_encrypted_pool(tmp_path, protected_store):
    first, bad, destination = tmp_path / "valid.txt", tmp_path / "invalid.txt", tmp_path / "pool.dpapi"
    first.write_text(route(PRIVATE_LABELS[0]), encoding="utf-8")
    bad.write_text(PRIVATE_USER + ":" + PRIVATE_KEY + "@malicious.invalid", encoding="utf-8")
    with pytest.raises(SessionError) as failure:
        proxy_pool.import_files([first, bad], destination)
    assert not destination.exists() and protected_store["saved"] == []
    assert_safe(failure.value)


@pytest.mark.parametrize("raw", [b"not-json", b"\xff", b"[]", b"{}"])
def test_invalid_protected_pool_payload_fails_closed(tmp_path, protected_store, raw):
    path = tmp_path / "pool.dpapi"
    protected_store["plaintext"][path] = raw
    with pytest.raises(SessionError) as failure:
        proxy_pool.StickyProxyPool.load(path)
    assert_safe(failure.value)


def test_missing_or_locked_pool_uses_safe_error_without_secret_exception_text(tmp_path, protected_store, monkeypatch):
    path = tmp_path / "pool.dpapi"
    with pytest.raises(SessionError) as missing:
        proxy_pool.StickyProxyPool.load(path)
    assert_safe(missing.value)

    def fail(_path):
        raise RuntimeError(PRIVATE_USER + PRIVATE_KEY + PRIVATE_LABELS[0])

    monkeypatch.setattr(proxy_pool.store, "load_protected_bytes", fail)
    with pytest.raises(SessionError) as locked:
        proxy_pool.StickyProxyPool.load(path)
    assert_safe(locked.value)


@pytest.mark.parametrize("label,endpoint", [
    ("", "http://proxy.packetstream.io:31112"),
    ("x" * 65, "http://proxy.packetstream.io:31112"),
    ("bad-label", "http://proxy.packetstream.io:31112"),
    ("bad_label", "http://proxy.packetstream.io:31112"),
    ("\u200bhidden", "http://proxy.packetstream.io:31112"),
    ("goodlabel", "http://malicious.invalid:31112"),
    ("goodlabel", "http://proxy.packetstream.io:31111"),
    ("goodlabel", "https://proxy.packetstream.io:31112"),
    ("goodlabel", "http://proxy.packetstream.io:31112/path"),
    ("goodlabel", "http://proxy.packetstream.io:31112?key=" + PRIVATE_KEY),
])
def test_explicit_route_constructor_revalidates_label_and_exact_public_endpoint(label, endpoint):
    with pytest.raises(SessionError) as failure:
        proxy.PacketStreamProxy.from_route(PRIVATE_USER, PRIVATE_KEY, label, endpoint)
    assert_safe(failure.value)


def test_transport_option_mutation_cannot_change_the_next_worker_route():
    pool = proxy_pool.StickyProxyPool(tuple(proxy_pool.parse_lines(route(PRIVATE_LABELS[0]))))
    first = pool.proxy_for_index(0).transport_options()
    first["proxy"] = "http://malicious.invalid"
    first["proxy_auth"] = ("wrong", "wrong")
    second = pool.proxy_for_index(1).transport_options()
    assert second["proxy"] == "http://proxy.packetstream.io:31112"
    assert second["proxy_auth"] == (PRIVATE_USER, PRIVATE_KEY + "_country-EG_session-" + PRIVATE_LABELS[0])


def test_import_accepts_utf8_bom_files_but_preserves_source_and_protects_output(tmp_path, protected_store):
    source, destination = tmp_path / "routes.txt", tmp_path / "pool.dpapi"
    raw = ("\ufeff" + route(PRIVATE_LABELS[0]) + "\r\n").encode("utf-8")
    source.write_bytes(raw)
    result = proxy_pool.import_files([source], destination)
    assert result["pool_size"] == 1 and source.read_bytes() == raw
    assert PRIVATE_KEY.encode() not in destination.read_bytes()


def test_duplicate_or_extra_fields_in_protected_payload_are_rejected(tmp_path, protected_store):
    destination = tmp_path / "pool.dpapi"
    proxy_pool.save_pool(proxy_pool.parse_lines(route(PRIVATE_LABELS[0])), destination)
    original = json.loads(protected_store["plaintext"][destination])
    duplicate = dict(original, entries=[original["entries"][0], original["entries"][0]])
    extra = dict(original, unsafe=PRIVATE_KEY)
    malformed_entry = json.loads(json.dumps(original))
    malformed_entry["entries"][0]["unsafe"] = PRIVATE_KEY
    for payload in (duplicate, extra, malformed_entry):
        protected_store["plaintext"][destination] = json.dumps(payload).encode()
        with pytest.raises(SessionError) as failure:
            proxy_pool.StickyProxyPool.load(destination)
        assert_safe(failure.value)


def test_storage_failure_and_cli_output_do_not_expose_pool_authentication(tmp_path, protected_store, monkeypatch, capsys):
    source, destination = tmp_path / "routes.txt", tmp_path / "pool.dpapi"
    source.write_text(route(PRIVATE_LABELS[0]), encoding="utf-8")
    assert proxy_pool.main(["--import-file", str(source), "--pool", str(destination)]) == 0
    assert proxy_pool.main(["--status", "--pool", str(destination)]) == 0
    rendered = capsys.readouterr()
    assert_safe(rendered.out + rendered.err)

    def fail(_raw, _path):
        raise RuntimeError(PRIVATE_USER + PRIVATE_KEY + PRIVATE_LABELS[0])

    monkeypatch.setattr(proxy_pool.store, "save_protected_bytes", fail)
    with pytest.raises(SessionError) as error:
        proxy_pool.save_pool(proxy_pool.parse_lines(route(PRIVATE_LABELS[0])), destination)
    assert_safe(error.value)


@pytest.mark.parametrize("modifier", ["_country-EG", "_COUNTRY-EG", "_Country-eG"])
def test_supplied_base_credentials_cannot_hide_a_duplicate_country_modifier(modifier):
    supplied_key = PRIVATE_KEY + modifier
    # The ordinary profile constructor historically accepts a suffix. The new
    # explicit-route boundary must preserve supplied base values, never silently
    # strip an extra routing modifier from an attachment or protected entry.
    with pytest.raises(SessionError) as direct:
        proxy.PacketStreamProxy.from_route(
            PRIVATE_USER, supplied_key, PRIVATE_LABELS[0], "http://proxy.packetstream.io:31112",
        )
    assert_safe(direct.value)
    with pytest.raises(SessionError) as parsed:
        proxy_pool.parse_lines(route(PRIVATE_LABELS[0], key=supplied_key))
    assert_safe(parsed.value)
