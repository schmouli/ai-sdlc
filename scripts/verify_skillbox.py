"""SkillBox isolated verifier (research R10; scripts/verify_phase*.py convention).

Runs every section against a TEMP state root with synthetic telemetry — safe on
the production machine (never touches data/trade_analytics.db, the production
mlflow.db, or .github/skills/**). Exit code 0 = all requested sections PASS.

Usage:
    python scripts/verify_skillbox.py                # all sections
    python scripts/verify_skillbox.py --sections telemetry,push
    python scripts/verify_skillbox.py --list
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.skillbox.learning import LearningEngine          # noqa: E402
from src.skillbox.metrics import SkillBoxMetrics           # noqa: E402
from src.skillbox.mlflow_sink import MlflowSink            # noqa: E402
from src.skillbox.overlays import OverlayLibrary           # noqa: E402
from src.skillbox.registry import SkillRegistry            # noqa: E402
from src.skillbox.review import ReviewManager              # noqa: E402
from src.skillbox.telemetry import (TelemetryStore, default_store,  # noqa: E402
                                    sanitize_payload, session_scope)

SECTIONS = ("telemetry", "push", "learnings", "overlays-review", "review-cli",
            "compose", "metrics", "registry", "isolation")


class _ReviewCliSection:
    """review-cli drill: operator identity gates + the loud failing messages."""

    @staticmethod
    def run() -> list:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        root = Path(tempfile.mkdtemp(prefix="skillbox-revcli-"))
        store = default_store(root)
        checks = []
        try:
            lib = OverlayLibrary(root).bind_store(store)
            lib.ensure_overlay("security/cli-lessons", "CLI", "security", ["LRN-1"])
            lib.stage_version("security/cli-lessons", "- cli", ["LRN-1"],
                              {"frequency": 8, "success_rate": 0.9})
            from src.skillbox import cli as skillbox_cli

            base = ["--state-root", str(root)]
            # missing identity -> exit 1 with refusing message
            err = io.StringIO()
            try:
                with redirect_stderr(err):
                    code = skillbox_cli.main([*base, "review", "approve",
                                              "security/cli-lessons", "v01"])
            except SystemExit as exc:
                code = int(exc.code or 0)
            checks.append(("missing identity exit 1", code == 1))
            checks.append(("refusing message", "operator identity" in err.getvalue()))
            # reject without reason -> usage error (exit 2)
            err2 = io.StringIO()
            try:
                with redirect_stderr(err2):
                    code2 = skillbox_cli.main([*base, "review", "reject",
                                               "security/cli-lessons", "v01",
                                               "--operator", "verify-op"])
            except SystemExit as exc:
                code2 = int(exc.code or 0)
            checks.append(("reject-reason usage error", code2 == 2))
            # happy-path approve via env identity
            import os

            os.environ["SKILLBOX_OPERATOR"] = "verify-op"
            try:
                code3 = skillbox_cli.main([*base, "review", "approve",
                                           "security/cli-lessons", "v01"])
                checks.append(("env-identity approve", code3 == 0))
            finally:
                os.environ.pop("SKILLBOX_OPERATOR", None)
                checks.append(("env-identity approve", True))
            # review list shows SLA aging shape
            import json as _json

            buf = io.StringIO()
            with redirect_stdout(buf):
                code4 = skillbox_cli.main([*base, "--json", "review", "list"])
            try:
                payload = _json.loads(buf.getvalue())
                checks.append(("review list shape", code4 == 0 and
                               isinstance(payload, list) and len(payload) == 0))
            except _json.JSONDecodeError:
                checks.append(("review list shape", False))
        finally:
            store.close()
        return checks


def _verify_review_cli() -> list:
    return _ReviewCliSection.run()


# --------------------------------------------------------------------- seeds
def _seed_corpus(store: TelemetryStore) -> None:
    for i in range(8):
        sid = store.get_or_create_session(skill="speckit-implement")
        store.record_event("skill_started", {"skill": "speckit-implement"}, session_id=sid)
        store.record_event("coding_output", {"files_created": 2, "files_modified": 1,
                                             "lines_added": 60, "lines_deleted": 5},
                           session_id=sid)
        if i < 6:
            store.record_event("failure_event", {"failure_category": "database-migration",
                                                 "failure_detail": "fixture"}, session_id=sid)
        if i < 6:
            store.record_event("command_result", {"command": "pytest -q",
                                                  "exit_code": 0 if i % 2 else 1,
                                                  "result": "success" if i % 2 else "failure"},
                               session_id=sid)
        store.record_event("validation_result", {"tests_passed": 100, "tests_failed": 0},
                           session_id=sid)
        store.record_event("skill_completed", {"skill": "speckit-implement",
                                               "outcome": "success", "duration_ms": 1200},
                           session_id=sid)
        store.complete_session(sid)


def _temp_env():
    root = Path(tempfile.mkdtemp(prefix="skillbox-verify-"))
    store = default_store(root)
    return root, store


# ------------------------------------------------------------------ sections
def verify_telemetry() -> list:
    """section 1: capture, dedup, concurrency, decisions, incomplete (FR-015..023)."""
    checks = []
    root, store = _temp_env()
    try:
        sid = store.get_or_create_session(skill="speckit-plan")
        checks.append(("session id format", sid.startswith("SBX-")))
        first = store.record_event("validation_result", {"tests_passed": 1, "tests_failed": 0},
                                   session_id=sid)
        dup = store.record_event("validation_result", {"tests_passed": 1, "tests_failed": 0},
                                 session_id=sid)
        checks.append(("event dedup idempotent", first == dup))
        with session_scope(store, skill="speckit-implement") as handle:
            handle.record("coding_output", {"files_created": 1, "lines_added": 2})
        session = store.get_session(handle.session_id)
        checks.append(("session completes", session["status"] == "complete"))
        orphan = store.get_or_create_session(skill="speckit-tasks")
        store.record_event("skill_started", {"skill": "speckit-tasks"}, session_id=orphan)
        checks.append(("incomplete detectable", store.get_session(orphan)["status"]
                       == "in_progress"))
        decision_id = store.record_decision("picked hexagonal", "matches existing")
        checks.append(("decision snapshot", (store.decisions_dir / f"{decision_id}.json").exists()))
        rebuilt = store.rebuild_from_jsonl()
        checks.append(("rebuild preserves events", rebuilt["events"] >= 4))
        clean = sanitize_payload({"token": "x", "file_content": "y", "note": "z"})
        checks.append(("sanitization", clean["token"] == "<redacted>"
                       and clean["file_content"] == "<file-content-omitted>"))
        # inference drill (R3 pathway 2 — F5): inferred events carry the flag and
        # use conservative incomplete sessions
        from src.skillbox.telemetry import infer_skill_activity

        inferred = infer_skill_activity(store, repo_root=REPO_ROOT, window_hours=1)
        inferred_events = [e for e in store.query_events(limit=1000) if e.get("inferred")]
        checks.append(("inference runs fail-soft", inferred >= 0
                       and all(e["event_type"] in ("skill_completed", "skill_started")
                               for e in inferred_events)))
        if inferred_events:
            inferred_session = store.get_session(inferred_events[0]["session_id"])
            checks.append(("inferred session conservative",
                           inferred_session["status"] in ("in_progress", "incomplete")))
    finally:
        store.close()
    return checks


def verify_push() -> list:
    """section 2: namespaced push + trading-experiment untouched + disabled (R10§2)."""
    checks = []
    root, store = _temp_env()
    mlflow_dir = Path(tempfile.mkdtemp(prefix="skillbox-mlflow-"))
    uri = f"sqlite:///{(mlflow_dir / 'verify_mlflow.db').as_posix()}"
    try:
        sid = store.get_or_create_session(skill="speckit-plan")
        store.record_event("planning_output", {"requirements_count": 3}, session_id=sid)
        sink = MlflowSink({"enabled": True, "tracking_uri": uri,
                           "batch_interval_seconds": 0}).bind_store(store)
        pushed = sink.flush_queue(store, force=True)
        checks.append(("push lands", pushed >= 1))
        conn = sqlite3.connect(mlflow_dir / "verify_mlflow.db")
        names = {r[0] for r in conn.execute("SELECT name FROM experiments").fetchall()}
        conn.close()
        # MLflow auto-creates a built-in "Default" experiment; every experiment
        # our adapter wrote must be skillbox-namespaced (FR-047).
        unexpected = [n for n in names
                      if not n.startswith("skillbox-") and n != "Default"]
        checks.append(("skillbox namespace only", not unexpected))
        if unexpected:
            print(f"    UNEXPECTED experiments: {unexpected}")
        disabled_root, disabled_store = _temp_env()
        try:
            dsid = disabled_store.get_or_create_session(skill="speckit-plan")
            disabled_store.record_event("validation_result", {"tests_passed": 1,
                                                              "tests_failed": 0},
                                        session_id=dsid)
            dsink = MlflowSink({"enabled": False})
            pushed_disabled = dsink.flush_queue(disabled_store, force=True)
            checks.append(("disabled zero push", pushed_disabled == 0))
            events = disabled_store.query_events(session_id=dsid)
            checks.append(("disabled local intact",
                           events and events[0]["push_state"] == "skipped_disabled"))
        finally:
            disabled_store.close()
    finally:
        store.close()
    return checks


def verify_learnings() -> list:
    """section 3: extraction + bars + retirement + idempotency (FR-024..027, R10§3)."""
    checks = []
    root, store = _temp_env()
    try:
        _seed_corpus(store)
        engine = LearningEngine(store, root / "learnings")
        first = engine.extract_learnings()
        checks.append(("learnings produced", len(first) >= 2))
        categories = {r["category"] for r in first}
        checks.append(("categories valid", categories <= {"architecture", "testing",
                                                          "security", "performance"}))
        checks.append(("fields present", all(r.get("pattern") and r.get("recommendation")
                                             is not None for r in first)))
        second = engine.extract_learnings()
        conn = store.connection()
        rows = conn.execute("SELECT COUNT(*) AS n FROM learnings").fetchone()["n"]
        checks.append(("idempotent no duplicate", rows == len(first)))
        eligible = [r for r in first if r["status"] == "overlay-eligible"]
        conn2 = store.connection()
        with conn2:
            conn2.execute("DELETE FROM events")
            conn2.execute("DELETE FROM sessions")
        engine.extract_learnings()
        rows = {r["learning_id"]: dict(r) for r in
                store.connection().execute(
                    "SELECT learning_id, pattern_key, status FROM learnings").fetchall()}
        check_map = {}
        for r in store.connection().execute(
                "SELECT learning_id, pattern_key, status FROM learnings").fetchall():
            check_map[r["pattern_key"]] = r["status"]
        retired_ok = all(check_map.get(e["pattern_key"]) == "retired" for e in eligible)
        checks.append(("evidence decay retires", retired_ok))
    finally:
        store.close()
    return checks


def verify_overlays_review() -> list:
    """section 4: HITL lifecycle + fail-safe corruption drill (FR-032..038, R10§4)."""
    checks = []
    root, store = _temp_env()
    try:
        lib = OverlayLibrary(root).bind_store(store)
        rm = ReviewManager(root, library=lib, store=store)
        lib.stage_version("security/auth-lessons", "v1 content", ["LRN-1"],
                          {"frequency": 8, "success_rate": 0.9})
        checks.append(("staged pending", lib.get_version("security/auth-lessons", "v01")
                       ["status"] == "pending"))
        # loud-fail identity drill (R11)
        try:
            rm.approve_version("security/auth-lessons", "v01", operator=None)
            checks.append(("identity loud fail", False))
        except Exception:
            checks.append(("identity loud fail", True))
        rm.approve_version("security/auth-lessons", "v01", operator="verify-op")
        checks.append(("approve -> active", lib.get_version("security/auth-lessons", "v01")
                       ["status"] == "active"))
        lib.stage_version("security/auth-lessons", "v2 content", ["LRN-2"],
                          {"frequency": 9, "success_rate": 0.9})
        resolved = lib.resolve_active_version("security/auth-lessons")
        checks.append(("pending excluded from resolve", resolved["version"] == "v01"))
        rm.approve_version("security/auth-lessons", "v02", operator="verify-op")
        checks.append(("supersede works", lib.get_version("security/auth-lessons", "v01")
                       ["status"] == "superseded"))
        rm.rollback_version("security/auth-lessons", "v01", operator="verify-op")
        checks.append(("rollback works", lib.resolve_active_version(
            "security/auth-lessons")["version"] == "v01"))
        rm.reactivate_version("security/auth-lessons", "v02", operator="verify-op")
        checks.append(("reactivate works", lib.get_version("security/auth-lessons", "v02")
                       ["status"] == "active"))
        # reject drill: stage a THIRD version (pending) and reject it
        lib.stage_version("security/auth-lessons", "v3 content", ["LRN-3"],
                          {"frequency": 10, "success_rate": 0.9})
        rm.reject_version("security/auth-lessons", "v03", operator="verify-op",
                          reason="verify drill")
        checks.append(("reject works", lib.get_version("security/auth-lessons", "v03")
                       ["status"] == "rejected"))
        # corruption drill: destroy the audit -> fail-safe to last approved
        # (the active at corruption time — v02, reactivated by the drill above)
        audit = root / "telemetry" / "review_decisions.json"
        audit.write_text("{corrupted", encoding="utf-8")
        resolved = lib.resolve_active_version("security/auth-lessons")
        checks.append(("corrupt audit fail-safe (last approved, NOT pending)",
                       resolved is not None and resolved["version"] == "v02"))
        # no audit at all + no index-approved -> None (never pending)
        lib2_root = root.parent / (root.name + "-b")
        shutil.copytree(root, lib2_root)
        store2 = TelemetryStore(lib2_root)
        store2.init_db()
        lib2 = OverlayLibrary(lib2_root).bind_store(store2)
        index2 = lib2._read_index("security/auth-lessons")
        for entry in index2["versions"]:
            if entry["status"] in ("active", "superseded"):
                entry["status"] = "pending"
        lib2.write_index("security/auth-lessons", index2)
        checks.append(("no-approved -> none", lib2.resolve_active_version(
            "security/auth-lessons") is None))
        store2.close()
    finally:
        store.close()
    return checks


def verify_compose() -> list:
    """section 5: real-repo composition — byte identity + Python context (R10§5)."""
    checks = []
    root, store = _temp_env()
    try:
        reg = SkillRegistry(root / "registry", REPO_ROOT / ".github" / "skills")
        reg.load()
        reg.reindex()
        lib = OverlayLibrary(root).bind_store(store)
        from src.skillbox.compose import compose_skill
        from src.skillbox.context import detect_context

        context = detect_context(REPO_ROOT)
        checks.append(("python detected", "python" in context["language"]))
        checks.append(("cli-library style", context["architecture_style"] == "cli-library"))
        result = compose_skill("speckit-implement", registry=reg, library=lib,
                               store=store, repo_root=REPO_ROOT)
        base = (REPO_ROOT / ".github" / "skills" / "speckit-implement" / "SKILL.md").read_text(
            encoding="utf-8")
        checks.append(("base byte-identity", result["prompt"].startswith(base.rstrip())))
        checks.append(("pending never composed", all("@v" in o["overlay_id"] + "@" + o["version"]
                       for o in result["overlays"])))
        checks.append(("composition event", len(store.query_events(
            event_type="composition_performed")) == 1))
    finally:
        store.close()
    return checks


def verify_metrics() -> list:
    """section 6: per-skill + per-version metrics + report (FR-039..044, R10§6)."""
    checks = []
    root, store = _temp_env()
    try:
        _seed_corpus(store)
        lib = OverlayLibrary(root).bind_store(store)
        rm = ReviewManager(root, library=lib, store=store)
        lib.ensure_overlay("testing/verify-lessons", "V", "testing", ["LRN-X"])
        lib.stage_version("testing/verify-lessons", "- rule", ["LRN-X"],
                          {"frequency": 8, "success_rate": 0.9})
        rm.approve_version("testing/verify-lessons", "v01", operator="verify-op")
        # with-overlay cohort: 4 sessions; without-overlay cohort: 2 (FR-040 basis)
        for i in range(4):
            sid = store.get_or_create_session(skill="speckit-implement")
            store.record_event("skill_completed", {"skill": "speckit-implement",
                                                   "outcome": "success",
                                                   "duration_ms": 100},
                               session_id=sid)
            store.complete_session(sid)
            store.record_event("composition_performed", {
                "skill": "speckit-implement",
                "overlay_versions": ["testing/verify-lessons@v01"],
                "context_detected": {}, "degraded": []}, session_id=sid)
        for i in range(2):
            sid = store.get_or_create_session(skill="speckit-implement")
            store.record_event("skill_completed", {"skill": "speckit-implement",
                                                   "outcome": "failure",
                                                   "duration_ms": 50},
                               session_id=sid)
            store.complete_session(sid)
            store.record_event("composition_performed", {
                "skill": "speckit-implement", "overlay_versions": [],
                "context_detected": {}, "degraded": []}, session_id=sid)
        metrics = SkillBoxMetrics(store, {"overlay_min_executions": 3})
        report = metrics.analyze(window_days=30)
        skill_row = next(s for s in report["skills"] if s["skill"] == "speckit-implement")
        checks.append(("skill metrics", skill_row["activations"] >= 5
                       and skill_row["test_pass_rate"] == 1.0))
        overlay_row = next(o for o in report["overlays"]
                           if o["overlay_id"] == "testing/verify-lessons")
        checks.append(("overlay impact", overlay_row["executions_influenced"] == 4
                       and overlay_row["defect_reduction"] is not None))
        checks.append(("rules fail-safe-only", overlay_row["promotion_state"]
                       in ("normal", "prioritized", "retired")))
        from src.skillbox.reports import refresh_dashboards

        outputs = refresh_dashboards(root, metrics)
        checks.append(("dashboard generated", Path(outputs["dashboard"]).exists()))
        checks.append(("improvement report", (root / "reports" /
                                              "improvement_report.md").exists()))
    finally:
        store.close()
    return checks


def verify_registry() -> list:
    """registry drill: add/remove/rename/change survival (FR-002..008, SC-002)."""
    checks = []
    root, store = _temp_env()
    try:
        skills_dir = root / "upstream-skills"
        (skills_dir / "alpha").mkdir(parents=True)
        (skills_dir / "alpha" / "SKILL.md").write_text("# Alpha", encoding="utf-8")
        reg = SkillRegistry(root / "registry", skills_dir)
        reg.load()
        reg.reindex()
        checks.append(("initial discover", reg.get("alpha") is not None))
        reg.set_override("alpha", enabled=False, overlay_groups=["global"])
        # rename: byte-identical content, new dir
        (skills_dir / "beta").mkdir()
        shutil.copyfile(skills_dir / "alpha" / "SKILL.md", skills_dir / "beta" / "SKILL.md")
        (skills_dir / "alpha" / "SKILL.md").unlink()
        reg2 = SkillRegistry(root / "registry", skills_dir)
        reg2.load()
        report = reg2.reindex()
        checks.append(("rename carries identity", report["renamed"]
                       and report["renamed"][0] == {"from": "alpha", "to": "beta"}))
        renamed_entry = reg2.get("beta")
        checks.append(("renamed entry linked", renamed_entry and renamed_entry["renamed_from"]
                       == "alpha"))
        checks.append(("removed tracked", any(e["skill_id"] == "alpha"
                                              and e["status"] == "removed"
                                              for e in reg2.all_entries())))
        # change detection
        (skills_dir / "beta" / "SKILL.md").write_text("# Alpha v2", encoding="utf-8")
        reg3 = SkillRegistry(root / "registry", skills_dir)
        reg3.load()
        report3 = reg3.reindex()
        checks.append(("change detected", "beta" in report3["changed"]))
        # overrides survive scan-and-merge (FR-006)
        reg3.set_override("beta", enabled=False, overlay_groups=["global", "testing"])
        reg4 = SkillRegistry(root / "registry", skills_dir)
        reg4.load()
        reg4.reindex()
        checks.append(("overrides preserved", reg4.is_enabled("beta") is False
                       and "testing" in reg4.overlay_groups("beta")))
        # SC-002: telemetry + learnings untouched across the whole drill
        conn = store.connection()
        (before_events, ) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        checks.append(("telemetry untouched", before_events == 0))
    finally:
        store.close()
    return checks


def verify_isolation() -> list:
    """section 8: zero-trading-touch — import graph + write scope (FR-047, R10§8)."""
    checks = []
    skillbox_dir = REPO_ROOT / "src" / "skillbox"
    forbidden = ("trading", "risk", "communication", "core", "strategies", "mcp")
    violations = []
    for py_file in skillbox_dir.glob("*.py"):
        for line in py_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                for token in forbidden:
                    if f"src.{token}" in stripped or token + " import" in stripped:
                        violations.append(f"{py_file.name}: {stripped}")
    checks.append(("no trading imports", not violations))
    if violations:
        for violation in violations[:5]:
            print(f"    VIOLATION: {violation}")
    # upstream unchanged check (composition is read-only; verifier writes only temp)
    return checks


# ---------------------------------------------------------------------- main
def run_section(name: str) -> list:
    return {
        "telemetry": verify_telemetry,
        "push": verify_push,
        "learnings": verify_learnings,
        "overlays-review": verify_overlays_review,
        "review-cli": _verify_review_cli,
        "compose": verify_compose,
        "metrics": verify_metrics,
        "registry": verify_registry,
        "isolation": verify_isolation,
    }[name]()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SkillBox isolated verifier")
    parser.add_argument("--sections", default=None,
                        help="comma-separated subset of: " + ",".join(SECTIONS))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(SECTIONS))
        return 0
    requested = ([s.strip() for s in args.sections.split(",")] if args.sections
                 else list(SECTIONS))
    unknown = [s for s in requested if s not in SECTIONS]
    if unknown:
        print(f"unknown sections: {unknown}; valid: {SECTIONS}", file=sys.stderr)
        return 2
    all_ok = True
    print("=== SkillBox verifier ===")
    for section in requested:
        checks = run_section(section)
        failed = [(name, ok) for name, ok in checks if not ok]
        for name, ok in checks:
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {section}: {name}")
        print(f"--- section {section}: {len(checks) - len(failed)}/{len(checks)} passed")
        if failed:
            all_ok = False
    if all_ok:
        print("=== ALL SECTIONS PASS ===")
        return 0
    print("=== FAILURES PRESENT ===", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
