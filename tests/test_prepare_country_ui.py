"""Country preparation browser handlers use mocked local requests only."""

import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "anghami_session/ui/app.js"
INDEX = APP.with_name("index.html")
NODE = shutil.which("node")


class Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = {}
        self.options = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.controls[attrs["id"]] = attrs
        if tag == "select":
            self.current = attrs.get("id")
        if tag == "option" and self.current == "prepare-country":
            self.options.append(attrs)

    def handle_endtag(self, tag):
        if tag == "select":
            self.current = None


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
  removeAttribute(key) { delete this.attrs[key]; if(key==='max')delete this.max; }
  addEventListener(type,fn) { (this.handlers[type] ??= []).push(fn); }
}
const elements = new Map();
const get = id => { if(!elements.has(id)) elements.set(id,new Element()); return elements.get(id); };
for(const [id,value] of Object.entries({'prepare-country':input.country ?? 'EG','prepare-count':String(input.count ?? '2'),'prepare-workers':String(input.workers ?? '1'),'start-row':input.start_row ?? '777','prepare-method':input.no_browser ? 'http' : 'browser','browser':'cloakbrowser','prepare-connection-mode':input.connection ?? 'direct','connection-mode':input.test_connection ?? 'direct','login-connection-mode':'direct','session-review-connection-mode':input.session_review_connection ?? 'direct','login-rows':input.login_rows ?? '','song-id':'1263607749','job-result-filter':'all','test-account-country':input.test_country ?? '', 'test-count':String(input.test_count ?? '1'),'test-workers':String(input.test_workers ?? '8'),'test-failure-limit':String(input.failures ?? '20'),'random-count':String(input.random_count ?? '1')})) get(id).value=String(value);
get('headless').checked=true;
get('prepare-reduce-browser-data').checked=true;
const calls=[],availabilityCalls=[];
const inputs = element => element.children.flatMap(child=>[...(child.tagName==='input' ? [child] : []),...(child.children ? inputs(child) : [])]);
const ctx=vm.createContext({
  document:{getElementById:get,createElement:tag=>new Element(tag),createElementNS:(_ns,tag)=>new Element(tag),createTextNode:text=>({textContent:text}),querySelector:selector=>selector.startsWith('meta') ? {content:'synthetic-token'} : get(selector),querySelectorAll:selector=>['#account-picker input','#cohort-table input','#session-review-accounts-table input','#review-accounts-table input'].includes(selector) ? inputs(get(selector.split(' ')[0].slice(1))) : []},
  ConsoleSelection:require(require('node:path').join(require('node:path').dirname(process.argv[1]),'selection.js')),
  CSS:{supports:()=>true},history:{replaceState(){}},location:{hash:'#accounts'},TextEncoder,
  setTimeout:()=>1,clearTimeout(){},fetch:async(path,opts={})=>{
    if(path.startsWith('/api/preparation-availability?')){
      availabilityCalls.push(path);
      const row=Number(path.split('=')[1]);
      const value=input.offset_availabilities?.[String(row)] ?? input.offset_availability ?? {available:true,counts:{EG:9007199254740991,LB:9007199254740991,all:9007199254740991},start_row:row};
      return {ok:!input.availability_error,status:input.availability_error ? 503 : 200,json:async()=>value};
    }
    calls.push({path,payload:opts.body ? JSON.parse(opts.body) : null});
    return {ok:true,status:202,json:async()=>({job:{id:'synthetic-country-job',action:input.action ?? 'prepare',status:'queued',phase:'queued',progress:{completed:0,total:2},results:[]}})};
  },
});
const entry=source.lastIndexOf('  refreshState();');
if(entry<0) throw Error('Missing UI entrypoint');
const injection=`  globalThis.countryHooks={
  init(value){state=value;busy=${JSON.stringify(!!input.busy)};requestPending=${JSON.stringify(!!input.pending)};updateSongState();},
  updatePreparationCountry,updatePreparationMethod,startPreparation,startReviewPreparation,startLogin,startTest,startSessionReview,renderSessionReview,renderAccounts,renderState,renderJob,renderRunVisibility,updateSelections,
  selectReview(rows){reviewRows.clear();rows.forEach(row=>reviewRows.add(row));},
  selectSessionReview(rows){sessionReviewRows.clear();rows.forEach(row=>sessionReviewRows.add(row));updateSessionReviewSelection();},
  sessionSelection(){return [...sessionReviewRows].sort((a,b)=>a-b);},
  changeCountry(value){$('prepare-country').value=value;$('prepare-country').handlers.change[0]();},
  changeMethod(value){$('prepare-method').value=value;$('prepare-method').handlers.change[0]();},
  changeWorkers(value){$('prepare-workers').value=String(value);$('prepare-workers').handlers.input[0]();},
  changeStartRow(value){$('start-row').value=String(value);$('start-row').handlers.input[0]();},
  changePrepareCount(value){$('prepare-count').value=String(value);$('prepare-count').handlers.input[0]();},
  updateAvailability(value){state.preparation_availability=value;updatePreparationCountry();},
  changeFailures(value){$('test-failure-limit').value=String(value);$('test-failure-limit').handlers.input[0]();},
  selectTests(rows){firstSelection=false;selectedRows.clear();rows.forEach(row=>selectedRows.add(row));},
  testSelection(){return [...selectedRows].sort((a,b)=>a-b);},
  changeTestCountry(value){$('test-account-country').value=value;$('test-account-country').handlers.change[0]();},
  lock(value){setBusy(value);},
};`;
vm.runInContext(source.slice(0,entry)+injection+source.slice(entry+'  refreshState();'.length),ctx);
const state={test_song_id:'1263607749',preparation_availability:input.availability ?? {available:true,counts:{EG:9007199254740991,LB:9007199254740991,all:9007199254740991},start_row:1},limits:{accounts:5,test_accounts:input.test_account_limit ?? 5,tests_per_account:5,workers:8,preparation_workers:input.worker_limit ?? 50,max_consecutive_failures:20},cohort:{ready_rows:input.ready_rows ?? [],accounts:input.ready_accounts ?? []},proxy:{configured:input.proxy_configured ?? true},sticky_pool:{configured:true,pool_size:2},test_proxy:{configured:true,pool_size:2},session_review:input.session_review ?? {accounts:[],total:0,held_rows:[],session_review_rows:[]},failure_review:{accounts:(input.review_rows ?? [31]).map(source_row=>({source_row,failure_code:'session_authentication_rejected',failed_stage:'relations',failed_at:'2026-10-04T00:00:00+00:00'})),total:(input.review_rows ?? [31]).length}};
if(input.omit_availability)delete state.preparation_availability;
if(input.omit_worker_limit)delete state.limits.preparation_workers;
(async()=>{
  const hooks=ctx.countryHooks;
  hooks.init(input.no_state ? null : state);
  hooks.updatePreparationCountry();
  hooks.updatePreparationMethod();
  hooks.updateSelections();
  const initialDisabled=get('start-row').disabled;
  if(input.changed_country!==undefined) hooks.changeCountry(input.changed_country);
  if(input.changed_start_row!==undefined) hooks.changeStartRow(input.changed_start_row);
  if(input.changed_prepare_count!==undefined) hooks.changePrepareCount(input.changed_prepare_count);
  if(input.updated_availability)hooks.updateAvailability(input.updated_availability);
  for(const operation of input.preparation_operations ?? []){
    if(operation.type==='country')hooks.changeCountry(operation.value);
    else if(operation.type==='start_row')hooks.changeStartRow(operation.value);
    else if(operation.type==='count')hooks.changePrepareCount(operation.value);
    else if(operation.type==='availability')hooks.updateAvailability(operation.value);
  }
  if(!input.before_availability_response)await new Promise(resolve=>setImmediate(resolve));
  if(input.changed_method!==undefined) hooks.changeMethod(input.changed_method);
  if(input.changed_workers!==undefined) hooks.changeWorkers(input.changed_workers);
  if(input.changed_failures!==undefined) hooks.changeFailures(input.changed_failures);
  if(input.lock!==undefined) hooks.lock(input.lock);
  if(input.render)hooks.renderState();
  if(input.selected_rows)hooks.selectTests(input.selected_rows);
  if(input.render_picker)hooks.renderAccounts();
  if(input.render_session_review)hooks.renderSessionReview();
  if(input.session_selected_rows)hooks.selectSessionReview(input.session_selected_rows);
  for(const operation of input.session_operations ?? []){
    if(operation.type==='all')get('session-review-select-all').handlers.click[0]();
    else if(operation.type==='clear')get('session-review-clear').handlers.click[0]();
    else if(operation.type==='manual'){const checkbox=inputs(get('session-review-accounts-table')).find(item=>item.value===String(operation.row));if(!checkbox)throw Error('Mock session-review checkbox missing');checkbox.checked=operation.checked;checkbox.handlers.change[0]();}
  }
  for(const operation of input.selection_operations ?? []){
    if(operation.type==='country')hooks.changeTestCountry(operation.value);
    else if(operation.type==='all')get('select-all-accounts').handlers.click[0]();
    else if(operation.type==='clear')get('clear-accounts').handlers.click[0]();
    else if(operation.type==='random'){get('random-count').value=String(operation.count);get('select-random-accounts').handlers.click[0]();}
    else if(operation.type==='manual'){const checkbox=inputs(get('account-picker')).find(item=>item.value===String(operation.row));if(!checkbox)throw Error('Mock account checkbox missing');checkbox.checked=operation.checked;checkbox.handlers.change[0]();}
  }
  if(input.job)hooks.renderJob(input.job);
  if(input.result_filter){get('job-result-filter').value=input.result_filter;hooks.renderRunVisibility(input.job);}
  if(input.updated_session_review){state.session_review=input.updated_session_review;if(input.updated_ready_rows)state.cohort.ready_rows=input.updated_ready_rows;hooks.renderState();}
  const before={prepareCountDisabled:get('prepare-count').disabled,prepareCountHint:get('prepare-count-hint').textContent,previewDisabled:get('preview-accounts').disabled,prepareDisabled:get('prepare-accounts').disabled,countryDisabled:get('prepare-country').disabled,startDisabled:get('start-row').disabled,startRow:get('start-row').value,country:get('prepare-country').value,countryHint:get('prepare-country-hint').textContent,startHint:get('start-row-hint').textContent,connection:get('prepare-connection-mode').value,prepareMax:get('prepare-count').max ?? null,testMax:get('test-count').max ?? null,prepareCount:get('prepare-count').value,workerValue:get('prepare-workers').value,workerDisabled:get('prepare-workers').disabled,workerMax:get('prepare-workers').max ?? null,workerHint:get('prepare-workers-hint').textContent,testWorkerMax:get('test-workers').max ?? null,failureMax:get('test-failure-limit').max ?? null,failureValue:get('test-failure-limit').value,jobMetrics:get('job-test-metrics').textContent,jobOverview:get('job-overview').textContent,selectedRows:hooks.testSelection(),pickerRows:inputs(get('account-picker')).map(item=>Number(item.value)),testCountry:get('test-account-country').value,testCountryDisabled:get('test-account-country').disabled,testCountryHint:get('test-account-country-hint').textContent,randomMax:get('random-count').max,randomValue:get('random-count').value,emptyTitle:get('empty-accounts-title').textContent,cohortText:get('cohort-table').textContent,runSummary:get('run-summary').textContent,testWorkerHint:get('test-workers-hint').textContent,playDisabled:get('run-play').disabled,likeDisabled:get('run-like').disabled,globalReady:get('stat-ready').textContent};
  if(input.submit==='review'){hooks.selectReview(input.review_rows ?? [31]);await hooks.startReviewPreparation();}
  else if(input.submit==='login')await hooks.startLogin();
  else if(input.submit==='test')await hooks.startTest(input.test_action ?? 'play');
  else if(input.submit==='session-review')await hooks.startSessionReview();
  else if(input.submit) await hooks.startPreparation(input.action ?? 'prepare');
  process.stdout.write(JSON.stringify({calls,availabilityCalls,initialDisabled,before,error:get('accounts-error').textContent,reviewError:get('review-error').textContent,loginError:get('login-error').textContent,workbenchError:get('workbench-error').textContent,selectionError:get('selection-error').textContent,sessionReviewError:get('session-review-error').textContent,sessionReviewText:get('session-review-accounts-table').textContent,sessionReviewSelected:hooks.sessionSelection(),sessionReviewButtonDisabled:get('check-review-sessions').disabled,sessionReviewCount:get('session-review-account-count').textContent,sessionReviewSelectionText:get('session-review-selection-count').textContent,sessionReviewBoxes:inputs(get('session-review-accounts-table')).map(item=>({row:Number(item.value),disabled:item.disabled,checked:item.checked})),cohortBoxes:inputs(get('cohort-table')).map(item=>({row:Number(item.value),disabled:item.disabled,checked:item.checked})),failureReviewText:get('review-accounts-table').textContent,jobResults:get('job-results').textContent,jobStatus:get('job-status').textContent,jobMessage:get('job-message').textContent}));
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
"""


def browser(**options):
    if NODE is None:
        pytest.skip("Node.js is needed to exercise real UI handlers")
    completed = subprocess.run(
        [NODE, "-e", DRIVER, str(APP)], input=json.dumps(options), text=True,
        encoding="utf-8", capture_output=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_registered_country_choice_is_accessible_egypt_default_and_independent_from_route():
    document = Controls()
    source = INDEX.read_text(encoding="utf-8")
    document.feed(source)
    assert '<label for="prepare-country">Registered account country</label>' in source
    assert document.controls["prepare-country"]["aria-describedby"] == "prepare-country-hint"
    assert [option["value"] for option in document.options] == ["EG", "LB", ""]
    assert "selected" in document.options[0]
    assert "disabled" in document.controls["start-row"]
    assert document.controls["start-row"]["aria-describedby"] == "start-row-hint"
    assert "prepare-connection-mode" in document.controls


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("country", ["EG", "LB"])
@pytest.mark.parametrize("connection", ["direct", "egypt", "sticky"])
def test_country_preview_and_prepare_payloads_filter_the_pool_without_changing_connection(action, country, connection):
    result = browser(action=action, country=country, connection=connection, submit=True, no_browser=connection == "sticky")
    assert len(result["calls"]) == 1
    assert result["calls"][0]["path"] == "/api/jobs"
    payload = result["calls"][0]["payload"]
    assert payload["action"] == action and payload["count"] == 2
    assert payload["account_country"] == country
    assert "start_row" not in payload and "review_rows" not in payload
    assert payload["proxy_egypt"] is (connection != "direct")
    assert payload["proxy_sticky_pool"] is (connection == "sticky")
    assert result["before"]["startDisabled"] is True
    assert result["before"]["countryDisabled"] is False
    assert result["before"]["connection"] == connection
    assert f"eligible {country} accounts" in result["before"]["countryHint"]
    assert "connection choice is independent" in result["before"]["countryHint"]
    assert result["error"] == ""


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("connection", ["direct", "egypt"])
def test_any_country_retains_optional_starting_row_and_omits_country_filter(action, connection):
    result = browser(action=action, country="", connection=connection, submit=True)
    payload = result["calls"][0]["payload"]
    assert "account_country" not in payload
    assert payload["start_row"] == 777
    assert result["before"]["startDisabled"] is False
    assert "row order" in result["before"]["countryHint"]


def test_empty_starting_row_still_uses_the_next_available_row():
    payload = browser(country="", start_row="", submit=True)["calls"][0]["payload"]
    assert "start_row" not in payload and "account_country" not in payload


@pytest.mark.parametrize("changed_country", ["EG", "LB"])
def test_country_change_disables_starting_row_but_preserves_value_and_connection(changed_country):
    result = browser(country="", changed_country=changed_country, connection="egypt", submit=True)
    assert result["initialDisabled"] is False
    assert result["before"]["startDisabled"] is True
    assert result["before"]["startRow"] == "777"
    assert result["before"]["connection"] == "egypt"
    assert result["calls"][0]["payload"]["account_country"] == changed_country
    assert "start_row" not in result["calls"][0]["payload"]


def test_switching_to_any_country_restores_starting_row_without_changing_route():
    result = browser(country="EG", changed_country="", connection="direct", submit=True)
    assert result["initialDisabled"] is True
    assert result["before"]["startDisabled"] is False
    assert result["calls"][0]["payload"]["start_row"] == 777
    assert "account_country" not in result["calls"][0]["payload"]


@pytest.mark.parametrize("country", ["EG", "LB", ""])
@pytest.mark.parametrize("fields", [{"busy": True}, {"pending": True}, {"no_state": True}, {"lock": True}])
def test_country_and_starting_row_controls_lock_for_busy_pending_and_initial_load(country, fields):
    result = browser(country=country, **fields)
    assert result["before"]["countryDisabled"] is True
    assert result["before"]["startDisabled"] is True


@pytest.mark.parametrize("country", ["EG", "LB", ""])
def test_unlocking_respects_current_country_mode(country):
    result = browser(country=country, busy=True, lock=False)
    assert result["before"]["countryDisabled"] is False
    assert result["before"]["startDisabled"] is bool(country)


@pytest.mark.parametrize("country", ["EG", "LB"])
@pytest.mark.parametrize("connection", ["direct", "egypt", "sticky"])
def test_failed_account_review_preparation_keeps_exact_rows_and_ignores_country_selection(country, connection):
    result = browser(country=country, connection=connection, no_browser=connection == "sticky", submit="review")
    payload = result["calls"][0]["payload"]
    assert payload["review_rows"] == [31] and payload["count"] == 1
    assert "account_country" not in payload and "start_row" not in payload
    assert payload["proxy_egypt"] is (connection != "direct")
    assert payload["proxy_sticky_pool"] is (connection == "sticky")


def test_invalid_country_submits_no_request():
    result = browser(country="XX", submit=True)
    assert result["calls"] == []
    assert "Choose Egypt, Lebanon or any country" in result["error"]
