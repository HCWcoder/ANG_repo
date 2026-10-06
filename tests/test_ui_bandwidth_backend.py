"""Offline transfer totals, sample projections, and safe UI serialization."""

import json
import threading
from types import SimpleNamespace

import pytest

from anghami_session import ui_jobs, ui_server
from anghami_session.bandwidth import NetworkUsageMeter, measured_request


def usage(*, proxy=True, count=1, partial=False, unavailable=False):
    meter = NetworkUsageMeter()
    counters = {"request_bytes": 100, "upload_body_bytes": 20,
                "download_body_bytes": 300, "response_header_bytes": 50}
    if partial:
        counters["download_body_bytes"] = None
    if unavailable:
        counters = dict.fromkeys(counters)
    for _ in range(count):
        meter._record(counters, proxy, unavailable)
    return meter.snapshot()


class SyntheticTransport:
    proxies = {}

    def get(self):
        return SimpleNamespace(request_size=100, upload_size=20, download_size=300, header_size=50,
                               url="https://secret.invalid/?sid=secret", cookies="secret")


def transfer(*, proxy=True):
    return measured_request(SyntheticTransport(), "get", proxy_used=proxy)


def test_public_usage_recomputes_cost_and_discards_arbitrary_fields():
    raw = {**usage(), "cost_usd": 500, "price_usd_per_gb": 1000,
           "url": "secret", "sid": "secret", "headers": {"Cookie": "secret"}}
    safe = ui_jobs.safe_network_usage(raw)
    assert safe["sent_bytes"] == 100  # upload is already inside REQUEST_SIZE
    assert safe["total_bytes"] == 450
    assert safe["cost_usd"] == pytest.approx(450 / 1e9)
    assert safe["price_usd_per_gb"] == 1
    assert "secret" not in json.dumps(safe)
    assert ui_jobs._public_report({"network_usage": raw})["network_usage"] == safe
    assert ui_server.safe_report({"network_usage": raw})["network_usage"] == safe


@pytest.mark.parametrize("field,value", [
    ("total_bytes", True), ("request_count", -1), ("sent_bytes", float("nan")),
    ("proxy_bytes", "secret"), ("direct_bytes", 20), ("measured_requests", 10),
    ("scope", "https://secret.invalid"), ("measurement", "fully billed"),
])
def test_invalid_usage_cannot_reach_results(field, value):
    raw = {**usage(), field: value}
    assert ui_jobs.safe_network_usage(raw) == {}
    assert ui_server.safe_report({"network_usage": raw}) == {"network_usage": {}}


def test_valid_samples_produce_decimal_gb_estimates_and_unique_account_average():
    summary = ui_jobs.summarize_network_usage([(7, usage()), (7, usage(count=2)), (8, usage()), (9, usage(unavailable=True))],
        total_tests=1000, completed_tests=4, setup_usage=usage(proxy=False))
    assert summary["total_bytes"] == 2250
    assert summary["proxy_bytes"] == 1800
    assert summary["direct_bytes"] == 450
    assert summary["sampled_tests"] == 3
    assert summary["sampled_accounts"] == 2
    assert summary["completed_tests"] == 4
    assert summary["measurement"] == "partial"
    assert summary["avg_bytes_per_test"] == 600
    assert summary["avg_bytes_per_account"] == 900
    assert summary["cost_usd"] == pytest.approx(0.0000018)
    assert summary["estimated_total_bytes"] == 600450
    assert summary["estimated_remaining_bytes"] == 597600
    assert summary["estimated_total_cost_usd"] == pytest.approx(0.0006)
    assert summary["estimated_remaining_cost_usd"] == pytest.approx(0.0005976)
    injected = {**summary, "avg_bytes_per_test": 999, "estimated_total_bytes": 999,
                "estimated_total_cost_usd": 999, "cost_usd": 999}
    assert ui_jobs.safe_network_usage(injected) == summary


def test_direct_traffic_has_zero_provider_cost_and_missing_stats_are_not_zero_billing():
    direct = ui_jobs.summarize_network_usage([(1, usage(proxy=False))], total_tests=1000, completed_tests=1)
    assert direct["total_bytes"] == 450
    assert direct["cost_usd"] == 0
    assert direct["estimated_total_cost_usd"] == 0
    missing = ui_jobs.summarize_network_usage([(1, usage(unavailable=True))], total_tests=1000, completed_tests=1)
    assert missing["measurement"] == "unavailable"
    assert missing["sampled_tests"] == 0
    assert "cost_usd" not in missing
    assert "avg_bytes_per_test" not in missing
    assert "estimated_total_bytes" not in missing


