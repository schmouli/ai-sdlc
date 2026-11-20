"""SkillBox learning extraction engine (research R5; FR-024..FR-027).

Deterministic aggregation over telemetry: outcome-rate patterns per (skill,
event_type, failure_category, context-cohort) tuples, cohort contrast patterns,
decision-record reason clustering. No LLM; templates make re-runs identical
(SC-004). Evidence bars: min_frequency + min_consistency; retirement on decay
(FR-027) — history never deleted.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.skillbox.telemetry import atomic_write_json, now_et

logger = logging.getLogger("skillbox.learning")

CATEGORIES = ("architecture", "testing", "security", "performance")

_LEARNING_ID_RX = re.compile(r"^LRN-[0-9a-f]{20}$")

# event_type -> learning category mapping (data-model.md E2 -> FR-025)
EVENT_CATEGORY_MAP: Dict[str, str] = {
    "planning_output": "architecture",
    "failure_event": "testing",       # refined by failure_category below
    "validation_result": "testing",
    "command_result": "testing",
    "composition_performed": "architecture",
    "deployment_result": "performance",
    "skill_started": "architecture",
    "skill_completed": "architecture",
    "review_decision_recorded": "security",
    "overlay_version_staged": "security",
    "coding_output": "performance",
    "mlflow_push_result": "performance",
}

_FAILURE_CATEGORY_MAP: Dict[str, str] = {
    "authentication": "security",
    "security": "security",
    "database": "performance",
    "database-migration": "architecture",
    "migration": "architecture",
    "performance": "performance",
    "timeout": "performance",
    "api": "testing",
    "integration": "testing",
    "validation": "testing",
    "dependency": "testing",
    "deployment": "performance",
}


def _pct(value: float) -> str:
    return f"{value:.0%}"


def _new_learning_id(ts: str) -> str:
    """Stable id derived from timestamp+content — deterministic (SC-004)."""
    import hashlib

    digest = hashlib.sha256(ts.encode("utf-8")).hexdigest()[:20]
    return f"LRN-{digest}"


class LearningEngine:
    """Scheduled learning analysis over accumulated telemetry (FR-024)."""

    def __init__(self, store, learnings_root: Optional[Path] = None,
                 config: Optional[Dict[str, Any]] = None):
        self.store = store
        if learnings_root is None:
            # fail-soft default (FR-051 family): derive from the store's state root
            root = getattr(store, "root", None)
            learnings_root = (Path(root) / "learnings") if root is not None else \
                Path(".skillbox") / "learnings"
        self.learnings_root = Path(learnings_root)
        cfg = config or {}
        self.window_days = int(cfg.get("window_days", 90))
        self.min_frequency = int(cfg.get("min_frequency", 5))
        self.min_consistency = float(cfg.get("min_consistency", 0.6))
        self.reason_cluster_threshold = float(cfg.get("reason_cluster_threshold", 0.5))

    # ------------------------------------------------------------------ analysis
    def extract_learnings(self, window_days: Optional[int] = None) -> List[Dict[str, Any]]:
        """Run analysis over the window; persist learnings + category snapshots.

        Idempotent: repeated analysis on unchanged telemetry returns the same
        persisted set (existing rows updated in place, not duplicated — SC-004).
        """
        window_days = window_days or self.window_days
        events = self._window_events(window_days)
        decisions = self._window_decisions(window_days)

        raw_patterns = self._aggregate_outcome_patterns(events)
        raw_patterns += self._aggregate_cohort_patterns(events)
        raw_patterns += self._cluster_decision_reasons(decisions)

        existing = self._existing_learnings()
        produced: List[Dict[str, Any]] = []
        produced_keys = set()
        for raw in raw_patterns:
            key = raw["pattern_key"]
            produced_keys.add(key)
            if key in existing:
                record = existing[key]
                record["frequency"] = raw["frequency"]
                record["success_rate"] = raw["success_rate"]
                record["updated_at"] = now_et()
                record["status"] = self._status_for(record, raw)
            else:
                record = {
                    "learning_id": _new_learning_id(key),
                    "pattern": raw["pattern"],
                    "pattern_key": key,
                    "frequency": raw["frequency"],
                    "success_rate": raw["success_rate"],
                    "recommendation": raw["recommendation"],
                    "category": raw["category"],
                    "source_evidence": raw["source_evidence"],
                    "status": self._status_for(raw, raw),
                    "created_at": now_et(),
                    "updated_at": now_et(),
                }
            produced.append(record)
            self._persist(record)
        # FR-027: previously eligible/generated learnings NOT re-evidenced by
        # this run (their pattern vanished from current telemetry) are retired.
        for key, record in existing.items():
            if key in produced_keys:
                continue
            if record["status"] in ("overlay-eligible", "overlay-generated"):
                record["frequency"] = 0
                record["status"] = "retired"
                record["updated_at"] = now_et()
                produced.append(record)
                self._persist(record)
                self._retire_linked_overlays(record["learning_id"])
        self._refresh_category_files(produced, existing)
        self._mark_retirements(existing, produced)
        return produced

    # ---------------------------------------------------------------- patterns
    def _window_events(self, window_days: int) -> List[Dict[str, Any]]:
        from src.skillbox.telemetry import now_et as _now

        end = datetime.now()
        start = end.replace(day=max(1, end.day - window_days)) if window_days < 28 else None
        if start is None:
            from datetime import timedelta

            start = end - timedelta(days=window_days)
        return self.store.query_events(since=start.isoformat(), until=_now(), limit=20000)

    def _window_decisions(self, window_days: int) -> List[Dict[str, Any]]:
        conn = self.store.connection()
        rows = conn.execute(
            "SELECT decision_id, session_id, timestamp, decision, reason, context_summary, skill"
            " FROM decisions ORDER BY timestamp DESC LIMIT 5000"
        ).fetchall()
        return [dict(r) for r in rows]

    def _aggregate_outcome_patterns(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
        for event in events:
            etype = event.get("event_type")
            if etype == "failure_event":
                category = _FAILURE_CATEGORY_MAP.get(
                    (event.get("payload") or {}).get("failure_category", ""), "testing"
                )
                key = (event.get("skill"), "failure_event", category, "")
            elif etype == "command_result":
                category = EVENT_CATEGORY_MAP["command_result"]
                key = (event.get("skill"), "command_result", category, "")
            elif etype == "coding_output":
                category = EVENT_CATEGORY_MAP["coding_output"]
                key = (event.get("skill"), "coding_output", category, "")
            elif etype == "validation_result":
                category = EVENT_CATEGORY_MAP["validation_result"]
                key = (event.get("skill"), "validation_result", category, "")
            elif etype in ("skill_started", "skill_completed"):
                continue  # counted via completion outcomes below
            elif etype == "deployment_result":
                category = EVENT_CATEGORY_MAP["deployment_result"]
                key = (event.get("skill"), "deployment_result", category, "")
            else:
                continue
            groups[key].append(event)

        patterns: List[Dict[str, Any]] = []
        for (skill, etype, category, _ctx), group in groups.items():
            successes = sum(1 for e in group if self._event_success(e))
            rate = successes / len(group) if group else 0.0
            template = self._template_for(etype, category)
            if etype == "deployment_result":
                dep_successes = sum(1 for e in group
                                    if (e.get("payload") or {}).get("deployment") == "success")
                rate = dep_successes / len(group) if group else 0.0
            patterns.append({
                "pattern_key": f"outcome:{skill}:{etype}:{category}",
                "pattern": template["pattern"].format(skill=skill or "any-skill",
                                                      frequency=len(group), rate=_pct(rate),
                                                      category=category),
                "frequency": len(group),
                "success_rate": round(rate, 3),
                "recommendation": template["recommendation"].format(skill=skill or "any-skill"),
                "category": category,
                "source_evidence": self._evidence(group),
            })
        return patterns

    def _aggregate_cohort_patterns(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Contrast patterns: sessions without validation events vs with (R5)."""
        by_session: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for event in events:
            if event.get("session_id"):
                by_session[event["session_id"]].append(event)

        with_validation = [s for s in by_session.values()
                           if any(e["event_type"] == "validation_result" for e in s)]
        without_validation = [s for s in by_session.values()
                              if s and not any(e["event_type"] == "validation_result" for e in s)]
        patterns: List[Dict[str, Any]] = []
        successes_with = sum(self._session_success(s) for s in with_validation)
        rate_with = successes_with / len(with_validation) if with_validation else 0.0
        rate_without = (sum(self._session_success(s) for s in without_validation)
                        / len(without_validation)) if without_validation else 0.0
        if len(by_session) >= self.min_frequency and rate_without < rate_with:
            gap = rate_with - rate_without
            if gap >= self.min_consistency:
                patterns.append({
                    "pattern_key": "cohort:validation-coverage",
                    "pattern": (
                        f"Sessions lacking validation events succeeded {_pct(rate_without)}"
                        f" vs {_pct(rate_with)} for validated sessions"
                        f" (gap {_pct(gap)} over {len(without_validation)}+{len(with_validation)} sessions)"
                    ),
                    "frequency": len(without_validation) + len(with_validation),
                    "success_rate": round(rate_without, 3),
                    "recommendation": (
                        "Require validation-result events in every session; integration-test gaps "
                        "raise failure frequency"
                    ),
                    "category": "testing",
                    "source_evidence": self._evidence(
                        [e for s in without_validation for e in s][:20]
                    ),
                })
        return patterns

    def _cluster_decision_reasons(self, decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        clusters: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for d in decisions:
            tokens = set(re.findall(r"[a-z]{4,}", (d.get("reason") or "").lower()))
            if not tokens:
                continue
            matched = None
            for key, members in clusters.items():
                member_tokens = set(members[0]["_tokens"])
                overlap = len(tokens & member_tokens) / max(1, len(tokens | member_tokens))
                if overlap >= self.reason_cluster_threshold:
                    matched = key
                    break
            if matched is None:
                matched = d["decision_id"]
                clusters[matched] = []
            d["_tokens"] = list(tokens)
            clusters[matched].append(d)
        patterns: List[Dict[str, Any]] = []
        for members in clusters.values():
            if len(members) < 2:
                continue
            sample = members[0]
            patterns.append({
                "pattern_key": f"decision:{_compact_key(sample['decision'])}",
                "pattern": (
                    f"Recurring decision '{_compact_key(sample['decision'])}' with clustered reason"
                    f" theme ({len(members)} occurrences, e.g. '{_compact_key(sample['reason'])}')"
                ),
                "frequency": len(members),
                "success_rate": round(
                    sum(1 for m in members if (m.get("decision") or "").strip().lower().startswith(
                        tuple(t for t in ("approve",))) or False) / len(members), 3),
                "recommendation": (
                    f"Standardize this decision path: {_compact_key(sample['reason'])}"
                ),
                "category": "architecture",
                "source_evidence": self._evidence(members),
            })
        return patterns

    # ------------------------------------------------------------- persistence
    def _existing_learnings(self) -> Dict[str, Dict[str, Any]]:
        conn = self.store.connection()
        rows = conn.execute("SELECT * FROM learnings").fetchall()
        out = {}
        for row in rows:
            record = dict(row)
            record["source_evidence"] = json.loads(record["source_evidence"] or "[]")
            if record.get("pattern_key") is None:
                # legacy rows (created before pattern_key existed)
                record["pattern_key"] = record["pattern"]
            out[record["pattern_key"]] = record
        return out

    def _status_for(self, pattern: Dict[str, Any], raw: Optional[Dict[str, Any]] = None) -> str:
        """Status from evidence; retired is sticky until evidence recovers (FR-027)."""
        if pattern.get("status") == "retired":
            # recovery: evidence back above the bar re-eligibles the learning
            if raw is not None and raw["frequency"] >= self.min_frequency and \
                    raw["success_rate"] >= self.min_consistency:
                return "overlay-eligible"
            return "retired"
        if pattern["frequency"] >= self.min_frequency and \
                pattern["success_rate"] >= self.min_consistency:
            return "overlay-eligible"
        return "observation"

    def _persist(self, record: Dict[str, Any]) -> None:
        conn = self.store.connection()
        with conn:
            conn.execute(
                "INSERT INTO learnings (learning_id, pattern, pattern_key, frequency,"
                " success_rate, recommendation, category, source_evidence, status,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(learning_id) DO UPDATE SET frequency = excluded.frequency,"
                " success_rate = excluded.success_rate, status = excluded.status, "
                " updated_at = excluded.updated_at, frequency = excluded.frequency",
                (record["learning_id"], record["pattern"], record.get("pattern_key"),
                 record["frequency"],
                 record["success_rate"], record["recommendation"], record["category"],
                 json.dumps(record["source_evidence"]), record["status"],
                 record["created_at"], record["updated_at"]),
            )

    def _refresh_category_files(self, produced: List[Dict[str, Any]],
                                existing: Dict[str, Dict[str, Any]]) -> None:
        by_area = {
            "successes": [r for r in produced if r["status"] != "retired"],
            "failures": [r for r in produced if "failure" in r["pattern"].lower()
                         and r["status"] != "retired"],
            "retrospectives": list(existing.values()),
        }
        for cat in CATEGORIES:
            by_area[cat] = [r for r in produced if r["category"] == cat and r["status"] != "retired"]
        for area, records in by_area.items():
            area_dir = self.learnings_root / area
            area_dir.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "refreshed_at": now_et(),
                "count": len(records),
                "learnings": [self._public_view(r) for r in records],
            }
            atomic_write_json(area_dir / "current.json", snapshot)

    def _mark_retirements(self, existing: Dict[str, Dict[str, Any]],
                          produced: List[Dict[str, Any]]) -> List[str]:
        """FR-027: retire when current evidence drops below the bar; never delete."""
        retired = []
        for record in produced:
            below_bar = record["frequency"] < self.min_frequency or \
                record["success_rate"] < self.min_consistency
            # eligible/generated rows whose evidence decayed -> retired
            if record["status"] in ("overlay-eligible", "overlay-generated") and below_bar:
                record["status"] = "retired"
                record["updated_at"] = now_et()
                self._persist(record)
                self._retire_linked_overlays(record["learning_id"])
                retired.append(record["learning_id"])
        # legacy existing rows not re-produced by this run keep their status
        return retired

    def _retire_linked_overlays(self, learning_id: str) -> None:
        """Evidence decay cascade (FR-029): retire overlay versions + active fail-safe."""
        try:
            from src.skillbox.overlays import cascade_retire_for_learning

            cascade_retire_for_learning(self.store, learning_id)
        except Exception as exc:
            logger.warning("overlay retirement cascade deferred: %s", exc)

    def _public_view(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "learning_id": record["learning_id"],
            "pattern": record["pattern"],
            "frequency": record["frequency"],
            "success_rate": record["success_rate"],
            "recommendation": record["recommendation"],
            "category": record["category"],
            "status": record["status"],
        }

    # ------------------------------------------------------------------- misc
    def _evidence(self, events: List[Dict[str, Any]], cap: int = 20) -> List[Dict[str, Any]]:
        return [{"session_id": e.get("session_id"), "event_id": e.get("event_id"),
                 "event_type": e.get("event_type")} for e in events[:cap]]

    @staticmethod
    def _event_success(event: Dict[str, Any]) -> bool:
        payload = event.get("payload") or {}
        if event["event_type"] == "command_result":
            return payload.get("result") in ("success", "0")
        if event["event_type"] == "validation_result":
            return payload.get("tests_failed", 0) == 0
        if event["event_type"] == "failure_event":
            return False
        return payload.get("outcome") != "failure"

    @staticmethod
    def _session_success(events: List[Dict[str, Any]]) -> bool:
        completions = [e for e in events if e["event_type"] == "skill_completed"]
        return bool(completions) and all((e.get("payload") or {}).get("outcome") == "success"
                                         for e in completions)

    @staticmethod
    def _template_for(etype: str, category: str) -> Dict[str, str]:
        templates = {
            "failure_event": {
                "pattern": "'{skill}' {frequency} {category}-category failures "
                           "with {rate} success rate",
                "recommendation": "Add pre-flight checks for {skill} failure modes",
            },
            "command_result": {
                "pattern": "Command executions for {skill} in {frequency} runs at {rate} success rate",
                "recommendation": "Standardize the {skill} command pipeline; investigate failed commands",
            },
            "validation_result": {
                "pattern": "Validation runs for {skill} show {rate} pass rate over {frequency} runs",
                "recommendation": "Keep test coverage for {skill}; triage failing validations",
            },
        }
        base = templates.get(etype, templates["command_result"])
        return base


def _compact_key(text: str, limit: int = 120) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 3] + "..."
