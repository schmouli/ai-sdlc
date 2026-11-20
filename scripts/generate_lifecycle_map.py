"""Generate the AI-SDLC lifecycle map from the wiring-manifest + speckit hook config.

Renders docs/AI_SDLC_LIFECYCLE_MAP.md byte-idempotently from
.specify/extensions.yml + .specify/wiring-manifest.yml (the two normative
sources). See specs/005-ai-sdlc-wiring/contracts/wiring-manifest-schema.md.

Usage:
    python scripts/generate_lifecycle_map.py            # render (write only on change)
    python scripts/generate_lifecycle_map.py --check    # exit 0 match / 2 divergence
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS_PATH = REPO_ROOT / ".specify" / "extensions.yml"
MANIFEST_PATH = REPO_ROOT / ".specify" / "wiring-manifest.yml"
MAP_PATH = REPO_ROOT / "docs" / "AI_SDLC_LIFECYCLE_MAP.md"


def _short_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _enabled_stage_names(extensions: dict) -> set:
    names = set()
    for event, entries in (extensions.get("hooks") or {}).items():
        for entry in entries or []:
            if event.startswith("before_") or event.startswith("after_"):
                if entry.get("enabled") is not False:
                    names.add(event)
    return names


def render_map(extensions: dict, manifest: dict, ext_text: str, man_text: str) -> str:
    """Produce the map content deterministically. All ordering here is fixed."""
    lines: list[str] = []
    ap = lines.append

    ap("# AI-SDLC Lifecycle Map")
    ap("")
    ap("GENERATED " + "\u2014" + " do not hand-edit. Rendered by scripts/generate_lifecycle_map.py from:")
    ap("")
    ap(f"- source: .specify/extensions.yml (sha256:{_short_sha(ext_text)}) [sole hook truth]")
    ap(f"- source: .specify/wiring-manifest.yml (sha256:{_short_sha(man_text)}) [profiles/budgets/artifacts/trigger]")
    ap("")
    ap("Every speckit stage row below is advisory-only: findings never gate implementation and never touch the trading loop.")

    profiles = manifest.get("review_profiles") or {}
    consult = manifest.get("consult_matrix") or {}
    budgets = manifest.get("token_budgets") or {}
    stages = manifest.get("stages") or {}

    enabled_now = _enabled_stage_names(extensions)

    # Consult summary over the WHOLE consult_matrix (one line, identical per row);
    # a profile with no reviewers (count == 0, e.g. DRIFT) runs in the MAIN context
    # (no subagent dispatch => no consult) and renders the dedicated cell instead.
    digest_ct = sum(1 for v in consult.values() if v == "digest")
    none_ct = sum(1 for v in consult.values() if v == "none")
    consult_cell = f"{digest_ct} digest / {none_ct} none"
    main_context_cell = "0 (main-context)"

    ap("")
    ap("## Stage table (enabled hooks)")
    ap("")
    ap("| Stage | Command | Profile | Roster (count/cap) | Boundary sentence | Telemetry events | Consult summary |")
    ap("|---|---|---|---|---|---|---|")
    ordered = sorted(
        (e for e in stages.items() if e[0] in enabled_now and not e[1].get("_disabled_derived")),
        key=lambda kv: kv[1].get("gate_order") or 99,
    )
    for event, st in ordered:
        profile = st.get("profile") or "(none)"
        prow = profiles.get(profile) or {}
        roster = ", ".join(prow.get("reviewers") or [])
        count_cap = f"{prow.get('count', 0)}/{prow.get('fan_out_cap', 0)}"
        boundary = str(st.get("boundary_sentence") or "")
        events = ", ".join(st.get("telemetry_events") or []) or "(none)"
        # zero-reviewer profiles (DRIFT) run in the main context: dedicated consult cell
        cell = main_context_cell if prow.get("count", 0) == 0 else consult_cell
        ap(f"| `{event}` | `{st.get('command')}` | {profile} | {roster} ({count_cap}) | {boundary} | {events} | {cell} |")

    # Disabled rows (derived SOLELY from extensions.yml; no manifest disabled mirror).
    disabled = sorted(e for e in stages.items() if e[0] not in enabled_now)
    if disabled:
        ap("")
        ap("## Disabled stages (derived from the hook configuration)")
        ap("")
        ap("| Stage | Command | Note |")
        ap("|---|---|---|")
        for event, st in disabled:
            rost_exempt = bool(st.get("roster_exempt"))
            note = "roster-exempt while disabled (fan-out cap not asserted)" if rost_exempt else "disabled; no telemetry emitted"
            if event == "before_specify":
                note = "concept stress-test; disabled by default (6-lens swarm exceeds the fan-out cap) " + "\u2014" + " roster-exempt while disabled"
            ap(f"| `{event}` | `{st.get('command')}` | {note} |")

    ph = manifest.get("phases") or {}
    ps = manifest.get("phase_status") or {}
    ap("")
    ap("## Phase status (wiring phases)")
    ap("")
    ap("| Phase | Title | Invariant | Status |")
    ap("|---|---|---|---|")
    for key in sorted(ph.keys()):
        info = ph[key] or {}
        status = ps.get(f"phase_{key}", "pending")
        ap(f"| {key} | {info.get('title', '')} | {info.get('invariant', '')} | {status} |")

    ap("")
    ap("## Budgets (normative values)")
    ap("")
    ap(f"- digest_max_bytes: {budgets.get('digest_max_bytes')}")
    ap(f"- inline_excerpt_max_bytes: {budgets.get('inline_excerpt_max_bytes')}")
    ap(f"- hook_decision_cap: {budgets.get('hook_decision_cap')}")
    ap(f"- silent_capture_lookback_days: {budgets.get('silent_capture_lookback_days')}")
    ap(f"- retention_days: {budgets.get('retention_days')}")
    tr = manifest.get("trigger") or {}
    ap(f"- trigger: >= {tr.get('min_lifecycle_events')} lifecycle events, >= {tr.get('min_acceptance_rate')} acceptance across >= {tr.get('min_features')} features, AND the recorded adoption confirmation")
    ap("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the AI-SDLC lifecycle map")
    parser.add_argument("--check", action="store_true", help="exit 0 when the committed map matches the render, 2 otherwise")
    args = parser.parse_args()

    ext_text = EXTENSIONS_PATH.read_text(encoding="utf-8")
    man_text = MANIFEST_PATH.read_text(encoding="utf-8")
    extensions = _load_yaml(EXTENSIONS_PATH)
    manifest = _load_yaml(MANIFEST_PATH)

    content = render_map(extensions, manifest, ext_text, man_text)
    existing = MAP_PATH.read_text(encoding="utf-8") if MAP_PATH.exists() else ""

    if args.check:
        if existing == content:
            print("CHECK PASS: lifecycle map matches render")
            return 0
        print("CHECK FAIL: lifecycle map diverges from render")
        return 2

    if existing == content:
        print(f"UNCHANGED: {MAP_PATH}")
        return 0
    # byte-idempotent write: temp file + replace
    fd, tmp = tempfile.mkstemp(dir=str(MAP_PATH.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp, MAP_PATH)
    print(f"RENDERED: {MAP_PATH} ({len(content)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
