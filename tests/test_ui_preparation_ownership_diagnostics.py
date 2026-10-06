"""Ownership-read failures remain numeric and visible across local reports."""

from copy import deepcopy
import json

import pytest

from anghami_session import country_preparation as country
from anghami_session import ui_preparation as bridge
from anghami_session.errors import SessionError
from test_country_preparation_workers import imported
from test_ui_preparation_bridge import offline_bridge, invoke


DIAGNOSTIC = {"code": "ui_file_unreadable", "stage": "read", "attempts": 6,
              "errno": 13, "winerror": 32}


def test_checkpoint_roundtrips_fixed_ownership_diagnostics_without_exception_text(imported, tmp_path):
    _vault, plan, _source, _factory = imported
    progress = country._new_progress(plan)
    progress.update(status="paused", pause_reason="ui_unavailable", ui_read_failure=deepcopy(DIAGNOSTIC))
    path = tmp_path / "progress.json"
    country._atomic_json(path, progress)
    saved = country.load_progress(path, plan)
    assert saved["ui_read_failure"] == country.summarize(saved)["ui_read_failure"] == DIAGNOSTIC
    assert saved["consecutive_failures"] == 0


@pytest.mark.parametrize("mutation", [
    {"path": "synthetic private path"}, {"error": "synthetic private exception"},
    {"code": []}, {"stage": {}}, {"attempts": True}, {"attempts": 7},
    {"errno": "synthetic private errno"}, {"winerror": -1},
])
def test_checkpoint_rejects_untrusted_ownership_failure_fields(imported, tmp_path, mutation):
    _vault, plan, _source, _factory = imported
    progress = country._new_progress(plan)
    progress["ui_read_failure"] = {**DIAGNOSTIC, **mutation}
    path = tmp_path / "progress.json"
    country._atomic_json(path, progress)
    assert country.safe_ui_read_failure(progress["ui_read_failure"]) == {}
    with pytest.raises(SessionError, match="invalid"):
        country.load_progress(path, plan)


@pytest.mark.parametrize("private_extra", [False, True])
def test_ui_bridge_projects_only_fixed_ownership_read_diagnostics(offline_bridge, monkeypatch, private_extra):
    fixture = offline_bridge
    original = bridge.country.run_plan
    diagnostic = deepcopy(DIAGNOSTIC)
    if private_extra:
        diagnostic["exception"] = "synthetic private session and proxy text"

    def stopped(*args, **options):
        result = original(*args, **options)
        result.update(status="paused", pause_reason="ui_unavailable", ui_read_failure=diagnostic)
        return result

    monkeypatch.setattr(bridge.country, "run_plan", stopped)
    result = invoke(fixture)
    assert result["pause_reason"] == "ui_unavailable"
    if private_extra:
        assert "ui_read_failure" not in result
    else:
        assert result["ui_read_failure"] == DIAGNOSTIC
    assert "synthetic private session and proxy text" not in json.dumps(result)
