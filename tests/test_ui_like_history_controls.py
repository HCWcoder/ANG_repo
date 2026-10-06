"""Per-song saved like history and graceful stop controls use an offline DOM."""

import json
from pathlib import Path
import subprocess

import pytest

from test_ui_provider_review import APP, DRIVER, NODE


SONG = "1263607749"


def state(*, song=SONG, history_song=SONG, **history):
    return {"test_song_id": song, "job_controls": {"stop_preparation": True},
            "limits": {"accounts": 5, "test_accounts": 5, "tests_per_account": 5},
            "cohort": {"ready_rows": [1, 2, 3, 4, 5], "accounts": [
                {"source_row": row, "registered_country": "EG" if row < 5 else "LB", "session_saved": True}
                for row in range(1, 6)]}, "proxy": {"configured": True},
            "failure_review": {"accounts": [], "total": 0},
            "like_history": {"song_id": history_song, "confirmed_rows": [1],
                             "verification_pending_rows": [2], "unknown_rows": [3], "eligible_rows": [4, 5], **history}}


def job(**changes):
    return {"id": "a" * 32, "action": "like", "status": "running", "attempted": 1,
            "completed_tests": 1, "progress": {"completed": 1, "total": 2}, "succeeded": 1,
            "failed": 0, "count": 1, "results_total": 1, "results": [],
            "writes_attempted": 1, "writes_accepted": 1, "new_likes_verified": 1,
            "already_liked_verified": 0, "selected_accounts": 5, "eligible_accounts": 2,
            "already_liked_skipped": 1, "like_verification_held": 1, "unknown_like_held": 1,
            "duplicate_like_skipped_tests": 2, **changes}


