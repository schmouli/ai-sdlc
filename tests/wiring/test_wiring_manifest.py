"""Schema conformance tests for .specify/wiring-manifest.yml (T002 acceptance).

The manifest is the normative declarative source; these assertions pin its
literal values against contracts/wiring-manifest-schema.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST = REPO_ROOT / ".specify" / "wiring-manifest.yml"
EXTENSIONS = REPO_ROOT / ".specify" / "extensions.yml"
CONTRACTS = REPO_ROOT / ".specify" / "contracts"


def _load():
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _contract(name: str) -> str:
    return (CONTRACTS / name).read_text(encoding="utf-8")


def test_parses_and_version():
    d = _load()
    assert d["wiring_manifest_version"] == 1


def test_review_profiles_literal():
    rp = _load()["review_profiles"]
    assert rp["SPEC"]["count"] == 4
    assert rp["READINESS"]["count"] == 3
    assert rp["TASKS"]["count"] == 3
    assert rp["CONSISTENCY"]["count"] == 3
    assert rp["DRIFT"]["count"] == 0 and rp["DRIFT"]["reviewers"] == []
    assert all(p["fan_out_cap"] == 5 for p in rp.values() if p["count"] > 0)


def test_consult_matrix_values():
    cm = _load()["consult_matrix"]
    assert cm["zoom_out"] == "none"
    assert cm["comprehension"] == "none"
    assert all(v == "digest" for k, v in cm.items() if k not in ("zoom_out", "comprehension"))


def test_budgets_literal():
    b = _load()["token_budgets"]
    assert b["digest_max_bytes"] == 8192
    assert b["inline_excerpt_max_bytes"] == 4096
    assert b["hook_decision_cap"] == 8
    assert b["silent_capture_lookback_days"] == 14
    assert b["retention_days"] == 30


def test_context_budgets_literal():
    """feature-008: the owner-approved budget values are machine-pinned (file 13)."""
    b = _load()["token_budgets"]
    assert b["standing_instructions_max_bytes"] == 32500
    assert b["enforce_standing_instructions"] is False  # flips at the rewrite commit (B.8)
    ab = b["artifact_budgets"]
    assert ab["findings_max_bytes"] == 16384
    assert ab["resolution_max_bytes"] == 20480
    assert ab["drift_max_bytes"] == 10240


def test_trigger_literal():
    tr = _load()["trigger"]
    assert tr["min_lifecycle_events"] == 30
    assert tr["min_acceptance_rate"] == 0.60
    assert tr["min_features"] == 2


def test_disabled_stages_are_derived_not_mirrored():
    """R1-F9: the manifest carries NO `disabled` field (derived from extensions.yml)."""
    stages = _load()["stages"]
    for event, st in stages.items():
        assert "disabled" not in st, f"{event}: disabled must derive from extensions.yml"


def test_stage_commands_mirror_extensions_yml():
    """D5 mirror-equality: manifest stage commands == extensions.yml commands."""
    ext = yaml.safe_load(EXTENSIONS.read_text(encoding="utf-8"))
    ext_cmds = {}
    for event, entries in (ext.get("hooks") or {}).items():
        for entry in entries or []:
            ext_cmds[event] = entry.get("command")
    stages = _load()["stages"]
    for event, st in stages.items():
        assert event in ext_cmds, f"{event} missing from extensions.yml"
        assert st["command"] == ext_cmds[event], f"{event}: manifest command != extensions.yml"


def test_upstream_pin_literal():
    dep = _load()["egm_dependency"]
    assert dep["upstream_pin"] == "36343a16f0e0d830a7ed78b0da97695379ab3243"


def test_schema_contract_documented_keys_match():
    """The schema contract documents each top-level key the manifest carries."""
    ms = _load()
    schema = _contract("wiring-manifest-schema.md")
    for key in ms:
        assert key in schema, f"top-level key {key} undocumented in wiring-manifest-schema.md"


def test_no_disabled_mirror_in_schema_contract():
    schema = _contract("wiring-manifest-schema.md")
    assert "DISABLED-STATE DERIVES SOLELY FROM `EXTENSIONS.YML`" in schema.upper() or "derives solely from" in schema.lower()


def test_phase_status_keys():
    ps = _load()["phase_status"]
    assert set(ps.keys()) == {"phase_0", "phase_1", "phase_2", "phase_3"}
    assert all(v in ("pending", "complete") for v in ps.values())


def test_json_payloads_in_capture_contract_are_parseable():
    """Round-up test: every ```json block in capture-contract.md parses."""
    cc = _contract("capture-contract.md")
    blocks = re.findall(r"```json\n(.*?)```", cc, flags=re.S)
    assert blocks, "capture-contract.md must carry JSON examples"
    for blob in blocks:
        json.loads(blob)
