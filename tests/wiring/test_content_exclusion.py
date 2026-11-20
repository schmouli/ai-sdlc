"""US4 T027: content-exclusion regression (SC-011).

Walks ledger rows, round summaries, event payloads, and the rendered digest
against the schema field whitelist â€” asserts zero source-code content,
credentials, tokens, or security-sensitive configuration values in any
persisted wiring artifact. Per-file fixture conventions.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST = REPO_ROOT / ".specify" / "wiring-manifest.yml"


DENY_PATTERNS = [
    re.compile(r"password|passwd|secret[=:]|api[_-]?key|authorization|bearer\s+", re.I),
    re.compile(r"token[=:]|access_token|refresh_token|private_key|credential", re.I),
    re.compile(r"def \w+\(|class \w+ |from \w+ import", re.M),
    re.compile(r"SELECT .* FROM |INSERT INTO ", re.I),
]


def _assert_clean(name: str, obj):
    text = json.dumps(obj, sort_keys=True, default=str)
    for pat in DENY_PATTERNS:
        assert not pat.search(text), f"{name}: deny pattern {pat.pattern!r} matched: {text[:120]}"


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    if str(REPO_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
    mod = importlib.import_module("record_hook_acceptance")

    root = tmp_path / "repo"
    root.mkdir()
    state_root = root / ".skillbox"
    sb_spec = root / ".specify"
    sb_spec.mkdir()
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    (sb_spec / "wiring-manifest.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    monkeypatch.setattr(mod, "state_root", lambda: state_root)
    monkeypatch.setattr(mod, "capture_enabled", lambda: True)
    recorded = []

    def fake_record(event_type, payload):
        recorded.append({"event_type": event_type, "payload": payload})

    monkeypatch.setattr(mod, "record_event", fake_record)
    return {"recorded": recorded, "state_root": state_root, "mod": mod}


def test_recorder_event_payloads_clean(sandbox):
    sandbox["mod"].main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
        "--operator", "schmo",
    ])
    for r_item in sandbox["recorded"]:
        _assert_clean(f"event {r_item['event_type']}", r_item["payload"])


def test_ledger_entries_clean(sandbox):
    sandbox["mod"].main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_tasks", "--feature-dir", "specs/x",
    ])
    ledger = json.loads((sandbox["state_root"] / "wiring" / "hook_ledger.json").read_text(encoding="utf-8"))
    for entry in ledger.get("entries", []):
        _assert_clean("ledger entry", entry)


def test_sweep_event_payload_clean(tmp_path):
    from src.skillbox.sweep import sweep_rounds

    drop = tmp_path / "logs" / "egm_rounds"
    drop.mkdir(parents=True)
    summary = {
        "round_id": "r1", "profile": "SPEC", "target_file": "specs/x/spec.md",
        "reviewers": ["zoom_out", "comprehension"], "findings_count": 1,
        "round_kind": "review", "recorded_at": "2026-11-11T00:00:00-05:00",
        "consult": {"mode": "digest", "freshness": "fresh", "fallback": False},
    }
    (drop / "round-1.json").write_text(json.dumps(summary), encoding="utf-8")
    recorded = []

    class FakeStore:
        def record_event(self, event_type, skill=None, payload=None, **kw):
            recorded.append(payload)

    sweep_rounds(drop, FakeStore(), tmp_path / "sweep_state.json")
    for payload in recorded:
        _assert_clean("swept payload", payload)


def test_digest_content_clean():
    from src.kg.digest import render_digest

    synth = {
        "commit_sha": "abc1234", "built_at": "2026-11-10T15:00:00-05:00",
        "files": {
            "src/a.py": {"pagerank": 0.9, "imports": ["src/b.py"]},
            "src/b.py": {"pagerank": 0.5, "imports": []},
        },
        "coedit_pairs": [{"a": "src/a.py", "b": "src/b.py", "weight": 3}],
    }
    out = render_digest(synth, {"digest_max_bytes": 8192})
    for pat in DENY_PATTERNS:
        assert not pat.search(out), f"digest matches deny pattern {pat.pattern!r}"


def test_live_digest_content_clean():
    """The live rendered digest (if present) carries no forbidden content."""
    digest_path = REPO_ROOT / "output" / "kg" / "digest_for_reviews.md"
    if not digest_path.exists():
        pytest.skip("digest not yet rendered")
    text = digest_path.read_text(encoding="utf-8")
    for pat in DENY_PATTERNS:
        assert not pat.search(text), f"live digest matches deny pattern {pat.pattern!r}"


# ---------------------------------------------------------------- feature-008: series tracking + trading-loop isolation


def test_budget_series_tracked():
    """feature-008 (i): data/context_budget_series.jsonl must be version-control tracked.

    Deliberate fail-soft: when the file does not exist yet (pre-A.6), skip â€” the
    assertion becomes binding the moment the generator creates it (step A.6).
    """
    import subprocess as sp
    series = REPO_ROOT / "data" / "context_budget_series.jsonl"
    if not series.exists():
        pytest.skip("series not yet created (lands at feature-008 step A.6)")
    r = sp.run(["git", "check-ignore", "-q", str(series.relative_to(REPO_ROOT))],
               cwd=str(REPO_ROOT), capture_output=True)
    assert r.returncode != 0, "series file is excluded by version control â€” trend data lost"


def test_trading_loop_import_isolation():
    """feature-008 (ii): no module path this feature touches may be imported by the
    trading loop (constitution II): src/trading, src/communication, listener entrypoints."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    touched = ["generate_sdlc_pane", "verify_wiring", "wiring_manifest", "context_budget_series"]
    scan_roots = [
        REPO_ROOT / "src" / "trading",
        REPO_ROOT / "src" / "communication",
        REPO_ROOT / "scripts" / "start_ea_signal_listener.py",
        REPO_ROOT / "scripts" / "run_autotrader.py",
    ]
    pat = re.compile(
        r"^\s*(?:import\s+(" + "|".join(touched) + r")"
        r"|from\s+(" + "|".join(touched) + r")\s+import)", re.M)
    for root in scan_roots:
        if not root.exists(): pytest.skip(f"trading path absent in standalone checkout: {root}")
        if root.is_file():
            files = [root]
        else:
            files = list(root.rglob("*.py"))
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            m = pat.search(text)
            assert not m, f"{f}: trading path imports a feature-008 module ({m.group(0)!r})"
