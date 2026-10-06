import json

import pytest

from anghami_session.bandwidth import NetworkUsageMeter
from anghami_session.legacy_network_reporting import enrich_job_usage


JOB_ID = "a" * 32


def job(rows=None, count=1, completed=None, **extra):
    rows = rows if rows is not None else [1]
    return {"id": JOB_ID, "action": "play", "song_id": "123", "rows": rows,
            "count": count, "completed_tests": len(rows) * count if completed is None else completed,
            "proxy_egypt": True, "results": [], **extra}


def legacy(row, number=1, **extra):
    return {"source_row": row, "test_number": number, "song_id": "123",
            "bandwidth": {"total": {"request_count": 2, "request_bytes": 110,
                                     "upload_body_bytes": 10, "download_body_bytes": 200,
                                     "response_header_bytes": 30, "measurement_complete": True}}, **extra}


def write_report(base, row, number=1, report=None, job_id=JOB_ID):
    directory = base / "ui-test-reports" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"account-{row}.play-{number}.redacted.json"
    path.write_text(json.dumps(report if report is not None else legacy(row, number)), encoding="utf-8")
    return path


def measured():
    meter = NetworkUsageMeter()
    meter._record({"request_bytes": 110, "upload_body_bytes": 10,
                   "download_body_bytes": 200, "response_header_bytes": 30}, True, False)
    return meter.snapshot()


def test_all_reports_count_beyond_retained_ui_tail(tmp_path):
    for row in range(1, 506):
        write_report(tmp_path, row)
    original = job(list(range(1, 506)), results=[legacy(505)])
    enriched = enrich_job_usage(original, tmp_path)
    usage = enriched["network_usage"]
    assert usage["total_bytes"] == 505 * 340
    assert usage["sampled_tests"] == 505
    assert usage["measurement"] == "partial"
    assert usage["cost_usd"] == pytest.approx(505 * 340 / 1e9)
    assert "network_usage" not in original
    assert len(enriched["results"]) == 1


def test_each_check_and_cumulative_account_usage(tmp_path):
    for number in (1, 2, 3):
        write_report(tmp_path, 1, number)
    result = enrich_job_usage(job(count=3, results=[legacy(1, 3)]), tmp_path)["results"][0]
    assert result["network_usage"]["total_bytes"] == 340
    assert result["account_network_usage"]["total_bytes"] == 1020
    assert result["account_tests_completed"] == 3


def test_account_finished_count_includes_reports_without_measurements(tmp_path):
    write_report(tmp_path, 1)
    for number in (2, 3):
        write_report(tmp_path, 1, number, report={"source_row": 1, "test_number": number, "song_id": "123"})
    result = enrich_job_usage(job(count=3, results=[legacy(1)]), tmp_path)["results"][0]
    assert result["account_tests_completed"] == 3
    assert result["account_network_usage"]["sampled_tests"] == 1
    assert result["account_network_usage"]["total_bytes"] == 340
    assert result["account_network_usage"]["measurement"] == "partial"


def test_wrong_song_retained_result_never_gets_file_measurement(tmp_path):
    write_report(tmp_path, 1)
    result = enrich_job_usage(job(results=[legacy(1, song_id="999")]), tmp_path)["results"][0]
    assert "network_usage" not in result
    assert "account_network_usage" not in result


def test_failed_legacy_transfer_keeps_known_lower_bound(tmp_path):
    report = legacy(1)
    report["bandwidth"] = {
        "total": {"request_count": 2, "request_bytes": None, "upload_body_bytes": None,
                  "download_body_bytes": None, "response_header_bytes": None},
        "get_song": {"request_count": 1, "request_bytes": 100, "upload_body_bytes": 0,
                     "download_body_bytes": 200, "response_header_bytes": 30},
        "play_song": {"request_count": 1, "request_bytes": None, "upload_body_bytes": None,
                      "download_body_bytes": None, "response_header_bytes": None},
    }
    write_report(tmp_path, 1, report=report)
    usage = enrich_job_usage(job(), tmp_path)["network_usage"]
    assert usage["total_bytes"] == 330
    assert usage["unmeasured_requests"] == 1
    assert usage["measurement"] == "partial"


@pytest.mark.parametrize("changes", [{"song_id": "999"}, {"source_row": 2}, {"test_number": 2}])
def test_file_content_must_match_frozen_job(tmp_path, changes):
    write_report(tmp_path, 1, report=legacy(1, **changes) if "source_row" not in changes
                 else {**legacy(1), **changes})
    assert "network_usage" not in enrich_job_usage(job(), tmp_path)


def test_unselected_and_other_job_reports_are_excluded(tmp_path):
    write_report(tmp_path, 2)
    write_report(tmp_path, 1, job_id="b" * 32)
    assert "network_usage" not in enrich_job_usage(job(), tmp_path)


def test_cache_refreshes_after_incomplete_report_is_replaced(tmp_path):
    path = write_report(tmp_path, 1)
    assert enrich_job_usage(job(), tmp_path)["network_usage"]["total_bytes"] == 340
    path.write_text("{", encoding="utf-8")
    assert "network_usage" not in enrich_job_usage(job(), tmp_path)
    path.write_text(json.dumps(legacy(1)), encoding="utf-8")
    assert enrich_job_usage(job(), tmp_path)["network_usage"]["total_bytes"] == 340


def test_primary_measurement_preserves_setup_and_is_not_added_again(tmp_path):
    write_report(tmp_path, 1)
    primary = measured()
    enriched = enrich_job_usage(job(network_usage=primary, results=[legacy(1)]), tmp_path)
    assert enriched["network_usage"]["total_bytes"] == 340
    assert enriched["network_usage"]["scope"] == "python_http"
    assert enriched["network_usage"]["measurement"] == "measured"


def test_partial_snapshot_coverage_and_direct_cost(tmp_path):
    write_report(tmp_path, 1)
    usage = enrich_job_usage(job([1, 2], proxy_egypt=False), tmp_path)["network_usage"]
    assert usage["sampled_tests"] == 1
    assert usage["completed_tests"] == 2
    assert usage["direct_bytes"] == 340
    assert usage["cost_usd"] == 0


def test_report_before_progress_snapshot_is_counted_once(tmp_path):
    write_report(tmp_path, 1)
    usage = enrich_job_usage(job(completed=0, results=[legacy(1)]), tmp_path)["network_usage"]
    assert usage["completed_tests"] == usage["sampled_tests"] == 1
    assert usage["total_bytes"] == 340


def test_only_numeric_measurement_metadata_survives(tmp_path):
    usage = {**measured(), "cookie": "SECRET", "url": "SECRET", "cost_usd": 9999}
    write_report(tmp_path, 1, report=legacy(1, network_usage=usage))
    enriched = enrich_job_usage(job(results=[legacy(1)]), tmp_path)
    assert "SECRET" not in json.dumps(enriched["network_usage"])
    assert enriched["network_usage"]["cost_usd"] == pytest.approx(340 / 1e9)


@pytest.mark.parametrize("invalid", [{"id": "../private"}, {"rows": [1, 1]}, {"count": 0}, {"completed_tests": 3},
                                     {"song_id": "0"}, {"song_id": "0123"}, {"song_id": str(2**63)}])
def test_invalid_job_never_scans_reports(tmp_path, invalid):
    write_report(tmp_path, 1)
    assert "network_usage" not in enrich_job_usage(job(**invalid), tmp_path)
