"""SkillBox scheduler: continuous learning-loop maintenance (research R14).

Mirrors scripts/learning_scheduler.py conventions: `--loop N` / `--once`.
Pipeline per pass (each stage fail-soft, FR-051):
    reindex-if-due -> stitch inferred events -> batch MLflow push ->
    learnings when due -> overlay staging when due -> metrics + reports ->
    pending-review digest

NOTE: this pipeline NEVER approves overlay versions — activation is
exclusively the human review path (FR-034).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.skillbox.learning import LearningEngine          # noqa: E402
from src.skillbox.metrics import SkillBoxMetrics           # noqa: E402
from src.skillbox.mlflow_sink import MlflowSink            # noqa: E402
from src.skillbox.overlays import OverlayLibrary           # noqa: E402
from src.skillbox.registry import SkillRegistry            # noqa: E402
from src.skillbox.telemetry import default_store           # noqa: E402

logger = logging.getLogger("skillbox.scheduler")

NEVER_APPROVES_NOTICE = (
    "SkillBox scheduler: overlay activation is EXCLUSIVELY the human review "
    "path (spec FR-034) — this pipeline stages pending versions only."
)


def setup_logging() -> None:
    from src.log_config.logger_config import ensure_file_handler

    ensure_file_handler("skillbox", "logs/skillbox_scheduler.log", level="INFO")


def run_pass(state_root: Optional[Path] = None, force_learnings: bool = False) -> dict:
    """One scheduler pass; every stage independent + fail-soft (R14)."""
    from src.skillbox.config import get_skillbox_config, state_root as cfg_root

    root = Path(state_root) if state_root is not None else cfg_root()
    cfg = get_skillbox_config()
    store = default_store(root)
    stages: dict = {}

    # 1. reindex-if-due
    try:
        reg = SkillRegistry(root / "registry", REPO_ROOT / ".github" / "skills")
        reg.load()
        stages["reindex"] = reg.ensure_indexed()
    except Exception as exc:
        logger.warning("reindex stage degraded: %s", exc)
        stages["reindex"] = {"error": str(exc)}

    # 2. stitch inferred events (passive activity reconciliation)
    try:
        inferred = infer_skill_activity(store)
        stages["inferred_events"] = inferred
    except Exception as exc:
        logger.warning("inference stage degraded: %s", exc)
        stages["inferred_events"] = 0

    # 3. batch MLflow push
    try:
        sink = MlflowSink(cfg.get("mlflow", {})).bind_store(store)
        stages["pushed"] = sink.flush_queue(store)
    except Exception as exc:
        logger.warning("push stage degraded: %s", exc)
        stages["pushed"] = 0

    # 4. learnings when due (recompute_interval_hours, daily at most)
    lib = None
    try:
        engine = LearningEngine(store, root / "learnings", cfg.get("learning", {}))
        if force_learnings or _learning_due(root):
            learnings = engine.extract_learnings()
            stages["learnings"] = len(learnings)
            _stamp(root, "last_learning_run")
        else:
            stages["learnings"] = "skipped (not due)"
    except Exception as exc:
        logger.warning("learnings stage degraded: %s", exc)
        stages["learnings"] = "degraded"

    # 5. overlay staging when due (stage PENDING only; never activates — FR-034)
    try:
        lib = OverlayLibrary(root).bind_store(store)
        if isinstance(stages.get("learnings"), int):
            from src.skillbox.cli import generate_overlays

            result = generate_overlays(lib, engine)
            stages["overlays_staged"] = len(result["staged"])
        else:
            stages["overlays_staged"] = 0
    except Exception as exc:
        logger.warning("overlay staging degraded: %s", exc)
        stages["overlays_staged"] = 0

    # 6. metrics + reports + pending digest
    try:
        metrics = SkillBoxMetrics(store)
        from src.skillbox.reports import refresh_dashboards

        outputs = refresh_dashboards(root, metrics)
        stages["reports"] = outputs
    except Exception as exc:
        logger.warning("reports stage degraded: %s", exc)
        stages["reports"] = "degraded"

    # 7. IDE auto-injection refresh (approved overlays only — never pending)
    try:
        from src.skillbox.inject import refresh as inject_refresh

        stages["injection"] = inject_refresh(root, store=None)
    except Exception as exc:
        logger.warning("injection stage degraded: %s", exc)
        stages["injection"] = "degraded"

    # 8. Wiring sweep (feature 005 US4): round summaries -> egm_review_round events
    if cfg.get("wiring_capture_enabled"):
        try:
            from src.skillbox.sweep import sweep_rounds

            # drop dir resolves from the manifest artifacts (never hardcoded)
            drop_dir = Path("logs/egm_rounds")
            man_path = REPO_ROOT / ".specify" / "wiring-manifest.yml"
            budgets = {}
            if man_path.exists():
                import yaml as _yaml

                manifest = _yaml.safe_load(man_path.read_text(encoding="utf-8"))
                budgets = (manifest.get("token_budgets") or {})
                drop_rel = ((manifest.get("artifacts") or {}).get("round_drop_dir") or {}).get("path")
                if drop_rel:
                    drop_dir = REPO_ROOT / drop_rel
            state_path = root / "wiring" / "sweep_state.json"
            result = sweep_rounds(
                drop_dir, store, state_path,
                retention_days=int(budgets.get("retention_days", 30)),
            )
            for skipped in result.get("skipped") or []:
                logger.warning("sweep skipped %s: %s", skipped.get("file"), skipped.get("reason"))
            stages["wiring_sweep"] = {
                "recorded": result.get("recorded", 0),
                "cleaned": result.get("cleaned", 0),
                "pruned": result.get("pruned", 0),
                "skipped": len(result.get("skipped") or []),
            }
        except Exception as exc:
            logger.warning("wiring sweep degraded: %s", exc)
            stages["wiring_sweep"] = "degraded"
    else:
        stages["wiring_sweep"] = "inert (capture disabled)"

    store.close()
    logger.info("scheduler pass complete: %s", json.dumps(stages, default=str)[:400])
    return stages


def _learning_due(root: Path) -> bool:
    stamp = root / "generated" / ".last_learning_run"
    interval = float(_config_learning_interval())
    if not stamp.exists():
        return True
    age_hours = (time.time() - stamp.stat().st_mtime) / 3600
    return age_hours >= interval


def _config_learning_interval() -> float:
    try:
        from src.skillbox.config import get_skillbox_config

        return float(get_skillbox_config().get("learning", {})
                     .get("recompute_interval_hours", 24))
    except Exception:
        return 24.0


def _stamp(root: Path, name: str) -> None:
    stamp = root / "generated" / f".{name}"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(str(time.time()), encoding="utf-8")


def infer_skill_activity(store, repo_root: Optional[Path] = None,
                         window_hours: int = 2) -> int:
    """R3 pathway 2 — delegates to src/skillbox/telemetry.py (module owner, T016).

    Kept here for scheduler-process callers; the implementation lives in
    telemetry.py so both the API surface and the CLI share one code path.
    """
    from src.skillbox.telemetry import infer_skill_activity as _infer

    return _infer(store, repo_root=repo_root, window_hours=window_hours)



def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SkillBox learning-loop scheduler")
    parser.add_argument("--loop", type=int, default=None,
                        help="continuous mode; seconds between passes")
    parser.add_argument("--once", action="store_true", help="single pass then exit")
    parser.add_argument("--force-learnings", action="store_true")
    args = parser.parse_args(argv)

    setup_logging()
    logger.info(NEVER_APPROVES_NOTICE)

    from src.skillbox import config as skillbox_config

    if not skillbox_config.enabled():
        logger.info("SkillBox disabled — scheduler no-op (FR-049 inert mode)")
        return 0

    if args.once:
        stages = run_pass(force_learnings=args.force_learnings)
        print("pass complete:", json.dumps(stages, default=str))
        return 0
    interval = args.loop or float(getattr(skillbox_config, "loop_seconds", 300))
    try:
        while True:
            started = time.time()
            try:
                run_pass(force_learnings=args.force_learnings)
            except Exception as exc:
                logger.error("scheduler pass failed (continuing): %s", exc)
            elapsed = time.time() - started
            time.sleep(max(5, float(interval) - elapsed))
    except KeyboardInterrupt:
        logger.info("scheduler stopped by operator")
        return 0


if __name__ == "__main__":
    sys.exit(main())
