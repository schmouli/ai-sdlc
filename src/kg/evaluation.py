"""Evaluation harness: graph retrieval vs regex vs text search (FR-010, US4).

Runs each suite query through three methods with IDENTICAL hit scoring
(expected node within top-5):
  graph        - two-step retrieval (retrieval.py)
  regex        - query-token regex match over repo source files
  text_search  - token-overlap ranking over file contents
Records per-query rows (query_id, category, method, hit_in_top5, rank,
hops_measured, cost_nodes_examined); computes per-method x per-category
aggregates and the guidance section (SC-006). Hard-query definitions are
audited via hops_measured (>=2 expected, research R10). Invalid rows skip
with KG-EVAL-SUITE-INVALID (never fatal).
"""
import json
import os
import re

from src.kg import report as kg_report
from src.kg import retrieval as kg_retrieval
from src.kg import similarity as kg_similarity

_repo_root = "."

DEFAULT_TOP_K = 5


def _load_suite(suite_path):
    with open(suite_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _regex_search_files(query, regex_roots):
    """Rank repo files by regex hit count of query tokens (traditional baseline)."""
    tokens = sorted(set(kg_similarity.tokenize(query)))
    if not tokens:
        return [], 0  # keep the (ranked, cost) tuple shape (review F1)
    pattern = re.compile("|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)))
    scores = {}
    for root in regex_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in
                           {"__pycache__", ".git", ".venv", "node_modules"}]
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, ".").replace("\\", "/").lower()
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                except OSError:
                    continue
                hits = len(pattern.findall(content))
                if hits:
                    scores[rel] = scores.get(rel, 0) + hits
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"f:{rel}" for rel, _ in ranked], sum(1 for _ in ranked)


def _text_search_files(query, files_contents):
    """Rank file contents by token overlap (traditional baseline)."""
    q_tokens = kg_similarity.tokenize(query)
    scored = []
    cost = 0
    for rel, content in sorted(files_contents.items()):
        cost += 1
        f_tokens = kg_similarity.tokenize(content)
        sim = kg_similarity.similarity(q_tokens, f_tokens)
        if sim > 0:
            scored.append((rel, sim))
    scored.sort(key=lambda kv: (-kv[1], kv[0]))
    return [f"f:{rel}" for rel, _ in scored], cost


def _target_hops_from_anchors(snapshot, expected_node_id, anchors):
    """BFS distance from anchor nodes to the expected target (hop audit).

    Returns None only when the target is unreachable from all anchors.
    """
    from collections import deque

    active_ids = {n.id for n in snapshot.nodes if n.status == "active"}
    adj = {}
    for e in snapshot.edges:
        if e.src in active_ids and e.dst in active_ids and e.weight > 0:
            adj.setdefault(e.src, set()).add(e.dst)
            adj.setdefault(e.dst, set()).add(e.src)
    start = [a["node_id"] for a in anchors] or []
    hops = {s: 0 for s in start}
    queue = deque((s, 0) for s in start)
    while queue:
        node, d = queue.popleft()
        if node == expected_node_id:
            return d
        if d >= 6:
            continue
        for nxt in sorted(adj.get(node, ())):
            if nxt not in hops:
                hops[nxt] = d + 1
                queue.append((nxt, d + 1))
    return hops.get(expected_node_id)


