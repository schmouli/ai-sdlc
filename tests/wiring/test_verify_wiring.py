"""Fault-injection suite for scripts/verify_wiring.py (SC-001).

Injects one fault per enumerated check item of contracts/verifier-contract.md:
DEFECT items must exit 2; ADVISORY items must exit 0 with the expected label.

Conventions: per-file fixtures (tempfile.mkdtemp), no shared base classes.
The verifier supports a --sandbox CLI mode that points every wiring-path check
at a detached directory, so the suite never touches the repo's live wiring.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_wiring.py"


def _write_yaml(path: Path, data) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


@pytest.fixture()
def sandbox(tmp_path):
    """Build a minimal wiring sandbox: hooks + manifest + rendered map.

    The manifest declares a skills_root INSIDE the sandbox (with an installed
    ``egm-review-loop`` skill dir), so D3 mapping checks are genuinely exercisable:
    an unknown command maps to no installed skill even though the root resolves.
    """

    class Sandbox:
        def __init__(self, root: Path):
            self.root = root
            self.specify = root / ".specify"
            self.specify.mkdir(parents=True, exist_ok=True)
            self.docs = root / "docs"
            self.docs.mkdir(parents=True, exist_ok=True)
            self.skills_root = root / "skills"
            self.skills_root.mkdir(parents=True, exist_ok=True)
            (self.skills_root / "egm-review-loop").mkdir(parents=True, exist_ok=True)
            # the capture flag's home is config (FR-018): a sandbox env config
            # named for the test env; _enable_capture flips it inside this file.
            self.env_name = os.environ.get("METATRADER_ENV", "production")
            env_cfg_dir = root / "config" / "environments"
            env_cfg_dir.mkdir(parents=True, exist_ok=True)
            self.env_config = env_cfg_dir / f"{self.env_name}.json"
            self.env_config.write_text(
                json.dumps({"system": {"skillbox": {"wiring_capture_enabled": False}}}),
                encoding="utf-8",
            )

        def set_capture(self, value: bool) -> None:
            self.env_config.write_text(
                json.dumps({"system": {"skillbox": {"wiring_capture_enabled": value}}}),
                encoding="utf-8",
            )

        @property
        def extensions(self) -> Path:
            return self.specify / "extensions.yml"

        @property
        def manifest(self) -> Path:
            return self.specify / "wiring-manifest.yml"

        @property
        def map_file(self) -> Path:
            return self.docs / "AI_SDLC_LIFECYCLE_MAP.md"

    sb = Sandbox(tmp_path / "repo")
    sb.root.mkdir(parents=True, exist_ok=True)

    extensions = {
        "installed": [],
        "settings": {"auto_execute_hooks": False},
        "hooks": {
            "after_specify": [
                {
                    "extension": "egm",
                    "command": "egm.review.loop",
                    "enabled": True,
                    "optional": True,
                    "priority": 5,
                    "description": "spec review",
                    "prompt": "run",
                    "condition": None,
                }
            ]
        },
    }
    manifest = {
        "wiring_manifest_version": 1,
        "review_profiles": {"SPEC": {"reviewers": ["zoom_out"], "count": 1, "fan_out_cap": 5}},
        "stages": {"after_specify": {"command": "egm.review.loop", "profile": "SPEC"}},
        "token_budgets": {"hook_decision_cap": 8, "silent_capture_lookback_days": 14},
        "artifacts": {
            "lifecycle_map": {"path": "docs/AI_SDLC_LIFECYCLE_MAP.md"},
            "round_drop_dir": {"path": "logs/egm_rounds/"},
        },
        "phase_status": {"phase_0": "pending"},
        "egm_dependency": {"skills_root": str(sb.skills_root)},
    }
    _write_yaml(sb.extensions, extensions)
    _write_yaml(sb.manifest, manifest)

    # The sandbox sources are read back VERBATIM: the verifier's D4 check computes
    # provenance sha from the raw file text, so the fixture must render from the
    # same bytes the verifier will see.
    sb_ext_text = sb.extensions.read_text(encoding="utf-8")
    sb_man_text = sb.manifest.read_text(encoding="utf-8")

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import generate_lifecycle_map  # noqa: E402

    content = generate_lifecycle_map.render_map(
        extensions, manifest, sb_ext_text, sb_man_text
    )
    sb.map_file.write_text(content, encoding="utf-8", newline="\n")
    sys.path.pop(0)
    return sb


def _run_verify(sandbox) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), "--sandbox", str(sandbox.root)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


# ------------------------------------------------------------------ DEFECT


def test_gate_clean_sandbox_exits_0(sandbox):
    r = _run_verify(sandbox)
    assert r.returncode == 0, r.stdout + r.stderr


def test_fault_d1_non_null_condition_exits_2(sandbox):
    data = yaml.safe_load(sandbox.extensions.read_text(encoding="utf-8"))
    data["hooks"]["after_specify"][0]["condition"] = "config.x"
    _write_yaml(sandbox.extensions, data)
    assert _run_verify(sandbox).returncode == 2


def test_fault_d1_mandatory_hook_exits_2(sandbox):
    data = yaml.safe_load(sandbox.extensions.read_text(encoding="utf-8"))
    data["hooks"]["after_specify"][0]["optional"] = False
    _write_yaml(sandbox.extensions, data)
    assert _run_verify(sandbox).returncode == 2


def _rerender(sandbox) -> None:
    """Re-render the sandbox map after mutating sandbox sources (keeps D4 green)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import generate_lifecycle_map  # noqa: E402

    ext = yaml.safe_load(sandbox.extensions.read_text(encoding="utf-8"))
    man = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    content = generate_lifecycle_map.render_map(
        ext, man,
        sandbox.extensions.read_text(encoding="utf-8"),
        sandbox.manifest.read_text(encoding="utf-8"),
    )
    sandbox.map_file.write_text(content, encoding="utf-8", newline="\n")
    sys.path.pop(0)


