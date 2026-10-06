"""Synthetic per-song history skips, identity aliases, and graceful job stops."""

from copy import deepcopy
import json
import threading

import pytest

from anghami_session import ui_jobs, ui_server
from anghami_session.like_history import LikeHistoryError, local_history_report
from anghami_session.play_record import TEST_SONG_ID
from anghami_session.test_settings import write_test_song_id


class HistoryFactory:
    def __init__(self):
        self.lock = threading.RLock()
        self.rows = [1, 2, 3]
        self.identities = {row: f"synthetic-{row}@example.invalid" for row in self.rows}
        self.states = {}
        self.calls = []
        self.race_state = None
        self.invalid = False
        self.gate = None
        self.storage_error = False

    def __call__(self, _path):
        return HistoryVault(self)

    def like_history(self, *_):
        # This capability marker also exercises deferred proxy loading.
        raise AssertionError("Read history through the worker-owned vault.")


class HistoryVault:
    def __init__(self, factory):
        self.factory = factory

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def record(self, row):
        return {"email": self.factory.identities[row], "password": "private-synthetic-password"}

    def enrolled_test_rows(self):
        return self.factory.rows

    def test_accounts(self):
        return {"ready_rows": self.factory.rows}

    def like_history(self, song_id, rows=None):
        accounts = []
        for row in rows:
            state = self.factory.states.get((self.factory.identities[row], song_id))
            if state:
                accounts.append({"source_row": row, "state": state, "identity_key": "PRIVATE", "last_checked_at": "2026-10-04T10:00:00+00:00"})
        if self.factory.invalid:
            accounts.append({"source_row": 999, "state": "confirmed"})
        return {"song_id": song_id, "accounts": accounts}

    def test_like(self, row, song_id, **_):
        if self.factory.storage_error:
            raise LikeHistoryError()
        if self.factory.race_state:
            return local_history_report(row, song_id, self.factory.race_state)
        with self.factory.lock:
            self.factory.calls.append(("like", row, song_id))
        if self.factory.gate:
            self.factory.gate[0].set()
            assert self.factory.gate[1].wait(5)
        with self.factory.lock:
            self.factory.states[(self.factory.identities[row], song_id)] = "confirmed"
        return {"source_row": row, "song_id": song_id, "passed": True,
                "authenticated": True, "server_account_identity_verified": True,
                "liked_before": False, "liked_after": True, "persisted_state_verified": True,
                "mutation_attempted": True, "mutation_attempts": 1, "mutation_accepted": True,
                "mutation_result": "accepted", "mutation_http_status": 200}

    def test_play_record(self, row, song_id, **_):
        self.factory.calls.append(("play", row, song_id))
        return {"source_row": row, "song_id": song_id, "passed": True,
                "event_attempted": True, "event_result": "accepted", "event_accepted": True}


def manager(tmp_path, factory):
    return ui_jobs.JobManager(tmp_path / "fake.sqlite3", vault_factory=factory,
                             proxy_loader=lambda *_: pytest.fail("Cached history opened proxy credentials"),
                             test_proxy_loader=lambda *_: pytest.fail("Cached history opened test routes"))


def finish(instance):
    instance._thread.join(10)
    assert not instance._thread.is_alive()
    result = instance.snapshot()
    assert "private-synthetic" not in json.dumps(result)
    assert "PRIVATE" not in json.dumps(result)
    return result


@pytest.mark.parametrize("workers", [1, 3])
def test_like_identity_aliases_and_requested_repeats_issue_only_one_fresh_check_each(tmp_path, workers):
    factory = HistoryFactory()
    factory.identities[2] = factory.identities[1].upper()
    instance = manager(tmp_path, factory)
    instance.submit({"action": "like", "rows": [1, 2, 3], "count": 5, "workers": workers})
    job = finish(instance)
    assert job["status"] == "succeeded"
    assert job["selected_accounts"] == 3
    assert job["eligible_accounts"] == 2
    assert job["rows"] == [1, 3]
    assert job["selected_rows"] == [1, 2, 3]
    assert job["requested_tests"] == 15
    assert job["requested_tests_per_account"] == 5
    assert job["duplicate_like_skipped_tests"] == 13
    assert job["count"] == 1
    assert job["writes_accepted"] == job["new_likes_verified"] == 2
    assert len(factory.calls) == 2


