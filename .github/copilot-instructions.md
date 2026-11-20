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
