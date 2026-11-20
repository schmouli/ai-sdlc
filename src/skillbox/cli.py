"""SkillBox CLI: `python -m src.skillbox <command>` (contracts/cli.md, research R12/R15).

Exit codes: 0 success; 1 command failure (structured reason printed); 2 usage error.
Operator identity: --operator flag or SKILLBOX_OPERATOR env (R11 — never inferred).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.skillbox import (OperatorIdentityError, SkillboxContractError,
                          SkillboxLookupError)


def _emit(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    elif isinstance(data, dict):
        for key, value in data.items():
            print(f"{key}: {value}")
    elif isinstance(data, list):
        for item in data:
            print(f"- {item}")
    else:
        print(data)


def _store(root: Optional[Path]):
    from src.skillbox.telemetry import default_store

    return default_store(root)


def _root(args) -> Path:
    from src.skillbox.config import ensure_state_root, state_root

    root = Path(args.state_root) if getattr(args, "state_root", None) else state_root()
    ensure_state_root(root)
    return root


# ---------------------------------------------------------------- command impls
def cmd_status(args) -> int:
    from src.skillbox.config import enabled as cfg_enabled

    root = _root(args)
    store = _store(root)
    try:
        pending_events = len(store.pending_push_events())
        sessions = store.connection().execute(
            "SELECT status, COUNT(*) AS n FROM sessions GROUP BY status"
        ).fetchall()
        summary = {
            "enabled": cfg_enabled(),
            "state_root": str(root),
            "pending_pushes": pending_events,
            "sessions": {r["status"]: r["n"] for r in sessions},
        }
        try:
            from src.skillbox.overlays import OverlayLibrary

            lib = OverlayLibrary(root).bind_store(store)
            summary["overlays"] = len(lib.all_meta())
        except Exception:
            summary["overlays"] = 0
        _emit(summary, args.json)
        return 0
    finally:
        store.close()


def cmd_registry_reindex(args) -> int:
    from src.skillbox.registry import SkillRegistry

    root = _root(args)
    skills_root = Path(".github/skills")
    reg = SkillRegistry(root / "registry", skills_root)
    reg.load()
    report = reg.reindex()
    _emit(report, args.json)
    return 0


def cmd_registry_list(args) -> int:
    from src.skillbox.registry import SkillRegistry

    root = _root(args)
    reg = SkillRegistry(root / "registry", Path(".github/skills"))
    reg.load()
    reg.ensure_indexed()
    entries = reg.all_entries()
    _emit(entries if args.json else [f"{e['skill_id']} ({e.get('status', 'active')})" for e in entries],
          args.json)
    return 0


def cmd_context_detect(args) -> int:
    from src.skillbox.context import detect_context

    context = detect_context(Path("."))
    _emit(context, args.json)
    return 0


def cmd_events_record(args) -> int:
    store = _store(_root(args))
    try:
        payload = json.loads(args.payload) if args.payload else {}
        event_id = store.record_event(args.type, payload, skill=args.skill)
        _emit({"event_id": event_id}, args.json)
        return 0
    except json.JSONDecodeError as exc:
        print(f"invalid payload JSON: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"record failed: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


def cmd_events_query(args) -> int:
    store = _store(_root(args))
    try:
        events = store.query_events(skill=args.skill, session_id=args.session,
                                    event_type=args.type, since=args.since,
                                    until=args.until)
        _emit(events, args.json)
        return 0
    finally:
        store.close()


def cmd_events_reindex(args) -> int:
    store = _store(_root(args))
    try:
        counts = store.rebuild_from_jsonl()
        _emit(counts, args.json)
        return 0
    finally:
        store.close()


def cmd_push(args) -> int:
    from src.skillbox.config import mlflow_config
    from src.skillbox.mlflow_sink import MlflowSink

    store = _store(_root(args))
    try:
        sink = MlflowSink(mlflow_config()).bind_store(store)
        pushed = sink.flush_queue(store, force=True)
        _emit({"pushed": pushed}, args.json)
        return 0
    finally:
        store.close()


def cmd_learnings_extract(args) -> int:
    from src.skillbox.learning import LearningEngine

    root = _root(args)
    store = _store(root)
    try:
        cfg = _skillbox_section(args).get("learning", {})
        engine = LearningEngine(store, root / "learnings", cfg)
        learnings = engine.extract_learnings(window_days=args.window_days)
        _emit([_learning_view(r) for r in learnings], args.json)
        return 0
    finally:
        store.close()


def cmd_learnings_list(args) -> int:
    root = _root(args)
    store = _store(root)
    try:
        sql = "SELECT learning_id, pattern, frequency, success_rate, recommendation,"\
              " category, status FROM learnings"
        params: List[Any] = []
        conditions = []
        if args.category:
            conditions.append("category = ?")
            params.append(args.category)
        if args.status:
            conditions.append("status = ?")
            params.append(args.status)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        rows = store.connection().execute(sql, params).fetchall()
        _emit([dict(r) for r in rows], args.json)
        return 0
    finally:
        store.close()


def cmd_overlays_generate(args) -> int:
    from src.skillbox.learning import LearningEngine
    from src.skillbox.overlays import OverlayLibrary

    root = _root(args)
    store = _store(root)
    try:
        cfg = _skillbox_section(args)
        lib = OverlayLibrary(root).bind_store(store)
        engine = LearningEngine(store, root / "learnings", cfg.get("learning", {}))
        result = generate_overlays(lib, engine)
        _emit(result, args.json)
        return 0
    finally:
        store.close()


def cmd_overlays_list(args) -> int:
    from src.skillbox.overlays import OverlayLibrary

    root = _root(args)
    store = _store(root)
    try:
        lib = OverlayLibrary(root).bind_store(store)
        out = []
        for meta in lib.all_meta():
            if args.collection and meta["collection"] != args.collection:
                continue
            resolved = lib.resolve_active_version(meta["overlay_id"])
            out.append({**meta, "active_version": resolved["version"] if resolved else None})
        _emit(out, args.json)
        return 0
    finally:
        store.close()


def cmd_overlays_show(args) -> int:
    from src.skillbox.overlays import OverlayLibrary

    root = _root(args)
    store = _store(root)
    try:
        lib = OverlayLibrary(root).bind_store(store)
        overlay_id = args.overlay_id
        version = args.version
        if not version:
            resolved = lib.resolve_active_version(overlay_id)
            version = resolved["version"] if resolved else None
        if not version:
            print(f"no approved active version for {overlay_id}", file=sys.stderr)
            return 1
        entry = lib.get_version(overlay_id, version)
        if entry is None:
            print(f"unknown version {overlay_id}@{version}", file=sys.stderr)
            return 1
        content = lib.read_content(overlay_id, version) or ""
        _emit({**entry, "content": content}, args.json if args.json else False)
        if not args.json:
            print(content)
        return 0
    finally:
        store.close()


def cmd_review_list(args) -> int:
    from src.skillbox.overlays import OverlayLibrary

    root = _root(args)
    store = _store(root)
    try:
        cfg = _skillbox_section(args)
        sla = int(cfg.get("review", {}).get("pending_review_sla_days", 7))
        lib = OverlayLibrary(root).bind_store(store)
        pending = lib.list_pending_reviews(sla_days=sla)
        _emit(pending, args.json)
        return 0
    finally:
        store.close()


def _review_op(decision: str, db_decision: str):
    def op(args) -> int:
        from src.skillbox.review import default_review_manager

        root = _root(args)
        rm = default_review_manager(root)
        try:
            kwargs = {"rationale": args.reason or ""} if db_decision != "reject" else \
                {"reason": args.reason or ""}
            method = {
                "approve": rm.approve_version,
                "reject": rm.reject_version,
                "rollback": rm.rollback_version,
                "re-activate": rm.reactivate_version,
            }[db_decision]
            record = method(args.overlay_id, args.version, operator=args.operator, **kwargs)
            print(f"{db_decision} recorded: {args.overlay_id}@{args.version} by "
                  f"{record['operator']} ({record['from_status']} -> {record['to_status']})")
            return 0
        except OperatorIdentityError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except SkillboxContractError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"review failed: {exc}", file=sys.stderr)
            return 1

    return op


def cmd_metrics_analyze(args) -> int:
    from src.skillbox.metrics import SkillBoxMetrics

    root = _root(args)
    store = _store(root)
    try:
        metrics = SkillBoxMetrics(store)
        report = metrics.analyze(window_days=args.window_days)
        _emit(report, args.json)
        return 0
    finally:
        store.close()


def cmd_metrics_report(args) -> int:
    from src.skillbox.metrics import SkillBoxMetrics
    from src.skillbox.reports import write_improvement_report

    root = _root(args)
    store = _store(root)
    try:
        metrics = SkillBoxMetrics(store)
        report_path = write_improvement_report(root, metrics,
                                               baseline_days=args.baseline_days)
        print(f"improvement report written: {report_path}")
        return 0
    finally:
        store.close()


def cmd_dashboards_refresh(args) -> int:
    from src.skillbox.metrics import SkillBoxMetrics
    from src.skillbox.reports import refresh_dashboards

    root = _root(args)
    store = _store(root)
    try:
        outputs = refresh_dashboards(root, SkillBoxMetrics(store))
        _emit(outputs, args.json)
        return 0
    finally:
        store.close()


def cmd_inject_refresh(args) -> int:
    from src.skillbox.inject import refresh as inject_refresh

    # injection targets the REPO ROOT's .github/copilot-instructions.md —
    # a --state-root override (tests) must not relocate the IDE file
    store = _store(_root(args))
    try:
        result = inject_refresh(Path("."), store=store)
        _emit(result, args.json)
        return 0
    finally:
        store.close()


def cmd_inject_status(args) -> int:
    from src.skillbox.inject import compose_digest, inject_block_path
    from src.skillbox.overlays import OverlayLibrary

    root = _root(args)
    store = _store(root)
    try:
        lib = OverlayLibrary(root).bind_store(store)
        target = inject_block_path(Path("."))
        exists = target.exists()
        injected = exists and "SKILLBOX:BEGIN" in target.read_text(encoding="utf-8")
        _emit({
            "instructions_file": str(target),
            "file_exists": exists,
            "block_present": injected,
            "approved_overlays": len(compose_digest(lib)),
        }, args.json)
        return 0
    finally:
        store.close()


def _skillbox_section(args) -> Dict[str, Any]:
    try:
        from src.skillbox.config import get_skillbox_config

        return get_skillbox_config()
    except Exception:
        return {}


def _learning_view(record: Dict[str, Any]) -> Dict[str, Any]:
    return {k: record.get(k) for k in ("learning_id", "pattern", "frequency",
                                       "success_rate", "recommendation", "category",
                                       "status")}


# --------------------------------------------------------------- public ops
def generate_overlays(lib, engine) -> Dict[str, Any]:
    """Stage pending versions from overlay-eligible learnings (FR-028 agent-api)."""
    from src.skillbox.learning import CATEGORIES

    staged: List[Dict[str, Any]] = []
    rows = lib._store.connection().execute(
        "SELECT learning_id, pattern, frequency, success_rate, recommendation, category"
        " FROM learnings WHERE status = 'overlay-eligible'"
    ).fetchall()
    for row in rows:
        overlay_id = f"{row['category']}/{_slugify(row['pattern'])}"
        meta = lib.ensure_overlay(overlay_id, title=_short_title(row["pattern"]),
                                  collection=row["category"],
                                  source_learning_ids=[row["learning_id"]])
        content = _render_overlay_content(meta, row)
        entry = lib.stage_version(
            overlay_id, content, [row["learning_id"]],
            {"frequency": row["frequency"], "success_rate": row["success_rate"],
             "recommendation": row["recommendation"]},
        )
        staged.append({"overlay_id": overlay_id, "version": entry["version"]})
    return {"staged": staged, "retired": [], "regenerated": []}


def _slugify(pattern: str) -> str:
    words = "".join(ch if ch.isalnum() else " " for ch in pattern.lower()).split()
    keep = [w for w in words if w][:8]
    return "-".join(keep[:6])[:80] or "overlay"


def _short_title(pattern: str) -> str:
    return pattern[:80]


def _render_overlay_content(meta: Dict[str, Any], learning_row) -> str:
    lines = [
        f"# {meta['title']}",
        "",
        f"Collection: {meta['collection']}",
        f"Evidence: frequency={learning_row['frequency']}, "
        f"success_rate={learning_row['success_rate']:.2f}",
        "",
        "## Recommendation",
        "",
        learning_row["recommendation"],
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skillbox", description="SkillBox Runtime CLI")
    parser.add_argument("--env", default=None, help="config environment override")
    parser.add_argument("--state-root", default=None, help="state root override")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status").set_defaults(func=cmd_status)

    p = sub.add_parser("registry")
    rsub = p.add_subparsers(dest="registry_command")
    rsub.add_parser("reindex").set_defaults(func=cmd_registry_reindex)
    rlist = rsub.add_parser("list")
    rlist.set_defaults(func=cmd_registry_list)

    ctx = sub.add_parser("context")
    ctxsub = ctx.add_subparsers(dest="context_command")
    ctxsub.add_parser("detect").set_defaults(func=cmd_context_detect)

    ev = sub.add_parser("events")
    evsub = ev.add_subparsers(dest="events_command")
    rec = evsub.add_parser("record")
    rec.add_argument("--skill", default=None)
    rec.add_argument("--type", required=True)
    rec.add_argument("--payload", default=None)
    rec.set_defaults(func=cmd_events_record)
    q = evsub.add_parser("query")
    q.add_argument("--skill", default=None)
    q.add_argument("--session", default=None)
    q.add_argument("--type", default=None)
    q.add_argument("--since", default=None)
    q.add_argument("--until", default=None)
    q.set_defaults(func=cmd_events_query)
    evsub.add_parser("reindex").set_defaults(func=cmd_events_reindex)

    sub.add_parser("push").set_defaults(func=cmd_push)

    ln = sub.add_parser("learnings")
    lnsub = ln.add_subparsers(dest="learnings_command")
    ext = lnsub.add_parser("extract")
    ext.add_argument("--window-days", type=int, default=None)
    ext.set_defaults(func=cmd_learnings_extract)
    lnl = lnsub.add_parser("list")
    lnl.add_argument("--category", default=None)
    lnl.add_argument("--status", default=None)
    lnl.set_defaults(func=cmd_learnings_list)

    ov = sub.add_parser("overlays")
    ovsub = ov.add_subparsers(dest="overlays_command")
    ovsub.add_parser("generate").set_defaults(func=cmd_overlays_generate)
    ovl = ovsub.add_parser("list")
    ovl.add_argument("--collection", default=None)
    ovl.set_defaults(func=cmd_overlays_list)
    ovs = ovsub.add_parser("show")
    ovs.add_argument("overlay_id")
    ovs.add_argument("--version", default=None)
    ovs.set_defaults(func=cmd_overlays_show)

    rv = sub.add_parser("review")
    rvsub = rv.add_subparsers(dest="review_command")
    rvsub.add_parser("list").set_defaults(func=cmd_review_list)
    for decision, db_decision in (("approve", "approve"), ("reject", "reject"),
                                  ("rollback", "rollback"),
                                  ("reactivate", "re-activate")):
        d = rvsub.add_parser(decision)
        d.add_argument("overlay_id")
        d.add_argument("version")
        d.add_argument("--operator", default=None)
        d.add_argument("--reason", default=None)
        d.set_defaults(func=_review_op(decision, db_decision))

    mt = sub.add_parser("metrics")
    mtsub = mt.add_subparsers(dest="metrics_command")
    ma = mtsub.add_parser("analyze")
    ma.add_argument("--window-days", type=int, default=None)
    ma.set_defaults(func=cmd_metrics_analyze)
    mr = mtsub.add_parser("report")
    mr.add_argument("--baseline-days", type=int, default=None)
    mr.set_defaults(func=cmd_metrics_report)

    db = sub.add_parser("dashboards")
    dbsub = db.add_subparsers(dest="dashboards_command")
    dbsub.add_parser("refresh").set_defaults(func=cmd_dashboards_refresh)

    inj = sub.add_parser("inject")
    injsub = inj.add_subparsers(dest="inject_command")
    injsub.add_parser("refresh").set_defaults(func=cmd_inject_refresh)
    injsub.add_parser("status").set_defaults(func=cmd_inject_status)

    return parser


def _split_global_flags(parser: argparse.ArgumentParser, argv: List[str]):
    """Parse with global flags allowed on either side of the subcommand.

    Returns (args, []). When global flags appear after the subcommand the
    first pass yields unrecognized tokens; those known-flag tokens (plus
    their values) are hoisted to the front and parsing retried. Returns
    (None, unrecognized) when retry also fails, so main() can raise usage.
    """
    args, unrecognized = parser.parse_known_args(argv)
    if not unrecognized:
        return args, []
    global_flags = {"--env", "--state-root"}
    boolean_flags = {"--json", "--verbose"}
    hoisted, rest, skip = [], [], False
    for i, token in enumerate(argv):
        if skip:
            skip = False
            continue
        if token in boolean_flags:
            hoisted.append(token)
        elif token in global_flags:
            hoisted.append(token)
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                hoisted.append(argv[i + 1])
                skip = True
        else:
            rest.append(token)
    if not hoisted:
        return None, unrecognized
    try:
        return parser.parse_args(hoisted + rest), []
    except SystemExit:
        return None, unrecognized


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    # global flags accepted on either side of the subcommand
    # (python -m src.skillbox [--json] status  OR  python -m src.skillbox status [--json])
    if argv:
        hoisted_args, unrecognized = _split_global_flags(parser, argv)
        args = hoisted_args if hoisted_args is not None else parser.parse_args(argv)
    else:
        args, _ = parser.parse_known_args(argv)
    if getattr(args, "env", None):
        import os

        os.environ["METATRADER_ENV"] = args.env
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