@pytest.mark.parametrize("proxy_option", ["proxy_egypt", "proxy_test_session"])
def test_confirmed_accounts_skip_locally_without_any_proxy_or_http_dependency(tmp_path, proxy_option):
    factory = HistoryFactory()
    for email in factory.identities.values():
        factory.states[(email, TEST_SONG_ID)] = "confirmed"
    instance = manager(tmp_path, factory)
    instance.submit({"action": "like", "rows": factory.rows, "count": 5, "workers": 8, proxy_option: True})
    job = finish(instance)
    assert job["status"] == "succeeded"
    assert job["progress"] == {"completed": 0, "total": 0}
    assert job["already_liked_skipped"] == 3
    assert job["history_skipped_tests"] == 15
    assert job["eligible_accounts"] == job["writes_accepted"] == job["new_likes_verified"] == job["attempted"] == 0
    assert job["network_usage"]["request_count"] == 0
    assert not factory.calls


def test_pending_and_unknown_accounts_are_held_without_becoming_fresh_failures(tmp_path):
    factory = HistoryFactory()
    for row, state in zip(factory.rows, ("verification_pending", "write_unknown", "in_progress")):
        factory.states[(factory.identities[row], TEST_SONG_ID)] = state
    instance = manager(tmp_path, factory)
    instance.submit({"action": "like", "rows": factory.rows, "workers": 8, "proxy_egypt": True})
    job = finish(instance)
    assert job["status"] == "completed_with_pending"
    assert job["like_verification_held"] == 1 and job["unknown_like_held"] == 2
    assert job["history_held_tests"] == 3
    assert job["failed"] == job["account_failed"] == job["consecutive_failures"] == 0
    assert not factory.calls


def test_history_is_bound_to_song_and_does_not_filter_play_jobs(tmp_path):
    factory = HistoryFactory()
    for email in factory.identities.values():
        factory.states[(email, TEST_SONG_ID)] = "confirmed"
    instance = manager(tmp_path, factory)
    instance.submit({"action": "play", "rows": factory.rows})
    played = finish(instance)
    assert played["writes_accepted"] == 3
    different = write_test_song_id("1280677978", tmp_path / "test-settings.json")
    instance.submit({"action": "like", "rows": factory.rows, "song_id": different})
    liked = finish(instance)
    assert liked["already_liked_skipped"] == 0
    assert liked["new_likes_verified"] == 3


def test_invalid_history_stops_before_proxy_or_like(tmp_path):
    factory = HistoryFactory()
    factory.invalid = True
    instance = manager(tmp_path, factory)
    instance.submit({"action": "like", "rows": factory.rows, "proxy_egypt": True})
    job = finish(instance)
    assert job["status"] == "failed" and job["error"]["code"] == "like_history_invalid"
    assert not factory.calls


@pytest.mark.parametrize("workers", [1, 3])
@pytest.mark.parametrize("state", ["confirmed", "verification_pending", "write_unknown", "in_progress"])
def test_history_changes_after_selection_still_never_count_as_fresh_likes(tmp_path, workers, state):
    factory = HistoryFactory()
    factory.race_state = state
    instance = manager(tmp_path, factory)
    instance.submit({"action": "like", "rows": factory.rows, "workers": workers})
    job = finish(instance)
    assert job["status"] == ("succeeded" if state == "confirmed" else "completed_with_pending")
    assert job["writes_attempted"] == job["writes_accepted"] == job["new_likes_verified"] == job["already_liked_verified"] == 0
    assert job["completed_tests"] == job["attempted"] == 0
    assert job["cached_like_skips"] + job["cached_like_holds"] == 3
    assert not factory.calls
    assert all(report["history_skipped"] is True for report in job["results"])


def test_history_storage_failure_keeps_immediate_global_hold(tmp_path):
    factory = HistoryFactory()
    factory.storage_error = True
    instance = manager(tmp_path, factory)
    instance.submit({"action": "like", "rows": factory.rows, "workers": 1, "max_consecutive_failures": 50})
    job = finish(instance)
    assert job["status"] == "failed" and job["error"]["code"] == "journal_failed"
    assert job["stop_reason"] == "journal_failed"
    assert job["writes_accepted"] == 0


