# Contract: Wiring Verifier

**Script**: `scripts/verify_wiring.py` · **Exit contract**: `0` pass (labels allowed), `2` defect · **Check enumeration**: this document is the normative check list FR-001 references ("the verifier contract"); severities and requiredness are pinned HERE (in code), not in the editable manifest. The fault-injection suite (`tests/wiring/test_verify_wiring.py`) injects one fault per enumerated check item; DEFECT items exit 2, ADVISORY items exit 0 with the expected label.

## Checks (each listed exactly once)

### DEFECT (exit 2)

| # | Check | Detail |
|---|---|---|
| D1 | Hook config parse + user-gating | `extensions.yml` parses (loud failure on YAML error); every hook `optional: true`, `condition: null` |
| D2 | Auto-execution off | `settings.auto_execute_hooks == false` (absent key or `true` are both defects) |
| D3 | Command→skill mapping | every hook command (dot→hyphen) maps to an existing `egm-*` skill under `skills_root` (manifest-declared) |
| D4 | Map byte-identity | `docs/AI_SDLC_LIFECYCLE_MAP.md` byte-matches a fresh render (`generate_lifecycle_map.py --check`) |
| D5 | Manifest command-mirror equality | every manifest `stages.<event>.command` equals the corresponding hook command (map must never silently disagree) |
| D6 | Manifest parse + version | `wiring_manifest_version` ∈ {1}; required sub-keys present |
| D7 | Required-artifact presence | lifecycle map present; hook ledger well-formed **when capture enabled**; capture-protocol doc presence — DEFECT iff capture enabled (machine-observable); ADVISORY before enablement |
| D8 | Managed-surface integrity | speckit skill bodies + registered review-context skill unchanged vs registry content-hash baseline (between reindex passes) |
| D9 | Marker integrity | SKILLBOX begin/end present; recomputed fingerprint matches `skillbox-digest:` line; no wiring content inside markers |
| D10 | Ledger well-formedness | parseable ts; no future ts; event/command present in manifest (only when capture enabled) |
| D11 | Hook-decision ceiling | count of wired enabled user-facing hook events ≤ manifest `hook_decision_cap` (structural; never on ledger rows) |
| D12 | Graph snapshot integrity | snapshot missing/corrupt, or `schema_version` mismatch |
| D13 | Roster cap | per-event roster resolution over the manifest's `review_profiles` ≤ `fan_out_cap`, with `before_specify` exempt while `disabled: true` |

### ADVISORY (exit 0, labeled)

| # | Check | Label semantics |
|---|---|---|
| A1 | Digest absent/stale | snapshot stale: label names snapshot sha and HEAD sha |
| A2 | Review-tooling absence | on-disk skill/agent/persona missing under configured roots — environmental (install it) |
| A3 | Review-tooling pin stale | `upstream_pin` mismatch — label names both values |
| A4 | Silent-capture (gates) | capture enabled + zero ledger entries within `silent_capture_lookback_days` (14) — the hook-death signal |
| A5 | Silent-capture (rounds) | capture enabled + gates recorded + zero round summaries within the lookback window |
| A6 | Partial-loss reconciliation | ledger entries without corresponding recorded events within the lookback window (ledger-vs-store reconciliation in the verifier's ledger pass) |
| A7 | Manifest root unresolvable | configured root missing (portability) — label names the unresolvable path |
| A8 | Capture-state surfacing | inert (flag off) or enabled — surfaced as information with source file |
| A9 | Adoption marker absence | before enablement only (advisory-labeled; DEFECT-class once capture is enabled) |

### Surfaced (exit 0, always)

- Wiring-capture state: key `wiring_capture_enabled`, its value, and the configuration file it came from.
- Graph snapshot freshness: FRESH/STALE via `run_knowledge_graph.py` report (`commit_sha` vs `git rev-parse HEAD`) — advisory per A1 label when stale.

## Emission rule

One `wiring_health_check` event per run, **only when** capture is enabled AND the event type exists in the active vocabulary (telemetry `_EVENT_TYPES` membership — never importability). Disabled mode: no sessions, no events, no ledger writes (FR-016).

## Delegation rules (no re-implementation)

- kg: never parse `graph_snapshot.json`; use `run_knowledge_graph.py verify` + `report --format json`.
- SkillBox: use `python -m src.skillbox ...` status verbs; registry hashes as published; inject fingerprint RECOMPUTED (inject status parsed for presence only — it always exits 0).
- Failure of a delegated CLI: advisory label with the CLI's reason code (never masked); only the wiring's own contracts produce DEFECTs, except D12 (kg integrity, which mirrors the CLI's own exit-2 semantics).

## Output

Numbered sections; per-check PASS/FAIL/LABEL lines with reason codes; final summary (checks run, failed, labeled, emitted event). Stdout remains human-readable; `--format json` optional for tooling.
