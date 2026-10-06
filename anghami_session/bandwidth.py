"""Numeric-only accounting of Python HTTP transfers within a caller's scope.

The counters describe HTTP headers and wire body bytes reported by libcurl.
They do not measure TLS/TCP traffic, browser traffic, or provider billing. No
request or response objects, URLs, headers, cookies, or body data are retained.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import math

from curl_cffi.const import CurlInfo


MAX_COUNTER = 2**53 - 1
BYTE_COUNTERS = {
    # REQUEST_SIZE includes the upload body in the installed curl transport;
    # SIZE_UPLOAD_T is a breakdown and must never be added a second time.
    "request_bytes": (CurlInfo.REQUEST_SIZE, "request_size"),
    "upload_body_bytes": (CurlInfo.SIZE_UPLOAD_T, "upload_size"),
    "download_body_bytes": (CurlInfo.SIZE_DOWNLOAD_T, "download_size"),
    "response_header_bytes": (CurlInfo.HEADER_SIZE, "header_size"),
}
BANDWIDTH_CURL_INFOS = (*[info for info, _ in BYTE_COUNTERS.values()], CurlInfo.USED_PROXY)
_ACTIVE_METER = ContextVar("anghami_http_bandwidth_meter", default=None)


def bandwidth_transport_options(options):
    """Add numeric byte infos without modifying routing or transport behavior."""
    result = dict(options)
    if _ACTIVE_METER.get() is None:
        return result
    infos = list(result.get("curl_infos") or ())
    for info in BANDWIDTH_CURL_INFOS:
        if info not in infos:
            infos.append(info)
    result["curl_infos"] = infos
    return result


def _byte_count(value):
    if type(value) not in (int, float):
        return None
    if type(value) is float and (not math.isfinite(value) or not value.is_integer()):
        return None
    if not 0 <= value <= MAX_COUNTER:
        return None
    return int(value)


def _attribute(value, name, default=None):
    try:
        return getattr(value, name, default)
    except Exception:
        return default


class NetworkUsageMeter:
    """An accumulator containing only validated numbers and fixed labels."""

    def __init__(self, parent=None):
        self._parent = parent
        self._counts = {name: 0 for name in BYTE_COUNTERS}
        self._counts.update({
            "request_count": 0, "measured_requests": 0,
            "partial_requests": 0, "unmeasured_requests": 0,
            "transport_errors": 0, "direct_bytes": 0,
            "proxy_bytes": 0, "unknown_route_bytes": 0,
            "request_header_bytes": 0, "sent_bytes": 0,
        })

    def _record(self, counters, proxy_used, failed):
        counts = self._counts
        counts["request_count"] += 1
        counts["transport_errors"] += int(failed is True)
        available = sum(value is not None for value in counters.values())
        coverage = "measured_requests" if available == len(BYTE_COUNTERS) else (
            "partial_requests" if available else "unmeasured_requests"
        )
        counts[coverage] += 1
        for name, value in counters.items():
            if value is not None:
                counts[name] += value
        request_size, upload_size = counters["request_bytes"], counters["upload_body_bytes"]
        # Missing stats preserve a known lower bound, rather than guessing a
        # full transfer size or treating missing coverage as measured zero.
        sent = max(request_size or 0, upload_size or 0)
        counts["sent_bytes"] += sent
        if request_size is not None and upload_size is not None:
            counts["request_header_bytes"] += max(0, request_size - upload_size)
        route = "proxy_bytes" if proxy_used is True else (
            "direct_bytes" if proxy_used is False else "unknown_route_bytes"
        )
        counts[route] += sent + (counters["download_body_bytes"] or 0) + (counters["response_header_bytes"] or 0)
        if self._parent is not None:
            self._parent._record(counters, proxy_used, failed)

    def snapshot(self):
        result = dict(self._counts)
        result["received_bytes"] = result["download_body_bytes"] + result["response_header_bytes"]
        result["total_bytes"] = result["sent_bytes"] + result["received_bytes"]
        result["measurement"] = (
            "unavailable" if result["measured_requests"] + result["partial_requests"] == 0 else
            "measured" if result["partial_requests"] + result["unmeasured_requests"] == 0 else "partial"
        )
        result["scope"] = "python_http"
        return result


@contextmanager
def measure_network_usage(*, propagate=True):
    """Capture transfers in this context; workers receive their own scopes.

    Nested scopes normally also contribute to their parent. A child with
    ``propagate=False`` records independently, leaving the parent's setup-only
    counters untouched. Exiting always restores the preceding context.
    """
    meter = NetworkUsageMeter(_ACTIVE_METER.get() if propagate is True else None)
    token = _ACTIVE_METER.set(meter)
    try:
        yield meter
    finally:
        _ACTIVE_METER.reset(token)


def measured_request(transport, method, *args, proxy_used=None, **kwargs):
    """Forward exactly one request and account for its numeric transfer info.

    curl_cffi attaches a parsed response to transport failures before resetting
    the curl handle. That response preserves numeric infos. Without it the
    handle may already be reset, so querying the handle could falsely report
    zero traffic; such transfers are explicitly marked unmeasured instead.
    """
    meter = _ACTIVE_METER.get()
    if meter is None:
        return getattr(transport, method)(*args, **kwargs)
    response, failed = None, False
    try:
        response = getattr(transport, method)(*args, **kwargs)
        return response
    except BaseException as exc:
        failed = True
        response = _attribute(exc, "response")
        raise
    finally:
        infos = _attribute(response, "infos")
        infos = infos if type(infos) is dict else {}
        counters = {}
        for name, (info, attribute) in BYTE_COUNTERS.items():
            value = _byte_count(infos.get(info))
            if value is None:
                value = _byte_count(_attribute(response, attribute))
            counters[name] = value
        used = infos.get(CurlInfo.USED_PROXY)
        if type(used) is int and used in (0, 1):
            proxy_used = used == 1
        elif type(proxy_used) is not bool:
            # Only presence is inspected; route URLs and credentials are never
            # copied into the measurement object or any report.
            proxies = _attribute(transport, "proxies")
            proxy_used = bool(proxies) if type(proxies) is dict else None
        meter._record(counters, proxy_used, failed)
