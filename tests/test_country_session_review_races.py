"""Country checkpoints hold uncertain identities without replay or false failure."""

import json

import pytest

from anghami_session import country_preparation as country
from anghami_session.errors import RequestFailure, SessionReviewRequiredError
from test_country_preparation import imported
from test_country_preparation_workers import imported as parallel_imported


def hold(store, row):
    with store._db:
        store._db.execute("CREATE TABLE IF NOT EXISTS account_session_review(source_row INTEGER PRIMARY KEY,email_key BLOB)")
        store._db.execute("INSERT OR REPLACE INTO account_session_review SELECT source_row,email_key FROM accounts WHERE source_row=?",(row,))
        store._db.execute("UPDATE accounts SET state='session_review_pending' WHERE source_row=?",(row,))


def completed(selected, row, *, proxy=None, parallel=False):
    if parallel:
        selected.attach(row,{"synthetic_saved_row":row},proxy=proxy)
    selected.enable_test_account(row)
    return {"passed":True,"selected_rows":[row],"prepared_rows":[row],
            "prepared_account_count":1,"phase":"complete"}


def seed(path, plan, *, state="connection_pending", streak=0):
    progress=country._new_progress(plan)
    progress["consecutive_failures"]=streak
    item=progress["rows"][0]
    item.update(state=state,attempts=1,phase=state,error_code="provider_unavailable" if state=="connection_pending" else None,
                connection="direct")
    if state=="connection_pending":
        item.update(provider_failure=RequestFailure("request_transport_failed",stage="preflight",curl_code=7).diagnostics,
                    provider_retry_count=2)
    country._atomic_json(path,progress)


def assert_clean_held(path, plan, row):
    progress=country.load_progress(path,plan)
    item=next(item for item in progress["rows"] if item["source_row"]==row)
    assert item["state"]==item["phase"]=="session_review_pending"
    assert item["attempts"]==0 and item["error_code"] is None and item["connection"] is None
    assert item["login_failure"] is None
    raw=next(item for item in json.loads(path.read_text()) ["rows"] if item["source_row"]==row)
    assert "provider_failure" not in raw and "provider_retry_count" not in raw
    return progress


@pytest.mark.parametrize("workers,no_browser",[(1,False),(1,True),(2,True)])
def test_resumed_provider_pending_becomes_clean_hold_without_failure_streak(imported,tmp_path,workers,no_browser):
    store,source=imported
    plan=country.build_selected_plan(store,source,[4],country="LB")
    path=tmp_path/"progress.json"
    seed(path,plan,streak=2)
    hold(store,4)
    result=country.run_plan(store,plan,path,workers=workers,no_browser=no_browser,
                            prepare=lambda *_a,**_k:pytest.fail("Held account entered preparation"))
    assert result["counts"]["session_review_pending"]==1
    assert result["account_failed_count"]==0 and result["consecutive_failures"]==2
    assert_clean_held(path,plan,4)


@pytest.mark.parametrize("workers,no_browser",[(1,False),(1,True),(2,True)])
def test_interrupted_intent_that_is_now_held_does_not_abort_unrelated_resume(imported,tmp_path,workers,no_browser):
    store,source=imported
    plan=country.build_selected_plan(store,source,[4],country="LB")
    path=tmp_path/"progress.json"
    seed(path,plan,state="in_progress")
    hold(store,4)
    result=country.run_plan(store,plan,path,workers=workers,no_browser=no_browser,
                            prepare=lambda *_a,**_k:pytest.fail("Held interrupted account was replayed"))
    assert result["status"]=="completed" and result["counts"]["unknown"]==0
    assert result["counts"]["session_review_pending"]==1 and result["account_failed_count"]==0
    assert_clean_held(path,plan,4)


