"""Human-readable reporting + failure reason codes for the knowledge graph.

Reason codes are owned by the authoritative registry in
specs/003-code-knowledge-graph/contracts/failure-contract.md (12 codes plus the
feature-005 additive `KG-DIGEST-RENDER-FAILED`).
Every skip/abort carries exactly one code plus one contextual detail string
(rule 2) and is recorded to the durable log (FR-015, Constitution V).

Log wiring follows the repo convention: ensure_file_handler from
src/log_config/logger_config.py, targeting logs/kg_builder.log.
"""
import logging

from src.log_config.logger_config import ensure_file_handler

LOGGER_NAME = "kg_builder"
LOG_FILE = "logs/kg_builder.log"

# Authoritative registry: contracts/failure-contract.md (v1).
KG_PARSE_SYNTAX = "KG-PARSE-SYNTAX"
KG_PARSE_ENCODING = "KG-PARSE-ENCODING"
KG_PARSE_EMPTY = "KG-PARSE-EMPTY"
KG_GIT_UNAVAILABLE = "KG-GIT-UNAVAILABLE"
KG_GIT_SHALLOW = "KG-GIT-SHALLOW"
KG_SNAPSHOT_MISSING = "KG-SNAPSHOT-MISSING"
KG_SNAPSHOT_SCHEMA = "KG-SNAPSHOT-SCHEMA"
KG_SNAPSHOT_WRITE_FAIL = "KG-SNAPSHOT-WRITE-FAIL"
KG_STATE_MISMATCH = "KG-STATE-MISMATCH"
KG_LIB_UNAVAILABLE = "KG-LIB-UNAVAILABLE"
KG_EVAL_SUITE_INVALID = "KG-EVAL-SUITE-INVALID"
KG_EVAL_NO_GRAPH = "KG-EVAL-NO-GRAPH"
KG_DIGEST_RENDER_FAILED = "KG-DIGEST-RENDER-FAILED"

ALL_REASON_CODES = frozenset({
    KG_PARSE_SYNTAX,
    KG_PARSE_ENCODING,
    KG_PARSE_EMPTY,
    KG_GIT_UNAVAILABLE,
    KG_GIT_SHALLOW,
    KG_SNAPSHOT_MISSING,
    KG_SNAPSHOT_SCHEMA,
    KG_SNAPSHOT_WRITE_FAIL,
    KG_STATE_MISMATCH,
    KG_LIB_UNAVAILABLE,
    KG_EVAL_SUITE_INVALID,
    KG_EVAL_NO_GRAPH,
    KG_DIGEST_RENDER_FAILED,
})

# Fail-soft boundary per failure-contract.md rule 3: never abort the run.
FAIL_SOFT_CODES = frozenset({
    KG_PARSE_SYNTAX,
    KG_PARSE_ENCODING,
    KG_PARSE_EMPTY,
    KG_GIT_UNAVAILABLE,
    KG_GIT_SHALLOW,
})


def get_kg_logger():
    """Wire the kg_builder logger to the durable log file."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        ensure_file_handler(LOGGER_NAME, LOG_FILE)
    return logger


def emit_failure(code, detail):
    """Record a failure with a reason code + context.

    Returns a structured record for command stdout `failures[]` payloads
    while guaranteeing the durable log carries the same information.
    """
    logger = get_kg_logger()
    logger.warning("%s: %s", code, detail)
    return {"reason_code": code, "detail": detail}


def summarize(result):
    """One-line, human-readable summary routed to stderr/log (FR-015)."""
    logger = get_kg_logger()
    logger.info("%s", result)
    return result
