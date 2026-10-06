"""A separately protected, immutable PacketStream route pool for Workbench tests."""

from pathlib import Path

from .errors import SessionError
from .proxy_pool import StickyProxyPool, parse_lines, save_pool


DEFAULT_TEST_ROUTE_PATH = Path(__file__).resolve().parents[1] / ".anghami" / "packetstream-test-route.dpapi"
MAX_TEST_ROUTE_BYTES = 1024 * 1024
MAX_TEST_ROUTES = 10_000
_INVALID = "Enter 1-10000 PacketStream Egypt or US sticky routes, one per line, within 1 MiB, on HTTP port 31112 or HTTPS port 31111."


def save_test_route(route, path=DEFAULT_TEST_ROUTE_PATH):
    """Validate the entire pasted value before crossing the protected boundary."""
    try:
        if type(route) is not str or len(route.encode("utf-8")) > MAX_TEST_ROUTE_BYTES:
            raise ValueError
        if not 1 <= len([line for line in route.splitlines() if line.strip()]) <= MAX_TEST_ROUTES:
            raise ValueError
        profiles = parse_lines(route)
        if not 1 <= len(profiles) <= MAX_TEST_ROUTES:
            raise ValueError
    except Exception:
        raise SessionError(_INVALID) from None
    try:
        return save_pool(profiles, Path(path))
    except Exception:
        raise SessionError("The Workbench test route pool could not be saved securely.") from None


def load_test_pool(path=DEFAULT_TEST_ROUTE_PATH):
    """Load only this Workbench pool, including older single-entry files."""
    try:
        pool = StickyProxyPool.load(Path(path))
        if not 1 <= len(pool) <= MAX_TEST_ROUTES:
            raise ValueError
        return pool
    except Exception:
        raise SessionError("The Workbench test route pool is missing, invalid, or could not be unlocked. Save its routes before selecting this mode.") from None


def load_test_route(path=DEFAULT_TEST_ROUTE_PATH):
    """Return only the exact saved route; no pool or general-proxy fallback."""
    try:
        pool = load_test_pool(path)
        if len(pool) != 1:
            raise ValueError
        return pool.proxy_for_index(0)
    except Exception:
        raise SessionError("The Workbench test route is missing, invalid, or could not be unlocked. Save one route before selecting this mode.") from None
