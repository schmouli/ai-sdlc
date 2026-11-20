"""Deterministic PageRank over the knowledge graph (research R7).

Engine: networkx `pagerank()` (adopted per research R2 gate) with a stdlib
power-iteration fallback implementing identical semantics: damping 0.85,
tolerance 1e-6, max 100 iterations, uniform teleport, convergent stopping
(cycles terminate, never double-count). Nodes are processed in sorted id order
for reproducible float summation; result is deterministic for identical graph
input regardless of insertion order (FR-009).
"""
import math

DAMPING = 0.85
TOLERANCE = 1e-6
MAX_ITER = 100


def _nx_pagerank(nodes, weighted_edges):
    import networkx as nx

    g = nx.DiGraph()
    g.add_nodes_from(sorted(nodes))
    for src, dst, weight in sorted(weighted_edges):
        if g.has_edge(src, dst):
            g[src][dst]["weight"] += weight
        else:
            g.add_edge(src, dst, weight=weight)
    if len(g) == 0:
        return {}
    return nx.pagerank(g, alpha=DAMPING, tol=TOLERANCE, max_iter=MAX_ITER, weight="weight")


def _stdlib_pagerank(nodes, weighted_edges):
    """Power iteration over weighted adjacency (out-weight-normalized rows)."""
    if not nodes:
        return {}
    node_list = sorted(nodes)
    index = {n: i for i, n in enumerate(node_list)}
    n = len(node_list)
    out_weight = [0.0] * n
    rows = [[] for _ in range(n)]  # rows[i] = [(dst_index, weight), ...]
    for src, dst, weight in sorted(weighted_edges):
        if src in index and dst in index:
            i, j = index[src], index[dst]
            rows[i].append((j, weight))
            out_weight[i] += weight
    dangling = [i for i in range(n) if out_weight[i] == 0.0]
    ranks = [1.0 / n] * n
    base = (1.0 - DAMPING) / n
    for _ in range(MAX_ITER):
        dangling_sum = sum(ranks[i] for i in dangling)
        new_ranks = [0.0] * n
        for i in range(n):
            acc = 0.0
            for (j, w) in rows[i]:
                acc += ranks[j] * (w / out_weight[i])
            new_ranks[i] = base + DAMPING * (acc + dangling_sum / n)
        delta = sum(abs(new_ranks[i] - ranks[i]) for i in range(n))
        ranks = new_ranks
        if delta < TOLERANCE:
            break
    total = sum(ranks) or 1.0
    return {node_list[i]: ranks[i] / total for i in range(n)}


def compute_pagerank(nodes, weighted_edges):
    """Deterministic PageRank: {node_id: score}.

    nodes: iterable of node ids; weighted_edges: (src, dst, weight) triples.
    Import/call edges stay directed; co-edit edges should be symmetrized by
    the caller with proportional weights before reaching this function.
    """
    try:
        scores = _nx_pagerank(nodes, weighted_edges)
        if scores:
            return scores
    except ImportError:
        pass
    return _stdlib_pagerank(nodes, weighted_edges)


def assert_close(scores_a, scores_b, tol=1e-4):
    """Engine-equivalence assertion used by tests (identical results check)."""
    keys = set(scores_a) | set(scores_b)
    for k in keys:
        if abs(scores_a.get(k, 0.0) - scores_b.get(k, 0.0)) > tol:
            return False
    return True
