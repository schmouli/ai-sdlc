"""Installer tests: source integrity, dependency resolution, conflict refusal,
adaptations, and a full simulated install into a temp git repo."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import install as installer  # noqa: E402


def test_every_component_source_exists():
    """The installer's component map must never point at a missing file."""
    missing = installer._check_sources(list(installer.ORDER))
    assert not missing, missing


def test_resolve_components_includes_dependency():
    got = installer._resolve_components(["telemetry"])
    assert got == ["spec-kit", "skillbox", "telemetry"], got


def test_resolve_components_all_order():
    assert installer._resolve_components(["egm", "kg"]) == ["egm", "kg"]


def test_unknown_component_error(capsys):
    assert installer.main(["--target", ".", "--components", "bogus"]) == 2
    assert "unknown component" in capsys.readouterr().err


def test_target_not_git_repo_refused(tmp_path, capsys):
    bare = tmp_path / "notagit"
    bare.mkdir()
    assert installer.main(["--target", str(bare)]) == 1
    assert "not a git repo" in capsys.readouterr().err


def test_dry_run_writes_nothing(tmp_path, capsys):
    target = tmp_path / "tgt"
    (target / ".git").mkdir(parents=True)
    rc = installer.main(["--target", str(target), "--dry-run"])
    assert rc == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert not (target / "src" / "kg").exists()


def test_conflict_refused_without_force(tmp_path, capsys):
    target = tmp_path / "tgt"
    (target / ".git").mkdir(parents=True)
    (target / "src").mkdir()
    (target / "src" / "__init__.py").write_text("# pre-existing\n", encoding="utf-8")
    assert installer.main(["--target", str(target)]) == 1
    assert "refusing overwrite" in capsys.readouterr().err


def test_conflict_allowed_with_force(tmp_path):
    target = tmp_path / "tgt"
    (target / ".git").mkdir(parents=True)
    (target / "src").mkdir()
    (target / "src" / "__init__.py").write_text("# pre-existing\n", encoding="utf-8")
    assert installer.main(["--target", str(target), "--force"]) == 0
    assert "# pre-existing" not in (target / "src" / "__init__.py").read_text(encoding="utf-8")


@pytest.fixture()
def target_repo(tmp_path):
    target = tmp_path / "simulated-target"
    (target / ".git").mkdir(parents=True)
    yield target


EXPECTED_PATHS = (
    ".specify/wiring-manifest.yml",
    ".specify/contracts/wiring-manifest-schema.md",
    ".specify/memory/constitution.md",
    "egm/skills/egm-review-loop/SKILL.md",
    "egm/agents/egm-zoom-out.agent.md",
    "src/kg/retrieval.py",
    "src/kg/graph_builder.py",
    "src/skillbox/compose.py",
    "src/skillbox/review.py",
    "scripts/run_knowledge_graph.py",
    "scripts/verify_wiring.py",
    "scripts/verify_skillbox.py",
    "scripts/skillbox_scheduler.py",
    "scripts/record_hook_acceptance.py",
    "scripts/generate_lifecycle_map.py",
    "scripts/generate_sdlc_pane.py",
    "scripts/hooks/post-commit",
    "tests/kg/test_digest.py",
    "tests/skillbox/test_compose.py",
    "tests/wiring/test_wiring_manifest.py",
    "tests/fixtures/kg_query_suite.json",
    ".skillbox/overlays/testing/engineering-best-practices/index.json",
    ".skillbox/overlays/testing/engineering-best-practices/versions/v01.md",
    ".skillbox/telemetry/review_decisions.json",
    ".skillbox/registry/skills.json",
    "config/environments/production.json",
    ".github/copilot-instructions.md",
    "docs/AI_SDLC_WIRING_FLOW.md",
    "requirements.txt",
)


def test_full_install_copies_expected_tree(target_repo):
    assert installer.main(["--target", str(target_repo)]) == 0
    for rel in EXPECTED_PATHS:
        assert (target_repo / rel).exists(), rel


def test_install_does_not_copy_volatile_state(target_repo):
    installer.main(["--target", str(target_repo)])
    kg_repo = target_repo / "src" / "kg"
    assert kg_repo.exists()
    # nothing under .skillbox telemetry except the audit chain
    telemetry = target_repo / ".skillbox" / "telemetry"
    if telemetry.exists():
        assert [p.name for p in telemetry.iterdir()] == ["review_decisions.json"]
    assert not (target_repo / "output").exists()
    assert not (target_repo / ".specify" / "feature.json").exists()


def test_install_specs_dir_excluded(target_repo):
    installer.main(["--target", str(target_repo)])
    assert not (target_repo / "specs").exists()


def test_adapt_specify_rewrites_egm_roots(target_repo):
    installer.main(["--target", str(target_repo)])
    manifest = yaml.safe_load(
        (target_repo / ".specify" / "wiring-manifest.yml").read_text(encoding="utf-8"))
    dep = manifest["egm_dependency"]
    assert dep["skills_root"] == "egm/skills"
    assert dep["agents_root"] == "egm/agents"
    assert dep["personas_root"] == "egm/personas/agents"


def test_seed_env_configs_created(target_repo):
    installer.main(["--target", str(target_repo)])
    for env in ("development", "production", "testing"):
        cfg = json.loads((target_repo / "config" / "environments" / f"{env}.json").read_text(encoding="utf-8"))
        assert "skillbox" in cfg["system"]


def test_existing_instructions_not_overwritten(tmp_path):
    target = tmp_path / "tgt"
    (target / ".git").mkdir(parents=True)
    gh = target / ".github"
    gh.mkdir()
    original = "# My Repo Rules\n"
    (gh / "copilot-instructions.md").write_text(original, encoding="utf-8")
    installer.main(["--target", str(target)])
    assert (gh / "copilot-instructions.md").read_text(encoding="utf-8") == original


def test_installed_repo_bootstrap_commands_run(target_repo):
    """The simulated target must reach the same end-state the bootstrap
    commands produce: registry indexes, KG builds, digest renders, verifier
    passes. Runs the real commands (not mocks) — slow but honest."""
    installer.main(["--target", str(target_repo)])
    py = sys.executable
    env = {"METATRADER_ENV": "production", "PATH": __import__("os").environ["PATH"],
           "PYTHONPATH": str(target_repo)}

    def run(*cmd, expect=0):
        r = subprocess.run([py, *cmd], cwd=str(target_repo), env=env,
                           capture_output=True, text=True)
        assert r.returncode == expect, f"{cmd}: {r.stdout[-400:]} {r.stderr[-400:]}"
        return r.stdout

    run("-m", "src.skillbox", "registry", "reindex")
    run("scripts/run_knowledge_graph.py", "build", "--full")
    run("scripts/run_knowledge_graph.py", "digest")
    run("-m", "src.skillbox", "inject", "refresh")
    run("scripts/generate_lifecycle_map.py")
    run("scripts/verify_wiring.py")
    assert "VERIFY WIRING: PASS" in run("scripts/verify_wiring.py")
