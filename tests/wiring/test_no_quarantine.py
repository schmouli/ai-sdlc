"""Flagship regression (SC-004/SC-011, feature 005 T024): all five new event types
persist in primary telemetry and NONE land in quarantine; unknown types still
quarantine (existing behavior pinned). Per-file fixture conventions."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.skillbox.telemetry import TelemetryStore, ContractViolationError, _EVENT_TYPES, default_store


@pytest.fixture()
def store(tmp_path):
    return default_store(tmp_path)


FIVE_TYPES = {
    "hook_gate_accepted": {
        "hook_event": "after_specify", "hook_command": "egm.review.loop",
        "feature_dir": "specs/005-ai-sdlc-wiring",
    },
    "hook_gate_skipped": {"hook_event": "after_tasks", "hook_command": "egm.review.loop", "reason": "user declined"},
    "speckit_stage_completed": {
        "stage": "tasks", "feature_dir": "specs/005-ai-sdlc-wiring",
        "decision": "accepted", "recorded_by": "record_hook_acceptance",
    },
    "egm_review_round": {
        "profile": "TASKS", "target_file": "specs/005-ai-sdlc-wiring/tasks.md",
        "reviewers": ["implementation_readiness", "structural", "codebase_details"],
        "findings_count": 2, "round_kind": "loop",
        "consult_mode": "digest", "consult_freshness": "fresh", "consult_fallback": False,
    },
    "wiring_health_check": {"checks_total": 12, "checks_failed": 0},
}


def test_all_five_types_in_vocabulary():
    for t in FIVE_TYPES:
        assert t in _EVENT_TYPES, f"{t} missing from _EVENT_TYPES"
    assert "EVENT_TYPES" in dir(__import__("src.skillbox.telemetry", fromlist=["EVENT_TYPES"]))


def _quarantine_files(store):
    return list(Path(store.quarantine_dir).glob("QRN-*.json"))


def test_all_five_types_persist_zero_quarantine(store, tmp_path):
    for event_type, payload in FIVE_TYPES.items():
        eid = store.record_event(event_type=event_type, payload=payload, skill="speckit-implement")
        assert eid
    assert not _quarantine_files(store)


def test_unknown_type_still_quarantines(store):
    with pytest.raises(ContractViolationError):
        store.record_event(event_type="nonexistent_type_xyz", payload={"a": 1}, skill="test")
    assert _quarantine_files(store)


def test_required_fields_enforced_for_new_types(store):
    """FR-012: required-key enforcement added for new types only."""
    for event_type, payload in FIVE_TYPES.items():
        for req_field in payload:
            bad_payload = {k: v for k, v in payload.items() if k != req_field}
            with pytest.raises(ContractViolationError):
                store.record_event(event_type=event_type, payload=bad_payload, skill="t")


def test_old_types_keep_permissive_contract(store):
    """Pre-existing types keep the permissive contract (unknown keys tolerated)."""
    eid = store.record_event(event_type="command_result", payload={"command": "x", "extraneous_key": 1}, skill="t")
    assert eid


def test_schema_version_stamped_v2(tmp_path):
    store = default_store(tmp_path)
    store.record_event(event_type="wiring_health_check", payload={"checks_total": 1, "checks_failed": 0}, skill="t")
    import json
    jsonl_files = list((tmp_path / "telemetry" / "raw").glob("*.jsonl"))
    assert jsonl_files, f"JSONL not found in {tmp_path}"
    last_line = jsonl_files[0].read_text(encoding="utf-8").strip().splitlines()[-1]
    line = json.loads(last_line)
    assert line.get("schema_version") == 2
