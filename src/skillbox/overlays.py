"""SkillBox overlay library: append-only version lineage (research R6; FR-032/FR-033).

Version content lives at ``<root>/overlays/<collection>/<slug>/versions/vNN.md``
(immutable once written); per-overlay ``index.json`` carries the lineage. Status
resolution is recomputed from files + registry on every read — fail-safe per
FR-051: never returns a pending version.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.skillbox import DEGRADE_AUDIT_MISMATCH, DEGRADE_OVERLAY_LOAD
from src.skillbox.telemetry import atomic_write_json, now_et

logger = logging.getLogger("skillbox.overlays")

VALID_COLLECTIONS = ("global", "architecture", "testing", "security", "performance",
                     "project", "language", "frameworks")
VALID_STATUS = ("pending", "active", "superseded", "rejected", "retired")


def overlay_dir(root: Path, overlay_id: str) -> Path:
    return Path(root) / "overlays" / overlay_id


def _slug_ok(overlay_id: str) -> bool:
    parts = overlay_id.split("/")
    return (len(parts) == 2 and all(p and ".." not in p for p in parts)
            and parts[0] in VALID_COLLECTIONS)


class OverlayLibrary:
    """Version-lineage store over the overlays directory tree (E5/E6)."""

    def __init__(self, state_root: Path, registry_overlays_file: Optional[Path] = None):
        self.state_root = Path(state_root)
        self.overlays_root = self.state_root / "overlays"
        self.registry_file = registry_overlays_file or (self.state_root / "registry" / "overlays.json")
        self._registry: Dict[str, Dict[str, Any]] = {}
        self._load_registry()

    # ------------------------------------------------------------- metadata
    def _load_registry(self) -> None:
        if not self.registry_file.exists():
            return
        try:
            data = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("overlay registry unreadable: %s", exc)
            return
        for entry in data.get("overlays", []) or []:
            if isinstance(entry, dict) and entry.get("overlay_id"):
                self._registry[entry["overlay_id"]] = entry

    def _save_registry(self) -> None:
        atomic_write_json(self.registry_file, {
            "schema_version": 1,
            "overlays": list(self._registry.values()),
        })

    def ensure_overlay(self, overlay_id: str, title: str, collection: str,
                       source_learning_ids: List[str]) -> Dict[str, Any]:
        if overlay_id not in self._registry:
            record = {
                "overlay_id": overlay_id,
                "collection": collection,
                "title": title,
                "source_learning_ids": list(source_learning_ids),
                "created_at": now_et(),
                "impact_metrics_ref": overlay_id,
            }
            self._registry[overlay_id] = record
            self._save_registry()
        return self._registry[overlay_id]

    def get_meta(self, overlay_id: str) -> Optional[Dict[str, Any]]:
        return self._registry.get(overlay_id)

    def all_meta(self) -> List[Dict[str, Any]]:
        return list(self._registry.values())

    # --------------------------------------------------------------- staging
    def stage_version(
        self, overlay_id: str, content: str, source_learning_ids: List[str],
        evidence_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Stage a NEW version as pending; never overwrites existing (FR-032/FR-033)."""
        if not _slug_ok(overlay_id):
            raise ValueError(f"invalid overlay id: {overlay_id!r}")
        import hashlib

        content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        index_path = overlay_dir(self.state_root, overlay_id) / "index.json"
        index = self._read_index(overlay_id)

        # idempotency: unchanged content at the tip -> no new version (SC-004)
        if index["versions"]:
            tip = index["versions"][-1]
            if tip["content_hash"] == content_hash:
                return tip

        next_num = len(index["versions"]) + 1
        version = f"v{next_num:02d}"
        versions_dir = overlay_dir(self.state_root, overlay_id) / "versions"
        versions_dir.mkdir(parents=True, exist_ok=True)
        content_path = versions_dir / f"{version}.md"
        if content_path.exists():
            raise ValueError(f"version file collision: {content_path}")
        content_path.write_text(content, encoding="utf-8")

        entry = {
            "version": version,
            "content_path": str(content_path.relative_to(self.state_root)),
            "content_hash": content_hash,
            "generated_at": now_et(),
            "source_learning_ids": list(source_learning_ids),
            "evidence_summary": dict(evidence_summary),
            "status": "pending",
            "status_changed_at": now_et(),
            "pending_since": now_et(),
        }
        index["versions"].append(entry)
        index["overlay_id"] = overlay_id
        atomic_write_json(index_path, index)

        self._mirror_to_db(entry)
        self.record_event_staged(overlay_id, version, source_learning_ids)
        return entry

    def _read_index(self, overlay_id: str) -> Dict[str, Any]:
        path = overlay_dir(self.state_root, overlay_id) / "index.json"
        if not path.exists():
            return {"schema_version": 1, "overlay_id": overlay_id, "versions": [],
                    "active_version": None}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("overlay index unreadable (%s): %s", overlay_id, exc)
            return {"schema_version": 1, "overlay_id": overlay_id, "versions": [],
                    "active_version": None}

    def write_index(self, overlay_id: str, index: Dict[str, Any]) -> None:
        atomic_write_json(overlay_dir(self.state_root, overlay_id) / "index.json", index)

    def read_content(self, overlay_id: str, version: str) -> Optional[str]:
        """Read a version's immutable content (composition input)."""
        entry = self.get_version(overlay_id, version)
        if entry is None:
            return None
        path = self.state_root / entry["content_path"]
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("overlay content unreadable (%s@%s): %s", overlay_id, version, exc)
            return None

    def get_version(self, overlay_id: str, version: str) -> Optional[Dict[str, Any]]:
        for entry in self._read_index(overlay_id)["versions"]:
            if entry["version"] == version:
                return entry
        return None

    def list_versions(self, overlay_id: str) -> List[Dict[str, Any]]:
        return list(self._read_index(overlay_id)["versions"])

    # ---------------------------------------------------------- DB mirror
    def _mirror_to_db(self, entry: Dict[str, Any], overlay_id: str = "") -> None:
        """Best-effort SQLite mirror (queryability)."""
        try:
            if not overlay_id:
                overlay_id = entry["overlay_id"]
        except KeyError:
            pass
        try:
            conn = self._store_connection()
            if conn is None:
                return
            with conn:
                conn.execute(
                    "INSERT INTO overlay_versions (overlay_id, version, content_path,"
                    " content_hash, generated_at, source_learning_ids, evidence_summary,"
                    " status, status_changed_at, pending_since)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(overlay_id, version) DO UPDATE SET status = excluded.status,"
                    " status_changed_at = excluded.status_changed_at, pending_since = excluded.pending_since",
                    (overlay_id, entry["version"], entry["content_path"], entry["content_hash"],
                     entry["generated_at"], json.dumps(entry["source_learning_ids"]),
                     json.dumps(entry["evidence_summary"]), entry["status"],
                     entry["status_changed_at"], entry.get("pending_since")),
                )
        except Exception as exc:
            logger.debug("overlay DB mirror deferred: %s", exc)

    def bind_store(self, store) -> "OverlayLibrary":
        self._store = store
        return self

    def _store_connection(self):
        store = getattr(self, "_store", None)
        if store is None:
            return None
        try:
            return store.connection()
        except Exception:
            return None

    def record_event_staged(self, overlay_id: str, version: str,
                            source_learning_ids: List[str]) -> None:
        store = getattr(self, "_store", None)
        if store is None:
            return
        try:
            store.record_event("overlay_version_staged", {
                "overlay_id": overlay_id, "version": version,
                "source_learning_ids": list(source_learning_ids)[:10],
            })
        except Exception as exc:
            logger.debug("staged event write deferred: %s", exc)

    # --------------------------------------------------- fail-safe resolution
    def resolve_active_version(self, overlay_id: str) -> Optional[Dict[str, Any]]:
        """The version that composes — fail-safe to last approved or None (FR-051).

        Recomputed from index.json on every read; a pending version is NEVER a
        resolution outcome; mismatched audit states log DEGRADE_AUDIT_MISMATCH.
        """
        index = self._read_index(overlay_id)
        active = index.get("active_version")
        if active:
            entry = self.get_version(overlay_id, active)
            if entry is not None and entry["status"] == "active":
                try:
                    from src.skillbox.review import verify_audit_for_version

                    if verify_audit_for_version(self.state_root, overlay_id, active):
                        return entry
                    logger.warning(
                        "%s: audit reconciliation failed for active %s (%s)",
                        overlay_id, active, DEGRADE_AUDIT_MISMATCH,
                    )
                    return self._last_verifiable_approved(overlay_id)
                except Exception as exc:
                    logger.warning("audit check degraded for %s: %s", overlay_id, exc)
                    return self._last_verifiable_approved(overlay_id)
        return self._last_verifiable_approved(overlay_id, exclude_pending=True)

    def _last_verifiable_approved(self, overlay_id: str, exclude_pending: bool = True) -> Optional[Dict[str, Any]]:
        """Newest version whose content hash verifies AND status is approved-set."""
        approved_states = {"active", "superseded"}
        for entry in reversed(self.list_versions(overlay_id)):
            status = entry.get("status")
            if exclude_pending and status in ("pending", "rejected", "retired"):
                continue
            if status not in approved_states:
                continue
            try:
                from src.skillbox.review import verify_audit_for_version

                if verify_audit_for_version(self.state_root, overlay_id, entry["version"]):
                    return entry
            except Exception:
                continue
        return None

    def list_pending_reviews(self, sla_days: int = 7) -> List[Dict[str, Any]]:
        """FR-037 surface: pending versions with aging + evidence + diff summary."""
        from datetime import datetime
        from src.skillbox.telemetry import ET

        out = []
        for meta in self._registry.values():
            overlay_id = meta["overlay_id"]
            for entry in self._read_index(overlay_id)["versions"]:
                if entry.get("status") != "pending":
                    continue
                pending_since = entry.get("pending_since") or entry["generated_at"]
                try:
                    since_dt = datetime.fromisoformat(pending_since)
                    age_days = max(0, (datetime.now(ET) - since_dt).total_seconds() / 86400)
                except (ValueError, TypeError):
                    age_days = 0
                active = self.resolve_active_version(overlay_id)
                active_content = (self.read_content(overlay_id, active["version"]) or ""
                                  if active else "")
                pending_content = self.read_content(overlay_id, entry["version"]) or ""
                out.append({
                    "overlay_id": overlay_id,
                    "version": entry["version"],
                    "pending_since": pending_since,
                    "age_days": round(age_days, 2),
                    "sla_exceeded": age_days > sla_days,
                    "content_summary": pending_content[:400],
                    "diff_summary": self._diff_summary(active_content, pending_content),
                    "evidence_summary": entry.get("evidence_summary", {}),
                })
        return out

    @staticmethod
    def _diff_summary(old: str, new: str) -> str:
        old_lines = set(old.splitlines())
        new_lines = [ln for ln in new.splitlines() if ln not in old_lines]
        return ("+" + "\n+".join(new_lines))[:400] if new_lines else "(no content delta vs active)"


def cascade_retire_for_learning(store, learning_id: str) -> List[str]:
    """FR-029 cascade: retire overlay versions whose source learning is retired.

    Fail-safe direction only; the active version is demoted to retired —
    compositions then skip the overlay (never activate anything).
    """
    retired: List[str] = []
    library = OverlayLibrary(Path(store.root)).bind_store(store)
    for meta in library.all_meta():
        if learning_id not in (meta.get("source_learning_ids") or []):
            continue
        overlay_id = meta["overlay_id"]
        index = library._read_index(overlay_id)
        changed = False
        for entry in index["versions"]:
            if entry.get("status") in ("pending", "active") and \
                    learning_id in (entry.get("source_learning_ids") or []):
                entry["status"] = "retired"
                entry["status_changed_at"] = now_et()
                if index.get("active_version") == entry["version"]:
                    index["active_version"] = None
                changed = True
                library._mirror_to_db(entry, overlay_id)
                retired.append(f"{overlay_id}@{entry['version']}")
        if changed:
            library.write_index(overlay_id, index)
    return retired
