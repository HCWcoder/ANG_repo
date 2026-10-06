"""Held renewals cannot drift into preparation or be counted as failed accounts."""

import pytest
import sys

from anghami_session import country_preparation as country, preparation
from anghami_session.errors import SessionError, SessionReviewRequiredError
from test_account_preparation import PreparationVault, fake_capture, fake_recovery
from test_country_preparation import imported, successful_prepare


def hold(vault, row, *, table_only=False):
    with vault._db:
        vault._db.execute("CREATE TABLE IF NOT EXISTS account_session_review(source_row INTEGER PRIMARY KEY, email_key BLOB)")
        vault._db.execute("INSERT INTO account_session_review SELECT source_row,email_key FROM accounts WHERE source_row=?", (row,))
        if not table_only:
            vault._db.execute("UPDATE accounts SET state='session_review_pending' WHERE source_row=?", (row,))


@pytest.mark.parametrize("table_only", [True, False])
def test_country_plan_holds_entire_identity_even_if_alias_has_ready_session(imported, table_only):
    vault, source = imported
    with vault._db:
        vault._db.execute("UPDATE accounts SET state='ready',session=? WHERE source_row=5", (b"synthetic-saved",))
    hold(vault, 4, table_only=table_only)
    plan = country.build_plan(vault, source, country="LB")
    states = {item["source_row"]: item["state"] for item in plan["rows"]}
    assert states[4] == "session_review_pending" and 5 not in states
    selected = country.build_selected_plan(vault, source, [4], country="LB")
    assert selected["rows"] == [{"source_row": 4, "state": "session_review_pending"}]


@pytest.mark.parametrize("held_after_plan", [True, False])
def test_country_runner_skips_held_identity_and_roundtrips_progress(imported, tmp_path, held_after_plan):
    vault, source = imported
    if not held_after_plan:
        hold(vault, 4)
    plan = country.build_plan(vault, source, country="LB")
    if held_after_plan:
        hold(vault, 4)
    path = tmp_path / "progress.json"
    calls = []
    result = country.run_plan(vault, plan, path, prepare=successful_prepare(calls))
    assert calls == [7, 9]
    assert result["counts"]["session_review_pending"] == result["session_review_pending_count"] == 1
    assert result["account_failed_count"] == 0
    progress = country.load_progress(path, plan)
    held = next(item for item in progress["rows"] if item["source_row"] == 4)
    assert held["state"] == held["phase"] == "session_review_pending"
    assert held["attempts"] == 0 and held["error_code"] is None


def test_frozen_wrapper_rechecks_hold_before_any_preparation_access(imported):
    vault, _source = imported
    fixed = country.SelectedRowVault(vault, 4, country="LB")
    before = list(vault.calls)
    hold(vault, 5, table_only=True)
    for action in (lambda: fixed.record(4), lambda: fixed.session(4),
                   lambda: fixed.select_test_candidates(1, start_row=4),
                   lambda: fixed._require_session_not_held(4)):
        with pytest.raises(SessionError, match="explicit saved-session review"):
            action()
    assert vault.calls == before
    with pytest.raises(SessionError, match="explicit saved-session review"):
        country.SelectedRowVault(vault, 4, country="LB")


@pytest.mark.parametrize("no_browser", [True, False])
@pytest.mark.parametrize("dry_run", [True, False])
def test_preparation_guard_rejects_held_selector_output_before_login_or_http(tmp_path, monkeypatch, no_browser, dry_run):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    fake_capture(monkeypatch, vault)
    fake_recovery(monkeypatch, vault)

    def held(row):
        raise SessionError("This account needs an explicit saved-session review before preparation or testing.")

    monkeypatch.setattr(vault, "_require_session_not_held", held, raising=False)
    with pytest.raises(SessionError, match="explicit saved-session review"):
        preparation.prepare_test_accounts(vault, count=1, no_browser=no_browser, dry_run=dry_run)
    assert vault.events == [("select", 1, 1)]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("no_browser", [True, False])
def test_new_hold_during_saved_session_lookup_is_never_a_login_fallback(tmp_path, monkeypatch, no_browser):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    fake_capture(monkeypatch, vault)
    fake_recovery(monkeypatch, vault)
    monkeypatch.setattr(vault, "_require_session_not_held", lambda row: None, raising=False)

    def held_session(row):
        vault.events.append(("held_session", row))
        raise SessionReviewRequiredError()

    monkeypatch.setattr(vault, "session", held_session)
    with pytest.raises(SessionReviewRequiredError):
        preparation.prepare_test_accounts(vault, count=1, no_browser=no_browser)
    assert [event[0] for event in vault.events] == ["select", "held_session"]


@pytest.mark.parametrize("no_browser", [True, False])
def test_hold_arriving_at_login_checkpoint_is_rechecked_before_external_call(tmp_path, monkeypatch, no_browser):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    fake_capture(monkeypatch, vault)
    fake_recovery(monkeypatch, vault)
    active_hold = [False]

    def guard(row):
        if active_hold[0]:
            raise SessionReviewRequiredError()

    def progress(report):
        if report["phase"] in {"login", "session_recovery"}:
            active_hold[0] = True

    monkeypatch.setattr(vault, "_require_session_not_held", guard, raising=False)
    with pytest.raises(SessionReviewRequiredError):
        preparation.prepare_test_accounts(vault, count=1, no_browser=no_browser, progress=progress)
    assert [event[0] for event in vault.events] == ["select", "session", "record"]
    assert not vault.reviewed


def test_hold_after_confirmed_recovery_preserves_hold_without_replay_or_failed_account(tmp_path, monkeypatch):
    vault = PreparationVault(tmp_path / "synthetic.sqlite3")
    active_hold = [False]
    fake_recovery(monkeypatch, vault)
    recovery = sys.modules["anghami_session.session_recovery"]
    original = recovery.recover_legacy_session

    def confirmed_recovery(record, *, proxy=None):
        saved, metadata = original(record, proxy=proxy)
        return saved, {**metadata, "session_renewed": True}

    monkeypatch.setattr(recovery, "recover_legacy_session", confirmed_recovery)

    def guard(row):
        if active_hold[0]:
            raise SessionReviewRequiredError()

    def progress(report):
        if report["phase"] == "validation":
            active_hold[0] = True

    monkeypatch.setattr(vault, "_require_session_not_held", guard, raising=False)
    with pytest.raises(SessionReviewRequiredError) as result:
        preparation.prepare_test_accounts(vault, count=1, no_browser=True, progress=progress)
    assert [event[0] for event in vault.events] == ["select", "session", "record", "recover"]
    assert not vault.reviewed and not getattr(result.value, "renewal_unknown", False)
