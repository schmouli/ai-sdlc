# SDLC Artifact Convention

**Schema**: sdlc-v1 · **Effective**: 2026-11-13 (birth rule: tickets numbered **007 and later**) · **Status**: normative, change-controlled

This document is the single normative source for Layer-B review & measurement artifacts in this repository. The generator (`scripts/generate_sdlc_pane.py`) renders from it and never restates a second copy of its tables; banner behavior may not change without a same-commit amendment to this document. On divergence between code and this document, **this document governs**.

---

## 1. Ticket directories

Every AI-SDLC feature lives in `specs/<3-digit>-<name>/` (regex `^specs/\d{3}-[a-z0-9-]+$`). The directory scan is the census — there is no registry.

**Ticket-directory precedence**: artifacts written into the ticket directory are canonical. Cwd-written reports from installer skills are disposable duplicates the generator never reads.

**Smoke-dir exclusion**: directories whose path fails the ticket regex (e.g. `specs/smoke-f5-test`) are excluded from all trend panels and rendered in a separate non-ticket listing — visible, never silently dropped. An entry matching the regex whose directory is absent is a **ghost** (and appears in both listings when both apply).

**NNN allocation discipline**: ticket numbers come from `create-new-feature.ps1`'s specs-aware directory scan (next free number); `-Number N` exists for explicit picking. Parallel sessions use staggered starts or pre-created `-Number N` directories.

---

## 2. Layer-B artifacts (this convention)

Layer A (spec-kit canonicals — `spec.md`, `plan.md`, `tasks.md`, `research.md`, `data-model.md`, `quickstart.md`, `contracts/`, `checklists/`) is unchanged and unwired. Every **Layer-B artifact** (review & measurement file in the ticket root) MUST carry the YAML frontmatter block below.

### 2.1 Frontmatter schema block (sdlc-v1)

```yaml
---
schema: sdlc-v1
stage: after_specify
round: 2
attempt: 1
family: findings
findings_count: 5
resolved_count: 4
run: 1
written_at: 2026-11-13T21:35:00-05:00
---
```

- `schema` — collection schema version. This design pins `sdlc-v1`; a future revision lands as `sdlc-v2` (rendering an UNKNOWN-SCHEMA banner, never a silent re-interpretation).
- `stage` — the exact speckit stage name (`after_specify` … `after_converge`). Typos get INVALID-STAGE, never silent ignore. `stage` is the join key: filenames alone do not disambiguate stages.
- `round` — int, 1-based, stage-local. See requiredness matrix.
- `attempt` — int ≥ 1; `1` by default; incremented only when a stage is genuinely re-reviewed; the filename gains `_attemptN`.
- `family` — `findings` | `resolution` | `readiness` | `drift` | `consistency`.
- `findings_count` / `resolved_count` — ints; `0` = clean review (a fact, not an omission).
- `run` — int, drift only: `1` = the metric run; `>1` = versioned snapshot.
- `written_at` — session stamps at write time, UTC ISO-8601; mtime is the degraded fallback.

### 2.2 Requiredness matrix (verbatim from the design's Metric definitions)

| Field | Required for | Optional/omitted for | Absent ⇒ |
|---|---|---|---|
| `schema` | all | — | INVALID-FRONTMATTER |
| `stage` | all | — | INVALID-FRONTMATTER (file listed, not counted) |
| `family` | all | — | INVALID-FRONTMATTER |
| `round` | findings, resolution, consistency | readiness (singleton), drift (uses `run`) | NULL-COUNT-class warning |
| `attempt` | all — `1` by default; incremented only when a stage is genuinely re-reviewed | — | defaults to 1 |
| `findings_count` | findings, drift, consistency, readiness, combined-file resolution | separate resolution files | `n/a` + NULL-COUNT |
| `resolved_count` | resolution (incl. combined-file) | all others | `n/a` + NULL-COUNT |
| `run` | drift only (1 = metric run; >1 = snapshot) | all others | — |
| `written_at` | all (session stamps at write time, UTC ISO-8601) | — | degrades window/ORPHAN sourcing to mtime (documented as degraded) |

