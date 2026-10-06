"""Preparation sticky pool controls run offline against mocked local responses."""

import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest


APP = Path(__file__).resolve().parents[1] / "anghami_session/ui/app.js"
INDEX = APP.with_name("index.html")
NODE = shutil.which("node")
ROUTES = "synthetic-prep-first\nsynthetic-prep-second\nsynthetic-prep-third"
PRIVATE = "synthetic-private-error-must-not-be-shown"

DRIVER = r"""
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8'),input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.handlers={};this.value='';this.disabled=false;this.hidden=false;this.checked=false;this.dataset={};this.style={};this.attrs={};this._text='';this.className='';this.classes=new Set();this.classList={add:(name)=>this.classes.add(name),remove:(name)=>this.classes.delete(name),toggle:(name,active)=>active?this.classes.add(name):this.classes.delete(name)};}
 set textContent(value){this._text=String(value??'');this.children=[];}
 get textContent(){return this._text+this.children.map(child=>child.textContent??'').join(' ');}
 append(...children){this.children.push(...children);}
 appendChild(child){this.children.push(child);return child;}
 replaceChildren(...children){this._text='';this.children=children;}
 setAttribute(key,value){this.attrs[key]=String(value);}
 removeAttribute(key){delete this.attrs[key];if(key==='max')delete this.max;}
 addEventListener(type,fn){(this.handlers[type]??=[]).push(fn);}
}
const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
for(const[id,value]of Object.entries({'prepare-country':input.country??'EG','prepare-count':'2','start-row':'','prepare-method':'browser','browser':'cloakbrowser','prepare-connection-mode':input.connection??'direct','connection-mode':input.test_connection??'direct','login-connection-mode':'direct','song-id':'1263607749','job-result-filter':'all'}))get(id).value=value;
get('headless').checked=true;get('prepare-sticky-saved').hidden=true;get('prepare-sticky-error').hidden=true;
let serviceState={test_song_id:'1263607749',preparation_availability:{available:true,counts:{EG:20,LB:10,all:30},start_row:1},limits:{accounts:5,test_accounts:5,tests_per_account:5,workers:8,max_consecutive_failures:20},cohort:{ready_rows:[],accounts:[]},proxy:{configured:true},sticky_pool:{configured:!!input.saved,pool_size:input.saved?3:0},test_proxy:{configured:true,pool_size:11},reports:[],job:null,failure_review:{accounts:[{source_row:31,failure_code:'session_authentication_rejected',failed_stage:'relations',failed_at:'2026-10-04T00:00:00+00:00'}],total:1}};
const calls=[],pendingLocks=[],snapshots=[];
let hooks;
const ctx=vm.createContext({document:{getElementById:get,createElement:tag=>new Element(tag),createElementNS:(_ns,tag)=>new Element(tag),createTextNode:text=>({textContent:text}),querySelector:selector=>selector.startsWith('meta')?{content:'synthetic-token'}:get(selector),querySelectorAll:()=>[]},CSS:{supports:()=>!input.mask_unsupported},history:{replaceState(){}},location:{hash:'#accounts'},TextEncoder,setTimeout:()=>1,clearTimeout(){},fetch:async(path,opts={})=>{
 calls.push({path,payload:opts.body?JSON.parse(opts.body):null});
 if(path==='/api/preparation-proxy'){
  pendingLocks.push({routes:get('prepare-sticky-routes').disabled,show:get('prepare-sticky-show-links').disabled,save:get('prepare-sticky-save').disabled,discard:get('prepare-sticky-discard').disabled,remove:get('prepare-sticky-remove').disabled,prepare:get('prepare-accounts').disabled,country:get('prepare-country').disabled});
  if(input.prepare_during_save)await hooks.startPreparation('prepare');
  if(input.save_error)return{ok:false,status:400,json:async()=>({error:input.private_error})};
  const summary=input.response??{configured:true,provider:'PacketStream',country:'EG',sticky:true,pool_size:2};
  if(summary.configured===true)serviceState.sticky_pool=summary;
  return{ok:true,status:200,json:async()=>summary};
 }
 if(path==='/api/preparation-proxy/remove'){
  pendingLocks.push({routes:get('prepare-sticky-routes').disabled,show:get('prepare-sticky-show-links').disabled,save:get('prepare-sticky-save').disabled,discard:get('prepare-sticky-discard').disabled,remove:get('prepare-sticky-remove').disabled,prepare:get('prepare-accounts').disabled,country:get('prepare-country').disabled});
  if(input.remove_error)return{ok:false,status:400,json:async()=>({error:input.private_error})};
  const summary=input.remove_response??{configured:false,pool_size:0};
  if(summary.configured===false&&summary.pool_size===0)serviceState.sticky_pool=summary;
  return{ok:true,status:200,json:async()=>summary};
 }
 if(path==='/api/state')return input.refresh_error?{ok:false,status:503,json:async()=>({error:'Local state refresh is unavailable.'})}:{ok:true,status:200,json:async()=>serviceState};
 return{ok:true,status:202,json:async()=>({job:{id:'synthetic-prep-job',action:'prepare',status:'queued',phase:'queued',progress:{completed:0,total:2},results:[]}})};
}});
const entry=source.lastIndexOf('  refreshState();');
const injection=`  globalThis.prepStickyHooks={init(value){state=value;busy=${JSON.stringify(!!input.busy)};requestPending=${JSON.stringify(!!input.pending)};reviewRows.add(31);},updateProxyMode,startPreparation,startReviewPreparation,lock(value){setBusy(value);},snapshot(){return{needsSave:prepareStickyNeedsSave,sticky:state?.sticky_pool,testProxy:state?.test_proxy};}};`;
vm.runInContext(source.slice(0,entry)+injection+source.slice(entry+'  refreshState();'.length),ctx);
function snapshot(){return{...hooks.snapshot(),visible:!get('prepare-sticky-settings').hidden,routes:get('prepare-sticky-routes').value,show:get('prepare-sticky-show-links').checked,masked:get('prepare-sticky-routes').classes.has('masked'),inputHidden:get('prepare-sticky-routes').hidden,maskHintHidden:get('prepare-sticky-mask-hint').hidden,lineCount:get('prepare-sticky-line-count').textContent,badge:get('prepare-sticky-badge').textContent,hint:get('prepare-proxy-mode-hint').textContent,savedText:get('prepare-sticky-saved').textContent,savedHidden:get('prepare-sticky-saved').hidden,error:get('prepare-sticky-error').textContent,accountsError:get('accounts-error').textContent,reviewError:get('review-error').textContent,prepareDisabled:get('prepare-accounts').disabled,reviewDisabled:get('prepare-review-accounts').disabled,saveDisabled:get('prepare-sticky-save').disabled,discardHidden:get('prepare-sticky-discard').hidden,removeHidden:get('prepare-sticky-remove').hidden,removeDisabled:get('prepare-sticky-remove').disabled,method:get('prepare-method').value,country:get('prepare-country').value};}
(async()=>{
 hooks=ctx.prepStickyHooks;hooks.init(JSON.parse(JSON.stringify(serviceState)));hooks.lock(!!input.busy);if(input.pending)hooks.init(JSON.parse(JSON.stringify(serviceState)));hooks.updateProxyMode();snapshots.push(snapshot());
 for(const action of input.actions??[]){
  if(action.kind==='connection'){get('prepare-connection-mode').value=action.value;await get('prepare-connection-mode').handlers.change[0]();}
  if(action.kind==='input'){get('prepare-sticky-routes').value=action.value;await get('prepare-sticky-routes').handlers.input[0]();}
  if(action.kind==='test-input'){get('test-session-route').value=action.value;await get('test-session-route').handlers.input[0]();}
  if(action.kind==='show'){get('prepare-sticky-show-links').checked=action.value;get('prepare-sticky-show-links').handlers.change[0]();}
  if(action.kind==='save')await get('prepare-sticky-save').handlers.click[0]();
  if(action.kind==='discard')await get('prepare-sticky-discard').handlers.click[0]();
  if(action.kind==='remove')await get('prepare-sticky-remove').handlers.click[0]();
  if(action.kind==='lock')hooks.lock(action.value);
  if(action.kind==='prepare')await hooks.startPreparation('prepare');
  if(action.kind==='preview')await hooks.startPreparation('preview');
  if(action.kind==='review')await hooks.startReviewPreparation();
  snapshots.push(snapshot());
 }
 process.stdout.write(JSON.stringify({calls,pendingLocks,snapshots}));
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
"""


