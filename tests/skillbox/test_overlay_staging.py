"""Tests for SkillBox overlay staging (US2 — append-only lineage, FR-032/FR-033/FR-029)."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from src.skillbox.overlays import OverlayLibrary, overlay_dir
from src.skillbox.telemetry import default_store


@pytest.fixture()
def env():
    root = Path(tempfile.mkdtemp(prefix="skillbox-ovl-"))
    store = default_store(root)
    lib = OverlayLibrary(root).bind_store(store)
    yield store, lib, root
    store.close()


class TestStaging:
    def test_stage_v01_pending(self, env):
        store, lib, root = env
        entry = lib.stage_version("security/authentication-lessons",
                                  "# Authentication Lessons\n- Always test token refresh.",
                                  ["LRN-1"], {"frequency": 8, "success_rate": 0.9})
        assert entry["status"] == "pending"
        assert entry["version"] == "v01"
        assert entry["pending_since"]
        content_path = root / entry["content_path"]
        assert content_path.exists()

    def test_immutable_existing_version_regen_makes_v02(self, env):
        store, lib, root = env
        v1 = lib.stage_version("security/authentication-lessons", "content-one",
                               ["LRN-1"], {"frequency": 8, "success_rate": 0.9})
        v2 = lib.stage_version("security/authentication-lessons", "content-two",
                               ["LRN-1"], {"frequency": 9, "success_rate": 0.9})
        assert v2["version"] == "v02"
        v1_content = (root / v1["content_path"]).read_text(encoding="utf-8")
        assert v1_content == "content-one"  # never overwritten (FR-032)
        lib.stage_version("security/authentication-lessons", "content-two",
                          ["LRN-1"], {"frequency": 9, "success_rate": 0.9})
        versions = [e["version"] for e in lib.list_versions("security/authentication-lessons")]
        assert versions == ["v01", "v02"]  # unchanged content -> no new version (SC-004)

    def test_unchanged_telemetry_no_new_versions(self, env):
        store, lib, root = env
        lib.stage_version("testing/token-refresh", "stable-guidance", ["LRN-9"],
                          {"frequency": 6, "success_rate": 0.8})
        result = lib.stage_version("testing/token-refresh", "stable-guidance", ["LRN-9"],
                                   {"frequency": 6, "success_rate": 0.8})
        assert result["version"] == "v01"
        assert len(lib.list_versions("testing/token-refresh")) == 1

    def test_invalid_overlay_id_rejected(self, env):
        store, lib, root = env
        with pytest.raises(ValueError):
            lib.stage_version("../escape", "x", [], {})

    def test_registred_overlays_listed(self, env):
        store, lib, root = env
        lib.ensure_overlay("global/basics", "Basics", "global", ["LRN-2"])
        assert lib.get_meta("global/basics")["collection"] == "global"
        assert any(m["overlay_id"] == "global/basics" for m in lib.all_meta())


class TestResolutionFailSafe:
    def test_pending_never_resolves_active(self, env):
        store, lib, root = env
        lib.stage_version("security/authentication-lessons", "pending-only", ["LRN-1"],
                          {"frequency": 8, "success_rate": 0.9})
        assert lib.resolve_active_version("security/authentication-lessons") is None

    def test_corrupt_audit_fails_safe_to_last_approved(self, env):
        store, lib, root = env
        v1 = lib.stage_version("security/authentication-lessons", "one", ["LRN-1"],
                               {"frequency": 8, "success_rate": 0.9})
        from src.skillbox.review import ReviewManager

        rm = ReviewManager(root, library=lib, store=store)
        rm.approve_version("security/authentication-lessons", "v01", operator="schmo")
        v2 = lib.stage_version("security/authentication-lessons", "two", ["LRN-2"],
                               {"frequency": 9, "success_rate": 0.9})
        assert lib.resolve_active_version("security/authentication-lessons")["version"] == "v01"
        # corrupt the audit file (truncate)
        audit = root / "telemetry" / "review_decisions.json"
        audit.write_text("{corrupt", encoding="utf-8")
        resolved = lib.resolve_active_version("security/authentication-lessons")
        assert resolved is not None and resolved["version"] == "v01"  # last verifiable (FR-051)
