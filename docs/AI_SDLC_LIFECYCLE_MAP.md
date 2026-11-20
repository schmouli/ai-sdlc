# AI-SDLC Lifecycle Map

GENERATED — do not hand-edit. Rendered by scripts/generate_lifecycle_map.py from:

- source: .specify/extensions.yml (sha256:0930ae8cca25) [sole hook truth]
- source: .specify/wiring-manifest.yml (sha256:056eb15ba4cb) [profiles/budgets/artifacts/trigger]

Every speckit stage row below is advisory-only: findings never gate implementation and never touch the trading loop.

## Stage table (enabled hooks)

| Stage | Command | Profile | Roster (count/cap) | Boundary sentence | Telemetry events | Consult summary |
|---|---|---|---|---|---|---|
| `after_specify` | `egm.review.loop` | SPEC | zoom_out, comprehension, design_critic, structural (4/5) | Advisory only: review findings never gate implementation or touch the trading loop. | hook_gate_accepted, hook_gate_skipped, speckit_stage_completed, egm_review_round | 5 digest / 2 none |
| `after_plan` | `egm.review` | READINESS | implementation_readiness, codebase_details, style (3/5) | Advisory only: readiness findings never gate implementation or touch the trading loop. | hook_gate_accepted, hook_gate_skipped, speckit_stage_completed, egm_review_round | 5 digest / 2 none |
| `after_tasks` | `egm.review.loop` | TASKS | implementation_readiness, structural, codebase_details (3/5) | Advisory only: task-review findings never gate implementation or touch the trading loop. | hook_gate_accepted, hook_gate_skipped, speckit_stage_completed, egm_review_round | 5 digest / 2 none |
| `after_analyze` | `egm.review.loop` | CONSISTENCY | comprehension, design_critic, codebase_details (3/5) | Advisory only: consistency findings never gate implementation or touch the trading loop. | hook_gate_accepted, hook_gate_skipped, speckit_stage_completed, egm_review_round | 5 digest / 2 none |
| `after_implement` | `egm.drift.report` | DRIFT |  (0/0) | Advisory only: drift findings never block implementation or touch the trading loop. | hook_gate_accepted, hook_gate_skipped, speckit_stage_completed | 0 (main-context) |
| `after_converge` | `egm.review.loop` | TASKS | implementation_readiness, structural, codebase_details (3/5) | Advisory only: convergence re-audit findings never block anything. | hook_gate_accepted, hook_gate_skipped, egm_review_round | 5 digest / 2 none |

## Disabled stages (derived from the hook configuration)

| Stage | Command | Note |
|---|---|---|
| `after_clarify` | `egm.review.loop` | disabled; no telemetry emitted |
| `before_specify` | `egm.brainstorm` | concept stress-test; disabled by default (6-lens swarm exceeds the fan-out cap) — roster-exempt while disabled |

## Phase status (wiring phases)

| Phase | Title | Invariant | Status |
|---|---|---|---|
| 0 | Verifier/manifest/map baseline | verifier exit 0 | complete |
| 1 | Digest consultation | verifier exit 0 | complete |
| 2 | Telemetry capture | verifier exit 0; quarantine criterion met | complete |
| 3 | Conditional expansion | data-gated (trigger + recorded confirmation) | pending |

## Budgets (normative values)

- digest_max_bytes: 8192
- inline_excerpt_max_bytes: 4096
- hook_decision_cap: 8
- silent_capture_lookback_days: 14
- retention_days: 30
- trigger: >= 30 lifecycle events, >= 0.6 acceptance across >= 2 features, AND the recorded adoption confirmation

