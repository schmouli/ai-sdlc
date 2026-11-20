"""Tests for SkillBox learning extraction (US2 — FR-024..FR-027, deterministic R5)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.skillbox.learning import LearningEngine
from src.skillbox.telemetry import default_store


@pytest.fixture()
def env():
    root = Path(tempfile.mkdtemp(prefix="skillbox-lrn-"))
    store = default_store(root)
    engine = LearningEngine(store, root / "learnings")
    yield store, engine, root
    store.close()


def _seed_corpus(store, sessions=8, failures=6, commands=6):
    ids = []
    for i in range(sessions):
        sid = store.get_or_create_session(skill="speckit-implement")
        store.record_event("coding_output", {"files_created": 2, "files_modified": 1,
                                             "lines_added": 50, "lines_deleted": 5},
                           session_id=sid)
        if i < failures:
            store.record_event("failure_event",
                               {"failure_category": "database-migration",
                                "failure_detail": "migration fixture"},
                               session_id=sid)
        if i < commands:
            store.record_event("command_result", {"command": "pytest -q",
                                                  "exit_code": 0 if i % 2 else 1,
                                                  "result": "success" if i % 2 else "failure"},
                               session_id=sid)
        store.record_event("skill_completed", {"skill": "speckit-implement",
                                               "outcome": "success", "duration_ms": 1000},
                           session_id=sid)
        store.complete_session(sid)
        ids.append(sid)
    return ids


class TestDeterministicExtraction:
    def test_same_corpus_same_learnings(self, env):
        store, engine, root = env
        _seed_corpus(store)
        first = engine.extract_learnings()
        second = engine.extract_learnings()
        keys1 = sorted(r["pattern"] for r in first if r["frequency"])
        keys2 = sorted(r["pattern"] for r in second if r["frequency"])
        assert keys1 == keys2  # SC-004 idempotency
        ids1 = sorted(r["learning_id"] for r in first if r in first)
        # re-run must not duplicate rows
        conn = store.connection()
        count = conn.execute("SELECT COUNT(*) AS n FROM learnings").fetchone()["n"]
        assert count == len(first)

    def test_required_fields_present(self, env):
        store, engine, root = env
        _seed_corpus(store, failures=8)
        learnings = engine.extract_learnings()
        assert learnings, "must produce learnings from a seeded corpus"
        for record in learnings:
            assert record["pattern"]
            assert record["frequency"] >= 1
            assert 0.0 <= record["success_rate"] <= 1.0
            assert record["recommendation"]

    def test_category_assignment_from_event_types(self, env):
        store, engine, root = env
        _seed_corpus(store, failures=8, commands=8)
        learnings = engine.extract_learnings()
        categories = {r["category"] for r in learnings}
        assert categories <= {"architecture", "testing", "security", "performance"}
        assert "testing" in categories  # failure_event -> testing (database-migration->architecture excepted)
        assert "performance" in categories  # command_result -> testing; deployment absent:
        # command_result maps to testing per data-model mapping; performance requires deployment_result
        conn = store.connection()
        conn.execute("INSERT OR IGNORE INTO sessions (session_id, started_at, last_activity_at,"
                     " skills_touched, status, inferred, metadata) VALUES"
                     " ('SBX-20261109-9999', '2026-11-09T10:00:00-05:00',"
                     " '2026-11-09T10:00:00-05:00', '[]', 'complete', 0, '{}')")
        store.record_event("deployment_result", {"deployment": "success", "environment": "dev"},
                           session_id="SBX-20261109-9999")
        learnings = engine.extract_learnings()
        assert "performance" in {r["category"] for r in learnings}


class TestEvidenceBars:
    def test_below_bar_stays_observation(self, env):
        store, engine, root = env
        # 2 observations only (min_frequency=5) -> observation status
        for _ in range(2):
            sid = store.get_or_create_session(skill="speckit-plan")
            store.record_event("failure_event", {"failure_category": "api",
                                                 "failure_detail": "f"}, session_id=sid)
        learnings = engine.extract_learnings()
        api_learnings = [r for r in learnings if "failure" in r["pattern"].lower()]
        assert api_learnings
        assert all(r["status"] == "observation" for r in api_learnings)

    def test_meets_bar_becomes_overlay_eligible(self, env):
        store, engine, root = env
        for _ in range(8):
            sid = store.get_or_create_session(skill="speckit-plan")
            store.record_event("validation_result", {"tests_passed": 100, "tests_failed": 0},
                               session_id=sid)
        learnings = engine.extract_learnings()
        validation = [r for r in learnings if r["category"] == "testing"
                      and r["frequency"] >= 5]
        assert validation and any(r["status"] == "overlay-eligible" for r in validation)


class TestRetirement:
    def test_evidence_decay_retires_learning(self, env):
        store, engine, root = env
        # strong validation signal first -> overlay-eligible
        for _ in range(8):
            sid = store.get_or_create_session(skill="speckit-plan")
            store.record_event("validation_result", {"tests_passed": 100, "tests_failed": 0},
                               session_id=sid)
        first = engine.extract_learnings()
        eligible = [r for r in first if r["status"] == "overlay-eligible"]
        assert eligible, "seeded corpus must yield eligible learnings for the drill"
        # wipe the DB events -> evidence drops; re-extraction retires (FR-027)
        conn = store.connection()
        with conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM sessions")
        engine.extract_learnings()
        rows = conn.execute("SELECT learning_id, status FROM learnings").fetchall()
        by_id = {r["learning_id"]: r["status"] for r in rows}
        for record in eligible:
            assert by_id.get(record["learning_id"]) == "retired"  # FR-027

    def test_retired_history_never_deleted(self, env):
        store, engine, root = env
        for _ in range(8):
            sid = store.get_or_create_session(skill="speckit-plan")
            store.record_event("validation_result", {"tests_passed": 100, "tests_failed": 0},
                               session_id=sid)
        engine.extract_learnings()
        conn = store.connection()
        with conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM sessions")
        engine.extract_learnings()
        rows = conn.execute("SELECT COUNT(*) AS n FROM learnings").fetchone()
        assert rows["n"] > 0, "retirement keeps history rows"


class TestReasonClustering:
    def test_recurring_decisions_cluster(self, env):
        store, engine, root = env
        for i in range(4):
            store.record_decision("selected repository pattern",
                                  "matches existing architecture and regression budget",
                                  session_id=None)
        learnings = engine.extract_learnings()
        decision_learnings = [r for r in learnings if r["pattern"].startswith("Recurring decision")]
        assert decision_learnings, "clustered decision reasons must surface"
        assert decision_learnings[0]["frequency"] >= 4
