"""Hook-acceptance recorder (feature 005 US4, T033).

The single documented recording command for the telemetry-capture contract.
Ledger-first in-process write; the ledger is the ledger-of-record; the telemetry
event is the lossy copy; partial failure is logged and surfaced; exit non-zero
only when neither record lands. Flag-off = inert (exit 0, labeled).

Environment resolution (FR-018): `METATRADER_ENV` (default `production`) selects
`config/environments/{env}.json`, the same precedence scripts/verify_wiring.py
uses — the flag's home is config, and the ledger path ALWAYS resolves from the
configured SkillBox state_root.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _active_env() -> str:
    return os.environ.get("METATRADER_ENV", "production")


def _read_env_config(env: str) -> dict:
    cfg_file = REPO_ROOT / "config" / "environments" / f"{env}.json"
    if not cfg_file.exists():
        return {}
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def state_root() -> Path:
    """SkillBox state root from the active environment config (repo/.skillbox default)."""
    sr = (_read_env_config(_active_env()).get("system") or {}).get("skillbox", {}).get("state_root")
    if sr:
        p = Path(sr)
        return p if p.is_absolute() else (REPO_ROOT / p)
    return REPO_ROOT / ".skillbox"


def capture_enabled() -> bool:
    """`system.skillbox.wiring_capture_enabled` from the active env config; fail-soft False."""
    try:
        from src.skillbox.config import get_skillbox_config
        return bool(get_skillbox_config().get("wiring_capture_enabled"))
    except Exception:
        val = (_read_env_config(_active_env()).get("system") or {}).get("skillbox", {}).get("wiring_capture_enabled")
        return bool(val) if isinstance(val, bool) else False


def _lock_acquire(lock: Path, retries: int = 3, backoff: float = 0.05,
                  stale_age: float = 10.0):
    """O_CREAT|O_EXCL lockfile acquire; 3 retries / ~50 ms backoff; stale takeover by
    mtime age after 10 s; returns the fd, or None on exhausted retries (the caller
    raises — the recorder's existing partial-failure path logs and exits per the
    pinned exit contract: non-zero only when neither record lands)."""
    import time
    lock.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries):
        try:
            return os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as e:
            if e.errno == errno.EEXIST:
                try:
                    if time.time() - lock.stat().st_mtime > stale_age:  # stale takeover
                        lock.unlink()
                        continue
                except OSError:
                    pass
            time.sleep(backoff * (attempt + 1))
    return None


def append_ledger(entry: dict) -> None:
    """Ledger-first whole-file read-modify-write under the lockfile (feature 007
    T009 / Contract 3): O_CREAT|O_EXCL acquire w/ retries + stale takeover; the
    write adopts the repo's temp-rename atomic pattern
    (src/skillbox/telemetry.py::atomic_write_json); delete-on-release in finally.
    Exhausted retries raise into the caller — main()'s existing partial-failure
    path logs the loss to stderr and exits 0 when the event lands. CLI, entry
    schema (frozen), and event emission untouched."""
    from src.skillbox.telemetry import atomic_write_json

    ledger = state_root() / "wiring" / "hook_ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock = ledger.parent / (ledger.name + ".lock")
    fd = _lock_acquire(lock)
    if fd is None:
        raise RuntimeError(f"ledger lock unavailable after retries: {lock}")
    try:
        if ledger.exists():
            # utf-8-sig strips a BOM if one is present (no-op otherwise); writes are
            # always BOM-free UTF-8.
            data = json.loads(ledger.read_text(encoding="utf-8-sig"))
        else:
            data = {"schema_version": 1, "entries": []}
        data["entries"].append(entry)
        atomic_write_json(ledger, data)
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except OSError:
            pass  # delete-on-release; a vanished lock is fine


def record_event(event_type: str, payload: dict) -> None:
    from src.skillbox.telemetry import default_store
    store = default_store(state_root())
    try:
        store.record_event(event_type=event_type, payload=payload, skill=None)
    finally:
        store.close()


STAGES = ["after_specify", "after_plan", "after_tasks", "after_analyze",
          "after_implement", "after_converge"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Record a hook-gate decision (feature 005 US4)")
    parser.add_argument("--event", required=True, choices=["accepted", "skipped"])
    parser.add_argument("--hook-command", required=True)
    parser.add_argument("--stage", required=True, choices=STAGES + ["before_specify", "after_clarify"])
    parser.add_argument("--feature-dir", required=True)
    parser.add_argument("--operator", default=None)
    args = parser.parse_args(argv)

    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "ts": ts,
        "event": args.stage,
        "command": args.hook_command,
        "decision": args.event,
        "feature_dir": args.feature_dir,
    }
    if args.operator:
        entry["operator"] = args.operator

    # flag-off: inert (no ledger writes, no events, exit 0 with inert label)
    if not capture_enabled():
        print("INERT: wiring_capture_enabled is false - no ledger writes, no events", file=sys.stderr)
        return 0

    ledger_ok = False
    event_ok = False
    try:
        append_ledger(entry)
        ledger_ok = True
    except Exception as exc:
        print(f"ledger write failed: {exc}", file=sys.stderr)

    if ledger_ok:
        try:
            event_type = "hook_gate_accepted" if args.event == "accepted" else "hook_gate_skipped"
            payload = {
                "hook_event": args.stage,
                "hook_command": args.hook_command,
                "feature_dir": args.feature_dir,
            }
            record_event(event_type, payload)
            event_ok = True
            # stage completion: emit iff every hook entry of this stage currently
            # enabled in extensions.yml has a prior accepted ledger entry for this
            # feature_dir, including this one (capture-contract completion predicate).
            if args.event == "accepted":
                completion_done = _check_stage_completion(args.stage, args.hook_command, args.feature_dir)
                if completion_done:
                    record_event("speckit_stage_completed", {
                        "stage": args.stage, "feature_dir": args.feature_dir,
                        "decision": "accepted", "recorded_by": "record_hook_acceptance",
                        **({"operator": args.operator} if args.operator else {}),
                    })
        except Exception as exc:
            print(f"event record failed: {exc}", file=sys.stderr)

    if ledger_ok and event_ok:
        return 0
    if ledger_ok:
        print("partial failure: ledger written but event not recorded (partial-loss advisory will surface)", file=sys.stderr)
        return 0
    if event_ok:
        print("partial failure: event recorded but ledger not written", file=sys.stderr)
        return 0
    print("total failure: neither record landed", file=sys.stderr)
    return 1


def _check_stage_completion(stage: str, command: str, feature_dir: str) -> bool:
    """Completion predicate (capture-contract): `speckit_stage_completed` fires iff
    every hook entry of `stage` currently enabled in `.specify/extensions.yml` has
    a prior accepted ledger entry for this feature_dir (including this recorded one)."""
    import yaml
    try:
        extensions = yaml.safe_load((REPO_ROOT / ".specify" / "extensions.yml").read_text(encoding="utf-8"))
    except Exception:
        # fail-soft: unreadable hook config cannot prove completion
        return False
    # Find all hook entries for this stage that are enabled
    enabled_hooks = []
    for event, entries in (extensions.get("hooks") or {}).items():
        if event == stage:
            for entry in entries or []:
                if entry.get("enabled") is not False and entry.get("optional") is True:
                    enabled_hooks.append(entry.get("command"))
    # Check the ledger for prior accepted entries for this feature_dir
    ledger_path = state_root() / "wiring" / "hook_ledger.json"
    accepted = set()
    if ledger_path.exists():
        data = json.loads(ledger_path.read_text(encoding="utf-8-sig"))
        for e in data.get("entries", []):
            if e.get("decision") == "accepted" and e.get("feature_dir") == feature_dir:
                accepted.add(e.get("command"))
    # Include the command being recorded now
    accepted.add(command)
    # All enabled hooks for this stage must have accepted entries
    return all(cmd in accepted for cmd in enabled_hooks)


if __name__ == "__main__":
    sys.exit(main())
