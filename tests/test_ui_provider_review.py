"""Provider visibility and exact failed-row preparation use synthetic data only."""

import http.client
import json
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from anghami_session import ui_server
from anghami_session.errors import SessionError


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "anghami_session/ui/app.js"
NODE = shutil.which("node")
PRIVATE = "synthetic-private-value-must-not-appear"
STAMP = "2026-10-03T12:00:00+00:00"


def review_account(row=31, **fields):
    return {"source_row": row, "failure_code": "session_authentication_rejected",
            "failed_stage": "relations", "failed_at": STAMP, **fields}


def test_review_filter_rebuilds_account_facts_and_excludes_provider_or_private_fields():
    result = ui_server.safe_failure_review({
        "accounts": [review_account(email=PRIVATE, password=PRIVATE), review_account(),
                     review_account(32, failure_code="request_transport_failed"),
                     review_account(33, failure_code="session_identity_mismatch", failed_stage="identity")],
        "failed_rows": [900], "total": 4, "session": PRIVATE,
    })
    assert result == {"accounts": [review_account(), review_account(33, failure_code="session_identity_mismatch", failed_stage="identity")],
                      "failed_rows": [31, 33], "total": 4}
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("fields", [
    {"source_row": True}, {"source_row": "31"}, {"source_row": 0}, {"source_row": 2**31},
    {"failure_code": PRIVATE}, {"failure_code": []}, {"failed_stage": PRIVATE}, {"failed_stage": []},
    {"failed_at": "2026-10-03T12:00:00"}, {"failed_at": PRIVATE}, {"failed_at": []},
])
def test_review_filter_rejects_malformed_or_unbound_account_metadata(fields):
    assert ui_server.safe_failure_review({"accounts": [review_account(**fields)], "total": 1})["accounts"] == []


def test_service_state_exposes_only_safe_review_without_loading_account_secrets(tmp_path, monkeypatch):
    class Vault:
        def __init__(self, _path): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def summary(self): return {"records": 1, "unique_accounts": 1, "sessions_saved": 0, "duplicate_rows": 0, "states": {}}
        def test_accounts(self): return {"test_rows": [], "ready_rows": [], "accounts": []}
        def failure_review(self): return {"accounts": [review_account(email=PRIVATE)], "failed_rows": [31], "total": 1}

    def unavailable(_path):
        raise SessionError("Synthetic profile is unavailable.")

    monkeypatch.setattr(ui_server, "AccountVault", Vault)
    monkeypatch.setattr(ui_server, "load_packetstream_proxy", unavailable)
    monkeypatch.setattr(ui_server, "load_test_pool", unavailable)
    monkeypatch.setattr(ui_server.StickyProxyPool, "load", unavailable)
    service = ui_server.ConsoleService(tmp_path / "accounts.sqlite3")
    result = service.state()
    assert result["failure_review"] == {"accounts": [review_account()], "failed_rows": [31], "total": 1}
    assert PRIVATE not in json.dumps(result)
    server = ui_server.create_server(port=0, service=service)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    try:
        connection.request("GET", "/api/state", headers={"X-App-Token": server.token})
        response = connection.getresponse()
        public = json.loads(response.read())
        assert response.status == 200 and public["failure_review"] == result["failure_review"]
        assert PRIVATE not in json.dumps(public)
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(3)
    assert not thread.is_alive()


def test_attempt_history_and_request_failure_are_strict_and_preserve_retry_evidence():
    result = ui_server.safe_report({
        "proxy_pool_size": 2, "account_failed": 1, "provider_retries": 2, "connection_pending": 1,
        "retried": 1, "retry_count": 2, "provider_attempts": 3, "outcome": "connection_pending",
        "session_failure": {"code": "request_rate_limited", "stage": "relations", "http_status": 429,
                            "retry_after_seconds": 2, "failure_category": "account", "rotate_route": True, "url": PRIVATE},
        "attempt_history": [
            {"attempt": 1, "proxy_route_number": 1, "outcome": "provider_issue", "password": PRIVATE,
             "session_failure": {"code": "request_transport_failed", "stage": "preflight", "curl_code": 7, "message": PRIVATE}},
            {"attempt": 2, "proxy_route_number": 2, "outcome": "provider_issue"},
            {"attempt": 3, "proxy_route_number": 999, "outcome": "succeeded"},
            {"attempt": 4, "outcome": "succeeded"},
        ], "raw_request": PRIVATE,
    })
    assert result["session_failure"]["failure_category"] == "provider"
    assert result["session_failure"]["rotate_route"] is False
    assert result["session_failure"]["retry_after_seconds"] == 2
    assert len(result["attempt_history"]) == 3
    assert result["attempt_history"][0]["session_failure"]["curl_code"] == 7
    assert "proxy_route_number" not in result["attempt_history"][2]
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("phase", ["provider_wait", "provider_retry", "connection_pending", "account_failed"])
def test_fixed_provider_preparation_phases_are_preserved(phase):
    assert ui_server.safe_report({"phase": phase, "failed_phase": phase}) == {"phase": phase, "failed_phase": phase}


