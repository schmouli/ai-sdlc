# Contract: Telemetry Capture Contract

**Flag**: `system.skillbox.wiring_capture_enabled` (default `false` in every configuration). Enablement runbook (tracked + machine-local config edits, `wiring_adopted_at` set, 20-event quarantine watch) is a mandatory section of the capture-protocol documentation. · **Schemas home**: spec-004 amendment (`specs/004-skillbox-adaptive-learning/amendments/2026-11-11-wiring-capture.md`, created in Phase 2, authored against the Phase-0 inline format in the review-context skill) · **This document is the authoritative spec-005-side contract the amendment mirrors.**

This is the content the spec-004 amendment carries (event schemas, ingestion co-delivery, ledger format, round-summary schema, sweep state). With capture disabled, the entire path is inert: no sessions, no events, no ledger writes; the verifier surfaces the inert state as information.

## Recording command (the single documented act)

```
python scripts/record_hook_acceptance.py --event accepted|skipped --hook-command <command> --stage <hook_event> --feature-dir <dir> [--operator <id>]
```

Behavior (ledger is the ledger-of-record; its path ALWAYS resolves from the configured SkillBox `state_root` — the manifest's artifacts.hook_ledger is mirror content, NEVER the resolution source): **flag off → exit 0** with an explicit inert label on stderr (no ledger writes, no events — "neither record lands" quantifies failure, not inertness); flag on: append ledger entry first; record telemetry event second (in-process); partial failure logged and surfaced, never blocking the gate; exit non-zero only when **neither** record lands. Emits `hook_gate_accepted` or `hook_gate_skipped`; when the completion predicate fires — the authoritative rule, stated once here: FOR `--event accepted`, emit `speckit_stage_completed` iff every hook entry of that stage currently `enabled: true` in `.specify/extensions.yml` has a prior accepted ledger entry for this `feature_dir`, including this one. Re-acceptance re-emits completion (ledger is append-only, duplicates are data); the Phase-3 tally deduplicates per stage-directory. Additionally emits `speckit_stage_completed` (once per stage-directory in the tally).

## Event payload schemas (authoritative; required-key enforcement for these five only)

Note: **five** event types (the standalone consult event was struck in review; consults ride round-summary fields below). Existing permissive validation applies to all pre-existing types (unknown type ⇒ quarantine, unchanged).

| Type | Required fields | Optional |
|---|---|---|
| `hook_gate_accepted` | `hook_event`, `hook_command`, `feature_dir` (str) | `operator`, `stage_completed` (bool, set true by the completion predicate) |
| `hook_gate_skipped` | `hook_event`, `hook_command`, `reason` (str) | `feature_dir` |
| `speckit_stage_completed` | `stage`, `feature_dir`, `decision` ("accepted"), `recorded_by` | `operator` |
| `egm_review_round` | `profile`, `target_file`, `reviewers` (list), `findings_count` (int), `round_kind`, `consult_mode`, `consult_freshness`, `consult_fallback` (bool) | `session_note` |
| `wiring_health_check` | `checks_total`, `checks_failed` (int) | `staleness_labels` (list) |

Every row carries `schema_version: 2` in the JSONL record builder. Unknown schema generations: dropped + logged (regime activates only when a third generation exists).

## Store changes (additive, co-delivered in the same change)

1. `telemetry.py::_EVENT_TYPES` + 5 entries; add public alias `EVENT_TYPES`.
2. `telemetry.py::validate_payload`: required-key sets for the five types (pre-existing types keep the permissive contract).
3. Schema stamp: `schema_version: 2` written in the JSONL record builder; SQLite: guarded `ALTER TABLE events ADD COLUMN schema_version` in `init_db` (try/except for already-migrated DBs). Reconciliation with the store's global version: the per-row stamp is normative (this contract wins — the design's `event_schema` name is superseded); the store's global `schema_meta.SCHEMA_VERSION` **stays 1** (bumping it would falsely advertise a second schema generation, contradicting FR-013); the `ALTER TABLE` is idempotent against existing DBs whose `schema_meta` says 1.
4. Loaders (`query_events`, `_restore_event`, `rebuild_from_jsonl`): accept rows with/without the stamp — verified once by an extension of `tests/skillbox/test_telemetry.py` (which already exercises `rebuild_from_jsonl`; no new file).
5. Config: `get_skillbox_config()` merges `wiring_capture_enabled`. Tracked-file edit shape: `development.json` gains the key inside its existing `system.skillbox` block; `production.jsonc` has NO `system.skillbox` block today → create it containing ONLY `wiring_capture_enabled: false` (no `enabled` key — inherit the existing default-false elsewhere; adding `enabled` here would change live production behavior and is prohibited).
6. Scheduler: step 8 — sweep when flag enabled.

