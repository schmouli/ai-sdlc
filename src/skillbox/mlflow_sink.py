"""SkillBox MLflow push adapter (research R2; contracts/mlflow-push.md).

Best-effort side channel (FR-018/FR-019, constitution Principle V): local
persistence is the source of truth; a push failure never blocks/drops anything.
Writes ONLY to ``skillbox-*``-namespaced experiments; trading experiments are
never touched (FR-047 — verified in scripts/verify_skillbox.py).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.skillbox import DEGRADE_PUSH
from src.skillbox.telemetry import _compact

logger = logging.getLogger("skillbox.mlflow_sink")

SKILLBOX_EXPERIMENT_PREFIX = "skillbox-"
DEFAULT_EXPERIMENT = "skillbox-agent-learning"
TRADING_EXPERIMENT = "mt4-self-learning"


class MlflowSink:
    """Batched, failure-tolerant event push to MLflow. Disabled-safe by design."""

    def __init__(self, mlflow_cfg: Optional[Dict[str, Any]] = None):
        cfg = mlflow_cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.experiment = cfg.get("experiment") or DEFAULT_EXPERIMENT
        if not self.experiment.startswith(SKILLBOX_EXPERIMENT_PREFIX):
            self.experiment = SKILLBOX_EXPERIMENT_PREFIX + self.experiment
        self.tracking_uri = cfg.get("tracking_uri")
        self.batch_interval = int(cfg.get("batch_interval_seconds", 60))
        env_trace_flag = os.environ.get("MT4_SKILLBOX_MLFLOW_TRACES_ENABLED", "").strip().lower()
        self.traces_enabled = bool(cfg.get("tracing_enabled", True)) and env_trace_flag not in ("0", "false", "no", "off")
        self._last_flush = 0.0
        self._setup_ok: Optional[bool] = None

    # ---------------------------------------------------------------- setup
    def _ensure_setup(self) -> bool:
        """Import + set tracking URI once; permanent-disable on failure (fail-soft)."""
        if self._setup_ok is not None:
            return self._setup_ok
        try:
            import mlflow

            uri = self.tracking_uri or os.environ.get(
                "MT4_SKILLBOX_MLFLOW_TRACKING_URI"
            ) or "sqlite:///mlflow.db"
            mlflow.set_tracking_uri(uri)
            self._mlflow = mlflow
            self._setup_ok = True
        except Exception as exc:
            logger.warning("MLflow sink permanently disabled (%s): %s", DEGRADE_PUSH, exc)
            self._setup_ok = False
        return self._setup_ok

    # ----------------------------------------------------------------- push
    def push_events(self, events: List[Dict[str, Any]], force: bool = False) -> int:
        """Flush a batch of event records; returns pushed count (never raises)."""
        if not events:
            return 0
        env_flag = os.environ.get("MT4_SKILLBOX_MLFLOW_ENABLED", "").strip().lower()
        enabled = self.enabled and env_flag not in ("0", "false", "no", "off")
        if not enabled:
            # explicit disabled state (config or env): mark and keep local record
            self._record_states(events, "skipped_disabled")
            return 0
        if not force and (time.time() - self._last_flush) < self.batch_interval:
            return 0
        if not self._ensure_setup():
            self._record_states(events, "failed", "mlflow unavailable")
            return 0
        pushed = 0
        by_session: Dict[str, List[Dict[str, Any]]] = {}
        for event in events:
            by_session.setdefault(event.get("session_id") or "no-session", []).append(event)
        for session_id, group in by_session.items():
            try:
                self._push_session_batch(session_id, group)
                pushed += len(group)
                self._record_states(group, "pushed")
            except Exception as exc:
                reason = _compact(str(exc))
                logger.warning("push failed for session %s (%s): %s", session_id,
                               DEGRADE_PUSH, reason)
                self._record_states(group, "failed", reason)
        self._last_flush = time.time()
        return pushed

    def _push_session_batch(self, session_id: str, group: List[Dict[str, Any]]) -> None:
        mlflow = getattr(self, "_mlflow", None)
        if mlflow is None:
            raise RuntimeError("mlflow runtime unavailable")
        experiment_id = mlflow.set_experiment(self.experiment).experiment_id
        with mlflow.start_run(run_name=session_id, experiment_id=experiment_id) as run:
            mlflow.set_tags({
                "skillbox": "true",
                "skillbox_session": session_id,
                "mlrun.source": "skillbox",
            })
            counts: Dict[str, int] = {}
            for event in group:
                etype = event.get("event_type", "unknown")
                counts[etype] = counts.get(etype, 0) + 1
            for etype, count in sorted(counts.items()):
                mlflow.log_metric(f"events_{etype}", count)
            lines = [json.dumps(e, default=str) for e in group]
            mlflow.log_text("\n".join(lines), "events.jsonl")
        # Per-event MLflow traces (spec 004 amendment 2026-11-12-trace-emit): one
        # trace + span per event so wiring/skillbox events appear in the UI Traces
        # tab. Fail-soft per span: a trace emission error marks the batch failed
        # (retryable) but never raises; the metrics/artifact push above is the
        # primary mirror and already succeeded by this point.
        if self.traces_enabled:
            experiment_id = str(experiment_id)
            for event in group:
                try:
                    self._emit_event_trace(event, experiment_id, session_id)
                except Exception as exc:
                    raise RuntimeError(f"trace emission failed: {_compact(str(exc))}")

    def _emit_event_trace(self, event: Dict[str, Any], experiment_id: str,
                          session_id: str) -> None:
        """Emit one MLflow trace (single span) for one event; sanitized attributes only."""
        from mlflow.tracing.fluent import start_span_no_context, SpanType

        attributes = {
            "skillbox": "true",
            "skillbox.session": _compact(str(session_id)),
            "skillbox.event_type": _compact(str(event.get("event_type", "unknown"))),
        }
        raw_ts = event.get("timestamp")
        if raw_ts:
            attributes["skillbox.timestamp"] = _compact(str(raw_ts))
        sv = event.get("schema_version")
        if sv is not None:
            attributes["skillbox.schema_version"] = _compact(str(sv))
        span = start_span_no_context(
            name="skillbox_event",
            span_type=SpanType.TOOL,
            experiment_id=experiment_id,
            attributes=attributes,
            tags={
                "skillbox": "true",
                "skillbox_session": _compact(str(session_id)),
                "mlrun.source": "skillbox",
            },
        )
        span.set_inputs({"event_type": _compact(str(event.get("event_type", "unknown")))})
        span.set_outputs({"recorded": True, "push_state": "pushed"})
        span.end()

    def _record_states(self, events: List[Dict[str, Any]], status: str,
                       reason: str = "") -> None:
        store = getattr(self, "store", None)
        if store is None or not events:
            return
        try:
            store.record_push_result([e.get("event_id") for e in events if e.get("event_id")],
                                     status, reason)
        except Exception as exc:
            logger.debug("push-state recording deferred: %s", exc)

    def bind_store(self, store) -> "MlflowSink":
        self.store = store
        return self

    def flush_queue(self, store, force: bool = False) -> int:
        """Drain pending + previously-failed events (FR-019 retry semantics)."""
        self.bind_store(store)
        pending = store.pending_push_events()
        failed = store.failed_push_events()
        return self.push_events(pending + failed, force=force)
