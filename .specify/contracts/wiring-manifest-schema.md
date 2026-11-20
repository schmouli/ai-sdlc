# Contract: Wiring Manifest Schema

**Version**: 1 (`wiring_manifest_version: 1`) · **File**: `.specify/wiring-manifest.yml` · **Consumers**: `generate_lifecycle_map.py`, `verify_wiring.py`, `run_knowledge_graph.py digest`

This is the normative schema the manifest task implements. Manifest content is mirror/enumeration material; enforcement homes are the verifier contract (severities/requiredness/artifact tolerance) and `extensions.yml` (hook truth).

## Top-level keys (all required unless noted)

```yaml
wiring_manifest_version: 1

review_profiles:
  SPEC:         { reviewers: [zoom_out, comprehension, design_critic, structural], count: 4, fan_out_cap: 5 }
  READINESS:    { reviewers: [implementation_readiness, codebase_details, style],  count: 3, fan_out_cap: 5 }
  TASKS:        { reviewers: [implementation_readiness, structural, codebase_details], count: 3, fan_out_cap: 5 }
  CONSISTENCY:  { reviewers: [comprehension, design_critic, codebase_details],     count: 3, fan_out_cap: 5 }
  DRIFT:        { reviewers: [],                                                    count: 0, fan_out_cap: 0 }

consult_matrix:
  zoom_out: none
  comprehension: none
  design_critic: digest
  codebase_details: digest
  structural: digest
  implementation_readiness: digest
  style: digest
# inline fallback is GLOBAL behavior (digest absent or stale), not a profile mode

stages:
  after_specify:   { command: egm.review.loop,  profile: SPEC,      gate_order: 1, telemetry_events: [hook_gate_accepted, hook_gate_skipped, speckit_stage_completed, egm_review_round], boundary_sentence: "Advisory only: review findings never gate implementation or touch the trading loop." }  # gate_order = scalar POSITION index; per-stage gate identities live in extensions.yml
  after_plan:      { command: egm.review,       profile: READINESS, gate_order: 2, telemetry_events: [hook_gate_accepted, hook_gate_skipped, speckit_stage_completed, egm_review_round], boundary_sentence: "Advisory only: readiness findings never gate implementation or touch the trading loop." }
  after_tasks:     { command: egm.review.loop,  profile: TASKS,     gate_order: 3, telemetry_events: [hook_gate_accepted, hook_gate_skipped, speckit_stage_completed, egm_review_round], boundary_sentence: "Advisory only: task-review findings never gate implementation or touch the trading loop." }
  after_analyze:   { command: egm.review.loop,  profile: CONSISTENCY, gate_order: 4, telemetry_events: [hook_gate_accepted, hook_gate_skipped, speckit_stage_completed, egm_review_round], boundary_sentence: "Advisory only: consistency findings never gate implementation or touch the trading loop." }
  after_implement: { command: egm.drift.report, profile: DRIFT,     gate_order: 5, telemetry_events: [hook_gate_accepted, hook_gate_skipped, speckit_stage_completed], boundary_sentence: "Advisory only: drift findings never block implementation or touch the trading loop." }
  after_converge:  { command: egm.review.loop,  profile: TASKS,     gate_order: 6, telemetry_events: [hook_gate_accepted, hook_gate_skipped, speckit_stage_completed, egm_review_round], boundary_sentence: "Advisory only: convergence re-audit findings never block anything." }
  before_specify:  { command: egm.brainstorm,   roster_exempt: true, telemetry_events: [], boundary_sentence: "Concept stress-test; disabled by default (6-lens swarm exceeds the fan-out cap)." }
  after_clarify:   { command: egm.review.loop,  roster_exempt: false, telemetry_events: [], boundary_sentence: "Optional re-review after clarification edits." }

token_budgets:
  digest_max_bytes: 8192
  inline_excerpt_max_bytes: 4096
  hook_decision_cap: 8
  silent_capture_lookback_days: 14
  retention_days: 30
  # feature-008 context budgets (advisory until the one-time standing-file rewrite)
  standing_instructions_max_bytes: 32500   # ceiling for .github/copilot-instructions.md
  enforce_standing_instructions: false     # flips to true ONLY in the rewrite commit (008 B.8)
  artifact_budgets:                        # per-round Layer-B artifact family ceilings
    findings_max_bytes: 16384
    resolution_max_bytes: 20480
    drift_max_bytes: 10240

trigger:
  min_lifecycle_events: 30
  min_acceptance_rate: 0.60
  min_features: 2

phase_status:   # flipped to `complete` by the operator at each phase's green-verifier boundary, followed by map re-render + committed manifest edit; display-only (no verifier check asserts it)
  # (values: complete | pending)
  phase_0: pending
  phase_1: pending
  phase_2: pending
  phase_3: pending

wiring_adopted_at: null

artifacts:
  lifecycle_map:  { path: docs/AI_SDLC_LIFECYCLE_MAP.md, required: true }
  digest:         { path: output/kg/digest_for_reviews.md }
  hook_ledger:    { path: .skillbox/wiring/hook_ledger.json }
  round_drop_dir: { path: logs/egm_rounds/ }

egm_dependency:
  skills_root:    C:/Users/schmo/.copilot/skills
  agents_root:    C:/Users/schmo/.copilot/agents
  personas_root:  C:/Users/schmo/.copilot/egm/agents
  expected_skills:    [egm-brainstorm, egm-draft, egm-review, egm-review-loop, egm-resolve, egm-drift-report, egm-bootstrap]
  expected_agents:    [egm-zoom-out.agent.md, egm-comprehension.agent.md, egm-design.agent.md, egm-structural.agent.md, egm-style.agent.md, egm-implementation-readiness.agent.md, egm-codebase-details.agent.md, egm-synthesizer.agent.md, egm-bootstrap-orchestrator.agent.md]
  expected_personas:  [zoom_out, comprehension, design_critic, structural, style, implementation_readiness, codebase_details]
  upstream_pin:       36343a16f0e0d830a7ed78b0da97695379ab3243
  design_dir:         design/
```

