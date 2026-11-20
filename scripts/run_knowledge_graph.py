"""Knowledge Graph CLI entry point.

Subcommand surface per specs/003-code-knowledge-graph/contracts/cli-contract.md:
build, update, retrieve, evaluate, report, export, verify.

Convention: machine-consumable JSON goes to stdout; human-readable summaries
to stderr. Exit codes: 0 success (soft-failures included in payload), 2 hard
failure (reason code per contracts/failure-contract.md).
"""
import argparse
import json
import os
import sys
import subprocess
from pathlib import Path
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.kg import report as kg_report

# Contract constants (cli-contract.md defaults).
DEFAULT_SNAPSHOT = os.path.join("output", "kg", "graph_snapshot.json")
DEFAULT_STATE_DIR = os.path.join("output", "kg")


def _not_implemented(task_id):
    raise NotImplementedError(f"handler lands in task {task_id}")


def cmd_build(args):
    from src.kg import graph_builder
    result = graph_builder.build_full(
        repo_root=".",
        out_path=args.out,
        sample=args.sample,
        coedit_max_files=args.coedit_max_files,
    )
    print(json.dumps(result, indent=2))
    summary = (
        f"{result['mode']} build: {result['stats']['files_active']} files, "
        f"{result['stats']['symbols_active']} symbols, "
        f"imports={result['stats']['imports']}, calls={result['stats']['calls']}, "
        f"coedit_pairs={result['stats']['coedit_pairs']}, "
        f"parse_failures={result['stats']['parse_failures']}"
    )
    _render_digest_side_effect()  # digest-artifact contract: BEFORE the summary print
    print(summary, file=sys.stderr)
    kg_report.summarize(summary)
    return 0


def cmd_update(args):
    from src.kg import graph_builder
    result = graph_builder.incremental_update(
        repo_root=".", out_path=args.out, coedit_max_files=args.coedit_max_files
    )
    print(json.dumps(result, indent=2))
    # Full-rebuild fallback (no snapshot/state at first ever run) has no `changes`
    # delta - summarize from the build payload instead (review F3).
    ch = result.get("changes") or {
        "nodes_added": (result.get("stats") or {}).get("files_active", 0)
                       + (result.get("stats") or {}).get("symbols_active", 0),
        "nodes_updated": 0,
        "nodes_retired": (result.get("stats") or {}).get("retired_nodes", 0),
        "edges_added": sum((result.get("stats") or {}).get(k, 0)
                           for k in ("imports", "calls", "coedit_pairs")),
        "renames": [],
    }
    summary = (
        f"update ({result.get('mode', 'update')}): commits={len(result.get('commits_consumed', []))}, "
        f"+{ch.get('nodes_added', 0)} nodes, ~{ch.get('nodes_updated', 0)} updated, "
        f"-{ch.get('nodes_retired', 0)} retired, +{ch.get('edges_added', 0)} edges, "
        f"renames={len(ch.get('renames', []))}"
    )
    _render_digest_side_effect()  # digest-artifact contract: BEFORE the summary print
    print(summary, file=sys.stderr)
    kg_report.summarize(summary)
    return 0


def _load_snapshot_or_fail(out_path):
    """Shared loader: KG-SNAPSHOT-MISSING / KG-SNAPSHOT-SCHEMA -> exit 2."""
    from src.kg import models as kg_models
    if not os.path.exists(out_path):
        rec = kg_report.emit_failure(
            kg_report.KG_SNAPSHOT_MISSING, f"{out_path}: run build first"
        )
        print(json.dumps({"error": rec["reason_code"], "hint": "run build first"}),
              file=sys.stderr)
        return None
    try:
        return kg_models.load_snapshot(out_path)
    except ValueError as exc:
        rec = kg_report.emit_failure(kg_report.KG_SNAPSHOT_SCHEMA, str(exc))
        print(json.dumps({"error": rec["reason_code"], "detail": str(exc)}), file=sys.stderr)
        return None


def cmd_retrieve(args):
    from src.kg import retrieval as kg_retrieval
    snapshot = _load_snapshot_or_fail(args.out or DEFAULT_SNAPSHOT)
    if snapshot is None:
        return 2
    result = kg_retrieval.retrieve(
        snapshot, args.query, top_k=args.top_k, anchors_requested=args.anchors,
        min_anchor_sim=args.min_anchor_sim if args.min_anchor_sim is not None else 0.0,
    )
    if args.format == "json":
        print(json.dumps({"query": args.query, **result}, indent=2))
    else:
        print(f"Query: {args.query}")
        for a in result["anchors"]:
            print(f"  anchor: {a['node_id']} (sim={a['similarity']:.3f})")
        if result["no_confident_match"]:
            print("  no_confident_match: no anchor cleared the similarity floor")
        for i, r in enumerate(result["results"], 1):
            print(f"  {i}. [{r['score']:.4f}] {r['node_id']} "
                  f"(hops={r['hops_from_anchor']})")
    return 0


