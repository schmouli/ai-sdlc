"""Tests for SkillBox metrics + reports (US5 — FR-039..FR-044, fail-safe-only rules)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.skillbox.metrics import SkillBoxMetrics
from src.skillbox.overlays import OverlayLibrary
from src.skillbox.registry import SkillRegistry
from src.skillbox.reports import refresh_dashboards, write_improvement_report
from src.skillbox.review import ReviewManager
from src.skillbox.telemetry import default_store


@pytest.fixture()
def env():
    root = Path(tempfile.mkdtemp(prefix="skillbox-met-"))
    store = default_store(root)
    yield store, root
    store.close()


def _seed_skill_sessions(store, skill="speckit-implement", count=5, success_rate=1.0,
                         incomplete=0):
    """Seed `count` sessions; the LAST `incomplete` sessions never complete (FR-022)."""
    for i in range(count):
        sid = store.get_or_create_session(skill=skill)
        store.record_event("skill_started", {"skill": skill}, session_id=sid)
        if i >= count - incomplete:
            continue  # intentionally never completed (incomplete — FR-022)
        if (i + 1) / max(1, count - incomplete) <= success_rate:
            store.record_event("skill_completed",
                               {"skill": skill, "outcome": "success", "duration_ms": 900},
                               session_id=sid)
        else:
            store.record_event("skill_completed",
                               {"skill": skill, "outcome": "failure", "duration_ms": 400},
                               session_id=sid)
        store.complete_session(sid)


class TestSkillMetrics:
    def test_per_skill_profile_correctness(self, env):
        store, root = env
        _seed_skill_sessions(store, skill="speckit-implement", count=5, success_rate=0.8)
        metrics = SkillBoxMetrics(store)
        report = metrics.analyze(window_days=30)
        impl = [s for s in report["skills"] if s["skill"] == "speckit-implement"]
        assert impl
        assert impl[0]["activations"] >= 4
        assert impl[0]["successes"] + impl[0]["failures"] == len(
            [e for e in store.query_events(event_type="skill_completed")
             if e["payload"]["outcome"] in ("success", "failure")])

    def test_incomplete_sessions_excluded_from_success_numerators(self, env):
        store, root = env
        _seed_skill_sessions(store, skill="speckit-plan", count=4, success_rate=1.0,
                             incomplete=1)
        metrics = SkillBoxMetrics(store)
        report = metrics.analyze(window_days=30)
        row = next(s for s in report["skills"] if s["skill"] == "speckit-plan")
        assert row["incomplete_sessions"] >= 1  # FR-022 surfaced
        assert row["successes"] == 3  # incomplete session NOT a success (FR-022)
        assert row["activations"] == 4  # but it still counts as an activation

    def test_incomplete_excluded_from_success_rate_window(self, env):
        store, root = env
        # 3 complete successes, 1 incomplete (no completion event)
        for _ in range(3):
            sid = store.get_or_create_session(skill="speckit-tasks")
            store.record_event("skill_completed",
                               {"skill": "speckit-tasks", "outcome": "success",
                                "duration_ms": 100}, session_id=sid)
            store.complete_session(sid)
        sid = store.get_or_create_session(skill="speckit-tasks")  # never completed
        store.record_event("skill_started", {"skill": "speckit-tasks"}, session_id=sid)
        metrics = SkillBoxMetrics(store)
        report = metrics.analyze(window_days=30)
        row = next(s for s in report["skills"] if s["skill"] == "speckit-tasks")
        assert row["activations"] == 4
        assert row["successes"] == 3  # incomplete session NOT in numerator (FR-022)
        assert 0.0 <= row["improvement_score"] <= 1.0  # FR-039 composite present

    def test_improvement_score_reflects_outcomes(self, env):
        store, root = env
        _seed_skill_sessions(store, skill="speckit-implement", count=4, success_rate=1.0)
        _seed_skill_sessions(store, skill="speckit-plan", count=4, success_rate=0.0)
        metrics = SkillBoxMetrics(store)
        report = metrics.analyze(window_days=30)
        by_skill = {s["skill"]: s for s in report["skills"]}
        assert by_skill["speckit-implement"]["improvement_score"] > \
            by_skill["speckit-plan"]["improvement_score"]  # orders outcomes meaningfully
        assert by_skill["speckit-implement"]["improvement_score"] > 0.5


class TestOverlayImpact:
    def _compositions(self, store, lib, approve=True, with_overlay=3, without_overlay=2):
        from src.skillbox.review import ReviewManager

        lib.ensure_overlay("testing/demo-lessons", "Demo", "testing", ["LRN-1"])
        lib.stage_version("testing/demo-lessons", "- guidance", ["LRN-1"],
                          {"frequency": 9, "success_rate": 0.9})
        if approve:
            rm = ReviewManager(store.root, library=lib, store=store)
            rm.approve_version("testing/demo-lessons", "v01", operator="schmo")
        for i in range(with_overlay):
            sid = store.get_or_create_session(skill="speckit-implement")
            store.record_event("skill_completed",
                               {"skill": "speckit-implement", "outcome": "success",
                                "duration_ms": 100},
                               session_id=sid)
            store.complete_session(sid)
            store.record_event("composition_performed", {
                "skill": "speckit-implement",
                "overlay_versions": ["testing/demo-lessons@v01"] if approve else [],
                "context_detected": {}, "degraded": [],
            }, session_id=sid)
        for i in range(without_overlay):
            sid = store.get_or_create_session(skill="speckit-implement")
            store.record_event("composition_performed", {
                "skill": "speckit-implement", "overlay_versions": [],
                "context_detected": {}, "degraded": [],
            }, session_id=sid)

    def test_impact_per_version_with_floor(self, env):
        store, root = env
        _, lib = None, OverlayLibrary(root).bind_store(store)
        self._compositions(store, lib, approve=True, with_overlay=4, without_overlay=3)
        metrics = SkillBoxMetrics(store, {"overlay_min_executions": 3})
        report = metrics.analyze(window_days=30)
        target = next(o for o in report["overlays"]
                      if o["overlay_id"] == "testing/demo-lessons")
        assert target["executions_influenced"] == 4
        assert target["defect_reduction"] is not None  # floor met -> comparison reported

    def test_floor_not_met_no_defect_reduction(self, env):
        store, root = env
        lib = OverlayLibrary(root).bind_store(store)
        self._compositions(store, lib, approve=True, with_overlay=1, without_overlay=2)
        metrics = SkillBoxMetrics(store, {"overlay_min_executions": 3})
        report = metrics.analyze(window_days=30)
        target = next(o for o in report["overlays"]
                      if o["overlay_id"] == "testing/demo-lessons")
        assert target["defect_reduction"] is None  # FR-040 floor enforced

    def test_lifecycle_fail_safe_only(self, env):
        store, root = env
        lib = OverlayLibrary(root).bind_store(store)
        # high success rate -> prioritized (fail-safe direction OK); never 'active'
        self._compositions(store, lib, approve=True, with_overlay=6, without_overlay=2)
        metrics = SkillBoxMetrics(store, {"overlay_min_executions": 3,
                                          "overlay_promote_rate": 0.8,
                                          "overlay_retire_rate": 0.4})
        report = metrics.analyze(window_days=30)
        target = next(o for o in report["overlays"] if o["overlay_id"] == "testing/demo-lessons")
        assert target["promotion_state"] in ("prioritized", "normal", "retired")
        assert target["promotion_state"] != "active"  # activation NEVER via rules (FR-041)

    def test_retire_rule_with_evidence_recorded(self, env):
        store, root = env
        lib = OverlayLibrary(root).bind_store(store)
        self._compositions(store, lib, approve=False, with_overlay=0, without_overlay=1)
        # craft a failing composition cohort for the retire rule
        sid = store.get_or_create_session(skill="speckit-implement")
        store.record_event("skill_completed",
                           {"skill": "speckit-implement", "outcome": "failure",
                            "duration_ms": 10}, session_id=sid)
        store.complete_session(sid)
        lib.ensure_overlay("testing/bad-overlay", "Bad", "testing", ["LRN-9"])
        lib.stage_version("testing/bad-overlay", "- bad", ["LRN-9"],
                          {"frequency": 5, "success_rate": 0.1})
        rm = ReviewManager(store.root, library=lib, store=store)
        rm.approve_version("testing/bad-overlay", "v01", operator="schmo")
        for i in range(5):
            sid = store.get_or_create_session(skill="speckit-implement")
            store.record_event("skill_completed",
                               {"skill": "speckit-implement", "outcome": "failure",
                                "duration_ms": 10}, session_id=sid)
            store.complete_session(sid)
            store.record_event("composition_performed", {
                "skill": "speckit-implement",
                "overlay_versions": ["testing/bad-overlay@v01"],
                "context_detected": {}, "degraded": [],
            }, session_id=sid)
        metrics = SkillBoxMetrics(store, {"overlay_min_executions": 3,
                                          "overlay_retire_rate": 0.4})
        report = metrics.analyze(window_days=30)
        bad = next(o for o in report["overlays"] if o["overlay_id"] == "testing/bad-overlay")
        assert bad["promotion_state"] == "retired"
        # evidence recorded (FR-041) as decision records
        decisions = store.connection().execute(
            "SELECT decision, reason FROM decisions WHERE decision LIKE 'overlay retire%'"
        ).fetchall()
        assert decisions and "rate=" in decisions[0]["reason"]


class TestReports:
    def test_improvement_report_window_deltas_complete(self, env):
        store, root = env
        _seed_skill_sessions(store, skill="speckit-implement", count=4)
        metrics = SkillBoxMetrics(store)
        report_path = write_improvement_report(root, metrics)
        content = report_path.read_text(encoding="utf-8")
        assert "Per-Skill Changes" in content
        assert "speckit-implement" in content
        assert "Per-Overlay-Version Changes" in content
        assert "Human Approval Events" in content  # FR-042 timeline markers section

    def test_dashboards_refresh_outputs(self, env):
        store, root = env
        _seed_skill_sessions(store, skill="speckit-implement", count=3)
        metrics = SkillBoxMetrics(store)
        outputs = refresh_dashboards(root, metrics)
        dashboard = Path(outputs["dashboard"])
        assert dashboard.exists()
        assert "SkillBox Adaptive Learning Dashboard" in dashboard.read_text(encoding="utf-8")
        assert Path(outputs["latest"]).exists()
        assert json.loads(Path(outputs["latest"]).read_text(encoding="utf-8"))["report"]
