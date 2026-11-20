"""SkillBox ProjectContext detection (research R8; FR-010/FR-011).

Marker-file heuristics — deterministic, no manual config. All detected
characteristics reported (conservative multi-detection); ``unknown`` when no
marker matches (never guesses).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("skillbox.context")

_FRAMEWORK_MARKERS = ("fastapi", "django", "flask", "streamlit", "scikit-learn",
                      "xgboost", "schedule", "networkx", "mlflow")
_NODE_FRAMEWORK_MAP = {"next": "nextjs", "react": "react", "@angular/core": "angular",
                       "vue": "vue"}


def detect_context(repo_root: Path) -> Dict[str, Any]:
    """Detect ProjectContext (E11) from repository markers."""
    repo_root = Path(repo_root)
    languages: List[str] = []
    frameworks: List[str] = []
    databases: List[str] = []
    deploy: List[str] = []
    confidence = "unknown"

    # --- Python
    req = repo_root / "requirements.txt"
    pyproject = repo_root / "pyproject.toml"
    setup_py = repo_root / "setup.py"
    src_dir = repo_root / "src"
    python_markers = [p for p in (req, pyproject, setup_py) if p.exists()]
    has_python_pkg = src_dir.exists() and any(1 for _ in src_dir.glob("*/__init__.py"))
    if python_markers or has_python_pkg:
        languages.append("python")
        confidence = "marker-exact"
    if req.exists():
        content = req.read_text(encoding="utf-8", errors="replace").lower()
        for marker in _FRAMEWORK_MARKERS:
            if marker in content:
                frameworks.append(marker)
        if "psycopg" in content or "postgres" in content:
            databases.append("postgres")
        if "pymysql" in content or "mysql" in content:
            databases.append("mysql")
        if "sqlite3" in content or not databases:
            databases.append("sqlite")

    # --- Node / TypeScript
    pkg = repo_root / "package.json"
    if pkg.exists():
        try:
            import json

            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
            languages.append("typescript")
            for key, fw in _NODE_FRAMEWORK_MAP.items():
                if key in deps:
                    frameworks.append(fw)
            confidence = "marker-exact"
        except (json.JSONDecodeError, OSError):
            languages.append("typescript")
            confidence = "heuristic"

    # --- JVM
    if (repo_root / "pom.xml").exists() or (repo_root / "build.gradle").exists():
        languages.append("java")
        confidence = "marker-exact"
    # --- Go
    if (repo_root / "go.mod").exists():
        languages.append("go")
        confidence = "marker-exact"
    # --- C#
    if list(repo_root.glob("*.csproj")):
        languages.append("csharp")
        confidence = "marker-exact"

    # --- deployment markers
    if list(repo_root.glob("Dockerfile*")):
        deploy.append("docker")
    if (repo_root / ".github" / "workflows").exists():
        deploy.append("gh-actions")

    # --- architecture style / repo type (R8 heuristics)
    architecture_style = "unknown"
    if (repo_root / "scripts").exists() and (repo_root / "src").exists():
        architecture_style = "cli-library"
    if list(repo_root.glob("apps/*")):
        architecture_style = "monorepo"
    repository_type = "monorepo" if architecture_style == "monorepo" else "single-project"

    if not languages:
        confidence = "unknown"

    return {
        "language": languages,
        "framework": frameworks,
        "database": databases or ["unknown"],
        "deployment_platform": deploy or ["none-detected"],
        "architecture_style": architecture_style,
        "repository_type": repository_type,
        "detection_confidence": confidence,
    }
