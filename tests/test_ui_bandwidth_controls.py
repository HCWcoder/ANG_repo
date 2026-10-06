"""Bandwidth presentation exercises the real UI with an offline DOM and synthetic jobs."""

import json
from pathlib import Path
import subprocess

import pytest

from test_ui_provider_review import APP, DRIVER, NODE


def browser_bandwidth(**inputs):
    if NODE is None:
        pytest.skip("Node.js is needed to exercise actual browser handlers")
    driver = DRIVER.replace(
        "process.stdout.write(JSON.stringify({calls,",
        "process.stdout.write(JSON.stringify({reportText:get('report-summary').textContent,bandwidth:get('job-bandwidth').textContent,bandwidthHidden:get('job-bandwidth').hidden,calls,",
    )
    if inputs.get("report") is not None:
        driver = driver.replace("renderFailureReview,startReviewPreparation,renderRunVisibility,renderJob,resultStatus,",
                                "renderFailureReview,startReviewPreparation,renderRunVisibility,renderJob,resultStatus,loadReport,")
        driver = driver.replace(
            "return {ok:true,status:202,json:async()=>({job:{id:'synthetic-review-job',action:'prepare',status:'queued',phase:'queued',progress:{completed:0,total:1},results:[]}})};",
            "return {ok:true,status:200,json:async()=>({report:input.report,name:'synthetic-report.json'})};",
        )
        driver = driver.replace("if(input.render_job) hooks.renderJob(input.render_job);",
                                "if(input.render_job) hooks.renderJob(input.render_job); if(input.report) await hooks.loadReport('synthetic-report.json');")
    completed = subprocess.run([NODE, "-e", driver, str(APP)], input=json.dumps(inputs), text=True,
                               encoding="utf-8", capture_output=True, timeout=10, check=False)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def network_usage(**changes):
    return {
        "measurement": "measured", "scope": "python_http", "sent_bytes": 1000,
        "received_bytes": 24000, "total_bytes": 25000, "proxy_bytes": 25000, "direct_bytes": 0,
        "request_count": 5, "measured_requests": 5, "partial_requests": 0, "unmeasured_requests": 0,
        "transport_errors": 0, **changes,
    }


def job(**changes):
    usage = network_usage(sent_bytes=2000, received_bytes=48000, total_bytes=50000,
                          proxy_bytes=50000, cost_usd=0.00005, price_usd_per_gb=1,
                          sampled_tests=2, completed_tests=2, sampled_accounts=2,
                          avg_bytes_per_test=25000, avg_bytes_per_account=25000,
                          estimated_total_bytes=100000, estimated_total_cost_usd=0.0001)
    return {
        "id": "synthetic-bandwidth-run", "action": "play", "status": "running",
        "attempted": 3, "completed_tests": 2, "succeeded": 2, "failed": 0,
        "progress": {"completed": 2, "total": 4}, "active_workers": 1,
        "workers": 2, "count": 1, "max_consecutive_failures": 20, "results_total": 2,
        "network_usage": usage, "proxy_egypt": True,
        "results": [{"source_row": 7, "test_number": 1, "passed": True,
                     "network_usage": network_usage()}], **changes,
    }


def test_live_results_show_total_each_account_and_projected_cost_at_decimal_gb_rate():
    shown = browser_bandwidth(render_job=job())
    assert not shown["bandwidthHidden"]
    assert "Observed HTTP transfer 50.00 KB" in shown["bandwidth"]
    assert "Estimated proxy cost $0.000050" in shown["bandwidth"]
    assert "Upload 2.00 KB" in shown["bandwidth"] and "Download 48.00 KB" in shown["bandwidth"]
    assert "Average / check 25.00 KB" in shown["bandwidth"]
    assert "Average / account 25.00 KB" in shown["bandwidth"]
    assert "Projected full run 100.00 KB" in shown["bandwidth"]
    assert "Projected proxy cost $0.000100" in shown["bandwidth"]
    assert "Usage recorded for 2 of 2 finished checks." in shown["bandwidth"]
    assert "Active checks are added when they finish." in shown["bandwidth"]
    assert "HTTP transfer estimate; PacketStream billing may differ." in shown["bandwidth"]
    assert "$1 per GB (1,000,000,000 bytes)" in shown["bandwidth"]
    assert "Account 7 · Run 1" in shown["results"]
    assert "HTTP usage: 25.00 KB · Est. proxy cost: $0.000025" in shown["results"]
    assert not shown["calls"]


