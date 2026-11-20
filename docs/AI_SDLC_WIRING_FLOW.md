# AI-SDLC Wiring: Implementation Flow (Feature 005)

This diagram shows what was actually implemented and wired together in this session — the complete flow from the maintainer's feature idea through to the learning loop's data-gated Phase 3.

```mermaid
flowchart TD
    %% ═══════════════ MAINTAINER / SPECKIT SIDE ═══════════════
    subgraph SPECKIT["Speckit lifecycle (user-gated hooks)"]
        S1["/speckit-specify<br/>→ spec.md"]
        S2["/speckit-plan<br/>→ plan.md"]
        S3["/speckit-tasks<br/>→ tasks.md"]
        S4["/speckit-implement<br/>→ code changes"]
        S5["/speckit-converge"]
    end

    HOOK1["after_specify hook:<br/>egm.review.loop (SPEC profile)"]
    HOOK2["after_plan hook:<br/>egm.review (READINESS profile)"]
    HOOK3["after_tasks hook:<br/>egm.review.loop (TASKS profile)"]

    S1 --> HOOK1
    S2 --> HOOK2
    S3 --> HOOK3
    S4 --> S5

    %% ═══════════════ EGM SIDE ═══════════════
    subgraph EGM["EGM Goldfish layer (read-only, advisory)"]
        GF1["zoom_out"]
        GF2["comprehension"]
        GF3["design_critic"]
        GF4["codebase_details"]
        GF5["implementation_readiness"]
        GF6["structural"]
    end

    HOOK1 -.->|direct dispatch| GF1 & GF2 & GF3
    HOOK2 -.-> GF5 & GF4 & GF6
    HOOK3 -.-> GF4 & GF6

   %% ═══════════════ WIRING LAYER (what we built) ═══════════════
    subgraph WIRING["Wiring layer (feature 005 implementation)"]
        MAN[".specify/wiring-manifest.yml<br/>profiles · budgets · trigger · artifacts"]
        MAPGEN["scripts/generate_lifecycle_map.py<br/>byte-idempotent renderer"]
        MAP["docs/AI_SDLC_LIFECYCLE_MAP.md<br/>(generated, committed)"]

        VERIFY["scripts/verify_wiring.py<br/>D1–D13 defect · A1–A9 advisory<br/>exit 0 pass / 2 defect"]
        LEDGER[".skillbox/wiring/hook_ledger.json<br/>(ledger-of-record: gate decisions)"]
        REC["scripts/record_hook_acceptance.py<br/>ledger-first in-process write"]

        DIGESTMOD["src/kg/digest.py<br/>PageRank top-25 + co-edit pairs"]
        DIGESTCLI["run_knowledge_graph.py digest<br/>+ auto-render on build/update"]
        DIGEST["output/kg/digest_for_reviews.md<br/>(freshness-stamped, ≤ 8KB)"]

        SWEEP["src/skillbox/sweep.py<br/>content-hash dedup + retention"]
        DROP["logs/egm_rounds/<br/>(round summaries from EGM)"]

        TEL["src/skillbox/telemetry.py<br/>5 new event types<br/>+ required-key enforcement"]
        CFG["src/skillbox/config.py<br/>wiring_capture_enabled merge"]
        SCHED["scripts/skillbox_scheduler.py<br/>step 8: wiring sweep"]

        FLAG["config/environments/<br/>wiring_capture_enabled: false<br/>(default-off, operator-gated)"]
    end

%% ═══════════════ GRAPH SIDE ═══════════════
    subgraph KG["Code Knowledge Graph (src/kg, feature 003)"]
        SNAP["output/kg/graph_snapshot.json<br/>(nodes: files+symbols, edges: import/call/co-edit)"]
    end

%% ═══════════════ SKILLBOX SIDE ═══════════════
    subgraph SKILLBOX["SkillBox (feature 004)"]
        TSTORE["telemetry store<br/>(SQLite WAL + JSONL)"]
        LEARN["learning extraction"]
        OVERLAY["overlay staging"]
        REVIEW["HUMAN approval gate<br/>(review approve)"]
        INJECT["inject refresh<br/→ copilot-instructions.md"]
    end

%% ═══════════════ FLOWS ═══════════════
    MAN --> MAPGEN
    MAPGEN --> MAP
    MAN -.-> VERIFY
    MAP -.-> VERIFY

    %% Verifier gates every wiring phase
    VERIFY -->|"exit 0 = phase gate"| SPECKIT

    %% Digest: graph build/update auto-renders
    KG_SNAP_UPDATE["graph build/update<br/>(post-commit hook)"] --> SNAP
    SNAP --> DIGESTMOD
    DIGESTMOD --> DIGESTCLI
    DIGESTCLI --> DIGEST

    %% EGM reviewers read the digest
    DIGEST -.->|"consult mode: digest<br/>(reading list)"| GF1 & GF2 & GF3
    DIGEST -.->|fallback: ≤ 4096 B| GF4

    %% Recording: gate decisions
    HOOK1 -->|"gate accepted"| REC
    HOOK2 -->|"gate accepted"| REC
    HOOK3 -->|"gate accepted"| REC
    REC --> LEDGER
    REC -->|"hook_gate_accepted/skipped"| TEL
    REC -->|"stage completed (last gate)"| TEL

    %% Sweep: rounds → events
    DROP -->|"step 8 (flag on)"| SWEEP
    SWEEP -->|"egm_review_round"| TEL
    EGM -.->|"writes round summaries"| DROP

    %% Telemetry → learning loop
    TEL --> TSTORE
    TSTORE --> LEARN
    LEARN --> OVERLAY
    OVERLAY --> REVIEW
    REVIEW --> INJECT

    %% Phase 3: conditional expansion
    TALLIES["trigger tally:<br/>≥30 events AND ≥60% across 2+<br/>AND adoption confirmation"]
    TSTORE -.-> TALLIES
    TALLIES -->|"unmet: locked<br/>met: status screen"| PHASE3["Phase 3 status screen<br/>+ overlay eligibility<br/>(SKIP-UNLESS-TRIGGERED)"]

%% ═══════════════ SAFETY BOUNDARY ═══════════════
    STYLE_TRADING["Trading system<br/>(src/core, src/ml, EAs)<br/>UNTOUCHED"]

    style SPECKIT fill:#e8f4fd
    style EGM fill:#f5e8ff
    style WIRING fill:#fff3d6
    style KG fill:#e8f8e8
    style SKILLBOX fill:#fce8e8
    style STYLE_TRADING fill:#ffcccc,stroke:#cc0000,stroke-width:2px

    classDef dotted stroke-dasharray: 5 5
    class HOOK1,HOOK2,HOOK3 dotted
```

