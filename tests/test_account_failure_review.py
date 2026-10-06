"""Account review persists only confirmed rejections; all account data is fake."""

import json
import os

import pytest

from anghami_session import vault
from anghami_session.errors import RequestFailure, SessionError
from test_account_vault import account_files, fake_session, saved_session

pytestmark = pytest.mark.skipif(os.name != "nt", reason="DPAPI account storage requires Windows")


def test_review_migration_is_lazy_and_provider_failure_cannot_change_accounts(account_files):
    source, path, _raw = account_files
    vault.migrate_registered(source, path)
    with vault.AccountVault(path) as store:
        before = store.summary()
        assert store.failure_review() == {"accounts": [], "total": 0, "failed_rows": []}
        with pytest.raises(SessionError):
            store.record_account_failure(1, RequestFailure("request_transport_failed", curl_code=56))
        assert store.summary() == before
        assert store._db.execute("SELECT 1 FROM sqlite_master WHERE name='account_failure_review'").fetchone() is None


def test_account_rejection_quarantines_aliases_but_keeps_saved_sessions(account_files, saved_session, fake_session):
    source, path, _raw = account_files
    vault.migrate_registered(source, path)
    saved_session["account_email"] = "first@example.com"
    with vault.AccountVault(path) as store:
        store.attach(1, saved_session)
        store.attach(4, saved_session)
        store.enable_test_account(1)
        store.enable_test_account(4)
        store.record_account_failure(1, RequestFailure("session_authentication_rejected", stage="relations"))
        assert store.failure_review()["failed_rows"] == [1, 4]
        assert store.test_accounts()["ready_rows"] == []
        assert store.session(1)["account_email"] == "first@example.com"
        assert store.session(4)["account_email"] == "first@example.com"
        public = json.dumps(store.failure_review())
        for secret in ("first@example.com", "private-password", "private-new-session"):
            assert secret not in public
    with vault.AccountVault(path) as store:
        assert store.failure_review()["total"] == 2


def test_successful_repreparation_clears_only_the_verified_review_row(account_files, saved_session, fake_session):
    source, path, _raw = account_files
    vault.migrate_registered(source, path)
    with vault.AccountVault(path) as store:
        store.attach(3, saved_session)
        store.record_account_failure(3, RequestFailure("session_authentication_rejected", stage="relations"))
        store.record_account_failure(1, RequestFailure("session_authentication_rejected", stage="relations"))
        store.attach(3, saved_session)
        assert store.failure_review()["failed_rows"] == [1, 4]
        assert 3 in store.test_accounts()["ready_rows"]


def test_failed_repreparation_keeps_review_and_previous_saved_session(account_files, saved_session, fake_session):
    source, path, _raw = account_files
    vault.migrate_registered(source, path)
    with vault.AccountVault(path) as store:
        store.attach(3, saved_session)
        store.record_account_failure(3, RequestFailure("session_authentication_rejected", stage="relations"))
        old = store._db.execute("SELECT session FROM accounts WHERE source_row=3").fetchone()[0]
        fake_session["fail"] = True
        with pytest.raises(SessionError):
            store.attach(3, saved_session)
        assert store.failure_review()["failed_rows"] == [3]
        assert store._db.execute("SELECT session FROM accounts WHERE source_row=3").fetchone()[0] == old


def test_corrupt_review_rows_are_not_exposed(account_files):
    source, path, _raw = account_files
    vault.migrate_registered(source, path)
    with vault.AccountVault(path) as store:
        store.record_account_failure(1, RequestFailure("session_authentication_rejected", stage="relations"))
        with store._db:
            store._db.execute("UPDATE account_failure_review SET failure_code='private-secret' WHERE source_row=1")
            store._db.execute("UPDATE account_failure_review SET failed_at='2026-10-03T12:00:00' WHERE source_row=4")
        assert store.failure_review() == {"accounts": [], "total": 0, "failed_rows": []}


@pytest.mark.parametrize("failure,expected", [
    (RequestFailure("request_transport_failed", curl_code=56), "ready"),
    (RequestFailure("request_rate_limited", http_status=429), "ready"),
    (RequestFailure("session_authentication_rejected", stage="relations"), "account_failed"),
])
def test_explicit_health_check_preserves_ready_for_provider_and_reviews_rejection(account_files, saved_session, fake_session, monkeypatch, failure, expected):
    source, path, _raw = account_files
    vault.migrate_registered(source, path)
    with vault.AccountVault(path) as store:
        store.attach(3, saved_session)
        class Failed:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def check(self, **kwargs): raise failure
        monkeypatch.setattr(vault, "AnghamiSession", Failed)
        with pytest.raises(RequestFailure):
            store.check(3)
        assert store._db.execute("SELECT state FROM accounts WHERE source_row=3").fetchone()[0] == expected
        assert store.failure_review()["total"] == (1 if expected == "account_failed" else 0)
