# Capture-Protocol: AI-SDLC Wiring (Feature 005 US4)

One page. The recording command, ledger schema, sweep step, trigger-evaluation
tally procedure, and required regression-test names for the telemetry capture
contract. Contract: [specs/005-ai-sdlc-wiring/contracts/capture-contract.md](../specs/005-ai-sdlc-wiring/contracts/capture-contract.md).

## Recording command (the single documented act)

```text
python scripts/record_hook_acceptance.py --event accepted|skipped --hook-command <command> --stage <hook_event> --feature-dir <feature_dir>
```

Ledger-first in-process write (the ledger is the ledger-of-record); partial failure
logged and surfaced; exit non-zero only when neither record lands. When capture is
disabled the command is inert (exit 0, stderr label "INERT"). Hook prompts carry
the recording-command pointer; the capture-protocol documentation is the sole
compliance mechanism. The ledger path resolves from `METATRADER_ENV`
(default `production`) → `config/environments/{env}.json` →
`system.skillbox.state_root` — the same precedence `scripts/verify_wiring.py`
uses. A legacy ledger with a UTF-8 BOM is read via `utf-8-sig`; rewritten ledgers
are always BOM-free UTF-8.

## Hook ledger schema

`.skillbox/wiring/hook_ledger.json` (path resolves from the configured SkillBox
`state_root`):

```json
{"schema_version": 1, "entries": [
  {"ts": "<ISO>", "event": "<hook_event>", "command": "<command>",
   "decision": "accepted|skipped", "feature_dir": "<feature_dir>", "operator": "<optional>"}
]}
```

## Sweep scheduler step

Scheduler step 8 in `scripts/skillbox_scheduler.py::run_pass` calls
`src.skillbox.sweep.sweep_rounds()` when `system.skillbox.wiring_capture_enabled`
is true. Input: the manifest `artifacts.round_drop_dir` (`logs/egm_rounds/`)
containing `round-*.json` summaries (written by the review orchestration duty in
the review-context skill). Content-hash dedup via
`.skillbox/wiring/sweep_state.json`, pruned to the 30-day retention window;
malformed summaries are skipped with the file name + reason logged; retention
cleanup after 30 days.

## Trigger-evaluation tally procedure (FR-021)

Inputs: hook ledger (working-tree state) + event store.
Legs: ≥ 30 lifecycle events (excluding wiring-origin health events, accumulated
since `wiring_adopted_at`) AND gate-acceptance rate ≥ 60% across ≥ 2 features
(denominator guarded by the silent-capture advisory).
Confirmation: manifest key `expansion_adoption_confirmation` (the maintainer's
recorded judgment line) must be present.
Evaluated by: the documented trigger evaluation (executed only on demand).

## Required regression-test names

`tests/wiring/test_no_quarantine.py`, `tests/wiring/test_record_hook_acceptance.py`,
`tests/wiring/test_sweep.py`, `tests/wiring/test_content_exclusion.py`,
extended `tests/skillbox/test_telemetry.py` (v1-row compat).

## Enablement runbook (flag flip)

Governing specification (divergence — spec wins):
`specs/006-ai-sdlc-telemetry-wiring/spec.md`.

1. Edit `config/environments/development.json` (tracked): set
   `system.skillbox.wiring_capture_enabled: true` (the key already exists
   there, value `false`).
2. In machine-local `config/environments/production.json` (gitignored,
   hand-edited), ADD `wiring_capture_enabled: true` inside the existing
   `system.skillbox` object — the file has no such key before this rollout.
   The runtime reads `.json` only. In the SAME change, update the tracked
   template `config/environments/production.jsonc` (currently pinned `false`;
   it is not a runtime source) so its recorded posture matches.
3. Set `wiring_adopted_at` in `.specify/wiring-manifest.yml` (America/New_York
   local wall-clock timestamp) — only AFTER both runtime environments carry
   the enabled flag (FR-003 of the governing spec). The wiring verifier treats
   a missing marker as ADVISORY while capture is off and DEFECT once capture
   is enabled (check A9), so the marker must exist before the final clean
   `scripts/verify_wiring.py` pass.
4. Emit live events through the first 20 recorded events after the second
   (machine-local production) flag flip. Record the window boundary (timestamp
   of the first recorded event after that flip) and the 20th-event timestamp
   in the enablement note. Count failures per the governing rule (spec FR-004):
   quarantine entries with timestamps strictly inside the window count as
   failures, as do entries with missing, unparseable, or boundary-tied
   timestamps; entries strictly before the boundary are excluded. The pass
   condition is an in-window-failure count of exactly 0. If natural traffic is
   insufficient, use the recording command (§ Recording command) for the tail,
   and disclose the share of recorded tail events in the enablement note.
5. The phase-2 completion gate (both must exit 0, run AFTER the 20-event
   window closes and BEFORE any phase flip):
   - `python scripts/verify_wiring.py` — wiring policy gate (hook truth,
     roster caps, map parity, snapshot freshness, capture-state surfacing A8,
     adoption-marker A9).
   - `python scripts/verify_skillbox.py` — SkillBox subsystem self-check
     (note: requires the repo venv interpreter, which carries the `mlflow`
     dependency the push section needs).
   A nonzero result from either command is a fail-closed rollback trigger
   (spec FR-010): revert both runtime flags to `false`, revert the tracked
   production template, unset `wiring_adopted_at`, leave
   `phase_status.phase_2` pending, preserve the failing samples, and re-run
   the sequence from step 1 only after resolution.
   Then (operator, FR-007): set `phase_status.phase_2: complete` in the
   manifest and re-render via
   `python scripts/generate_lifecycle_map.py`;
   confirm `python scripts/generate_lifecycle_map.py --check` exits 0.