def test_missing_finished_report_cannot_make_run_coverage_complete():
    summary = ui_jobs.summarize_network_usage([(7, usage()), (8, {})], total_tests=10, completed_tests=2)
    assert summary["scope"] == "python_http"
    assert summary["measurement"] == "partial"
    assert summary["sampled_tests"] == 1
    assert summary["completed_tests"] == 2
    assert summary["request_count"] == 1  # Never invent requests for the missing report.
    assert summary["total_bytes"] == 450
    assert ui_jobs.safe_network_usage(summary) == summary


def test_legacy_total_is_partial_and_upload_body_is_not_counted_twice():
    counts = {"request_count": 2, "request_bytes": 1000, "upload_body_bytes": 400,
              "download_body_bytes": 200, "response_header_bytes": 100}
    old = {"bandwidth": {"total": counts, "get_song": counts, "play_song": counts}}
    result = ui_jobs.legacy_network_usage(old, proxy_used=True)
    assert result["measurement"] == "partial"
    assert result["scope"] == "legacy_play_requests"
    assert result["request_count"] == 2
    assert result["total_bytes"] == 1300
    assert result["proxy_bytes"] == 1300


def test_legacy_failed_event_preserves_known_metadata_and_marks_missing_counter_coverage():
    metadata = {"request_count": 1, "request_bytes": 100, "upload_body_bytes": 0,
                "download_body_bytes": 300, "response_header_bytes": 50}
    failure = {"request_count": 1, "request_bytes": None, "upload_body_bytes": None,
               "download_body_bytes": None, "response_header_bytes": None}
    old = {"bandwidth": {"get_song": metadata, "play_song": failure,
                         "total": {**failure, "request_count": 2}}}
    result = ui_jobs.legacy_network_usage(old, proxy_used=True)
    assert result["measurement"] == "partial"
    assert result["total_bytes"] == 450
    assert result["request_count"] == 2
    assert result["partial_requests"] == result["unmeasured_requests"] == 1
    assert result["cost_usd"] == pytest.approx(450 / 1e9)


def manager_job(tmp_path, *, total=600):
    manager = ui_jobs.JobManager(tmp_path / "fake.sqlite3")
    manager._latest = {"id": "a" * 32, "action": "play", "status": "running", "results": [],
                       "results_total": 0, "results_truncated": 0, "progress": {"completed": 0, "total": total}}
    return manager


def test_all_results_remain_in_total_after_recent_tail_trims_and_each_report_counts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_jobs, "_journal", lambda *_: None)
    manager = manager_job(tmp_path)
    for row in range(1, 504):
        manager._append({"source_row": row, "test_number": 1, "network_usage": usage()}, completed=row)
    job = manager.snapshot()
    assert len(job["results"]) == 500
    assert job["results_total"] == 503
    assert job["results_truncated"] == 3
    assert job["network_usage"]["total_bytes"] == 503 * 450
    assert job["network_usage"]["sampled_tests"] == job["network_usage"]["sampled_accounts"] == 503
    assert job["network_usage"]["completed_tests"] == 503
    manager._append({"source_row": 1, "test_number": 1, "network_usage": usage()})
    assert manager.snapshot()["network_usage"]["total_bytes"] == 503 * 450


def test_per_account_usage_keeps_repeated_checks_and_retry_bytes_together(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_jobs, "_journal", lambda *_: None)
    manager = manager_job(tmp_path, total=2)
    manager._append({"source_row": 7, "test_number": 1, "network_usage": usage(count=2)})
    first = manager.snapshot()["results"][-1]
    assert first["account_tests_completed"] == 1
    assert "account_network_usage" not in first
    manager._append({"source_row": 7, "test_number": 2, "network_usage": usage()})
    final = manager.snapshot()["results"][-1]
    assert final["network_usage"]["total_bytes"] == 450
    assert final["account_network_usage"]["total_bytes"] == 1350
    assert final["account_tests_completed"] == 2
    assert manager.snapshot()["network_usage"]["avg_bytes_per_account"] == 1350
    assert ui_server.safe_report(final)["account_network_usage"]["total_bytes"] == 1350


