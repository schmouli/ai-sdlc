"""Deterministic lexical anchor selection (research R6).

Token-overlap cosine on binary token vectors: score = |q∩n| / sqrt(|q|·|n|).
Tokenization splits snake_case AND camelCase identifiers (e.g.
AdaptiveThresholdController -> adaptive + threshold + controller), keeping the
full identifier as an extra token; this makes class-name queries match their
targets. Ties break lexicographically by node id (FR-009 determinism).
`min_anchor_sim` floor yields the documented no-confident-match sentinel
rather than an arbitrary node (spec Edge Case, zero lexical overlap).
"""
import re

_CAMEL_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z0-9])|[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])"
)
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")

# English question/auxiliary words that carry no code semantics; filtered from
# QUERY text so "where do rejected signals get archived" anchors on
# rejected/signal/archive rather than generic do_GET/get_* identifiers
# (implement-time refinement of R6 "normalized text matching").
QUERY_STOPWORDS = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "is", "are", "was",
    "were", "be", "been", "being", "what", "which", "who", "whom", "where",
    "when", "why", "how", "do", "does", "did", "can", "could", "should",
    "would", "will", "shall", "may", "might", "and", "or", "of", "in", "on",
    "for", "to", "from", "by", "with", "at", "as", "it", "its", "into",
    "per", "via", "after", "before", "between",
})

_SUFFIXES = ("ing", "ies", "ed", "es", "s")


def _normalize_token(tok):
    """Light suffix stemming for >4-char tokens: archived->archive, regimes->regime."""
    if len(tok) <= 4:
        return tok
    for suf in _SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


def query_tokens(text):
    """Query-side tokens: stopword-filtered + suffix-normalized."""
    base = tokenize(text)
    filtered = {t for t in base if t not in QUERY_STOPWORDS}
    normalized = set()
    for t in filtered:
        normalized.add(t)
        n = _normalize_token(t)
        if n != t:
            normalized.add(n)
    return normalized


def tokenize(text):
    """Lowercase identifier/path token extraction.

    Splits snake_case and camelCase before lowering (case boundaries carry
    the split information): a query of 'adaptive threshold controller'
    overlaps 'AdaptiveThresholdController' through its subtokens while the
    full identifier remains an extra matching token.
    """
    tokens = set()
    for raw in _TOKEN_RE.findall(text or ""):
        tokens.add(raw.lower())
        for part in _CAMEL_RE.findall(raw):
            if part:
                tokens.add(part.lower())
        if "_" in raw:
            tokens.update(p.lower() for p in raw.split("_") if p)
    return tokens


def similarity(query_tokens, node_tokens):
    """Cosine over binary token sets, deterministic.

    BOTH sides are stem-expanded: each token contributes itself plus its
    light-stem form, so 'archived' (stem 'archiv') matches 'archive' (whose
    stem is also 'archiv' after the -e/ed asymmetry fix) without inflating
    either side's denominator with unrelated expansions.
    """
    if not query_tokens or not node_tokens:
        return 0.0
    q_expanded = set()
    for t in query_tokens:
        q_expanded.add(t)
        q_expanded.add(_normalize_token(t))
    n_expanded = set()
    for t in node_tokens:
        n_expanded.add(t)
        n_expanded.add(_normalize_token(t))
    overlap = len(q_expanded & n_expanded)
    if overlap == 0:
        return 0.0
    return overlap / math_sqrt(len(q_expanded) * len(n_expanded))


def math_sqrt(x):
    return x ** 0.5


def select_anchors(query, nodes, top_k=3, min_anchor_sim=0.0):
    """Return [(node_id, similarity)] sorted desc, ties by lexicographic id.

    nodes: iterable of objects with .id and .display_text (or (id, text)
    tuples). Query tokenization applies stopword filtering + light suffix
    normalization (query_tokens); node text uses raw tokenization. Results
    below min_anchor_sim are excluded; an empty result is the documented
    no-confident-match sentinel.
    """
    q_tokens = query_tokens(query)
    scored = []
    for node in nodes:
        node_id, text = (node if isinstance(node, tuple) else (node.id, node.display_text))
        sim = similarity(q_tokens, tokenize(text))
        if sim > 0 and sim >= min_anchor_sim:
            scored.append((node_id, sim))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[:top_k]
