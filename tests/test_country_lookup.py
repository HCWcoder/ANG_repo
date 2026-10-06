"""Country lookup pacing uses fake time and no network requests."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from anghami_session import country_lookup, provider_recovery, proxy


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.waits.append(seconds)
        self.now += seconds


def test_eight_workers_share_one_limit_without_bursting_or_real_waits():
    clock = Clock()
    limiter = country_lookup._CountryLookupLimiter(clock=clock, sleep=clock.sleep)
    barrier = threading.Barrier(8)

    def worker(_worker):
        barrier.wait()
        starts = []
        for _ in range(2):
            observed = []

            def before_start():
                observed.append(clock.now)
                return True

            assert limiter.wait(before_start)
            starts.append(observed[-1])
        return starts

    with ThreadPoolExecutor(max_workers=8) as executor:
        starts = sorted(value for values in executor.map(worker, range(8)) for value in values)
    assert starts == pytest.approx([index / 8 for index in range(16)])
    assert len(clock.waits) == 15
    assert min(second - first for first, second in zip(starts, starts[1:])) >= 1 / 8


def test_short_sleep_wakeups_do_not_allow_early_starts():
    clock = Clock()

    def early_wakeup(seconds):
        clock.now += min(seconds, 0.025)

    limiter = country_lookup._CountryLookupLimiter(clock=clock, sleep=early_wakeup)
    assert limiter.wait()
    assert limiter.wait()
    assert clock.now == pytest.approx(0.125)


def test_idle_time_does_not_accumulate_a_burst_allowance():
    clock = Clock()
    limiter = country_lookup._CountryLookupLimiter(clock=clock, sleep=clock.sleep)
    assert limiter.wait()
    clock.now = 20.0
    assert limiter.wait()
    assert limiter.wait()
    assert clock.now == pytest.approx(20.125)


def test_provider_refusal_does_not_consume_a_start_slot():
    clock = Clock()
    limiter = country_lookup._CountryLookupLimiter(clock=clock, sleep=clock.sleep)
    assert limiter.wait(lambda: False) is False
    assert clock.waits == []
    assert limiter.wait()
    assert clock.now == 0.0


def test_country_cooldown_is_shared_with_browser_and_http_entry_point(monkeypatch):
    clock = Clock()
    limiter = country_lookup._CountryLookupLimiter(clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(country_lookup, "_limiter", limiter)
    monkeypatch.setattr(provider_recovery, "_clock", clock)
    monkeypatch.setattr(provider_recovery.time, "sleep", clock.sleep)
    provider_recovery._deadlines["country_check"] = 2.0

    assert proxy.wait_country_lookup_start()
    assert clock.now == 2.0
    assert country_lookup.wait_country_lookup_start()
    assert clock.now == 2.125


def test_cooldown_received_while_queued_delays_next_start(monkeypatch):
    clock = Clock()
    limiter = country_lookup._CountryLookupLimiter(clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(country_lookup, "_limiter", limiter)
    monkeypatch.setattr(provider_recovery, "_clock", clock)
    monkeypatch.setattr(provider_recovery.time, "sleep", clock.sleep)
    assert country_lookup.wait_country_lookup_start()

    def pacing_wait(seconds):
        clock.sleep(seconds)
        provider_recovery._deadlines["country_check"] = 3.0

    limiter._sleep = pacing_wait
    assert country_lookup.wait_country_lookup_start()
    assert clock.now == 3.0
    limiter._sleep = clock.sleep
    assert country_lookup.wait_country_lookup_start()
    assert clock.now == 3.125


def test_long_country_cooldown_remains_deferred_without_pacing_or_network(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(country_lookup, "_limiter", country_lookup._CountryLookupLimiter(clock=clock, sleep=clock.sleep))
    monkeypatch.setattr(provider_recovery, "_clock", clock)
    provider_recovery._deadlines["country_check"] = 3600.0
    assert country_lookup.wait_country_lookup_start() is False
    assert clock.waits == []
    assert provider_recovery._deadlines["country_check"] == 3600.0