def test_uncertain_session_renewal_displays_only_fixed_code_and_boolean_facts():
    result = ui_server.safe_report({"error_code": "session_renewal_unknown", "renewal_attempted": True,
                                    "renewal_completed": False, "renewal_unknown": True,
                                    "result_unknown": True, "renewal_response": PRIVATE})
    assert result == {"error_code": "session_renewal_unknown", "renewal_attempted": True,
                      "renewal_completed": False, "renewal_unknown": True, "result_unknown": True}
    assert PRIVATE not in json.dumps(result)
    assert ui_server.safe_report({"renewal_unknown": PRIVATE, "renewal_completed": 1}) == {
        "renewal_unknown": None, "renewal_completed": None}


@pytest.mark.parametrize("name,value", [
    ("account_failed", True), ("provider_retries", -1), ("connection_pending", "1"), ("retried", 2**31),
    ("retry_count", 3), ("retry_count", False), ("provider_attempts", 0), ("provider_attempts", 4),
    ("connection_status", PRIVATE), ("connection_status", []), ("outcome", PRIVATE), ("outcome", {}),
])
def test_invalid_provider_visibility_facts_are_not_exposed(name, value):
    assert ui_server.safe_report({name: value}) == {name: None}


def test_active_retry_descriptor_retains_only_valid_typed_connection_fields():
    result = ui_server.safe_report({"proxy_pool_size": 2, "active_tests": [{
        "source_row": 31, "test_number": 1, "started_at": STAMP,
        "proxy_pool_size": 2, "proxy_route_number": 1, "retry_count": 1,
        "provider_attempts": 2, "connection_status": "waiting", "password": PRIVATE,
    }]})
    assert result["active_tests"][0]["connection_status"] == "waiting"
    assert result["active_tests"][0]["provider_attempts"] == 2
    assert PRIVATE not in json.dumps(result)


