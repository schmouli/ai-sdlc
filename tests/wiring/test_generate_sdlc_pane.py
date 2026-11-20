"""Feature 007 T003: generate_sdlc_pane correctness suite (tests-first, red → green).

Per design § DIP step 4: temp-dir fixtures only (never real specs/, ledger, or KG
data), assertions for the glyph legend, dedupe rule property, the 14-predicate
integrity firing table, record-mode schema/dedupe/staleness/--force matrix,
render determinism (now=FIXED_TS), persisted-hash write-skip, empty state,
capture-disabled watermark (monkeypatched flag — no config edits),
family-stage mismatch, pre-convention classification, ticket-regex exclusions,
and the refusal exit semantics (refusals exit 0 with the reason on stderr; a
missing report file exits non-zero).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

FIXED_TS = datetime(2026, 11, 14, 12, 0, 0)  # ET display fixed for determinism


@pytest.fixture()
def panemod(tmp_path, monkeypatch):
    """Import the generator module with its roots redirected into a temp tree."""
    mod = importlib.import_module("generate_sdlc_pane")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "specs").mkdir()
    (root / "data").mkdir()
    (root / "dashboards").mkdir()
    monkeypatch.setattr(mod, "REPO_ROOT", root)
    monkeypatch.setattr(mod, "SPECS_DIR", root / "specs")
    monkeypatch.setattr(mod, "PANE_PATH", root / "dashboards" / "sdlc_dashboard.html")
    monkeypatch.setattr(mod, "SERIES_PATH", root / "data" / "kg_eval_series.jsonl", raising=False)
    monkeypatch.setattr(mod, "REPORT_PATH", root / "output" / "kg" / "evaluation_report.json", raising=False)
    # series_path()/report_path() are functions in the implementation — bind them to the sandbox
    monkeypatch.setattr(mod, "series_path", lambda: root / "data" / "kg_eval_series.jsonl")
    monkeypatch.setattr(mod, "report_path", lambda: root / "output" / "kg" / "evaluation_report.json")
    return {"mod": mod, "root": root}


# ---------------------------------------------------------------- fixtures

def _ticket(root: Path, name: str, artifacts: list[tuple[str, dict | None]]) -> Path:
    d = root / "specs" / name
    d.mkdir(parents=True, exist_ok=True)
    for fname, fm in artifacts:
        if fm is None:
            (d / fname).write_text("# raw markdown, no frontmatter\n", encoding="utf-8")
        else:
            fm_text = "---\n" + "\n".join(f"{k}: {json.dumps(v) if isinstance(v, (list, dict)) else v}"
                                          for k, v in fm.items()) + "\n---\n\n# body\n"
            (d / fname).write_text(fm_text, encoding="utf-8")
    return d


def _ledger(tmp_path, entries: list[dict], monkeypatch, pane_mod):
    f = tmp_path / "hook_ledger.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"schema_version": 1, "entries": entries}, indent=2), encoding="utf-8")
    monkeypatch.setattr(pane_mod, "ledger_path", lambda: f)


def _series(tmp_path, lines: list[dict], monkeypatch, pane_mod):
    f = tmp_path / "kg_eval_series.jsonl"
    f.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
    monkeypatch.setattr(pane_mod, "series_path", lambda: f)


def _report(tmp_path, payload: dict, monkeypatch, pane_mod) -> Path:
    f = tmp_path / "evaluation_report.json"
    f.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    monkeypatch.setattr(pane_mod, "report_path", lambda: f)
    return f


KG_REPORT_OK = {
    "suite_version": "1.0.0",
    "graph_commit_sha": "00e0673",
    "status": "complete",
    "aggregate": {
        "kg": {"easy": {"hit_rate": 0.70, "total": 10, "mean_cost": 2427},
               "hard": {"hit_rate": 0.27, "total": 10, "mean_cost": 3000}},
        "regex": {"easy": {"hit_rate": 0.66, "total": 10, "mean_cost": 214},
                  "hard": {"hit_rate": 0.73, "total": 10, "mean_cost": 300}},
        "text_search": {"easy": {"hit_rate": 0.60, "total": 10, "mean_cost": 122},
                        "hard": {"hit_rate": 0.20, "total": 10, "mean_cost": 150}},
    },
}

SERIES_LINE_OK = {
    "ts_utc": "2026-11-13T22:10:00Z",
    "recorded_head": "abc1234",
    "snapshot_commit": "00e0673",
    "suite_version": "1.0.0",
    "hit_rate": KG_REPORT_OK["aggregate"],
    "mean_cost": {"kg": 2427, "regex": 214, "text_search": 122},
    "report_content_hash": "deadbeef",
}

FM = lambda stage, fam, **kw: {"schema": "sdlc-v1", "stage": stage, "family": fam,
                               "attempt": 1, "written_at": "2026-11-13T21:40:00-05:00", **kw}


# ---------------------------------------------------------------- glyphs

def test_glyph_zero_distinct_from_na_and_emitdash(panemod):
    """US1-AC3 (`0`), US4-AC14 (`n/a`), FR-004 (`—`) — the three-value rule."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-a", [
        ("findings_round1.md", FM("after_specify", "findings", round=2, findings_count=0)),
        ("findings_round2.md", FM("after_specify", "findings", round=2, findings_count=None)),
    ])
    collected = mod.collect()
    html = mod.render_html(collected, now=FIXED_TS)
    assert ">0<" in html          # the clean count renders the NUMBER 0
    assert "n/a" in html          # null field renders the literal n/a
    assert "—" in html            # absent family renders the em-dash
    assert html.index(">0<") < html.index("n/a")  # distinct cells


