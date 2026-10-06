"""Renewal review is durable, separate from rejection, and GET-only."""

from copy import deepcopy
import hashlib
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest

from anghami_session import client, session_recovery, vault
from anghami_session.errors import RequestFailure, SessionError


STAMP = "2026-10-04T00:00:00+00:00"
JOB = "a" * 32
SECRET = "synthetic-review-session-secret"


def bundle(email="review@example.invalid", sid=SECRET):
    return {"format_version": 1, "created_at_utc": STAMP,
            "origin": "https://play.anghami.com", "account_email": email,
            "requests": {"relations": {"method": "GET",
                "url": client.GATEWAY_URL + "?type=GETuserrelations&sid=" + sid,
                "headers": {"cookie": "appsidsave=" + sid}}}}


@pytest.fixture
def review_vault(tmp_path, monkeypatch):
    protected = {}

    def crypt(value, *, decrypt=False):
        value = bytes(value)
        if decrypt:
            return protected[value]
        encrypted = b"synthetic-encrypted:" + hashlib.sha256(value).digest()
        protected[encrypted] = value
        return encrypted

    monkeypatch.setattr(vault, "_crypt", crypt)
    monkeypatch.setattr(client.requests, "Session", lambda **_: pytest.fail("Unexpected HTTP transport"))
    key = b"synthetic-index-key"
    path = tmp_path / "review.sqlite3"
    with sqlite3.connect(path) as database:
        database.executescript("""
            CREATE TABLE metadata (name TEXT PRIMARY KEY,value BLOB NOT NULL);
            CREATE TABLE accounts (source_row INTEGER PRIMARY KEY,email_key TEXT NOT NULL,
                record BLOB NOT NULL,session BLOB,state TEXT NOT NULL,checked_at_utc TEXT);
        """)
        database.executemany("INSERT INTO metadata VALUES (?,?)", {
            "format_version": "1", "index_key": crypt(key), "source_sha256": "b"*64,
        }.items())
        for row, email, sid in [(101,"review@example.invalid",SECRET),
                                (102," REVIEW@EXAMPLE.INVALID ",SECRET),
                                (103,"review@example.invalid","other-synthetic-session"),
                                (104,"other@example.invalid","unrelated-synthetic-session")]:
            record = {"source_row":row,"country":"EG","email":email,"password":"synthetic-password"}
            database.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?)", (
                row,vault._email_key(key,email),vault._pack(record),vault._pack(bundle(email.strip().casefold(),sid)),"ready",STAMP))
    with vault.AccountVault(path) as store:
        for row in (101,102,103,104): store.enable_test_account(row)
        yield store


@pytest.fixture
def get_transport(monkeypatch):
    state = {"calls":[],"failure":None,"wrong_profile":False,"hook":None}

    class Response:
        status_code=200
        headers={}
        infos={}

        def __init__(self,data): self.data=data
        def json(self): return self.data

    class Transport:
        def __init__(self,**_): pass
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def close(self): pass
        def get(self,url,**options):
            query=parse_qs(urlsplit(url).query)
            operation=query["type"][0]
            state["calls"].append(("GET",operation,"sid" in query,options.get("allow_redirects")))
            if state["failure"] is not None: raise state["failure"]
            if operation == "GETprofile":
                if state["hook"] is not None: state["hook"]()
                return Response({"status":"ok","email":"wrong@example.invalid" if state["wrong_profile"] else "review@example.invalid"})
            return Response({"status":"ok" if "sid" in query else "failed"})
        def post(self,*_a,**_k): pytest.fail("Review sent a POST")

    from anghami_session.media_gateway import PlaybackGateway
    monkeypatch.setattr(PlaybackGateway,"bootstrap",lambda *_: pytest.fail("Review renewed a session"))
    monkeypatch.setattr(client.requests,"Session",Transport)
    return state


def hold(store,row=101):
    return store.record_session_review(row,RequestFailure("request_transport_failed",stage="identity",curl_code=28,retry_safe=False),JOB)


def rows(store):
    return store._db.execute("SELECT source_row,email_key,record,session,state,checked_at_utc FROM accounts ORDER BY source_row").fetchall()


def test_absent_review_is_read_only_and_lazy(review_vault):
    before=review_vault.path.read_bytes()
    assert review_vault.session_review()=={"accounts":[],"total":0,"held_rows":[],"session_review_rows":[]}
    assert not review_vault._session_review_table_exists()
    assert review_vault.path.read_bytes()==before