DRIVER = r"""
const fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.handlers={}; this.value=''; this.disabled=false; this.hidden=false; this.checked=false; this.dataset={}; this.style={}; this.attrs={}; this._text=''; this.className=''; this.classList={add(){},remove(){},toggle(){}}; }
  set textContent(value) { this._text=String(value ?? ''); this.children=[]; }
  get textContent() { return this._text + this.children.map(child=>child.textContent ?? '').join(' '); }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this._text=''; this.children=children; }
  setAttribute(key,value) { this.attrs[key]=String(value); }
  addEventListener(type,fn) { (this.handlers[type] ??= []).push(fn); }
}
const elements = new Map();
const get = id => { if(!elements.has(id)) elements.set(id,new Element()); return elements.get(id); };
for(const [id,value] of Object.entries({'prepare-method':'browser','browser':'cloakbrowser','prepare-connection-mode':input.connection ?? 'direct','connection-mode':'direct','login-connection-mode':'direct','random-count':'1','test-count':'1','test-workers':'8','test-failure-limit':'20','job-result-filter':input.filter ?? 'all','song-id':'1263607749'})) get(id).value=value;
get('headless').checked=true; get('prepare-reduce-browser-data').checked=true;
if(input.no_browser) get('prepare-method').value='http';
const descendants = (element) => element.children.flatMap(child=>[child,...descendants(child)]);
let calls=[];
const ctx = vm.createContext({
  document:{getElementById:get,createElement:tag=>new Element(tag),createElementNS:(_ns,tag)=>new Element(tag),createTextNode:text=>({textContent:text}),
    querySelector:selector=>selector.startsWith('meta') ? {content:'synthetic-token'} : get(selector),
    querySelectorAll:selector=>selector==='#review-accounts-table input' ? descendants(get('review-accounts-table')).filter(child=>child.tagName==='input') : []},
  history:{replaceState(){}},location:{hash:'#accounts'},CSS:{supports:()=>true},TextEncoder,
  setTimeout:()=>1,clearTimeout(){},console,fetch:async(path,opts={})=>{
    calls.push({path,payload:opts.body ? JSON.parse(opts.body) : null});
    return {ok:true,status:202,json:async()=>({job:{id:'synthetic-review-job',action:'prepare',status:'queued',phase:'queued',progress:{completed:0,total:1},results:[]}})};
  },
});
const entry = source.lastIndexOf('  refreshState();');
if(entry<0) throw Error('Cannot find real UI entrypoint');
const injected = `  globalThis.reviewHooks={
  init(value){state=value;busy=!!${JSON.stringify(!!input.busy)};requestPending=false;},
  select(rows){reviewRows.clear();rows.forEach(row=>reviewRows.add(row));},
  renderFailureReview,startReviewPreparation,renderRunVisibility,renderJob,resultStatus,
  selectAll(){return $('review-select-all').handlers.click[0]();},
  change(row,checked){const boxes=document.querySelectorAll('#review-accounts-table input');const box=boxes.find(box=>Number(box.value)===row);box.checked=checked;box.handlers.change[0]();},
  selected(){return [...reviewRows];},
};`;
vm.runInContext(source.slice(0,entry)+injected+source.slice(entry+'  refreshState();'.length),ctx);
const state={test_song_id:'1263607749',limits:{accounts:5,test_accounts:1,tests_per_account:5,workers:8,max_consecutive_failures:20},cohort:{ready_rows:[7],accounts:[]},proxy:{configured:true},sticky_pool:{configured:true,pool_size:2},failure_review:input.review ?? {accounts:[],total:0}};
(async()=>{
  const hooks=ctx.reviewHooks; hooks.init(state); hooks.renderFailureReview();
  if(input.select_all) hooks.selectAll(); else if(input.rows) hooks.select(input.rows);
  if(input.change) hooks.change(input.change.row,input.change.checked);
  if(input.submit) await hooks.startReviewPreparation();
  if(input.job) hooks.renderRunVisibility(input.job);
  if(input.render_job) hooks.renderJob(input.render_job);
  process.stdout.write(JSON.stringify({calls,selected:hooks.selected(),reviewText:get('review-accounts-table').textContent,error:get('review-error').textContent,metrics:get('job-overview').textContent,active:get('job-active-tests').textContent,results:get('job-results').textContent,summary:get('job-test-metrics').textContent,message:get('job-message').textContent,buttonDisabled:get('prepare-review-accounts').disabled,status:get('job-status').textContent,statusClass:get('job-status').className,progress:get('job-progress-label').textContent}));
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
"""


