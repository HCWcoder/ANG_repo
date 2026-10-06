"""Persisted preparation cooldowns use fake time and synthetic local owners."""

from datetime import datetime as RealDatetime, timedelta, timezone
import json

import pytest

from anghami_session import country_preparation as country, ui_preparation as bridge
from anghami_session.errors import SessionError
from test_country_preparation_workers import imported
from test_ui_preparation_checkpoint_resume import NEW_JOB, drained, invoke, save

NOW = RealDatetime(2026, 10, 4, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        current = NOW
        sleeps = []
        after_sleep = None
        def sleep(self, seconds):
            assert 0 < seconds <= 1
            self.sleeps.append(seconds)
            self.current += timedelta(seconds=seconds)
            if self.after_sleep is not None:
                self.after_sleep()
    value = Clock()
    class FakeDatetime(RealDatetime):
        @classmethod
        def now(cls, tz=None):
            return value.current.astimezone(tz) if tz is not None else value.current.replace(tzinfo=None)
    monkeypatch.setattr(bridge, "datetime", FakeDatetime)
    monkeypatch.setattr(bridge, "sleep", value.sleep)
    return value


def owner(base, *, job_id=NEW_JOB, status="running"):
    country._atomic_json(base / "ui-last-job.json", {"id": job_id, "status": status})


@pytest.mark.parametrize("value", [
    None, True, 123, [], {}, "", "not-a-date", "2026-10-04T00:00:01",
    "2026-10-04T01:00:01+01:00", "2026-10-04T00:00:01-03:00",
    "2026-99-99T00:00:00+00:00", "x" * 65,
    (NOW + timedelta(seconds=86406)).isoformat(),
])
def test_invalid_or_unbounded_resume_deadline_rejects_before_claim_and_dispatch(drained, clock, value):
    fixture = drained
    fixture.manifest["resume_not_before_utc"] = value
    save(fixture)
    with pytest.raises(SessionError, match="continuation"):
        invoke(fixture)
    assert fixture.dispatch == [] and clock.sleeps == []
    assert json.loads(fixture.manifest_path.read_text())["resumed_by"] is None
    assert not (fixture.base / f"ui-preparation-progress-{NEW_JOB}.json").exists()


@pytest.mark.parametrize("value", [
    (NOW - timedelta(seconds=5)).isoformat(), NOW.isoformat(),
    (NOW + timedelta(seconds=86405)).isoformat(), "2026-10-04T00:00:01Z",
])
def test_utc_deadline_validation_accepts_expiry_and_exact_upper_bound(clock, value):
    actual = bridge._cooldown_deadline(value)
    assert actual.utcoffset() == timedelta(0)
    assert actual == RealDatetime.fromisoformat(value)


def test_expired_resume_deadline_returns_immediately_without_wait_callback(tmp_path, clock):
    owner(tmp_path)
    callbacks = []
    assert bridge._wait_resume_cooldown(tmp_path, NEW_JOB, NOW - timedelta(seconds=5), lambda: callbacks.append(True)) is True
    assert clock.sleeps == callbacks == [] and clock.current == NOW


def test_future_deadline_waits_exact_original_absolute_time_without_extension(tmp_path, clock):
    owner(tmp_path)
    deadline = bridge._cooldown_deadline((NOW + timedelta(seconds=3.25)).isoformat())
    callbacks = []
    assert bridge._wait_resume_cooldown(tmp_path, NEW_JOB, deadline, lambda: callbacks.append(clock.current)) is True
    assert clock.sleeps == [1.0, 1.0, 1.0, 0.25]
    assert clock.current == deadline and len(callbacks) == 4
    assert deadline == NOW + timedelta(seconds=3.25)


@pytest.mark.parametrize("interrupt", ["stop", "owner", "finished", "missing"])
def test_future_wait_rechecks_stop_and_ownership_each_second_and_aborts(tmp_path, clock, interrupt):
    owner(tmp_path)
    def interrupt_once():
        if interrupt == "stop":
            (tmp_path / f"ui-preparation-progress-{NEW_JOB}.stop").write_text("stop")
        elif interrupt == "owner":
            owner(tmp_path, job_id="f" * 32)
        elif interrupt == "finished":
            owner(tmp_path, status="failed")
        else:
            (tmp_path / "ui-last-job.json").unlink()
    clock.after_sleep = interrupt_once
    callbacks = []
    assert bridge._wait_resume_cooldown(tmp_path, NEW_JOB, NOW + timedelta(seconds=5), lambda: callbacks.append(True)) is False
    assert clock.sleeps == [1.0] and callbacks == [True]


def test_owner_replacement_during_final_sleep_is_rechecked_before_dispatch(tmp_path, clock):
    owner(tmp_path)
    clock.after_sleep = lambda: owner(tmp_path, job_id="f" * 32)
    assert bridge._wait_resume_cooldown(tmp_path, NEW_JOB, NOW + timedelta(seconds=1), lambda: None) is False
    assert clock.current == NOW + timedelta(seconds=1)


def test_expired_deadline_bridge_dispatches_exact_continuation_only_once(drained, clock):
    fixture = drained
    owner(fixture.base)
    fixture.manifest["resume_not_before_utc"] = (NOW - timedelta(seconds=1)).isoformat()
    save(fixture)
    result = invoke(fixture)
    assert len(fixture.dispatch) == 1 and clock.sleeps == []
    assert result["selected_rows"] == fixture.manifest["selected_rows"]
    assert json.loads(fixture.manifest_path.read_text())["resume_not_before_utc"] == fixture.manifest["resume_not_before_utc"]
    with pytest.raises(SessionError, match="checkpoint"):
        invoke(fixture)
    assert len(fixture.dispatch) == 1


def test_future_deadline_bridge_emits_wait_progress_before_any_country_dispatch(drained, clock, monkeypatch):
    fixture = drained
    owner(fixture.base)
    deadline = NOW + timedelta(seconds=2.5)
    fixture.manifest["resume_not_before_utc"] = deadline.isoformat()
    save(fixture)
    original = country.run_plan
    def checked_dispatch(*args, **options):
        assert clock.current == deadline
        return original(*args, **options)
    monkeypatch.setattr(country, "run_plan", checked_dispatch)
    invoke(fixture)
    assert len(fixture.dispatch) == 1 and clock.sleeps == [1.0, 1.0, 0.5]
    assert [item["phase"] for item in fixture.published[:3]] == ["provider_wait"] * 3
    claimed = json.loads(fixture.manifest_path.read_text())
    assert claimed["resume_not_before_utc"] == deadline.isoformat()


@pytest.mark.parametrize("interrupt", ["stop", "owner"])
def test_bridge_wait_interruption_returns_paused_without_country_dispatch(drained, clock, interrupt):
    fixture = drained
    owner(fixture.base)
    fixture.manifest["resume_not_before_utc"] = (NOW + timedelta(seconds=5)).isoformat()
    save(fixture)
    def interrupt_once():
        if interrupt == "stop":
            (fixture.base / f"ui-preparation-progress-{NEW_JOB}.stop").write_text("stop")
        else:
            owner(fixture.base, job_id="f" * 32)
    clock.after_sleep = interrupt_once
    result = invoke(fixture)
    assert fixture.dispatch == [] and clock.sleeps == [1.0]
    assert result["status"] == "paused" and result["pause_reason"] == "stop_requested"
    assert result["active_workers"] == 0 and result["active_rows"] == []
    assert not any(event[0] in {"recover", "attach", "enroll"} for event in fixture.factory.events)

