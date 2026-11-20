"""Knowledge graph snapshot verifier (repo verify_*.py convention).

Checks the cross-entity invariants from specs/003-code-knowledge-graph/data-model.md
against a live snapshot + update-state pair:
  1. Every active edge references active nodes; retired nodes have zero active edges.
  2. Edge id uniqueness + per-type endpoint-kind rules (coedit=file<->file,
     calls=symbol<->symbol); coedit pairs link Python files only.
  3. stats equals recomputed counts.
  4. update_state.last_commit_sha equals snapshot.commit_sha.
  5. s:<path>#* ids reference an existing f:<path> node.
  6. Re-serialization determinism (sorted node/edge arrays byte-stable).

Exit 0 = all pass; exit 1 = at least one violation. PASS/FAIL lines printed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.kg import models as kg_models

DEFAULT_SNAPSHOT = os.path.join("output", "kg", "graph_snapshot.json")
DEFAULT_STATE_DIR = os.path.join("output", "kg")


def check_invariants(snapshot, state):
    failures = []
    nodes = snapshot.nodes
    edges = snapshot.edges

    node_by_id = {n.id: n for n in nodes}
    if len(node_by_id) != len(nodes):
        failures.append("duplicate node ids present")

    # 1 + 2: endpoint integrity and type rules
    edge_ids = set()
    for e in edges:
        if e.id in edge_ids:
            failures.append(f"duplicate edge id: {e.id}")
        edge_ids.add(e.id)
        endpoints_retired = [ep for ep in (e.src, e.dst)
                             if node_by_id.get(ep) is not None
                             and node_by_id[ep].status != "active"]
        # Review F4: deletion/def-removal zero import/calls weights and KEEP the
        # edge as an audit tombstone (data-model Edge lifecycle). Co-edit edges are
        # historical facts (weight = linking commits) and may legitimately reference
        # retired files - the graph records what co-edited with what, even after
        # deletion. Tombstone rules:
        #   - import/calls touching retired nodes: valid iff weight == 0
        #   - coedit touching retired nodes: always valid (historical record)
        #   - anything touching retired nodes with weight > 0 for import/calls:
        #     violation ("retired nodes have zero active edges")
        tombstone_ok = (
            endpoints_retired
            and (e.type == "coedit" or e.weight == 0.0)
        )
        if not tombstone_ok:
            for ep in endpoints_retired:
                failures.append(
                    f"edge {e.id} touches retired node {ep} (re-parent or zero the weight)"
                )
        for endpoint in (e.src, e.dst):
            n = node_by_id.get(endpoint)
            if n is None:
                failures.append(f"edge {e.id} references missing node {endpoint}")
        if e.type == "coedit":
            if not (e.src.startswith("f:") and e.dst.startswith("f:")):
                failures.append(f"coedit edge {e.id} endpoints must be file nodes")
            else:
                for ep in (e.src, e.dst):
                    path = ep[2:]
                    if not path.endswith(".py"):
                        failures.append(f"coedit edge {e.id} links non-Python path {path}")
        if e.type == "calls":
            if not (e.src.startswith("s:") and e.dst.startswith("s:")):
                failures.append(f"calls edge {e.id} endpoints must be symbol nodes")
        if e.type in ("import", "calls") and e.weight == 0.0 and not endpoints_retired:
            failures.append(f"weight-0 {e.type} edge {e.id} between active nodes (should be dropped)")

    # 3: stats reconciliation
    recomputed = kg_models.compute_stats(
        nodes, edges,
        parse_failures=snapshot.stats.get("parse_failures", 0),
        coedit_commits_used=snapshot.stats.get("coedit_commits_used", 0),
        coedit_commits_skipped_bulk=snapshot.stats.get("coedit_commits_skipped_bulk", 0),
    )
    for key in kg_models.STATS_KEYS:
        if snapshot.stats.get(key) != recomputed[key]:
            failures.append(
                f"stats[{key}]={snapshot.stats.get(key)} != recomputed {recomputed[key]}"
            )

    # 4: update-state pairing
    if state is not None and state.last_commit_sha != snapshot.commit_sha:
        failures.append(
            f"update_state.last_commit_sha={state.last_commit_sha} != "
            f"snapshot.commit_sha={snapshot.commit_sha}"
        )

    # 5: symbol ownership
    for n in nodes:
        if n.kind == "symbol":
            owner_id = kg_models.file_node_id(n.path)
            if owner_id not in node_by_id:
                failures.append(f"symbol {n.id} has no owning file node {owner_id}")

    # 6: re-serialization determinism
    d1 = json.dumps(snapshot.to_dict(), sort_keys=False)
    d2 = json.dumps(kg_models.GraphSnapshot.from_dict(json.loads(d1)).to_dict(), sort_keys=False)
    if d1 != d2:
        failures.append("re-serialization not byte-stable")

    return failures


def main():
    out_path = DEFAULT_SNAPSHOT
    if not os.path.exists(out_path):
        print("FAIL KG-SNAPSHOT-MISSING: no snapshot at", out_path)
        print("  run: python scripts/run_knowledge_graph.py build --full")
        return 1

    checks = []
    try:
        snapshot = kg_models.load_snapshot(out_path)
        checks.append(("snapshot loads with valid schema", True, ""))
    except ValueError as exc:
        print(f"FAIL KG-SNAPSHOT-SCHEMA: {exc}")
        return 1

    state = None
    state_path = os.path.join(DEFAULT_STATE_DIR, kg_models.UPDATE_STATE_FILENAME)
    if os.path.exists(state_path):
        state = kg_models.load_update_state(DEFAULT_STATE_DIR)
        checks.append(("update_state.json present", True, ""))

    failures = check_invariants(snapshot, state)

    print("=== Knowledge Graph Verifier ===")
    print(f"snapshot: {out_path} (commit {snapshot.commit_sha}, "
          f"built {snapshot.built_at})")
    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label} {detail}")
    invariants = [
        "edge endpoint integrity (weight-0 tombstones on retired nodes accepted)",
        "edge id uniqueness + endpoint-kind rules",
        "stats reconciliation",
        "update-state pairing",
        "symbol ownership",
        "byte-stable re-serialization",
    ]
    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        print(f"RESULT: FAIL ({len(failures)} violation(s))")
        return 1
    for inv in invariants:
        print(f"  [PASS] {inv}")
    s = snapshot.stats
    print(f"RESULT: PASS — files={s['files_active']} symbols={s['symbols_active']} "
          f"imports={s['imports']} calls={s['calls']} coedit={s['coedit_pairs']} "
          f"retired={s['retired_nodes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
