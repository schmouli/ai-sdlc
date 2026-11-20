"""Wiring verifier: machine-checks the AI-SDLC wiring contracts (spec 005 FR-001).

Contract: specs/005-ai-sdlc-wiring/contracts/verifier-contract.md
Exit codes: 0 = contracts pass (advisory labels allowed); 2 = wiring defect.

Checks (defect unless noted advisory/surfaced):
  D1  hook config parse + user-gating (optional, condition null)
  D2  auto_execute_hooks explicitly false
  D3  command -> declared skill mapping (skills_root; unresolvable root = A2
      advisory, resolvable root with a mapping miss = DEFECT)
  D4  lifecycle map byte-identity (via generate_lifecycle_map.py --check subprocess)
  D5  manifest stage-command mirror equality vs extensions.yml
  D6  manifest parseability + version
  D7  required-artifact presence (map; ledger well-formedness when capture enabled;
      capture-protocol doc = DEFECT iff capture enabled, ADVISORY before)
  D8  managed-surface integrity (registry content-hash baseline vs live SKILL.md
      bodies; registry absent = A2 advisory; live repo only)
  D9  SKILLBOX marker integrity: begin/end present (absent entirely = A2 advisory),
      recomputed fingerprint vs the stored skillbox-digest line (mismatch = DEFECT),
      no wiring content inside markers (live repo only)
  D10 ledger well-formedness (only when capture enabled)
  D11 hook-decision ceiling (wired enabled events <= cap; structural)
  D12 graph snapshot integrity (kg CLI verify delegation; live repo only)
  D13 roster cap (per-event fan-out <= cap; before_specify exempt while disabled)
  A1  digest absent/stale (label names snapshot sha + HEAD sha; live repo only)
  A2  review-tooling absence (environmental)
  A3  review-tooling pin stale (live repo only)
  A4  silent-capture gates (capture enabled + no ledger entry ts inside the
      silent_capture_lookback_days window)
  A5  silent-capture rounds (capture enabled + gates recorded + no round summaries
      modified inside the lookback window)
  A6  partial-loss reconciliation (ledger entries without a recorded telemetry event
      inside the lookback window; capture-enabled only)
  A7  manifest root unresolvable (portability)
  A8  capture-state surfacing (inert (flag off) or enabled, with the source config file)
  A9  adoption marker absence (DEFECT if capture enabled; ADVISORY before)

--sandbox <dir>: run every check against a detached directory containing only
.specify/extensions.yml + .specify/wiring-manifest.yml + docs/ + a skills_root +
.skillbox state (test mode); live-repo-only checks (D8/D9/D12/A1 and the A3 pin
comparison) are skipped entirely in sandbox mode. Sandbox exits 0/2 on the path-
and config-based checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS_PATH = REPO_ROOT / ".specify" / "extensions.yml"
MANIFEST_PATH = REPO_ROOT / ".specify" / "wiring-manifest.yml"
MAP_PATH = REPO_ROOT / "docs" / "AI_SDLC_LIFECYCLE_MAP.md"
GENERATOR = REPO_ROOT / "scripts" / "generate_lifecycle_map.py"
PROTOCOL_DOC = REPO_ROOT / "docs" / "WIRING_HOOK_TELEMETRY.md"
SKILLBOX_STATE = REPO_ROOT / ".skillbox"
REGISTRY = SKILLBOX_STATE / "registry" / "skills.json"
REVIEW_SKILL_DIR = REPO_ROOT / ".github" / "skills"

PASS = 0
DEFECT = 2


class Report:
    def __init__(self) -> None:
        self.defects: list[str] = []
        self.labels: list[str] = []
        self.info: list[str] = []

    def defect(self, code: str, msg: str) -> None:
        self.defects.append(f"{code}: {msg}")

    def advisory(self, code: str, msg: str) -> None:
        self.labels.append(f"ADVISORY {code}: {msg}")

    def surfaced(self, code: str, msg: str) -> None:
        self.info.append(f"INFO {code}: {msg}")

    @property
    def exit_code(self) -> int:
        return DEFECT if self.defects else PASS


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _state_root(manifest: dict, repo_root: Path) -> Path:
    """Resolve the SkillBox state root (config file may override; default repo/.skillbox)."""
    cfg = repo_root / "config" / "environments"
    env = os.environ.get("METATRADER_ENV", "production")
    cfg_file = cfg / f"{env}.json"
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            sr = (data.get("system") or {}).get("skillbox", {}).get("state_root")
            if sr:
                p = Path(sr)
                if not p.is_absolute():
                    p = repo_root / p
                return p
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    return repo_root / ".skillbox"


def _capture_enabled(manifest: dict, repo_root: Path) -> bool:
    """`system.skillbox.wiring_capture_enabled` from the METATRADER_ENV config
    (FR-018: the flag's home is config; there is no manifest fallback). The config
    resolves relative to the repo root under check (sandbox mode points it at the
    detached directory)."""
    env = os.environ.get("METATRADER_ENV", "production")
    cfg_file = repo_root / "config" / "environments" / f"{env}.json"
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            val = (data.get("system") or {}).get("skillbox", {}).get("wiring_capture_enabled")
            if isinstance(val, bool):
                return val
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    return False


def _skills_root(manifest: dict) -> Path:
    return Path((manifest.get("egm_dependency") or {}).get("skills_root", ""))


def _personas_root(manifest: dict) -> Path:
    return Path((manifest.get("egm_dependency") or {}).get("personas_root", ""))


def _installed_egm_skills(manifest: dict) -> set:
    """Skill names discovered under skills_root (dir names egm-*)."""
    root = _skills_root(manifest)
    if not root.exists():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir() and p.name.startswith("egm-")}


def _installed_personas(manifest: dict) -> set:
    root = _personas_root(manifest)
    if not root.exists():
        return set()
    return {p.stem for p in root.glob("*.md")}


def _hook_events(extensions: dict) -> dict:
    """Return {event: hook_entry} for every hook entry (first entry per event)."""
    out: dict = {}
    for event, entries in (extensions.get("hooks") or {}).items():
        if not isinstance(entries, list):
            continue
        out[event] = entries[0]
    return out


def _enabled_count(extensions: dict) -> int:
    n = 0
    for event, entries in (extensions.get("hooks") or {}).items():
        for entry in entries or []:
            if entry.get("enabled") is not False and not event.startswith("before_specify"):
                n += 1
    return n


def check_config(report: Report, extensions: dict, manifest: dict) -> None:
    """D1/D2: parseability, user-gating, auto-execution off."""
    if not isinstance(extensions, dict):
        report.defect("D1", "extensions.yml is not a mapping")
        return
    hooks = extensions.get("hooks")
    if not isinstance(hooks, dict) or not hooks:
        report.defect("D1", "extensions.yml carries no hooks block")
    for event, entries in (hooks or {}).items():
        for i, entry in enumerate(entries or []):
            if entry.get("optional") is not True:
                report.defect("D1", f"{event}[{i}]: optional is not true")
            cond = entry.get("condition")
            if cond not in (None, ""):
                report.defect("D1", f"{event}[{i}]: condition is non-null ({cond!r})")


def check_auto_exec(report: Report, extensions: dict) -> None:
    """D2: settings.auto_execute_hooks must be explicitly false."""
    settings = extensions.get("settings") or {}
    if settings.get("auto_execute_hooks") is not False:
        report.defect("D2", "settings.auto_execute_hooks is not explicitly false")


def check_d3_command_mapping(report: Report, extensions: dict, manifest: dict) -> None:
    """D3: every hook command (dot->hyphen) maps to an installed egm-* skill dir.

    Severity per verifier-contract: an unresolvable skills_root is environmental
    (A2 advisory); a resolvable root with a mapping miss is a D3 DEFECT.
    """
    installed = _installed_egm_skills(manifest)
    root = _skills_root(manifest)
    for event, entry in _hook_events(extensions).items():
        cmd = entry.get("command") or ""
        skill = cmd.replace(".", "-")
        if not installed and not root.exists():
            # environmental: root genuinely absent (sandbox/portability) -> advisory
            report.advisory("A2", f"skills_root unresolvable ({root}); cannot verify command mapping for {event} -> {skill}")
            continue
        if skill not in installed:
            report.defect("D3", f"{event}: command {cmd!r} does not map to an installed egm-* skill under {root}")


def check_d5_mirror(report: Report, extensions: dict, manifest: dict) -> None:
    """D5: every manifest stages.<event>.command equals the hook command."""
    hooks = _hook_events(extensions)
    for event, st in (manifest.get("stages") or {}).items():
        mcmd = (st or {}).get("command")
        if event in hooks and mcmd != hooks[event].get("command"):
            report.defect("D5", f"{event}: manifest command {mcmd!r} != hook command {hooks[event].get('command')!r}")


def check_d6_manifest(report: Report, manifest: dict) -> None:
    """D6: parseability + version."""
    if not isinstance(manifest, dict):
        report.defect("D6", "wiring-manifest.yml is not a mapping")
        return
    if manifest.get("wiring_manifest_version") != 1:
        report.defect("D6", f"wiring_manifest_version must be 1 (got {manifest.get('wiring_manifest_version')!r})")


# feature-008 context budgets (additive; existing checks untouched)
BUDGET_REQUIRED_FAMILIES = ("findings", "resolution", "drift")


def check_context_budgets(report: Report, manifest: dict, sandbox: Path | None = None) -> None:
    """feature-008: token_budgets.* context sub-keys — types/semantics + enforce gate.

    Enforce semantics (stated once): only the standing-instruction ceiling is ever
    enforceable, via the owner-gated flag flip in the rewrite commit; the artifact
    budgets are advisory-only by design. Sandbox/live-only: when the standing file
    is absent from the tree under check (sandbox fixtures), the size checks degrade
    to advisory and never error.
    """
    tb = manifest.get("token_budgets")
    if not isinstance(tb, dict):
        return  # block absent entirely: not a defect (additive sub-keys)
    standing_cap = tb.get("standing_instructions_max_bytes")
    if standing_cap is not None and (isinstance(standing_cap, bool)
                                     or not isinstance(standing_cap, int) or standing_cap <= 0):
        report.defect("D14", f"standing_instructions_max_bytes must be a positive int (got {standing_cap!r})")
    enforce = tb.get("enforce_standing_instructions")
    if enforce is not None and not isinstance(enforce, bool):
        report.defect("D14", f"enforce_standing_instructions must be a bool (got {enforce!r})")
    budgets = tb.get("artifact_budgets")
    if budgets is not None and not isinstance(budgets, dict):
        report.defect("D14", f"artifact_budgets must be a mapping (got {type(budgets).__name__})")
        budgets = None
    if isinstance(budgets, dict):
        required = {f"{f}_max_bytes" for f in BUDGET_REQUIRED_FAMILIES}
        for k, v in budgets.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*_max_bytes", str(k)):
                report.defect("D14", f"artifact_budgets member key {k!r} must match '<family>_max_bytes'")
                continue
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                report.defect("D14", f"artifact_budgets.{k} must be a positive int (got {v!r})")
                continue
            if k not in required:
                report.advisory("A10", f"well-formed family budget beyond the v1 set: {k}={v} "
                                       f"(advisory note, not a defect — the manifest-only amendment path)")
        missing = required - set(budgets)
        if missing:
            report.defect("D14", f"required v1 artifact budget(s) missing: {sorted(missing)}")
    # standing-size gate (live-only; advisory degradation when the file is absent)
    instructions = (sandbox or REPO_ROOT) / INSTRUCTIONS_PATH.relative_to(REPO_ROOT)
    if not instructions.exists():
        report.advisory("A10", f"standing file absent ({instructions}); standing-size check skipped")
        return
    try:
        size = instructions.stat().st_size
    except OSError as exc:
        report.advisory("A10", f"standing file unreadable ({exc}); standing-size check skipped")
        return
    if isinstance(standing_cap, int) and not isinstance(standing_cap, bool):
        if enforce is True:
            if size > standing_cap:
                report.defect(
                    "D14",
                    f"standing instructions {size:,} B exceed enforced ceiling {standing_cap:,} B "
                    "(enforce_standing_instructions: true)",
                )
            else:
                report.surfaced("A10", f"standing instructions {size:,} B <= enforced ceiling {standing_cap:,} B")
        else:
            if size > standing_cap:
                report.advisory(
                    "A10",
                    f"standing instructions {size:,} B exceed advisory ceiling {standing_cap:,} B "
                    "(advisory until the approved rewrite flips enforce_standing_instructions)",
                )
            else:
                report.surfaced("A10", f"standing instructions {size:,} B within advisory ceiling "
                                       f"{standing_cap:,} B")
    else:
        report.surfaced("A10", f"standing instructions {size:,} B (no standing_instructions_max_bytes declared)")
    if enforce is True and standing_cap is None:
        report.defect("D14", "enforce_standing_instructions is true but standing_instructions_max_bytes "
                             "is not declared — the flag requires a ceiling")


def _extract_skmarker_region(text: str) -> str | None:
    """The SKILLBOX:BEGIN…SKILLBOX:END region inclusive, or None when absent."""
    b = text.find("SKILLBOX:BEGIN")
    e = text.find("SKILLBOX:END")
    if b == -1 or e == -1 or e < b:
        return None
    end = text.find("\n", e)
    return text[b:end if end != -1 else len(text)]


def check_d15_marker_region_hard(report: Report, sandbox: Path | None = None) -> None:
    """feature-008 D15 (NEW; additive over D9): hard marker-region gate.

    Live-only. DEFECT (hard) when the LIVE standing file lacks the SKILLBOX
    region entirely (closing D9's advisory degradation hole — a committed
    deletion passes D9 forever) or when the working region differs byte-wise
    from HEAD's region. D9 itself is untouched (pinned advisory test stays green).
    """
    if sandbox is not None:
        return  # live-only check
    instructions = INSTRUCTIONS_PATH
    if not instructions.exists():
        report.advisory("A11", f"standing file absent ({instructions}); marker-region gate skipped")
        return
    working_text = instructions.read_text(encoding="utf-8")
    working_region = _extract_skmarker_region(working_text)
    if working_region is None:
        report.defect("D15", "SKILLBOX region absent entirely from .github/copilot-instructions.md "
                             "(hard gate; D9's advisory degradation would not catch this)")
        return
    try:
        head_proc = subprocess.run(["git", "show", f"HEAD:.github/copilot-instructions.md"],
                                   cwd=str(REPO_ROOT), capture_output=True, timeout=15)
        head_text = head_proc.stdout.decode("utf-8", errors="replace") if head_proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        head_text = None
    if head_text is None:
        # unborn/unavailable HEAD: nothing to compare — region presence already verified
        report.surfaced("A11", "HEAD version unavailable (unborn or git error); region presence verified only")
        return
    head_region = _extract_skmarker_region(head_text)
    if head_region is None:
        report.advisory("A11", "HEAD version carries no SKILLBOX region (pre-feature file) — "
                               "byte-equality leg skipped; live presence enforced")
        return
    if working_region != head_region:
        report.defect("D15", "working SKILLBOX region differs byte-wise from HEAD's region "
                             "(the rewrite must copy the region byte-for-byte)")


def check_d7_required_artifacts(report: Report, manifest: dict, sandbox: Path | None, capture: bool, state_root: Path) -> None:
    """D7: required artifacts (map always; ledger when capture enabled; protocol doc iff enabled)."""
    artifacts = (manifest.get("artifacts") or {})
    root = sandbox or REPO_ROOT
    map_path = root / (artifacts.get("lifecycle_map") or {}).get("path", "docs/AI_SDLC_LIFECYCLE_MAP.md")
    if not map_path.exists():
        report.defect("D7", f"required artifact missing: {map_path}")
    if capture:
        ledger = state_root / "wiring" / "hook_ledger.json"
        if not ledger.exists():
            report.defect("D7", f"hook ledger missing (capture enabled): {ledger}")
        proto = root / "docs" / "WIRING_HOOK_TELEMETRY.md"
        if not proto.exists():
            report.defect("D7", "capture-protocol doc missing while capture enabled (docs/WIRING_HOOK_TELEMETRY.md)")


def check_d11_ceiling(report: Report, extensions: dict, manifest: dict) -> None:
    """D11: wired enabled user-facing hook events <= cap."""
    cap = (manifest.get("token_budgets") or {}).get("hook_decision_cap", 8)
    wired = _enabled_count(extensions)
    if wired > cap:
        report.defect("D11", f"wired enabled hook events {wired} exceed cap {cap}")


def check_d13_roster(report: Report, extensions: dict, manifest: dict) -> None:
    """D13: per-event roster + profile count <= fan_out_cap; before_specify exempt while disabled."""
    profiles = manifest.get("review_profiles") or {}
    stages = manifest.get("stages") or {}
    hooks = _hook_events(extensions)
    for event, st in stages.items():
        profile = (st or {}).get("profile")
        if not profile:
            continue  # disabled stage (e.g. before_specify exempt)
        prow = profiles.get(profile) or {}
        cap = prow.get("fan_out_cap")
        reviewers = prow.get("reviewers") or []
        n = prow.get("count", len(reviewers))
        if cap is None:
            report.defect("D13", f"{event}: profile {profile} has no fan_out_cap")
            continue
        if n > cap:
            report.defect("D13", f"{event}: profile {profile} roster {n} exceeds cap {cap}")


def check_a2_tooling_presence(report: Report, manifest: dict, sandbox: Path | None) -> None:
    """A2: review tooling absence is environmental advisory (live repo only)."""
    if sandbox is not None:
        return  # sandbox has no user-level EGM tree by construction
    dep = manifest.get("egm_dependency") or {}
    for key, root_key, label in (
        ("expected_skills", "skills_root", "skill"),
        ("expected_agents", "agents_root", "agent"),
        ("expected_personas", "personas_root", "persona"),
    ):
        base = root_key
        root = Path(dep.get(base, ""))
        if not root.exists():
            report.advisory("A2", f"{label} root unresolvable: {root}")
            continue
        for name in dep.get(key) or []:
            target = root / name
            if not target.exists() and key == "expected_personas":
                target = root / f"{name}.md"
            if not target.exists() and key == "expected_agents":
                target = root / name  # agents already carry .agent.md suffix
            if not (target.exists() or (root / f"{name}.md").exists() or (root / f"{name}.agent.md").exists()):
                report.advisory("A2", f"{label} missing: {target}")


def check_a3_pin(report: Report, manifest: dict) -> None:
    """A3: upstream pin comparison (live repo only; resource doc is user-level)."""
    if os.environ.get("METATRADER_ENV") == "__sandbox__":
        return
    dep = manifest.get("egm_dependency") or {}
    pin = dep.get("upstream_pin")
    doc = Path(dep.get("skills_root", "")) / "egm" if False else None
    # The installed docs/copilot.md carries the pin line; check the live file.
    pin_doc = Path(dep.get("skills_root", "")).parent / "egm" / "docs" / "copilot.md"
    if not pin_doc.exists():
        report.advisory("A3", "upstream pin source doc not found; pin comparison skipped")
        return
    text = pin_doc.read_text(encoding="utf-8", errors="replace")
    if pin and pin not in text:
        report.advisory("A3", f"upstream pin {pin} not found in {pin_doc}")


def check_a4_a9_capture_advisories(report: Report, manifest: dict, capture: bool, state_root: Path,
                                   sandbox: Path | None = None) -> None:
    """A4/A5/A9: silent-capture + marker advisories (capture-enabled only).

    A4: capture enabled + zero ledger entries with a ts inside
    ``silent_capture_lookback_days`` — the hook-death signal.
    A5: capture enabled + gate decisions recorded + zero round summaries
    modified within the same lookback window.
    """
    if not capture:
        report.advisory("A9", "adoption marker absent (capture disabled; becomes DEFECT once enabled)")
        report.advisory("A4", "silent-capture advisory suppressed while capture disabled")
        return
    marker = manifest.get("wiring_adopted_at")
    if not marker:
        report.defect("A9", "wiring_adopted_at absent while capture enabled")
    lookback_days = int((manifest.get("token_budgets") or {}).get("silent_capture_lookback_days", 14))
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=lookback_days)

    ledger = state_root / "wiring" / "hook_ledger.json"
    fresh_entries = 0
    try:
        data = json.loads(ledger.read_text(encoding="utf-8-sig")) if ledger.exists() else {}
    except json.JSONDecodeError:
        data = {}
    for entry in data.get("entries") or []:
        try:
            ts = datetime.fromisoformat(entry.get("ts"))
            ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        except (TypeError, ValueError):
            continue
        if ts >= window_start:
            fresh_entries += 1
    if fresh_entries == 0:
        report.advisory(
            "A4",
            f"silent-capture (gates): zero ledger entries within the {lookback_days}-day lookback"
            f" window (capture enabled — hook-death signal): {ledger}",
        )

    # A5: gates recorded (any ledger entry at all, fresh or aged) but zero round
    # summaries modified inside the lookback window. The drop dir must EXIST for
    # the comparison to be meaningful (an unwritten dir is not a round-skip signal).
    gates_recorded = bool(data.get("entries"))
    drop_rel = ((manifest.get("artifacts") or {}).get("round_drop_dir") or {}).get("path", "logs/egm_rounds/")
    drop_dir = (sandbox or REPO_ROOT) / drop_rel
    fresh_rounds = 0
    if drop_dir.exists():
        for f in drop_dir.glob("*.json"):
            try:
                mt = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mt >= window_start:
                fresh_rounds += 1
    if gates_recorded and fresh_rounds == 0 and drop_dir.exists():
        report.advisory(
            "A5",
            f"silent-capture (rounds): gate decisions recorded but zero round summaries within"
            f" the {lookback_days}-day lookback window: {drop_dir}",
        )


def check_a6_partial_loss_reconciliation(report: Report, manifest: dict, capture: bool,
                                         state_root: Path, sandbox: Path | None = None) -> None:
    """A6: ledger entries without corresponding recorded events within the lookback
    window (ledger-vs-store reconciliation; capture-enabled only)."""
    if not capture:
        report.advisory("A6", "partial-loss reconciliation suppressed while capture disabled")
        return
    if sandbox is not None:
        report.advisory("A6", "partial-loss reconciliation skipped in sandbox (telemetry store not wired)")
        return
    lookback_days = int((manifest.get("token_budgets") or {}).get("silent_capture_lookback_days", 14))
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=lookback_days)
    ledger = state_root / "wiring" / "hook_ledger.json"
    if not ledger.exists():
        return
    try:
        data = json.loads(ledger.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return
    # Gate rows carry event types hook_gate_accepted/hook_gate_skipped; each ledger
    # acceptance decision should have a matching telemetry event (same hook window).
    gate_rows = [
        e for e in data.get("entries") or []
        if e.get("decision") in ("accepted", "skipped")
    ]
    if not gate_rows:
        return
    fresh_rows = []
    for e in gate_rows:
        try:
            ts = datetime.fromisoformat(e.get("ts"))
            ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        except (TypeError, ValueError):
            continue
        if ts >= window_start:
            fresh_rows.append(e)
    if not fresh_rows:
        return
    try:
        from src.skillbox.telemetry import TelemetryStore
        # state_root here IS the skillbox state root (the store lives under it).
        store = TelemetryStore(state_root)
        store.init_db()
        window_rows = store.query_events(event_type="hook_gate_accepted", limit=5000)
        window_rows += store.query_events(event_type="hook_gate_skipped", limit=5000)
        matched = set()
        for row in window_rows:
            ts_str = row.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_str)
                ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
            except (TypeError, ValueError):
                continue
            payload = row.get("payload") or {}
            matched.add((str(payload.get("hook_event")), str(payload.get("hook_command"))))
        orphans = [
            f"{e.get('event')}:{e.get('command')}@{e.get('ts')}"
            for e in fresh_rows
            if (str(e.get("event")), str(e.get("command"))) not in matched
        ]
        store.close()
    except Exception as exc:  # fail-soft (FR-024): unresolved store is advisory context
        report.advisory("A6", f"partial-loss reconciliation degraded ({exc}); not asserted")
        return
    if orphans:
        report.advisory(
            "A6",
            f"partial-loss reconciliation: {len(orphans)} ledger entr{'y' if len(orphans) == 1 else 'ies'}"
            f" without a recorded telemetry event within the {lookback_days}-day lookback window:"
            f" {', '.join(orphans[:5])}",
        )


def check_d8_managed_surface(report: Report, manifest: dict) -> None:
    """D8: managed speckit skill bodies + registered review-context skill unchanged
    vs the registry content-hash baseline (between reindex passes)."""
    registry_path = REGISTRY
    if not registry_path.exists():
        report.advisory("A2", f"SkillBox registry absent ({registry_path}); managed-surface baseline unavailable")
        return
    try:
        with open(registry_path, encoding="utf-8-sig") as fh:
            reg = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        report.defect("D8", f"SkillBox registry unparseable: {exc}")
        return
    for entry in reg.get("skills") or []:
        if entry.get("status", "active") == "removed":
            continue
        sid = entry.get("skill_id")
        expected = entry.get("content_hash")
        skill_md = REVIEW_SKILL_DIR / sid / "SKILL.md"
        if not skill_md.exists():
            report.defect("D8", f"managed skill file missing for {sid}: {skill_md}")
            continue
        actual = "sha256:" + hashlib.sha256(skill_md.read_bytes()).hexdigest()
        if actual != expected:
            report.defect(
                "D8",
                f"managed-surface drift: {sid} content sha {actual} != registry baseline {expected}"
                f" (reindex: python -m src.skillbox registry reindex)",
            )


INSTRUCTIONS_PATH = REPO_ROOT / ".github" / "copilot-instructions.md"


def check_d9_marker_integrity(report: Report, manifest: dict) -> None:
    """D9: SKILLBOX BEGIN/END markers present; the stored ``skillbox-digest:`` line
    matches a recomputation; no wiring content inside markers.

    ADVISORY if no SKILLBOX markers at all; DEFECT on absent-incomplete markers,
    missing fingerprint line, or fingerprint mismatch.
    """
    instructions = INSTRUCTIONS_PATH
    if not instructions.exists():
        report.advisory("A2", f"instructions file absent ({instructions}); marker integrity skipped")
        return
    text = instructions.read_text(encoding="utf-8")
    if "SKILLBOX:BEGIN" not in text and "SKILLBOX:END" not in text:
        report.advisory("A2", "no SKILLBOX markers in .github/copilot-instructions.md; marker integrity skipped")
        return
    begin_idx = text.find("SKILLBOX:BEGIN")
    end_idx = text.find("SKILLBOX:END")
    if begin_idx == -1 or end_idx == -1:
        report.defect("D9", "SKILLBOX BEGIN/END markers incomplete in .github/copilot-instructions.md")
        return
    fingerprint_line = None
    for line in text[begin_idx:end_idx].splitlines():
        if "skillbox-digest:" in line:
            fingerprint_line = line
    if fingerprint_line is None:
        report.defect("D9", "skillbox-digest fingerprint line missing inside the SKILLBOX block")
        return
    stored = fingerprint_line.split("skillbox-digest:", 1)[1].strip().rstrip("-->").strip()
    if not stored:
        report.defect("D9", "skillbox-digest fingerprint empty")
        return
    # The injection pipeline composes the fingerprint over the APPROVED DIGEST
    # (json payload), not over the rendered text; verify via the module owner so
    # drift detection never re-implements the compose step (delegation rule).
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from src.skillbox.inject import compose_digest, _content_fingerprint
        from src.skillbox.overlays import OverlayLibrary
        from src.skillbox.telemetry import default_store

        store = default_store(SKILLBOX_STATE)
        try:
            library = OverlayLibrary(SKILLBOX_STATE).bind_store(store)
            expected = _content_fingerprint(compose_digest(library))
        finally:
            store.close()
    except Exception as exc:
        report.advisory("A2", f"fingerprint recompute degraded ({exc}); line-presence verified only")
        return
    finally:
        try:
            sys.path.remove(str(REPO_ROOT))
        except ValueError:
            pass
    if expected != stored:
        report.defect(
            "D9",
            f"marker fingerprint mismatch: stored {stored} vs recomputed {expected}"
            f" (re-run python -m src.skillbox inject refresh)",
        )
    # wiring content must not appear inside the injection block
    for token in ("wiring_capture_enabled", "hook_ledger", "wiring_adopted_at"):
        if token in text[begin_idx:end_idx]:
            report.defect("D9", f"wiring content leaked inside SKILLBOX markers: {token!r}")


# Delegated CLI + snapshot locations (module constants: fault tests point the
# checks at isolated temp artifacts without touching live state).
KG_CLI = REPO_ROOT / "scripts" / "run_knowledge_graph.py"
KG_SNAPSHOT = REPO_ROOT / "output" / "kg" / "graph_snapshot.json"
DIGEST_PATH = REPO_ROOT / "output" / "kg" / "digest_for_reviews.md"


def check_d12_graph_snapshot(report: Report, manifest: dict) -> None:
    """D12: graph snapshot integrity — missing/corrupt/schema mismatch is a DEFECT.

    Delegated: never parses graph_snapshot.json directly; runs the kg CLI's own
    ``verify`` (its exit-2 semantics are mirrored, per the verifier contract).
    """
    if not KG_SNAPSHOT.exists():
        report.defect("D12", f"graph snapshot missing: {KG_SNAPSHOT} (run scripts/run_knowledge_graph.py build)")
        return
    sys.path.insert(0, str(REPO_ROOT))
    try:
        r = subprocess.run(
            [sys.executable, str(KG_CLI), "verify", "--out", str(KG_SNAPSHOT)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
    except Exception as exc:
        report.defect("D12", f"kg verify subprocess failed to launch: {exc}")
        return
    finally:
        try:
            sys.path.remove(str(REPO_ROOT))
        except ValueError:
            pass
    if r.returncode != 0:
        detail = (r.stdout or r.stderr or "").strip()
        report.defect("D12", f"graph snapshot integrity failed (kg verify exit {r.returncode}): {detail[:300]}")
    else:
        try:
            verdict = json.loads(r.stdout.strip().splitlines()[-1])
            if verdict.get("verdict") != "PASS":
                report.defect("D12", f"graph snapshot invariants failed: {verdict}")
        except (json.JSONDecodeError, IndexError, TypeError):
            report.defect("D12", "kg verify emitted no parseable verdict JSON")


def check_a1_digest_freshness(report: Report) -> None:
    """A1: digest absent/stale — advisory label naming snapshot sha + HEAD sha."""
    digest_path = DIGEST_PATH
    if not digest_path.exists():
        report.advisory("A1", "digest not yet rendered (output/kg/digest_for_reviews.md absent)")
        return
    try:
        head = subprocess.check_output(
            ["git", "-C", ".", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        report.advisory("A1", "HEAD sha unavailable; digest freshness not asserted")
        return
    source_commit = None
    for line in digest_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("source_commit:"):
            source_commit = line.split("source_commit:", 1)[1].strip()
            break
    if source_commit is None:
        report.advisory("A1", "digest carries no source_commit header")
        return
    if source_commit != head:
        report.advisory("A1", f"digest stale (snapshot={source_commit} vs HEAD={head})")


def check_d10_ledger(report: Report, manifest: dict, capture: bool, state_root: Path) -> None:
    """D10: ledger well-formedness (only when capture enabled)."""
    if not capture:
        return
    ledger = state_root / "wiring" / "hook_ledger.json"
    if not ledger.exists():
        report.defect("D10", f"ledger missing while capture enabled: {ledger}")
        return
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.defect("D10", f"ledger unparseable: {exc}")
        return
    if data.get("schema_version") != 1:
        report.defect("D10", f"ledger schema_version must be 1 (got {data.get('schema_version')!r})")
    allowed = {(st.get("command")) for st in (manifest.get("stages") or {}).values()}
    now = datetime.now(timezone.utc)
    for entry in data.get("entries") or []:
        ts = entry.get("ts")
        try:
            dt = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            report.defect("D10", f"ledger ts unparseable: {ts!r}")
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt > now + timedelta(minutes=5):
            report.defect("D10", f"ledger ts in the future: {ts!r}")
        if entry.get("command") not in allowed:
            report.defect("D10", f"ledger command not in manifest stages: {entry.get('command')!r}")


def check_d11_ceiling_full(report: Report, extensions: dict, manifest: dict) -> None:
    check_d11_ceiling(report, extensions, manifest)


def emit_health_event(report: Report, capture: bool, manifest: dict,
                      sandbox: Path | None = None) -> None:
    """Emission rule (verifier contract): one ``wiring_health_check`` event per run,
    ONLY when capture is enabled AND the type exists in the active vocabulary
    (``_EVENT_TYPES`` membership — never importability). Disabled mode: no sessions,
    no events, no ledger writes (FR-016). Sandbox mode writes nothing (detached
    tree; the live store is never touched from a test run). Failure-tolerant
    (FR-024): any store problem degrades to an advisory label."""
    if not capture:
        report.surfaced("A8", "capture disabled: wiring telemetry inert (no events emitted)")
        return
    if sandbox is not None:
        report.surfaced("A8", "sandbox mode: wiring_health_check emission skipped (no live telemetry writes)")
        return
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from src.skillbox import telemetry as sb_telemetry  # noqa: E402

        vocab = getattr(sb_telemetry, "_EVENT_TYPES", {})
        if "wiring_health_check" not in vocab:
            report.surfaced("A8", "capture enabled but wiring_health_check not in active vocabulary: emission skipped")
            return
        total_checks = len(report.defects) + len(report.labels) + len(report.info)
        payload = {"checks_total": total_checks, "checks_failed": len(report.defects)}
        stale_labels = [label for label in report.labels if "A1" in label or "stale" in label.lower()]
        if stale_labels:
            payload["staleness_labels"] = stale_labels
        store = sb_telemetry.default_store()
        try:
            store.record_event(event_type="wiring_health_check", payload=payload, skill="verify_wiring")
        finally:
            store.close()
        report.surfaced("A8", f"emitted wiring_health_check (checks_total={total_checks}, checks_failed={len(report.defects)})")
    except Exception as exc:  # fail-soft per FR-024: emission-gate problems are A8, never A6
        report.surfaced("A8", f"emission skipped: telemetry store unavailable ({exc})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-SDLC wiring verifier")
    parser.add_argument("--sandbox", type=Path, default=None, help="run against a detached sandbox directory (test mode)")
    args = parser.parse_args(argv)

    report = Report()
    sandbox: Path | None = args.sandbox
    root = sandbox or REPO_ROOT

    ext_path = root / ".specify" / "extensions.yml"
    man_path = root / ".specify" / "wiring-manifest.yml"
    map_path = root / "docs" / "AI_SDLC_LIFECYCLE_MAP.md"

    extensions: dict | None
    manifest: dict | None
    try:
        extensions = _load_yaml(ext_path)
    except Exception as exc:
        extensions = None
        report.defect("D1", f"extensions.yml unparseable: {exc}")
    try:
        manifest = _load_yaml(man_path)
    except Exception as exc:
        manifest = None
        report.defect("D6", f"wiring-manifest.yml unparseable: {exc}")

    if isinstance(extensions, dict):
        check_config(report, extensions, manifest or {})
        check_auto_exec(report, extensions)
    if isinstance(manifest, dict):
        check_d6_manifest(report, manifest)

    if isinstance(extensions, dict) and isinstance(manifest, dict):
        capture = _capture_enabled(manifest, root)
        env = os.environ.get("METATRADER_ENV", "production")
        env_cfg_file = root / "config" / "environments" / f"{env}.json"
        state_root = _state_root(manifest, root)
        report.surfaced(
            "A8",
            f"capture state: {'enabled' if capture else 'disabled'}"
            f" (key system.skillbox.wiring_capture_enabled; source: {env_cfg_file}"
            f"{' [exists]' if env_cfg_file.exists() else ' [missing -> default false]'})",
        )

        check_d3_command_mapping(report, extensions, manifest)
        check_d5_mirror(report, extensions, manifest)
        check_d7_required_artifacts(report, manifest, sandbox, capture, state_root)
        check_d10_ledger(report, manifest, capture, state_root)
        check_d11_ceiling_full(report, extensions, manifest)
        check_d13_roster(report, extensions, manifest)
        check_context_budgets(report, manifest, sandbox)
        check_d15_marker_region_hard(report, sandbox)
        check_a2_tooling_presence(report, manifest, sandbox)
        if sandbox is None:
            check_a3_pin(report, manifest)
            check_d8_managed_surface(report, manifest)
            check_d9_marker_integrity(report, manifest)
            check_d12_graph_snapshot(report, manifest)
            check_a1_digest_freshness(report)
        check_a4_a9_capture_advisories(report, manifest, capture, state_root, sandbox=sandbox)
        check_a6_partial_loss_reconciliation(report, manifest, capture, state_root, sandbox=sandbox)

        # D4: map byte-identity via the generator's --check subprocess (sandbox
        # points the generator at the sandbox's copies).
        if sandbox is not None:
            sys.path.insert(0, str(REPO_ROOT / "scripts"))
            import generate_lifecycle_map as glm  # noqa: E402

            try:
                ext_live = _load_yaml(ext_path)
                man_live = _load_yaml(man_path)
                content = glm.render_map(ext_live, man_live, ext_path.read_text(encoding="utf-8"), man_path.read_text(encoding="utf-8"))
                if map_path.read_text(encoding="utf-8") != content:
                    report.defect("D4", "lifecycle map diverges from render (hand-edited or stale)")
            except FileNotFoundError:
                pass  # D7 already reported the missing map
            sys.path.pop(0)
        else:
            r = subprocess.run(
                [sys.executable, str(GENERATOR), "--check"],
                capture_output=True, text=True, cwd=str(REPO_ROOT),
            )
            if r.returncode == 2:
                report.defect("D4", "lifecycle map diverges from its render (hand-edited or stale)")

        emit_health_event(report, capture, manifest, sandbox=sandbox)

    # ---------------- output ----------------
    out = sys.stdout
    for item in report.defects:
        out.write(f"DEFECT {item}\n")
    for item in report.labels:
        out.write(f"LABEL {item}\n")
    for item in report.info:
        out.write(f"{item}\n")
    total = len(report.defects) + len(report.labels) + len(report.info)
    if report.defects:
        out.write(f"\nVERIFY WIRING: FAIL ({len(report.defects)} defect(s), {len(report.labels)} advisory, {total} checks)\n")
        return DEFECT
    out.write(f"\nVERIFY WIRING: PASS (0 defects, {len(report.labels)} advisory, {total} checks)\n")
    return PASS


if __name__ == "__main__":
    sys.exit(main())