def browser_handler(**input):
    if NODE is None:
        pytest.skip("Node.js is needed to exercise actual browser handlers")
    result = subprocess.run([NODE, "-e", DRIVER, str(APP)], input=json.dumps(input), text=True,
                            encoding="utf-8", capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("connection,no_browser", [("direct", False), ("egypt", False), ("sticky", True), ("direct", True)])
def test_actual_review_preparation_handler_submits_only_selected_review_rows(connection, no_browser):
    result = browser_handler(review={"accounts": [review_account(31), review_account(35)], "total": 2},
                             rows=[35], submit=True, connection=connection, no_browser=no_browser)
    assert len(result["calls"]) == 1
    payload = result["calls"][0]["payload"]
    assert payload == {"action": "prepare", "review_rows": [35], "count": 1, "browser": "cloakbrowser",
                       "headless": not no_browser, "proxy_egypt": connection != "direct",
                       "proxy_sticky_pool": connection == "sticky", "reduce_browser_data": not no_browser,
                       "no_browser": no_browser}
    assert "start_row" not in payload and "rows" not in payload


def test_actual_select_all_uses_at_most_five_and_excludes_provider_rows_and_private_metadata():
    result = browser_handler(review={"accounts": [review_account(row, email=PRIVATE) for row in range(31, 38)] +
                                    [review_account(99, failure_code="request_transport_failed")], "total": 7},
                             select_all=True, submit=True)
    assert result["calls"][0]["payload"]["review_rows"] == list(range(31, 36))
    assert "99" not in result["reviewText"] and PRIVATE not in result["reviewText"]


def test_actual_individual_review_checkbox_changes_exact_selection():
    result = browser_handler(review={"accounts": [review_account(31), review_account(35)], "total": 2},
                             change={"row": 35, "checked": True}, submit=True)
    assert result["calls"][0]["payload"]["review_rows"] == [35]


@pytest.mark.parametrize("rows", [[], [99], list(range(31, 37))])
def test_actual_invalid_review_selection_sends_no_request(rows):
    result = browser_handler(review={"accounts": [review_account(row) for row in range(31, 38)], "total": 7}, rows=rows, submit=True)
    assert result["calls"] == [] and "Choose" in result["error"]


def test_actual_busy_review_handler_does_not_submit_or_change_selection():
    result = browser_handler(review={"accounts": [review_account(31)], "total": 1}, rows=[31], busy=True,
                             change={"row": 31, "checked": False}, submit=True)
    assert result["calls"] == [] and result["buttonDisabled"] is True
    assert result["selected"] == [31]


def test_actual_pending_terminal_job_is_a_warning_and_unlocks_review_selection():
    job = {"id": "provider-pending", "action": "play", "status": "completed_with_pending", "phase": "complete",
           "attempted": 1, "completed_tests": 1, "progress": {"completed": 1, "total": 1},
           "connection_pending": 1, "failed": 0, "succeeded": 0, "account_failed": 0,
           "results": [{"source_row": 35, "passed": False, "outcome": "connection_pending"}], "results_total": 1}
    result = browser_handler(review={"accounts": [review_account(31)], "total": 1}, rows=[31],
                             busy=True, render_job=job)
    assert result["status"] == "Completed with connections pending"
    assert result["statusClass"] == "badge warning"
    assert result["progress"] == "1 / 1 checks finished"
    assert result["buttonDisabled"] is False


def test_actual_live_view_separates_provider_waits_pending_connections_and_account_failures():
    job = {"action": "play", "status": "running", "attempted": 4, "completed_tests": 3,
           "progress": {"total": 8}, "succeeded": 1, "failed": 1, "account_failed": 1,
           "connection_pending": 1, "provider_retries": 2, "retried": 1,
           "active_workers": 1, "workers": 8, "consecutive_failures": 1, "max_consecutive_failures": 20,
           "elapsed_seconds": 8, "count": 1, "results_total": 3,
           "active_tests": [{"source_row": 39, "test_number": 1, "provider_attempts": 2, "connection_status": "waiting", "started_at": STAMP}],
           "results": [{"source_row": 31, "passed": False, "outcome": "account_failed"},
                       {"source_row": 35, "passed": False, "outcome": "connection_pending", "provider_attempts": 3, "retry_count": 2},
                       {"source_row": 37, "passed": True, "outcome": "succeeded"}]}
    result = browser_handler(job=job, filter="pending")
    assert "Account failures 1" in result["metrics"]
    assert "Other failures 0" in result["metrics"]
    assert "Connection pending 1" in result["metrics"] and "Provider retries 2" in result["metrics"]
    assert "Attempt 2 / 3" in result["active"] and "Waiting for provider" in result["active"]
    assert "Account 35" in result["results"] and "Connection pending" in result["results"]
    assert "Account 31" not in result["results"] and "Account 37" not in result["results"]


def test_actual_result_history_renders_sanitized_diagnostics_without_private_fields():
    job = ui_server.safe_report({"action": "play", "status": "completed_with_pending", "attempted": 1,
                                "completed_tests": 1, "progress": {"total": 1}, "results_total": 1,
                                "connection_pending": 1, "results": [{"source_row": 31, "passed": False,
                                    "outcome": "connection_pending", "attempt_history": [
                                        {"attempt": 1, "outcome": "provider_issue", "session_failure": {"code": "request_rate_limited", "stage": "relations", "http_status": 429, "url": PRIVATE}},
                                        {"attempt": 2, "outcome": "provider_issue", "password": PRIVATE}]}]})
    result = browser_handler(job=job)
    assert "HTTP 429" in result["results"] and "Connection attempts" in result["results"]
    assert PRIVATE not in json.dumps(result)
