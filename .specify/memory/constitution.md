# MetaTrader AutoTrading System Constitution

## Core Principles

### I. End-to-End Workflow Integrity

Changes must account for the complete workflow: MT4 EA detection and telemetry, signal-file transport, Python validation and listener gates, ML decisions, journaling, risk sizing, command-file execution, trade export, and outcome stitching. A change is incomplete if it updates only one layer of a cross-layer contract.

### II. Safety-Gated Trading

Trading hours, cooldowns, validation, portfolio risk, position sizing, and execution safeguards remain active unless an approved specification explicitly changes them. MCP live-order mutations require both configuration and environment gates. No feature or maintenance task may bypass the command-file execution path.

### III. Evidence-Driven Development

Use reproducible evidence before changing behavior: EA heartbeats and rejection funnels, signal artifacts, listener logs, journal rows, model and threshold metadata, and focused tests. Separate generated candidates, ML approvals, risk approvals, executed trades, and closed outcomes in requirements and verification.

### IV. Controlled Machine Learning

ML training uses validated data and preferably time-ordered walk-forward splits with purge/embargo. The registry champion is the scoring source of truth with a fallback model. Candidate promotion is approval-gated; shadow models never gate trades; adaptive thresholds remain bounded, minimum-sample protected, and auditable.

### V. Observable and Fail-Soft Operations

Operational telemetry, MLflow, dashboards, and MCP observability may degrade without blocking valid trading paths, but failures must be logged and surfaced. Missing or stale market/regime data must follow the documented safe fallback. New decision points require structured reason codes and enough context to diagnose them.

### VI. Environment and Deployment Discipline

Production, development, and testing behavior must remain explicitly separated through `METATRADER_ENV` and configuration files. MQL4 changes require synchronization to the intended terminal data folder and compilation with zero errors and warnings. Python changes require focused tests or verifiers and documentation updates for changed operational behavior.

### VII. Minimal, Compatible Changes

Prefer existing helpers, naming, schemas, strategy patterns, and file conventions. Preserve backward compatibility unless the specification identifies a deliberate contract change, migration, rollback, and verification plan. Do not modify unrelated worktree changes or production state as part of implementation.

## Additional Constraints

- Time fields and analytics use America/New_York conventions unless an existing external contract requires another timezone.
- Signal filenames, heartbeat names, strategy identifiers, journal link tokens, and command/response formats are compatibility contracts.
- `ml_rejected` is a valid outcome for a candidate unless evidence shows a model, feature, threshold, or routing defect.
- Open trades are not closed outcomes and must not be used for calibration or win-rate calculations.
- Tests and verifiers must use isolated or synthetic state where possible; live order mutation is never a test shortcut.

## Development Workflow

For cross-cutting features, use the Spec Kit sequence:

1. `/speckit-constitution` for governance changes.
2. `/speckit-specify` for user needs, scenarios, requirements, and acceptance criteria.
3. `/speckit-clarify` when requirements or safety behavior are ambiguous.
4. `/speckit-plan` for affected files, contracts, data changes, deployment, and verification.
5. `/speckit-tasks` for ordered implementation tasks.
6. `/speckit-analyze` and `/speckit-checklist` before implementation when useful.
7. `/speckit-implement`, followed by `/speckit-converge` and focused validation.

Small isolated fixes may use the repository bug-fix workflow without a full specification. Specifications belong under `specs/`; reusable agent workflows belong under `.github/skills/`.

## Governance

This constitution governs Spec Kit artifacts and implementation plans for this repository. Every specification and plan must identify affected EA, Python, ML, risk, data, configuration, deployment, and verification surfaces when applicable. Amendments require an explicit constitution change and review of existing active specifications for compatibility.

**Version**: 1.0.0 | **Ratified**: 2026-11-03 | **Last Amended**: 2026-11-03
