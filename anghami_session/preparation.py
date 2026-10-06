"""Prepare an explicitly selected group of imported test accounts."""

from datetime import datetime, timezone
from copy import deepcopy
import time
from .errors import (RequestFailure, SessionError, SessionReviewRequiredError, SessionStorageError,
                     safe_login_failure, safe_request_failure, safe_session_storage_failure)
from .provider_recovery import provider_failure, wait_for_provider
from .play_record import _Failure, _journal
from .session_recovery import read_only_validation_failure, retry_readonly_validation


_SCOPE_GUARD_MESSAGES = frozenset({
    "The frozen account identity changed.", "The country preparation row cannot change.",
    "The frozen row has a different country tag.", "Country preparation selects exactly its frozen row.",
    "The selected preparation connection cannot change.",
    "The captured session does not identify this account. Sign in to the selected account.",
})


def _local_report_diagnostics(value):
    if value is None:
        return {"error_kind": "report_error"}
    if not isinstance(value, dict) or set(value) - {"error_kind", "errno", "winerror"}:
        raise ValueError("Use fixed local report diagnostics.")
    kind = value.get("error_kind")
    if type(kind) is not str or kind not in {"filesystem_error", "report_error"}:
        raise ValueError("Use a fixed local report error kind.")
    result = {"error_kind": kind}
    for name in ("errno", "winerror"):
        if name in value:
            number = value[name]
            if kind != "filesystem_error" or type(number) is not int or not 0 <= number <= 65535:
                raise ValueError("Use bounded numeric local report evidence.")
            result[name] = number
    return result


class PreparationFinalizationError(SessionError):
    """A local report failed after this call acknowledged a completed account."""

    def __init__(self, row, *, failed_phase, diagnostics=None):
        if type(row) is not int or row < 1 or type(failed_phase) is not str or failed_phase not in {"validation", "complete"}:
            raise ValueError("Use a completed preparation row and fixed reporting phase.")
        super().__init__("The prepared account was committed, but its local report could not be saved.")
        self.row, self.failed_phase = row, failed_phase
        self.local_diagnostics = _local_report_diagnostics(diagnostics)


def safe_finalization_failure(error):
    if not isinstance(error, PreparationFinalizationError):
        return {}
    try:
        if type(error.row) is not int or error.row < 1 or type(error.failed_phase) is not str or error.failed_phase not in {"validation", "complete"}:
            return {}
        return {"code": "journal_failed", "failed_phase": error.failed_phase,
                **_local_report_diagnostics(error.local_diagnostics)}
    except (AttributeError, TypeError, ValueError):
        return {}


class PreparationSessionStoreError(SessionError):
    """A local pending-session failure must never reject an account."""

    def __init__(self, *, operation):
        if type(operation) is not str or operation not in {"load", "save"}:
            raise ValueError("Use a fixed pending-session operation.")
        message = ("The pending session could not be unlocked or validated." if operation == "load"
                   else "The pending session could not be saved securely.")
        super().__init__(message)
        self.operation = operation
        self.renewal_completed = operation == "save"
        self.retry_safe = False


class PreparationCandidateReviewError(SessionError):
    """The issued candidate is durable, but its validation needs local review."""

    def __init__(self):
        super().__init__("The issued session was retained securely and needs validation review.")
        self.candidate_retained = self.renewal_completed = self.validation_pending = True
        self.retry_safe = False


def safe_session_store_failure(error):
    """Expose fixed local evidence without candidate data or exception text."""
    if isinstance(error, PreparationSessionStoreError):
        operation = getattr(error, "operation", None)
        value = {"code": f"pending_session_{operation}_failed", "operation": operation,
                 "renewal_completed": getattr(error, "renewal_completed", None)}
    elif isinstance(error, dict):
        value = error
    else:
        return {}
    operation = value.get("operation")
    if (set(value) != {"code", "operation", "renewal_completed"}
            or type(operation) is not str or operation not in {"load", "save"}
            or value.get("code") != f"pending_session_{operation}_failed"
            or type(value.get("renewal_completed")) is not bool
            or value["renewal_completed"] != (operation == "save")):
        return {}
    return dict(value)