def test_partial_legacy_run_explains_excluded_requests_and_lower_bound_projection():
    original = job()
    original["network_usage"].update(measurement="partial", scope="legacy_play_requests",
                                     sampled_tests=1, completed_tests=2)
    original["results"][0]["network_usage"].update(measurement="partial", scope="legacy_play_requests")
    shown = browser_bandwidth(render_job=original)
    assert "Recorded HTTP transfer (minimum) 50.00 KB" in shown["bandwidth"]
    assert "Est. proxy cost (minimum) $0.000050" in shown["bandwidth"]
    assert "Projected full run (minimum) 100.00 KB" in shown["bandwidth"]
    assert "Usage recorded for 1 of 2 finished checks." in shown["bandwidth"]
    assert "Only metadata and play requests were recorded; session checks, login and connection retries are excluded." in shown["bandwidth"]
    assert "HTTP usage: 25.00 KB recorded (partial)" in shown["results"]
    assert "Est. proxy cost: $0.000025 (minimum)" in shown["results"]


def test_partial_current_run_explains_failed_transfers_and_not_fully_measured_totals():
    original = job()
    original["network_usage"]["measurement"] = "partial"
    shown = browser_bandwidth(render_job=original)
    assert "Some requests or failed transfers were not fully measured." in shown["bandwidth"]
    assert "Totals and projections are minimum estimates." in shown["bandwidth"]


def test_unknown_route_classification_keeps_observed_bytes_but_marks_cost_as_minimum():
    original = job()
    original["network_usage"]["unknown_route_bytes"] = 5000
    original["results"][0]["network_usage"]["unknown_route_bytes"] = 2500
    shown = browser_bandwidth(render_job=original)
    assert "Observed HTTP transfer 50.00 KB" in shown["bandwidth"]
    assert "Est. proxy cost (minimum) $0.000050" in shown["bandwidth"]
    assert "Projected proxy cost (minimum) $0.000100" in shown["bandwidth"]
    assert "Some measured traffic could not be classified as direct or proxy." in shown["bandwidth"]
    assert "HTTP usage: 25.00 KB · Est. proxy cost: $0.000025 (minimum)" in shown["results"]


@pytest.mark.parametrize("usage", [None, {}, {"measurement": "unavailable", "total_bytes": 0, "cost_usd": 0}])
def test_missing_coverage_is_never_shown_as_zero_bandwidth_or_zero_proxy_cost(usage):
    original = job(network_usage=usage)
    original["results"][0]["network_usage"] = usage
    shown = browser_bandwidth(render_job=original)
    assert "Observed HTTP transfer Not recorded" in shown["bandwidth"]
    assert "Estimated proxy cost Not recorded" in shown["bandwidth"]
    assert "Bandwidth was not recorded. A full-run estimate is unavailable." in shown["bandwidth"]
    assert "HTTP usage: Not recorded · Est. proxy cost: Not recorded" in shown["results"]
    assert "0 B" not in shown["bandwidth"] and "proxy cost $0" not in shown["bandwidth"]


def test_direct_run_still_records_traffic_and_shows_zero_proxy_charge():
    original = job(proxy_egypt=False)
    original["network_usage"].update(proxy_bytes=0, direct_bytes=50000, cost_usd=0,
                                     estimated_total_cost_usd=0)
    original["results"][0]["network_usage"].update(proxy_bytes=0, direct_bytes=25000)
    shown = browser_bandwidth(render_job=original)
    assert "Observed HTTP transfer 50.00 KB" in shown["bandwidth"]
    assert "Estimated proxy cost $0" in shown["bandwidth"]
    assert "Projected proxy cost $0" in shown["bandwidth"]
    assert "HTTP usage: 25.00 KB · Est. proxy cost: $0" in shown["results"]
    assert "Direct traffic has no proxy charge." in shown["bandwidth"]


def test_run_uses_cumulative_backend_totals_instead_of_truncated_result_list():
    original = job(completed_tests=5000, results_total=5000)
    original["network_usage"].update(total_bytes=1000000000, sent_bytes=100000000,
                                     received_bytes=900000000, proxy_bytes=1000000000,
                                     cost_usd=1, completed_tests=5000, sampled_tests=5000,
                                     avg_bytes_per_test=200000, estimated_total_bytes=2000000000,
                                     estimated_total_cost_usd=2)
    shown = browser_bandwidth(render_job=original)
    assert "Observed HTTP transfer 1.00 GB" in shown["bandwidth"]
    assert "Estimated proxy cost $1.0000" in shown["bandwidth"]
    assert "Usage recorded for 5,000 of 5,000 finished checks." in shown["bandwidth"]
    assert "HTTP usage: 25.00 KB" in shown["results"]


