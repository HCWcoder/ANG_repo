"""Ordered, user-bound encrypted PacketStream sticky-session pool.

The pool contains routes, not proof of live exit IPs. Each account must verify
its selected route and its own authenticated session before becoming ready.
"""

import argparse
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re

from .errors import SessionError
from .proxy import PacketStreamProxy
from . import store

DEFAULT_STICKY_POOL_PATH = Path(__file__).resolve().parents[1] / ".anghami" / "packetstream-sticky-pool.dpapi"
_INVALID = "The sticky proxy pool is invalid. Use PacketStream Egypt sticky routes on HTTP port 31112 or HTTPS port 31111."
_MAX_ENTRIES = 100_000
_MAX_INPUT_BYTES = 16 * 1024 * 1024
PREPARATION_MAX_ROUTES = 10_000
PREPARATION_MAX_INPUT_BYTES = 1024 * 1024
_LINE = re.compile(
    r"(?:(?P<scheme>https?)://)?(?P<username>[^:\s@/]+):"
    r"(?P<auth_key>[^:\s@/]+)_country-EG_session-(?P<label>[A-Za-z0-9]{1,64})"
    r"(?P<separator>[:@])proxy\.packetstream\.io:(?P<port>31111|31112)\Z"
)


def _entry(proxy):
    if not isinstance(proxy, PacketStreamProxy):
        raise SessionError(_INVALID)
    # Revalidate every boundary instead of trusting a dataclass made elsewhere.
    checked = PacketStreamProxy.from_route(proxy.username, proxy.auth_key, proxy._session_label, proxy._endpoint)
    return {
        "username": checked.username, "auth_key": checked.auth_key,
        "session_label": checked._session_label, "endpoint": checked._endpoint,
    }


def _payload(proxies):
    if not isinstance(proxies, (list, tuple)) or not 1 <= len(proxies) <= _MAX_ENTRIES:
        raise SessionError(_INVALID)
    entries, seen = [], set()
    for proxy in proxies:
        entry = _entry(proxy)
        key = tuple(entry.values())
        if key not in seen:
            seen.add(key)
            entries.append(entry)
    return {"format_version": 1, "provider": "PacketStream", "country": "EG", "entries": entries}


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_lines(text):
    """Parse supplied data only; malformed lines reject the complete import."""
    try:
        if type(text) is not str or len(text.encode("utf-8")) > _MAX_INPUT_BYTES:
            raise ValueError
        proxies = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = _LINE.fullmatch(line)
            if match is None:
                raise ValueError
            port = int(match["port"])
            scheme = "https" if port == 31111 else "http"
            if match["scheme"] not in {None, scheme}:
                raise ValueError
            proxies.append(PacketStreamProxy.from_route(
                match["username"], match["auth_key"], match["label"],
                f"{scheme}://proxy.packetstream.io:{port}",
            ))
            if len(proxies) > _MAX_ENTRIES:
                raise ValueError
        payload = _payload(proxies)
        return [PacketStreamProxy.from_route(
            entry["username"], entry["auth_key"], entry["session_label"], entry["endpoint"],
        ) for entry in payload["entries"]]
    except Exception:
        raise SessionError(_INVALID) from None


