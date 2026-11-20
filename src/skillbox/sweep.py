"""Deterministic round-summary sweep (feature 005 US4).

Converts fixed-format round summaries from ``logs/egm_rounds/`` into
``egm_review_round`` events, idempotently (content-hash dedup via a persisted
state artifact), with fail-soft semantics. The sweep runs as scheduler step 8
(when capture is enabled) and is a no-op otherwise.

Contract: specs/005-ai-sdlc-wiring/contracts/capture-contract.md
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


RETENTION_DAYS = 30


def _load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"entries": {}}


def _save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8", newline="\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sweep_rounds(
    drop_dir: Path,
    store,
    state_path: Path,
    now: datetime | None = None,
    retention_days: int = RETENTION_DAYS,
) -> dict:
    """Sweep the drop dir; record one egm_review_round per new/unseen summary.

    Fail-soft: malformed files are logged-and-skipped (left in place, with the
    file name + reason carried back to the caller); deleted after
    ``retention_days``. Returns a result dict (recorded count, skipped list,
    cleaned count, pruned count).
    """
    now = now or datetime.now(timezone.utc)
    result = {"recorded": 0, "skipped": [], "cleaned": 0, "pruned": 0}
    if not drop_dir.exists():
        return result
    state = _load_state(state_path)
    seen = state.setdefault("entries", {})

    # prune dedup state to the retention window (bounded sweep state): hashes whose
    # swept_at is older than retention_days are forgotten, so a file that re-appears
    # after the window can be re-swept and never grows unbounded.
    cutoff = now - timedelta(days=retention_days)
    pruned_hashes = []
    for sha, meta in list(seen.items()):
        try:
            swept_at = datetime.fromisoformat(meta["swept_at"])
            swept_at = swept_at.replace(tzinfo=timezone.utc) if swept_at.tzinfo is None else swept_at
        except (KeyError, TypeError, ValueError):
            pruned_hashes.append(sha)  # malformed state entries are pruned too
            continue
        if swept_at < cutoff:
            pruned_hashes.append(sha)
    for sha in pruned_hashes:
        del seen[sha]
    result["pruned"] = len(pruned_hashes)

    for f in sorted(drop_dir.glob("round-*.json")):
        text = f.read_text(encoding="utf-8", errors="replace")
        sha = _sha(text)
        if sha in seen:
            continue
        try:
            summary = json.loads(text)
            for req in ("round_id", "profile", "target_file", "reviewers", "findings_count", "round_kind", "recorded_at", "consult"):
                if req not in summary:
                    raise ValueError(f"missing required field: {req}")
        except (json.JSONDecodeError, ValueError) as exc:
            # per-file skip reason (not just a count): caller logs name + reason
            result["skipped"].append({"file": f.name, "reason": str(exc)})
            continue

        store.record_event(
            event_type="egm_review_round",
            skill=None,
            payload={
                "round_id": summary["round_id"],
                "profile": summary["profile"],
                "target_file": summary["target_file"],
                "reviewers": summary["reviewers"],
                "findings_count": summary["findings_count"],
                "round_kind": summary["round_kind"],
                "consult_mode": summary["consult"].get("mode"),
                "consult_freshness": summary["consult"].get("freshness"),
                "consult_fallback": summary["consult"].get("fallback"),
                "source_file_sha256": sha,
            },
        )
        seen[sha] = {"file": f.name, "swept_at": now.isoformat()}
        result["recorded"] += 1

    # retention cleanup: remove consumed files older than retention_days
    for f in drop_dir.glob("round-*.json"):
        try:
            mt = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mt < cutoff:
                f.unlink()
                result["cleaned"] += 1
        except OSError:
            pass

    _save_state(state_path, state)
    return result