def run_evaluation(snapshot, suite_path, regex_roots=None, top_k=DEFAULT_TOP_K):
    """Execute the suite; returns the report dict (report file schema)."""
    suite = _load_suite(suite_path)
    regex_roots = regex_roots or [_repo_root]

    # text-search corpus: active file node paths -> file contents
    files_contents = {}
    for n in snapshot.nodes:
        if n.kind == "file" and n.status == "active" and not n.id.startswith("f:external/"):
            path = os.path.join(_repo_root, n.path)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    files_contents[n.path] = fh.read()
            except OSError:
                continue

    active_ids = {n.id for n in snapshot.nodes if n.status == "active"}
    per_query = []
    invalid = 0
    for q in suite.get("queries", []):
        expected = q.get("expected_node_id", "")
        if expected not in active_ids:
            kg_report.emit_failure(
                kg_report.KG_EVAL_SUITE_INVALID,
                f"{q.get('id')}: expected_node_id {expected!r} not in active graph; skipping",
            )
            invalid += 1
            continue

        # method: graph
        g_res = kg_retrieval.retrieve(snapshot, q["query"], top_k=top_k)
        g_ids = [r["node_id"] for r in g_res["results"]]
        g_rank = g_ids.index(expected) + 1 if expected in g_ids else None
        # measured hop distance for hard-query audit: target hops if retrieved,
        # else BFS distance from anchors to the expected node (research R10)
        hops = None
        for r in g_res["results"]:
            if r["node_id"] == expected:
                hops = r["hops_from_anchor"]
                break
        if hops is None:
            hops = _target_hops_from_anchors(snapshot, expected, g_res["anchors"])
        per_query.append({
            "query_id": q["id"], "category": q["category"], "method": "graph",
            "hit_in_top5": g_rank is not None and g_rank <= top_k,
            "rank": g_rank, "hops_measured": hops,
            "cost_nodes_examined": len(snapshot.nodes),
        })

        # method: regex
        regex_ranked, regex_cost = _regex_search_files(q["query"], regex_roots)
        s_path = expected.split("#", 1)[0][2:] if expected.startswith("s:") else expected[2:]
        r_rank = None
        for i, node_id in enumerate(regex_ranked, 1):
            if node_id[2:] == s_path:
                r_rank = i
                break
        per_query.append({
            "query_id": q["id"], "category": q["category"], "method": "regex",
            "hit_in_top5": r_rank is not None and r_rank <= top_k,
            "rank": r_rank, "hops_measured": None,
            "cost_nodes_examined": regex_cost,
        })

        # method: text_search
        ts_ranked, ts_cost = _text_search_files(q["query"], files_contents)
        t_rank = None
        for i, node_id in enumerate(ts_ranked, 1):
            if node_id[2:] == s_path:
                t_rank = i
                break
        per_query.append({
            "query_id": q["id"], "category": q["category"], "method": "text_search",
            "hit_in_top5": t_rank is not None and t_rank <= top_k,
            "rank": t_rank, "hops_measured": None,
            "cost_nodes_examined": ts_cost,
        })

    return {
        "suite_version": suite.get("suite_version", ""),
        "graph_commit_sha": snapshot.commit_sha,
        "per_query": per_query,
        "aggregate": _aggregate(per_query),
        "guidance": _guidance(_aggregate(per_query), hops_audit=per_query),
        "invalid_queries": invalid,
    }


def _aggregate(per_query):
    agg = {}
    for row in per_query:
        key = (row["method"], row["category"])
        cur = agg.setdefault(key, {"hits": 0, "total": 0, "cost": 0.0})
        cur["hits"] += 1 if row["hit_in_top5"] else 0
        cur["total"] += 1
        cur["cost"] += row["cost_nodes_examined"] or 0
    out = {}
    for (method, category), v in sorted(agg.items()):
        out.setdefault(method, {})[category] = {
            "hit_rate": round(v["hits"] / v["total"], 4) if v["total"] else 0.0,
            "total": v["total"],
            "mean_cost": round(v["cost"] / v["total"], 1) if v["total"] else 0.0,
        }
    return out


def _guidance(aggregate, hops_audit=None):
    """Recorded per-category verdicts (SC-006); hard-query hop audit summarized."""
    verdicts = {}
    methods = set(aggregate.keys())
    for category in ("easy", "hard"):
        rated = {m: aggregate.get(m, {}).get(category, {}).get("hit_rate", 0.0)
                 for m in methods}
        best = max(rated.items(), key=lambda kv: (kv[1], kv[0]))
        verdicts[category] = {
            "preferred_method": best[0] if best[1] > 0 else "none",
            "hit_rates": rated,
        }
    hard_hops = [r["hops_measured"] for r in (hops_audit or [])
                 if r["method"] == "graph" and r["category"] == "hard"
                 and r["hops_measured"] is not None]
    verdicts["hard_query_hop_audit"] = {
        "min_hops_measured": min(hard_hops) if hard_hops else None,
        "mean_hops_measured": round(sum(hard_hops) / len(hard_hops), 2) if hard_hops else None,
        "note": "hard queries expected >=2 hops from anchor (research R10); measured, not claimed",
    }
    return verdicts
