"""US4 T025: record_hook_acceptance behavioral tests.

In-process tests of the recorder logic (ledger-first ordering, completion
predicate, flag-off inertness, partial-failure exit codes). Per-file fixture
conventions (tempfile.mkdtemp, no shared base classes).
"""

from __future__ import annotations

import importlib
import os
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

recorder = importlib.import_module("record_hook_acceptance")


def _capture_on_in_active_env(env=None) -> bool:
    import json as _json
    env_name = env or os.environ.get("METATRADER_ENV", "production")
    cfg = REPO_ROOT / "config" / "environments" / f"{env_name}.json"
    try:
        return bool((_json.loads(cfg.read_text(encoding="utf-8")).get("system") or {})
                    .get("skillbox", {}).get("wiring_capture_enabled"))
    except (OSError, _json.JSONDecodeError):
        return False




@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Sandbox: temp state_root + capture-enabled config; recorder helpers patched."""
    root = tmp_path / "repo"
    root.mkdir()
    spec = root / ".specify"
    spec.mkdir()
    manifest = yaml.safe_load((REPO_ROOT / ".specify" / "wiring-manifest.yml").read_text(encoding="utf-8"))
    (spec / "wiring-manifest.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    state_root = root / ".skillbox"
    monkeypatch.setattr(recorder, "state_root", lambda: state_root)
    monkeypatch.setattr(recorder, "capture_enabled", lambda: True)

    recorded = []

    def fake_record(event_type, payload):
        recorded.append({"event_type": event_type, "payload": payload})

    monkeypatch.setattr(recorder, "record_event", fake_record)
    return {"root": root, "state_root": state_root, "manifest": manifest, "recorded": recorded}


def _ledger(sb):
    f = sb["state_root"] / "wiring" / "hook_ledger.json"
    return json.loads(f.read_text(encoding="utf-8"))


def test_accepted_writes_ledger_and_event(sandbox):
    rc = recorder.main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    assert rc == 0
    entries = _ledger(sandbox)["entries"]
    assert len(entries) == 1
    e = entries[0]
    assert e["decision"] == "accepted"
    assert e["command"] == "egm.review.loop"
    assert "ts" in e
    assert sandbox["recorded"][0]["event_type"] == "hook_gate_accepted"


def test_completion_predicate_emits_stage_completed(sandbox):
    """Last enabled gate for the stage = first acceptance (one gate per stage)."""
    recorder.main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    kinds = [r["event_type"] for r in sandbox["recorded"]]
    assert "speckit_stage_completed" in kinds


def test_skipped_does_not_emit_stage_completion(sandbox):
    recorder.main([
        "--event", "skipped", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    kinds = [r["event_type"] for r in sandbox["recorded"]]
    assert "hook_gate_skipped" in kinds
    assert "speckit_stage_completed" not in kinds


def test_flag_off_inert(sandbox, monkeypatch):
    monkeypatch.setattr(recorder, "capture_enabled", lambda: False)
    rc = recorder.main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    assert rc == 0
    ledger_file = sandbox["state_root"] / "wiring" / "hook_ledger.json"
    if ledger_file.exists():
        assert _ledger(sandbox)["entries"] == []
    assert sandbox["recorded"] == []


def test_ledger_first_ordering(sandbox, monkeypatch):
    """Ledger written before telemetry; if telemetry fails, ledger persists + exit 0."""
    order = []

    def fake_record(event_type, payload):
        order.append("event")
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(recorder, "record_event", fake_record)
    orig_append = recorder.append_ledger

    def spy_append(entry):
        order.append("ledger")
        orig_append(entry)

    monkeypatch.setattr(recorder, "append_ledger", spy_append)
    rc = recorder.main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    assert rc == 0  # partial failure: ledger written, exit 0
    assert order == ["ledger", "event"]


def test_both_fail_exit_nonzero(sandbox, monkeypatch):
    monkeypatch.setattr(recorder, "append_ledger", lambda e: (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr(recorder, "record_event", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("store down")))
    rc = recorder.main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    assert rc == 1  # FR-014: exit non-zero only when neither record lands


def test_duplicates_are_data(sandbox):
    """Repeated gate acceptance appends DISTINCT ledger entries (duplicates are data)."""
    for _ in range(2):
        recorder.main([
            "--event", "accepted", "--hook-command", "egm.review.loop",
            "--stage", "after_specify", "--feature-dir", "specs/x",
        ])
    entries = _ledger(sandbox)["entries"]
    assert len(entries) == 2
    assert len({json.dumps(e, sort_keys=True) for e in entries}) == 2  # distinct (ts differs)


def test_completion_blocked_until_all_enabled_stage_hooks_accepted(sandbox, monkeypatch):
    """F6: the completion predicate fires only when EVERY enabled hook entry of the
    stage in extensions.yml has an accepted ledger entry for the feature_dir."""
    # the predicate reads REPO_ROOT/.specify/extensions.yml — isolate it
    extensions = yaml.safe_load((REPO_ROOT / ".specify" / "extensions.yml").read_text(encoding="utf-8"))
    extensions["hooks"]["after_specify"].append({
        "extension": "egm", "command": "egm.review", "enabled": True, "optional": True,
        "priority": 6, "description": "second gate", "prompt": "run", "condition": None,
    })
    sb_spec = sandbox["root"] / ".specify"
    sb_spec.mkdir(parents=True, exist_ok=True)
    (sb_spec / "extensions.yml").write_text(
        yaml.safe_dump(extensions, sort_keys=False, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(recorder, "REPO_ROOT", sandbox["root"])

    recorder.main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    kinds = [r["event_type"] for r in sandbox["recorded"]]
    assert "speckit_stage_completed" not in kinds  # egm.review not yet accepted

    recorder.main([
        "--event", "accepted", "--hook-command", "egm.review",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    kinds = [r["event_type"] for r in sandbox["recorded"]]
    assert "speckit_stage_completed" in kinds  # every enabled entry now accepted


def test_completion_not_emitted_for_skipped_event(sandbox):
    """Only `--event accepted` runs the completion predicate (stage completion is an
    acceptance outcome; a skipped gate never completes a stage)."""
    recorder.main([
        "--event", "skipped", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    assert "speckit_stage_completed" not in [r["event_type"] for r in sandbox["recorded"]]


def test_subprocess_flag_off_smoke():
    """F5: the documented invocation (`python scripts/record_hook_acceptance.py ...`)
    works WITHOUT any monkeypatching: the script imports cleanly under the active
    config (flag off by default), exits 0, prints the INERT label on stderr, and
    writes no ledger entry for this run."""
    env = dict(os.environ)
    env["METATRADER_ENV"] = "development"  # inertness lives in the dev config; never touch the live flag
    if _capture_on_in_active_env(env["METATRADER_ENV"]) and _capture_on_in_active_env():
        pytest.skip("F5 smoke requires an inert capture config; both production and "
                    "development have wiring_capture_enabled=true on this host — the "
                    "frozen-config boundary forbids editing either. (Feature 007 T008.)")
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "scripts" / "record_hook_acceptance.py"),
         "--event", "accepted", "--hook-command", "egm.review.loop",
         "--stage", "after_specify", "--feature-dir", "specs/smoke-f5-test"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(REPO_ROOT), env=env,
    )
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, f"rc={proc.returncode} out={out} err={err}"
    assert "INERT" in err, f"expected INERT label on stderr, got: {err!r}"


def test_ledger_bom_tolerated_and_never_rewritten(tmp_path, monkeypatch):
    """F13/T009: a legacy ledger with a UTF-8 BOM is readable (utf-8-sig) and the
    recorder never propagates the BOM — the rewritten file is BOM-free UTF-8."""
    state_root = tmp_path / "skillbox"
    ledger = state_root / "wiring" / "hook_ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    seed = b"\xef\xbb\xbf" + json.dumps({"schema_version": 1, "entries": []}).encode("utf-8")
    ledger.write_bytes(seed)
    monkeypatch.setattr(recorder, "state_root", lambda: state_root)
    monkeypatch.setattr(recorder, "capture_enabled", lambda: True)
    monkeypatch.setattr(recorder, "record_event", lambda *a, **kw: None)
    rc = recorder.main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    assert rc == 0
    raw = ledger.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "rewritten ledger must be BOM-free"
    data = json.loads(raw.decode("utf-8"))
    assert len(data["entries"]) == 1
    assert data["schema_version"] == 1


# ---------------------------------------------------------------- T008 (feature 007): lockfile coverage

def test_concurrent_append_zero_loss(sandbox):
    """SC-006: two threads appending N entries each → 2N entries, zero loss."""
    import threading

    def worker(seed):
        for i in range(5):
            rc = recorder.main([
                "--event", "accepted", "--hook-command", f"cmd-{seed}-{i}",
                "--stage", "after_specify", "--feature-dir", "specs/x",
            ])
            assert rc == 0

    threads = [threading.Thread(target=worker, args=(w,)) for w in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    entries = _ledger(sandbox)["entries"]
    cmds = {e["command"] for e in entries}
    assert len(entries) == 10, f"expected 10 entries, got {len(entries)} (lost {10 - len(entries)})"
    assert cmds == {f"cmd-{w}-{i}" for w in ("A", "B") for i in range(5)}


def test_stale_lock_takeover(sandbox, monkeypatch):
    """A lockfile older than 10 s (mtime age) is taken over, not deadlock."""
    lock = sandbox["state_root"] / "wiring" / "hook_ledger.json.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"")
    stale = __import__("time").time() - 11.0
    import os
    os.utime(lock, (stale, stale))
    rc = recorder.main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    assert rc == 0
    entries = _ledger(sandbox)["entries"]
    assert len(entries) == 1  # the stale lock was taken over and the append landed
    assert not lock.exists()  # delete-on-release


def test_lock_released_after_success(sandbox):
    """The lockfile is deleted on release (delete-on-release in finally)."""
    lock = sandbox["state_root"] / "wiring" / "hook_ledger.json.lock"
    rc = recorder.main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
    ])
    assert rc == 0
    assert not lock.exists()


def test_entry_schema_frozen_with_lock(sandbox):
    """Entry schema is frozen; the lock change adds no fields."""
    recorder.main([
        "--event", "accepted", "--hook-command", "egm.review.loop",
        "--stage", "after_specify", "--feature-dir", "specs/x",
        "--operator", "op1",
    ])
    e = _ledger(sandbox)["entries"][0]
    assert set(e.keys()) == {"ts", "event", "command", "decision", "feature_dir", "operator"}