# ---------------------------------------------------------------- dedupe property

def test_dedupe_property_n_keys_in_n_rows_out(panemod, tmp_path, monkeypatch):
    """US4-AC11: N distinct (ticket, stage, command, decision) keys in → N rows out."""
    mod = panemod["mod"]
    entries = []
    base = 1700000000.0
    # three distinct keys, each duplicated in a burst inside the 10 s window
    for key_i, (feat, stage, cmd, dec) in enumerate([
        ("specs/007-a", "after_specify", "egm.review.loop", "accepted"),
        ("specs/007-a", "after_tasks", "egm.review.loop", "accepted"),
        ("specs/007-b", "after_specify", "egm.review.loop", "accepted"),
    ]):
        for rep in range(3):
            entries.append({"ts": datetime.utcfromtimestamp(base + key_i + rep * 0.1)
                            .isoformat().replace("+00:00", "Z"),
                            "event": stage, "command": cmd, "decision": dec,
                            "feature_dir": feat})
    _ledger(tmp_path, entries, monkeypatch, mod)
    deduped = mod.dedupe_ledger(entries)
    assert len(deduped) == 3  # N distinct keys in → N rows out (no hardcoded counts)
    # earliest bucket timestamp retained
    times = [e["ts"] for e in deduped]
    assert times == sorted(times)


# ---------------------------------------------------------------- 14-predicate firing table

def test_banner_missing_artifact_convention_era(panemod, tmp_path, monkeypatch):
    """US4-AC1: accepted ledger row whose stage/ticket has no matching family artifact."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-a", [])  # dir exists, zero artifacts
    _ledger(tmp_path, [{"ts": "2026-11-13T21:00:00Z", "event": "after_specify",
                        "command": "egm.review.loop", "decision": "accepted",
                        "feature_dir": "specs/007-a"}], monkeypatch, mod)
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert any(b["name"] == "MISSING-ARTIFACT" and b.get("ticket") == "007-a"
               and b["stage"] == "after_specify" for b in banners)


def test_banner_missing_artifact_suppressed_for_skipped(panemod, tmp_path, monkeypatch):
    """US4-AC2: skipped rows render 'skipped — no expectation', suppressed banner."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-a", [])
    _ledger(tmp_path, [{"ts": "2026-11-13T21:00:00Z", "event": "after_specify",
                        "command": "egm.review.loop", "decision": "skipped",
                        "feature_dir": "specs/007-a"}], monkeypatch, mod)
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert not any(b["name"] == "MISSING-ARTIFACT" for b in banners)


def test_banner_orphan_artifact(panemod, tmp_path, monkeypatch):
    """US4-AC3: artifact with no accepted ledger row for (feature_dir, stage)."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-b", [("findings_round1.md", FM("after_specify", "findings", round=1,
                                                      findings_count=0))])
    _ledger(tmp_path, [], monkeypatch, mod)
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert any(b["name"] == "ORPHAN-ARTIFACT" and b.get("ticket") == "007-b" for b in banners)


def test_banner_null_count(panemod, tmp_path):
    """US4-AC14: parses-but-required-field-missing/null → NULL-COUNT, cell n/a."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-c", [("findings_round1.md",
                             {k: v for k, v in FM("after_specify", "findings", round=1,
                                                  findings_count=1).items()
                              if k != "findings_count"})])
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert any(b["name"] == "NULL-COUNT" for b in banners)


