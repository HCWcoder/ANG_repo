"""The manual browser probe is checked with synthetic offline inputs only."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("offline_browser_flow_probe", Path(__file__).with_name("probe_browser_request_flow.py"))
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)

SECRET = "synthetic-private-secret"


def report():
    return {"source_row": 31, "phase": "playback", "requests": [], "trace_observer_errors": 0,
            "trace_limit_reached": False, "media_response_count": 0, "media_success_response_count": 0,
            "blocked_out_of_scope_requests": 0}


def request(query, **options):
    return SimpleNamespace(url=probe.GATEWAY_URL + "?" + query, method=options.get("method", "GET"),
                           headers=options.get("headers", {"cookie": SECRET}),
                           post_data=options.get("data"), resource_type="fetch", failure=options.get("failure"))


def test_request_trace_omits_all_values_and_classifies_status_without_body():
    result = report()
    recorder = probe.FlowRecorder(result)
    req = request("type=REGISTERwebplay&songid=1263607749&sid=" + SECRET + "&fingerprint=" + SECRET + "&playsecs=115&playper=1",
                  method="POST", headers={"cookie": SECRET, "authorization": SECRET, "content-type": "application/json"},
                  data=json.dumps({"password": SECRET, "session": SECRET}))
    recorder.on_request(req)
    recorder.on_response(SimpleNamespace(request=req, url=req.url, status=200, headers={}, json=lambda: {"status": SECRET, "cookie": SECRET, "reply": SECRET}))
    serialized = json.dumps(result)
    assert SECRET not in serialized
    event = result["requests"][0]
    assert event["body"]["field_names"] == ["password", "session"]
    assert event["reported_play_seconds"] == 115
    assert event["reported_play_fraction"] == 1
    assert event["api_status"] is None
    assert event["encrypted_reply_present"] is True
    assert event["song_matches_test_song"] is True


def test_opaque_encrypted_body_is_never_read():
    class EncryptedRequest:
        method = "POST"
        @property
        def post_data(self):
            pytest.fail("Encrypted payload must remain unread")
    assert probe.body_summary(EncryptedRequest(), {"x-angh-encpayload": SECRET}) == {
        "format": "opaque_binary", "field_names": [], "encrypted": True,
    }


@pytest.mark.parametrize("query", [
    "type=REGISTERwebplay&songid=999",
    "type=REGISTERwebplay&songid=1263607749&songid=999",
    "type=REGISTERwebplay&type=REGISTERwebplay&songid=1263607749",
    "type=GETdownload&angh_type=REGISTERwebplay&fileid=1263607749",
    "type=PUTplaylist&songid=1263607749",
])
def test_request_guard_blocks_wrong_duplicate_conflicting_or_like_operations(query):
    result = report()
    recorder = probe.FlowRecorder(result)
    recorder.playback_armed = True
    calls = []
    recorder.guard_route(SimpleNamespace(request=request(query), abort=lambda reason: calls.append("abort"), continue_=lambda: calls.append("continue")))
    assert calls == ["abort"]
    assert result["blocked_out_of_scope_requests"] == 1


def test_guard_allows_normal_opaque_accounting_and_blocks_following_media():
    result = report()
    recorder = probe.FlowRecorder(result)
    recorder.playback_armed = True
    calls = []
    def route(query):
        return SimpleNamespace(request=request(query), abort=lambda reason: calls.append("abort"), continue_=lambda: calls.append("continue"))
    recorder.guard_route(route("type=REGISTERwebplay"))
    recorder.finished = True
    recorder.guard_route(route("type=GETdownload&fileid=1263607749"))
    recorder.guard_route(route("type=REGISTERwebplay&songid=1263607749"))
    assert calls == ["continue", "abort", "continue"]


def test_offline_legacy_shape_matches_full_duration_without_credentials_or_network():
    result = probe.offline_python_shape(114.99)
    assert result["external_requests"] == result["actual_events_sent"] == result["audio_bytes"] == 0
    assert [event["operation"] for event in result["requests"]] == ["GETsong", "REGISTERwebplay"]
    assert result["requests"][1]["reported_play_seconds"] == 114.99
    assert result["requests"][1]["reported_play_fraction"] == 1
    assert "offline-sid" not in json.dumps(result)
    assert "offline-fingerprint" not in json.dumps(result)


def test_natural_end_does_not_claim_accounting_when_trace_is_absent():
    result = report()
    result["python_request_shape"] = probe.offline_python_shape(114.99)
    result["playback"] = {"natural_end_verified": True}
    probe.compare_shapes(result)
    assert result["comparison"]["completed_browser_playback_verified"] is True
    assert result["comparison"]["completion_accounting_observed"] is False
    assert result["comparison"]["accounting_api_acceptance_verified"] is False
    assert result["comparison"]["request_flow_comparison_conclusive"] is False


def test_retained_browser_blocks_workers_and_closes_only_when_probe_owns_cleanup():
    calls = []
    context = SimpleNamespace(add_init_script=lambda script: calls.append("script"), on=lambda *args: None, route=lambda *args: None)
    browser = SimpleNamespace(new_context=lambda **options: (calls.append(options), context)[1], close=lambda: calls.append("closed"))
    holder = probe.RetainedBrowser(browser, probe.FlowRecorder(report()))
    holder.new_context(proxy={"server": "https://synthetic.invalid"})
    holder.close()
    assert "closed" not in calls
    assert calls[0]["service_workers"] == "block"
    holder.real_close()
    assert calls[-1] == "closed"


def test_exact_row_selection_rechecks_scope_before_any_credentials(monkeypatch, tmp_path):
    source = tmp_path / "registered.txt"
    source.write_bytes(b"synthetic offline source")
    digest = probe.hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    called = []
    monkeypatch.setattr(probe, "require_new_egypt_account", lambda vault, row, source: (_ for _ in ()).throw(probe.ProbeFailure("selected_row_changed")))
    vault = SimpleNamespace(path=tmp_path / "accounts.sqlite3", record=lambda row: called.append("credentials"))
    exact = probe.ExactRowPreparationVault(vault, 31, digest)
    with pytest.raises(probe.ProbeFailure, match="bounded"):
        exact.select_test_candidates(1, start_row=31)
    assert called == []
    with pytest.raises(probe.ProbeFailure):
        exact.record(32)
    assert called == []


def test_prior_attempt_evidence_is_preserved_before_any_account_lookup(monkeypatch, tmp_path, capsys):
    directory = tmp_path / ".anghami"
    directory.mkdir()
    path = directory / "browser-request-flow-row-31-1263607749.redacted.json"
    prior = '{"play_click_attempted": true, "scope": "preserve this exact record"}\n'
    path.write_text(prior, encoding="utf-8")
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "AccountVault", lambda: pytest.fail("No account lookup after a prior playback attempt"))
    assert probe.main(["--row", "31", "--run"]) == 1
    assert path.read_text(encoding="utf-8") == prior
    assert "previous_playback_attempt_exists" in capsys.readouterr().out


def test_media_guard_blocks_native_autoplay_other_media_restart_and_post_end():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime unavailable for the offline JavaScript media guard")
    script = """
      global.window = {}; global.document = new EventTarget();
      class Media extends EventTarget {
        constructor(){super(); this.currentTime=0; this.duration=115; this.ended=false; this.currentSrc='synthetic-source'; this.src=this.currentSrc; this.paused=true; this.readyState=4;}
        play(){this.paused=false; return Promise.resolve();}
        pause(){this.paused=true;}
      }
      global.HTMLMediaElement=Media;
    """ + probe.MEDIA_INIT_SCRIPT + """
      (async () => {
        const state=window.__boundedPlaybackState, first=new Media(), second=new Media();
        let rejects=0;
        await first.play().catch(()=>rejects++);
        state.armed=true; await first.play(); await second.play().catch(()=>rejects++);
        first.currentTime=115; first.ended=true; first.dispatchEvent(new Event('ended'));
        first.currentTime=0; first.duration=200; first.currentSrc='following-source';
        await first.play().catch(()=>rejects++); await second.play().catch(()=>rejects++);
        if(rejects!==4 || !state.ended || state.endedSnapshot.current_time_seconds!==115 || state.endedSnapshot.duration_seconds!==115) process.exit(1);
      })().catch(()=>process.exit(1));
    """
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, "Offline media playback guard failed"


def test_passive_auth_summary_decodes_normal_reply_without_persisting_keys_or_identity(monkeypatch):
    seen = []
    monkeypatch.setattr(probe, "_derive_key", lambda fingerprint, timestamp, request: (seen.append((fingerprint, timestamp, request)), b"synthetic-key")[1])
    monkeypatch.setattr(probe, "_decrypt", lambda reply, key: {
        "status": "ok", "authenticate": {"email": SECRET + "@example.invalid", "reqkey": SECRET,
        "reskey": SECRET, "socketsessionid": SECRET, "signingkey": SECRET},
    })
    req = SimpleNamespace(all_headers=lambda: {"cookie": "fingerprint=" + SECRET,
                          "x-angh-ts": "1791000000", "x-angh-encpayload": "3"})
    result = probe.auth_response_summary({"reply": SECRET}, req, SECRET + "@example.invalid")
    assert seen == [(SECRET, 1791000000, False)]
    assert result["decode_result"] == "decoded"
    assert result["semantic_status"] == "ok"
    assert result["selected_account_identity_matched"] is True
    assert result["playback_keys_present"] is True
    assert SECRET not in json.dumps(result)


def test_auth_summary_reports_challenge_signal_and_omits_private_messages():
    result = probe.auth_response_summary({"status": "failed", "error": {"message": "verification required " + SECRET}, "session": SECRET}, SimpleNamespace())
    assert result["verification_required"] is True
    assert result["verification_signal_source"] == "message_classification"
    assert SECRET not in json.dumps(result)


def test_auth_decode_failure_and_page_error_never_report_exception_text(monkeypatch):
    monkeypatch.setattr(probe, "_derive_key", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError(SECRET)))
    req = SimpleNamespace(all_headers=lambda: {"cookie": "fingerprint=" + SECRET,
                          "x-angh-ts": "1791000000", "x-angh-encpayload": "3"})
    result = probe.auth_response_summary({"reply": SECRET}, req)
    assert result["decode_result"] == "decode_failed"
    assert SECRET not in json.dumps(result)
    recorder = probe.FlowRecorder(report())
    recorder.on_page_error(RuntimeError("TypeError: " + SECRET))
    recorder.on_failure(request("type=GETsong", failure="proxy connection failed " + SECRET))
    assert recorder.report["page_error_events"] == [{"phase": "playback", "code": "javascript_type"}]
    assert recorder.report["request_failure_counts"] == {"fetch:proxy": 1}
    assert SECRET not in json.dumps(recorder.report)


def test_direct_launcher_explicitly_bypasses_browser_system_proxy(monkeypatch):
    calls = []
    browser = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(probe.capture, "launch_browser", lambda **options: (calls.append(options), browser)[1])
    holders = []
    with probe.retain_login_browser(probe.FlowRecorder(report()), holders, direct=True):
        probe.capture.launch_browser(headless=True, backend="cloakbrowser")
    assert calls == [{"headless": True, "backend": "cloakbrowser", "direct": True}]


def test_direct_attempt_never_loads_proxy_and_restores_environment_after_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic.invalid")
    monkeypatch.setenv("ALL_PROXY", "http://synthetic.invalid")
    monkeypatch.setattr(probe, "require_prepared_egypt_account", lambda *args: "synthetic-source-digest")
    monkeypatch.setattr(probe, "require_no_playback_in_prior_proxy_attempt", lambda *args: True)
    monkeypatch.setattr(probe, "read_test_song_id", lambda path: probe.SONG_ID)
    monkeypatch.setattr(probe, "version", lambda package: "0.5.11")
    monkeypatch.setattr(probe, "load_packetstream_proxy", lambda: pytest.fail("Direct mode must never load a proxy profile"))
    class Vault:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def record(self, row):
            assert row == 31
            return {"email": "synthetic@example.invalid", "password": SECRET}
    monkeypatch.setattr(probe, "AccountVault", Vault)
    def fake_capture(**options):
        assert "proxy" not in options
        assert "HTTPS_PROXY" not in os.environ
        assert "ALL_PROXY" not in os.environ
        assert os.environ["NO_PROXY"] == "*"
        raise probe.ProbeFailure("synthetic_stop")
    monkeypatch.setattr(probe.capture, "capture_login", fake_capture)
    assert probe.main(["--row", "31", "--run", "--direct", "--use-prepared"]) == 1
    assert os.environ["HTTPS_PROXY"] == "http://synthetic.invalid"
    assert os.environ["ALL_PROXY"] == "http://synthetic.invalid"
    assert not (tmp_path / ".anghami" / "browser-request-flow-row-31-1263607749.redacted.json").exists()
    direct = json.loads((tmp_path / ".anghami" / "browser-request-flow-row-31-1263607749-direct.redacted.json").read_text())
    assert direct["connection"] == "direct"
    assert direct["proxy_country"] is None
    assert direct["play_click_count"] == 0
    assert SECRET not in json.dumps(direct)


@pytest.mark.parametrize("change", [
    {"cleanup_verified": False}, {"media_response_count": 1},
    {"trace_observer_errors": 1}, {"trace_limit_reached": True},
    {"requests": [{"operation": "GETdownload"}]},
    {"requests": [{"operation": "REGISTERwebplay"}]},
    {"playback": {"started": True, "play_calls": 1, "current_time_seconds": 0}},
])
def test_direct_followup_rejects_any_uncertain_proxy_attempt(monkeypatch, tmp_path, change):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    directory = tmp_path / ".anghami"
    directory.mkdir()
    prior = {"source_row": 31, "song_id": probe.SONG_ID, "connection": "proxy_egypt",
             "prepared": True, "phase": "failed", "error_code": "browser_playback_timeout",
             "cleanup_verified": True, "play_click_count": 1, "media_response_count": 0,
             "trace_observer_errors": 0, "trace_limit_reached": False, "requests": [],
             "playback": {"started": False, "play_calls": 0, "current_time_seconds": 0}}
    path = directory / "browser-request-flow-row-31-1263607749.redacted.json"
    path.write_text(json.dumps(prior))
    assert probe.require_no_playback_in_prior_proxy_attempt(31) is True
    prior.update(change)
    path.write_text(json.dumps(prior))
    with pytest.raises(probe.ProbeFailure) as failed:
        probe.require_no_playback_in_prior_proxy_attempt(31)
    assert failed.value.code == "prior_proxy_attempt_uncertain"


def test_browser_auth_settle_rejects_verification_before_play(monkeypatch):
    result = report()
    result["requests"] = [{"phase": "song_page", "operation": "authenticate", "http_status": 200,
                           "authentication_diagnostic": {"verification_required": True}}]
    recorder = probe.FlowRecorder(result)
    monkeypatch.setattr(probe, "browser_dom_state", lambda page: {})
    page = SimpleNamespace(wait_for_timeout=lambda value: None)
    with pytest.raises(probe.ProbeFailure) as failed:
        probe.settle_browser_auth(page, recorder, lambda: None)
    assert failed.value.code == "browser_verification_required"


def test_saved_browser_restores_bound_cookies_and_registers_cleanup_before_context(monkeypatch):
    calls = []
    context = SimpleNamespace(add_init_script=lambda value: None, on=lambda *args: None, route=lambda *args: None,
                              add_cookies=lambda values: calls.append(values), new_page=lambda: "synthetic-page")
    browser = SimpleNamespace(new_context=lambda **options: context, close=lambda: calls.append("closed"))
    monkeypatch.setattr(probe, "normal_browser_launch", lambda **options: (calls.append(options), browser)[1])
    recorder = probe.FlowRecorder(report())
    monkeypatch.setattr(recorder, "bind", lambda saved: calls.append("bound"))
    saved = {"requests": {"relations": {"headers": {"cookie": "appsidsave=" + SECRET + "; fingerprint=" + SECRET}}}}
    holders = []
    assert probe.restore_saved_browser(saved, recorder, holders) == "synthetic-page"
    assert calls[0] == "bound"
    assert calls[1] == {"headless": True, "backend": "cloakbrowser", "direct": True}
    assert len(holders) == 1
    assert {cookie["name"] for cookie in calls[2]} == {"appsidsave", "fingerprint"}
    assert all(cookie["value"] == SECRET and cookie["domain"] == ".anghami.com" and cookie["secure"] is True for cookie in calls[2])
    assert SECRET not in json.dumps(recorder.report)
    holders[0].real_close()
    assert calls[-1] == "closed"


def test_saved_browser_launch_is_owned_when_context_creation_fails(monkeypatch):
    calls = []
    browser = SimpleNamespace(new_context=lambda **options: (_ for _ in ()).throw(RuntimeError(SECRET)), close=lambda: calls.append("closed"))
    monkeypatch.setattr(probe, "normal_browser_launch", lambda **options: browser)
    recorder = probe.FlowRecorder(report())
    monkeypatch.setattr(recorder, "bind", lambda saved: None)
    holders = []
    with pytest.raises(RuntimeError):
        probe.restore_saved_browser({}, recorder, holders)
    assert len(holders) == 1
    holders[0].real_close()
    assert calls == ["closed"]


@pytest.mark.parametrize("change", [
    {"cleanup_verified": False}, {"play_click_count": 1}, {"play_click_attempted": True},
    {"media_response_count": 1}, {"requests": [{"operation": "REGISTERwebplay"}]},
    {"requests": [{"operation": "GETdownload"}]}, {"login_failure": {"code": "login_timeout"}},
])
def test_saved_mode_rejects_uncertain_previous_direct_login(monkeypatch, tmp_path, change):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    directory = tmp_path / ".anghami"
    directory.mkdir()
    prior = {"source_row": 31, "song_id": probe.SONG_ID, "connection": "direct", "phase": "failed",
             "cleanup_verified": True, "play_click_attempted": False, "play_click_count": 0,
             "media_response_count": 0, "trace_observer_errors": 0, "trace_limit_reached": False,
             "requests": [], "login_failure": {"code": "login_rejected", "stage": "home", "auth_http_status": 200, "authentication_result": "failed"}}
    path = directory / "browser-request-flow-row-31-1263607749-direct.redacted.json"
    path.write_text(json.dumps(prior))
    assert probe.require_no_playback_in_rejected_direct_login(31) is True
    prior.update(change)
    path.write_text(json.dumps(prior))
    with pytest.raises(probe.ProbeFailure) as failed:
        probe.require_no_playback_in_rejected_direct_login(31)
    assert failed.value.code == "prior_direct_login_uncertain"


def test_saved_mode_does_not_call_password_login_attach_or_enroll(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "require_prepared_egypt_account", lambda *args: "synthetic-digest")
    monkeypatch.setattr(probe, "require_no_playback_in_prior_proxy_attempt", lambda *args: True)
    monkeypatch.setattr(probe, "require_no_playback_in_rejected_direct_login", lambda *args: True)
    monkeypatch.setattr(probe, "read_test_song_id", lambda path: probe.SONG_ID)
    monkeypatch.setattr(probe, "version", lambda package: "0.5.11")
    monkeypatch.setattr(probe, "_selected_session", lambda saved: ({"cookie": SECRET}, SECRET, SECRET))
    monkeypatch.setattr(probe.capture, "capture_login", lambda **options: pytest.fail("Saved mode must not send a password"))
    class Vault:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def session(self, row):
            assert row == 31
            return {"account_email": "synthetic@example.invalid"}
        def attach(self, *args, **options): pytest.fail("Saved mode must not replace account sessions")
        def enable_test_account(self, *args): pytest.fail("Saved mode must not change enrollment")
        def _http_session(self, row, proxy):
            assert row == 31 and proxy is None
            raise probe.ProbeFailure("synthetic_identity_stop")
    monkeypatch.setattr(probe, "AccountVault", Vault)
    def restore(saved, recorder, holders):
        holders.append(SimpleNamespace(context=SimpleNamespace(pages=[]), browser=SimpleNamespace(version="152.0.7977.82"), real_close=lambda: None))
        return "synthetic-page"
    monkeypatch.setattr(probe, "restore_saved_browser", restore)
    assert probe.main(["--row", "31", "--run", "--direct", "--use-prepared", "--saved-session"]) == 1
    path = tmp_path / ".anghami" / "browser-request-flow-row-31-1263607749-direct-saved.redacted.json"
    result = json.loads(path.read_text())
    assert result["saved_session_restored"] is True
    assert result["password_required"] is False
    assert result["play_click_count"] == 0
    assert result["error_code"] == "synthetic_identity_stop"
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("scenario", ["detached_natural_end", "source_swap_before_ended", "gapless_boundary",
                                      "native_autoplay", "duration_change", "time_rewind", "same_target_resume"])
def test_detached_primary_guard_rejects_new_track_before_original_play(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable for the executable offline media guard")
    script = """
      global.window={}; global.document=new EventTarget();
      class Media extends EventTarget {
        constructor(){super();this.currentTime=0;this.duration=115;this.ended=false;
          this.src='synthetic-target';this.currentSrc=this.src;this.paused=true;
          this.readyState=4;this.webkitAudioDecodedByteCount=500;this.nativeCalls=0;}
        play(){this.nativeCalls++;this.paused=false;this.dispatchEvent(new Event('playing'));return Promise.resolve();}
        pause(){this.paused=true;}
      }
      global.HTMLMediaElement=Media;
    """ + probe.MEDIA_INIT_SCRIPT + """
      (async () => {
        const state=window.__boundedPlaybackState, first=new Media();
        state.armed=true;state.expectedDuration=115;
        await first.play();
        const move=time=>{first.currentTime=time;first.dispatchEvent(new Event('timeupdate'));};
        const scenario=SCENARIO;
        if(scenario==='detached_natural_end'){
          move(114);first.currentTime=115;first.ended=true;first.dispatchEvent(new Event('ended'));
          if(!state.ended || state.completionMode!=='natural_end' || state.endedSnapshot.current_time_seconds!==115) throw Error();
        } else if(scenario==='gapless_boundary'){
          move(114.5);first.src='synthetic-following';first.currentSrc=first.src;first.duration=272;first.currentTime=0;
          first.dispatchEvent(new Event('loadstart'));
          await first.play().then(()=>{throw Error();},()=>{});
          if(!state.ended || state.completionMode!=='target_boundary' || state.endedSnapshot.duration_seconds!==115
             || state.endedSnapshot.current_time_seconds!==114.5 || first.nativeCalls!==1 || !first.paused) throw Error();
        } else if(scenario==='same_target_resume'){
          move(40);first.paused=true;await first.play();
          if(first.nativeCalls!==2 || state.scopeViolation || state.ended || first.currentTime!==40) throw Error();
        } else {
          move(40);
          if(scenario==='duration_change'){
            first.duration=272;first.dispatchEvent(new Event('durationchange'));
          } else if(scenario==='time_rewind'){
            first.currentTime=0;first.dispatchEvent(new Event('seeking'));
          } else {
            first.src='synthetic-following';first.currentSrc=first.src;first.duration=272;first.currentTime=0;
            if(scenario==='native_autoplay'){
              first.paused=false;first.dispatchEvent(new Event('playing'));
            } else {
              await first.play().then(()=>{throw Error();},()=>{});
            }
          }
          if(!state.scopeViolation || !first.paused || first.nativeCalls!==1) throw Error();
        }
      })().catch(()=>process.exit(1));
    """
    script = script.replace("SCENARIO", json.dumps(scenario))
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, "Detached media boundary guard failed for " + scenario


@pytest.mark.parametrize("second", [
    {"current_time_seconds": 0, "duration_seconds": 167},
    {"current_time_seconds": 11, "duration_seconds": 272},
])
def test_python_loop_independently_stops_rewind_or_duration_swap(monkeypatch, second):
    result = report()
    recorder = probe.FlowRecorder(result)
    base = {"started": True, "ended": False, "paused": False, "scope_violation": False,
            "play_calls": 1, "blocked_calls": 0, "current_time_seconds": 10,
            "duration_seconds": 167, "ready_state": 4, "decoded_audio_bytes": 500,
            "media_error_code": None}
    samples = iter([base, {**base, **second}])
    page = SimpleNamespace(url=probe.SONG_URL, evaluate=lambda script: next(samples), wait_for_timeout=lambda value: None)
    with pytest.raises(probe.ProbeFailure) as failed:
        probe.finish_playback(page, recorder, 167, lambda: None)
    assert failed.value.code == "browser_media_scope_changed"


def test_decoder_evidence_proves_delivery_while_unknown_operations_keep_coverage_partial():
    result = report()
    result["python_request_shape"] = probe.offline_python_shape(167)
    result["playback"] = {"decoded_audio_bytes": 966000, "ready_state": 4, "observed_progress_verified": True,
                          "natural_end_verified": True}
    recorder = probe.FlowRecorder(result)
    recorder.on_request(request("type=REGISTERwebplay&songid=1263607749&playsecs=167&playper=1"))
    recorder.on_request(request("type=unrecognized_public_operation"))
    probe.compare_shapes(result)
    assert result["media_response_count"] == 0
    assert result["comparison"]["browser_performs_audio_delivery"] is True
    assert result["comparison"]["browser_audio_delivery_evidence"] == "decoded_media_progress"
    assert result["comparison"]["unknown_gateway_operation_count"] == 1
    assert result["comparison"]["coverage_partial"] is True
    assert result["comparison"]["request_flow_comparison_conclusive"] is False
