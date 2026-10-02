"""Prepare a small, explicitly selected group of imported test accounts."""

from datetime import datetime, timezone
from copy import deepcopy
from .errors import SessionError, safe_login_failure
from .play_record import _journal


def prepare_test_accounts(vault, *, count, start_row=1, proxy=None, browser_backend="chrome", headless=False, dry_run=False, progress=None, reduce_browser_data=False, no_browser=False):
    if type(count) is not int or not 1 <= count <= 5:
        raise SessionError("Choose 1-5 additional accounts for this preparation run.")
    if type(start_row) is not int or start_row < 1:
        raise SessionError("The starting source row must be a positive integer.")
    if browser_backend not in {"chrome", "cloakbrowser"}:
        raise SessionError("Select a supported browser: chrome or cloakbrowser.")
    if type(reduce_browser_data) is not bool:
        raise SessionError("The browser data selection must be true or false.")
    if type(no_browser) is not bool:
        raise SessionError("The browser-free selection must be true or false.")
    if no_browser:
        headless, reduce_browser_data = False, False
    rows = vault.select_test_candidates(count, start_row=start_row)
    if len(rows) != count:
        raise SessionError(f"Only {len(rows)} additional unique accounts are available from that starting row. Choose a smaller count.")
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_accounts": count, "selected_rows": rows,
        "prepared_rows": [], "prepared_account_count": 0, "attempted_accounts": 0,
        "passed": False, "dry_run": bool(dry_run), "phase": "preview" if dry_run else "preparing",
        "browser": "none" if no_browser else browser_backend, "headless": bool(headless), "automatic_retry": False,
        "no_browser": no_browser, "browser_required": not no_browser,
        "preparation_method": "http" if no_browser else "browser",
        "reduce_browser_data": reduce_browser_data,
        "like_events_sent": 0, "play_events_sent": 0,
    }
    if proxy is not None:
        report["proxy"] = proxy.summary()
    else:
        report["connection"] = "direct"
    if dry_run:
        return report

    report_path = vault.path.parent / "accounts-prepare-tests-report.json"
    def checkpoint():
        _journal(report, report_path)
        if progress is not None:
            progress(deepcopy(report))

    checkpoint()
    try:
        for number, row in enumerate(rows, 1):
            report.update({"active_row": row, "attempted_accounts": number, "phase": "session_lookup"})
            checkpoint()
            try:
                saved = vault.session(row)
            except SessionError:
                saved = None
            metadata = None
            if saved is None:
                record = vault.record(row)
                try:
                    if no_browser:
                        from .session_recovery import recover_legacy_session
                        report["phase"] = "session_recovery"
                        checkpoint()
                        saved, metadata = recover_legacy_session(record, proxy=proxy)
                    else:
                        from .capture import capture_login
                        report["phase"] = "login"
                        checkpoint()
                        options = {
                            "email": record["email"], "password": record["password"],
                            "browser_backend": browser_backend, "headless": headless,
                        }
                        if proxy is not None:
                            options["proxy"] = proxy
                        if reduce_browser_data:
                            options["reduce_browser_data"] = True
                        try:
                            saved, metadata = capture_login(**options)
                        finally:
                            options.clear()
                finally:
                    del record
            report["phase"] = "validation"
            checkpoint()
            options = {} if proxy is None else {"proxy": proxy}
            vault.attach(row, saved, **options)
            if metadata is not None:
                metadata_kind = "session-recovery" if no_browser else "login-request"
                metadata_path = vault.path.parent / f"account-{row}.{metadata_kind}.redacted.json"
                _journal(metadata, metadata_path)
            vault.enable_test_account(row)
            report["prepared_rows"].append(row)
            report["prepared_account_count"] = len(report["prepared_rows"])
            checkpoint()
        report.update({"passed": True, "phase": "complete"})
        checkpoint()
        return report
    except BaseException as exc:
        login_failure = safe_login_failure(exc)
        report.update({
            "passed": False, "failed_phase": report["phase"], "phase": "stopped",
            "failed_row": report.get("active_row"),
            "error_code": "cancelled" if isinstance(exc, (KeyboardInterrupt, EOFError)) else login_failure.get("code", "preparation_failed"),
        })
        if login_failure:
            report["login_failure"] = login_failure
        checkpoint()
        raise
