"""Process-wide start pacing for the single public country lookup service."""

import threading
import time


COUNTRY_LOOKUP_REQUESTS_PER_SECOND = 8


class _CountryLookupLimiter:
    """Space request starts without holding the lock during network requests."""

    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self, before_start=lambda: True):
        with self._lock:
            while True:
                # Check again after each pacing wait. Another worker may have
                # received a 429 while this request was waiting for its slot.
                if not before_start():
                    return False
                now = self._clock()
                remaining = self._next_start - now
                if remaining <= 0:
                    self._next_start = now + 1 / COUNTRY_LOOKUP_REQUESTS_PER_SECOND
                    return True
                self._sleep(remaining)


_limiter = _CountryLookupLimiter()


def wait_country_lookup_start():
    """Honor shared cooldowns and pace HTTP and browser lookups together."""
    from .provider_recovery import wait_before_provider_request

    return _limiter.wait(lambda: wait_before_provider_request("country_check"))
