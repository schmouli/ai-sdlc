"""Idempotence + rendering tests for scripts/generate_lifecycle_map.py (SC-002).

Per-file fixtures: renders in-process from the live manifest/extension dicts;
never writes the repo's committed map (--check runs against a temp copy).
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_lifecycle_map as glm  # noqa: E402


@pytest.fixture()
def sources():
    ext_text = (REPO_ROOT / ".specify" / "extensions.yml").read_text(encoding="utf-8")
    man_text = (REPO_ROOT / ".specify" / "wiring-manifest.yml").read_text(encoding="utf-8")
    ext = yaml.safe_load(ext_text)
    man = yaml.safe_load(man_text)
    return ext, man, ext_text, man_text


def test_render_deterministic(sources):
    ext, man, ext_t, man_t = sources
    assert glm.render_map(ext, man, ext_t, man_t) == glm.render_map(ext, man, ext_t, man_t)


def test_render_changes_when_manifest_changes(sources):
    ext, man, ext_t, man_t = sources
    before = glm.render_map(ext, man, ext_t, man_t)
    man2 = copy.deepcopy(man)
    # flip the FIRST non-complete phase (whichever it currently is) to force a change
    target = next((k for k, v in man2["phase_status"].items() if v != "complete"), None)
    man2["phase_status"][target] = "complete"
    assert glm.render_map(ext, man2, ext_t, man_t) != before


def test_boundary_sentences_rendered_verbatim(sources):
    ext, man, ext_t, man_t = sources
    out = glm.render_map(ext, man, ext_t, man_t)
    enabled = ext.get("hooks") or {}
    enabled_names = {
        event
        for event, entries in enabled.items()
        if any((entry or {}).get("enabled") is not False for entry in entries or [])
    }
    for event, st in (man.get("stages") or {}).items():
        sentence = (st or {}).get("boundary_sentence")
        if sentence and event in enabled_names:
            assert sentence in out, f"{event}: boundary sentence not rendered verbatim"


def test_disabled_stage_block_present(sources):
    ext, man, ext_t, man_t = sources
    out = glm.render_map(ext, man, ext_t, man_t)
    assert "Disabled stages" in out
    assert "before_specify" in out
    assert "roster-exempt" in out.lower()


def test_provenance_lines_present(sources):
    ext, man, ext_t, man_t = sources
    out = glm.render_map(ext, man, ext_t, man_t)
    assert ".specify/extensions.yml (sha256:" in out
    assert ".specify/wiring-manifest.yml (sha256:" in out


def test_consult_cell_global_matrix_format(sources):
    ext, man, ext_t, man_t = sources
    out = glm.render_map(ext, man, ext_t, man_t)
    consult = man.get("consult_matrix") or {}
    digest_ct = sum(1 for v in consult.values() if v == "digest")
    none_ct = sum(1 for v in consult.values() if v == "none")
    assert f"{digest_ct} digest / {none_ct} none" in out


def test_drift_row_consult_cell_is_main_context(sources):
    """Zero-reviewer profiles (DRIFT, count == 0) render '0 (main-context)' in the
    consult-summary cell instead of the global digest/none counts (map render spec)."""
    ext, man, ext_t, man_t = sources
    out = glm.render_map(ext, man, ext_t, man_t)
    drift_rows = [
        line for line in out.splitlines()
        if line.startswith("| `after_implement` |")
    ]
    assert drift_rows, "after_implement (DRIFT) row must render in the stage table"
    cell = drift_rows[0].rstrip().rstrip("|").split("|")[-1].strip()
    assert cell == "0 (main-context)", f"DRIFT consult cell: {cell!r}"


def test_no_wiring_adopted_at_in_rendered_map(sources):
    """SC-008: `wiring_adopted_at` (adoption marker) must not appear in any rendered
    map output — it is activation state, not wiring documentation."""
    ext, man, ext_t, man_t = sources
    assert "wiring_adopted_at" not in glm.render_map(ext, man, ext_t, man_t)
    # and a manifest carrying a NON-null marker must also stay out of the render
    man2 = copy.deepcopy(man)
    man2["wiring_adopted_at"] = "2026-11-11T00:00:00-05:00"
    assert "wiring_adopted_at" not in glm.render_map(ext, man2, ext_t, man_t)


def test_phase_status_block_rendered(sources):
    ext, man, ext_t, man_t = sources
    out = glm.render_map(ext, man, ext_t, man_t)
    for key, info in (man.get("phases") or {}).items():
        assert (info or {}).get("title", "") in out
        assert (info or {}).get("invariant", "") in out


def test_byte_idempotence_and_check_exit_codes(sources, tmp_path, monkeypatch):
    """SC-002: two renders are identical; --check exits 0 on match, 2 on drift."""
    ext, man, ext_t, man_t = sources
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    spec_dir = tmp_path / ".specify"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "extensions.yml").write_text(ext_t, encoding="utf-8")
    (spec_dir / "wiring-manifest.yml").write_text(man_t, encoding="utf-8")
    target = docs / "AI_SDLC_LIFECYCLE_MAP.md"

    first = glm.render_map(ext, man, ext_t, man_t)
    target.write_text(first, encoding="utf-8", newline="\n")

    second = glm.render_map(ext, man, ext_t, man_t)
    assert first == second  # determinism

    # --check via module main against temp tree
    saved = (glm.MAP_PATH, glm.EXTENSIONS_PATH, glm.MANIFEST_PATH)
    glm.MAP_PATH = target
    glm.EXTENSIONS_PATH = spec_dir / "extensions.yml"
    glm.MANIFEST_PATH = spec_dir / "wiring-manifest.yml"
    argv_backup = sys.argv
    try:
        sys.argv = ["generate_lifecycle_map.py", "--check"]
        assert glm.main() == 0  # match
        target.write_text(first + "<!-- hand edit -->\n", encoding="utf-8", newline="\n")
        assert glm.main() == 2  # divergence
    finally:
        sys.argv = argv_backup
        glm.MAP_PATH, glm.EXTENSIONS_PATH, glm.MANIFEST_PATH = saved