def test_hold_preserves_encrypted_data_and_quarantines_aliases_durably(review_vault):
    before=rows(review_vault)
    result=hold(review_vault)
    assert result["held_rows"]==[101,102,103]
    assert [(r[:4],r[5]) for r in rows(review_vault)]==[(r[:4],r[5]) for r in before]
    assert [r[4] for r in rows(review_vault)]==["session_review_pending"]*3+["ready"]
    assert review_vault.failure_review()=={"accounts":[],"total":0,"failed_rows":[]}
    assert review_vault.test_accounts()["ready_rows"]==[104]
    assert review_vault.enrolled_test_rows().issuperset({101,102,103,104})
    report=review_vault.session_review()
    assert report["held_rows"]==[101,102,103] and report["total"]==3
    assert report["accounts"][0]["session_failure"]=={
        "code":"request_transport_failed","stage":"identity","failure_category":"provider","curl_code":28}
    for secret in (SECRET,"synthetic-password","example.invalid"):
        assert secret not in json.dumps(result)+json.dumps(report)
        assert secret.encode() not in review_vault.path.read_bytes()
    with vault.AccountVault(review_vault.path) as reopened:
        assert reopened.session_review()==report
        assert reopened.test_accounts()["ready_rows"]==[104]


@pytest.mark.parametrize("method",["session","pending_session","check","enable_test_account","attach","save_pending_session"])
def test_ordinary_paths_cannot_clear_or_use_held_account(review_vault,method):
    hold(review_vault)
    before=rows(review_vault)
    args=(101,bundle()) if method in {"attach","save_pending_session"} else (101,)
    with pytest.raises(SessionError,match="explicit saved-session review"):
        getattr(review_vault,method)(*args)
    assert rows(review_vault)==before
    assert review_vault.session_review()["held_rows"]==[101,102,103]


def test_selection_excludes_held_identity_even_after_alias_state_is_tampered(review_vault):
    hold(review_vault)
    with review_vault._db:
        review_vault._db.execute("DELETE FROM test_accounts")
        review_vault._db.execute("UPDATE accounts SET state='ready' WHERE source_row=102")
    assert review_vault.select_test_candidates(10)==[104]
    assert review_vault.select_test_candidates(10,country="EG",randomize=True)==[104]
    assert review_vault.test_accounts()["ready_rows"]==[]


def test_existing_confirmed_rejection_is_preserved(review_vault):
    with review_vault._db:
        review_vault._db.execute("CREATE TABLE account_failure_review(source_row INTEGER PRIMARY KEY,failure_code TEXT,failed_stage TEXT,failed_at TEXT)")
        review_vault._db.execute("INSERT INTO account_failure_review VALUES(102,'session_authentication_rejected','relations',?)",(STAMP,))
        review_vault._db.execute("UPDATE accounts SET state='account_failed' WHERE source_row=102")
    before=review_vault.failure_review()
    hold(review_vault)
    assert review_vault.failure_review()==before
    assert review_vault.session_review()["session_review_rows"]==[101,103]
    assert review_vault.session_review()["held_rows"]==[101,102,103]
    assert review_vault.test_accounts()["accounts"][1]["state"]=="account_failed"
    assert rows(review_vault)[1][4]=="account_failed"


@pytest.mark.parametrize("row",[0,-1,True,1.0,"101",None])
def test_strict_hold_row_validation_precedes_db(row):
    store=vault.AccountVault.__new__(vault.AccountVault)
    with pytest.raises(SessionError):
        store.record_session_review(row,RequestFailure("request_transport_failed",stage="identity"),JOB)


@pytest.mark.parametrize("job",[None,"", "a"*31,"A"*32,"z"*32,123])
def test_strict_job_id_validation_precedes_db(job):
    store=vault.AccountVault.__new__(vault.AccountVault)
    with pytest.raises(SessionError):
        store.record_session_review(101,RequestFailure("request_transport_failed",stage="identity"),job)


@pytest.mark.parametrize("failure",[
    SessionError(SECRET),{},RequestFailure("session_identity_mismatch",stage="identity"),
    RequestFailure("session_response_invalid",stage="identity"),RequestFailure("request_transport_failed",stage="relations")])
def test_only_typed_provider_renewal_failure_is_accepted(review_vault,failure):
    before=rows(review_vault)
    with pytest.raises(SessionError): review_vault.record_session_review(101,failure,JOB)
    assert rows(review_vault)==before and not review_vault._session_review_table_exists()


