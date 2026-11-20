"""Tests for SkillBox context detection + composition (US4 — FR-009..FR-014)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.skillbox import SkillboxLookupError
from src.skillbox.compose import compose_skill
from src.skillbox.overlays import OverlayLibrary
from src.skillbox.registry import SkillRegistry
from src.skillbox.telemetry import default_store


@pytest.fixture()
def repo_env(tmp_path):
    """A fixture 'repo' with one upstream skill + markers."""
    repo = tmp_path / "repo"
    (repo / ".github" / "skills" / "demo-skill").mkdir(parents=True)
    (repo / ".github" / "skills" / "demo-skill" / "SKILL.md").write_text(
        "# Demo Skill\n\nDo demo things.\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("pandas>=2.0\nmlflow>=3.0\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    root = Path(tempfile.mkdtemp(prefix="skillbox-comp-"))
    store = default_store(root)
    reg = SkillRegistry(root / "registry", repo / ".github" / "skills")
    reg.load()
    reg.reindex()
    lib = OverlayLibrary(root).bind_store(store)
    yield repo, store, reg, lib
    store.close()


class TestContextDetection:
    def test_detect_python_from_real_repo(self):
        from src.skillbox.context import detect_context

        context = detect_context(Path("."))
        assert "python" in context["language"]
        assert context["detection_confidence"] in ("marker-exact", "heuristic")

    def test_node_markers(self, tmp_path):
        from src.skillbox.context import detect_context

        (tmp_path / "package.json").write_text(
            '{"dependencies": {"next": "14.0"}}', encoding="utf-8")
        context = detect_context(tmp_path)
        assert "typescript" in context["language"]
        assert "nextjs" in context["framework"]

    def test_no_markers_unknown(self, tmp_path):
        from src.skillbox.context import detect_context

        context = detect_context(tmp_path)
        assert context["language"] == []
        assert context["detection_confidence"] == "unknown"

    def test_multiple_languages_all_reported(self, tmp_path):
        from src.skillbox.context import detect_context

        (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        context = detect_context(tmp_path)
        assert set(context["language"]) >= {"python", "go"}  # conservative (edge case)


class TestComposition:
    def test_base_content_byte_identical(self, repo_env):
        repo, store, reg, lib = repo_env
        result = compose_skill("demo-skill", registry=reg, library=lib, store=store,
                               repo_root=repo)
        expected = (repo / ".github" / "skills" / "demo-skill" / "SKILL.md").read_text("utf-8")
        assert result["prompt"].startswith(expected.rstrip())  # SC-005
        sha = result["base_content_sha256"]
        assert sha.startswith("sha256:")

    def test_approved_overlay_included_attribution(self, repo_env):
        repo, store, reg, lib = repo_env
        from src.skillbox.review import ReviewManager

        lib.ensure_overlay("global/demo-basics", "Demo Basics", "global", ["LRN-1"])
        lib.stage_version("global/demo-basics", "- Always run the demo check.", ["LRN-1"],
                          {"frequency": 8, "success_rate": 0.9})
        rm = ReviewManager(store.root, library=lib, store=store)
        rm.approve_version("global/demo-basics", "v01", operator="schmo")
        result = compose_skill("demo-skill", registry=reg, library=lib, store=store,
                               repo_root=repo)
        assert "skillbox-overlay:global/demo-basics@v01" in result["prompt"]
        assert "- Always run the demo check." in result["prompt"]
        assert "## Overlay Guidance (SkillBox)" in result["prompt"]

    def test_pending_version_never_composes(self, repo_env):
        repo, store, reg, lib = repo_env
        lib.ensure_overlay("global/demo-basics", "Demo Basics", "global", ["LRN-1"])
        lib.stage_version("global/demo-basics", "- Pending guidance (must NOT appear).",
                          ["LRN-1"], {"frequency": 8, "success_rate": 0.9})
        result = compose_skill("demo-skill", registry=reg, library=lib, store=store,
                               repo_root=repo)
        assert "Pending guidance (must NOT appear)" not in result["prompt"]  # SC-014
        assert result["overlays"] == []

    def test_no_approved_overlay_skips_gracefully(self, repo_env):
        repo, store, reg, lib = repo_env
        result = compose_skill("demo-skill", registry=reg, library=lib, store=store,
                               repo_root=repo)
        assert result["overlays"] == []
        assert result["composition_metadata"]["degraded"] == []  # skip is normal, not degraded

    def test_unknown_skill_raises(self, repo_env):
        repo, store, reg, lib = repo_env
        with pytest.raises(SkillboxLookupError) as exc_info:
            compose_skill("nope-skill", registry=reg, library=lib, store=store,
                          repo_root=repo)
        assert "unknown_skill" in str(exc_info.value)

    def test_disabled_skill_raises(self, repo_env):
        repo, store, reg, lib = repo_env
        reg.set_override("demo-skill", enabled=False)
        with pytest.raises(SkillboxLookupError) as exc_info:
            compose_skill("demo-skill", registry=reg, library=lib, store=store,
                          repo_root=repo)
        assert "skill_disabled" in str(exc_info.value)

    def test_explicit_context_honored(self, repo_env):
        repo, store, reg, lib = repo_env
        context = {"language": ["python"], "framework": ["fastapi"], "database": ["postgres"],
                   "deployment_platform": [], "architecture_style": "service",
                   "repository_type": "single-project", "detection_confidence": "marker-exact"}
        result = compose_skill("demo-skill", project_context=context, registry=reg,
                               library=lib, store=store, repo_root=repo)
        assert result["composition_metadata"]["context"] == context

    def test_composition_telemetry_written(self, repo_env):
        repo, store, reg, lib = repo_env
        compose_skill("demo-skill", registry=reg, library=lib, store=store, repo_root=repo)
        events = store.query_events(event_type="composition_performed")
        assert events and events[0]["payload"]["skill"] == "demo-skill"

    def test_unreadable_overlay_degrades_fail_soft(self, repo_env):
        repo, store, reg, lib = repo_env
        from src.skillbox.review import ReviewManager

        lib.ensure_overlay("global/demo-broken", "Broken", "global", ["LRN-1"])
        lib.stage_version("global/demo-broken", "content", ["LRN-1"],
                          {"frequency": 8, "success_rate": 0.9})
        rm = ReviewManager(store.root, library=lib, store=store)
        rm.approve_version("global/demo-broken", "v01", operator="schmo")
        # delete the version content file -> load fails, composition continues
        (store.root / lib.get_version("global/demo-broken", "v01")["content_path"]).unlink()
        result = compose_skill("demo-skill", registry=reg, library=lib, store=store,
                               repo_root=repo)
        assert any(d.startswith("DEGRADE_OVERLAY_LOAD") for d in
                   result["composition_metadata"]["degraded"])
        assert result["prompt"]  # base guidance still returned (FR-013/FR-051)
