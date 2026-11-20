"""Two-step retrieval pipeline (FR-008, research R6/R7).

Step 1: anchor selection via lexical similarity (similarity.py).
Step 2: traversal expansion from anchors with importance ranking
        score = pagerank × 0.7 + anchor_sim × 0.3 (weights per research R6),
        results carry measured hops_from_anchor (BFS distance, 0 = anchor).
Deterministic: sorted iteration + lexicographic tie-breaks (FR-009).
Retrieval filters to active-status nodes only (FR-013 contract).
"""
from collections import deque

ANCHOR_WEIGHT = 0.3
PAGERANK_WEIGHT = 0.7


def _adjacency(edges, active_ids, derived_pairs=None):
    """Traversal adjacency: stored edges + derived structural pairs.

    Derived pairs come from id prefixes and qualified names per the data
    model (no separate stored relations): file<->symbol ownership plus
    method<->defining-class membership - the traversal substrate consumes
    those derivations so an anchor on a file or method reaches its
    structural neighbors.
    """
    adj = {}
    for e in edges:
        if e.src in active_ids and e.dst in active_ids and e.weight > 0:
            adj.setdefault(e.src, set()).add(e.dst)
            adj.setdefault(e.dst, set()).add(e.src)
    for a, b in (derived_pairs or ()):
        if a in active_ids and b in active_ids:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
    return adj


def derived_class_membership(active_nodes):
    """[(member_id, class_id)] derived from qualified-name prefixes.

    A method `Class.method` (or nested `Class.Inner.method`) is structurally
    bound to its defining class; retrieval expands that relation the same way
    file->symbol ownership is expanded, so anchoring a member surfaces its
    class (and vice versa) at hop distance 0-1. Both endpoints must exist as
    active nodes.
    """
    pairs = []
    by_id = {n.id for n in active_nodes}
    for n in active_nodes:
        if n.kind != "symbol" or "." not in n.qualified_name:
            continue
        # walk progressively shorter prefixes: Inner.method -> Class.method
        parts = n.qualified_name.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            parent_qualified = ".".join(parts[:cut])
            parent_id = "s:" + n.path + "#" + parent_qualified
            if parent_id in by_id:
                pairs.append((n.id, parent_id))
                break
    return pairs


def derived_structural_pairs(active_nodes):
    """Union of derived relations: file<->symbol ownership + method<->class."""
    pairs = []
    for n in active_nodes:
        if n.kind == "symbol":
            pairs.append(("f:" + n.path, n.id))
    pairs.extend(derived_class_membership(active_nodes))
    return pairs


def _bfs_hops(adj, anchors, max_hops=4):
    """Multi-source BFS: {node_id: min hops from any anchor}."""
    hops = {a: 0 for a in anchors}
    queue = deque((a, 0) for a in anchors)
    while queue:
        node, d = queue.popleft()
        if d >= max_hops:
            continue
        for nxt in sorted(adj.get(node, ())):
            if nxt not in hops:
                hops[nxt] = d + 1
                queue.append((nxt, d + 1))
    return hops


def _symmetrized_edges(edges):
    """PageRank input: import/call directed; co-edit symmetrized (research R7)."""
    out = []
    for e in edges:
        if e.type == "coedit":
            out.append((e.src, e.dst, e.weight))
            out.append((e.dst, e.src, e.weight))
        else:
            out.append((e.src, e.dst, e.weight))
    return out


def retrieve(snapshot, query, top_k=5, anchors_requested=3, min_anchor_sim=0.0):
    """Run the two-step pipeline against a loaded GraphSnapshot.

    Returns dict per cli-contract: anchors, no_confident_match flag, results
    [{node_id, score, hops_from_anchor, anchor_id}] sorted desc by score with
    lexicographic tie-break.
    """
    from src.kg import pagerank as kg_pagerank
    from src.kg import similarity as kg_similarity

    active = [n for n in snapshot.nodes
              if n.status == "active" and not n.id.startswith("f:external/")]
    # external module nodes are traversal context only (data-model Edge table:
    # confidence 'external' kept for context, excluded from stats) - excluding
    # them from RANKED RESULTS also removes builtin-import hub noise
    # (f:external/os, pathlib) that otherwise outranks real targets.
    active_ids = {n.id for n in active}

    q_tokens = kg_similarity.query_tokens(query)
    anchor_pairs = kg_similarity.select_anchors(
        query, [(n.id, n.display_text) for n in active],
        top_k=anchors_requested, min_anchor_sim=min_anchor_sim,
    )
    if not anchor_pairs:
        return {"anchors": [], "no_confident_match": True, "results": []}

    pr_scores = kg_pagerank.compute_pagerank(
        sorted(active_ids), _symmetrized_edges(snapshot.edges)
    )
    adj = _adjacency(snapshot.edges, active_ids, derived_structural_pairs(active))
    anchor_ids = [a for a, _ in anchor_pairs]
    hops = _bfs_hops(adj, anchor_ids)
    anchor_sim = dict(anchor_pairs)
    max_sim_in_set = max(anchor_sim.values())
    node_by_id = {n.id: n for n in active}

    # Implement-time refinement of the R6 weighting (evidence-driven:
    # PageRank over 2.4k nodes is near-uniform - 77% of nodes share one bin -
    # so an anchor-only similarity component left traversal discoveries ranked
    # by the bare carry floor, tie-broken lexicographically (relevance-blind).
    # The similarity component now scores EVERY traversed node by its OWN
    # similarity to the query (normalized against the best anchor), with the
    # carry floor applying only to zero-overlap traversal discoveries; the
    # PageRank component keeps its documented 0.7 weight.
    HOP_DECAY = 0.5
    scored_nodes = []
    for node_id in sorted(pr_scores):
        if node_id not in hops or node_id not in active_ids:
            continue
        sim_component = anchor_sim.get(node_id, 0.0)
        if sim_component == 0.0:
            node = node_by_id.get(node_id)
            if node is not None:
                sim_component = kg_similarity.similarity(q_tokens, kg_similarity.tokenize(node.display_text))
        sim_component = sim_component / max_sim_in_set if max_sim_in_set else 0.0
        if sim_component == 0.0:
            sim_component = HOP_DECAY ** hops.get(node_id, 4)
        scored_nodes.append((node_id, sim_component))

    results = []
    for node_id, sim_component in scored_nodes:
        score = PAGERANK_WEIGHT * pr_scores.get(node_id, 0.0) + ANCHOR_WEIGHT * sim_component
        if score <= 0:
            continue
        results.append({
            "node_id": node_id,
            "score": round(score, 6),
            "hops_from_anchor": hops.get(node_id, -1),
            "anchor_id": anchor_ids[0],
        })
    results.sort(key=lambda r: (-r["score"], r["node_id"]))
    results = results[:top_k]

    return {
        "anchors": [{"node_id": a, "similarity": round(s, 6)} for a, s in anchor_pairs],
        "no_confident_match": False,
        "results": results,
    }