def test_public_history_is_row_only_per_song_and_drops_raw_metadata():
    value = {"song_id": TEST_SONG_ID, "accounts": [
        {"source_row": 1, "state": "confirmed", "identity_key": "PRIVATE", "email": "PRIVATE"},
        {"source_row": 2, "state": "verification_pending"}, {"source_row": 3, "state": "write_unknown"},
        {"source_row": 4, "state": ["PRIVATE"]}]}
    public = ui_server.safe_like_history(value, TEST_SONG_ID, [1, 2, 3, 4, 5])
    assert public["confirmed_rows"] == [1] and public["verification_pending_rows"] == [2] and public["unknown_rows"] == [3]
    assert public["eligible_rows"] == [4, 5]
    assert "PRIVATE" not in json.dumps(public)


@pytest.mark.parametrize("action", ["play", "like"])
def test_stop_request_drains_active_test_and_never_dispatches_next_identity(tmp_path, action):
    factory = HistoryFactory()
    factory.gate = (threading.Event(), threading.Event())
    if action == "play":
        original = HistoryVault.test_play_record
        def gated(self, *args, **kwargs):
            self.factory.gate[0].set()
            assert self.factory.gate[1].wait(5)
            return original(self, *args, **kwargs)
        from unittest.mock import patch
        context = patch.object(HistoryVault, "test_play_record", gated)
    else:
        from contextlib import nullcontext
        context = nullcontext()
    with context:
        instance = manager(tmp_path, factory)
        queued = instance.submit({"action": action, "rows": factory.rows, "workers": 1})
        assert factory.gate[0].wait(5)
        stopped = instance.request_stop(queued["id"])
        assert stopped["stop_requested"] is True
        factory.gate[1].set()
        job = finish(instance)
    assert job["status"] == "stopped" and job["stop_reason"] == "user_stop"
    assert job["writes_accepted"] == 1
    assert job["active_workers"] == 0
    assert len(factory.calls) == 1


def test_stop_control_rejects_wrong_identity_and_non_test_action(tmp_path):
    instance = manager(tmp_path, HistoryFactory())
    with pytest.raises(ui_jobs.JobValidationError):
        instance.request_stop("a" * 32)
    instance._latest = {"id": "a" * 32, "action": "preview", "status": "running"}
    with pytest.raises(ui_jobs.JobValidationError):
        instance.request_stop("a" * 32)
    service = ui_server.ConsoleService(tmp_path / "fake.sqlite3", manager=instance)
    with pytest.raises(ValueError):
        service.stop_job({"job_id": "a" * 32, "rows": [1]})


def test_cancelled_no_write_reports_remain_distinct_and_safe():
    report = {"outcome": "cancelled", "error_code": "cancelled", "history_status": "confirmed",
              "stop_requested": True, "mutation_attempted": False, "mutation_accepted": False,
              "mutation_attempts": 0, "mutation_result": "not_attempted", "cookies": "PRIVATE"}
    assert ui_jobs._public_report(report)["outcome"] == "cancelled"
    assert ui_server.safe_report(report)["outcome"] == "cancelled"
    assert "PRIVATE" not in json.dumps(ui_server.safe_report(report))
    assert ui_jobs._write_counts("like", report)["writes_accepted"] == 0


def test_untrusted_history_label_never_hides_a_claimed_fresh_write():
    report = {"history_skipped": True, "mutation_attempted": True, "mutation_attempts": 1,
              "mutation_accepted": True, "mutation_result": "accepted"}
    assert ui_jobs._write_counts("like", report)["writes_attempted"] == 1


def test_stop_api_requires_bound_job_and_preserves_token_checks(console, tmp_path):
    import http.client
    from test_ui_server import request
    instance = manager(tmp_path, HistoryFactory())
    instance._latest = {"id": "a" * 32, "action": "like", "status": "queued", "stop_requested": False}
    console.server.service = ui_server.ConsoleService(tmp_path / "fake.sqlite3", manager=instance)
    status, _, content = request(console, "POST", "/api/jobs/stop", body={"job_id": "a" * 32})
    assert status == 202
    assert json.loads(content)["job"]["stop_requested"] is True
    status, _, _ = request(console, "POST", "/api/jobs/stop", body={"job_id": "b" * 32})
    assert status == 400
    # Send the rejection probe explicitly: no helper may add X-App-Token.
    connection = http.client.HTTPConnection("127.0.0.1", console.port, timeout=3)
    try:
        connection.request("POST", "/api/jobs/stop", body=json.dumps({"job_id": "a" * 32}),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read())["error"]["code"] == "token_rejected"
    finally:
        connection.close()


# The existing loopback fixture exposes no external endpoints or real vault.
from test_ui_server import console