def test_banner_gap(panemod, tmp_path):
    """US4-AC5: numbering gap within (stage, attempt) — rounds 1 and 3, no 2."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-d", [
        ("findings_round1.md", FM("after_specify", "findings", round=1, findings_count=0)),
        ("findings_round3.md", FM("after_specify", "findings", round=3, findings_count=0)),
    ])
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert any(b["name"] == "GAP" for b in banners)


def test_banner_drift_first_run_missing(panemod, tmp_path):
    """US2-AC3: drift series present without run: 1 → banner + drift metric n/a."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-e", [("drift_report_run2.md",
                             FM("after_implement", "drift", run=2, findings_count=1))])
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert any(b["name"] == "DRIFT-FIRST-RUN-MISSING" for b in banners)
    ticket = next(t for t in mod.collect()["tickets"] if t["number"] == 7)
    assert ticket["drift"].startswith("n/a") or ticket["drift"] == "n/a"


def test_banner_missed_convention(panemod):
    """US4-AC6: convention-era ticket lacking convention artifacts."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-f", [])
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert any(b["name"] == "MISSED-CONVENTION" and b.get("ticket") == "007-f" for b in banners)


def test_banner_invalid_frontmatter_unparseable(panemod):
    """US4-AC7: absent/unparseable frontmatter → INVALID-FRONTMATTER, per file."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-g", [("findings_round1.md", None)])
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert any(b["name"] == "INVALID-FRONTMATTER" for b in banners)


def test_banner_invalid_frontmatter_wrong_typed(panemod):
    """US4-AC7: present-but-empty-valued/wrong-typed required field → INVALID-FRONTMATTER."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-h", [("findings_round1.md",
                             FM("after_specify", "findings", round=1, findings_count="three"))])
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert any(b["name"] == "INVALID-FRONTMATTER" for b in banners)
    # disjointness: NULL-COUNT must NOT fire for a wrong-typed field
    assert not any(b["name"] == "NULL-COUNT"
                   and b.get("path", "").endswith("findings_round1.md") for b in banners)


def test_banner_family_stage_mismatch(panemod):
    """US4-AC8: filename family contradicts frontmatter stage — listed, counts excluded."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-i", [("findings_round1.md", FM("after_plan", "findings", round=1,
                                                      findings_count=0))])
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert any(b["name"] == "FAMILY-STAGE-MISMATCH" for b in banners)


def test_banner_invalid_stage(panemod):
    """US4-AC9: frontmatter stage naming no wired stage."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-j", [("findings_round1.md", FM("after_nonsense", "findings", round=1,
                                                      findings_count=0))])
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert any(b["name"] == "INVALID-STAGE" for b in banners)


def test_banner_unknown_schema(panemod):
    """US4-AC18: newer schema → banner, known fields render, unknown-field no-data."""
    mod = panemod["mod"]; root = panemod["root"]
    fm = FM("after_specify", "findings", round=1, findings_count=0)
    fm["schema"] = "sdlc-v2"
    _ticket(root, "007-k", [("findings_round1.md", fm)])
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert any(b["name"] == "UNKNOWN-SCHEMA" and "sdlc-v2" in json.dumps(b) for b in banners)


def test_banner_capture_disabled_watermark(panemod, monkeypatch):
    """US4-AC4: watermark via monkeypatched capture flag (no config edits)."""
    mod = panemod["mod"]
    monkeypatch.setattr(mod, "capture_enabled", lambda: False)
    rb = mod.repo_banners(mod.collect())
    assert any(b["name"] == "CAPTURE-DISABLED" for b in rb)
    html = mod.render_html(mod.collect(), rb, now=FIXED_TS)
    assert "capture" in html.lower()


def test_banner_truncated_line(panemod, tmp_path, monkeypatch):
    """US3-AC3: truncated series last line → TRUNCATED-LINE, skipped, never fatal."""
    mod = panemod["mod"]
    f = tmp_path / "kg_eval_series.jsonl"
    f.write_text(json.dumps(SERIES_LINE_OK) + "\n" + '{"ts_utc": "2026-11-1', encoding="utf-8")
    monkeypatch.setattr(mod, "series_path", lambda: f)
    rb = mod.repo_banners(mod.collect())
    assert any(b["name"] == "TRUNCATED-LINE" for b in rb)


def test_banner_kg_series_lag(panemod, tmp_path, monkeypatch):
    """US3-AC5: evaluation report provenance matches no series line."""
    mod = panemod["mod"]
    _report(tmp_path, KG_REPORT_OK, monkeypatch, mod)
    _series(tmp_path, [{**SERIES_LINE_OK, "snapshot_commit": "different"}], monkeypatch, mod)
    rb = mod.repo_banners(mod.collect())
    assert any(b["name"] == "KG-SERIES-LAG" for b in rb)


def test_banner_tracked_output_warning(panemod, tmp_path, monkeypatch):
    """Spec Edge Cases: a pane output excluded by version control → TRACKED-OUTPUT-WARNING."""
    mod = panemod["mod"]

    def fake_check(target: str) -> bool:  # True = ignored
        return True

    monkeypatch.setattr(mod, "_is_git_ignored", fake_check)
    rb = mod.repo_banners(mod.collect())
    assert any(b["name"] == "TRACKED-OUTPUT-WARNING" for b in rb)


def test_preconvention_banner_free(panemod):
    """001–006 render pre-convention with no banners (spec FR-005 / counting rules)."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "005-ai-sdlc-wiring", [("findings_round1.md", None)])
    _ticket(root, "006-ai-sdlc-telemetry-wiring", [])
    banners = mod.evaluate_integrity(mod.collect(), mod.repo_banners(mod.collect()))
    assert not any(b["name"] == "MISSED-CONVENTION" for b in banners)
    html = mod.render_html(mod.collect(), banners, now=FIXED_TS)
    assert "005-ai-sdlc-wiring" in html and "pre-convention" in html.lower()