## Rules

- **Stage completion**: `speckit_stage_completed` is emitted by the recording command per the completion predicate in [capture-contract.md](./capture-contract.md). `gate_order` is a scalar render-order index; the per-stage gate identities and enabled state live in `extensions.yml` and are never mirrored here.
- **Mirror equality (DEFECT)**: every `stages.<event>.command` must equal the corresponding hook's `command` in `.specify/extensions.yml` (dot→hyphen equal). The hook configuration remains the sole hook truth; the manifest mirror is checked, never authoritative.
- **Context budget sub-keys (feature 008)**: `token_budgets.standing_instructions_max_bytes` (int > 0; ceiling for `.github/copilot-instructions.md`), `token_budgets.enforce_standing_instructions` (bool; advisory→enforced at the one-time rewrite commit), and `token_budgets.artifact_budgets.*` (members keyed `<family>_max_bytes`, int > 0; required v1 members findings/resolution/drift). Values are single-normative-home facts: the convention doc links, never restates. Validation lives in `verify_wiring.py`'s additive checks; the new hard marker-region check guards the standing file's SKILLBOX region independent of D9.
- **Boundary sentences** render verbatim into the lifecycle map; they are policy display text, not enforcement.
- **Disabled state derives solely from `extensions.yml`** (single source): the manifest carries NO `disabled` mirror field for stages. A stage's disabled/render-exempt status is read from the hook configuration at render time; the roster-exemption annotation renders when the hook is disabled AND `roster_exempt: true` is declared in the manifest. Enabling a hook requires the coupled edit (both files, one change) per the rules below.
- **Adding OR enabling a hook event** = coupled wire change: `extensions.yml` + manifest `stages` + map re-render + verifier, together (subtraction allowed; the hook-decision ceiling 8 is a DEFECT-class check on wired, enabled events).
- **`profile` is REQUIRED on every non-disabled stage** (a manifest stage row for an enabled hook without a profile is a parse defect).
- **Unknown manifest top-level keys** are ignored (forward compatibility); unknown sub-keys inside `stages`/`review_profiles`/`consult_matrix` are a parse defect.
- **Validation**: parseable YAML; `wiring_manifest_version` ∈ {1}; every reviewer name exists under `personas_root`; artifact paths are well-formed (existence is enforced per the verifier contract, not here).

## Map Render Specification (normative for `generate_lifecycle_map.py`)

- **Header**: `# AI-SDLC Lifecycle Map` + `GENERATED` warning + one provenance line per source: `source: .specify/extensions.yml (sha256:<12>)`, `source: .specify/wiring-manifest.yml (sha256:<12>)`.
- **Row order**: enabled stages by `gate_order` ascending; then disabled stages alphabetically (`after_clarify`, `before_specify`).
- **Columns/order**: Stage | Command | Profile/Roster | Fan-out | Boundary sentence | Telemetry events | Consult summary.
- **Consult-summary cell**: `"<k> digest / <m> none"` where k/m count manifest-declared reviewers per consult_matrix (e.g. `"5 digest / 2 none"`); DRIFT renders `"0 (main-context)"`.
- **Disabled rows**: rendered in a separate small block, with roster-exemption annotation when `roster_exempt: true`.
- **Phase-status block**: rendered from the manifest's `phase_status` + `phases` (titles + invariants, verbatim).

## Phases block (FR-004 normative home)

```yaml
phases:
  0: { title: "Verifier/manifest/map baseline", invariant: "verifier exit 0" }
  1: { title: "Digest consultation", invariant: "verifier exit 0" }
  2: { title: "Telemetry capture", invariant: "verifier exit 0; quarantine criterion met" }
  3: { title: "Conditional expansion", invariant: "data-gated (see trigger + confirmation)" }
```
