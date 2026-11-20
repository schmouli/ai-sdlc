"""Tests for SkillBox MLflow sink (US1: push isolation — contracts/mlflow-push.md)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.skillbox.mlflow_sink import MlflowSink


@pytest.fixture()
def temp_mlflow_uri(tmp_path):
    return f"sqlite:///{(tmp_path / 'skillbox_mlflow.db').as_posix()}"


@pytest.fixture()
def store():
    from src.skillbox.telemetry import default_store

    root = Path(tempfile.mkdtemp(prefix="skillbox-push-"))
    store = default_store(root)
    yield store
    store.close()


def _experiment_names(uri: str) -> set:
    db_path = uri.replace("sqlite:///", "")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM experiments").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


class TestPushIsolation:
    def test_events_land_in_skillbox_experiment_only(self, store, temp_mlflow_uri, monkeypatch):
        monkeypatch.setenv("MT4_SKILLBOX_MLFLOW_TRACKING_URI", temp_mlflow_uri)
        monkeypatch.setenv("MT4_SKILLBOX_MLFLOW_ENABLED", "1")
        sid = store.get_or_create_session(skill="speckit-plan")
        store.record_event("planning_output", {"requirements_count": 3}, session_id=sid)
        sink = MlflowSink({"enabled": True, "tracking_uri": temp_mlflow_uri,
                           "batch_interval_seconds": 0})
        pushed = sink.flush_queue(store, force=True)
        assert pushed == 1
        names = _experiment_names(temp_mlflow_uri)
        assert any(n.startswith("skillbox-") for n in names)
        assert "mt4-self-learning" not in names  # trading experiment untouched (FR-047)

    def test_disabled_mode_zero_pushes_local_intact(self, store):
        sid = store.get_or_create_session(skill="speckit-plan")
        store.record_event("validation_result", {"tests_passed": 1, "tests_failed": 0},
                           session_id=sid)
        sink = MlflowSink({"enabled": False})
        pushed = sink.flush_queue(store, force=True)
        assert pushed == 0
        pending = store.pending_push_events()
        assert len(pending) == 0  # marked skipped_disabled, not still-pending
        events = store.query_events(session_id=sid)
        assert events and events[0]["push_state"] == "skipped_disabled"

    def test_push_failure_marks_failed_and_retries(self, store, temp_mlflow_uri, monkeypatch):
        monkeypatch.setenv("MT4_SKILLBOX_MLFLOW_TRACKING_URI", temp_mlflow_uri)
        sid = store.get_or_create_session(skill="speckit-plan")
        store.record_event("failure_event", {"failure_category": "test", "failure_detail": "x"},
                           session_id=sid)
        sink = MlflowSink({"enabled": True, "tracking_uri": temp_mlflow_uri,
                           "batch_interval_seconds": 0})
        # poison the runtime to force a failure path without raising
        sink._setup_ok = True
        sink._mlflow = None
        pushed = sink.flush_queue(store, force=True)
        assert pushed == 0
        failed = [e for e in store.query_events(session_id=sid)
                  if e["push_state"] == "failed"]
        assert failed, "failure must be recorded with push_state=failed"
        # heal + retry
        import mlflow

        mlflow.set_tracking_uri(temp_mlflow_uri)
        sink._mlflow = mlflow
        pushed = sink.flush_queue(store, force=True)
        assert pushed >= 1

    def test_import_failure_disables_sink_fail_soft(self, store, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "mlflow", None)  # force import failure
        sink = MlflowSink({"enabled": True, "tracking_uri": "sqlite:///:memory:"})
        sid = store.get_or_create_session(skill="speckit-plan")
        store.record_event("validation_result", {"tests_passed": 2, "tests_failed": 0},
                           session_id=sid)
        pushed = sink.flush_queue(store, force=True)
        assert pushed == 0  # never raises to caller (fail-soft)
        assert sink._setup_ok is False
