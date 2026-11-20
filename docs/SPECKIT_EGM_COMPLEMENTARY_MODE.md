# Speckit Ã— EGM Complementary Mode

**Status**: Active | **Wiring**: [`.specify/extensions.yml`](../.specify/extensions.yml) | **Reviewer context**: [`.github/skills/metatrader-elephant/SKILL.md`](../.github/skills/metatrader-elephant/SKILL.md)

This repository runs Spec Kit (structure) and the Elephant-Goldfish Model (adversarial review + drift detection) as complementary toolchains. Speckit artifacts live under `specs/NNN-feature/`; EGM skills are installed at user level (`C:/Users/schmo/.copilot/skills/egm-*`) with read-only reviewer agents.

## The complementary anatomy

| Speckit provides | EGM provides |
|---|---|
| Constitution-gated artifact flow (`specify â†’ plan â†’ tasks â†’ implement â†’ converge`) | Zero-context Goldfish review of those artifacts |
| Deterministic cross-checks (`/speckit-analyze`) | LLM-judgment consistency passes and unstated-assumption hunting |
| Task ordering and `[X]` completion tracking in `tasks.md` | Readiness audits ("can an implementer execute this without making design decisions?") |
| Feature specs and plans as the source of truth | Drift detection: code vs docs, reported only as divergence |

## Hook wiring

`.specify/extensions.yml` registers EGM commands as **optional** hooks on speckit stages. When a stage runs, the skill lists the hook and you choose to run it or skip it — plain speckit sessions are unchanged. All hooks are advisory: findings never block implementation and never touch the trading loop.

The authoritative stage→tool→roster→boundary→telemetry→consult view is the **AI-SDLC Lifecycle Map** ([`docs/AI_SDLC_LIFECYCLE_MAP.md`](AI_SDLC_LIFECYCLE_MAP.md), generated from `.specify/extensions.yml` + `.specify/wiring-manifest.yml` — do not hand-edit). This document intentionally does not restate the per-stage table; on any conflict the generated map wins.

Policy (unchanged): **Fan-out cap: 5 Goldfish per round.** Hook reviews are direct-dispatched (never through `egm-synthesizer`; Copilot subagents cannot nest), pinned to the profiles in [`metatrader-elephant`](../.github/skills/metatrader-elephant/SKILL.md), and each round allows at most one re-audit. The weekly drift ritual's scope includes the map-vs-configuration pair (FR-007).

Notes:

- Hook `command` fields use dots (`egm.review.loop`); invocation maps to `/egm-review-loop`.
- `condition` fields must stay `null` — current speckit command templates skip hooks carrying non-empty conditions.
- Disable any gate with `enabled: false`; delete the entry to remove it.

## Reviewer context

Pass the [`metatrader-elephant`](../.github/skills/metatrader-elephant/SKILL.md) skill as the EGM `GoldfishContext` reading list. It maps the constitution, docs index, self-learning architecture, and the seven safety invariants (safety-gated trading, approval-gated promotion, bounded adaptive thresholds, fail-soft telemetry, closed-outcomes-only calibration, EST/EDT contracts, verify-script coverage) into audit criteria reviewers apply to every spec, plan, and drift report.

## Operation

```text
# Typical feature, using complementary gates at the moments that add signal:
/egm-brainstorm <feature idea>        # upstream concept stress-test (optional)
/speckit-specify <feature idea>       # accepts the concepts brief as input
/speckit-clarify                      # if requirements are ambiguous
/speckit-plan <technical choices>
/speckit-tasks
/speckit-analyze
[accept after_analyze hook: /egm-review-loop CONSISTENCY pass (3 Goldfish)]
/speckit-implement
[accept after_implement hook: /egm-drift-report on specs/NNN-feature/]
/speckit-converge
[accept after_converge hook: /egm-review-loop TASKS re-audit (3 Goldfish)]

# Weekly upkeep ritual (EGM recommendation, adopted here):
#   lifecycle map: docs/AI_SDLC_LIFECYCLE_MAP.md (generated; map-vs-configuration pair is in drift scope per FR-007)
/egm-drift-report specs               # reconcile doc-vs-code drift; human picks the fix side
```

Wired hook reviews fan out at most 5 Goldfish per round (spec gate: 4; plan/tasks/analyze/converge gates: 3; drift: 0). The full staged `/egm-review` flow (up to 9 dispatches in one pass) and `/egm-brainstorm` (6) remain available as manual one-offs but are never fired by hooks. Goldfish reviewers are strictly read-only; `/egm-review-loop` may propose fixes that you approve before anything is edited.

## Safety boundary

- Goldfish reviews are **advisory**: they never gate `speckit-implement`, never mutate trading configuration, models, registry, or trading state, and cannot amend the constitution.
- EGM skills are manual (`disable-model-invocation: true`): nothing reviews mid-task unless you accept a hook or invoke a command.
- Governance remains [`.specify/memory/constitution.md`](../.specify/memory/constitution.md); the EGM layer adds review pressure, not authority.
