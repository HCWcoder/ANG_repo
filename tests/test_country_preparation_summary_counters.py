"""Country summary distinguishes confirmed account rejection from infrastructure."""

import json

import pytest

from anghami_session import country_preparation as country


def progress_for(rows):
    plan = {"country": "EG", "source_sha256": "a" * 64, "plan_id": "b" * 64,
            "tagged_rows": len(rows), "duplicate_rows": 0,
            "rows": [{"source_row": number, "state": "pending"} for number in range(1, len(rows) + 1)]}
    progress = country._new_progress(plan)
    for item, (state, code) in zip(progress["rows"], rows):
        item.update(state=state, error_code=code, attempts=int(state in {"failed", "unknown", "ready", "connection_pending"}))
    return progress


def test_mixed_failed_codes_keep_raw_total_and_separate_confirmed_accounts():
    progress = progress_for([
        ("failed", "account_failed"), ("failed", "infrastructure_failed"),
        ("failed", "proxy_preflight_failed"), ("failed", "browser_unavailable"),
        ("failed", "browser_cleanup_failed"), ("unknown", "interrupted_unknown"),
        ("connection_pending", "provider_unavailable"), ("ready", None),
        ("already_ready", None), ("pending", None),
    ])
    report = country.summarize(progress)
    assert report["counts"]["failed"] == 5
    assert report["account_failed_count"] == 1 and report["infrastructure_failed_count"] == 4
    assert report["counts"]["unknown"] == report["counts"]["connection_pending"] == 1
    assert report["counts"]["ready"] == 1 and report["attempted_accounts"] == 8
    assert "source_sha256" not in json.dumps(report) and "identity_bindings" not in json.dumps(report)


@pytest.mark.parametrize("rows,account_failed,infrastructure_failed", [
    ([], 0, 0),
    ([("failed", "account_failed")] * 3, 3, 0),
    ([("failed", "infrastructure_failed")] * 3, 0, 3),
    ([("unknown", "interrupted_unknown"), ("connection_pending", "provider_unavailable")], 0, 0),
    ([("pending", None), ("already_ready", None), ("already_enrolled", None)], 0, 0),
])
def test_failure_counters_require_failed_state_and_exact_account_code(rows, account_failed, infrastructure_failed):
    report = country.summarize(progress_for(rows))
    assert report["account_failed_count"] == account_failed
    assert report["infrastructure_failed_count"] == infrastructure_failed
    assert report["counts"]["failed"] == account_failed + infrastructure_failed
    assert type(report["account_failed_count"]) is type(report["infrastructure_failed_count"]) is int