def cmd_evaluate(args):
    from src.kg import evaluation as kg_evaluation
    snapshot = _load_snapshot_or_fail(DEFAULT_SNAPSHOT)
    if snapshot is None:
        return 2
    try:
        report = kg_evaluation.run_evaluation(snapshot, suite_path=args.suite)
    except FileNotFoundError:
        rec = kg_report.emit_failure(
            kg_report.KG_EVAL_SUITE_INVALID, f"suite file missing: {args.suite}"
        )
        print(json.dumps(rec), file=sys.stderr)
        return 2
    from src.kg.models import _atomic_write_json
    _atomic_write_json(args.out, report)
    print(json.dumps({"aggregate": report["aggregate"], "guidance": report["guidance"],
                       "invalid_queries": report["invalid_queries"],
                       "report_path": args.out}, indent=2))
    return 0


def cmd_report(args):
    from src.kg import models as kg_models
    snapshot = _load_snapshot_or_fail(args.out or DEFAULT_SNAPSHOT)
    if snapshot is None:
        return 2
    state_path = os.path.join(DEFAULT_STATE_DIR, kg_models.UPDATE_STATE_FILENAME)
    state = None
    if os.path.exists(state_path):
        state = kg_models.load_update_state(DEFAULT_STATE_DIR)
    payload = {
        "snapshot_path": args.out or DEFAULT_SNAPSHOT,
        "built_at": snapshot.built_at,
        "commit_sha": snapshot.commit_sha,
        "built_from_commit_count": snapshot.built_from_commit_count,
        "stats": snapshot.stats,
        "last_update": ({"last_commit_sha": state.last_commit_sha,
                          "updated_at": state.updated_at,
                          "processed_commits_recorded": len(state.processed_commits)}
                         if state else None),
    }
    nodes = snapshot.nodes
    by_kind_status = {}
    for n in nodes:
        key = f"{n.kind}:{n.status}"
        by_kind_status[key] = by_kind_status.get(key, 0) + 1
    edges_by_type = {}
    for e in snapshot.edges:
        edges_by_type[e.type] = edges_by_type.get(e.type, 0) + 1
    payload["nodes_by_kind_status"] = by_kind_status
    payload["edges_by_type"] = edges_by_type
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(f"Snapshot: {payload['snapshot_path']}")
        print(f"  built_at: {payload['built_at']}  commit: {payload['commit_sha']}")
        print(f"  nodes: {json.dumps(by_kind_status)}")
        print(f"  edges: {json.dumps(edges_by_type)}")
        print(f"  parse_failures: {payload['stats']['parse_failures']}  "
              f"bulk_commits_skipped: {payload['stats']['coedit_commits_skipped_bulk']}")
        if payload["last_update"]:
            print(f"  last update: {payload['last_update']['updated_at']} "
                  f"(state commit {payload['last_update']['last_commit_sha']})")
    return 0


def cmd_export(args):
    """FR-016 interop: emit the adopted library's native format (research R2)."""
    from src.kg import graph_builder as kg_graph_builder
    if args.library_format != "networkx":
        rec = kg_report.emit_failure(
            kg_report.KG_LIB_UNAVAILABLE,
            f"unsupported --library-format {args.library_format!r} (v1 supports: networkx)",
        )
        print(json.dumps(rec), file=sys.stderr)
        return 2
    try:
        import networkx  # noqa: F401
    except ImportError:
        rec = kg_report.emit_failure(
            kg_report.KG_LIB_UNAVAILABLE,
            "networkx not importable in this venv (pip install networkx); stdlib engine unaffected",
        )
        print(json.dumps(rec), file=sys.stderr)
        return 2
    out_path = args.out or os.path.join(DEFAULT_STATE_DIR, "graph_snapshot.networkx.json")
    # Review F7: --out is purely the export destination; the graph source is
    # always the canonical snapshot.
    snapshot = _load_snapshot_or_fail(DEFAULT_SNAPSHOT)
    if snapshot is None:
        return 2
    payload = kg_graph_builder.export_networkx(snapshot)
    from src.kg.models import _atomic_write_json
    _atomic_write_json(out_path, payload)
    print(json.dumps({"format": "networkx", "nodes": len(payload["nodes"]),
                       "edges": len(payload["edges"]), "out_path": out_path}))
    return 0