def browser(**options):
    if NODE is None:
        pytest.skip("Node.js is needed to exercise actual UI handlers")
    completed = subprocess.run([NODE, "-e", DRIVER, str(APP)], input=json.dumps(options),
                               text=True, encoding="utf-8", capture_output=True, timeout=10, check=False)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


class Inputs(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.controls = {}
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.controls[attrs["id"]] = {"tag": tag, **attrs}


def test_preparation_paste_box_is_accessible_multiline_and_separate_from_workbench():
    source = INDEX.read_text(encoding="utf-8")
    document = Inputs(source)
    routes = document.controls["prepare-sticky-routes"]
    assert routes["tag"] == "textarea" and routes["rows"] == "6"
    assert routes["autocomplete"] == "off" and routes["spellcheck"] == "false"
    assert routes["maxlength"] == "1048576"
    assert set(routes["aria-describedby"].split()) <= document.controls.keys()
    assert '<label for="prepare-sticky-routes">' in source
    assert document.controls["prepare-sticky-show-links"]["type"] == "checkbox"
    assert "checked" not in document.controls["prepare-sticky-show-links"]
    assert "test-session-route" in document.controls


@pytest.mark.parametrize("saved", [False, True])
def test_selecting_saved_sticky_mode_immediately_shows_paste_box_even_with_existing_pool(saved):
    result = browser(saved=saved, actions=[{"kind": "connection", "value": "sticky"}])
    before, after = result["snapshots"]
    assert before["visible"] is False and after["visible"] is True
    assert after["method"] == "http"
    assert after["prepareDisabled"] is not saved
    assert result["calls"] == []


@pytest.mark.parametrize("connection", ["direct", "egypt"])
def test_other_preparation_connections_hide_paste_controls_and_stay_independent(connection):
    result = browser(connection="sticky", actions=[{"kind": "connection", "value": connection}])
    assert result["snapshots"][-1]["visible"] is False
    assert result["calls"] == []


def test_multiline_save_sends_only_local_prep_payload_clears_credentials_and_updates_count():
    result = browser(connection="sticky", actions=[{"kind": "input", "value": ROUTES},
                     {"kind": "show", "value": True}, {"kind": "save"}])
    assert [call["path"] for call in result["calls"]] == ["/api/preparation-proxy", "/api/state"]
    assert result["calls"][0]["payload"] == {"routes": ROUTES}
    pasted, final = result["snapshots"][1], result["snapshots"][-1]
    assert pasted["lineCount"] == "3 lines pasted" and pasted["prepareDisabled"] is True
    assert final["routes"] == "" and final["show"] is False and final["masked"] is True
    assert final["needsSave"] is False and final["prepareDisabled"] is False
    assert final["badge"] == "2 routes saved" and final["savedHidden"] is False
    assert "2 unique EG sticky routes saved securely for preparation" in final["savedText"]
    assert final["testProxy"]["pool_size"] == 11
    assert all(all(locks.values()) for locks in result["pendingLocks"])


def test_us_sticky_pool_save_feedback_uses_server_returned_country():
    result = browser(connection="sticky", response={
        "configured": True, "provider": "PacketStream", "country": "US",
        "sticky": True, "pool_size": 1,
    }, actions=[{"kind": "input", "value": "synthetic-us-route"}, {"kind": "save"}])
    final = result["snapshots"][-1]
    assert final["badge"] == "1 route saved"
    assert "1 unique US sticky route saved securely for preparation" in final["savedText"]


def test_input_mask_and_explicit_show_checkbox_follow_browser_support():
    result = browser(connection="sticky", mask_unsupported=True,
                     actions=[{"kind": "input", "value": ROUTES}, {"kind": "show", "value": True}])
    initial, shown = result["snapshots"][1], result["snapshots"][-1]
    assert initial["inputHidden"] is True and initial["maskHintHidden"] is False
    assert shown["inputHidden"] is False and shown["maskHintHidden"] is True


def test_confirmed_routes_stay_saved_but_preparation_waits_for_fresh_availability_after_refresh_failure():
    result = browser(connection="sticky", saved=True, refresh_error=True,
                     actions=[{"kind": "input", "value": ROUTES}, {"kind": "save"}])
    final = result["snapshots"][-1]
    assert final["routes"] == "" and final["show"] is False
    assert final["needsSave"] is False and final["prepareDisabled"] is True
    assert final["sticky"]["pool_size"] == 2 and final["savedHidden"] is False
    assert final["error"] == ""
    assert [call["path"] for call in result["calls"]] == ["/api/preparation-proxy", "/api/state"]


@pytest.mark.parametrize("saved", [False, True])
def test_unsaved_preparation_routes_block_preparation_and_explicit_review_but_allow_offline_preview(saved):
    result = browser(connection="sticky", saved=saved, actions=[{"kind": "input", "value": ROUTES},
                     {"kind": "prepare"}, {"kind": "review"}, {"kind": "preview"}])
    draft = result["snapshots"][1]
    assert draft["prepareDisabled"] is True and draft["reviewDisabled"] is True
    assert draft["discardHidden"] is not saved
    assert [call["path"] for call in result["calls"]] == ["/api/jobs"]
    payload = result["calls"][0]["payload"]
    assert payload["action"] == "preview" and payload["proxy_sticky_pool"] is True
    assert payload["account_country"] == "EG" and "routes" not in payload


def test_save_error_uses_fixed_message_clears_credentials_and_keeps_old_pool_until_explicit_discard():
    result = browser(connection="sticky", saved=True, save_error=True, private_error=PRIVATE,
                     actions=[{"kind": "input", "value": ROUTES}, {"kind": "save"},
                              {"kind": "prepare"}, {"kind": "review"}, {"kind": "discard"}])
    failed, final = result["snapshots"][2], result["snapshots"][-1]
    assert failed["routes"] == "" and failed["show"] is False
    assert failed["needsSave"] is True and failed["sticky"]["pool_size"] == 3
    assert failed["prepareDisabled"] is True and failed["discardHidden"] is False
    assert "Use saved routes" in failed["error"] and PRIVATE not in failed["error"]
    assert final["needsSave"] is False and final["prepareDisabled"] is False
    assert final["sticky"]["pool_size"] == 3 and final["error"] == ""
    assert [call["path"] for call in result["calls"]] == ["/api/preparation-proxy"]


def test_manually_clearing_pasted_draft_does_not_silently_reuse_old_routes():
    result = browser(connection="sticky", saved=True, actions=[{"kind": "input", "value": ROUTES},
                     {"kind": "input", "value": ""}, {"kind": "prepare"}])
    final = result["snapshots"][-1]
    assert final["needsSave"] is True and final["prepareDisabled"] is True
    assert result["calls"] == []


@pytest.mark.parametrize("fields", [{"busy": True}, {"pending": True}])
def test_save_and_discard_do_nothing_while_busy_or_request_pending(fields):
    result = browser(connection="sticky", saved=True, actions=[{"kind": "input", "value": ROUTES},
                     {"kind": "save"}, {"kind": "discard"}], **fields)
    assert result["calls"] == []
    assert result["snapshots"][-1]["routes"] == ROUTES


def test_prepare_cannot_race_while_route_pool_save_is_in_progress():
    result = browser(connection="sticky", prepare_during_save=True,
                     actions=[{"kind": "input", "value": ROUTES}, {"kind": "save"}])
    assert all(call["path"] != "/api/jobs" for call in result["calls"])
    assert all(all(locks.values()) for locks in result["pendingLocks"])


@pytest.mark.parametrize("action", ["prepare", "review"])
def test_successful_save_unblocks_preparation_and_preserves_country_and_review_selection(action):
    result = browser(connection="sticky", country="LB", actions=[{"kind": "input", "value": ROUTES},
                     {"kind": "save"}, {"kind": action}])
    payload = result["calls"][-1]["payload"]
    assert result["calls"][-1]["path"] == "/api/jobs"
    assert payload["proxy_sticky_pool"] is True and payload["proxy_egypt"] is True
    if action == "prepare":
        assert payload["account_country"] == "LB" and payload["count"] == 2
    else:
        assert payload["review_rows"] == [31] and "account_country" not in payload


def test_workbench_unsaved_routes_do_not_block_a_saved_preparation_pool():
    result = browser(connection="sticky", test_connection="session", saved=True,
                     actions=[{"kind": "test-input", "value": "synthetic-other-pool"}, {"kind": "prepare"}])
    assert result["calls"][0]["path"] == "/api/jobs"
    assert result["calls"][0]["payload"]["proxy_sticky_pool"] is True


def test_invalid_save_size_clears_draft_without_submitting_or_enabling_old_routes():
    result = browser(connection="sticky", saved=True,
                     actions=[{"kind": "input", "value": "x\n" * 10001}, {"kind": "save"}])
    final = result["snapshots"][-1]
    assert result["calls"] == []
    assert final["routes"] == "" and final["needsSave"] is True
    assert final["prepareDisabled"] is True and "1–10,000" in final["error"]


@pytest.mark.parametrize("response", [{"configured": False}, {"configured": True, "pool_size": 0},
                                     {"configured": True, "pool_size": "2"}])
def test_unconfirmed_save_response_does_not_unlock_preparation(response):
    result = browser(connection="sticky", saved=True, response=response,
                     actions=[{"kind": "input", "value": ROUTES}, {"kind": "save"}])
    final = result["snapshots"][-1]
    assert final["routes"] == "" and final["needsSave"] is True
    assert final["prepareDisabled"] is True and final["sticky"]["pool_size"] == 3


@pytest.mark.parametrize("saved", [False, True])
def test_remove_button_is_visible_only_for_a_saved_preparation_pool(saved):
    result = browser(connection="sticky", saved=saved)
    assert result["snapshots"][-1]["removeHidden"] is not saved
    assert result["calls"] == []


def test_remove_clears_only_preparation_pool_and_its_draft_then_blocks_preparation():
    result = browser(connection="sticky", saved=True,
                     actions=[{"kind": "input", "value": ROUTES}, {"kind": "show", "value": True},
                              {"kind": "remove"}, {"kind": "prepare"}, {"kind": "preview"}])
    removed = result["snapshots"][3]
    assert [call["path"] for call in result["calls"]] == ["/api/preparation-proxy/remove", "/api/state", "/api/jobs"]
    assert result["calls"][0]["payload"] == {}
    assert result["calls"][-1]["payload"]["action"] == "preview"
    assert removed["sticky"] == {"configured": False, "pool_size": 0}
    assert removed["testProxy"] == {"configured": True, "pool_size": 11}
    assert removed["routes"] == "" and removed["show"] is False and removed["needsSave"] is False
    assert removed["badge"] == "No saved routes" and removed["removeHidden"] is True
    assert removed["savedText"] == "Preparation route pool removed." and removed["savedHidden"] is False
    assert removed["prepareDisabled"] is True and removed["reviewDisabled"] is True
    assert all(all(locks.values()) for locks in result["pendingLocks"])


@pytest.mark.parametrize("fields", [{"busy": True}, {"pending": True}, {"saved": False}])
def test_remove_sends_no_request_if_busy_pending_or_pool_is_missing(fields):
    result = browser(connection="sticky", actions=[{"kind": "remove"}], **({"saved": True} | fields))
    assert result["calls"] == []


def test_remove_failure_preserves_prior_pool_and_draft_without_exposing_error_text():
    result = browser(connection="sticky", saved=True, remove_error=True, private_error=PRIVATE,
                     actions=[{"kind": "input", "value": ROUTES}, {"kind": "remove"}])
    final = result["snapshots"][-1]
    assert final["sticky"] == {"configured": True, "pool_size": 3}
    assert final["testProxy"] == {"configured": True, "pool_size": 11}
    assert final["routes"] == ROUTES and final["needsSave"] is True
    assert final["removeHidden"] is False and final["removeDisabled"] is False
    assert "could not be removed" in final["error"] and PRIVATE not in final["error"]
    assert [call["path"] for call in result["calls"]] == ["/api/preparation-proxy/remove"]


def test_confirmed_removal_stays_removed_when_following_state_refresh_fails():
    result = browser(connection="sticky", saved=True, refresh_error=True,
                     actions=[{"kind": "remove"}])
    final = result["snapshots"][-1]
    assert final["sticky"] == {"configured": False, "pool_size": 0}
    assert final["prepareDisabled"] is True and final["removeHidden"] is True
    assert final["savedText"] == "Preparation route pool removed." and final["error"] == ""


@pytest.mark.parametrize("response", [{"configured": True, "pool_size": 0}, {"configured": False, "pool_size": 1},
                                     {"configured": False, "pool_size": "0"}])
def test_unconfirmed_removal_preserves_prior_pool(response):
    result = browser(connection="sticky", saved=True, remove_response=response,
                     actions=[{"kind": "remove"}])
    final = result["snapshots"][-1]
    assert final["sticky"] == {"configured": True, "pool_size": 3}
    assert final["prepareDisabled"] is False and final["removeHidden"] is False
    assert "could not be removed" in final["error"]