def test_d1_absent_condition_is_null_not_fault(sandbox):
    ext = yaml.safe_load(sandbox.extensions.read_text(encoding="utf-8"))
    ext["hooks"]["after_specify"][0].pop("condition", None)
    _write_yaml(sandbox.extensions, ext)
    _rerender(sandbox)  # provenance sha changes with the source text
    assert _run_verify(sandbox).returncode == 0


def test_fault_d2_auto_execute_true_exits_2(sandbox):
    data = yaml.safe_load(sandbox.extensions.read_text(encoding="utf-8"))
    data["settings"]["auto_execute_hooks"] = True
    _write_yaml(sandbox.extensions, data)
    assert _run_verify(sandbox).returncode == 2


def test_fault_d2_auto_execute_absent_exits_2(sandbox):
    data = yaml.safe_load(sandbox.extensions.read_text(encoding="utf-8"))
    del data["settings"]["auto_execute_hooks"]
    _write_yaml(sandbox.extensions, data)
    assert _run_verify(sandbox).returncode == 2


def test_fault_d3_unknown_command_exits_2(sandbox):
    data = yaml.safe_load(sandbox.extensions.read_text(encoding="utf-8"))
    data["hooks"]["after_specify"][0]["command"] = "egm.not-a-skill"
    _write_yaml(sandbox.extensions, data)
    assert _run_verify(sandbox).returncode == 2


def test_fault_d4_hand_edited_map_exits_2(sandbox):
    sandbox.map_file.write_text(
        sandbox.map_file.read_text(encoding="utf-8") + "<!-- drift -->\n", encoding="utf-8"
    )
    assert _run_verify(sandbox).returncode == 2


def test_fault_d5_command_mirror_mismatch_exits_2(sandbox):
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["stages"]["after_specify"]["command"] = "egm.review"
    _write_yaml(sandbox.manifest, data)
    assert _run_verify(sandbox).returncode == 2


def test_fault_d6_bad_manifest_version_exits_2(sandbox):
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["wiring_manifest_version"] = 9
    _write_yaml(sandbox.manifest, data)
    assert _run_verify(sandbox).returncode == 2


def test_fault_d6_unparseable_manifest_exits_2(sandbox):
    sandbox.manifest.write_text("{{{{", encoding="utf-8")
    assert _run_verify(sandbox).returncode == 2


