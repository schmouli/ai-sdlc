"""SkillBox composition engine (research R7; FR-009..FR-014, US4).

Builds the EffectiveSkill activation package: upstream SKILL.md bytes verbatim
first, approved overlay sections attributed after. Fail-soft — any overlay
problem degrades that overlay; activation still succeeds base-only.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.skillbox import (DEGRADE_CONTEXT, DEGRADE_OVERLAY_LOAD, SkillboxContractError,
                          SkillboxLookupError)
from src.skillbox.telemetry import now_et

logger = logging.getLogger("skillbox.compose")

OVERLAY_HEADER = "## Overlay Guidance (SkillBox)"


def _sha256(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def compose_skill(
    skill_name: str,
    project_context: Optional[Dict[str, Any]] = None,
    library=None,
    registry=None,
    store=None,
    repo_root: Path = Path("."),
) -> Dict[str, Any]:
    """composeSkill(skillName, projectContext) — agent-api.md contract."""
    started = time.time()
    degraded: List[str] = []

    if registry is None:
        from src.skillbox.registry import SkillRegistry
        from src.skillbox.telemetry import default_store

        store = store or default_store()
        registry = SkillRegistry(Path(store.root) / "registry", repo_root / ".github" / "skills")
        registry.load()
        registry.ensure_indexed()

    entry = registry.get(skill_name)
    if entry is None or entry.get("status", "active") == "removed":
        raise SkillboxLookupError(f"unknown_skill: {skill_name!r} not in registry")
    if not registry.is_enabled(skill_name):
        raise SkillboxLookupError(f"skill_disabled: {skill_name!r} is disabled by operator")

    base_path = repo_root / entry["base_path"]
    try:
        base_content = base_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillboxLookupError(f"unreadable skill source for {skill_name!r}: {exc}")

    # context detection (FR-010): explicit context wins; detection degrades softly
    if project_context is None:
        try:
            from src.skillbox.context import detect_context

            project_context = detect_context(repo_root)
        except Exception as exc:
            degraded.append(f"{DEGRADE_CONTEXT}:{_compact(str(exc))}")
            project_context = {
                "language": [], "framework": [], "database": ["unknown"],
                "deployment_platform": ["none-detected"], "architecture_style": "unknown",
                "repository_type": "unknown", "detection_confidence": "unknown",
            }

    if library is None:
        from src.skillbox.overlays import OverlayLibrary

        library = OverlayLibrary(Path(store.root)).bind_store(store)

    selected: List[Dict[str, Any]] = []
    overlay_sections: List[str] = []
    for group in registry.overlay_groups(skill_name):
        matched = _match_collection(library, group, project_context)
        for meta in matched:
            overlay_id = meta["overlay_id"]
            try:
                active = library.resolve_active_version(overlay_id)
            except Exception as exc:
                degraded.append(f"{DEGRADE_OVERLAY_LOAD}:{overlay_id}:{_compact(str(exc))}")
                continue
            if active is None:
                # no approved active version -> skip gracefully (FR-048/SC-014)
                logger.info("overlay %s skipped: no approved active version", overlay_id)
                continue
            content = library.read_content(overlay_id, active["version"])
            if content is None:
                degraded.append(f"{DEGRADE_OVERLAY_LOAD}:{overlay_id}:content-unreadable")
                continue
            selected.append({
                "overlay_id": overlay_id,
                "version": active["version"],
                "title": meta.get("title", overlay_id),
                "collection": meta.get("collection", overlay_id.split("/")[0]),
            })
            overlay_sections.append(
                f"### {meta.get('title', overlay_id)} ({overlay_id}@{active['version']})\n\n"
                f"<!-- skillbox-overlay:{overlay_id}@{active['version']} -->\n\n{content.strip()}"
            )

    prompt_parts = [base_content.rstrip(), "", OVERLAY_HEADER, ""]
    if overlay_sections:
        prompt_parts.extend([*overlay_sections, ""])
    else:
        prompt_parts.append("*(no approved overlays active for this context)*")
    prompt = "\n".join(prompt_parts)

    session_id = None
    if store is not None:
        session_id = store.get_or_create_session(skill=skill_name)

    effective = {
        "skill": skill_name,
        "base_source_path": str(base_path),
        "base_content_sha256": _sha256(base_content),
        "overlays": selected,
        "prompt": prompt,
        "composition_metadata": {
            "session_id": session_id,
            "context": project_context,
            "degraded": degraded,
            "duration_ms": int((time.time() - started) * 1000),
        },
    }

    if store is not None:
        try:
            store.record_event("composition_performed", {
                "skill": skill_name,
                "overlay_versions": [f"{o['overlay_id']}@{o['version']}" for o in selected],
                "context_detected": project_context,
                "degraded": degraded,
            }, skill=skill_name, session_id=session_id)
        except Exception as exc:
            logger.warning("composition telemetry deferred: %s", exc)
    return effective


def _match_collection(library, group: str, context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Select overlays whose collection matches the group and the context (FR-011)."""
    metas = library.all_meta()
    out = []
    for meta in metas:
        collection = meta.get("collection", "")
        overlay_id = meta["overlay_id"]
        if collection == group or overlay_id.split("/")[0] == group:
            out.append(meta)
            continue
        # technology-specific matching: language-python matches python context
        if group.startswith("language-"):
            lang = group.split("-", 1)[1]
            if lang in (context.get("language") or []):
                out.append(meta)
                continue
        if group.startswith("framework-"):
            fw = group.split("-", 1)[1]
            if fw in (context.get("framework") or []):
                out.append(meta)
    return out


def _compact(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def activate_skill(skill_name: str, repo_root: Path = Path(".")) -> Dict[str, Any]:
    """Public API — full composition with automatic context detection."""
    return _activate(skill_name, None, repo_root)


def activate_enhanced_skill(skill_name: str, project_context: Optional[Dict[str, Any]] = None,
                            repo_root: Path = Path(".")) -> Dict[str, Any]:
    """Public API — composition with explicit context (US4 variant)."""
    return _activate(skill_name, project_context, repo_root)


def _activate(skill_name: str, project_context: Optional[Dict[str, Any]],
              repo_root: Path) -> Dict[str, Any]:
    if project_context is not None and not isinstance(project_context, dict):
        raise SkillboxContractError("project_context must be a mapping")
    from src.skillbox.telemetry import default_store

    store = default_store()
    return compose_skill(skill_name, project_context=project_context, store=store,
                         repo_root=repo_root)