## Ledger format (`.skillbox/wiring/hook_ledger.json`)

```json
{"schema_version": 1, "entries": [
  {"ts": "2026-11-10T15:00:00-05:00", "event": "after_tasks", "command": "egm.review.loop",
   "decision": "accepted", "feature_dir": "specs/005-ai-sdlc-wiring", "operator": "schmo"}
]}
```
- Committed seed in Phase 0; appends = working-tree churn (tracked intentionally).
- Well-formedness (when capture enabled): parseable ts; no future ts; event/command present in manifest; duplicates distinct (data).

## Round summaries (drop dir `logs/egm_rounds/`; written by the review orchestration per FR-017)

```json
{"round_id": "20261110T150000-0500-after_specify-SPEC",
 "profile": "SPEC", "target_file": "specs/005-.../spec.md",
 "reviewers": ["zoom_out", "comprehension", "design_critic", "structural"],
 "findings_count": 4, "round_kind": "spec-review", "recorded_at": "<ISO EST>",
 "consult": {"mode": "digest", "freshness": "fresh|stale|absent|fallback", "fallback": false}}
```
- **Mandatory**: round_id, profile, target_file, reviewers, findings_count, round_kind, recorded_at, consult. Consult fields make round summaries the single capture surface for graph consults (the standalone event type was struck).
- Sweep: idempotent (content-hash dedup via persisted state under `.skillbox/wiring/sweep_state.json`, bounded by the retention window); malformed skipped with logged reason; retention cleanup 30 days; no-op when flag off.
- Manual (non-round) graph consults: **not recorded** (accepted limitation, per spec Assumptions).

## Audit-chain boundary

Gate decisions live **only** in the hook ledger. Overlay version-state decisions continue through the existing review-approval hash chain (`review_decisions.json`) — untouched, no new record kinds, no reader extension (spec-005 documented divergence from the design).

## Ingestion co-delivery + regression gates

- The schema extension, ingestion handling, config merge, scheduler step, and the regression tests land **in the same change**.
- Regression gates (tests named in the capture-protocol doc): `tests/wiring/test_content_exclusion.py` (ledger rows, round summaries, event payloads, and the digest walked against the schema field whitelist — SC-011) plus: `test_no_quarantine.py` (five types ⇒ primary store, zero quarantine; unknown type still quarantines), `test_sweep.py` (double-sweep idempotence within retention window; malformed skip; disabled no-op; 30-day cleanup), `test_record_hook_acceptance.py` (ledger well-formedness; event co-record; completion predicate; partial-failure ordering).
- Rollout criterion: flag flips per environment only after (a) ingestion live in that environment, (b) **0 quarantined events over the first 20 live events**, (c) verifier exit 0.

## Trigger tally (Phase 3 evaluator; procedure named in the capture-protocol doc)

Inputs: hook ledger (working-tree state — the ledger-of-record) + event store. Legs: ≥30 non-health events since `wiring_adopted_at`; acceptance ≥60% across ≥2 features (denominator = recorded gate decisions in window; an unusable window is labeled, never zero-counted). Gating confirmation: manifest key `expansion_adoption_confirmation` present (non-empty line + date). Dedup: stage completion counted once per stage-directory. Output: locked | unlocked-with-evidence (rendered by the Phase-3 status screen when unlocked).
