"""Code Knowledge Graph (kg) package - feature 003-code-knowledge-graph.

Builds and maintains a graph of the repository's Python source: file and symbol
nodes connected by import, call, and co-edit edges (co-edits derived from git
history). Provides deterministic two-step retrieval (lexical anchor search +
PageRank traversal), incremental per-commit updates with stable node identity,
and an evaluation harness comparing graph retrieval against regex/text-search
baselines.

Design contracts: specs/003-code-knowledge-graph/ (plan.md, data-model.md,
contracts/*.md, research.md). This package is standalone, read-only advisory
tooling: it never modifies trading configuration, EA files, registry, models,
or trading state (spec FR-018, constitution Principle II/VII).

Feature 005 addition: ``digest`` renders the review-facing digest artifact
(contract: specs/005-ai-sdlc-wiring/contracts/digest-artifact.md).
"""

from src.kg import coedit, evaluation, graph_builder, models, pagerank, parser, report, retrieval, similarity  # noqa: F401
from src.kg import digest  # noqa: F401
from src.kg.digest import render_digest  # noqa: F401

__all__ = [
    "models",
    "parser",
    "coedit",
    "graph_builder",
    "similarity",
    "pagerank",
    "retrieval",
    "evaluation",
    "report",
    "digest",
    "render_digest",
]