### 2.3 Frontmatter state machine (three disjoint states)

1. **INVALID-FRONTMATTER** — frontmatter absent or unparseable, OR a required field present but empty-valued (non-null) or wrong-typed (per file).
2. **NULL-COUNT** — frontmatter parses but a required field (count or non-count, per the matrix) is missing or null → the cell renders `n/a`. (Naming history: the name predates the generalization from count fields to any required field; it is retained for continuity.)
3. **Valid** — the value is data; `0` = clean review.

Known-schema boundary: NULL-COUNT applies under the known schema (`sdlc-v1`). A future-schema artifact (UNKNOWN-SCHEMA) renders known fields and no-data for unknown fields — best-effort, banner noted.

### 2.4 Glyph legend (the three-value rule)

| Glyph | Meaning | Example cell |
|---|---|---|
| `0` | the number zero (clean review) | findings count of a converged round |
| `n/a` | no data (field absent/null on a valid artifact) | findings count missing from frontmatter |
| `—` | not captured (the artifact family/dataset is absent entirely) | analyze rounds where none ran |
| `(0, 3)` | parenthetical = clean-round breakdown | 2 rounds, one clean, one with 3 findings |

---

## 3. Stage → artifact naming table (8 rows)

| Stage | Layer-B artifact(s) (ticket root) | Family | Written by | Notes |
|---|---|---|---|---|
| before_specify | none (hook disabled) | — | — | exemption row: no artifact expectation |
| after_clarify | none (hook disabled) | — | — | exemption row |
| after_specify | `findings_roundN[_attemptM].md`, `resolution_roundN[_attemptM].md` | findings / resolution | review session per loop round | N = internal loop round, 1-based; attempt suffix only on re-reviews |
| after_plan | `implementation_readiness_report.md` | readiness | plan-audit session | singleton (no `round`); carries `findings_count` |
| after_tasks | `findings_tasks_rN[_attemptM].md`, `resolution_tasks_rN[_attemptM].md` | findings / resolution | review session per loop round | separate series from after_specify (`_tasks_`) |
| after_analyze | `consistency_report_roundN[_attemptM].md` | consistency | analyze session per loop round | same loop-round semantics as findings rounds |
| after_implement | `drift_report_run1.md` (`_run2.md`, …) | drift | drift session | run-suffixed (first run = metric) |
| after_converge | `findings_tasks_rN[_attemptM].md`, `resolution_tasks_rN[_attemptM].md` | findings / resolution | review session on re-audit rounds | same filenames as after_tasks; N continues that series; `stage: after_converge` in frontmatter separates the two stages |

**Special forms**:
- **Combined-file converge form** (a converged-final round may be a single combined file): the resolution file carries BOTH `findings_count` (the count of findings it itself contains) and `resolved_count`.
- **Clean intermediate round**: a loop round that converged mid-loop (zero findings, below cap) writes `findings_roundN.md` with `findings_count: 0` plus its `resolution_roundN.md` — not the combined-file form.

**Drift naming precedence**: the convention name `drift_report_runN.md` (in the ticket folder) wins; a cwd-written dated copy from the skill remains disposable — the ticket-folder file is canonical.

---

## 4. Counting rules

- **Convention mode**: a stage's round count = distinct (round, attempt) pairs across that stage's Layer-B artifacts, frontmatter-authoritative (a resolution-only clean round counts).
- **Fallback mode** (any ticket whose Layer-B files lack parseable frontmatter — pre-convention or a missed birth-rule ticket, any era): file-count rounds with an "unverified, no frontmatter" footnote; findings counts stay `n/a`, never zero.
- **`_tasks_` series attribution**: every `*_tasks_*`-series file attributes to after_tasks; converge columns fold into Tasks columns (stage attribution visible in drill-downs).
- Drift/readiness/consistency columns render `—` when their artifact family is absent, `n/a` when present without counts.
- Files without valid frontmatter are listed (drill-down), never metric-parsed or double-counted.
- **Birth rule**: tickets numbered 007+ are convention-era; 001–006 render pre-convention (banner-free, own section). Ticket 007 counts as convention-era with MISSED-CONVENTION until its artifacts exist.