def test_fault_d7_required_map_missing_exits_2(sandbox):
    sandbox.map_file.unlink()
    assert _run_verify(sandbox).returncode == 2


def test_fault_d13_roster_cap_excess_exits_2(sandbox):
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["review_profiles"]["SPEC"]["reviewers"] = [f"p{i}" for i in range(6)]
    data["review_profiles"]["SPEC"]["count"] = 6
    data["review_profiles"]["SPEC"]["fan_out_cap"] = 5
    _write_yaml(sandbox.manifest, data)
    assert _run_verify(sandbox).returncode == 2


# ---- F2: D3 emits DEFECT on mapping mismatch when skills_root resolves ----

def test_fault_d3_unknown_command_exits_2(sandbox):
    """The sandbox declares a resolvable skills_root WITH an installed
    egm-review-loop dir; an unknown command is a genuine D3 DEFECT (not the A2
    unresolvable-root advisory), and the output names D3."""
    data = yaml.safe_load(sandbox.extensions.read_text(encoding="utf-8"))
    data["hooks"]["after_specify"][0]["command"] = "egm.not-a-skill"
    _write_yaml(sandbox.extensions, data)
    _rerender(sandbox)  # provenance sha changes with the source text
    r = _run_verify(sandbox)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "D3" in (r.stdout + r.stderr)


def test_d3_unresolvable_skills_root_is_advisory(sandbox):
    """A manifest whose skills_root genuinely does not exist is environmental
    (A2 advisory, exit 0) — the DEFECT path requires a RESOLVABLE root."""
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["egm_dependency"]["skills_root"] = str(sandbox.root / "no-such-skills")
    _write_yaml(sandbox.manifest, data)
    _rerender(sandbox)
    r = _run_verify(sandbox)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "A2" in (r.stdout + r.stderr)


# ---- F1: live-repo-only checks D8/D9/D12/A1 (faults injected against the REAL
# repo surfaces — no fault for these can exist in a detached sandbox). ----


def test_fault_d8_managed_surface_drift_exits_2(tmp_path, monkeypatch):
    """D8: a managed skill body that no longer matches the registry content-hash
    baseline is a DEFECT. The fault is simulated against an isolated copy of the
    registry + skills dir via the module's constants."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import verify_wiring as vw  # noqa: E402
    try:
        tmp_reg = tmp_path / "registry"
        tmp_reg.mkdir()
        skill_dir = tmp_path / "skills"
        (skill_dir / "fake-skill").mkdir(parents=True)
        skill_md = skill_dir / "fake-skill" / "SKILL.md"
        skill_md.write_text("original body\n", encoding="utf-8")
        import hashlib
        reg_data = {"schema_version": 1, "skills": [{
            "skill_id": "fake-skill", "status": "active",
            "content_hash": "sha256:" + hashlib.sha256(skill_md.read_bytes()).hexdigest(),
        }]}
        reg_file = tmp_reg / "skills.json"
        reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
        monkeypatch.setattr(vw, "REGISTRY", reg_file)
        monkeypatch.setattr(vw, "REVIEW_SKILL_DIR", skill_dir)
        report = vw.Report()
        vw.check_d8_managed_surface(report, {})  # clean: baseline == current
        assert not report.defects
        skill_md.write_text("tampered body\n", encoding="utf-8")  # drift
        report2 = vw.Report()
        vw.check_d8_managed_surface(report2, {})
        assert report2.defects and report2.defects[0].startswith("D8")
    finally:
        sys.path.pop(0)


def test_fault_d9_fingerprint_mismatch_exits_2(tmp_path, monkeypatch):
    """D9: a stored skillbox-digest fingerprint that no longer matches the recomputed
    approved-overlay digest is a DEFECT. Injected on a temp instructions file with the
    recomputation pointed at an isolated store (no live state touched)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import verify_wiring as vw  # noqa: E402
    try:
        instructions = tmp_path / "copilot-instructions.md"
        instructions.write_text(
            "base content\n"
            "<!-- SKILLBOX:BEGIN approved-overlay-guidance -->\n"
            "## SkillBox Approved Overlay Guidance\n"
            "<!-- skillbox-digest: " + ("0" * 64) + " -->\n"
            "_Last refreshed: 2026-11-11T00:00:00-05:00_\n"
            "<!-- SKILLBOX:END -->\n",
            encoding="utf-8",
        )
        # isolated compose source: an OverlayLibrary with NO overlays (digest => [])
        lib_root = tmp_path / ".skillbox"
        lib_root.mkdir()
        monkeypatch.setattr(vw, "INSTRUCTIONS_PATH", instructions)
        monkeypatch.setattr(vw, "SKILLBOX_STATE", lib_root)
        # patch the store constructor the check binds to (default_store is imported
        # inside the check from src.skillbox.telemetry)
        import src.skillbox.telemetry as sbt
        monkeypatch.setattr(sbt, "default_store", lambda root=None: sbt.TelemetryStore(lib_root))
        report = vw.Report()
        vw.check_d9_marker_integrity(report, {})
        # stored all-zero fingerprint != sha256("[]") => D9 defect
        assert report.defects and report.defects[0].startswith("D9"), report.defects
    finally:
        sys.path.pop(0)