def cmd_verify(args):
    from src.kg import models as kg_models
    out_path = args.out or DEFAULT_SNAPSHOT
    if not os.path.exists(out_path):
        rec = kg_report.emit_failure(kg_report.KG_SNAPSHOT_MISSING,
                                      f"{out_path}: run build first")
        print(json.dumps({"verdict": "FAIL", "error": rec["reason_code"]}))
        return 2
    try:
        snapshot = kg_models.load_snapshot(out_path)
    except ValueError as exc:
        kg_report.emit_failure(kg_report.KG_SNAPSHOT_SCHEMA, str(exc))
        print(json.dumps({"verdict": "FAIL", "error": "KG-SNAPSHOT-SCHEMA",
                           "detail": str(exc)}))
        return 2
    state = None
    state_path = os.path.join(DEFAULT_STATE_DIR, kg_models.UPDATE_STATE_FILENAME)
    if os.path.exists(state_path):
        state = kg_models.load_update_state(DEFAULT_STATE_DIR)
    from scripts import verify_knowledge_graph as _vg  # reuse invariants
    failures = _vg.check_invariants(snapshot, state)
    print(json.dumps({"verdict": "PASS" if not failures else "FAIL",
                       "violations": failures}))
    return 0 if not failures else 2




def _render_digest_side_effect(out_path="output/kg/digest_for_reviews.md"):
    """Feature-005 US3: auto-render the digest at build/update completion. Fail-soft:
    logs KG-DIGEST-RENDER-FAILED, never alters the caller's exit status."""
    try:
        _args = type("NS", (), {"out": out_path})()
        cmd_digest(_args)
    except Exception as exc:
        print(json.dumps({"error": f"KG-DIGEST-RENDER-FAILED: {exc}"}), file=sys.stderr)
        kg_report.summarize(f"KG-DIGEST-RENDER-FAILED: {exc}")