@pytest.mark.parametrize("bad", [True, -1, "50000", None])
def test_invalid_numeric_values_do_not_produce_displayed_estimates(bad):
    original = job()
    original["network_usage"].update(total_bytes=bad, cost_usd=bad, proxy_bytes=bad,
                                     avg_bytes_per_test=bad, estimated_total_bytes=bad,
                                     estimated_total_cost_usd=bad)
    shown = browser_bandwidth(render_job=original)
    assert "Observed HTTP transfer Not recorded" in shown["bandwidth"]
    assert "Estimated proxy cost Not recorded" in shown["bandwidth"]
    assert "Average / check Not recorded" in shown["bandwidth"]
    assert "Projected full run Not recorded" in shown["bandwidth"]


def test_preview_does_not_show_unknown_bandwidth_as_unrelated_result_noise():
    shown = browser_bandwidth(render_job={"id": "preview", "action": "preview", "status": "succeeded",
                                          "progress": {"completed": 1, "total": 1},
                                          "results": [{"source_row": 7, "status": "selected"}]})
    assert shown["bandwidthHidden"] and not shown["bandwidth"]
    assert "HTTP usage" not in shown["results"]


def test_result_distinguishes_this_check_from_cumulative_account_traffic():
    original = job()
    original["results"][0].update(account_tests_completed=3,
        account_network_usage=network_usage(total_bytes=75000, proxy_bytes=75000))
    shown = browser_bandwidth(render_job=original)
    assert "This check · HTTP usage: 25.00 KB" in shown["results"]
    assert "Account total: 75.00 KB across 3 finished checks · Est. proxy cost: $0.000075" in shown["results"]


def test_html_contains_accessible_bandwidth_slot_and_decimal_pricing_is_not_configurable():
    html = (Path(APP).parent / "index.html").read_text(encoding="utf-8")
    assert 'id="job-bandwidth" aria-label="HTTP bandwidth and estimated proxy cost" hidden' in html
    assert 'id="bandwidth-rate"' not in html


def test_saved_run_report_shows_the_same_totals_and_per_check_usage():
    shown = browser_bandwidth(report=job(status="succeeded"))
    assert "Bandwidth and estimated cost" in shown["reportText"]
    assert "Observed HTTP transfer 50.00 KB" in shown["reportText"]
    assert "Projected proxy cost $0.000100" in shown["reportText"]
    assert "Account 7 · Run 1" in shown["reportText"]
    assert "This check · HTTP usage: 25.00 KB" in shown["reportText"]
    assert shown["calls"] == [{"path": "/api/reports?name=synthetic-report.json", "payload": None}]


def test_standalone_account_report_shows_transfer_and_cost_without_run_projection():
    shown = browser_bandwidth(report={"source_row": 7, "passed": True, "network_usage": network_usage()})
    assert "Observed HTTP transfer 25.00 KB" in shown["reportText"]
    assert "Estimated proxy cost $0.000025" in shown["reportText"]
    assert "Projected full run" not in shown["reportText"]


def legacy_part(**changes):
    return {"request_count": 1, "request_bytes": 4000, "upload_body_bytes": 1000,
            "download_body_bytes": 500, "response_header_bytes": 100,
            "measurement_complete": True, **changes}


def legacy_job_result(**changes):
    return {"source_row": 7, "test_number": 1, "passed": True,
            "proxy": {"provider": "PacketStream", "country": "EG", "sticky": True},
            "bandwidth": {"total": legacy_part()}, **changes}


def test_old_primary_result_displays_legacy_partial_bytes_without_inventing_run_total():
    shown = browser_bandwidth(render_job=job(network_usage=None, results=[legacy_job_result()]))
    assert "Observed HTTP transfer Not recorded" in shown["bandwidth"]
    assert "This check · HTTP usage: 4.60 KB recorded (partial)" in shown["results"]
    assert "Est. proxy cost: $0.000005 (minimum)" in shown["results"]
    assert not shown["calls"]