**Duty-activation rule**: artifact-writing duties activate only after the duty Phase (feature 007 Phase 3) merges — the single-user window between commits is explicitly accepted. Before that merge, 007 legitimately renders MISSED-CONVENTION.

**Prompt-amendment date**: the six hook duty sentences were appended to `.specify/extensions.yml` on **2026-11-14** (feature 007, T010/T011 one-commit ride-along; amended-in-place, prompts carry the duty sentences verbatim).

---

## 5. Canonical integrity predicate table (feature-008 amended: 14 → 18 predicates; 2026-11-14, feature 008 same-commit amendment)

Per-ticket predicates are evaluated for **convention-era tickets only** (001–006 render banner-free pre-convention). Repo-level predicates are **always** evaluated. Data warnings from parse failures render outside this canon.

| # | Predicate | Scope | Predicate |
|---|---|---|---|
| 1 | MISSING-ARTIFACT | per-ticket | an accepted ledger row whose stage/ticket has no matching family artifact (render-time state; a `skipped` row renders "skipped — no expectation", suppressed banner, skip visible in the Ledger stages column) |
| 2 | ORPHAN-ARTIFACT | per-ticket | a family artifact with no accepted ledger row for that (feature_dir, stage) after dedupe — set-membership test, no time window; suppressed when `written_at` is null (INVALID-FRONTMATTER governs then) |
| 3 | NULL-COUNT | per-ticket | a required field missing or null in otherwise-parseable frontmatter under the known schema — count fields and non-count fields alike; a future-schema artifact renders no data with UNKNOWN-SCHEMA instead |
| 4 | GAP | per-ticket | round/attempt numbering gap within (stage, attempt) |
| 5 | DRIFT-FIRST-RUN-MISSING | per-ticket | drift series present without `run: 1` → drift metric renders `n/a` |
| 6 | MISSED-CONVENTION | per-ticket | convention-era ticket lacking convention artifacts |
| 7 | INVALID-FRONTMATTER | per-ticket | frontmatter absent or unparseable, or required field present but empty-valued (non-null)/wrong-typed — per file |
| 8 | FAMILY-STAGE-MISMATCH | per-ticket | filename family contradicting frontmatter `stage` (file listed, counts excluded) |
| 9 | INVALID-STAGE | per-ticket | frontmatter `stage` naming no wired stage |
| 10 | UNKNOWN-SCHEMA | per-ticket | collection schema version newer than the known one — known fields render, unknown-field data renders as no data, banner notes the schema version |
| 11 | CAPTURE-DISABLED | repo-level | persistent watermark when `wiring_capture_enabled` is false |
| 12 | TRUNCATED-LINE | repo-level | truncated series line, skipped on render |
| 13 | KG-SERIES-LAG | repo-level | an evaluation report exists whose `snapshot_commit` + `suite_version` matches no series line |
| 14 | TRACKED-OUTPUT-WARNING | repo-level | a pane output (the pane or the benchmark series) is excluded by version control |
| 15 | BUDGET-BREACH | repo-level (fires per ticket) | a recent-round budgeted-family artifact (findings/resolution/drift) whose byte size exceeds its family ceiling — most recent round per ticket only; **advisory data, never a gate** |
| 16 | SERIES-UNCOMMITTED | repo-level | `data/context_budget_series.jsonl` modified or untracked per `git status --porcelain` — commit the render to advance the tracked trend |
| 17 | SERIES-DUPLICATE | repo-level | ≥2 retained lines in a size series share the same measurement identity (a failed supersede) — resolve before trusting the trend |
| 18 | BUDGET-MISSING-STANDING | repo-level | `.github/copilot-instructions.md` absent at render — standing size renders as a banner, never a zero |

**Composition note — ORPHAN-ARTIFACT ∘ INVALID-FRONTMATTER**: the two never co-fire for the same file; invalid-frontmatter files are suppressed from ORPHAN evaluation (INVALID-FRONTMATTER governs; the file is listed, not counted).

