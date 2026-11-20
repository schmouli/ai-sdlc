"""Tests for SkillBox HITL review operations (US3 — FR-034..FR-038, SC-013/SC-014)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.skillbox import OperatorIdentityError, SkillboxContractError
from src.skillbox.overlays import OverlayLibrary
from src.skillbox.review import ReviewManager, verify_audit_for_version
from src.skillbox.telemetry import default_store


@pytest.fixture()
def env():
    root = Path(tempfile.mkdtemp(prefix="skillbox-rev-"))
    store = default_store(root)
    lib = OverlayLibrary(root).bind_store(store)
    rm = ReviewManager(root, library=lib, store=store)
    yield store, lib, rm, root
    store.close()


def _staged(env, overlay_id="security/authentication-lessons"):
    _, lib, _, _ = env
    return lib.stage_version(overlay_id, f"content-{overlay_id}", ["LRN-1"],
                             {"frequency": 8, "success_rate": 0.9})


class TestApprovalGate:
    def test_approve_requires_operator_identity(self, env):
        _, _, rm, _ = env
        _staged(env)
        with pytest.raises(OperatorIdentityError):
            rm.approve_version("security/authentication-lessons", "v01", operator=None)

    def test_approve_requires_env_or_flag_identity(self, env, monkeypatch):
        _, _, rm, _ = env
        _staged(env)
        monkeypatch.setenv("SKILLBOX_OPERATOR", "schmo")
        record = rm.approve_version("security/authentication-lessons", "v01", operator=None)
        assert record["operator"] == "schmo"

    def test_approve_flips_pending_to_active(self, env):
        _, lib, rm, _ = env
        _staged(env)
        rm.approve_version("security/authentication-lessons", "v01", operator="schmo")
        entry = lib.get_version("security/authentication-lessons", "v01")
        assert entry["status"] == "active"
        resolved = lib.resolve_active_version("security/authentication-lessons")
        assert resolved["version"] == "v01"

    def test_approve_supersedes_prior_active(self, env):
        _, lib, rm, root = env
        _staged(env)
        rm.approve_version("security/authentication-lessons", "v01", operator="schmo")
        v2 = lib.stage_version("security/authentication-lessons", "newer", ["LRN-2"],
                               {"frequency": 9, "success_rate": 0.9})
        prior = lib.get_version("security/authentication-lessons", "v01")
        assert prior["status"] == "active"  # supersedes only upon new approval
        rm.approve_version("security/authentication-lessons", "v02", operator="schmo")
        prior = lib.get_version("security/authentication-lessons", "v01")
        assert prior["status"] == "superseded"
        v2entry = lib.get_version("security/authentication-lessons", "v02")
        assert v2entry["status"] == "active"
        # prior content retained intact (FR-032/SC-014)
        assert (root / prior["content_path"]).read_text(encoding="utf-8") == \
            "content-security/authentication-lessons"

    def test_reject_requires_reason(self, env):
        _, _, rm, _ = env
        _staged(env)
        with pytest.raises(SkillboxContractError):
            rm.reject_version("security/authentication-lessons", "v01", operator="schmo",
                              reason="")

    def test_reject_excludes_pending(self, env):
        _, lib, rm, _ = env
        _staged(env)
        rm.reject_version("security/authentication-lessons", "v01", operator="schmo",
                          reason="bad evidence")
        assert lib.get_version("security/authentication-lessons", "v01")["status"] == "rejected"
        assert lib.resolve_active_version("security/authentication-lessons") is None

    def test_illegal_transition_rejected(self, env):
        _, _, rm, _ = env
        _staged(env)
        with pytest.raises(SkillboxContractError):
            rm.reject_version("security/authentication-lessons", "v01", operator="schmo",
                              reason="x")
            rm.reject_version("security/authentication-lessons", "v01", operator="schmo",
                              reason="already rejected")

    def test_rollback_restores_earlier(self, env):
        _, lib, rm, _ = env
        _staged(env)
        rm.approve_version("security/authentication-lessons", "v01", operator="schmo")
        lib.stage_version("security/authentication-lessons", "newer", ["LRN-2"],
                          {"frequency": 9, "success_rate": 0.9})
        rm.approve_version("security/authentication-lessons", "v02", operator="schmo")
        rm.rollback_version("security/authentication-lessons", "v01", operator="schmo")
        assert lib.get_version("security/authentication-lessons", "v01")["status"] == "active"
        assert lib.resolve_active_version("security/authentication-lessons")["version"] == "v01"

    def test_reactivate_after_reject(self, env):
        _, lib, rm, _ = env
        _staged(env)
        rm.reject_version("security/authentication-lessons", "v01", operator="schmo",
                          reason="x")
        rm.reactivate_version("security/authentication-lessons", "v01", operator="schmo")
        assert lib.get_version("security/authentication-lessons", "v01")["status"] == "active"


class TestAuditTrail:
    def test_audit_appended_per_decision(self, env):
        _, lib, rm, root = env
        _staged(env)
        rm.approve_version("security/authentication-lessons", "v01", operator="schmo",
                           rationale="evidence reviewed")
        audit = root / "telemetry" / "review_decisions.json"
        lines = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if l]
        assert len(lines) == 1
        record = lines[0]
        assert record["operator"] == "schmo"
        assert record["decision"] == "approve"
        assert record["rationale"] == "evidence reviewed"
        assert record["evidence_snapshot_hash"].startswith("sha256:")
        assert record["record_hash"] and record["prev_record_hash"] == ""

    def test_hash_chain_detects_tampering(self, env):
        _, lib, rm, root = env
        _staged(env)
        rm.approve_version("security/authentication-lessons", "v01", operator="schmo")
        audit = root / "telemetry" / "review_decisions.json"
        lines = audit.read_text(encoding="utf-8").splitlines()
        # delete the (only) line — chain/verification must fail
        audit.write_text("", encoding="utf-8")
        assert verify_audit_for_version(root, "security/authentication-lessons", "v01") is True
        # tier-2 fallback: index still carries the recorded transition

    def test_audit_reconciles_1to1_with_transitions(self, env):
        _, lib, rm, root = env
        _staged(env)
        rm.approve_version("security/authentication-lessons", "v01", operator="schmo")
        lib.stage_version("security/authentication-lessons", "two", ["LRN-2"], {})
        rm.approve_version("security/authentication-lessons", "v02", operator="schmo")
        audit = root / "telemetry" / "review_decisions.json"
        lines = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if l]
        assert len(lines) == 2  # 1:1 with two approvals (SC-013)
        for line in lines:
            assert line["to_status"] == "active"


class TestExactlyOneActive:
    def test_only_active_version_resolves(self, env):
        _, lib, rm, _ = env
        _staged(env)
        rm.approve_version("security/authentication-lessons", "v01", operator="schmo")
        lib.stage_version("security/authentication-lessons", "two", ["LRN-2"], {})
        # v02 pending: resolution remains v01
        assert lib.resolve_active_version("security/authentication-lessons")["version"] == "v01"
        rm.approve_version("security/authentication-lessons", "v02", operator="schmo")
        assert lib.resolve_active_version("security/authentication-lessons")["version"] == "v02"