def test_complete_legacy_total_is_preferred_and_request_body_is_not_counted_twice():
    report = legacy_job_result()
    report["bandwidth"].update(get_song=legacy_part(request_bytes=99999), play_song=legacy_part(request_bytes=88888))
    shown = browser_bandwidth(report=report)
    assert "Recorded HTTP transfer (minimum) 4.60 KB" in shown["reportText"]
    assert "Upload 4.00 KB" in shown["reportText"]
    assert "Download 600 B" in shown["reportText"]
    assert "5.60 KB" not in shown["reportText"]
    assert "Only metadata and play requests were recorded; session checks, login and connection retries are excluded." in shown["reportText"]


def test_incomplete_legacy_total_uses_known_stage_lower_bounds_even_after_failed_transfer():
    report = legacy_job_result(passed=False)
    report["bandwidth"] = {
        "total": legacy_part(request_bytes=None, download_body_bytes=None, measurement_complete=False),
        "get_song": legacy_part(),
        "play_song": legacy_part(request_bytes=None, upload_body_bytes=600,
                                 download_body_bytes=None, response_header_bytes=100, measurement_complete=False),
    }
    shown = browser_bandwidth(report=report)
    assert "Recorded HTTP transfer (minimum) 5.30 KB" in shown["reportText"]
    assert "Upload 4.60 KB" in shown["reportText"]
    assert "Download 700 B" in shown["reportText"]
    assert "Est. proxy cost (minimum) $0.000005" in shown["reportText"]


def test_legacy_direct_metadata_preserves_zero_proxy_charge_and_partial_coverage():
    report = legacy_job_result(proxy_egypt=False)
    report.pop("proxy")
    shown = browser_bandwidth(report=report)
    assert "Recorded HTTP transfer (minimum) 4.60 KB" in shown["reportText"]
    assert "Est. proxy cost (minimum) $0" in shown["reportText"]
    assert "Observed HTTP transfer" not in shown["reportText"]


@pytest.mark.parametrize("metadata", [
    {}, {"proxy": {"provider": "unknown", "country": "EG"}},
    {"proxy": {"provider": "PacketStream", "country": "EG"}, "proxy_egypt": False},
    {"proxy_egypt": "false"},
])
def test_unknown_or_conflicting_legacy_route_never_becomes_free_direct_traffic(metadata):
    report = legacy_job_result()
    report.pop("proxy")
    report.update(metadata)
    shown = browser_bandwidth(report=report)
    assert "Recorded HTTP transfer (minimum) 4.60 KB" in shown["reportText"]
    assert "Est. proxy cost (minimum) Not recorded" in shown["reportText"]
    assert "Some measured traffic could not be classified as direct or proxy." in shown["reportText"]
    assert "proxy cost (minimum) $0" not in shown["reportText"]


@pytest.mark.parametrize("bad", [True, -1, "1", 1.5, 2**53, None])
def test_legacy_request_count_requires_a_nonnegative_safe_integer(bad):
    report = legacy_job_result()
    report["bandwidth"] = {"total": legacy_part(request_count=bad)}
    shown = browser_bandwidth(render_job=job(network_usage=None, results=[report]))
    assert "HTTP usage: Not recorded" in shown["results"]
    assert "proxy cost: Not recorded" in shown["results"]


@pytest.mark.parametrize("bad", [True, -1, "90000", 0.5, 2**53, None])
def test_legacy_invalid_byte_counter_is_unknown_and_does_not_discard_other_known_counters(bad):
    report = legacy_job_result()
    report["bandwidth"] = {"get_song": legacy_part(request_bytes=bad, upload_body_bytes=1000)}
    shown = browser_bandwidth(report=report)
    assert "Recorded HTTP transfer (minimum) 1.60 KB" in shown["reportText"]
    assert "Upload 1.00 KB" in shown["reportText"]


def test_legacy_zero_requests_are_unavailable_and_canonical_new_measurements_take_precedence():
    zero = legacy_job_result()
    zero["bandwidth"] = {"total": legacy_part(request_count=0, request_bytes=0, upload_body_bytes=0,
                                              download_body_bytes=0, response_header_bytes=0)}
    shown = browser_bandwidth(render_job=job(network_usage=None, results=[zero]))
    assert "HTTP usage: Not recorded" in shown["results"]
    newer = legacy_job_result(network_usage=network_usage())
    shown = browser_bandwidth(report=newer)
    assert "Observed HTTP transfer 25.00 KB" in shown["reportText"]
    assert "4.60 KB" not in shown["reportText"]