def test_ticket_regex_exclusions(panemod, tmp_path, monkeypatch):
    """FR-017: regex-excluded feature_dir renders in the exclusion listing."""
    mod = panemod["mod"]
    _ledger(tmp_path, [{"ts": "2026-11-13T21:00:00Z", "event": "after_specify",
                        "command": "egm.review.loop", "decision": "accepted",
                        "feature_dir": "specs/smoke-f5-test"}], monkeypatch, mod)
    c = mod.collect()
    assert any("smoke-f5-test" in str(x) for x in c["excluded"])  # visible, not dropped


def test_ghost_detection(panemod, tmp_path, monkeypatch):
    """Feature_dir matching the regex whose directory is absent → ghost listing."""
    mod = panemod["mod"]
    _ledger(tmp_path, [{"ts": "2026-11-13T21:00:00Z", "event": "after_specify",
                        "command": "egm.review.loop", "decision": "accepted",
                        "feature_dir": "specs/004-absent"}], monkeypatch, mod)
    c = mod.collect()
    assert any("specs/004-absent" in json.dumps(x) for x in c["ghosts"])


def test_command_mismatch_listing(panemod, tmp_path, monkeypatch):
    """Ledger entries whose command disagrees with extensions.yml hook truth."""
    mod = panemod["mod"]
    _ledger(tmp_path, [{"ts": "2026-11-13T21:00:00Z", "event": "after_specify",
                        "command": "egm.not-a-real-command", "decision": "accepted",
                        "feature_dir": "specs/007-a"}], monkeypatch, mod)
    c = mod.collect()
    assert c["command_mismatch"], "mismatched commands must appear in the listing"


def test_attempt_suffix_re_review(panemod):
    """attempt-suffixed re-review is a distinct (round, attempt) pair."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-l", [
        ("findings_round1.md", FM("after_specify", "findings", round=1, findings_count=2)),
        ("findings_round1_attempt2.md", FM("after_specify", "findings", round=1, attempt=2,
                                           findings_count=1)),
    ])
    ticket = next(t for t in mod.collect()["tickets"] if t["number"] == 7)
    assert ticket["spec_rounds"].startswith("2")


def test_combined_file_round(panemod):
    """Combined-file converge form: resolution carries BOTH findings_count and resolved_count."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-m", [("resolution_round4.md",
                             FM("after_converge", "resolution", round=4,
                                findings_count=3, resolved_count=3))])
    ticket = next(t for t in mod.collect()["tickets"] if t["number"] == 7)
    # converge folds into the tasks columns; round counted frontmatter-authoritatively
    assert ticket["tasks_rounds"].startswith("1")


def test_run_gap_series(panemod):
    """drift run-suffixed series: run1 metric + run2 snapshot; run1 = metric."""
    mod = panemod["mod"]; root = panemod["root"]
    _ticket(root, "007-n", [
        ("drift_report_run1.md", FM("after_implement", "drift", run=1, findings_count=2)),
        ("drift_report_run2.md", FM("after_implement", "drift", run=2, findings_count=1)),
    ])
    ticket = next(t for t in mod.collect()["tickets"] if t["number"] == 7)
    assert "2" in ticket["drift"]  # first-run metric value, not run2's


# ---------------------------------------------------------------- render/behavior

