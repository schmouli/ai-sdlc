"""SkillBox metrics: per-skill performance, per-overlay-version impact (research R10§6; FR-039..FR-041).

Lifecycle rules apply ONLY in the fail-safe direction (FR-041): retire /
deprioritize allowed; activation never (that's the HITL path exclusively).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from src.skillbox.telemetry import atomic_write_json, now_et

logger = logging.getLogger("skillbox.metrics")

ET = ZoneInfo("America/New_York")


class SkillBoxMetrics:
    """Recompute per-skill/per-overlay-version metrics from session data (SC-007)."""

    def __init__(self, store, config: Optional[Dict[str, Any]] = None):
        self.store = store
        self.config = config or {}

    def analyze(self, window_days: Optional[int] = None,
                baseline_days: Optional[int] = None) -> Dict[str, Any]:
        window_days = window_days or 30
        baseline_days = baseline_days or (window_days * 2)
        end = datetime.now(ET)
        from datetime import timedelta

        window_start = (end - timedelta(days=window_days)).isoformat()
        baseline_start = (end - timedelta(days=window_days + baseline_days)).isoformat()

        skills = self.skill_metrics(window_start, end.isoformat())
        overlays = self.overlay_metrics(window_start, end.isoformat())
        baseline_skills = self.skill_metrics(baseline_start, window_start)
        baseline_overlays = self.overlay_metrics(baseline_start, window_start)
        result = {
            "window_start": window_start,
            "window_end": end.isoformat(),
            "skills": skills,
            "overlays": overlays,
            "baseline_skills": baseline_skills,
            "baseline_overlays": baseline_overlays,
            "deltas": self._deltas(skills, overlays, baseline_skills, baseline_overlays),
        }
        self._snapshot(result)
        return result

    # ------------------------------------------------------------------ skills
    def skill_metrics(self, since: str, until: str) -> List[Dict[str, Any]]:
        events = self.store.query_events(since=since, until=until, limit=50000)
        by_skill: Dict[str, List[Dict[str, Any]]] = {}
        for event in events:
            skill = event.get("skill")
            if skill:
                by_skill.setdefault(skill, []).append(event)

        sessions = self.store.connection().execute(
            "SELECT session_id, skills_touched, status FROM sessions"
        ).fetchall()
        session_status = {(r["session_id"], ): r["status"] for r in sessions}
        session_by_id = {r["session_id"]: r for r in sessions}

        out = []
        for skill, group in sorted(by_skill.items()):
            completions = [e for e in group if e["event_type"] == "skill_completed"]
            # FR-022: sessions without completion excluded from success metrics
            complete_sessions = [sid for sid, r in session_by_id.items()
                                 if skill in json.loads(r["skills_touched"] or "[]")
                                 and r["status"] == "complete"]
            successes = sum(1 for e in completions
                            if (e.get("payload") or {}).get("outcome") == "success")
            failures = sum(1 for e in completions
                           if (e.get("payload") or {}).get("outcome") == "failure")
            durations = [(e.get("payload") or {}).get("duration_ms") for e in completions]
            durations = [d for d in durations if isinstance(d, (int, float))]
            validations = [e for e in group if e["event_type"] == "validation_result"]
            tests_passed = sum((e.get("payload") or {}).get("tests_passed", 0) for e in validations)
            tests_failed = sum((e.get("payload") or {}).get("tests_failed", 0) for e in validations)
            test_pass_rate = tests_passed / max(1, tests_passed + tests_failed)
            deployments = [e for e in group if e["event_type"] == "deployment_result"]
            deployment_rate = None
            if deployments:
                ok = sum(1 for e in deployments
                         if (e.get("payload") or {}).get("deployment") == "success")
                deployment_rate = ok / len(deployments)
            # FR-022 auditability: any session for this skill without a completion
            # (incomplete OR still in_progress) counts as an activation but is
            # excluded from the success numerator (FR-022).
            skill_sessions = [r for r in session_by_id.values()
                              if skill in json.loads(r["skills_touched"] or "[]")]
            active_incomplete = [r for r in skill_sessions
                                 if r["status"] in ("incomplete", "in_progress")]
            incomplete = len(active_incomplete)
            activations = len([r for r in skill_sessions
                               if r["status"] in ("complete", "incomplete", "in_progress")])
            out.append({
                "skill": skill,
                "activations": activations,
                "successes": successes,
                "failures": failures,
                "avg_duration_ms": round(sum(durations) / len(durations), 1) if durations else 0.0,
                "test_pass_rate": round(test_pass_rate, 3),
                "deployment_success_rate": round(deployment_rate, 3)
                if deployment_rate is not None else None,
                "learning_contribution": self._learning_contribution(skill, events),
                "improvement_score": self._improvement_score(
                    activations, successes, failures, test_pass_rate,
                    deployment_rate, durations),
                "incomplete_sessions": incomplete,
            })
        return out

    @staticmethod
    def _improvement_score(activations: int, successes: int, failures: int,
                           test_pass_rate: float, deployment_rate: Optional[float],
                           durations: List[float]) -> float:
        """FR-039/E9 composite improvement score, 0.0..1.0.

        Deterministic weighted blend: success rate (0.4), test pass rate (0.3),
        deployment success (0.2, excluded from the blend when never deployed),
        duration-efficiency (0.1; sub-1000ms sessions score highest). A skill
        without any completed session scores 0.0 (no evidence — conservative).
        """
        if activations <= 0 or (successes + failures) == 0:
            return 0.0
        completion = successes + failures
        success_rate = successes / completion
        deploy_component = (deployment_rate if deployment_rate is not None else 0.5)
        deploy_weight = 0.2 if deployment_rate is not None else 0.0
        if durations:
            avg = sum(durations) / len(durations)
            duration_component = max(0.0, min(1.0, 1.0 - (avg / 1000.0)))
        else:
            duration_component = 0.0
        score = (0.4 * success_rate + 0.3 * test_pass_rate
                 + deploy_weight * deploy_component
                 + (0.1 if durations else 0.0) * duration_component)
        if deploy_weight == 0.0:
            score += 0.2  # redistribute the excluded deployment weight
        return round(max(0.0, min(1.0, score)), 3)

    def _learning_contribution(self, skill: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """FR-030 (Q3=C): usage count + outcome delta windows."""
        compositions = [e for e in events if e["event_type"] == "composition_performed"
                        and e.get("skill") == skill]
        applied = sum(len((e.get("payload") or {}).get("overlay_versions") or [])
                      for e in compositions)
        rate_delta = None
        with_overlays = [e for e in compositions
                         if (e.get("payload") or {}).get("overlay_versions")]
        without_overlays = [e for e in compositions
                            if not (e.get("payload") or {}).get("overlay_versions")]
        if with_overlays and without_overlays:
            def success_rate(rows):
                vals = []
                for e in rows:
                    sid = e.get("session_id")
                    if not sid:
                        continue
                    session = self.store.get_session(sid)
                    if session and session["status"] == "complete":
                        vals.append(1.0)
                    elif session:
                        vals.append(0.0)
                return sum(vals) / len(vals) if vals else 0.0
            rate_delta = round(success_rate(with_overlays) - success_rate(without_overlays), 3)
        return {"applied_count": applied, "outcome_delta": rate_delta}

    # ---------------------------------------------------------------- overlays
    def overlay_metrics(self, since: str, until: str) -> List[Dict[str, Any]]:
        """Per-overlay-VERSION impact (E10); cohort comparison with min floor."""
        min_executions = int(self.config.get("overlay_min_executions", 3))
        compositions = [e for e in self.store.query_events(
            event_type="composition_performed", since=since, until=until, limit=50000)]
        with_versions: Dict[str, List[Dict[str, Any]]] = {}
        without: List[Dict[str, Any]] = []
        for event in compositions:
            versions = (event.get("payload") or {}).get("overlay_versions") or []
            if versions:
                for ref in versions:
                    with_versions.setdefault(ref, []).append(event)
            else:
                without.append(event)

        def session_success(events_list):
            """Outcome-rate over the cohort's sessions (FR-033): uses the skill
            completion outcome, not just session status."""
            values = []
            for e in events_list:
                payloads = [x.get("payload") for x in
                            self.store.query_events(session_id=e.get("session_id") or "",
                                                    event_type="skill_completed")]
                if payloads:
                    values.append(1.0 if payloads[-1].get("outcome") == "success" else 0.0)
                else:
                    session = self.store.get_session(e.get("session_id") or "")
                    if session and session["status"] != "complete":
                        values.append(0.0)  # incomplete sessions fail-safe to 0
            return sum(values) / len(values) if values else 0.0

        def cohort_outcome_rate(session_ids):
            """Success rate over the *session* completions in the cohort."""
            if not session_ids:
                return 0.0
            values = []
            for sid in session_ids:
                completions = self.store.query_events(session_id=sid,
                                                      event_type="skill_completed")
                if completions:
                    values.append(1.0 if completions[-1]["payload"].get("outcome") == "success"
                                  else 0.0)
                else:
                    values.append(0.0)  # incomplete -> not a success (FR-022)
            return sum(values) / len(values)

        def defect_rate(events_list):
            return None  # defect proxy handled via cohort_outcome_rate above

        out = []
        cohort_session_ids = [e.get("session_id") for e in without]
        for ref, group in sorted(with_versions.items()):
            overlay_id, _, version = ref.partition("@")
            rate = session_success(group)
            cohort_rate = cohort_outcome_rate(cohort_session_ids)
            defect_reduction = None
            if len(group) >= min_executions and without:
                defect_reduction = round((rate - cohort_rate) * 100, 1)
            out.append({
                "overlay_id": overlay_id,
                "version": version,
                "executions_influenced": len(group),
                "success_rate": round(rate, 3),
                "defect_reduction": defect_reduction,
                "promotion_state": self._evaluate_rule(overlay_id, version, rate,
                                                       len(group)),
            })
        return out

    def _evaluate_rule(self, overlay_id: str, version: str, rate: float,
                       executions: int) -> str:
        """Fail-safe-only lifecycle rules (FR-041): retire OR prioritize, never activate."""
        floor = float(self.config.get("overlay_min_executions", 3))
        retire_bar = float(self.config.get("overlay_retire_rate", 0.4))
        promote_bar = float(self.config.get("overlay_promote_rate", 0.8))
        if executions >= floor and rate < retire_bar:
            self._record_lifecycle("retire", overlay_id, version, rate, executions)
            return "retired"
        if executions >= floor and rate >= promote_bar:
            self._record_lifecycle("prioritize", overlay_id, version, rate, executions)
            return "prioritized"
        return "normal"

    def _record_lifecycle(self, action: str, overlay_id: str, version: str,
                          rate: float, executions: int) -> None:
        """Audit trail with metric evidence (FR-041)."""
        self.store.record_decision(
            f"overlay {action}", f"rate={rate} executions={executions}",
            skill=overlay_id,
            context_summary=f"overlay_version={overlay_id}@{version}",
        )
        logger.info("overlay lifecycle %s: %s@%s (rate=%s, executions=%s)",
                    action, overlay_id, version, rate, executions)

    # --------------------------------------------------------------- snapshots
    def _snapshot(self, result: Dict[str, Any]) -> None:
        conn = self.store.connection()
        generated = now_et()
        for kind, key in (("skills", "skill"), ("overlays", "overlay_id")):
            for row in result.get(kind, []):
                subject = row.get(key) if key != "overlay_id" else \
                    f"{row.get('overlay_id')}@{row.get('version')}"
                with conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO metric_snapshots (subject_type, subject_id,"
                        " window_start, window_end, metrics, generated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (key if key != "skill" else "skill",
                         subject, result["window_start"], result["window_end"],
                         json.dumps(row), generated),
                    )
        root = self.store.root if hasattr(self.store, "root") else Path(".skillbox")
        try:
            atomic_write_json(Path(root) / "registry" / "metrics.json",
                              {"generated_at": generated, "latest": result})
        except OSError as exc:
            logger.warning("metrics registry snapshot deferred: %s", exc)

    @staticmethod
    def _deltas(skills, overlays, baseline_skills, baseline_overlays):
        """FR-042: per-subject current-vs-baseline deltas (no element missing)."""
        def by_key(rows, key):
            return {r.get(key): r for r in rows if r.get(key)}

        deltas = {"skills": [], "overlays": []}
        base_s = by_key(baseline_skills, "skill")
        for row in skills:
            base = base_s.get(row["skill"], {})
            deltas["skills"].append({
                "subject": row["skill"],
                "activations": row["activations"] - base.get("activations", 0),
                "success_rate_change": round(
                    (row["successes"] / max(1, row["activations"]))
                    - (base.get("successes", 0) / max(1, base.get("activations", 0) or 1)), 3),
            })
        base_o = by_key(baseline_overlays, "overlay_id")
        for row in overlays:
            ref = f"{row.get('overlay_id')}@{row.get('version')}"
            base = {f"{b.get('overlay_id')}@{b.get('version')}": b
                    for b in baseline_overlays}.get(ref, {})
            deltas["overlays"].append({
                "subject": ref,
                "executions": row["executions_influenced"] - base.get("executions_influenced", 0),
                "success_rate_change": round(
                    row["success_rate"] - base.get("success_rate", row["success_rate"]), 3),
            })
        return deltas
