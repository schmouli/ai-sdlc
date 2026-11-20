"""SkillBox Runtime façade — public API (research R15, agent-api.md contract).

Stable public surface (FR-050 / FR-065 of the spec):

    activate_skill(skill_name)                          -> EffectiveSkill
    activate_enhanced_skill(skill_name, context=None)   -> EffectiveSkill
    session_scope(skill=None)                           -> context manager
    record_event(event_type, payload, skill, session_id)-> event_id
    record_decision(decision, reason, ...)              -> decision_id
    extract_learnings(window_days=None)                 -> list[Learning]
    generate_overlays()                                 -> {staged, retired, regenerated}
    list_pending_reviews()                              -> [...]
    approve_version / reject_version /                  -> review ops (HITL gate;
        rollback_version / reactivate_version              approval-only activation, FR-034)
    analyze_skill_performance(window_days, baseline_days)-> metrics report data

Structured degradation reason codes (constitution Principle V) — every fail-soft
path logs one of these:

    DEGRADE_CONTEXT         context detection degraded (composition continues)
    DEGRADE_OVERLAY_LOAD    overlay load/parse failure (overlay skipped)
    DEGRADE_PUSH            MLflow push failure (local record untouched)
    DEGRADE_AUDIT_MISMATCH  review-audit/lineage reconciliation mismatch
                            (fail-safe resolution: last approved or none)
"""

__version__ = "0.1.0"

DEGRADE_CONTEXT = "DEGRADE_CONTEXT"
DEGRADE_OVERLAY_LOAD = "DEGRADE_OVERLAY_LOAD"
DEGRADE_PUSH = "DEGRADE_PUSH"
DEGRADE_AUDIT_MISMATCH = "DEGRADE_AUDIT_MISMATCH"

__all__ = [
    "__version__",
    "DEGRADE_CONTEXT",
    "DEGRADE_OVERLAY_LOAD",
    "DEGRADE_PUSH",
    "DEGRADE_AUDIT_MISMATCH",
    "SkillboxLookupError",
    "SkillboxContractError",
    "OperatorIdentityError",
    "ContractViolationError",
    "activate_skill",
    "activate_enhanced_skill",
    "session_scope",
    "record_event",
    "record_decision",
    "extract_learnings",
    "generate_overlays",
    "list_pending_reviews",
    "approve_version",
    "reject_version",
    "rollback_version",
    "reactivate_version",
    "analyze_skill_performance",
    "refresh_injection",
]


def activate_skill(skill_name: str, repo_root=None):
    """Compose a skill with automatic context detection (agent-api.md)."""
    from pathlib import Path

    from src.skillbox.compose import activate_skill as _activate

    return _activate(skill_name, Path(repo_root) if repo_root else Path("."))


def activate_enhanced_skill(skill_name: str, project_context=None, repo_root=None):
    """Compose with explicit project context (agent-api.md US4 variant)."""
    from pathlib import Path

    from src.skillbox.compose import activate_enhanced_skill as _activate

    return _activate(skill_name, project_context,
                     Path(repo_root) if repo_root else Path("."))


def session_scope(skill=None):
    """Session context manager (FR-015)."""
    from src.skillbox.telemetry import default_store, session_scope as _scope

    return _scope(default_store(), skill=skill)


def record_event(event_type, payload, skill=None, session_id=None):
    """Record one typed telemetry event (FR-016); local-first (FR-018)."""
    from src.skillbox.telemetry import default_store

    return default_store().record_event(event_type, payload, skill=skill,
                                        session_id=session_id)


def record_decision(decision, reason, session_id=None, skill=None, context_summary=""):
    """Append a decision record (FR-017)."""
    from src.skillbox.telemetry import default_store

    return default_store().record_decision(decision, reason, session_id=session_id,
                                           skill=skill, context_summary=context_summary)


def extract_learnings(window_days=None):
    """Run the learning analysis (FR-024..FR-027)."""
    from src.skillbox.config import get_skillbox_config, state_root
    from src.skillbox.learning import LearningEngine
    from src.skillbox.telemetry import default_store

    root = state_root()
    store = default_store(root)
    engine = LearningEngine(store, root / "learnings",
                            get_skillbox_config().get("learning", {}))
    return engine.extract_learnings(window_days=window_days)


def generate_overlays():
    """Stage pending overlay versions from eligible learnings (FR-028/FR-033)."""
    from src.skillbox.cli import generate_overlays as _generate
    from src.skillbox.config import get_skillbox_config, state_root
    from src.skillbox.learning import LearningEngine
    from src.skillbox.overlays import OverlayLibrary
    from src.skillbox.telemetry import default_store

    root = state_root()
    store = default_store(root)
    lib = OverlayLibrary(root).bind_store(store)
    engine = LearningEngine(store, root / "learnings",
                            get_skillbox_config().get("learning", {}))
    return _generate(lib, engine)


def list_pending_reviews():
    """FR-037 review surface."""
    from src.skillbox.config import get_skillbox_config, state_root
    from src.skillbox.overlays import OverlayLibrary
    from src.skillbox.telemetry import default_store

    root = state_root()
    store = default_store(root)
    lib = OverlayLibrary(root).bind_store(store)
    sla = int(get_skillbox_config().get("review", {}).get("pending_review_sla_days", 7))
    return lib.list_pending_reviews(sla_days=sla)


def approve_version(overlay_id, version, operator=None, rationale=""):
    """HITL activation gate — approve (FR-034)."""
    from src.skillbox.review import default_review_manager

    return default_review_manager().approve_version(overlay_id, version,
                                                    operator=operator, rationale=rationale)


def reject_version(overlay_id, version, operator=None, reason=""):
    """HITL gate — reject with REQUIRED reason (FR-035)."""
    from src.skillbox.review import default_review_manager

    return default_review_manager().reject_version(overlay_id, version,
                                                   operator=operator, reason=reason)


def rollback_version(overlay_id, target_version, operator=None, rationale=""):
    """HITL gate — rollback to an earlier lineage version (FR-036)."""
    from src.skillbox.review import default_review_manager

    return default_review_manager().rollback_version(overlay_id, target_version,
                                                     operator=operator, rationale=rationale)


def reactivate_version(overlay_id, version, operator=None, rationale=""):
    """HITL gate — re-activate a superseded/rejected version (FR-036)."""
    from src.skillbox.review import default_review_manager

    return default_review_manager().reactivate_version(overlay_id, version,
                                                       operator=operator,
                                                       rationale=rationale)


def analyze_skill_performance(window_days=None, baseline_days=None):
    """Per-skill + per-overlay-version metric analysis (FR-039..FR-041)."""
    from src.skillbox.metrics import SkillBoxMetrics
    from src.skillbox.telemetry import default_store

    return SkillBoxMetrics(default_store()).analyze(window_days=window_days,
                                                    baseline_days=baseline_days)


def refresh_injection(repo_root=None):
    """Refresh the IDE auto-injection block from approved overlays (US4 extension)."""
    from pathlib import Path

    from src.skillbox.inject import refresh

    return refresh(Path(repo_root) if repo_root is not None else Path("."))


class SkillboxError(Exception):
    """Base class for SkillBox contract errors (the only raising paths)."""


class SkillboxLookupError(SkillboxError):
    """Unknown / disabled / ambiguous skill lookup (agent-api.md)."""


class SkillboxContractError(SkillboxError):
    """Malformed arguments or invalid state transitions (agent-api.md)."""


class OperatorIdentityError(SkillboxError):
    """Review operation without explicit operator identity (research R11)."""


class ContractViolationError(SkillboxContractError):
    """Telemetry payload/type validation failure (agent-api.md)."""
