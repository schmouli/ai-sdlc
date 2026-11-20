"""Tests for the SkillBox IDE auto-injection bridge (US4 extension)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from src.skillbox.inject import (BEGIN_MARKER, END_MARKER, compose_digest, refresh,
                                 render_block)
from src.skillbox.overlays import OverlayLibrary
from src.skillbox.review import ReviewManager
from src.skillbox.telemetry import default_store


@pytest.fixture()
def env(tmp_path):
    """A fixture repo: fake copilot-instructions + seeded overlay lineage."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".github").mkdir()
    (repo / ".github" / "copilot-instructions.md").write_text(
        "# Base Instructions\n\nKeep the base content.\n", encoding="utf-8")
    root = repo / ".skillbox"  # injection + review share one state root
    store = default_store(root)
    lib = OverlayLibrary(root).bind_store(store)
    yield repo, store, lib
    store.close()


def _stage_and_approve(lib, repo, overlay_id="testing/engineering-best-practices",
                       content="# Best Practices\n\n## Recommendation\n\n"
                               "- Verify before assuming.\n- Test-first for regressions."):
    state_root = repo / ".skillbox"
    lib.ensure_overlay(overlay_id, title="Best Practices", collection=overlay_id.split("/")[0],
                       source_learning_ids=["LRN-SEED"])
    entry = lib.stage_version(overlay_id, content, ["LRN-SEED"],
                              {"seed": True, "frequency": None, "success_rate": None})
    rm = ReviewManager(state_root, library=lib)
    rm.approve_version(overlay_id, entry["version"], operator="schmo",
                       rationale="seed approval")
    return entry


class TestApprovedOnlyInjection:
    def test_pending_never_injects(self, env):
        repo, store, lib = env
        lib.ensure_overlay("testing/pending-overlay", "P", "testing", ["LRN-1"])
        lib.stage_version("testing/pending-overlay", "- pending guidance must not inject",
                          ["LRN-1"], {"frequency": None, "success_rate": None})
        result = refresh(repo, library=lib, store=store)
        target = repo / ".github" / "copilot-instructions.md"
        text = target.read_text(encoding="utf-8")
        assert BEGIN_MARKER in text and END_MARKER in text  # an empty digest block is written
        assert "pending guidance must not inject" not in text  # SC-014

    def test_approved_content_injects(self, env):
        repo, store, lib = env
        _stage_and_approve(lib, repo)
        refresh(repo, library=lib, store=store)
        text = (repo / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert "Verify before assuming." in text
        assert "testing/engineering-best-practices@v01" in text
        assert text.index("# Base Instructions") < text.index("Overlay Guidance")

    def test_base_content_untouched(self, env):
        repo, store, lib = env
        original = (repo / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        _stage_and_approve(lib, repo)
        refresh(repo, library=lib, store=store)
        text = (repo / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert text.startswith(original.rstrip("\n"))  # base bytes preserved

    def test_idempotent_refresh(self, env):
        repo, store, lib = env
        _stage_and_approve(lib, repo)
        first = refresh(repo, library=lib, store=store)
        snapshot = (repo / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        second = refresh(repo, library=lib, store=store)
        again = (repo / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert first["changed"] is True and second["changed"] is False
        assert snapshot == again

    def test_rollback_removes_from_injection(self, env):
        repo, store, lib = env
        entry = _stage_and_approve(lib, repo)
        refresh(repo, library=lib, store=store)
        text1 = (repo / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert "Verify before assuming." in text1
        # disapprove: rollback path — reject v01 by staging v02 approved then rejecting v01 is
        # not possible; instead retire the overlay via evidence-decay cascade
        from src.skillbox.overlays import cascade_retire_for_learning

        cascade_retire_for_learning(store, "LRN-SEED")
        refresh(repo, library=lib, store=store)
        text2 = (repo / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert "Verify before assuming." not in text2  # retired -> not injected


class TestBlockIntegrity:
    def test_block_replaced_not_duplicated(self, env):
        repo, store, lib = env
        _stage_and_approve(lib, repo)
        lib.stage_version("testing/engineering-best-practices",
                          "# BP v2\n\n## Recommendation\n\n- NEW GUIDANCE",
                          ["LRN-SEED"], {"seed": True, "frequency": None, "success_rate": None})
        ReviewManager(repo / ".skillbox").approve_version(
            "testing/engineering-best-practices", "v02", operator="schmo")
        refresh(repo, library=lib, store=store)
        refresh(repo, library=lib, store=store)
        text = (repo / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert text.count(BEGIN_MARKER) == 1 and text.count(END_MARKER) == 1
        assert "NEW GUIDANCE" in text and "Verify before assuming." not in text

    def test_degraded_fail_soft(self, env, monkeypatch):
        repo, store, lib = env
        monkeypatch.setattr("src.skillbox.inject.compose_digest",
                            lambda _lib: (_ for _ in ()).throw(RuntimeError("boom")))
        result = refresh(repo, library=lib, store=store)
        assert result["changed"] is False and "error" in result  # block left intact
        text = (repo / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert "Keep the base content." in text


class TestDigest:
    def test_digest_shape(self, env):
        repo, store, lib = env
        _stage_and_approve(lib, repo)
        digest = compose_digest(lib)
        assert len(digest) == 1
        assert digest[0]["version"] == "v01"
        assert digest[0]["bullets"]