def prepare_test_accounts(vault, *, count, start_row=1, proxy=None, browser_backend="chrome", headless=False, dry_run=False, progress=None, reduce_browser_data=False, no_browser=False, report_path=None, proxy_factory=None, selected_rows=None, account_country=None, should_stop=None):
    if type(count) is not int or count < 1:
        raise SessionError("The preparation account count must be a positive integer.")
    if type(start_row) is not int or start_row < 1:
        raise SessionError("The starting source row must be a positive integer.")
    if account_country is not None and (type(account_country) is not str or account_country not in {"EG", "LB"}):
        raise SessionError("Select the registered account country: EG or LB.")
    if account_country is not None and (selected_rows is not None or start_row != 1):
        raise SessionError("Country selection cannot be combined with reviewed accounts or a starting source row.")
    if browser_backend not in {"chrome", "cloakbrowser"}:
        raise SessionError("Select a supported browser: chrome or cloakbrowser.")
    if type(reduce_browser_data) is not bool:
        raise SessionError("The browser data selection must be true or false.")
    if type(no_browser) is not bool:
        raise SessionError("The browser-free selection must be true or false.")
    if should_stop is not None and not callable(should_stop):
        raise SessionError("The preparation stop check must be callable.")
    if proxy_factory is not None and (not callable(proxy_factory) or proxy is not None or not no_browser):
        raise SessionError("Per-account sticky routes require browser-free preparation and one route provider.")
    if no_browser:
        headless, reduce_browser_data = False, False
    if selected_rows is not None:
        if (count > 5 or not isinstance(selected_rows, (list, tuple)) or len(selected_rows) != count
                or any(type(row) is not int or row < 1 for row in selected_rows)
                or len(set(selected_rows)) != count):
            raise SessionError("Choose 1-5 distinct accounts from failed-account review.")
        reviewed = vault.failure_review()
        if not isinstance(reviewed, dict) or not set(selected_rows) <= set(reviewed.get("failed_rows", [])):
            raise SessionError("Choose accounts currently listed in failed-account review.")
        rows = list(selected_rows)
        identities = set()
        for row in rows:
            record = vault.record(row)
            email = record.get("email") if isinstance(record, dict) else None
            del record
            if not isinstance(email, str) or not email.strip() or email.strip().casefold() in identities:
                raise SessionError("Choose one reviewed source row per account identity.")
            identities.add(email.strip().casefold())
    elif account_country is not None:
        rows = vault.select_test_candidates(count, country=account_country, randomize=True)
    else:
        rows = vault.select_test_candidates(count, start_row=start_row)
    if len(rows) != count:
        if account_country is not None:
            raise SessionError(f"Only {len(rows)} additional unique {account_country} accounts are available. Choose a smaller count.")
        raise SessionError(f"Only {len(rows)} additional unique accounts are available from that starting row. Choose a smaller count.")
    require_not_held = getattr(vault, "_require_session_not_held", None)
    if callable(require_not_held):
        for row in rows:
            require_not_held(row)
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
        "connection_pending_rows": [], "account_failed_rows": [], "provider_attempts": 0,
    }
    if account_country is not None:
        report.update({"account_country": account_country, "selection_mode": "random_country"})
    if proxy is not None:
        report["proxy"] = proxy.summary()
    elif proxy_factory is not None:
        report["connection"] = "proxy_egypt"
    else:
        report["connection"] = "direct"
    if dry_run:
        return report

    report_path = vault.path.parent / "accounts-prepare-tests-report.json" if report_path is None else report_path
    def stop_requested():
        if should_stop is None:
            return False
        value = should_stop()
        if type(value) is not bool:
            raise SessionError("The preparation stop check must return true or false.")
        return value

    class StopGate:
        """Let an existing bounded provider cooldown observe the same stop."""
        def is_set(self):
            return stop_requested()

        def wait(self, seconds):
            # Provider cooldowns call wait in chunks of at most 250 ms.
            time.sleep(seconds)
            return stop_requested()

    stop_gate = StopGate() if should_stop is not None else None
    def checkpoint():
        try:
            _journal(report, report_path)
        except _Failure as exc:
            row = report.get("active_row")
            if exc.code == "journal_failed" and row in report["prepared_rows"] and report["phase"] in {"validation", "complete"}:
                raise PreparationFinalizationError(row, failed_phase=report["phase"],
                                                   diagnostics=getattr(exc, "local_diagnostics", None)) from None
            raise
        if progress is not None:
            progress(deepcopy(report))

    def stopped_report():
        report.update(passed=False, phase="stopped", pause_reason="stop_requested",
                      stop_requested=True, active_row=None)
        checkpoint()
        return report

    def prepare_row(row, row_proxy):
        renewed = False
        candidate_retained = False
        attach_acknowledged = False
        saved = None
        try:
            if callable(require_not_held):
                require_not_held(row)
            if selected_rows is not None:
                saved = None
            else:
                pending = getattr(vault, "pending_session", None)
                try:
                    saved = pending(row) if callable(pending) else None
                except SessionError as exc:
                    # The frozen wrapper also checks scope before delegating.
                    # Preserve its guard failures; the vault's fixed local
                    # decode error cannot be treated as an account rejection.
                    if str(exc) != "The pending session could not be unlocked or validated.":
                        raise
                    raise PreparationSessionStoreError(operation="load") from None
                except Exception:
                    raise PreparationSessionStoreError(operation="load") from None
                renewed = saved is not None
                candidate_retained = saved is not None
                if saved is None:
                    try:
                        saved = vault.session(row)
                    except SessionReviewRequiredError:
                        raise
                    except SessionError:
                        if callable(require_not_held):
                            require_not_held(row)
                        saved = None
            metadata = None
            if saved is None:
                record = vault.record(row)
                try:
                    if no_browser:
                        from .session_recovery import recover_legacy_session
                        report["phase"] = "session_recovery"
                        checkpoint()
                        if callable(require_not_held):
                            require_not_held(row)
                        saved, metadata = recover_legacy_session(record, proxy=row_proxy)
                        renewed = isinstance(metadata, dict) and metadata.get("session_renewed") is True
                    else:
                        from .capture import capture_login
                        report["phase"] = "login"
                        checkpoint()
                        options = {
                            "email": record["email"], "password": record["password"],
                            "browser_backend": browser_backend, "headless": headless,
                        }
                        if row_proxy is not None:
                            options["proxy"] = row_proxy
                        if reduce_browser_data:
                            options["reduce_browser_data"] = True
                        try:
                            if callable(require_not_held):
                                require_not_held(row)
                            saved, metadata = capture_login(**options)
                        finally:
                            options.clear()
                finally:
                    del record
                if renewed:
                    # Commit the issued candidate before any further GET or
                    # local attach can fail. Its next attempt must only read it.
                    try:
                        vault.save_pending_session(row, saved)
                    except (SessionReviewRequiredError, PreparationSessionStoreError):
                        raise
                    except SessionError as exc:
                        if str(exc) in _SCOPE_GUARD_MESSAGES:
                            raise
                        failure = PreparationSessionStoreError(operation="save")
                        failure.storage_failure = safe_session_storage_failure(exc)
                        raise failure from None
                    except Exception as exc:
                        failure = PreparationSessionStoreError(operation="save")
                        failure.storage_failure = safe_session_storage_failure(exc)
                        raise failure from None
                    candidate_retained = True
            report["phase"] = "validation"
            checkpoint()
            if callable(require_not_held):
                require_not_held(row)
            options = {} if row_proxy is None else {"proxy": row_proxy}
            if renewed:
                _, attempts = retry_readonly_validation(lambda: vault.attach(row, saved, **options))
                report.update(attach_validation_attempts=attempts, attach_validation_retries=attempts - 1)
            else:
                vault.attach(row, saved, **options)
            attach_acknowledged = True
            # attach atomically transfers the pending candidate to the verified
            # saved session. Do not later claim a pending blob still exists.
            candidate_retained = False
            if metadata is not None:
                if isinstance(metadata, dict):
                    for key in ("session_validation_attempts", "session_validation_retries"):
                        if type(metadata.get(key)) is int:
                            report[key] = metadata[key]
                metadata_kind = "session-recovery" if no_browser else "login-request"
                metadata_path = vault.path.parent / f"account-{row}.{metadata_kind}.redacted.json"
                _journal(metadata, metadata_path)
            vault.enable_test_account(row)
        except BaseException as exc:
            if isinstance(exc, SessionReviewRequiredError):
                raise
            if isinstance(exc, PreparationSessionStoreError):
                raise
            if attach_acknowledged and isinstance(exc, SessionStorageError):
                exc.renewal_completed = renewed
                exc.verified_session_retained = True
                raise
            if renewed and isinstance(exc, SessionError):
                if read_only_validation_failure(exc, allow_cooldown_refusal=True):
                    exc.validation_candidate = deepcopy(saved)
                    exc.renewal_completed = True
                    exc.validation_pending = True
                    exc.candidate_retained = candidate_retained
                    exc.retry_safe = False
                    raise
                exc.retry_safe = False
                if safe_request_failure(exc).get("failure_category") != "account":
                    if candidate_retained and report["phase"] == "validation":
                        if isinstance(exc, SessionStorageError):
                            exc.candidate_retained = exc.renewal_completed = exc.validation_pending = True
                            raise
                        if isinstance(exc, RequestFailure):
                            exc.candidate_retained = exc.renewal_completed = exc.validation_pending = True
                            raise
                        # Scope and journal guards must keep their original
                        # identity. Only an otherwise untyped validation failure
                        # may become this account's retained-candidate review.
                        if not isinstance(exc, _Failure) and str(exc) not in _SCOPE_GUARD_MESSAGES:
                            raise PreparationCandidateReviewError() from None
                    exc.renewal_unknown = True
            raise

    checkpoint()
    try:
        for number, row in enumerate(rows, 1):
            if stop_requested():
                return stopped_report()
            report.update({"active_row": row, "attempted_accounts": number, "phase": "session_lookup"})
            report.pop("provider_failure", None)
            checkpoint()
            row_proxy = proxy if proxy_factory is None else proxy_factory(row)
            used_routes = [row_proxy]
            route_attempt = 0
            while True:
                if route_attempt and stop_requested():
                    # Only prewrite provider retries reach this boundary. Keep
                    # their connection outcome; no recovery or mutation replay.
                    report["connection_pending_rows"].append(row)
                    report["phase"] = "connection_pending"
                    checkpoint()
                    break
                route_attempt += 1
                if proxy_factory is not None:
                    if row_proxy is None:
                        raise SessionError("No verified sticky route was selected for this account.")
                    report["proxy"] = row_proxy.summary()
                try:
                    prepare_row(row, row_proxy)
                except Exception as exc:
                    if (getattr(exc, "renewal_completed", False) is True
                            and getattr(exc, "validation_pending", False) is True
                            and getattr(exc, "renewal_unknown", False) is not True):
                        # Keep a confirmed issued session encrypted and outside
                        # the ready cohort. A later preparation validates this
                        # exact candidate instead of renewing the old SID again.
                        if getattr(exc, "candidate_retained", False) is not True:
                            candidate = getattr(exc, "validation_candidate", None)
                            try:
                                vault.save_pending_session(row, candidate)
                            except Exception as save_error:
                                failure = PreparationSessionStoreError(operation="save")
                                failure.storage_failure = safe_session_storage_failure(save_error)
                                raise failure from None
                            exc.candidate_retained = True
                        if isinstance(exc, (PreparationCandidateReviewError, SessionStorageError)):
                            # The coordinator can isolate this account only
                            # after a bound durable hold is acknowledged.
                            raise
                        report["provider_attempts"] += 1
                        report["provider_failure"] = provider_failure(exc)
                        report["connection_pending_rows"].append(row)
                        for field, prefix in (("session_recovery", "session"), ("validation", "attach")):
                            if report["phase"] == field:
                                attempts = getattr(exc, "validation_read_attempts", 1)
                                if type(attempts) is int and 1 <= attempts <= 3:
                                    report[f"{prefix}_validation_attempts"] = attempts
                                    report[f"{prefix}_validation_retries"] = attempts - 1
                        report.update(validation_pending=True, renewal_completed=True, phase="connection_pending")
                        report["candidate_retained"] = True
                        checkpoint()
                        break
                    if getattr(exc, "renewal_unknown", False) is True:
                        raise
                    diagnostic = provider_failure(exc)
                    login_failure = safe_login_failure(exc)
                    # A failed JSON authentication status on a throttled or
                    # unavailable HTTP response is not an account rejection.
                    browser_status = next((login_failure.get(name) for name in ("auth_http_status", "page_http_status")
                                           if login_failure.get(name) == 429 or type(login_failure.get(name)) is int
                                           and 500 <= login_failure[name] <= 599), None)
                    if browser_status is not None:
                        diagnostic = provider_failure(RequestFailure(
                            "request_rate_limited" if browser_status == 429 else "request_http_failed",
                            stage="preflight", http_status=browser_status, retry_safe=False,
                        ))
                    if diagnostic:
                        report["provider_attempts"] += 1
                        report["provider_failure"] = diagnostic
                        retry_safe = diagnostic.get("retryable") is True and no_browser
                        if retry_safe and route_attempt < 3 and not stop_requested():
                            if diagnostic.get("http_status") == 429:
                                # Respect the provider's quota on this exact route;
                                # switching exit IPs is not a rate-limit remedy.
                                report["phase"] = "provider_wait"
                                checkpoint()
                                waited = (wait_for_provider(diagnostic) if stop_gate is None
                                          else wait_for_provider(diagnostic, stop_gate))
                                if waited:
                                    continue
                            elif diagnostic.get("rotate_route") is True and proxy_factory is not None:
                                candidate_proxy = proxy_factory(row)
                                if candidate_proxy is not None and all(candidate_proxy != used for used in used_routes):
                                    row_proxy = candidate_proxy
                                    used_routes.append(row_proxy)
                                    report["phase"] = "provider_retry"
                                    checkpoint()
                                    continue
                        report["connection_pending_rows"].append(row)
                        report["phase"] = "connection_pending"
                        checkpoint()
                        break
                    failure = safe_request_failure(exc)
                    if login_failure.get("code") == "login_rejected":
                        failure = safe_request_failure(RequestFailure(
                            "session_authentication_rejected", stage="preflight",
                            http_status=login_failure.get("auth_http_status"),
                        ))
                        report["login_failure"] = login_failure
                    if failure.get("failure_category") == "account":
                        vault.record_account_failure(row, failure)
                        if failure.get("code") == "session_identity_mismatch":
                            raise
                        report["account_failed_rows"].append(row)
                        report["account_failure"] = failure
                        report["phase"] = "account_failed"
                        checkpoint()
                        break
                    raise
                else:
                    report.pop("provider_failure", None)
                    report["prepared_rows"].append(row)
                    report["prepared_account_count"] = len(report["prepared_rows"])
                    checkpoint()
                    break
        if stop_requested():
            return stopped_report()
        report.update({"passed": len(report["prepared_rows"]) == count, "phase": "complete"})
        checkpoint()
        return report
    except BaseException as exc:
        login_failure = safe_login_failure(exc)
        finalization_failure = safe_finalization_failure(exc)
        session_store_failure = safe_session_store_failure(exc)
        storage_failure = safe_session_storage_failure(exc) or safe_session_storage_failure(getattr(exc, "storage_failure", None))
        report.update({
            "passed": False, "failed_phase": report["phase"], "phase": "stopped",
            "failed_row": report.get("active_row"),
            "error_code": "session_review_required" if isinstance(exc, SessionReviewRequiredError) else "cancelled" if isinstance(exc, (KeyboardInterrupt, EOFError)) else login_failure.get("code", "preparation_failed"),
        })
        if login_failure:
            report["login_failure"] = login_failure
        request_failure = safe_request_failure(exc)
        if request_failure:
            report["session_failure"] = request_failure
        if getattr(exc, "renewal_unknown", False) is True:
            report["renewal_unknown"] = True
        if getattr(exc, "candidate_retained", False) is True:
            report.update(candidate_retained=True, renewal_completed=True, validation_pending=True)
            if isinstance(exc, PreparationCandidateReviewError):
                report["preparation_review"] = {"code": "preparation_validation_unknown", "stage": "validation", "candidate_retained": True}
        if storage_failure:
            report["session_storage_failure"] = storage_failure
        if getattr(exc, "verified_session_retained", False) is True:
            report.update(verified_session_retained=True, renewal_completed=getattr(exc, "renewal_completed", False) is True)
        if finalization_failure:
            report["local_finalization_failure"] = finalization_failure
        if session_store_failure:
            report["local_session_failure"] = session_store_failure
            if session_store_failure["renewal_completed"]:
                report["renewal_completed"] = True
        try:
            checkpoint()
        except Exception:
            if finalization_failure:
                # Preserve the original postcommit evidence if the failure report
                # is also temporarily unwritable. No account work is repeated.
                raise exc from None
            raise
        raise
