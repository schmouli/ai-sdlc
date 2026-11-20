"""SkillBox telemetry: sessions, events, decision records (research R3/R4).

Storage model (contracts/state-files.md):
- Source of truth: append-only JSONL streams under ``<root>/telemetry/raw/YYYYMMDD.jsonl``
- Indexed store: SQLite (WAL) at ``<root>/telemetry/skillbox.db`` — fully rebuildable
- Decision records: SQLite ``decisions`` table + JSON snapshots under ``telemetry/decisions/``

All timestamps are America/New_York ISO-8601 (constitution Additional Constraints).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from zoneinfo import ZoneInfo

from src.skillbox import ContractViolationError

logger = logging.getLogger("skillbox.telemetry")

ET = ZoneInfo("America/New_York")

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
  skills_touched TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL CHECK (status IN ('in_progress','complete','incomplete')),
  inferred INTEGER NOT NULL DEFAULT 0, metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
  timestamp TEXT NOT NULL, skill TEXT, event_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  inferred INTEGER NOT NULL DEFAULT 0,
  push_state TEXT NOT NULL DEFAULT 'pending' CHECK (push_state IN ('pending','pushed','failed','skipped_disabled')),
  payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_skill_time ON events(skill, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_push ON events(push_state);

CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY, session_id TEXT, timestamp TEXT NOT NULL,
  decision TEXT NOT NULL, reason TEXT NOT NULL, context_summary TEXT NOT NULL DEFAULT '', skill TEXT
);

CREATE TABLE IF NOT EXISTS learnings (
  learning_id TEXT PRIMARY KEY, pattern TEXT NOT NULL, pattern_key TEXT,
  frequency INTEGER NOT NULL,
  success_rate REAL NOT NULL, recommendation TEXT NOT NULL, category TEXT NOT NULL
    CHECK (category IN ('architecture','testing','security','performance')),
  source_evidence TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('observation','overlay-eligible','overlay-generated','retired')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_learnings_status ON learnings(status, category);
CREATE INDEX IF NOT EXISTS idx_learnings_key ON learnings(pattern_key);

CREATE TABLE IF NOT EXISTS overlay_versions (
  overlay_id TEXT NOT NULL, version TEXT NOT NULL,
  content_path TEXT NOT NULL, content_hash TEXT NOT NULL, generated_at TEXT NOT NULL,
  source_learning_ids TEXT NOT NULL, evidence_summary TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','active','superseded','rejected','retired')),
  status_changed_at TEXT, pending_since TEXT,
  PRIMARY KEY (overlay_id, version)
);
CREATE INDEX IF NOT EXISTS idx_ov_status ON overlay_versions(status);

CREATE TABLE IF NOT EXISTS review_decisions (
  review_id TEXT PRIMARY KEY, overlay_id TEXT NOT NULL, version TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('approve','reject','rollback','re-activate')),
  operator TEXT NOT NULL, timestamp TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT '',
  evidence_snapshot_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_type TEXT NOT NULL CHECK (subject_type IN ('skill','overlay_version')),
  subject_id TEXT NOT NULL,
  window_start TEXT NOT NULL, window_end TEXT NOT NULL, metrics TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  UNIQUE (subject_type, subject_id, window_start, window_end)
);
"""

