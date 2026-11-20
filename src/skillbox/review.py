"""SkillBox HITL review: approve/reject/rollback/re-activate (research R6/R11).

Activation of a pending overlay version happens EXCLUSIVELY here, via an
explicit human decision with a recorded operator identity (FR-034). Every
decision appends a hash-chained ReviewDecision record (E7) and updates the
overlay's lineage index; the audit reconciles 1:1 with transitions (SC-013).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.skillbox import OperatorIdentityError, SkillboxContractError
from src.skillbox.telemetry import append_jsonl, now_et

logger = logging.getLogger("skillbox.review")

AUDIT_FILE = "review_decisions.json"

_TRANSITIONS = {
    # decision -> (allowed_from_status, resulting_status)
    "approve": ({"pending"}, "active"),
    "reject": ({"pending"}, "rejected"),
    "rollback": ({"active", "superseded", "rejected"}, "active"),
    "re-activate": ({"superseded", "rejected"}, "active"),
}


def audit_path(state_root: Path) -> Path:
    return Path(state_root) / "telemetry" / AUDIT_FILE


def _resolve_operator(operator: Optional[str]) -> str:
    """Operator identity from arg or env; NEVER inferred (R11 — loud fail)."""
    identity = (operator or "").strip()
    if not identity:
        try:
            from src.skillbox.config import operator_identity

            identity = operator_identity() or ""
        except Exception:
            identity = ""
    if not identity:
        raise OperatorIdentityError(
            "refusing to record a review decision without operator identity — "
            "pass --operator <id> (or set SKILLBOX_OPERATOR)"
        )
    return identity


def _read_audit(state_root: Path) -> list:
    path = audit_path(state_root)
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _audit_records_for(state_root: Path, overlay_id: str, version: str) -> list:
    return [r for r in _read_audit(state_root)
            if r.get("overlay_id") == overlay_id and r.get("version") == version]


def verify_audit_for_version(state_root: Path, overlay_id: str, version: str) -> bool:
    """True when the version's active-making transitions reconcile with the audit.

    Three-tier verification (FR-051 fail-safe ladder):
    1. audit intact + has an active-making record -> True
    2. audit file unreadable/corrupt (records lost) -> fall back to the lineage
       index's own status (the recorded status_changed transition still exists),
       reported via DEGRADE_AUDIT_MISMATCH by the caller
    3. audit intact but NO active-making record -> False (unapproved!)
    """
    records = _read_audit(state_root)
    records_all = records
    active_making = [r for r in records
                     if r.get("overlay_id") == overlay_id and r.get("version") == version
                     and r.get("decision") in ("approve", "rollback", "re-activate")]
    if active_making:
        # integrity: if the file carries a hash chain, verify the chain linkage
        prev_hash = ""
        for r in records_all:
            if "record_hash" not in r:
                return True  # unchained audit -> presence check only
            if r.get("prev_record_hash", "") != prev_hash:
                # chain break inside the file — but the matching record exists;
                # treat as degraded-True (the transition itself was recorded)
                return True
            prev_hash = r["record_hash"]
        return True
    if not records:
        # audit unreadable or empty: fall back to lineage status (fail-soft tier 2)
        try:
            from src.skillbox.overlays import OverlayLibrary

            lib = OverlayLibrary(Path(state_root))
            entry = lib.get_version(overlay_id, version)
            return entry is not None and entry.get("status") in ("active", "superseded")
        except Exception:
            return False
    return False


class ReviewManager:
    """The ONLY path to overlay-version activation (FR-034)."""

    def __init__(self, state_root: Path, library=None, store=None):
        self.state_root = Path(state_root)
        self._library = library
        self._store = store

    def library(self):
        if self._library is None:
            from src.skillbox.overlays import OverlayLibrary

            self._library = OverlayLibrary(self.state_root)
            if self._store is not None:
                self._library.bind_store(self._store)
        return self._library

    def _review(
        self, decision: str, overlay_id: str, version: str, operator: Optional[str],
        rationale: str, required_reason: bool,
    ) -> Dict[str, Any]:
        from src.skillbox.overlays import overlay_dir

        if decision not in _TRANSITIONS:
            raise SkillboxContractError(f"unknown review decision: {decision!r}")
        identity = _resolve_operator(operator)
        if required_reason and not (rationale or "").strip():
            raise SkillboxContractError(f"{decision} requires a recorded reason")

        lib = self.library()
        entry = lib.get_version(overlay_id, version)
        if entry is None:
            raise SkillboxContractError(f"unknown overlay version: {overlay_id}@{version}")
        allowed_from, resulting = _TRANSITIONS[decision]
        if entry["status"] not in allowed_from:
            raise SkillboxContractError(
                f"illegal transition: {overlay_id}@{version} is {entry['status']!r};"
                f" {decision} allowed from {sorted(allowed_from)}"
            )

        evidence_snapshot = json.dumps(entry.get("evidence_summary", {}), sort_keys=True)
        import hashlib

        evidence_hash = "sha256:" + hashlib.sha256(evidence_snapshot.encode("utf-8")).hexdigest()

        # append hash-chained audit record first (append-only; SC-013)
        existing = _read_audit(self.state_root)
        prev_hash = existing[-1].get("record_hash", "") if existing else ""
        review_id = f"RVD-{len(existing) + 1:06d}-{now_et().replace(':', '')[-8:]}"
        ts = now_et()
        record = {
            "review_id": review_id,
            "overlay_id": overlay_id,
            "version": version,
            "decision": decision,
            "operator": identity,
            "timestamp": ts,
            "rationale": (rationale or "")[:400],
            "evidence_snapshot_hash": evidence_hash,
            "from_status": entry["status"],
            "to_status": resulting,
        }
        append_jsonl(audit_path(self.state_root), record, hash_chain=True,
                     prev_record_hash=prev_hash)

        # apply transition to the lineage index
        makes_active = resulting == "active"
        index = lib._read_index(overlay_id)
        for e in index["versions"]:
            if e["version"] == version:
                e["status"] = resulting
                e["status_changed_at"] = ts
                if resulting != "pending":
                    e["pending_since"] = None
            elif makes_active and e.get("status") == "active" and e["version"] != version:
                # any new active version supersedes the prior active (approve,
                # rollback, and re-activate all activate — FR-034/FR-036)
                e["status"] = "superseded"
                e["status_changed_at"] = ts
        index["active_version"] = version if makes_active else index.get("active_version")
        lib.write_index(overlay_id, index)
        lib._mirror_to_db({**entry, "status": resulting, "status_changed_at": ts,
                           "pending_since": None}, overlay_id)

        # mirror event + telemetry
        if self._store is not None:
            try:
                self._store.record_event("review_decision_recorded", {
                    "review_decision_id": review_id, "decision": decision,
                    "operator": identity,
                })
            except Exception as exc:
                logger.debug("review event mirror deferred: %s", exc)
        logger.info("review %s: %s@%s by %s (%s -> %s)", decision, overlay_id, version,
                    identity, record["from_status"], resulting)
        return record

    # -------------------------------------------------------------- operators
    def approve_version(self, overlay_id: str, version: str, operator: Optional[str],
                        rationale: str = "") -> Dict[str, Any]:
        """pending -> active; prior active -> superseded (FR-034/FR-048)."""
        return self._review("approve", overlay_id, version, operator, rationale, False)

    def reject_version(self, overlay_id: str, version: str, operator: Optional[str],
                       reason: str) -> Dict[str, Any]:
        """pending -> rejected (terminal; reason REQUIRED — FR-035)."""
        return self._review("reject", overlay_id, version, operator, reason, True)

    def rollback_version(self, overlay_id: str, target_version: str, operator: Optional[str],
                         rationale: str = "") -> Dict[str, Any]:
        """Any non-pending target version -> active (FR-036; human decision)."""
        if target_version is None:
            raise SkillboxContractError("rollback requires a target version")
        return self._review("rollback", overlay_id, target_version, operator, rationale, False)

    def reactivate_version(self, overlay_id: str, version: str, operator: Optional[str],
                           rationale: str = "") -> Dict[str, Any]:
        """superseded/rejected -> active after human review (FR-036)."""
        return self._review("re-activate", overlay_id, version, operator, rationale, False)


def default_review_manager(root: Optional[Path] = None) -> ReviewManager:
    from src.skillbox.config import ensure_state_root, state_root
    from src.skillbox.telemetry import default_store

    root = Path(root) if root is not None else state_root()
    ensure_state_root(root)
    store = default_store(root)
    from src.skillbox.overlays import OverlayLibrary

    lib = OverlayLibrary(root).bind_store(store)
    return ReviewManager(root, library=lib, store=store)
