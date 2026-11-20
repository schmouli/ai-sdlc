"""SkillBox skill registry: discovery, content hashing, scan-and-merge (research R9).

Survives upstream upgrades (FR-002/FR-003/FR-004): content-hash change detection,
rename identity chains, removed-skill retention, override preservation. Never
writes to `.github/skills/**` (FR-001).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.skillbox.telemetry import atomic_write_json, now_et

logger = logging.getLogger("skillbox.registry")

SKILLS_REGISTRY_FILE = "skills.json"
_SCHEMA_VERSION = 1
_DEFAULT_OVERLAY_GROUPS = ["global", "testing", "security", "project"]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def discover_skills(skills_root: Path) -> List[Dict[str, Any]]:
    """Scan ``.github/skills/*/SKILL.md`` -> discovery records (id, path, sha)."""
    discovered: List[Dict[str, Any]] = []
    if not Path(skills_root).exists():
        return discovered
    for skill_dir in sorted(Path(skills_root).iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        discovered.append({
            "skill_id": skill_dir.name,
            "path": str(skill_md),
            "content_hash": "sha256:" + sha256_file(skill_md),
        })
    return discovered


class SkillRegistry:
    """Scan-and-merge registry over the upstream skills directory (FR-005..FR-008)."""

    def __init__(self, registry_root: Path, skills_root: Path):
        self.registry_root = Path(registry_root)
        self.registry_file = self.registry_root / SKILLS_REGISTRY_FILE
        self.skills_root = Path(skills_root)
        self._entries: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------- persistence
    def load(self) -> None:
        """Load the registry file; corrupt/missing -> empty (self-heal on reindex)."""
        self._entries = {}
        if not self.registry_file.exists():
            return
        try:
            data = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("registry file unreadable, will rebuild from scan: %s", exc)
            return
        if not isinstance(data, dict):
            return
        for entry in data.get("skills", []):
            if isinstance(entry, dict) and entry.get("skill_id"):
                self._entries[entry["skill_id"]] = entry

    def save(self) -> None:
        payload = {"schema_version": _SCHEMA_VERSION, "skills": list(self._entries.values())}
        atomic_write_json(self.registry_file, payload)

    # ------------------------------------------------------------------ access
    def get(self, skill_id: str) -> Optional[Dict[str, Any]]:
        return self._entries.get(skill_id)

    def all_entries(self) -> List[Dict[str, Any]]:
        return list(self._entries.values())

    def is_enabled(self, skill_id: str) -> bool:
        entry = self._entries.get(skill_id)
        if entry is None:
            return False
        return bool(entry.get("enabled", True))

    def overlay_groups(self, skill_id: str) -> List[str]:
        entry = self._entries.get(skill_id)
        if entry is None:
            return list(_DEFAULT_OVERLAY_GROUPS)
        return list(entry.get("overlay_groups") or _DEFAULT_OVERLAY_GROUPS)

    def set_override(self, skill_id: str, enabled: Optional[bool] = None,
                     overlay_groups: Optional[List[str]] = None) -> bool:
        """Operator override (FR-006); False when skill unknown."""
        entry = self._entries.get(skill_id)
        if entry is None:
            return False
        if enabled is not None:
            entry["enabled"] = bool(enabled)
        if overlay_groups is not None:
            entry["overlay_groups"] = list(overlay_groups)
        self.save()
        return True

    # ---------------------------------------------------------------- reindex
    def reindex(self) -> Dict[str, Any]:
        """Scan-and-merge (R9). Returns the change report (FR-002/FR-004)."""
        known = {eid: dict(entry) for eid, entry in self._entries.items()}
        discovered = discover_skills(self.skills_root)
        by_hash: Dict[str, str] = {}
        for old_id, entry in known.items():
            if entry.get("status", "active") == "active":
                by_hash[entry.get("content_hash", "")] = old_id

        report: Dict[str, Any] = {"added": [], "removed": [], "renamed": [],
                                  "changed": [], "anomalies": []}

        discovered_ids = set()
        for record in discovered:
            skill_id = record["skill_id"]
            discovered_ids.add(skill_id)
            content_hash = record["content_hash"]
            existing = known.get(skill_id)

            if existing is None:
                renamed_from = by_hash.get(content_hash)
                if renamed_from:
                    report["renamed"].append({"from": renamed_from, "to": skill_id})
                    old_entry = known[renamed_from]
                    old_entry["status"] = "removed"
                    old_entry["last_seen"] = now_et()
                    self._entries[renamed_from] = old_entry
                self._entries[skill_id] = {
                    "skill_id": skill_id,
                    "base_path": record["path"],
                    "content_hash": content_hash,
                    "enabled": True,
                    "overlay_groups": list(_DEFAULT_OVERLAY_GROUPS),
                    "status": "active",
                    "renamed_from": renamed_from,
                    "perf_summary_ref": skill_id,
                    "first_seen": now_et(),
                    "last_seen": now_et(),
                    "last_indexed": now_et(),
                }
                if renamed_from is None:
                    report["added"].append(skill_id)
                continue

            existing["base_path"] = record["path"]
            if existing.get("content_hash") != content_hash:
                ambiguous = any(
                    other_id != skill_id
                    and other.get("content_hash") == content_hash
                    and other.get("status", "active") == "active"
                    for other_id, other in known.items()
                )
                if ambiguous:
                    report["anomalies"].append(
                        f"skill {skill_id}: content matches another active skill; manual review advised"
                    )
                existing["content_hash"] = content_hash
                report["changed"].append(skill_id)
            # never clobber operator overrides (FR-006) — only refresh observables
            existing.setdefault("enabled", True)
            existing.setdefault("overlay_groups", list(_DEFAULT_OVERLAY_GROUPS))
            existing["status"] = "active"
            existing["last_seen"] = now_et()
            existing["last_indexed"] = now_et()
            existing.setdefault("renamed_from", None)
            self._entries[skill_id] = existing

        for old_id, entry in known.items():
            if old_id not in discovered_ids and entry.get("status", "active") == "active":
                entry["status"] = "removed"
                entry["last_seen"] = now_et()
                self._entries[old_id] = entry
                report["removed"].append(old_id)

        self.save()
        for key in ("added", "removed", "renamed", "changed"):
            logger.info("registry reindex %s: %s", key, report[key])
        if report["anomalies"]:
            logger.warning("registry reindex anomalies: %s", report["anomalies"])
        return report

    def ensure_indexed(self) -> Dict[str, Any]:
        """Reindex when the registry is empty or fully removed (FR-002 activation path)."""
        needs = not self._entries or all(
            e.get("status") == "removed" for e in self._entries.values()
        )
        if needs:
            return self.reindex()
        return {"added": [], "removed": [], "renamed": [], "changed": [], "anomalies": []}
