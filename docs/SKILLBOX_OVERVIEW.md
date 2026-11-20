# SkillBox Runtime Overview

**Spec**: [specs/004-skillbox-adaptive-learning/spec.md](../specs/004-skillbox-adaptive-learning/spec.md) |
**Plan**: [specs/004-skillbox-adaptive-learning/plan.md](../specs/004-skillbox-adaptive-learning/plan.md)

The SkillBox Runtime is an additive, self-improving learning layer around the Speckit skills
(`.github/skills/**` — never modified, FR-001). Every engineering execution produces telemetry; scheduled
analysis distills learnings; the strongest learnings stage as **pending** overlay-version documents; a
**human-in-the-loop review** decides what activates; composition delivers the approved knowledge to skill
activations; metrics prove whether the loop improves outcomes.

## The loop

```text
skill activation (Speckit) → telemetry capture (SBX-… session, typed events, decisions)
    → MLflow push (skillbox-* experiments, best-effort)
    → learning extraction (frequency / success-rate / recommendation; deterministic)
    → overlay version STAGING (append-only lineage, pending-by-default)
    → HUMAN REVIEW (approve / reject / rollback / re-activate — the ONLY activation path, FR-034)
    → composition (upstream bytes verbatim + approved overlays, attributed)
    → metrics + improvement reports (loop-effectiveness measurement)
```

## State

Everything lives under `.skillbox/` (FR-048): `registry/` (skills/overlays/metrics JSON), `telemetry/`
(SQLite WAL index + append-only JSONL source of truth + review audit hash-chain), `learnings/`,
`overlays/<collection>/<slug>/versions/vNN.md`, `reports/`. Safe-fail recovery paths are documented in the
state-files contract.

## Operator surface

```powershell
python -m src.skillbox status                     # runtime summary
python -m src.skillbox registry reindex           # upstream-scan merge (upgrade survival)
python -m src.skillbox context detect             # detected project characteristics
python -m src.skillbox events query --json        # structured query (FR-020)
python -m src.skillbox learnings extract          # run analysis on demand
python -m src.skillbox overlays generate          # stage pending versions
python -m src.skillbox review list                # pending versions with aging (SLA 7 days)
python -m src.skillbox review approve <overlay> <ver> --operator <id> --reason "..."
python -m src.skillbox metrics report             # improvement_report.md
python -m src.skillbox dashboards refresh         # skillbox_dashboard.html
python -m src.skillbox inject refresh             # refresh IDE auto-injection block (US4 extension)
python -m src.skillbox inject status              # injection state (file, block, approved count)
```

### IDE auto-injection (US4 extension)

An `inject` stage (Scheduler step 7) renders the **approved-active** overlays into a marked,
idempotent block at the end of `.github/copilot-instructions.md` — the instruction file VS Code
auto-loads into every session. Invariants: pending/rejected versions NEVER inject (SC-014);
base instruction content outside the markers is untouched; re-runs are byte-idempotent
(digest-fingerprinted); any failure leaves the existing block intact (fail-soft). One-time
seed: `python scripts/skillbox_seed_overlay.py [--approve --operator <id>]`.

Operator identity comes from `--operator` or env `SKILLBOX_OPERATOR` — review commands fail loudly without
it (never inferred, R11).

## Safety invariants (constitution-mapped)

- Upstream skills stay byte-identical (FR-001; verifier sweep).
- Zero pending/unapproved version ever composes (FR-033/SC-014 — structural: resolution recomputes and
  declines pending, rejected falls terminal).
- Metric-driven rules act fail-safe only: retire/prioritize allowed, activation never (FR-041).
- Telemetry local-first; MLflow push best-effort; disabled mode = inert no-op (FR-018/FR-019/FR-049).
- No trading-path contact: the verifier (§ isolation) asserts no trading/risk/ML-champion imports and no
  writes outside `.skillbox/` + logs.

## Verification

```powershell
& .venv\Scripts\python.exe -m pytest tests\skillbox -q        # 78 unit tests
& .venv\Scripts\python.exe scripts\verify_skillbox.py          # 45 checks across 9 sections, exit 0
```

Both are production-safe (temp state roots + synthetic telemetry).


**Feature 005 addition (telemetry capture)**: when `system.skillbox.wiring_capture_enabled` is true, scheduler step 8 runs `src.skillbox.sweep.sweep_rounds()` — converting round summaries from `logs/egm_rounds/` into `egm_review_round` events (content-hash dedup via the sweep-state file, 30-day retention, fail-soft). Five new event types (hook gates, stage completion, review rounds, wiring health) join the vocabulary; the capture path is flag-gated default-off per spec 005.
