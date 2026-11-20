"""SkillBox IDE auto-injection bridge (US4 extension; FR-053 surface).

Refreshes a marked block inside ``.github/copilot-instructions.md`` — the file
VS Code auto-loads into every session — with a compact digest of the APPROVED
active overlays. Pending/rejected versions never inject (SC-014); the base
instruction content outside the markers is never touched.

Idempotent: re-running with unchanged approved content leaves the file
byte-identical. Degradation fail-soft: any failure logs a reason and leaves
the existing block intact.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from src.skillbox import DEGRADE_OVERLAY_LOAD
from src.skillbox.telemetry import now_et

logger = logging.getLogger("skillbox.inject")

ET = ZoneInfo("America/New_York")

INSTRUCTIONS_FILE = Path(".github") / "copilot-instructions.md"
BEGIN_MARKER = "<!-- SKILLBOX:BEGIN approved-overlay-guidance (auto-generated; edit outside markers only) -->"
END_MARKER = "<!-- SKILLBOX:END -->"


def inject_block_path(repo_root: Path) -> Path:
    return Path(repo_root) / INSTRUCTIONS_FILE


def compose_digest(library, sla_hours: Optional[int] = None) -> List[Dict[str, Any]]:
    """Approved-active overlays → compact digest entries (approved-only, SC-014)."""
    digest: List[Dict[str, Any]] = []
    for meta in library.all_meta():
        overlay_id = meta["overlay_id"]
        active = library.resolve_active_version(overlay_id)
        if active is None:
            continue
        content = library.read_content(overlay_id, active["version"])
        if content is None:
            logger.warning("inject: content unreadable for %s (%s)", overlay_id,
                           DEGRADE_OVERLAY_LOAD)
            continue
        # the seed/recommendation section: extract complete bullet lines (a
        # continuation line joins its starter — capped text stays whole)
        bullets: List[str] = []
        last_was_bullet = False
        for raw_line in content.split("## Recommendation")[-1].splitlines()[1:]:
            line = raw_line.strip()
            if not line:
                last_was_bullet = False
                continue
            if line.startswith(("- ", "* ")):
                if len(bullets) < 8:
                    bullets.append(line[2:].strip())
                last_was_bullet = True
            elif line[0].isdigit() and ". " in line[:4]:
                if len(bullets) < 8:
                    bullets.append(line.split(". ", 1)[1].strip())
                last_was_bullet = True
            elif last_was_bullet and bullets:
                # continuation of the previous bullet (wrapped sentence)
                bullets[-1] = bullets[-1].rstrip() + " " + line
        digest.append({
            "overlay_id": overlay_id,
            "collection": meta.get("collection", overlay_id.split("/")[0]),
            "title": meta.get("title", overlay_id),
            "version": active["version"],
            "content_hash": active.get("content_hash", ""),
            "bullets": bullets,
            "source_learning_ids": meta.get("source_learning_ids", []),
        })
    return digest


def render_block(digest: List[Dict[str, Any]], approvals: List[Dict[str, Any]],
                 refreshed_at: Optional[str] = None) -> str:
    """The injected markdown block (tight; the instruction file is always loaded)."""
    lines = [
        BEGIN_MARKER,
        "",
        "## SkillBox Approved Overlay Guidance",
        "",
        "Auto-generated from human-approved SkillBox overlays (specs/004-skillbox-adaptive-learning).",
        "Pending/unapproved versions are never included; review via `python -m src.skillbox review list`.",
        "",
    ]
    if not digest:
        lines += ["*No approved overlays active yet — skill executions use the base skills only.*", ""]
    current_collection = None
    for entry in digest:
        if entry["collection"] != current_collection:
            current_collection = entry["collection"]
            lines.append(f"### {current_collection.capitalize()}")
        provenance = ""
        review = next((a for a in approvals
                       if a.get("overlay_id") == entry["overlay_id"]
                       and a.get("version") == entry["version"]), None)
        if review:
            provenance = f" (approved {review.get('timestamp', '')[:10]} by {review.get('operator')})"
        lines.append(f"- **{entry['title']}** `{entry['overlay_id']}@{entry['version']}`{provenance}")
        for bullet in entry["bullets"]:
            lines.append(f"  {bullet}")
    lines += ["", f"<!-- skillbox-digest: {_content_fingerprint(digest)} -->",
              f"_Last refreshed: {refreshed_at or now_et()}_", "", END_MARKER]
    return "\n".join(lines)


def _existing_refreshed_at(text: str) -> Optional[str]:
    """Preserve the original refresh timestamp when approved content is unchanged."""
    needle = "_Last refreshed: "
    if needle not in text:
        return None
    tail = text.split(needle, 1)[1]
    return tail.split("_", 1)[0].strip() or None


import hashlib  # noqa: E402


def _content_fingerprint(digest: List[Dict[str, Any]]) -> str:
    payload = json.dumps(digest, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _content_fingerprint_from(existing: str) -> Optional[str]:
    """Extract the previously stored digest fingerprint, if present."""
    needle = _FINGERPRINT_PREFIX
    if needle not in existing:
        return None
    tail = existing.split(needle, 1)[1]
    token = tail.split(" -->", 1)[0].strip()
    return token or None


_FINGERPRINT_PREFIX = "skillbox-digest: "


def _read_approvals(state_root: Path, overlay_version_pairs) -> List[Dict[str, Any]]:
    audit = Path(state_root) / "telemetry" / "review_decisions.json"
    if not audit.exists():
        return []
    pairs = set(overlay_version_pairs)
    approvals: List[Dict[str, Any]] = []
    with open(audit, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            pair = (record.get("overlay_id"), record.get("version"))
            if pair in pairs and record.get("decision") in ("approve", "rollback", "re-activate"):
                approvals.append(record)
    return approvals

def refresh(repo_root: Optional[Path] = None, library=None, store=None,
            instructions_path: Optional[Path] = None) -> Dict[str, Any]:
    """Regenerate the marked block from approved overlays; idempotent; fail-soft."""
    repo_root = Path(repo_root) if repo_root is not None else Path(".")
    target = Path(instructions_path) if instructions_path else inject_block_path(repo_root)
    try:
        if library is None:
            from src.skillbox.overlays import OverlayLibrary
            from src.skillbox.telemetry import default_store

            store = store or default_store(Path(repo_root) / ".skillbox")
            library = OverlayLibrary(Path(repo_root) / ".skillbox").bind_store(store)
        digest = compose_digest(library)
        approvals = _read_approvals(
            Path(repo_root) / ".skillbox",
            [(d["overlay_id"], d["version"]) for d in digest])
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        # idempotency: preserve the original refresh timestamp when the approved
        # digest is unchanged (a re-rendered timestamp alone is not a change)
        preserved_ts = _existing_refreshed_at(existing) if (
            BEGIN_MARKER in existing and _content_fingerprint(digest) == _content_fingerprint_from(existing)
        ) else None
        new_block = render_block(digest, approvals, refreshed_at=preserved_ts)
        if BEGIN_MARKER in existing and END_MARKER in existing:
            pre = existing.split(BEGIN_MARKER)[0]
            post = existing.split(END_MARKER, 1)[1]
            updated = pre + new_block + "\n" + post.lstrip("\n")
        else:
            updated = existing.rstrip("\n") + "\n\n" + new_block if existing.strip() else new_block
        # stable normalization: single trailing newline both sides of the compare
        if updated.rstrip("\n") + "\n" == existing.rstrip("\n") + "\n":
            return {"changed": False, "overlays": len(digest), "target": str(target)}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(updated, encoding="utf-8")
        if store is not None:
            try:
                store.record_event("inject_refreshed", {
                    "overlays": len(digest), "target": "copilot-instructions",
                })
            except Exception:
                pass
        return {"changed": True, "overlays": len(digest), "target": str(target)}
    except Exception as exc:
        logger.warning("inject refresh degraded (block left intact): %s", exc)
        return {"changed": False, "error": str(exc), "overlays": 0}