def browser(**inputs):
    if NODE is None:
        pytest.skip("Node.js is needed to exercise actual browser handlers")
    driver = DRIVER.replace("CSS:{supports:()=>true},TextEncoder,",
                            "CSS:{supports:()=>true},TextEncoder,ConsoleSelection:require(process.argv[2]),")
    driver = driver.replace("requestPending=false;},", "requestPending=false;songDirty=false;$('song-id').value=value.test_song_id;updateSongState();},")
    driver = driver.replace("const state={test_song_id:", "const state=input.state ?? {test_song_id:")
    driver = driver.replace("renderFailureReview,startReviewPreparation,renderRunVisibility,renderJob,resultStatus,",
                            """renderFailureReview,startReviewPreparation,renderRunVisibility,renderJob,resultStatus,renderAccounts,updateSelections,startTest,stopCurrentJob,
  testSelection(rows){selectedRows.clear();rows.forEach(row=>selectedRows.add(row));updateSelections();},
  testSelected(){return [...selectedRows];},
  selectNotLiked(){return $('select-not-liked-accounts').handlers.click[0]();},""")
    driver = driver.replace("if(input.submit) await hooks.startReviewPreparation();",
                            """if(input.submit) await hooks.startReviewPreparation();
  get('test-account-country').value=input.country ?? ''; hooks.renderAccounts();
  if(input.selected) hooks.testSelection(input.selected);
  if(input.tests_per_account){get('test-count').value=String(input.tests_per_account);hooks.updateSelections();}
  if(input.next_state){hooks.init(input.next_state);hooks.renderAccounts();}
  if(input.select_not_liked) hooks.selectNotLiked();
  if(input.submit_test) await hooks.startTest(input.submit_test);""")
    driver = driver.replace("if(input.render_job) hooks.renderJob(input.render_job);",
                            """if(input.render_job) hooks.renderJob(input.render_job);
  if(input.stop){await hooks.stopCurrentJob();if(input.stop_twice) await hooks.stopCurrentJob();}
  if(input.stop_via_preparation) await get('stop-prepare').handlers.click[0]();""")
    driver = driver.replace(
        "return {ok:true,status:202,json:async()=>({job:{id:'synthetic-review-job',action:'prepare',status:'queued',phase:'queued',progress:{completed:0,total:1},results:[]}})};",
        """if(path==='/api/jobs/stop') return input.stop_error ? {ok:false,status:409,json:async()=>({error:'The run changed.'})} : {ok:true,status:200,json:async()=>({job:{...input.render_job,stop_requested:true}})};
    return {ok:true,status:202,json:async()=>({job:{id:'synthetic-review-job',action:'prepare',status:'queued',phase:'queued',progress:{completed:0,total:1},results:[]}})};""",
    )
    driver = driver.replace("process.stdout.write(JSON.stringify({calls,",
                            """process.stdout.write(JSON.stringify({testSelected:hooks.testSelected(),picker:get('account-picker').textContent,likeSummary:get('like-history-summary').textContent,setupSummary:get('run-summary').textContent,bandwidth:get('job-bandwidth').textContent,
    likeDisabled:get('run-like').disabled,playDisabled:get('run-play').disabled,notLikedDisabled:get('select-not-liked-accounts').disabled,
    stopHidden:get('stop-job').hidden,stopDisabled:get('stop-job').disabled,stopText:get('stop-job').textContent,stopError:get('job-control-error').textContent,
    prepareStopHidden:get('stop-prepare').hidden,prepareStopDisabled:get('stop-prepare').disabled,prepareStopText:get('stop-prepare').textContent,
    prepareStopHint:get('prepare-stop-hint').textContent,prepareStatus:get('prepare-run-status').textContent,prepareStatusHidden:get('prepare-run-status').hidden,
    prepareStopError:get('prepare-control-error').textContent,
    stopReason:get('job-stop-reason').textContent,calls,""")
    result = subprocess.run([NODE, "-e", driver, str(APP), str(Path(APP).with_name("selection.js"))],
                            input=json.dumps(inputs), text=True, encoding="utf-8", capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_picker_marks_saved_and_held_likes_without_disabling_play_selection():
    shown = browser(state=state(), selected=[1, 2, 3, 4, 5])
    assert "Already liked (saved history)" in shown["picker"]
    assert "Like verification pending · Like skipped" in shown["picker"]
    assert "Unknown like result · Like held" in shown["picker"]
    assert shown["testSelected"] == [1, 2, 3, 4, 5]
    assert not shown["playDisabled"] and not shown["likeDisabled"]
    assert "2 eligible selected" in shown["likeSummary"]
    assert "1 already liked in saved history (will skip)" in shown["likeSummary"]
    assert "1 awaiting like verification" in shown["likeSummary"]
    assert "1 held from another like request" in shown["likeSummary"]
    assert not shown["calls"]


def test_select_not_yet_liked_excludes_saved_pending_unknown_accounts_without_requests():
    shown = browser(state=state(), selected=[1, 2, 3], select_not_liked=True)
    assert shown["testSelected"] == [4, 5]
    assert "2 eligible selected" in shown["likeSummary"]
    assert "0 already liked in saved history" in shown["likeSummary"]
    assert not shown["calls"]


@pytest.mark.parametrize("country,selected", [("EG", [4]), ("LB", [5])])
def test_select_not_yet_liked_respects_registered_country_filter(country, selected):
    shown = browser(state=state(), country=country, select_not_liked=True)
    assert shown["testSelected"] == selected and not shown["calls"]


def test_changing_the_saved_song_uses_fresh_history_and_keeps_play_ready_accounts():
    newer = state(song="1280677978", history_song="1280677978", confirmed_rows=[],
                  verification_pending_rows=[], unknown_rows=[], eligible_rows=[1, 2, 3, 4, 5])
    shown = browser(state=state(), selected=[1, 2, 3, 4, 5], next_state=newer)
    assert "Already liked (saved history)" not in shown["picker"]
    assert "Unknown like result" not in shown["picker"]
    assert "Likes for song 1280677978: 5 eligible selected" in shown["likeSummary"]
    assert shown["testSelected"] == [1, 2, 3, 4, 5]


def test_stale_history_for_another_song_does_not_mark_current_accounts_liked():
    shown = browser(state=state(song="1280677978"), selected=[1, 2, 3, 4, 5])
    assert "5 eligible selected" in shown["likeSummary"]
    assert "Already liked (saved history)" not in shown["picker"]


@pytest.mark.parametrize("bad", [True, "4", 0, -1, 1.5, 2**31, None])
def test_malformed_history_rows_do_not_mark_or_select_accounts(bad):
    saved = state(confirmed_rows=[bad], verification_pending_rows=[], unknown_rows=[], eligible_rows=[1, 2, 3, 4, 5])
    shown = browser(state=saved, selected=[1, 2, 3, 4, 5])
    assert "5 eligible selected" in shown["likeSummary"]
    assert "Already liked (saved history)" not in shown["picker"]


def test_unknown_like_history_has_precedence_over_conflicting_confirmed_rows():
    shown = browser(state=state(confirmed_rows=[1, 3]), selected=[3])
    assert "0 eligible selected" in shown["likeSummary"]
    assert "0 already liked in saved history" in shown["likeSummary"]
    assert "1 held from another like request" in shown["likeSummary"]


def test_like_submission_preserves_selected_rows_for_backend_history_skip_accounting():
    shown = browser(state=state(), selected=[1, 2, 3, 4, 5], submit_test="like")
    assert len(shown["calls"]) == 1
    assert shown["calls"][0]["path"] == "/api/jobs"
    assert shown["calls"][0]["payload"]["rows"] == [1, 2, 3, 4, 5]
    assert shown["calls"][0]["payload"]["song_id"] == SONG


def test_history_skips_and_holds_are_reported_separately_from_fresh_like_writes():
    shown = browser(state=state(), render_job=job(status="completed_with_pending"))
    assert "New likes verified 1" in shown["metrics"]
    assert "Saved likes skipped 1" in shown["metrics"]
    assert "Like verification held 1" in shown["metrics"] and "Unknown likes held 1" in shown["metrics"]
    assert "Repeated likes skipped 2" in shown["metrics"]
    assert "1 accounts skipped from saved like history" in shown["message"]
    assert "Other failures 0" in shown["metrics"] and shown["status"] == "Completed with pending checks"


@pytest.mark.parametrize("filter,expected,absent", [
    ("history_skipped", "Saved history already confirms", "A previous like needs verification"),
    ("pending", "A previous like needs verification", "Saved history already confirms"),
    ("failed", "", "Saved history already confirms"),
])
def test_cached_skip_and_like_hold_filters_do_not_mark_saved_history_as_failed(filter, expected, absent):
    report = job(results=[{"source_row": 1, "outcome": "history_skipped", "passed": True},
                          {"source_row": 2, "outcome": "like_history_pending", "passed": False}])
    shown = browser(state=state(), job=report, filter=filter)
    assert expected in shown["results"] and absent not in shown["results"]


def test_stop_run_sends_one_bound_request_and_keeps_running_checks_visible():
    shown = browser(state=state(), render_job=job(), stop=True, stop_twice=True)
    assert shown["calls"] == [{"path": "/api/jobs/stop", "payload": {"job_id": "a" * 32}}]
    assert shown["status"] == "Running" and shown["stopText"] == "Stopping…"
    assert shown["stopDisabled"] and not shown["stopHidden"]
    assert shown["progress"] == "1 / 2 checks finished" and not shown["stopError"]


@pytest.mark.parametrize("action,status", [("preview", "running"), ("check", "running"),
                                         ("prepare", "succeeded"), ("prepare", "stopped"),
                                         ("like", "succeeded"), ("like", "failed")])
def test_stop_control_does_not_request_stop_for_ineligible_or_terminal_jobs(action, status):
    shown = browser(state=state(), render_job=job(action=action, status=status), stop=True)
    assert shown["stopHidden"] and not shown["calls"]


def test_stop_error_retains_current_run_and_reenables_stop_button():
    shown = browser(state=state(), render_job=job(), stop=True, stop_error=True)
    assert shown["status"] == "Running" and shown["stopError"] == "The run changed."
    assert not shown["stopDisabled"] and shown["stopText"] == "Stop run"


def test_user_stop_is_a_warning_with_explicit_drain_message():
    shown = browser(state=state(), render_job=job(status="stopped", stop_reason="user_stop"))
    assert shown["statusClass"] == "badge warning" and shown["stopHidden"]
    assert "Checks already running were allowed to finish; no new checks started." in shown["stopReason"]


def test_history_ui_and_stop_control_are_accessible_without_bulk_run_trigger():
    html = Path(APP).with_name("index.html").read_text(encoding="utf-8")
    assert 'id="select-not-liked-accounts"' in html
    assert 'id="like-history-summary" class="activity-hint" role="status"' in html
    assert 'aria-label="Stop new checks and let running checks finish"' in html
    assert 'value="history_skipped"' in html


def test_setup_repeated_run_estimate_is_explicitly_play_only_and_likes_remain_once_per_song():
    shown = browser(state=state(), selected=[1, 2, 3, 4, 5], tests_per_account=5)
    assert "Play: 5 accounts × 5 tests = 25 runs" in shown["setupSummary"]
    assert "2 eligible selected" in shown["likeSummary"]
    assert "Like tests run once per account/song; extra repeats are skipped." in shown["likeSummary"]
    assert not shown["calls"]


def test_racing_cached_skips_and_holds_complete_progress_without_claiming_fresh_checks_or_writes():
    report = job(status="completed_with_pending", progress={"completed": 3, "total": 3},
                 completed_tests=1, attempted=1, cached_like_skips=1, cached_like_holds=1,
                 elapsed_seconds=60, already_liked_skipped=1, like_verification_held=1, unknown_like_held=0,
                 network_usage={"measurement": "measured", "scope": "python_http", "total_bytes": 25000,
                                "sent_bytes": 1000, "received_bytes": 24000, "proxy_bytes": 25000,
                                "sampled_tests": 1, "completed_tests": 1, "avg_bytes_per_test": 25000,
                                "avg_bytes_per_account": 25000, "estimated_total_bytes": 75000})
    shown = browser(state=state(), render_job=report)
    assert shown["progress"] == "3 / 3 checks finished"
    assert "Checks finished 3 / 3" in shown["metrics"] and "Checks run 1" in shown["metrics"]
    assert "New likes verified 1" in shown["metrics"] and "Like requests attempted 1" in shown["metrics"]
    assert "1.0 tests/min average" in shown["summary"]
    assert "3.0 tests/min average" not in shown["summary"]
    assert "Observed HTTP transfer 25.00 KB" in shown["bandwidth"]
    assert "Average / check 25.00 KB" in shown["bandwidth"]
    assert "Usage recorded for 1 of 1 finished checks." in shown["bandwidth"]
    assert not shown["calls"]


def test_all_racing_cached_resolutions_finish_without_claiming_real_checks_or_test_rate():
    report = job(status="completed_with_pending", progress={"completed": 2, "total": 2}, completed_tests=0,
                 attempted=0, cached_like_skips=1, cached_like_holds=1, elapsed_seconds=60,
                 writes_attempted=0, writes_accepted=0, new_likes_verified=0)
    shown = browser(state=state(), render_job=report)
    assert shown["progress"] == "2 / 2 checks finished"
    assert "Checks run 0" in shown["metrics"] and "Like requests attempted 0" in shown["metrics"]
    assert "New likes verified 0" in shown["metrics"]
    assert "tests/min average" not in shown["summary"]


def test_saved_history_resolutions_do_not_leave_false_waiting_accounts_during_live_run():
    report = job(progress={"completed": 2, "total": 3}, completed_tests=1,
                 cached_like_skips=1, cached_like_holds=0, active_workers=1)
    shown = browser(state=state(), render_job=report)
    assert shown["progress"] == "2 / 3 checks finished"
    assert "Waiting 0" in shown["metrics"] and "Checks run 1" in shown["metrics"]


def test_prefilter_history_counts_do_not_inflate_remaining_job_progress():
    report = job(status="succeeded", progress={"completed": 2, "total": 2}, completed_tests=2,
                 already_liked_skipped=100, like_verification_held=3, unknown_like_held=2,
                 cached_like_skips=0, cached_like_holds=0)
    shown = browser(state=state(), render_job=report)
    assert shown["progress"] == "2 / 2 checks finished"
    assert "Checks run" not in shown["metrics"]
    assert "Saved likes skipped 100" in shown["metrics"]


@pytest.mark.parametrize("bad", [True, "1", -1, 0.5, None])
def test_invalid_cached_counters_do_not_inflate_progress_or_network_check_rate(bad):
    report = job(cached_like_skips=bad, cached_like_holds=bad, elapsed_seconds=60)
    shown = browser(state=state(), render_job=report)
    assert shown["progress"] == "1 / 2 checks finished"
    assert "Checks run" not in shown["metrics"]
    assert "1.0 tests/min average" in shown["summary"]