## What the diagram shows

**Structure** (the four systems):

- **Speckit** (blue): the spec-driven lifecycle with user-gated hooks. Every hook is optional; accepting a gate runs the recording command.
- **EGM** (purple): the read-only reviewer layer. Goldfish never execute commands, never parse internal graph files, and never feed learnings back into their own context.
- **Wiring layer** (yellow): what we built — manifest, map, verifier, ledger, recorder, digest, sweep, telemetry types, capture flag. All repo-owned, all fail-soft.
- **KG** (green): the graph + its new digest artifact, auto-rendered at every build/update.
- **SkillBox** (red/pink): the learning runtime the capture contract feeds.

**The four information flows**:

1. **Digest flow** (KG → EGM): graph build/update auto-renders `digest_for_reviews.md`; reviewers read it as a plain file (no execution). Stale → labeled, never fails.
2. **Recording flow** (Speckit hooks → SkillBox): gate decisions write ledger-first via the recording command; a scheduled sweep converts EGM round summaries into review-round events. Inert when the flag is off.
3. **Learning flow** (SkillBox → itself): events → learnings → pending overlays → HUMAN approval → inject refresh → copilot-instructions.md. The only loop that changes behavior.
4. **Gate flow** (verifier → speckit): verifier exit 0 gates every wiring phase; the landmine (`auto_execute_hooks`) is pinned off and machine-checked.

**Safety boundary (red)**: the trading system sits outside all of this — no wiring component touches it, no advisory gates it, and the verifier asserts the trading-path isolation.

**Phase 3 discipline**: the trigger tally (≥30 events, ≥60% acceptance across 2+ features) AND the recorded maintainer confirmation both gate Phase 3. If either is unmet, nothing below the dashed line is built.