def test_hold_insert_failure_rolls_back_alias_states_and_lazy_schema(review_vault):
    before=rows(review_vault)
    review_vault._db.set_authorizer(lambda action,name,*_:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and name=="account_session_review" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(SessionError,match="could not be saved securely") as error: hold(review_vault)
    finally: review_vault._db.set_authorizer(None)
    assert SECRET not in str(error.value)
    assert rows(review_vault)==before and not review_vault._session_review_table_exists()


def test_review_uses_three_get_proofs_and_clears_only_identical_bound_aliases(review_vault,get_transport):
    hold(review_vault)
    before=rows(review_vault)
    result=review_vault.review_saved_session(101)
    assert get_transport["calls"]==[("GET","GETuserrelations",True,False),
                                   ("GET","GETuserrelations",False,False),("GET","GETprofile",True,False)]
    assert result["passed"] is True and result["session_review_cleared"] is True
    assert result["server_account_identity_verified"] is True and result["negative_control_passed"] is True
    assert result["cleared_rows"]==[101,102]
    assert review_vault.session_review()["session_review_rows"]==[103]
    assert review_vault.session_review()["held_rows"]==[101,102,103]
    assert review_vault.test_accounts()["ready_rows"]==[104]
    with pytest.raises(SessionError,match="explicit saved-session review"): review_vault.session(101)
    assert [r[:4] for r in rows(review_vault)]==[r[:4] for r in before]
    assert review_vault.review_saved_session(103)["cleared_rows"]==[103]
    assert review_vault.session_review()["held_rows"]==[]
    assert review_vault.test_accounts()["ready_rows"]==[101,102,103,104]


@pytest.mark.parametrize("failure",[RequestFailure("request_transport_failed",stage="relations",curl_code=7),
                                      RequestFailure("request_rate_limited",stage="relations",http_status=429)])
def test_review_provider_failure_keeps_all_holds_without_retries(review_vault,get_transport,failure):
    hold(review_vault)
    before=rows(review_vault)
    get_transport["failure"]=failure
    with pytest.raises(RequestFailure): review_vault.review_saved_session(101)
    assert len(get_transport["calls"])==1 and rows(review_vault)==before
    assert review_vault.session_review()["held_rows"]==[101,102,103]


def test_profile_identity_mismatch_keeps_hold(review_vault,get_transport):
    hold(review_vault)
    before=rows(review_vault)
    get_transport["wrong_profile"]=True
    with pytest.raises(RequestFailure) as error: review_vault.review_saved_session(101)
    assert error.value.code=="session_identity_mismatch"
    assert rows(review_vault)==before and review_vault.failure_review()["total"]==0


@pytest.mark.parametrize("change",["record","session","hold"])
def test_changed_binding_before_or_during_proof_never_clears_selected_hold(review_vault,get_transport,change):
    hold(review_vault)
    def mutate():
        with review_vault._db:
            if change=="record":
                replacement=review_vault.record(101);replacement["password"]="changed-synthetic-password"
                review_vault._db.execute("UPDATE accounts SET record=? WHERE source_row=101",(vault._pack(replacement),))
            elif change=="session":
                review_vault._db.execute("UPDATE accounts SET session=? WHERE source_row=101",(vault._pack(bundle(sid="changed-synthetic-session")),))
            else:
                review_vault._db.execute("UPDATE account_session_review SET job_id=? WHERE source_row=101",("b"*32,))
    get_transport["hook"]=mutate
    with pytest.raises(SessionError,match="hold was kept"): review_vault.review_saved_session(101)
    assert review_vault.session_review()["held_rows"]==[101,102,103]


def test_clear_failure_rolls_back_all_alias_promotions(review_vault,get_transport):
    hold(review_vault)
    before=rows(review_vault)
    def deny_delete(action,name,*_):
        return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_DELETE and name=="account_session_review" else sqlite3.SQLITE_OK
    review_vault._db.set_authorizer(deny_delete)
    try:
        with pytest.raises(SessionError,match="hold was kept"): review_vault.review_saved_session(101)
    finally: review_vault._db.set_authorizer(None)
    assert rows(review_vault)==before and review_vault.session_review()["held_rows"]==[101,102,103]


def test_explicit_capture_attach_requires_profile_and_clears_only_selected_new_session(review_vault,get_transport):
    hold(review_vault)
    replacement=bundle(sid="synthetic-captured-replacement")
    result=review_vault.attach(101,replacement,review_session=True,new_password="synthetic-updated-password")
    assert result["session_review_cleared"] is True and result["cleared_rows"]==[101]
    assert get_transport["calls"][-1]==("GET","GETprofile",True,False)
    assert client.validate_session(vault._unpack(rows(review_vault)[0][3]))==replacement
    with pytest.raises(SessionError,match="explicit saved-session review"): review_vault.session(101)
    assert review_vault.record(101)["password"]=="synthetic-updated-password"
    assert review_vault.session_review()["session_review_rows"]==[102,103]
    assert review_vault.session_review()["held_rows"]==[101,102,103]


def test_capture_identity_failure_keeps_old_session_password_and_holds(review_vault,get_transport):
    hold(review_vault)
    before=rows(review_vault)
    get_transport["wrong_profile"]=True
    with pytest.raises(RequestFailure):
        review_vault.attach(101,bundle(sid="new-synthetic-capture"),review_session=True,new_password="new-synthetic-password")
    assert rows(review_vault)==before and review_vault.session_review()["held_rows"]==[101,102,103]


def test_binding_changed_before_review_requires_verified_new_capture(review_vault,get_transport):
    hold(review_vault)
    with review_vault._db:
        record=review_vault.record(101);record["password"]="changed-synthetic-source-password"
        review_vault._db.execute("UPDATE accounts SET record=? WHERE source_row=101",(vault._pack(record),))
    with pytest.raises(SessionError,match="changed after its review hold"):
        review_vault.review_saved_session(101)
    assert get_transport["calls"]==[]
    result=review_vault.attach(101,bundle(sid="verified-new-synthetic-capture"),review_session=True)
    assert result["cleared_rows"]==[101] and review_vault.session_review()["session_review_rows"]==[102,103]
    assert review_vault.session_review()["held_rows"]==[101,102,103]


def test_alias_source_change_keeps_that_alias_held(review_vault,get_transport):
    hold(review_vault)
    with review_vault._db:
        record=review_vault.record(102);record["password"]="changed-alias-synthetic-password"
        review_vault._db.execute("UPDATE accounts SET record=? WHERE source_row=102",(vault._pack(record),))
    assert review_vault.review_saved_session(101)["cleared_rows"]==[101]
    assert review_vault.session_review()["session_review_rows"]==[102,103]
    assert review_vault.session_review()["held_rows"]==[101,102,103]


def test_invalid_ciphertext_error_is_fixed_and_keeps_hold(review_vault,get_transport,monkeypatch):
    hold(review_vault)
    before=rows(review_vault)
    monkeypatch.setattr(vault,"_unpack",lambda *_: (_ for _ in ()).throw(OSError(SECRET)))
    with pytest.raises(SessionError,match="could not be unlocked or validated") as error:
        review_vault.review_saved_session(101)
    assert SECRET not in str(error.value) and get_transport["calls"]==[] and rows(review_vault)==before


def test_state_only_hold_cannot_be_used_if_optional_table_is_missing(review_vault):
    with review_vault._db:
        review_vault._db.execute("UPDATE accounts SET state='session_review_pending' WHERE source_row=101")
    with pytest.raises(SessionError,match="explicit saved-session review"):
        review_vault.session(101)
    with pytest.raises(SessionError,match="explicit saved-session review"):
        review_vault.session(102)
    assert review_vault.session_review()["held_rows"]==[101,102,103]
    assert review_vault.test_accounts()["ready_rows"]==[104]


def test_missing_alias_hold_entry_stays_blocked_by_identity_and_cannot_be_reviewed(review_vault,get_transport):
    hold(review_vault)
    with review_vault._db:
        review_vault._db.execute("DELETE FROM account_session_review WHERE source_row=102")
        review_vault._db.execute("UPDATE accounts SET state='ready' WHERE source_row=102")
    report=review_vault.session_review()
    assert report["session_review_rows"]==[101,103] and report["held_rows"]==[101,102,103]
    assert review_vault.test_accounts()["ready_rows"]==[104]
    with pytest.raises(SessionError,match="explicit saved-session review"): review_vault.session(102)
    with pytest.raises(SessionError,match="no saved-session review hold"): review_vault.review_saved_session(102)
    with pytest.raises(SessionError,match="explicit saved-session review"):
        review_vault.attach(102,bundle(),review_session=True)
    assert get_transport["calls"]==[]


@pytest.mark.parametrize("operation",["attach","check"])
def test_peer_hold_arriving_during_ordinary_validation_cannot_promote_ready(review_vault,monkeypatch,operation):
    class Checking:
        def __init__(self,**_): pass
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def check(self,**_):
            hold(review_vault)
            return {"authenticated":True,"checked_at_utc":STAMP,"without_session":{"authentication_rejected":True}}
    monkeypatch.setattr(vault,"AnghamiSession",Checking)
    with pytest.raises(SessionError):
        if operation=="attach": review_vault.attach(101,bundle())
        else: review_vault.check(101)
    assert review_vault.session_review()["held_rows"]==[101,102,103]
    assert rows(review_vault)[0][4]=="session_review_pending"