def test_render_determinism_fixed_ts(panemod):
    """Deterministic tests: identical inputs + now=FIXED_TS → identical HTML bytes."""
    mod = panemod["mod"]
    html1 = mod.render_html(mod.collect(), mod.repo_banners(mod.collect()), now=FIXED_TS)
    html2 = mod.render_html(mod.collect(), mod.repo_banners(mod.collect()), now=FIXED_TS)
    assert html1 == html2


def test_persisted_hash_write_skip(panemod):
    """SC-004: unchanged collected content → second render writes nothing."""
    mod = panemod["mod"]; pane = panemod["root"] / "dashboards" / "sdlc_dashboard.html"
    mod.run_render(pane, now=FIXED_TS)
    first = pane.read_bytes()
    assert first  # first render writes (absent pane ⇒ write)
    mtime1 = pane.stat().st_mtime_ns
    mod.run_render(pane, now=FIXED_TS)
    assert pane.read_bytes() == first          # same bytes
    assert pane.stat().st_mtime_ns == mtime1   # no rewrite


def test_write_happens_on_input_change(panemod):
    """Touch an input → pane updates with exactly that change."""
    mod = panemod["mod"]; root = panemod["root"]; pane = panemod["root"] / "dashboards" / "sdlc_dashboard.html"
    mod.run_render(pane, now=FIXED_TS)
    before = pane.read_bytes()
    _ticket(root, "007-z", [("findings_round1.md", FM("after_specify", "findings", round=1,
                                                      findings_count=0))])
    mod.run_render(pane, now=FIXED_TS)
    assert pane.read_bytes() != before
    assert "007-z" in pane.read_text(encoding="utf-8")


def test_empty_state_visible_panels(panemod):
    """SC-001/US1-AC4: no inputs → exits 0, visible empty panels, never blank."""
    mod = panemod["mod"]
    html = mod.render_html(mod.collect(), mod.repo_banners(mod.collect()), now=FIXED_TS)
    assert html and html.lstrip().startswith("<!DOCTYPE") or html.lstrip().startswith("<html")
    assert "Consolidated" in html or "consolidated" in html.lower()
    assert "n/a" in html or "—" in html  # honest empty glyphs, not blanks


# ---------------------------------------------------------------- --record-kg-run (T005 coverage)

def test_record_line_schema_and_success(panemod, tmp_path, monkeypatch, capsys):
    """Contract 2 producer obligations: one line, field map stamped, atomic append."""
    mod = panemod["mod"]
    _report(tmp_path, KG_REPORT_OK, monkeypatch, mod)
    _series(tmp_path, [], monkeypatch, mod)
    monkeypatch.setattr(mod, "_git_head", lambda: "abc1234")
    rc = mod.record_kg_run()
    assert rc == 0
    lines = mod.series_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["snapshot_commit"] == "00e0673"            # graph_commit_sha verbatim
    assert rec["recorded_head"] == "abc1234"              # record-time HEAD
    assert rec["suite_version"] == "1.0.0"
    assert rec["hit_rate"]["kg"]["easy"]["hit_rate"] == 0.70  # verbatim, no rollup
    assert "report_content_hash" in rec and rec["ts_utc"]


def test_record_dedupe_skip_if_match(panemod, tmp_path, monkeypatch):
    mod = panemod["mod"]
    _report(tmp_path, KG_REPORT_OK, monkeypatch, mod)
    _series(tmp_path, [], monkeypatch, mod)
    monkeypatch.setattr(mod, "_git_head", lambda: "abc1234")
    assert mod.record_kg_run() == 0
    first = mod.series_path().read_text(encoding="utf-8")
    assert mod.record_kg_run() == 0  # refusal exits 0 with reason on stderr
    assert mod.series_path().read_text(encoding="utf-8") == first  # nothing appended


def test_record_force_overrides_dedupe(panemod, tmp_path, monkeypatch):
    mod = panemod["mod"]
    _report(tmp_path, KG_REPORT_OK, monkeypatch, mod)
    _series(tmp_path, [], monkeypatch, mod)
    monkeypatch.setattr(mod, "_git_head", lambda: "abc1234")
    assert mod.record_kg_run() == 0
    assert mod.record_kg_run(force=True) == 0
    assert len(mod.series_path().read_text(encoding="utf-8").strip().splitlines()) == 2


# ---------------------------------------------------------------- feature-008: shared helper (Contract 3)

