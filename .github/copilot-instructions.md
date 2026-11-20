# AI-SDLC Project Instructions

This repository is the AI-SDLC toolkit: spec-kit workflow harness + EGM zero-context review + a Code
Knowledge Graph + SkillBox self-learning overlays + wiring telemetry/dashboard.

## AI-SDLC Lifecycle Map

The generated stage-to-tool view lives at [docs/AI_SDLC_LIFECYCLE_MAP.md](docs/AI_SDLC_LIFECYCLE_MAP.md)
— rendered from `.specify/extensions.yml` + `.specify/wiring-manifest.yml`; do not hand-edit.

## Component map

- **Spec-Kit workflow**: [`.specify/`](.specify/) — constitution, templates, PowerShell stage scripts.
- **EGM review**: [`.specify/extensions.yml`](.specify/extensions.yml) + [.specify/wiring-manifest.yml](.specify/wiring-manifest.yml)
  declare optional stage hooks, review profiles (fan-out cap 5), consult matrix, and token budgets.
- **Code Knowledge Graph**: [`scripts/run_knowledge_graph.py`](scripts/run_knowledge_graph.py) over
  [`src/kg/`](src/kg/) — for codebase-structure questions consult the graph first:
  `python scripts/run_knowledge_graph.py retrieve --query "<terms>" --format json --top-k 8`.
- **SkillBox**: [`src/skillbox/`](src/skillbox/) — telemetry, learnings, overlays; approvals are
  manual-only: `python -m src.skillbox review approve <overlay> <version> --operator <you> --reason "…"`.

## Standing rules

- Findings from EGM review, the dashboard, and verifiers are **advisory**: they never auto-gate work.
- Overlay activation is exclusively the human review path; pending versions never compose or inject.
- The lifecycle map, KG snapshot/digest, and dashboard pane are generated artifacts: edit their
  sources (manifest/extensions), never the outputs.
- Verify after changes: `python scripts/verify_wiring.py` (wiring), `python -m pytest tests/skillbox -q`
  (skillbox changes), `python scripts/run_knowledge_graph.py verify` (kg changes).

<!-- SKILLBOX:BEGIN approved-overlay-guidance (auto-generated; edit outside markers only) -->

## SkillBox Approved Overlay Guidance

Auto-generated from human-approved SkillBox overlays (specs/004-skillbox-adaptive-learning).
Pending/unapproved versions are never included; review via `python -m src.skillbox review list`.

### Testing
- **Engineering Best Practices (Seed)** `testing/engineering-best-practices@v01` (approved 2026-11-09 by schmo)
  Verify before assuming: every claimed-green change runs its focused test/verifier (the `verify_*` convention) before the task is marked complete.
  Test-first for regressions: repro a defect with a failing test, then fix; the test names the behavior it protects.
  Isolated verification: tests and verifiers use temp/synthetic state (never touch production databases, live models, or the trading runtime).
  Single source of truth: registry/DB state wins over config fallbacks; when a champion exists, it is authoritative — mirrors spec invariants for both trading models and SkillBox overlays.
  Constitution over convenience: when a quick fix conflicts with `.specify/memory/constitution.md` principles (safety gates, approval-only activation, fail-soft telemetry), the principle wins.
  EST/EDT for state: persisted timestamps use America/New_York; UTC only where an external contract requires it.
  Minimal, additive changes: prefer extending existing patterns (`src/<module>` package + verify script + focused tests) over inventing parallel structures.
  Document operational changes: any behavior change ships with its docs entry (INDEX/overview) and, for specs, updated acceptance evidence.

<!-- skillbox-digest: 0b1a3d0c039b6db5a637ea4a8e8eb06ff799820460632cec313d8458ebf5e4c1 -->
_Last refreshed: 2026-11-20T12:56:13.739470-05:00_

<!-- SKILLBOX:END -->