---

## 6a. Size budgets (feature 008, added 2026-11-14) — semantics only; the manifest is the single normative home for values

The one normative home for every budget **number** is the wiring manifest: `token_budgets.standing_instructions_max_bytes` (ceiling for `.github/copilot-instructions.md`, ~25K tokens at the pane's ÷4 display proxy) and `token_budgets.artifact_budgets.*` (`findings_max_bytes`, `resolution_max_bytes`, `drift_max_bytes`). This section restates no numbers (by charter: no second copy of normative tables); it defines the semantics the generator and sessions must honor:

- **Breach = advisory pane data, never a gate.** A `BUDGET-BREACH` banner (§5 row 15) renders when a budgeted-family artifact's byte size exceeds its manifest ceiling. Nothing blocks, fails, or gates on it.
- **Applicability: most recent round per ticket only.** Within each naming series (findings_round\*, resolution_round\*, findings_tasks_r\*, resolution_tasks_r\*, drift_report_run\*) per ticket and attempt, only the highest round's files fire breach banners; finished tickets emit no noise. Readiness and consistency artifacts are deliberately unbudgeted (rare families; the pane still sizes them, so unbounded growth stays visible).
- **Remediation: disposition in the NEXT round's artifact.** Historical round-stamped records are NEVER edited to fit a budget. A budget change is a visible manifest commit, effective from the next render; the size series records the change via its identity key.
- **Standing-file ceiling asymmetry**: only the standing-instructions ceiling is ever enforceable — advisory until the owner's one-time approved rewrite flips `token_budgets.enforce_standing_instructions` (same commit as the rewrite), enforced after. Artifact budgets are advisory in perpetuity.
- **Standing-size history**: `data/context_budget_series.jsonl` (append-only, tracked, keep-last per measurement identity `(git_head, standing_bytes, artifact_total_bytes, breach_set_hash)`) is written by the pane generator at render time; series data-quality banners `SERIES-UNCOMMITTED` (§5 row 16) and `SERIES-DUPLICATE` (§5 row 17) surface uncommitted/duplicate captures; `BUDGET-MISSING-STANDING` (§5 row 18) renders when the standing file is absent (a banner, never a zero).

---

## 6. KG benchmark series — report → series field mapSurface: `data/kg_eval_series.jsonl` — append-only, tracked, one JSON object per evaluation run; written only by `python scripts/generate_sdlc_pane.py --record-kg-run`; never by the evaluator; never rewritten.

| Report field (`output/kg/evaluation_report.json`) | Series field | Rule |
|---|---|---|
| `graph_commit_sha` | `snapshot_commit` | verbatim |
| `git rev-parse HEAD` at record time | `recorded_head` | record-time truth (the report carries no run HEAD of its own; documented pairing is evaluate → then record) |
| `suite_version` | `suite_version` | verbatim; segment key for the trend (a suite edit starts a new segment, never a spurious jump) |
| `aggregate` per method/category | `hit_rate`, `mean_cost` | verbatim per easy/hard columns; no computed rollup; the payload has no "all" category and no node count |
| record moment | `ts_utc` | moment of `--record-kg-run`; staleness baseline |
| report file bytes | `report_content_hash` | hash over report file bytes at read time; part of the dedupe key |

**Refusal rules** (every refusal states its reason; exit 0 with the reason on stderr): dedupe-key match (`recorded_head` + `snapshot_commit` + `report_content_hash`) unless `--force`; report mtime older than the previous line's record timestamp (staleness); schema/partial-payload failure (never appends). First record on an empty series passes staleness vacuously. A truncated last line renders TRUNCATED-LINE and is skipped, never fatal.

---

## 7. Hook duty sentences (reproduced verbatim from the design § Capture wiring)

The six sentences below are appended to the six enabled hook prompts in `.specify/extensions.yml` (post-review documentation duties — never gate preconditions; the recorder command sentence is unchanged):

1. **after_specify**: "After the review completes, write `findings_roundN.md` and `resolution_roundN.md` (one pair per internal loop round; `_attemptN` filename suffix and `attempt: M` frontmatter only on genuine re-reviews) into this feature's directory, with `specs/SDLC_ARTIFACT_CONVENTION.md` frontmatter — counts in frontmatter are mandatory; this is a post-review documentation duty, never a gate precondition."
2. **after_plan**: "After the readiness audit completes, write `implementation_readiness_report.md` (singleton; no `round` field) into this feature's directory, with `specs/SDLC_ARTIFACT_CONVENTION.md` frontmatter carrying `findings_count` — mandatory; post-review documentation duty, never a precondition."
3. **after_tasks**: "After the review completes, write `findings_tasks_rN.md` and `resolution_tasks_rN.md` (one pair per internal loop round; `_attemptN` suffix only on re-reviews) into this feature's directory, with `specs/SDLC_ARTIFACT_CONVENTION.md` frontmatter — counts mandatory; post-review documentation duty, never a precondition."
4. **after_analyze**: "After the consistency pass completes, write `consistency_report_roundN.md` (one per internal loop round) into this feature's directory, with `specs/SDLC_ARTIFACT_CONVENTION.md` frontmatter carrying `findings_count` — mandatory; post-review documentation duty, never a precondition."
5. **after_implement**: "After the drift review completes, write `drift_report_run1.md` (run-suffixed; run 1 is the metric; later runs are snapshots) into this feature's directory, with `specs/SDLC_ARTIFACT_CONVENTION.md` frontmatter carrying `findings_count` — mandatory; post-review documentation duty, never a precondition. This supersedes the dated cwd filename; the ticket-folder file is canonical."
6. **after_converge**: "After the re-audit completes, write `findings_tasks_rN.md` and `resolution_tasks_rN.md` continuing the after_tasks round numbering, with `stage: after_converge` in the `specs/SDLC_ARTIFACT_CONVENTION.md` frontmatter — counts mandatory; post-review documentation duty, never a precondition."

---

## 8. Worked example artifact (one per family)

### findings (after_specify, round 2)

```markdown
---
schema: sdlc-v1
stage: after_specify
round: 2
attempt: 1
family: findings
findings_count: 3
written_at: 2026-11-13T21:40:00-05:00
---

# Findings — Round 2

**F1 (Major)** — ...
```

### resolution (after_tasks, round 1)

```markdown
---
schema: sdlc-v1
stage: after_tasks
round: 1
attempt: 1
family: resolution
resolved_count: 4
written_at: 2026-11-13T22:10:00-05:00
---

# Resolution — Round 1
```

### readiness (after_plan singleton — no `round`)

```markdown
---
schema: sdlc-v1
stage: after_plan
attempt: 1
family: readiness
findings_count: 2
written_at: 2026-11-13T23:00:00-05:00
---

# Implementation readiness report
```

### drift (after_implement, first run = the metric)

```markdown
---
schema: sdlc-v1
stage: after_implement
attempt: 1
family: drift
run: 1
findings_count: 2
written_at: 2026-11-14T09:00:00-05:00
---

# Drift report — run 1
```

### consistency (after_analyze)

```markdown
---
schema: sdlc-v1
stage: after_analyze
round: 1
attempt: 1
family: consistency
findings_count: 5
written_at: 2026-11-13T23:30:00-05:00
---

# Consistency report — Round 1
```

### combined-file converge resolution (after_converge)

```markdown
---
schema: sdlc-v1
stage: after_converge
round: 4
attempt: 1
family: resolution
findings_count: 3
resolved_count: 3
written_at: 2026-11-14T10:00:00-05:00
---

# Converged final round (combined findings + resolution)
```

---

## 9. Provenance of this document

Pinned verbatim-from: `specs/007-sdlc-metrics/design.md` § Metric definitions (requiredness matrix), § Stage→artifact naming table (8-row table + schema block), § Collector & pane (glyph rule provenance), § KG series (field map), § Capture wiring (six duty sentences). Change control: banner/predicate behavior changes require a same-commit amendment here; the generator's SC-003 fixtures are the equivalence check.
