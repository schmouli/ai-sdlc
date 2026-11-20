"""SkillBox configuration access (research R12).

Reads the ``system.skillbox`` block from the existing environment config manager
(``config.settings.get_config_manager``) and applies environment-variable overrides:

- ``MT4_SKILLBOX_ENABLED``       (0/false -> runtime inert)
- ``MT4_SKILLBOX_STATE_ROOT``    (test isolation; default from config)
- ``MT4_SKILLBOX_MLFLOW_ENABLED`` / ``MT4_SKILLBOX_MLFLOW_EXPERIMENT`` /
  ``MT4_SKILLBOX_MLFLOW_TRACKING_URI`` (push sink overrides)
- ``SKILLBOX_OPERATOR``          (HITL review identity; research R11)

All helpers are failure-tolerant: config load problems disable the runtime
(fail-soft, constitution Principle V) instead of raising into skill activations.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("skillbox.config")

DEFAULT_STATE_ROOT = ".skillbox"
DEFAULT_EXPERIMENT = "skillbox-agent-learning"

_STATE_ROOT_DIRS = (
    "registry",
    "telemetry/raw",
    "telemetry/decisions",
    "telemetry/sessions",
    "telemetry/metrics",
    "telemetry/quarantine",
    "telemetry/dashboards",
    "learnings/successes",
    "learnings/failures",
    "learnings/architecture",
    "learnings/performance",
    "learnings/security",
    "learnings/retrospectives",
    "overlays/global",
    "overlays/architecture",
    "overlays/testing",
    "overlays/security",
    "overlays/performance",
    "overlays/project",
    "overlays/language/python",
    "overlays/language/typescript",
    "overlays/language/csharp",
    "overlays/language/java",
    "overlays/language/go",
    "overlays/frameworks/react",
    "overlays/frameworks/nextjs",
    "overlays/frameworks/angular",
    "overlays/frameworks/fastapi",
    "overlays/frameworks/django",
    "overlays/frameworks/spring",
    "overlays/frameworks/dotnet",
    "generated",
    "reports",
)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _env_flag(name: str) -> Optional[bool]:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def _load_system_config() -> Dict[str, Any]:
    """Load the ``system`` section of the active environment config (fail-soft)."""
    try:
        from config.settings import get_config_manager

        manager = get_config_manager()
        system = manager.config.get("system", {}) if hasattr(manager, "config") else {}
        if not isinstance(system, dict):
            system = {}
        return system
    except Exception as exc:  # pragma: no cover - defensive; config outages degrade
        logger.warning("skillbox config load degraded: %s", exc)
        return {}


def enabled() -> bool:
    """Master switch: ``system.skillbox.enabled`` (default False) or env override."""
    env_flag = _env_flag("MT4_SKILLBOX_ENABLED")
    if env_flag is not None:
        return env_flag
    system = _load_system_config()
    block = system.get("skillbox", {})
    if isinstance(block, dict):
        return bool(block.get("enabled", False))
    return False


def get_skillbox_config() -> Dict[str, Any]:
    """The merged ``system.skillbox`` block with schema defaults applied."""
    system = _load_system_config()
    block = system.get("skillbox", {})
    if not isinstance(block, dict):
        block = {}
    merged: Dict[str, Any] = {
        "enabled": bool(block.get("enabled", False)),
        "wiring_capture_enabled": bool(block.get("wiring_capture_enabled", False)),
        "state_root": block.get("state_root", DEFAULT_STATE_ROOT),
        "mlflow": {
            "enabled": True,
            "experiment": DEFAULT_EXPERIMENT,
            "tracking_uri": None,
            "batch_interval_seconds": 60,
        },
        "learning": {
            "window_days": 90,
            "min_frequency": 5,
            "min_consistency": 0.6,
            "reason_cluster_threshold": 0.5,
            "recompute_interval_hours": 24,
        },
        "review": {"pending_review_sla_days": 7},
        "scheduler": {"loop_seconds": 300, "reindex_interval_hours": 24},
    }
    for section in ("mlflow", "learning", "review", "scheduler"):
        override = block.get(section)
        if isinstance(override, dict):
            merged[section].update(override)
    return merged


def mlflow_config() -> Dict[str, Any]:
    """Merged push-sink config with ``MT4_SKILLBOX_MLFLOW_*`` env overrides."""
    cfg = get_skillbox_config()["mlflow"]
    env_flag = _env_flag("MT4_SKILLBOX_MLFLOW_ENABLED")
    if env_flag is not None:
        cfg["enabled"] = env_flag
    env_exp = os.environ.get("MT4_SKILLBOX_MLFLOW_EXPERIMENT")
    if env_exp:
        cfg["experiment"] = env_exp
    env_uri = os.environ.get("MT4_SKILLBOX_MLFLOW_TRACKING_URI")
    if env_uri:
        cfg["tracking_uri"] = env_uri
    return cfg


def state_root() -> Path:
    """Resolved state-root directory (``MT4_SKILLBOX_STATE_ROOT`` overrides config)."""
    env_root = os.environ.get("MT4_SKILLBOX_STATE_ROOT")
    if env_root:
        return Path(env_root)
    cfg = get_skillbox_config()
    return Path(cfg.get("state_root", DEFAULT_STATE_ROOT))


def ensure_state_root(root: Optional[Path] = None) -> Path:
    """Idempotently create the full `.skillbox/` directory tree (T003, FR-048).

    Accepts an explicit ``root`` for test isolation (temp dirs); otherwise the
    configured state root is used. Returns the resolved root path.
    """
    root = Path(root) if root is not None else state_root()
    for rel in _STATE_ROOT_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def operator_identity() -> Optional[str]:
    """Explicit operator identity for HITL review decisions (R11; never inferred)."""
    raw = os.environ.get("SKILLBOX_OPERATOR")
    if raw and raw.strip():
        return raw.strip()
    return None
