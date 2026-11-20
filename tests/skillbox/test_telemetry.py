"""Tests for SkillBox telemetry (US1: sessions, events, decisions — FR-015..FR-023)."""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

import pytest

from src.skillbox import ContractViolationError
from src.skillbox.telemetry import (TelemetryStore, sanitize_payload, append_jsonl,
                                    default_store, session_scope)


@pytest.fixture()
def store():
    root = Path(tempfile.mkdtemp(prefix="skillbox-test-"))
    store = default_store(root)
    yield store
    store.close()


class TestSessionLifecycle:
    def test_session_id_format(self, store):
        session_id = store.get_or_create_session(skill="speckit-plan")
        assert session_id.startswith("SBX-")
        parts = session_id.split("-")
        assert len(parts[1]) == 8 and len(parts[2]) == 4  # YYYYMMDD-NNNN

    def test_session_scope_records_start_and_completion(self, store):
        with session_scope(store, skill="speckit-implement") as handle:
            handle.record("coding_output", {"files_created": 2, "lines_added": 40})
        events = store.query_events(session_id=handle.session_id)
        types = [e["event_type"] for e in events]
        assert "skill_started" in types and "skill_completed" in types
        session = store.get_session(handle.session_id)
        assert session["status"] == "complete"

    def test_failed_session_records_failure_outcome(self, store):
        with pytest.raises(RuntimeError):
            with session_scope(store, skill="speckit-plan") as handle:
                raise RuntimeError("boom")
        completions = [e for e in store.query_events(session_id=handle.session_id)
                       if e["event_type"] == "skill_completed"]
        assert completions and completions[-1]["payload"]["outcome"] == "failure"

    def test_incomplete_session_without_completion(self, store):
        session_id = store.get_or_create_session(skill="speckit-tasks")
        store.record_event("skill_started", {"skill": "speckit-tasks"},
                           session_id=session_id)
        assert store.get_session(session_id)["status"] == "in_progress"


class TestTypedEvents:
    ALL_TYPES = {
        "planning_output": {"requirements_count": 12, "tasks_generated": 32,
                            "architecture_selected": "hexagonal"},
        "coding_output": {"files_created": 7, "files_modified": 15, "lines_added": 250,
                          "lines_deleted": 40},
        "command_result": {"command": "pytest -q", "exit_code": 0, "result": "success"},
        "validation_result": {"tests_passed": 105, "tests_failed": 2},
        "deployment_result": {"deployment": "success", "environment": "staging"},
        "failure_event": {"failure_category": "database-migration", "failure_detail": "x"},
        "composition_performed": {"skill": "speckit-implement", "overlay_versions": [],
                                  "context_detected": {}, "degraded": []},
        "overlay_version_staged": {"overlay_id": "security/x", "version": "v01",
                                   "source_learning_ids": []},
        "review_decision_recorded": {"review_decision_id": "RVD-1", "decision": "approve",
                                     "operator": "op"},
        "mlflow_push_result": {"event_count": 3, "status": "pushed", "reason": ""},
    }

    def test_all_typed_events_validate_and_persist(self, store):
        session_id = store.get_or_create_session(skill="speckit-plan")
        for etype, payload in self.ALL_TYPES.items():
            event_id = store.record_event(etype, payload, session_id=session_id)
            assert event_id.startswith("EVT-")
        events = store.query_events(session_id=session_id, limit=100)
        assert {e["event_type"] for e in events} >= set(self.ALL_TYPES)

    def test_wrong_payload_type_quarantined(self, store):
        with pytest.raises(ContractViolationError):
            store.record_event("validation_result", {"tests_passed": "many"})
        quarantined = list(store.quarantine_dir.glob("QRN-*.json"))
        assert quarantined, "invalid payload must land in quarantine"

    def test_unknown_event_type_rejected(self, store):
        with pytest.raises(ContractViolationError):
            store.record_event("not_a_type", {})

    def test_idempotent_duplicate_payload(self, store):
        session_id = store.get_or_create_session(skill="speckit-plan")
        first = store.record_event("validation_result", {"tests_passed": 1, "tests_failed": 0},
                                   session_id=session_id)
        second = store.record_event("validation_result", {"tests_passed": 1, "tests_failed": 0},
                                    session_id=session_id)
        assert first == second  # dedup guard (FR-050)

    def test_jsonl_written(self, store):
        with session_scope(store, skill="speckit-plan"):
            pass
        raw_files = list(store.raw_dir.glob("*.jsonl"))
        assert raw_files, "raw JSONL stream must exist"
        lines = [json.loads(l) for l in raw_files[0].read_text(encoding="utf-8").splitlines() if l]
        assert any(r["event_type"] == "skill_started" for r in lines)


class TestSessionIsolation:
    def test_concurrent_sessions_no_cross_contamination(self, store):
        results = {}

        def run_session(tag: str) -> None:
            with session_scope(store, skill="speckit-implement") as handle:
                handle.record("coding_output", {"files_created": 1 if tag == "a" else 9,
                                                "lines_added": 1 if tag == "a" else 99})
                results[tag] = handle.session_id

        t1 = threading.Thread(target=run_session, args=("a",))
        t2 = threading.Thread(target=run_session, args=("b",))
        t1.start(); t1.join()
        t2.start(); t2.join()
        assert results["a"] != results["b"]
        events_a = [e for e in store.query_events(session_id=results["a"])
                    if e["event_type"] == "coding_output"]
        events_b = [e for e in store.query_events(session_id=results["b"])
                    if e["event_type"] == "coding_output"]
        assert events_a and events_b
        assert events_a[0]["payload"]["files_created"] == 1
        assert events_b[0]["payload"]["files_created"] == 9


class TestDecisions:
    def test_decision_record_persisted_with_snapshot(self, store):
        session_id = store.get_or_create_session(skill="speckit-implement")
        decision_id = store.record_decision(
            "selected repository pattern", "matches existing architecture",
            session_id=session_id, skill="speckit-implement")
        assert decision_id.startswith("DEC-")
        snapshot = store.decisions_dir / f"{decision_id}.json"
        assert snapshot.exists()
        data = json.loads(snapshot.read_text(encoding="utf-8"))
        assert data["decision"] == "selected repository pattern"


class TestSanitization:
    def test_credentials_redacted(self):
        clean = sanitize_payload({"token": "abc.def", "user": "schmo"})
        assert clean["token"] == "<redacted>"
        assert clean["user"] == "schmo"

    def test_file_contents_omitted(self):
        clean = sanitize_payload({"file_content": "SECRET-CODE"})
        assert clean["file_content"] == "<file-content-omitted>"

    def test_long_text_capped(self):
        clean = sanitize_payload({"failure_detail": "x" * 1000})
        assert len(clean["failure_detail"]) <= 400


class TestRebuild:
    def test_rebuild_from_jsonl_preserves_events(self, store):
        with session_scope(store, skill="speckit-plan") as handle:
            handle.record("planning_output", {"requirements_count": 5, "tasks_generated": 9})
        before = store.query_events(limit=1000)
        counts = store.rebuild_from_jsonl()
        after = store.query_events(limit=1000)
        assert counts["events"] >= len(before)
        assert len(after) == len(before)
        assert {e["event_id"] for e in after} == {e["event_id"] for e in before}