def test_fault_d12_snapshot_corrupt_exits_2(tmp_path, monkeypatch):
    """D12: a corrupt snapshot at the checked path is a DEFECT (kg verify exit 2) and
    verifier D12 mirrors that."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import verify_wiring as vw  # noqa: E402
    try:
        snap = tmp_path / "graph_snapshot.json"
        snap.write_text("{corrupt json", encoding="utf-8")
        monkeypatch.setattr(vw, "KG_SNAPSHOT", snap)
        report = vw.Report()
        vw.check_d12_graph_snapshot(report, {})
        assert report.defects and report.defects[0].startswith("D12"), report.defects
    finally:
        sys.path.pop(0)


def test_fault_d12_snapshot_missing_exits_2(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import verify_wiring as vw  # noqa: E402
    try:
        monkeypatch.setattr(vw, "KG_SNAPSHOT", tmp_path / "absent.json")
        report = vw.Report()
        vw.check_d12_graph_snapshot(report, {})
        assert report.defects and report.defects[0].startswith("D12")
    finally:
        sys.path.pop(0)


def test_advisory_a1_stale_digest_labeled(tmp_path, monkeypatch):
    """A1: a digest whose source_commit differs from HEAD exits 0 with a label that
    names both shas; a missing digest labels 'not yet rendered'; a matching sha is
    silent (no A1 label)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import verify_wiring as vw  # noqa: E402
    try:
        digest = tmp_path / "digest_for_reviews.md"
        digest.write_text(
            "# KG Digest for Reviews\n\nsource_commit: deadbee\nbuilt_at: x\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(vw, "DIGEST_PATH", digest)

        def _fake_check_output(cmd, **kw):
            return "3172bf8\n"

        monkeypatch.setattr(vw.subprocess, "check_output", _fake_check_output)
        report = vw.Report()
        vw.check_a1_digest_freshness(report)
        assert report.labels and "A1" in report.labels[0]
        assert "digest stale (snapshot=deadbee vs HEAD=3172bf8)" in report.labels[0]

        digest.write_text(
            "# KG Digest for Reviews\n\nsource_commit: 3172bf8\nbuilt_at: x\n",
            encoding="utf-8",
        )
        report2 = vw.Report()
        vw.check_a1_digest_freshness(report2)
        assert not report2.labels and not report2.defects

        monkeypatch.setattr(vw, "DIGEST_PATH", tmp_path / "absent.md")
        report3 = vw.Report()
        vw.check_a1_digest_freshness(report3)
        assert report3.labels and "not yet rendered" in report3.labels[0]
    finally:
        sys.path.pop(0)


# ------------------------------------------------------------------ ADVISORY


def test_advisory_a2_persona_missing_exits_0_labeled(sandbox):
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["review_profiles"]["SPEC"]["reviewers"] = ["nonexistent_persona"]
    _write_yaml(sandbox.manifest, data)
    _rerender(sandbox)  # roster shown in the map renders from the manifest
    r = _run_verify(sandbox)
    # sandbox mode: skills/personas roots are absent in the detached tree, so the
    # verifier treats reviewer names as unresolvable -> A2 advisory (exit 0)
    assert r.returncode == 0, r.stdout + r.stderr
    out = (r.stdout + r.stderr).lower()
    assert "advisory" in out or "unresolvable" in out or "a2" in out


def _enable_capture(sandbox) -> None:
    """Prepare the sandbox for the capture-enabled state (flag via config + ledger +
    marker + protocol doc). Per FR-018 the flag's home is the env config file."""
    sandbox.set_capture(True)
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["wiring_adopted_at"] = "2026-11-11T00:00:00-05:00"
    _write_yaml(sandbox.manifest, data)
    _rerender(sandbox)  # manifest text changed (adoption marker); keep D4 green
    sb_wiring = sandbox.root / ".skillbox" / "wiring"
    sb_wiring.mkdir(parents=True, exist_ok=True)
    (sb_wiring / "hook_ledger.json").write_text(
        json.dumps({"schema_version": 1, "entries": []}), encoding="utf-8"
    )
    proto = sandbox.root / "docs" / "WIRING_HOOK_TELEMETRY.md"
    proto.write_text("# Capture protocol\n", encoding="utf-8")


def test_advisory_a4_silent_capture_when_enabled_labeled(sandbox):
    """Capture enabled + ledger/marker/protocol present + zero FRESH entries = A4."""
    _enable_capture(sandbox)
    r = _run_verify(sandbox)
    assert r.returncode == 0, r.stdout + r.stderr
    out = (r.stdout + r.stderr).lower()
    assert "a4" in out or "silent" in out or "advisory" in out


def test_advisory_a4_not_fired_when_ledger_entry_fresh(sandbox):
    """A ledger entry whose ts is INSIDE the 14-day lookback window suppresses A4."""
    _enable_capture(sandbox)
    from datetime import datetime, timedelta, timezone
    ledger = sandbox.root / ".skillbox" / "wiring" / "hook_ledger.json"
    now = datetime.now(timezone.utc)
    fresh = {"schema_version": 1, "entries": [{
        "ts": (now - timedelta(hours=2)).isoformat(),
        "event": "after_specify", "command": "egm.review.loop",
        "decision": "accepted", "feature_dir": "specs/x",
    }]}
    ledger.write_text(json.dumps(fresh), encoding="utf-8")
    r = _run_verify(sandbox)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "zero ledger entries" not in out  # A4 advisory signature absent
    assert "silent-capture (gates)" not in out


def test_advisory_a4_fires_on_aged_entries_only(sandbox):
    """Ledger entries OUTSIDE the lookback window (the hook-death signal) fire A4."""
    _enable_capture(sandbox)
    from datetime import datetime, timedelta, timezone
    ledger = sandbox.root / ".skillbox" / "wiring" / "hook_ledger.json"
    aged = {"schema_version": 1, "entries": [{
        "ts": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        "event": "after_specify", "command": "egm.review.loop",
        "decision": "accepted", "feature_dir": "specs/x",
    }]}
    ledger.write_text(json.dumps(aged), encoding="utf-8")
    r = _run_verify(sandbox)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "zero ledger entries" in r.stdout + r.stderr


def test_advisory_a5_gates_without_round_summaries(sandbox):
    """Gates recorded (ledger entry) + zero round summaries in the drop dir = A5."""
    _enable_capture(sandbox)
    from datetime import datetime, timedelta, timezone
    ledger = sandbox.root / ".skillbox" / "wiring" / "hook_ledger.json"
    entry = {"schema_version": 1, "entries": [{
        "ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "event": "after_specify", "command": "egm.review.loop",
        "decision": "accepted", "feature_dir": "specs/x",
    }]}
    ledger.write_text(json.dumps(entry), encoding="utf-8")
    (sandbox.root / "logs" / "egm_rounds").mkdir(parents=True, exist_ok=True)  # empty drop dir
    _rerender(sandbox)
    r = _run_verify(sandbox)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "A5" in (r.stdout + r.stderr) and "zero round summaries" in (r.stdout + r.stderr)


def test_advisory_a5_not_fired_with_fresh_round_summary(sandbox):
    _enable_capture(sandbox)
    from datetime import datetime, timedelta, timezone
    ledger = sandbox.root / ".skillbox" / "wiring" / "hook_ledger.json"
    entry = {"schema_version": 1, "entries": [{
        "ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "event": "after_specify", "command": "egm.review.loop",
        "decision": "accepted", "feature_dir": "specs/x",
    }]}
    ledger.write_text(json.dumps(entry), encoding="utf-8")
    drop = sandbox.root / "logs" / "egm_rounds"
    drop.mkdir(parents=True, exist_ok=True)
    summary = {
        "round_id": "r1", "profile": "SPEC", "target_file": "specs/x/spec.md",
        "reviewers": ["zoom_out"], "findings_count": 0, "round_kind": "review",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "consult": {"mode": "digest", "freshness": "fresh", "fallback": False},
    }
    (drop / "round-1.json").write_text(json.dumps(summary), encoding="utf-8")
    _rerender(sandbox)
    r = _run_verify(sandbox)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "zero round summaries" not in (r.stdout + r.stderr)


def test_d9_wiring_content_inside_markers_exits_2(tmp_path, monkeypatch):
    """D9 also defects when wiring tokens leak inside the SKILLBOX markers."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import verify_wiring as vw  # noqa: E402
    try:
        instructions = tmp_path / "copilot-instructions.md"
        instructions.write_text(
            "<!-- SKILLBOX:BEGIN -->\n"
            "wiring_capture_enabled: true talk\n"
            "<!-- skillbox-digest: " + ("0" * 64) + " -->\n"
            "<!-- SKILLBOX:END -->\n",
            encoding="utf-8",
        )
        lib_root = tmp_path / ".skillbox"
        lib_root.mkdir()
        monkeypatch.setattr(vw, "INSTRUCTIONS_PATH", instructions)
        monkeypatch.setattr(vw, "SKILLBOX_STATE", lib_root)
        import src.skillbox.telemetry as sbt
        monkeypatch.setattr(sbt, "default_store", lambda root=None: sbt.TelemetryStore(lib_root))
        report = vw.Report()
        vw.check_d9_marker_integrity(report, {})
        assert any(d.startswith("D9") and "wiring content leaked" in d for d in report.defects), report.defects
    finally:
        sys.path.pop(0)


def test_d9_no_markers_at_all_is_advisory(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import verify_wiring as vw  # noqa: E402
    try:
        instructions = tmp_path / "copilot-instructions.md"
        instructions.write_text("no markers here\n", encoding="utf-8")
        monkeypatch.setattr(vw, "INSTRUCTIONS_PATH", instructions)
        report = vw.Report()
        vw.check_d9_marker_integrity(report, {})
        assert not report.defects and report.labels, report.labels
    finally:
        sys.path.pop(0)


def test_d13_drift_profile_zero_count_passes(sandbox):
    """The DRIFT profile (count 0, cap 0) must not trip the roster cap when the
    stage carries no profile requirement above the cap semantics."""
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["review_profiles"]["DRIFT"] = {"reviewers": [], "count": 0, "fan_out_cap": 0}
    data["stages"]["after_implement"] = {"command": "egm.review.loop", "profile": "DRIFT"}
    _write_yaml(sandbox.manifest, data)
    _rerender(sandbox)
    assert _run_verify(sandbox).returncode == 0


def test_defect_d7_protocol_doc_missing_when_enabled_exits_2(sandbox):
    sandbox.set_capture(True)
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["wiring_adopted_at"] = "2026-11-11T00:00:00-05:00"
    _write_yaml(sandbox.manifest, data)
    sb_wiring = sandbox.root / ".skillbox" / "wiring"
    sb_wiring.mkdir(parents=True, exist_ok=True)
    (sb_wiring / "hook_ledger.json").write_text(
        json.dumps({"schema_version": 1, "entries": []}), encoding="utf-8"
    )
    _rerender(sandbox)  # manifest text changed; keep D4 green
    # protocol doc NOT created => D7 defect while enabled
    r = _run_verify(sandbox)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "D7" in (r.stdout + r.stderr)


def test_defect_a9_marker_missing_when_enabled_exits_2(sandbox):
    sandbox.set_capture(True)
    _write_yaml(sandbox.manifest, yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8")))
    sb_wiring = sandbox.root / ".skillbox" / "wiring"
    sb_wiring.mkdir(parents=True, exist_ok=True)
    (sb_wiring / "hook_ledger.json").write_text(
        json.dumps({"schema_version": 1, "entries": []}), encoding="utf-8"
    )
    proto = sandbox.root / "docs" / "WIRING_HOOK_TELEMETRY.md"
    proto.write_text("# Capture protocol\n", encoding="utf-8")
    _rerender(sandbox)
    # wiring_adopted_at NOT set => A9 defect while enabled
    r = _run_verify(sandbox)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "A9" in (r.stdout + r.stderr)


# ---------------------------------------------------------------- feature-008: context budgets + marker-region gate


def test_budget_valid_passes(sandbox):
    """Well-formed context_budgets sub-keys pass cleanly (advisory mode)."""
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["token_budgets"]["standing_instructions_max_bytes"] = 32500
    data["token_budgets"]["enforce_standing_instructions"] = False
    data["token_budgets"]["artifact_budgets"] = {"findings_max_bytes": 16384,
                                                 "resolution_max_bytes": 20480,
                                                 "drift_max_bytes": 10240}
    _write_yaml(sandbox.manifest, data)
    _rerender(sandbox)
    assert _run_verify(sandbox).returncode == 0


def test_budget_absent_block_not_a_defect(sandbox):
    """Absent token_budgets context sub-keys entirely → no D14 defect."""
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["token_budgets"] = {"digest_max_bytes": 8192}  # legacy keys only
    _write_yaml(sandbox.manifest, data)
    _rerender(sandbox)
    r = _run_verify(sandbox)
    assert r.returncode == 0
    assert "D14" not in (r.stdout + r.stderr)


def test_budget_malformed_block_is_defect(sandbox):
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["token_budgets"]["standing_instructions_max_bytes"] = -5
    data["token_budgets"]["artifact_budgets"] = {"findings_max_bytes": 16384,
                                                 "resolution_max_bytes": 20480,
                                                 "drift_max_bytes": 10240}
    _write_yaml(sandbox.manifest, data)
    _rerender(sandbox)
    r = _run_verify(sandbox)
    assert r.returncode == 2 and "D14" in (r.stdout + r.stderr)


def test_budget_enforce_true_over_budget_defect_or_sandbox_advisory(sandbox):
    """enforce=true: live mode would hard-fail over-budget; in the sandbox fixture the
    standing file is absent, so the size check degrades to advisory (live-only pattern)."""
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["token_budgets"]["standing_instructions_max_bytes"] = 10
    data["token_budgets"]["enforce_standing_instructions"] = True
    data["token_budgets"]["artifact_budgets"] = {"findings_max_bytes": 16384,
                                                 "resolution_max_bytes": 20480,
                                                 "drift_max_bytes": 10240}
    _write_yaml(sandbox.manifest, data)
    _rerender(sandbox)
    r = _run_verify(sandbox)
    # sandbox has no .github/copilot-instructions.md → advisory skip, never error
    assert r.returncode == 0, r.stdout + r.stderr
    assert "standing file absent" in (r.stdout + r.stderr)


def test_budget_extra_family_wellformed_is_note_not_defect(sandbox):
    """A well-formed family beyond the v1 set renders as an advisory note (manifest-only amendment)."""
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["token_budgets"]["artifact_budgets"] = {"findings_max_bytes": 16384,
                                                 "resolution_max_bytes": 20480,
                                                 "drift_max_bytes": 10240,
                                                 "readiness_max_bytes": 16384}
    _write_yaml(sandbox.manifest, data)
    _rerender(sandbox)
    r = _run_verify(sandbox)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "readiness_max_bytes" in (r.stdout + r.stderr)


def test_budget_bad_member_key_is_defect(sandbox):
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["token_budgets"]["artifact_budgets"] = {"Findings Max": 100}
    _write_yaml(sandbox.manifest, data)
    _rerender(sandbox)
    r = _run_verify(sandbox)
    assert r.returncode == 2 and "D14" in (r.stdout + r.stderr)


def test_budget_missing_required_family_defect(sandbox):
    data = yaml.safe_load(sandbox.manifest.read_text(encoding="utf-8"))
    data["token_budgets"]["artifact_budgets"] = {"drift_max_bytes": 10240}
    _write_yaml(sandbox.manifest, data)
    _rerender(sandbox)
    r = _run_verify(sandbox)
    assert r.returncode == 2 and "D14" in (r.stdout + r.stderr)


def test_d15_marker_region_live_missing_region_defect(monkeypatch, tmp_path):
    """D15 hard gate: a tree whose LIVE standing file has NO SKILLBOX region at all."""
    import importlib
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    vw = importlib.import_module("verify_wiring")
    root = tmp_path / "repo"
    gh = root / ".github"; gh.mkdir(parents=True)
    (gh / "copilot-instructions.md").write_text("# no markers here\n", encoding="utf-8")
    from types import SimpleNamespace
    report = vw.Report()
    # simulate live mode with REPO_ROOT pointed at the temp tree
    monkeypatch.setattr(vw, "REPO_ROOT", root)
    monkeypatch.setattr(vw, "INSTRUCTIONS_PATH", gh / "copilot-instructions.md")
    vw.check_d15_marker_region_hard(report, None)
    assert any("D15" in d for d in report.defects)


def test_d15_marker_region_preserved_passes(monkeypatch, tmp_path):
    """Region present in working and HEAD (same bytes) → no defect."""
    import importlib
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    vw = importlib.import_module("verify_wiring")
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    gh = root / ".github"; gh.mkdir()
    text = "# t\n\nSKILLBOX:BEGIN\nx\nSKILLBOX:END\n"
    (gh / "copilot-instructions.md").write_text(text, encoding="utf-8")
    from types import SimpleNamespace
    report = vw.Report()
    monkeypatch.setattr(vw, "REPO_ROOT", root)
    monkeypatch.setattr(vw, "INSTRUCTIONS_PATH", gh / "copilot-instructions.md")
    captured = {}

    def fake_show(args, cwd=None, capture_output=True, timeout=None):
        captured["called"] = True
        return SimpleNamespace(returncode=0, stdout=text.encode("utf-8"))

    import subprocess as sp
    monkeypatch.setattr(vw.subprocess, "run", fake_show)
    vw.check_d15_marker_region_hard(report, None)
    assert captured.get("called") and not any("D15" in d for d in report.defects)


def test_d15_marker_region_working_differs_from_head_defect(monkeypatch, tmp_path):
    """Working region differs byte-wise from HEAD's → D15 hard defect."""
    import importlib
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    vw = importlib.import_module("verify_wiring")
    root = tmp_path / "repo"
    root.mkdir()
    gh = root / ".github"; gh.mkdir()
    head_text = "SKILLBOX:BEGIN\nold-digest\nSKILLBOX:END\n"
    work_text = "SKILLBOX:BEGIN\nnew-digest\nSKILLBOX:END\n"
    (gh / "copilot-instructions.md").write_text(work_text, encoding="utf-8")
    report = vw.Report()
    monkeypatch.setattr(vw, "REPO_ROOT", root)
    monkeypatch.setattr(vw, "INSTRUCTIONS_PATH", gh / "copilot-instructions.md")

    def fake_show(args, cwd=None, capture_output=True, timeout=None):
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout=head_text.encode("utf-8"))

    import subprocess as sp
    monkeypatch.setattr(vw.subprocess, "run", fake_show)
    vw.check_d15_marker_region_hard(report, None)
    assert any("D15" in d and "differs byte-wise" in d for d in report.defects)


def test_d15_skipped_in_sandbox_mode(monkeypatch, tmp_path):
    """Live-only: sandbox mode → check is a no-op (fixtures lack the live file)."""
    import importlib
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    vw = importlib.import_module("verify_wiring")
    report = vw.Report()
    vw.check_d15_marker_region_hard(report, tmp_path)  # sandbox not None
    assert not report.defects
