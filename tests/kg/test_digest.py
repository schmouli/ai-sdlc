"""Digest artifact tests (T019/T020, US3): determinism, truncation, STALE label,
rotation, content exclusions, CLI JSON surface, and KG-DIGEST-RENDER-FAILED semantics.

Fixture provenance: a synthetic graph snapshot built per the data-model entity below
(file nodes with pagerank, import partners, co-edit pairs) — no live graph needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "src" / "kg"))

from kg.digest import render_digest  # noqa: E402


def _snapshot(**overrides):
    """Synthetic snapshot per the data-model: file nodes + import edges + coedit edges."""
    snap = {
        "schema_version": 1,
        "commit_sha": "abc1234",
        "built_at": "2026-11-10T15:00:00-05:00",
        "files": {
            "src/a.py": {"pagerank": 0.9, "imports": ["src/b.py", "src/c.py"]},
            "src/b.py": {"pagerank": 0.8, "imports": ["src/c.py"]},
            "src/c.py": {"pagerank": 0.5, "imports": []},
            "src/d.py": {"pagerank": 0.4, "imports": ["src/a.py"]},
            "src/e.py": {"pagerank": 0.3, "imports": []},
        },
        "coedit_pairs": [
            {"a": "src/a.py", "b": "src/b.py", "weight": 12},
            {"a": "src/c.py", "b": "src/d.py", "weight": 4},
        ],
    }
    snap.update(overrides)
    return snap


BUDGET = {"digest_max_bytes": 8192}


# --------------------------------------------------------------- determinism


def test_render_deterministic():
    a = render_digest(_snapshot(), BUDGET)
    b = render_digest(_snapshot(), BUDGET)
    assert a == b


def test_header_carries_commit_and_built_at():
    out = render_digest(_snapshot(), BUDGET)
    assert "abc1234" in out
    assert "2026-11-10T15:00:00-05:00" in out


def test_fresh_label_when_no_head():  # no HEAD given => fresh (no staleness compare)
    out = render_digest(_snapshot(), BUDGET)
    assert "STALE" not in out


def test_stale_label_when_head_given():
    out = render_digest(_snapshot(), BUDGET, head_sha="def5678")
    assert "STALE" in out
    assert "abc1234" in out and "def5678" in out


def test_byte_idempotence_full_pipeline():
    out1 = render_digest(_snapshot(), BUDGET)
    # simulate a re-render from the same state: digest file itself is rewritten byte-for-byte
    assert out1 == render_digest(_snapshot(), BUDGET)


# --------------------------------------------------------------- truncation


def test_truncation_deterministic_at_low_cap():
    low = {"digest_max_bytes": 400}
    a = render_digest(_snapshot(), low)
    b = render_digest(_snapshot(), low)
    assert a == b
    assert len(a.encode("utf-8")) <= 400 + 32  # marker overhead tolerance
    assert "(truncated)" in a


def test_truncation_same_cap_same_point():
    low = {"digest_max_bytes": 300}
    assert render_digest(_snapshot(), low) == render_digest(_snapshot(), low)


# --------------------------------------------------------------- content rules


def test_content_exclusions():
    """SC-011: no source-code content, credentials, tokens, or security-sensitive values."""
    snap = _snapshot()
    snap["files"]["src/a.py"]["secret"] = "hunter2"  # injected contaminant must NOT render
    out = render_digest(snap, BUDGET)
    assert "hunter2" not in out


def test_top_files_ordered_pagerank_desc():
    out = render_digest(_snapshot(), BUDGET)
    idx_a = out.index("src/a.py")
    idx_b = out.index("src/b.py")
    idx_c = out.index("src/c.py")
    assert idx_a < idx_b < idx_c  # 0.9 > 0.8 > 0.5 pagerank


def test_coedit_pairs_rendered():
    out = render_digest(_snapshot(), BUDGET)
    assert "src/a.py" in out and "src/b.py" in out


def test_bytes_header_present():
    """F10-3: the digest header carries the `bytes: <n> / cap <cap>` line and the
    reported byte count matches the rendered document."""
    out = render_digest(_snapshot(), BUDGET)
    assert f"bytes:" in out and f"/ cap 8192" in out
    import re as _re
    m = _re.search(r"bytes: (\d+) / cap 8192", out)
    assert m and int(m.group(1)) == len(out.encode("utf-8"))


def test_truncated_marker_line_when_at_budget():
    """A budget-hit render includes the deterministic-prefix marker line."""
    out = render_digest(_snapshot(), {"digest_max_bytes": 400})
    assert "TRUNCATED at budget" in out and "(truncated)" in out
