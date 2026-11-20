# Engineering Best Practices — Seed Overlay

Collection: testing
Provenance: seed-authored best-practices skeleton (not telemetry-derived); staged through the
normal version lineage so activation follows the standard human-approval gate (spec FR-034).

Evidence basis: repository-observed practice from features 001–004 (spec-driven workflow,
test-first verification convention, EST timezone contract, fail-soft telemetry). Seed rationale:
the overlay library starts empty and the approval UX needs a real review target; evidence bars
(spec FR-026) apply to telemetry-derived learnings, while this seed carries operator-authored
acceptance — its version lineage records that distinction in `evidence_summary.seed: true`.

## Recommendation

- Verify before assuming: every claimed-green change runs its focused test/verifier (the
  `verify_*` convention) before the task is marked complete.
- Test-first for regressions: repro a defect with a failing test, then fix; the test names the
  behavior it protects.
- Isolated verification: tests and verifiers use temp/synthetic state (never touch production
  databases, live models, or the trading runtime).
- Single source of truth: registry/DB state wins over config fallbacks; when a champion exists,
  it is authoritative — mirrors spec invariants for both trading models and SkillBox overlays.
- Constitution over convenience: when a quick fix conflicts with `.specify/memory/constitution.md`
  principles (safety gates, approval-only activation, fail-soft telemetry), the principle wins.
- EST/EDT for state: persisted timestamps use America/New_York; UTC only where an external
  contract requires it.
- Minimal, additive changes: prefer extending existing patterns (`src/<module>` package +
  verify script + focused tests) over inventing parallel structures.
- Document operational changes: any behavior change ships with its docs entry (INDEX/overview)
  and, for specs, updated acceptance evidence.
