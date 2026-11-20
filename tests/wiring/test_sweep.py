"""US4 T026: sweep behavioral tests (idempotence, malformed skip, retention, no-op)."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from src.skillbox.sweep import sweep_rounds  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    drop = tmp_path / "logs" / "egm_rounds"
    drop.mkdir(parents=True)
    state = tmp_path / "wiring" / "sweep_state.json"
    recorded = []

    class FakeStore:
        def record_event(self, event_type, skill=None, payload=None, **kw):
            recorded.append({"event_type": event_type, "payload": payload})

    return {"drop": drop, "state": state, "recorded": recorded, "store": FakeStore()}


def _summary(name="round-20261110T150000-0500-TASKS.json", **overrides):
    s = {
        "round_id": "20261110T150000-0500-after_tasks-TASKS",
        "profile": "TASKS",
        "target_file": "specs/005-ai-sdlc-wiring/tasks.md",
        "reviewers": ["implementation_readiness", "structural", "codebase_details"],
        "findings_count": 2,
        "round_kind": "loop",
        "recorded_at": "2026-11-10T15:00:00-05:00",
        "consult": {"mode": "digest", "freshness": "fresh", "fallback": False},
    }
    s.update(overrides)
    return name, json.dumps(s)


def test_first_sweep_records_one_event(env):
    name, blob = _summary()
    (env["drop"] / name).write_text(blob, encoding="utf-8")
    r = sweep_rounds(env["drop"], env["store"], env["state"])
    assert r["recorded"] == 1
    assert len(env["recorded"]) == 1
    assert env["recorded"][0]["event_type"] == "egm_review_round"


def test_double_sweep_idempotent(env):
    name, blob = _summary()
    f = env["drop"] / name
    f.write_text(blob, encoding="utf-8")
    sweep_rounds(env["drop"], env["store"], env["state"])
    sweep_rounds(env["drop"], env["store"], env["state"])
    assert len(env["recorded"]) == 1


def test_never_re_records_within_retention_window(env):
    name, blob = _summary()
    (env["drop"] / name).write_text(blob, encoding="utf-8")
    for _ in range(3):
        sweep_rounds(env["drop"], env["store"], env["state"])
    assert len(env["recorded"]) == 1


def test_malformed_skipped_with_reason(env):
    (env["drop"] / "round-bad.json").write_text("{not json", encoding="utf-8")
    r = sweep_rounds(env["drop"], env["store"], env["state"])
    assert r["recorded"] == 0
    assert r["skipped"] and "reason" in r["skipped"][0]
    assert (env["drop"] / "round-bad.json").exists()  # left in place


def test_retention_cleanup(env):
    name, blob = _summary()
    f = env["drop"] / name
    f.write_text(blob, encoding="utf-8")
    # age the file past 30 days
    import os

    old_ts = time.time() - (31 * 86400)
    os.utime(f, (old_ts, old_ts))
    r = sweep_rounds(env["drop"], env["store"], env["state"])
    assert r["recorded"] == 1
    assert r["cleaned"] >= 1
    assert not f.exists()


def test_seen_state_pruned_to_retention_window(env):
    """F12: sweep state entries older than the retention window are pruned from
    sweep_state.json (bounded dedup state); a file whose hash was pruned can be
    re-swept after the window."""
    name, blob = _summary()
    (env["drop"] / name).write_text(blob, encoding="utf-8")
    r1 = sweep_rounds(env["drop"], env["store"], env["state"])
    assert r1["recorded"] == 1
    from src.skillbox.sweep import _sha
    import json as _json
    sha = _sha(blob)
    state = _json.loads(env["state"].read_text(encoding="utf-8"))
    assert sha in state["entries"]
    # age the swept_at past 30 days
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc) - timedelta(days=31)
    state["entries"][sha]["swept_at"] = old.isoformat()
    env["state"].write_text(_json.dumps(state), encoding="utf-8")
    r2 = sweep_rounds(env["drop"], env["store"], env["state"])
    assert r2["pruned"] >= 1
    state2 = _json.loads(env["state"].read_text(encoding="utf-8"))
    # pruned hash => the still-present file was eligible and re-recorded
    assert r2["recorded"] == 1
    assert sha in state2["entries"]
    assert len(env["recorded"]) == 2


def test_per_file_skip_reasons_returned(env):
    """F12: the sweep result carries per-file name + reason (not just a count)."""
    (env["drop"] / "round-bad1.json").write_text("{not json", encoding="utf-8")
    (env["drop"] / "round-bad2.json").write_text(json.dumps({"profile": "X"}), encoding="utf-8")
    r = sweep_rounds(env["drop"], env["store"], env["state"])
    files = {s["file"] for s in r["skipped"]}
    assert files == {"round-bad1.json", "round-bad2.json"}
    reasons = [s["reason"] for s in r["skipped"]]
    assert any("Expecting" in x or "JSON" in x for x in reasons)
    assert any("round_id" in x for x in reasons)


def test_disabled_no_op(env, monkeypatch):
    # simulate flag-off by pointing at a nonexistent dir
    r = sweep_rounds(env["drop"] / "nonexistent", env["store"], env["state"])
    assert r["recorded"] == 0


def test_summary_schema_validated(env):
    _, blob = _summary(round_id=None)
    name = "round-no-id.json"
    (env["drop"] / name).write_text(json.dumps(json.loads(blob)) if False else blob, encoding="utf-8")
    # round_id=None in the summary => valid JSON but missing required key
    bad = json.loads(blob)
    bad.pop("round_id")
    (env["drop"] / name).write_text(json.dumps(bad), encoding="utf-8")
    r = sweep_rounds(env["drop"], env["store"], env["state"])
    assert r["recorded"] == 0
    assert r["skipped"] and "round_id" in r["skipped"][0]["reason"]