# Event types with typed payload schemas (data-model.md E2). Values are python
# types; every key is optional, unknown keys are allowed (forward-compatible).
_EVENT_TYPES: Dict[str, Dict[str, Any]] = {
    "skill_started": {"skill": str},
    "skill_completed": {"skill": str, "outcome": str, "duration_ms": (int, float)},
    "planning_output": {"requirements_count": int, "tasks_generated": int, "architecture_selected": str},
    "coding_output": {
        "files_created": int,
        "files_modified": int,
        "lines_added": int,
        "lines_deleted": int,
    },
    "command_result": {"command": str, "exit_code": int, "result": str},
    "validation_result": {"tests_passed": int, "tests_failed": int},
    "deployment_result": {"deployment": str, "environment": str},
    "failure_event": {"failure_category": str, "failure_detail": str},
    "composition_performed": {"skill": str, "overlay_versions": list, "context_detected": dict, "degraded": list},
    "overlay_version_staged": {"overlay_id": str, "version": str, "source_learning_ids": list},
    "review_decision_recorded": {"review_decision_id": str, "decision": str, "operator": str},
    "mlflow_push_result": {"event_count": int, "status": str, "reason": str},
    "inject_refreshed": {"overlays": int, "target": str},
    # --- feature 005 additions (5 types; required-key enforcement added below) ---
    "hook_gate_accepted": {"hook_event": str, "hook_command": str, "feature_dir": str},
    "hook_gate_skipped": {"hook_event": str, "hook_command": str, "reason": str},
    "speckit_stage_completed": {"stage": str, "feature_dir": str, "decision": str, "recorded_by": str},
    "egm_review_round": {"profile": str, "target_file": str, "reviewers": list, "findings_count": int, "round_kind": str, "consult_mode": str, "consult_freshness": str, "consult_fallback": bool},
    "wiring_health_check": {"checks_total": int, "checks_failed": int},
}

# Feature 005: required-key sets for the NEW event types (FR-012). Pre-existing
# types keep the permissive contract (unknown keys allowed; present keys type-checked).
_REQUIRED_KEYS = {
    "hook_gate_accepted": {"hook_event", "hook_command", "feature_dir"},
    "hook_gate_skipped": {"hook_event", "hook_command", "reason"},
    "speckit_stage_completed": {"stage", "feature_dir", "decision", "recorded_by"},
    "egm_review_round": {"profile", "target_file", "reviewers", "findings_count", "round_kind", "consult_mode", "consult_freshness", "consult_fallback"},
    "wiring_health_check": {"checks_total", "checks_failed"},
}

# Public alias (feature 005): verifier/manifest tools reference this name.
EVENT_TYPES = _EVENT_TYPES

_TEXT_CAP = 400

_SANITIZE_DENY_KEYS = {
    "password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
    "credential", "credentials", "private_key", "access_token", "refresh_token",
    "session_token", "auth", "cookie", "set_cookie",
}
_SANITIZE_TYPE_KEYS = {"file_content", "contents", "content", "source_code", "code"}


def now_et() -> str:
    """Current timestamp as ET ISO-8601 (constitution timezone rule)."""
    return datetime.now(ET).isoformat()


def today_et() -> str:
    """Current ET date as YYYYMMDD (session-id and JSONL partitioning)."""
    return datetime.now(ET).strftime("%Y%m%d")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def _canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _compact(text: Any, limit: int = _TEXT_CAP) -> str:
    """Bounded free-text (mirrors src/ml/mlflow_telemetry.py _compact)."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def sanitize_payload(payload: Any, _depth: int = 0) -> Any:
    """Sanitize a telemetry payload per FR-052 (T005).

    - credential-shaped keys are replaced with ``"<redacted>"``
    - content-shaped keys (file contents / source code) are replaced with
      ``"<file-content-omitted>"`` — telemetry carries paths, never contents
    - all strings capped at 400 chars; nested structures traversed defensively
    """
    if _depth > 6:
        return "<depth-capped>"
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            key_l = str(key).lower()
            if key_l in _SANITIZE_DENY_KEYS:
                out[key] = "<redacted>"
            elif key_l in _SANITIZE_TYPE_KEYS:
                out[key] = "<file-content-omitted>"
            else:
                out[key] = sanitize_payload(value, _depth + 1)
        return out
    if isinstance(payload, (list, tuple)):
        return [sanitize_payload(v, _depth + 1) for v in payload[:50]]
    if isinstance(payload, str):
        return _compact(payload)
    if isinstance(payload, bool) or payload is None:
        return payload
    if isinstance(payload, (int, float)):
        return payload
    return _compact(payload)


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomic pretty-JSON write via temp-file + os.replace (contracts/state-files.md)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


_jsonl_lock_registry: Dict[str, threading.Lock] = {}
_jsonl_lock_guard = threading.Lock()


def _jsonl_lock(key: str) -> threading.Lock:
    with _jsonl_lock_guard:
        if key not in _jsonl_lock_registry:
            _jsonl_lock_registry[key] = threading.Lock()
        return _jsonl_lock_registry[key]


def append_jsonl(path: Path, record: Dict[str, Any], hash_chain: bool = False,
                 prev_record_hash: str = "") -> str:
    """Append one JSON object line; returns its record_hash (hash-chained when asked).

    With ``hash_chain=True`` the record gains ``prev_record_hash``/``record_hash``
    fields; the chain makes deletion/tampering detectable (review audit trail, E7).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line_record = dict(record)
    record_hash = _canonical_hash(line_record)
    if hash_chain:
        line_record["prev_record_hash"] = prev_record_hash
        line_record["record_hash"] = record_hash
    line = json.dumps(line_record, ensure_ascii=False, default=str)
    with _jsonl_lock(str(path)):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return record_hash


