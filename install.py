#!/usr/bin/env python
"""AI-SDLC toolkit installer — layer the toolkit into an existing git repository.

Copies self-contained components from this checkout into a target repo, adapts
the deployment knobs (EGM dependency paths, env config), and prints the
post-install bootstrap commands.

Rules (by design):
- `specs/` feature folders are never copied (per-repo workspace state);
- volatile runtime state is never copied (telemetry raw/sessions/db, learnings,
  reports, KG snapshots) — only the curated overlay lineage, audit chain and
  skill-registry baseline ship;
- an existing .github/copilot-instructions.md is never overwritten;
- component-owned paths that already exist in the target cause a refusal
  (idempotent safety) unless --force.

Usage:
    python install.py --target C:\\path\\to\\their-repo
    python install.py --target C:\\path\\to\\their-repo --components kg,skillbox
    python install.py --target . --components spec-kit,egm --dry-run

Exit codes: 0 installed (or dry-run plan); 1 validation refusal; 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Component id -> list of (source_rel, target_rel); forward-slash paths.
COMPONENTS: dict[str, list[tuple[str, str]]] = {
    "spec-kit": [
        (".specify", ".specify"),
    ],
    "egm": [
        ("egm", "egm"),
    ],
    "kg": [
        ("src/kg", "src/kg"),
        ("scripts/run_knowledge_graph.py", "scripts/run_knowledge_graph.py"),
        ("scripts/verify_knowledge_graph.py", "scripts/verify_knowledge_graph.py"),
        ("scripts/hooks/post-commit", "scripts/hooks/post-commit"),
        ("scripts/hooks/README.md", "scripts/hooks/README.md"),
        ("tests/kg", "tests/kg"),
        ("tests/fixtures/kg_query_suite.json", "tests/fixtures/kg_query_suite.json"),
    ],
    "skillbox": [
        ("src/skillbox", "src/skillbox"),
        ("scripts/skillbox_scheduler.py", "scripts/skillbox_scheduler.py"),
        ("scripts/skillbox_seed_overlay.py", "scripts/skillbox_seed_overlay.py"),
        ("scripts/verify_skillbox.py", "scripts/verify_skillbox.py"),
        ("tests/skillbox", "tests/skillbox"),
        (".skillbox/generated", ".skillbox/generated"),
        (".skillbox/overlays", ".skillbox/overlays"),
        (".skillbox/registry", ".skillbox/registry"),
        (".skillbox/telemetry/review_decisions.json",
         ".skillbox/telemetry/review_decisions.json"),
    ],
    "telemetry": [
        ("scripts/record_hook_acceptance.py", "scripts/record_hook_acceptance.py"),
        ("scripts/generate_lifecycle_map.py", "scripts/generate_lifecycle_map.py"),
        ("scripts/generate_sdlc_pane.py", "scripts/generate_sdlc_pane.py"),
        (".specify/contracts", ".specify/contracts"),
        ("tests/wiring", "tests/wiring"),
        ("docs/INDEX.md", "docs/INDEX.md"),
        ("docs/SDLC_ARTIFACT_CONVENTION.md", "docs/SDLC_ARTIFACT_CONVENTION.md"),
        ("docs/AI_SDLC_WIRING_FLOW.md", "docs/AI_SDLC_WIRING_FLOW.md"),
        ("docs/SPECKIT_EGM_COMPLEMENTARY_MODE.md", "docs/SPECKIT_EGM_COMPLEMENTARY_MODE.md"),
        ("docs/SKILLBOX_OVERVIEW.md", "docs/SKILLBOX_OVERVIEW.md"),
        ("docs/WIRING_HOOK_TELEMETRY.md", "docs/WIRING_HOOK_TELEMETRY.md"),
        ("dashboards", "dashboards"),
        ("data", "data"),
    ],
    "core": [
        ("src/__init__.py", "src/__init__.py"),
        ("src/log_config", "src/log_config"),
        ("config/__init__.py", "config/__init__.py"),
        ("config/settings.py", "config/settings.py"),
        ("config/environments", "config/environments"),
        ("tests/__init__.py", "tests/__init__.py"),
    ],
}

# Always brought along, regardless of --components (the toolkit cannot operate
# without the config layer; the wiring verifier is the universal gate; docs and
# requirements give the target a working surface).
ALWAYS = [
    ("src/__init__.py", "src/__init__.py"),
    ("src/log_config", "src/log_config"),
    ("config/__init__.py", "config/__init__.py"),
    ("config/settings.py", "config/settings.py"),
    ("config/environments", "config/environments"),
    ("tests/__init__.py", "tests/__init__.py"),
    ("scripts/verify_wiring.py", "scripts/verify_wiring.py"),
    ("LICENSE", "LICENSE"),
    ("README.md", "README.md"),
    ("requirements.txt", "requirements.txt"),
]

DEPENDS: dict[str, list[str]] = {
    "skillbox": [],
    "kg": [],
    "telemetry": ["spec-kit", "skillbox"],
    "spec-kit": [],
    "egm": [],
}

ORDER = ["spec-kit", "egm", "kg", "skillbox", "telemetry"]


def _p(rel: str) -> Path:
    return HERE / rel


def _norm(rel: str) -> str:
    return rel.replace("\\", "/")


def _resolve_components(requested: list[str]) -> list[str]:
    chosen: set[str] = set(requested)
    for name in requested:
        for dep in DEPENDS.get(name, []):
            chosen.add(dep)
    return [c for c in ORDER if c in chosen]


def _check_sources(names: list[str]) -> list[str]:
    missing = []
    for name in names:
        for src_rel, _ in COMPONENTS.get(name, []):
            if not _p(src_rel).exists():
                missing.append(f"{name}: {src_rel}")
    for src_rel, _ in ALWAYS:
        if not _p(src_rel).exists():
            missing.append(f"(always): {src_rel}")
    return missing


def _conflicts(target: Path, names: list[str], force: bool) -> list[str]:
    problems = []
    for _, dst_rel in ALWAYS:
        dst = target / _norm(dst_rel)
        if not force and dst.is_file():
            problems.append(f"{_norm(dst_rel)} already exists in target (use --force)")
    for name in names:
        for _src, dst_rel in COMPONENTS[name]:
            dst = target / _norm(dst_rel)
            if not force and (dst.is_file() or (dst.is_dir() and any(dst.iterdir()))):
                problems.append(f"{_norm(dst_rel)} already exists in target (use --force)")
    return problems


def _copy_entry(src: Path, dst: Path, force: bool) -> bool:
    if src.is_dir():
        if force and dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _adapt_specify(target: Path) -> None:
    """Point egm_dependency at the in-repo egm/ tree (repo-relative paths)."""
    manifest = target / ".specify" / "wiring-manifest.yml"
    if not manifest.exists():
        return
    text = manifest.read_text(encoding="utf-8")
    replacements = {
        "skills_root": "egm/skills",
        "agents_root": "egm/agents",
        "personas_root": "egm/personas/agents",
    }
    out_lines = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        key = stripped.split(":", 1)[0] if ":" in stripped else ""
        if key in replacements and "/.copilot/" in line:
            indent = line[: len(line) - len(line.lstrip())]
            nl = "\n" if line.endswith("\n") else ""
            out_lines.append(f"{indent}{key}: {replacements[key]}{nl}")
        else:
            out_lines.append(line)
    manifest.write_text("".join(out_lines), encoding="utf-8", newline="\n")


def _seed_env_config(target: Path) -> None:
    """Ensure config/environments/{development,production,testing}.json exist
    with an inert skillbox block (enabled only for production default)."""
    env_dir = target / "config" / "environments"
    env_dir.mkdir(parents=True, exist_ok=True)
    defaults = {"development": False, "production": True, "testing": False}
    for env, enabled in defaults.items():
        path = env_dir / f"{env}.json"
        if path.exists():
            continue
        path.write_text(json.dumps({
            "system": {"skillbox": {
                "enabled": enabled,
                "state_root": ".skillbox",
                "wiring_capture_enabled": False,
            }},
        }, indent=2) + "\n", encoding="utf-8", newline="\n")


def _seed_instructions(target: Path) -> None:
    """Starter .github/copilot-instructions.md when the target has none."""
    path = target / ".github" / "copilot-instructions.md"
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    from_here = HERE / ".github" / "copilot-instructions.md"
    if from_here.exists():
        shutil.copy2(from_here, path)
        return
    path.write_text(
        "# AI-SDLC Project Instructions\n\n"
        "See README.md for the component map (spec-kit, EGM, KG, SkillBox, telemetry).\n",
        encoding="utf-8", newline="\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True,
                    help="Path to the target git repository")
    ap.add_argument("--components", default="all",
                    help="Comma-separated ids: spec-kit,egm,kg,skillbox,telemetry "
                         "(default: all)")
    ap.add_argument("--dry-run", action="store_true", help="Print the plan, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite component-owned paths that already exist")
    args = ap.parse_args(argv)

    target = Path(args.target).resolve()
    if not target.exists():
        print(f"refusing: --target does not exist: {target}", file=sys.stderr)
        return 1
    if not args.dry_run and not (target / ".git").exists():
        print(f"refusing: --target is not a git repo (no .git): {target}\n"
              f"  run: git init {target}", file=sys.stderr)
        return 1

    requested = (list(ORDER) if args.components == "all"
                 else [c.strip() for c in args.components.split(",") if c.strip()])
    for name in requested:
        if name not in COMPONENTS:
            print(f"unknown component {name!r}; valid ids: "
                  f"{', '.join(sorted(COMPONENTS))}", file=sys.stderr)
            return 2
    names = _resolve_components(requested)

    missing = _check_sources(names)
    if missing:
        print("install.py sources missing (checkout incomplete?):", file=sys.stderr)
        print("\n".join(missing), file=sys.stderr)
        return 1
    if not args.dry_run:
        conflicts = _conflicts(target, names, force=args.force)
        if conflicts:
            print("refusing overwrite (idempotent-safety):", file=sys.stderr)
            print("\n".join(conflicts), file=sys.stderr)
            return 1

    copied: list[str] = []
    for src_rel, dst_rel in ALWAYS:
        if not args.dry_run:
            _copy_entry(_p(src_rel), target / _norm(dst_rel), force=args.force)
        copied.append(_norm(dst_rel))
    for name in names:
        for src_rel, dst_rel in COMPONENTS[name]:
            if not args.dry_run:
                _copy_entry(_p(src_rel), target / _norm(dst_rel), force=args.force)
            copied.append(_norm(dst_rel))

    if args.dry_run:
        print(f"DRY RUN — would install {len(copied)} paths into {target}:")
        for item in copied:
            print(f"  + {item}")
        return 0

    _adapt_specify(target)
    _seed_env_config(target)
    _seed_instructions(target)

    print(f"installed {len(copied)} paths into {target}")
    print()
    print("post-install bootstrap (run inside the target repo):")
    print("  python -m venv .venv && .venv\\Scripts\\pip install -r requirements.txt")
    print("  .venv\\Scripts\\python -m src.skillbox registry reindex")
    print("  .venv\\Scripts\\python scripts\\run_knowledge_graph.py build --full")
    print("  .venv\\Scripts\\python scripts\\run_knowledge_graph.py digest")
    print("  .venv\\Scripts\\python -m src.skillbox inject refresh")
    print("  .venv\\Scripts\\python scripts\\generate_lifecycle_map.py")
    print("  .venv\\Scripts\\python scripts\\verify_wiring.py")
    print()
    print("hook install (KG auto-stamp after every commit):")
    print("  Copy-Item scripts\\hooks\\post-commit .git\\hooks\\post-commit")
    print()
    print("customize after install:")
    print("  - README.md ownership + repo description")
    print("  - .specify/memory/constitution.md (your principles)")
    print("  - egm/ upstream pin if you track EGM upstream")
    return 0


if __name__ == "__main__":
    sys.exit(main())