def cmd_digest(args):
    """Render the review-facing digest (feature 005 US3).

    Reads the graph snapshot, renders the digest artifact via kg.digest.render_digest
    with the manifest's digest_max_bytes budget, applies one-generation rotation
    (previous digest -> digest_for_reviews_prev.md), and compares snapshot commit_sha
    against git HEAD for freshness labeling (A1 semantics). Missing/corrupt snapshot
    or schema mismatch exits 2 (digest-artifact contract; KG-SNAPSHOT-* semantics).
    Render failure logs KG-DIGEST-RENDER-FAILED, leaves any previous digest untouched,
    and never alters the build/update caller's exit status (auto-render side effect).
    """
    out_path = Path(args.out) if getattr(args, "out", None) else Path("output/kg/digest_for_reviews.md")
    snap_path = Path("output/kg/graph_snapshot.json")
    man_path = Path(".specify/wiring-manifest.yml")
    try:
        snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
        if not man_path.exists():
            print(json.dumps({"error": "KG-DIGEST-RENDER-FAILED",
                              "detail": f"manifest missing: {man_path} (budget unreadable; no render)"}),
                  file=sys.stderr)
            return 2
        manifest = yaml.safe_load(man_path.read_text(encoding="utf-8"))
        budgets = (manifest.get("token_budgets") or {})
        head = git_head_sha()
        status = "fresh" if (not head or snapshot.get("commit_sha") == head) else "stale"
        content = _render_digest_content(snapshot, budgets, head)
        # schema check (mirror the loader expectations; synthetic dicts tolerated)
        if isinstance(snapshot, dict) and "schema_version" in snapshot \
                and snapshot.get("schema_version") != _snapshot_schema_version():
            raise ValueError(
                f"unsupported snapshot schema_version: {snapshot.get('schema_version')!r} "
                f"(loader supports {_snapshot_schema_version()})"
            )
    except FileNotFoundError as exc:
        rec = kg_report.emit_failure(kg_report.KG_SNAPSHOT_MISSING, f"{snap_path}: run build first")
        print(json.dumps({"error": rec["reason_code"], "hint": "run build first", "detail": str(exc)}), file=sys.stderr)
        return 2
    except ValueError as exc:  # schema mismatch (GraphSnapshot.from_dict parity)
        rec = kg_report.emit_failure(kg_report.KG_SNAPSHOT_SCHEMA, str(exc))
        print(json.dumps({"error": rec["reason_code"], "detail": str(exc)}), file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        rec = kg_report.emit_failure(kg_report.KG_SNAPSHOT_SCHEMA, f"{snap_path} corrupt: {exc}")
        print(json.dumps({"error": rec["reason_code"], "detail": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"error": f"KG-DIGEST-RENDER-FAILED: {exc}"}), file=sys.stderr)
        kg_report.summarize(f"KG-DIGEST-RENDER-FAILED: {exc}")
        return 0  # render failure never alters exit status (contract)

    # one-generation rotation
    prev = Path(str(out_path).replace("_for_reviews", "_for_reviews_prev"))
    if out_path.exists():
        prev.write_text(out_path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8", newline="\n")
    result = {"path": str(out_path), "bytes": len(content.encode("utf-8")),
              "source_commit": snapshot.get("commit_sha"), "built_at": snapshot.get("built_at"),
              "status": status, "truncated": "(truncated)" in content}
    fmt = getattr(args, "format", "text") or "text"
    if fmt == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"digest: {result['path']} bytes={result['bytes']} status={status}"
              + (" (truncated)" if result["truncated"] else ""), file=sys.stderr)
    kg_report.summarize(f"digest: {result['path']} bytes={result['bytes']} status={status}")
    return 0


def _render_digest_content(snapshot, budgets, head):
    """Render via kg.digest; raise ValueError on schema mismatch (loader parity)."""
    from src.kg.digest import render_digest as _render
    from src.kg.models import SCHEMA_VERSION as _SV

    if isinstance(snapshot, dict) and "schema_version" in snapshot \
            and snapshot.get("schema_version") != _SV:
        raise ValueError(
            f"unsupported snapshot schema_version: {snapshot.get('schema_version')!r} "
            f"(loader supports {_SV})"
        )
    return _render(snapshot, budgets, head_sha=head)


def _snapshot_schema_version():
    try:
        from src.kg.models import SCHEMA_VERSION
        return SCHEMA_VERSION
    except Exception:  # pragma: no cover - loader unavailable is itself a schema failure
        return -1


def git_head_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return None



def build_parser():
    p = argparse.ArgumentParser(prog="run_knowledge_graph.py", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="full graph build (or --sample N)")
    b.add_argument("--sample", type=int, default=None, help="build over the N most import-connected files")
    b.add_argument("--full", action="store_true", help="build over all repo Python files (default)")
    b.add_argument("--coedit-max-files", type=int, default=20, help="bulk-commit threshold (default 20)")
    b.add_argument("--out", default=None, help="snapshot destination (default output/kg/graph_snapshot.json)")

    u = sub.add_parser("update", help="incremental fold-in of commits newer than recorded state")
    u.add_argument("--coedit-max-files", type=int, default=20, help="bulk-commit threshold (default 20)")
    u.add_argument("--out", default=None, help="snapshot destination (default output/kg/graph_snapshot.json)")

    r = sub.add_parser("retrieve", help="two-step retrieval: anchor + traversal")
    r.add_argument("--query", required=True, help="free-text query")
    r.add_argument("--top-k", type=int, default=5)
    r.add_argument("--anchors", type=int, default=3)
    r.add_argument("--format", choices=["json", "text"], default="text")
    r.add_argument("--min-anchor-sim", type=float, default=None, help="anchor similarity floor")
    r.add_argument("--out", default=None, help="snapshot path (default output/kg/graph_snapshot.json)")

    e = sub.add_parser("evaluate", help="graph vs regex vs text-search evaluation")
    e.add_argument("--suite", default="tests/fixtures/kg_query_suite.json")
    e.add_argument("--out", default="output/kg/evaluation_report.json")

    rep = sub.add_parser("report", help="graph state summary")
    rep.add_argument("--format", choices=["json", "text"], default="text")
    rep.add_argument("--out", default=None, help="snapshot path (default output/kg/graph_snapshot.json)")

    x = sub.add_parser("export", help="emit graph in an adopted library's native format")
    x.add_argument("--library-format", required=True, help="e.g. networkx (per FR-016 interop contract)")
    x.add_argument("--out", default=None)

    v = sub.add_parser("verify", help="self-check snapshot invariants")
    v.add_argument("--out", default=None, help="snapshot path (default output/kg/graph_snapshot.json)")

    b.set_defaults(func=cmd_build)
    u.set_defaults(func=cmd_update)
    r.set_defaults(func=cmd_retrieve)
    e.set_defaults(func=cmd_evaluate)
    rep.set_defaults(func=cmd_report)
    x.set_defaults(func=cmd_export)
    d = sub.add_parser("digest", help="render the review-facing digest (feature 005)")
    d.add_argument("--format", choices=["text", "json"], default="text")
    d.add_argument("--out", default=None, help="digest output path (default output/kg/digest_for_reviews.md)")

    b.set_defaults(func=cmd_build)
    u.set_defaults(func=cmd_update)
    r.set_defaults(func=cmd_retrieve)
    e.set_defaults(func=cmd_evaluate)
    rep.set_defaults(func=cmd_report)
    x.set_defaults(func=cmd_export)
    v.set_defaults(func=cmd_verify)
    d.set_defaults(func=cmd_digest)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        return 0
    except NotImplementedError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