class TelemetryStore:
    """SQLite (WAL) indexed store + session/event/decision APIs (T004, T010-T013)."""

    def __init__(self, state_root: Path):
        self.root = Path(state_root)
        self.db_path = self.root / "telemetry" / "skillbox.db"
        self.raw_dir = self.root / "telemetry" / "raw"
        self.decisions_dir = self.root / "telemetry" / "decisions"
        self.sessions_dir = self.root / "telemetry" / "sessions"
        self.quarantine_dir = self.root / "telemetry" / "quarantine"
        self._conn: Optional[sqlite3.Connection] = None
        self._write_guard = threading.RLock()

    # ------------------------------------------------------------------- DB
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
        return self._conn

    def init_db(self) -> None:
        """Create schema per contracts/state-files.md DDL (idempotent)."""
        conn = self.connection()
        with self._write_guard:
            conn.executescript(_DDL)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            # Feature 005: guarded migration ? per-row schema stamp column.
            # Global schema_meta.SCHEMA_VERSION stays 1 (this is an additive
            # event-level stamp, not a second generation; FR-013).
            try:
                conn.execute("ALTER TABLE events ADD COLUMN schema_version INTEGER")
            except Exception:
                pass  # column already exists (already-migrated DB)
            conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ------------------------------------------------------------- sessions
    def allocate_session_id(self) -> str:
        """Allocate ``SBX-YYYYMMDD-NNNN`` per-day ordinal (R4)."""
        day = today_et()
        conn = self.connection()
        with self._write_guard, conn:  # serialized write transaction
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE substr(session_id, 5, 8) = ?",
                (day,),
            ).fetchone()
            ordinal = (row["n"] if row else 0) + 1
            return f"SBX-{day}-{ordinal:04d}"

    def get_or_create_session(
        self, skill: Optional[str] = None, session_id: Optional[str] = None,
        inferred: bool = False, metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Return an existing in-progress session id, or allocate a new one (FR-015)."""
        conn = self.connection()
        if session_id:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE session_id = ? AND status = 'in_progress'",
                (session_id,),
            ).fetchone()
            if row:
                return row["session_id"]
        new_id = self.allocate_session_id()
        ts = now_et()
        with self._write_guard, conn:
            conn.execute(
                "INSERT INTO sessions (session_id, started_at, last_activity_at, skills_touched,"
                " status, inferred, metadata) VALUES (?, ?, ?, ?, 'in_progress', ?, ?)",
                (new_id, ts, ts, json.dumps([skill] if skill else []), int(inferred),
                 json.dumps(sanitize_payload(metadata or {}))),
            )
        self._mirror_session(new_id)
        return new_id

    def touch_session(self, session_id: str, skill: Optional[str] = None) -> None:
        """Update last_activity_at + skills_touched for the session."""
        conn = self.connection()
        with self._write_guard, conn:
            row = conn.execute(
                "SELECT skills_touched FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return
            skills = json.loads(row["skills_touched"] or "[]")
            if skill and skill not in skills:
                skills.append(skill)
            conn.execute(
                "UPDATE sessions SET last_activity_at = ?, skills_touched = ? WHERE session_id = ?",
                (now_et(), json.dumps(skills), session_id),
            )
        self._mirror_session(session_id)

    def complete_session(self, session_id: str) -> None:
        conn = self.connection()
        with self._write_guard, conn:
            conn.execute(
                "UPDATE sessions SET status = 'complete', last_activity_at = ? WHERE session_id = ?",
                (now_et(), session_id),
            )
        self._mirror_session(session_id)

    def mark_session_incomplete(self, session_id: str) -> None:
        """Finalize a session with no completion event as incomplete (FR-022)."""
        conn = self.connection()
        with self._write_guard, conn:
            conn.execute(
                "UPDATE sessions SET status = 'incomplete', last_activity_at = ? WHERE session_id = ?",
                (now_et(), session_id),
            )
        self._mirror_session(session_id)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["skills_touched"] = json.loads(record["skills_touched"] or "[]")
        record["metadata"] = json.loads(record["metadata"] or "{}")
        return record

    def _mirror_session(self, session_id: str) -> None:
        record = self.get_session(session_id)
        if record is None:
            return
        try:
            atomic_write_json(self.sessions_dir / f"{session_id}.json", record)
        except OSError as exc:
            logger.debug("session mirror write failed (non-fatal): %s", exc)

    def finalize_stale_incomplete_sessions(self) -> int:
        """Mark stale in-progress sessions incomplete (FR-022 auditability, scheduler).

        Conservative rule: a session is stale when its last activity is older
        than 24h AND it has no skill_completed event.
        """
        conn = self.connection()
        with self._write_guard:
            rows = conn.execute(
                "SELECT s.session_id FROM sessions s WHERE s.status = 'in_progress' AND"
                " (SELECT COUNT(*) FROM events e WHERE e.session_id = s.session_id"
                "  AND e.event_type = 'skill_completed') = 0"
            ).fetchall()
        finalized = 0
        for row in rows:
            sess = self.get_session(row["session_id"])
            if not sess:
                continue
            try:
                last_dt = datetime.fromisoformat(sess["last_activity_at"])
                stale = (datetime.now(ET) - last_dt).total_seconds() > 86400
            except (ValueError, TypeError):
                continue
            if stale:
                self.mark_session_incomplete(sess["session_id"])
                finalized += 1
        return finalized

    # --------------------------------------------------------------- events
    def validate_payload(self, event_type: str, payload: Any) -> Dict[str, Any]:
        """Write-time typed payload validation (FR-016 / data-model E2)."""
        schema = _EVENT_TYPES.get(event_type)
        if schema is None:
            raise ContractViolationError(f"unknown event_type: {event_type!r}")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ContractViolationError(f"payload for {event_type} must be an object")
        required = _REQUIRED_KEYS.get(event_type)
        if required:
            missing = required - set(payload.keys())
            if missing:
                raise ContractViolationError(
                    f"payload for {event_type} missing required fields: {sorted(missing)}"
                )
        for key, expected in schema.items():
            value = payload.get(key)
            if value is None:
                continue
            if isinstance(expected, tuple):
                expected = (int, float)
            if not isinstance(value, expected):
                raise ContractViolationError(
                    f"payload field {key!r} for {event_type} must be "
                    f"{getattr(expected, '__name__', expected)}"
                )
        return payload

    def record_event(
        self, event_type: str, payload: Dict[str, Any], skill: Optional[str] = None,
        session_id: Optional[str] = None, inferred: bool = False,
    ) -> str:
        """Record one typed TelemetryEvent: local-first persistence (FR-016/FR-018).

        Invalid payloads land in quarantine + raise ContractViolationError
        (loud for the caller, fail-soft for the system — agent-api.md).
        """
        try:
            self.validate_payload(event_type, payload)
        except ContractViolationError as exc:
            self.quarantine_event(event_type, payload, str(exc))
            raise
        clean_payload = sanitize_payload(payload)
        session_id = session_id or self.get_or_create_session(skill=skill, inferred=inferred)
        if skill is None and session_id:
            # attribute the event to the session's skill when omitted (FR-016
            # provenance: every event carries its skill attribution)
            existing = self.get_session(session_id)
            if existing and existing.get("skills_touched"):
                skill = sorted(existing["skills_touched"])[0] or None
        self.touch_session(session_id, skill)
        event_id = _new_id("EVT")
        ts = now_et()
        payload_hash = _canonical_hash({"event_type": event_type, "payload": clean_payload,
                                        "session_id": session_id})
        conn = self.connection()
        with self._write_guard, conn:
            dup = conn.execute(
                "SELECT event_id FROM events WHERE session_id = ? AND payload_hash = ?",
                (session_id, payload_hash),
            ).fetchone()
            if dup is not None:
                return dup["event_id"]  # idempotent re-recording (FR-050)
            conn.execute(
                "INSERT INTO events (event_id, session_id, timestamp, skill, event_type,"
                " payload, inferred, push_state, payload_hash) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (event_id, session_id, ts, skill, event_type, json.dumps(clean_payload),
                 int(inferred), payload_hash),
            )
        line_record = {
            "event_id": event_id, "session_id": session_id, "timestamp": ts,
            "skill": skill, "event_type": event_type, "payload": clean_payload,
            "inferred": inferred, "schema_version": 2,
        }
        jsonl_path = self.raw_dir / f"{today_et()}.jsonl"
        try:
            append_jsonl(jsonl_path, line_record)
        except OSError as exc:
            logger.warning("raw JSONL append failed (DB record kept): %s", exc)
        return event_id

    def quarantine_event(self, event_type: str, payload: Any, reason: str) -> str:
        """Land a write-time-invalid event in quarantine + log (fail-soft)."""
        qid = _new_id("QRN")
        record = {"quarantine_id": qid, "timestamp": now_et(), "event_type": event_type,
                  "reason": reason, "payload": sanitize_payload(payload)}
        try:
            atomic_write_json(self.quarantine_dir / f"{qid}.json", record)
        except OSError as exc:
            logger.warning("quarantine write failed: %s", exc)
        logger.warning("event quarantined: type=%s reason=%s id=%s", event_type, reason, qid)
        return qid

    def query_events(
        self, skill: Optional[str] = None, session_id: Optional[str] = None,
        event_type: Optional[str] = None, since: Optional[str] = None,
        until: Optional[str] = None, limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Structured query API (FR-020): by skill/session/type/time window."""
        sql = ["SELECT * FROM events WHERE 1=1"]
        params: List[Any] = []
        if skill:
            sql.append("AND skill = ?")
            params.append(skill)
        if session_id:
            sql.append("AND session_id = ?")
            params.append(session_id)
        if event_type:
            sql.append("AND event_type = ?")
            params.append(event_type)
        if since:
            sql.append("AND timestamp >= ?")
            params.append(since)
        if until:
            sql.append("AND timestamp <= ?")
            params.append(until)
        sql.append("ORDER BY timestamp ASC LIMIT ?")
        params.append(limit)
        rows = self.connection().execute(" ".join(sql), params).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["payload"] = json.loads(record["payload"] or "{}")
            out.append(record)
        return out

    def pending_push_events(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Events awaiting push (FR-019 queue)."""
        rows = self.connection().execute(
            "SELECT * FROM events WHERE push_state = 'pending' ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["payload"] = json.loads(record["payload"] or "{}")
            out.append(record)
        return out

    def failed_push_events(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Events that failed to push — retried on the next flush (FR-019)."""
        rows = self.connection().execute(
            "SELECT * FROM events WHERE push_state = 'failed' ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["payload"] = json.loads(record["payload"] or "{}")
            out.append(record)
        return out

    def record_push_result(self, event_ids: List[str], status: str, reason: str = "") -> int:
        """Mark events pushed/failed/skipped_disabled + record a push result (FR-019)."""
        if not event_ids:
            return 0
        conn = self.connection()
        placeholders = ",".join("?" for _ in event_ids)
        with self._write_guard, conn:
            conn.execute(
                f"UPDATE events SET push_state = ? WHERE event_id IN ({placeholders})",
                [status, *event_ids],
            )
        session_id = self._bookkeeping_session()
        self.record_event(
            "mlflow_push_result",
            {"event_count": len(event_ids), "status": status, "reason": _compact(reason)},
            session_id=session_id,
        )
        # the bookkeeping event itself follows the batch's outcome (FR-019) so
        # the push queue does not grow meta-events indefinitely
        with self._write_guard, conn:
            row = conn.execute(
                "SELECT event_id, payload_hash FROM events WHERE session_id = ?"
                " AND event_type = 'mlflow_push_result' ORDER BY timestamp DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is not None:
                conn.execute("UPDATE events SET push_state = ? WHERE event_id = ?",
                             (status, row["event_id"]))
        logger.info("MLflow push %s: %d events%s", status, len(event_ids),
                    f" ({reason})" if reason else "")
        return len(event_ids)

    def _bookkeeping_session(self) -> str:
        """A dedicated session for push-result bookkeeping events (keeps run-level
        sessions clean of meta events)."""
        conn = self.connection()
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE status = 'in_progress' ORDER BY started_at ASC LIMIT 1"
        ).fetchone()
        if row is not None:
            return row["session_id"]
        return self.get_or_create_session(skill=None, metadata={"bookkeeping": True})

    # ------------------------------------------------------------ decisions
    def record_decision(
        self, decision: str, reason: str, session_id: Optional[str] = None,
        skill: Optional[str] = None, context_summary: str = "",
    ) -> str:
        """Append a DecisionRecord (FR-017) with JSON snapshot under decisions/."""
        decision_id = _new_id("DEC")
        ts = now_et()
        conn = self.connection()
        with self._write_guard, conn:
            conn.execute(
                "INSERT INTO decisions (decision_id, session_id, timestamp, decision,"
                " reason, context_summary, skill) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (decision_id, session_id, ts, _compact(decision), _compact(reason),
                 _compact(context_summary), skill),
            )
        try:
            atomic_write_json(self.decisions_dir / f"{decision_id}.json", {
                "decision_id": decision_id, "session_id": session_id, "timestamp": ts,
                "decision": _compact(decision), "reason": _compact(reason),
                "context_summary": _compact(context_summary), "skill": skill,
            })
        except OSError as exc:
            logger.warning("decision snapshot write failed (DB record kept): %s", exc)
        return decision_id

    # -------------------------------------------------------------- rebuild
    def rebuild_from_jsonl(self) -> Dict[str, int]:
        """Drop/recreate telemetry tables and reparse raw streams (SC-002)."""
        conn = self.connection()
        with self._write_guard, conn:
            for table in ("events", "sessions"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.executescript(_DDL)
        counts = {"events": 0, "sessions": 0}
        if not self.raw_dir.exists():
            return counts
        for jsonl_file in sorted(self.raw_dir.glob("*.jsonl")):
            with open(jsonl_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        self.quarantine_event("unparseable_jsonl", line, "json_decode_error")
                        continue
                    self._restore_event(record, counts)
        return counts

    def _restore_event(self, record: Dict[str, Any], counts: Dict[str, int]) -> None:
        event_id = record.get("event_id") or _new_id("EVT")
        session_id = record.get("session_id")
        if not session_id:
            return
        conn = self.connection()
        ts = record.get("timestamp", now_et())
        with self._write_guard, conn:
            exists = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO sessions (session_id, started_at, last_activity_at,"
                    " skills_touched, status, inferred, metadata) VALUES"
                    " (?, ?, ?, '[]', 'incomplete', ?, '{}')",
                    (session_id, ts, ts, int(bool(record.get("inferred")))),
                )
                counts["sessions"] += 1
            payload_hash = _canonical_hash(
                {"event_type": record.get("event_type"), "payload": record.get("payload"),
                 "session_id": session_id}
            )
            conn.execute(
                "INSERT OR IGNORE INTO events (event_id, session_id, timestamp, skill,"
                " event_type, payload, inferred, push_state, payload_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'pushed', ?)",
                (event_id, session_id, ts, record.get("skill"),
                 record.get("event_type", "failure_event"),
                 json.dumps(record.get("payload") or {}), int(bool(record.get("inferred"))),
                 payload_hash),
            )
        counts["events"] += 1


class _SessionHandle:
    def __init__(self, store: "TelemetryStore", session_id: str, skill: Optional[str],
                 started: datetime):
        self._store = store
        self.session_id = session_id
        self._skill = skill
        self._started = started

    def elapsed_ms(self) -> int:
        return int((datetime.now(ET) - self._started).total_seconds() * 1000)

    def record(self, event_type: str, payload: Dict[str, Any]) -> str:
        return self._store.record_event(event_type, payload, skill=self._skill,
                                        session_id=self.session_id)

    def record_decision(self, decision: str, reason: str, context_summary: str = "") -> str:
        return self._store.record_decision(decision, reason, session_id=self.session_id,
                                           skill=self._skill, context_summary=context_summary)


@contextmanager
def session_scope(store: TelemetryStore, skill: Optional[str] = None) -> Iterator[_SessionHandle]:
    """Allocate/resume a session, record skill_started, complete on clean exit (T010).

    On exception: outcome=failure recorded, session left in_progress (caller or
    scheduler finalize pass applies the conservative FR-022 incomplete rule).
    """
    session_id = store.get_or_create_session(skill=skill)
    store.record_event("skill_started", {"skill": skill or "session"}, skill=skill,
                       session_id=session_id)
    handle = _SessionHandle(store, session_id, skill, datetime.now(ET))
    try:
        yield handle
    except Exception as exc:
        store.record_event(
            "skill_completed",
            {"skill": skill or "session", "outcome": "failure",
             "duration_ms": handle.elapsed_ms()},
            skill=skill, session_id=session_id,
        )
        logger.warning("session %s failed: %s", session_id, exc)
        raise
    store.record_event(
        "skill_completed",
        {"skill": skill or "session", "outcome": "success",
         "duration_ms": handle.elapsed_ms()},
        skill=skill, session_id=session_id,
    )
    store.complete_session(session_id)


def default_store(root: Optional[Path] = None) -> TelemetryStore:
    """A store bound to the configured (or explicit) state root, schema initialized."""
    from src.skillbox.config import ensure_state_root, state_root

    root = Path(root) if root is not None else state_root()
    ensure_state_root(root)
    store = TelemetryStore(root)
    store.init_db()
    return store


def infer_skill_activity(store: TelemetryStore, repo_root: Optional[Path] = None,
                         window_hours: int = 2) -> int:
    """R3 pathway 2: reconcile repo activity into inferred events (conservative).

    A skill run is inferred when a speckit artifact changed within the window
    and no explicit session exists for the same minute bucket (explicit
    instrumentation wins — R3). Sessions never get fabricated completions:
    inferred events attach to sessions that stay incomplete (FR-022). Returns
    the number of inferred events recorded.
    """
    from datetime import timedelta

    repo_root = Path(repo_root) if repo_root is not None else Path(".")
    inferred = 0
    cutoff = datetime.now(ET) - timedelta(hours=window_hours)
    specs_dir = repo_root / "specs"
    if not specs_dir.exists():
        return 0
    explicit_minutes = set()
    for event in store.query_events(since=cutoff.isoformat(), limit=1000):
        if event.get("inferred"):
            continue
        session = store.get_session(event["session_id"])
        if session:
            try:
                started = datetime.fromisoformat(session["started_at"])
                explicit_minutes.add(started.strftime("%Y%m%d%H%M"))
            except (ValueError, TypeError):
                continue
    for artifact in sorted(specs_dir.glob("*/tasks.md")):
        mtime = datetime.fromtimestamp(artifact.stat().st_mtime, tz=ET)
        if mtime < cutoff:
            continue
        bucket = mtime.strftime("%Y%m%d%H%M")
        if bucket in explicit_minutes:
            continue  # explicit instrumentation wins (R3)
        store.record_event("skill_completed", {
            "skill": "speckit-tasks", "outcome": "inferred", "duration_ms": 0,
        }, skill="speckit-tasks", inferred=True)
        inferred += 1
    return inferred