def test_append_series_record_skip_on_match(panemod, tmp_path):
    """KG-mode contract via the shared helper: duplicate identity skips, nothing appended."""
    mod = panemod["mod"]
    sp = tmp_path / "s.jsonl"
    rec = {"k": "x", "v": 1, "ts_utc": "2026-11-14T10:00:00Z"}
    st, _ = mod.append_series_record(sp, tmp_path / "s.lock", rec, ("k",), ("x",),
                                     mode="skip-on-match")
    assert st == "RECORDED"
    st, _ = mod.append_series_record(sp, tmp_path / "s.lock", rec, ("k",), ("x",),
                                     mode="skip-on-match")
    assert st == "SKIPPED"
    assert len(sp.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_append_series_record_keep_last_supersedes_recent(panemod, tmp_path):
    """Keep-last: matching most-recent record's ts replaced; single-record touch."""
    mod = panemod["mod"]
    sp = tmp_path / "s.jsonl"
    older = {"k": "x", "ts_utc": "2026-11-14T09:00:00Z"}
    st, _ = mod.append_series_record(sp, tmp_path / "s.lock", older, ("k",), ("x",),
                                     mode="keep-last")
    assert st == "RECORDED"
    newer = {"k": "x", "ts_utc": "2026-11-14T12:00:00Z"}
    st, _ = mod.append_series_record(sp, tmp_path / "s.lock", newer, ("k",), ("x",),
                                     mode="keep-last")
    assert st == "RECORDED"
    lines = [json.loads(l) for l in sp.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 1 and lines[0]["ts_utc"] == newer["ts_utc"]  # superseded, not appended


def test_append_series_record_keep_last_refuses_nonrecent_match(panemod, tmp_path):
    """Keep-last: identity on a NON-recent record → skipped (earlier records immutable)."""
    mod = panemod["mod"]
    sp = tmp_path / "s.jsonl"
    mod.append_series_record(sp, tmp_path / "s.lock", {"k": "x", "ts_utc": "2026-11-14T09:00:00Z"},
                             ("k",), ("x",), mode="keep-last")
    mod.append_series_record(sp, tmp_path / "s.lock", {"k": "y", "ts_utc": "2026-11-14T10:00:00Z"},
                             ("k",), ("y",), mode="keep-last")
    st, msg = mod.append_series_record(sp, tmp_path / "s.lock",
                                       {"k": "x", "ts_utc": "2026-11-14T12:00:00Z"},
                                       ("k",), ("x",), mode="keep-last")
    assert st == "SKIPPED" and "immutable" in msg


def test_append_series_record_staleness_noop_when_source_none(panemod, tmp_path):
    """Budget-path staleness is a deliberate no-op (differs from KG's mtime rule)."""
    mod = panemod["mod"]
    sp = tmp_path / "s.jsonl"
    mod.append_series_record(sp, tmp_path / "s.lock", {"k": "x", "ts_utc": "2026-11-14T09:00:00Z"},
                             ("k",), ("x",), mode="keep-last")
    st, _ = mod.append_series_record(sp, tmp_path / "s.lock",
                                     {"k": "x", "ts_utc": "2026-11-14T08:00:00Z"},
                                     ("k",), ("x",), mode="keep-last", staleness_source=None)
    # earlier timestamp is fine for keep-last identity match (no staleness apply)
    assert st == "RECORDED"


def test_budget_series_record_and_size_columns(panemod, tmp_path, monkeypatch):
    """US1: byte columns, cumulative, standing panel, series record via run_render."""
    mod = panemod["mod"]; root = panemod["root"]
    standing = root / ".github" / "copilot-instructions.md"
    standing.parent.mkdir(parents=True, exist_ok=True)
    standing.write_bytes(b"x" * 1000)
    monkeypatch.setattr(mod, "STANDING_PATH", standing)
    monkeypatch.setattr(mod, "MANIFEST_PATH", root / ".specify" / "wiring-manifest.yml")
    bseries = root / "data" / "context_budget_series.jsonl"
    monkeypatch.setattr(mod, "budget_series_path", lambda: bseries)
    monkeypatch.setattr(mod, "budget_series_lock_path",
                        lambda: bseries.parent / (bseries.name + ".lock"))
    _ticket(root, "007-a", [("findings_round1.md", FM("after_specify", "findings", round=1,
                                                      findings_count=0))])
    pane = root / "dashboards" / "sdlc_dashboard.html"
    mod.run_render(pane, now=FIXED_TS)
    html = pane.read_text(encoding="utf-8")
    assert "Context size" in html or "context size" in html.lower()
    assert "1,000" in html          # standing bytes rendered
    assert "token proxy" in html    # labeled proxy
    assert "Ticket bytes" in html and "Artifact bytes" in html  # new columns
    lines = bseries.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["standing_bytes"] == 1000
    assert rec["artifact_total_bytes"] == pane.stat().st_size * 0 + rec["artifact_total_bytes"] > 0
    assert rec["git_head"] == "unknown"  # sandbox: git absent → unknown, still records


def test_budget_series_keep_last_second_render(panemod, tmp_path, monkeypatch):
    """SC-007: repeated render with unchanged state → no duplicate line; ts replaced."""
    mod = panemod["mod"]; root = panemod["root"]
    standing = root / ".github" / "copilot-instructions.md"
    standing.parent.mkdir(parents=True, exist_ok=True)
    standing.write_bytes(b"x" * 500)
    monkeypatch.setattr(mod, "STANDING_PATH", standing)
    monkeypatch.setattr(mod, "MANIFEST_PATH", root / ".specify" / "wiring-manifest.yml")
    bseries = root / "data" / "context_budget_series.jsonl"
    monkeypatch.setattr(mod, "budget_series_path", lambda: bseries)
    monkeypatch.setattr(mod, "budget_series_lock_path",
                        lambda: bseries.parent / (bseries.name + ".lock"))
    _ticket(root, "007-a", [])
    pane = root / "dashboards" / "sdlc_dashboard.html"
    t1 = datetime(2026, 11, 14, 12, 0, 0, tzinfo=__import__("datetime").timezone.utc)
    t2 = datetime(2026, 11, 14, 13, 0, 0, tzinfo=__import__("datetime").timezone.utc)
    mod.run_render(pane, t1)
    first = bseries.read_text(encoding="utf-8")
    mod.run_render(pane, t2)
    second = bseries.read_text(encoding="utf-8")
    assert len(first.strip().splitlines()) == 1
    assert len(second.strip().splitlines()) == 1
    assert json.loads(second)["ts_utc"] == "2026-11-14T13:00:00Z"  # replaced, not appended


def test_banner_budget_breach_most_recent_only(panemod, tmp_path, monkeypatch):
    """US2: breach banner on most recent round only; historical rounds quiet."""
    mod = panemod["mod"]; root = panemod["root"]
    standing = root / ".github" / "copilot-instructions.md"
    standing.parent.mkdir(parents=True, exist_ok=True)
    standing.write_bytes(b"x" * 10)
    monkeypatch.setattr(mod, "STANDING_PATH", standing)
    man = root / ".specify" / "wiring-manifest.yml"
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(yaml.safe_dump({
        "token_budgets": {"standing_instructions_max_bytes": 32500,
                          "enforce_standing_instructions": False,
                          "artifact_budgets": {"findings_max_bytes": 50}},
    }, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(mod, "MANIFEST_PATH", man)
    big = "y" * 60
    # big content WITH valid frontmatter (size counts the whole file)
    _ticket(root, "007-a", [])
    d1 = root / "specs" / "007-a" / "findings_round1.md"
    d1.write_text("---\n" + "\n".join(f"{k}: {v}" for k, v in FM("after_specify", "findings",
                          round=1, findings_count=1).items()) + "\n---\n\n" + big, encoding="utf-8")
    d2 = root / "specs" / "007-a" / "findings_round2.md"
    d2.write_text("---\n" + "\n".join(f"{k}: {v}" for k, v in FM("after_specify", "findings",
                          round=2, findings_count=1).items()) + "\n---\n\n" + big, encoding="utf-8")
    ctx = mod.context_size(mod.collect()["tickets"])
    names = [(b["artifact"]) for b in ctx["breaches"]]
    assert names == ["findings_round2.md"]  # only the most recent round
    html = mod.render_html(mod.collect(), [], now=FIXED_TS)
    assert "BUDGET-BREACH" in html


def test_banner_budget_missing_standing(panemod, tmp_path, monkeypatch):
    """Edge: missing standing file → BUDGET-MISSING-STANDING banner, not a zero."""
    mod = panemod["mod"]; root = panemod["root"]
    monkeypatch.setattr(mod, "STANDING_PATH", root / ".github" / "absent.md")
    monkeypatch.setattr(mod, "MANIFEST_PATH", root / ".specify" / "wiring-manifest.yml")
    ctx = mod.context_size([])
    rb = mod.budget_repo_banners(ctx)
    assert any(b["name"] == "BUDGET-MISSING-STANDING" for b in rb)
    assert ctx["standing_bytes"] is None


def test_banner_series_duplicate(panemod, tmp_path, monkeypatch):
    """Edge: two retained lines sharing identity → SERIES-DUPLICATE banner."""
    mod = panemod["mod"]; root = panemod["root"]
    bseries = root / "data" / "context_budget_series.jsonl"
    bseries.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts_utc": "2026-11-14T10:00:00Z", "git_head": "h", "standing_bytes": 10,
            "artifact_total_bytes": 20, "breaches": []}
    bseries.write_text(json.dumps(line) + "\n" + json.dumps(line) + "\n", encoding="utf-8")
    dup = mod._series_duplicates()
    assert dup == 1
    rb = mod.budget_repo_banners(mod.context_size([]))
    assert any(b["name"] == "SERIES-DUPLICATE" for b in rb)


def test_render_never_leaks_standing_content(panemod, tmp_path, monkeypatch):
    """The pane renders the standing file's SIZE, never its content (marker-region safety)."""
    mod = panemod["mod"]; root = panemod["root"]
    standing = root / ".github" / "copilot-instructions.md"
    standing.parent.mkdir(parents=True, exist_ok=True)
    standing.write_text("SKILLBOX:BEGIN\nsecret-marker-content\nSKILLBOX:END", encoding="utf-8")
    monkeypatch.setattr(mod, "STANDING_PATH", standing)
    monkeypatch.setattr(mod, "MANIFEST_PATH", root / ".specify" / "wiring-manifest.yml")
    html = mod.render_html(mod.collect(), [], now=FIXED_TS)
    assert "secret-marker-content" not in html
    assert "SKILLBOX:BEGIN" not in html


def test_record_staleness_refuse(panemod, tmp_path, monkeypatch, capsys):
    """Report mtime older than the previous line's record timestamp → refuse."""
    mod = panemod["mod"]
    rep = _report(tmp_path, KG_REPORT_OK, monkeypatch, mod)
    old = (datetime(2026, 11, 1).timestamp())
    os.utime(rep, (old, old))
    _series(tmp_path, [dict(SERIES_LINE_OK, ts_utc="2026-11-13T22:10:00Z")], monkeypatch, mod)
    monkeypatch.setattr(mod, "_git_head", lambda: "abc1234")
    rc = mod.record_kg_run()
    err = capsys.readouterr().err
    assert rc == 0  # refusal exits 0
    assert "stale" in err.lower()


def test_record_invalid_payload_refuse(panemod, tmp_path, monkeypatch):
    """Partial/error payload refuses, never appends (Contract 2)."""
    mod = panemod["mod"]
    bad = {"status": "error", "error": "boom"}  # missing aggregate/graph_commit_sha
    _report(tmp_path, bad, monkeypatch, mod)
    _series(tmp_path, [], monkeypatch, mod)
    monkeypatch.setattr(mod, "_git_head", lambda: "abc1234")
    n_before = len(mod.series_path().read_text(encoding="utf-8").strip().splitlines()) \
        if mod.series_path().exists() else 0
    assert mod.record_kg_run() == 0
    n_after = len(mod.series_path().read_text(encoding="utf-8").strip().splitlines()) \
        if mod.series_path().exists() else 0
    assert n_after == n_before  # series unchanged


def test_record_missing_report_exits_nonzero(panemod, tmp_path, monkeypatch):
    """A missing report file exits non-zero (the pinned exit semantics)."""
    mod = panemod["mod"]
    monkeypatch.setattr(mod, "report_path",
                        lambda: tmp_path / "nope" / "evaluation_report.json")
    _series(tmp_path, [], monkeypatch, mod)
    assert mod.record_kg_run() != 0  # non-zero: unreadable/absent input


def test_series_staleness_passes_vacuously_on_empty(panemod, tmp_path, monkeypatch):
    """First record on an empty series passes staleness vacuously."""
    mod = panemod["mod"]
    rep = _report(tmp_path, KG_REPORT_OK, monkeypatch, mod)
    old = (datetime(2020, 1, 1).timestamp())
    os.utime(rep, (old, old))
    _series(tmp_path, [], monkeypatch, mod)
    monkeypatch.setattr(mod, "_git_head", lambda: "abc1234")
    assert mod.record_kg_run() == 0
    assert len(mod.series_path().read_text(encoding="utf-8").strip().splitlines()) == 1


# ---------------------------------------------------------------- KG benchmark table render

def test_kg_table_segments_on_suite_version(panemod, tmp_path, monkeypatch):
    """Consumer guarantee: read the series as one segment per suite version."""
    mod = panemod["mod"]
    l1 = dict(SERIES_LINE_OK, ts_utc="2026-11-12T22:10:00Z", suite_version="1.0.0")
    l2 = dict(SERIES_LINE_OK, ts_utc="2026-11-13T22:10:00Z", suite_version="2.0.0")
    _series(tmp_path, [l1, l2], monkeypatch, mod)
    html = mod.render_html(mod.collect(), [], now=FIXED_TS)
    assert "1.0.0" in html and "2.0.0" in html
    mod.assert_segments([l1, l2], segments={"1.0.0": [l1], "2.0.0": [l2]})