@pytest.mark.parametrize("workers,no_browser",[(1,False),(1,True),(2,True)])
def test_hold_during_dispatch_checkpoint_skips_before_account_access(imported,tmp_path,monkeypatch,workers,no_browser):
    store,source=imported
    plan=country.build_selected_plan(store,source,[4],country="LB")
    path=tmp_path/"progress.json"
    original=country._atomic_json
    held=False
    def checkpoint(target,value):
        nonlocal held
        original(target,value)
        if target==path and not held and value["rows"][0]["state"]=="in_progress":
            held=True
            hold(store,4)
    monkeypatch.setattr(country,"_atomic_json",checkpoint)
    if workers>1:
        # The synthetic serial vault is in-memory, so a skipped worker is
        # modeled directly; no account request or separate vault is opened.
        def worker(*_a,**_k):
            assert held
            return {"kind":"skipped","state":"session_review_pending"}
        monkeypatch.setattr(country,"_http_worker",worker)
    calls=[]
    def prepare(selected,**options):
        selected.record(options["start_row"])
        calls.append(options["start_row"])
        return completed(selected,options["start_row"])
    result=country.run_plan(store,plan,path,workers=workers,no_browser=no_browser,prepare=prepare)
    assert held and calls==[]
    assert result["account_failed_count"]==0 and result["consecutive_failures"]==0
    assert_clean_held(path,plan,4)


@pytest.mark.parametrize("no_browser",[False,True])
def test_serial_hold_after_prepare_success_is_not_reported_ready(imported,tmp_path,no_browser):
    store,source=imported
    plan=country.build_selected_plan(store,source,[4],country="LB")
    path=tmp_path/"progress.json"
    def prepare(selected,**options):
        result=completed(selected,options["start_row"])
        hold(store,4)
        return result
    result=country.run_plan(store,plan,path,no_browser=no_browser,prepare=prepare)
    assert result["counts"]["ready"]==0 and result["counts"]["session_review_pending"]==1
    assert result["account_failed_count"]==0 and result["consecutive_failures"]==0
    assert_clean_held(path,plan,4)


def test_serial_hold_during_paid_preflight_clears_old_provider_pending_diagnostics(imported,tmp_path,monkeypatch):
    store,source=imported
    plan=country.build_selected_plan(store,source,[4],country="LB")
    path=tmp_path/"progress.json"
    seed(path,plan)
    class Proxy:
        country="EG"
        def verify_country(self):
            hold(store,4)
            return {"country":"EG","country_verified":True,"proxy_used":True}
    monkeypatch.setattr(country,"load_packetstream_proxy",lambda *_:Proxy())
    result=country.run_plan(store,plan,path,proxy_egypt=True,
                            prepare=lambda *_a,**_k:pytest.fail("Held account reached login"))
    assert result["counts"]["session_review_pending"]==1 and result["account_failed_count"]==0
    assert_clean_held(path,plan,4)


@pytest.mark.parametrize("moment",["during_prepare","after_prepare"])
def test_parallel_hold_does_not_stop_or_fail_unrelated_accounts(parallel_imported,tmp_path,moment):
    parent,plan,_source,_factory=parallel_imported
    path=tmp_path/"progress.json"
    def prepare(selected,**options):
        row=options["start_row"]
        if row==1 and moment=="during_prepare":
            hold(selected._vault,row)
            selected.session(row)
            pytest.fail("Held account continued session use")
        result=completed(selected,row,parallel=True)
        if row==1: hold(selected._vault,row)
        return result
    result=country.run_plan(parent,plan,path,workers=2,no_browser=True,prepare=prepare)
    assert result["status"]=="completed" and result["counts"]["ready"]==7
    assert result["counts"]["session_review_pending"]==1
    assert result["account_failed_count"]==0 and result["consecutive_failures"]==0
    assert_clean_held(path,plan,1)


def test_coordinator_rechecks_hold_after_worker_already_returned_ready(parallel_imported,tmp_path,monkeypatch):
    parent,plan,_source,factory=parallel_imported
    path=tmp_path/"progress.json"
    original=country._http_worker
    def worker(*args,**kwargs):
        result=original(*args,**kwargs)
        if args[2]==1 and result["kind"]=="ready":
            with factory(parent.path) as store: hold(store,1)
        return result
    monkeypatch.setattr(country,"_http_worker",worker)
    result=country.run_plan(parent,plan,path,workers=2,no_browser=True,
        prepare=lambda selected,**options:completed(selected,options["start_row"],parallel=True))
    assert result["status"]=="completed" and result["counts"]["ready"]==7
    assert result["counts"]["session_review_pending"]==1 and result["account_failed_count"]==0
    assert_clean_held(path,plan,1)


def test_typed_hold_never_overrides_a_genuinely_unknown_renewal():
    error=SessionReviewRequiredError()
    assert country._failure_code(error)=="session_review_pending"
    error.renewal_unknown=True
    assert country._failure_code(error)=="interrupted_unknown"