@dataclass(frozen=True, slots=True)
class StickyProxyPool:
    _proxies: tuple = field(repr=False)

    def __post_init__(self):
        try:
            payload = _payload(self._proxies)
            checked = tuple(PacketStreamProxy.from_route(
                entry["username"], entry["auth_key"], entry["session_label"], entry["endpoint"],
            ) for entry in payload["entries"])
            object.__setattr__(self, "_proxies", checked)
        except Exception:
            raise SessionError(_INVALID) from None

    def __len__(self):
        return len(self._proxies)

    def proxy_for_index(self, index):
        if type(index) is not int or index < 0:
            raise SessionError("Use a nonnegative sticky proxy pool position.")
        return self._proxies[index % len(self)]

    def fingerprint(self):
        return hashlib.sha256(_canonical(_payload(self._proxies))).hexdigest()

    def summary(self):
        endpoints = sorted({proxy._endpoint for proxy in self._proxies})
        return {
            "provider": "PacketStream", "country": "EG", "sticky": True,
            "pool_size": len(self), "endpoint": endpoints[0] if len(endpoints) == 1 else "mixed",
        }

    @classmethod
    def load(cls, path=DEFAULT_STICKY_POOL_PATH):
        try:
            raw = store.load_protected_bytes(Path(path))
            if len(raw) > _MAX_INPUT_BYTES:
                raise ValueError
            payload = json.loads(raw.decode("utf-8"))
            if (
                type(payload) is not dict
                or set(payload) != {"format_version", "provider", "country", "entries"}
                or type(payload["format_version"]) is not int or payload["format_version"] != 1
                or payload["provider"] != "PacketStream" or payload["country"] != "EG"
                or type(payload["entries"]) is not list
                or not 1 <= len(payload["entries"]) <= _MAX_ENTRIES
            ):
                raise ValueError
            proxies = []
            for entry in payload["entries"]:
                if type(entry) is not dict or set(entry) != {"username", "auth_key", "session_label", "endpoint"}:
                    raise ValueError
                proxies.append(PacketStreamProxy.from_route(
                    entry["username"], entry["auth_key"], entry["session_label"], entry["endpoint"],
                ))
            pool = cls(tuple(proxies))
            if len(pool) != len(proxies):
                raise ValueError
            return pool
        except FileNotFoundError:
            raise SessionError("No sticky proxy pool is configured. Import its route files first.") from None
        except Exception:
            raise SessionError("The sticky proxy pool could not be unlocked or is invalid. Reimport it with the Windows user who will run preparation.") from None


def save_pool(proxies, path=DEFAULT_STICKY_POOL_PATH):
    try:
        pool = StickyProxyPool(tuple(proxies))
        raw = _canonical(_payload(pool._proxies))
        if len(raw) > _MAX_INPUT_BYTES:
            raise ValueError
        store.save_protected_bytes(raw, Path(path))
        return pool.summary()
    except Exception:
        raise SessionError("The sticky proxy pool could not be saved securely.") from None


def save_preparation_routes(routes, path=DEFAULT_STICKY_POOL_PATH):
    """Validate a UI replacement fully before atomically protecting its pool."""
    try:
        if type(routes) is not str or len(routes.encode("utf-8")) > PREPARATION_MAX_INPUT_BYTES:
            raise ValueError
        # Enforce the supplied line limit before parse_lines deduplicates routes.
        count = sum(1 for line in routes.splitlines() if line.strip())
        if not 1 <= count <= PREPARATION_MAX_ROUTES:
            raise ValueError
    except Exception:
        raise SessionError("Paste 1-10,000 sticky proxy routes, using at most 1 MiB of UTF-8 text.") from None
    return save_pool(parse_lines(routes), path)


def import_files(paths, path=DEFAULT_STICKY_POOL_PATH):
    try:
        if not paths:
            raise ValueError
        proxies = []
        for source in paths:
            source = Path(source)
            if source.stat().st_size > _MAX_INPUT_BYTES:
                raise ValueError
            proxies.extend(parse_lines(source.read_text(encoding="utf-8-sig")))
            if len(proxies) > _MAX_ENTRIES:
                raise ValueError
        return save_pool(proxies, path)
    except Exception:
        raise SessionError("The sticky proxy route files could not be imported. No partial pool was saved.") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--import-file", action="append", metavar="PATH", help="Import one or more route files in supplied order; repeat this option for each file.")
    actions.add_argument("--status", action="store_true", help="Show the configured pool count without credentials.")
    parser.add_argument("--pool", type=Path, default=DEFAULT_STICKY_POOL_PATH)
    args = parser.parse_args(argv)
    try:
        result = import_files(args.import_file, args.pool) if args.import_file else StickyProxyPool.load(args.pool).summary()
        print(json.dumps(result, indent=2))
        return 0
    except SessionError as exc:
        print(json.dumps({"passed": False, "message": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