def test_test_scope_includes_retry_transfers_but_isolates_setup_meter(tmp_path, monkeypatch):
    manager = manager_job(tmp_path, total=1)
    def retrying(*_, **__):
        transfer()
        transfer()
        return {"kind": "succeeded", "report": {"passed": True, "retry_count": 1}}
    monkeypatch.setattr(manager, "_test_once_unmetered", retrying)
    from anghami_session.bandwidth import measure_network_usage
    with measure_network_usage() as setup:
        transfer(proxy=False)
        result = manager._test_once({}, None, 7, 1, "synthetic", threading.Event())
    assert result["report"]["network_usage"]["request_count"] == 2
    assert result["report"]["network_usage"]["total_bytes"] == 900
    assert setup.snapshot()["request_count"] == 1
    assert setup.snapshot()["total_bytes"] == 450


def test_unhandled_worker_failure_retains_numeric_transfer_usage(tmp_path, monkeypatch):
    manager = manager_job(tmp_path, total=1)
    def failing(*_, **__):
        transfer()
        raise RuntimeError("secret")
    monkeypatch.setattr(manager, "_test_once_unmetered", failing)
    with pytest.raises(RuntimeError) as caught:
        manager._test_once({}, None, 7, 1, "synthetic", threading.Event())
    assert caught.value._network_usage["total_bytes"] == 450
    assert "secret" not in json.dumps(caught.value._network_usage)


class MeteredVault:
    def __init__(self, _path):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def record(self, row):
        return {"email": f"synthetic-{row}@example.invalid", "password": "secret"}

    def enrolled_test_rows(self):
        return [7, 8]

    def test_accounts(self):
        return {"ready_rows": [7, 8]}

    def test_play_record(self, row, song_id, **_):
        for _ in range(3):
            transfer(proxy=False)
        return {"passed": True, "source_row": row, "song_id": song_id,
                "event_attempted": True, "event_accepted": True, "event_result": "accepted"}


@pytest.mark.parametrize("workers", [1, 2])
def test_end_to_end_serial_and_parallel_totals_preserve_original_run_counts(tmp_path, workers):
    manager = ui_jobs.JobManager(tmp_path / "fake.sqlite3", vault_factory=MeteredVault)
    manager.submit({"action": "play", "rows": [7, 8], "count": 2,
                    "workers": workers, "max_consecutive_failures": 1})
    manager._thread.join(10)
    assert not manager._thread.is_alive()
    job = manager.snapshot()
    assert job["status"] == "succeeded"
    assert job["writes_accepted"] == job["completed_tests"] == 4
    assert job["network_usage"]["request_count"] == 12
    assert job["network_usage"]["total_bytes"] == 5400
    assert job["network_usage"]["setup_bytes"] == 0
    assert job["network_usage"]["cost_usd"] == 0
    assert job["network_usage"]["avg_bytes_per_test"] == 1350
    assert job["network_usage"]["avg_bytes_per_account"] == 2700
    assert all(item["network_usage"]["total_bytes"] == 1350 for item in job["results"])
    reports = list((tmp_path / "ui-test-reports" / job["id"]).glob("*.json"))
    assert len(reports) == 4
    assert all("network_usage" in json.loads(path.read_text()) for path in reports)
    assert "secret" not in manager._report_path.read_text()


def test_fatal_read_only_child_keeps_known_transfer_in_run_total_without_false_sample(tmp_path):
    class FailingSession:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def check(self, **_):
            transfer(proxy=False)
            raise ui_jobs._JobFailure({"code": "read_scope_invalid", "message": "The read-only response scope changed."})

    class ReadVault(MeteredVault):
        def _http_session(self, *_):
            return FailingSession()

    manager = ui_jobs.JobManager(tmp_path / "fake.sqlite3", vault_factory=ReadVault)
    manager.submit({"action": "check", "rows": [7]})
    manager._thread.join(10)
    job = manager.snapshot()
    assert job["status"] == "failed"
    assert job["error"]["code"] == "read_scope_invalid"
    assert job["network_usage"]["total_bytes"] == 450
    assert job["network_usage"]["setup_bytes"] == 450
    assert job["network_usage"]["sampled_tests"] == 0
    assert "estimated_total_bytes" not in job["network_usage"]

