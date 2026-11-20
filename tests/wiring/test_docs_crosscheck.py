"""Task T045/T047 acceptance for feature 005: documentation cross-checks.

Verifies docs/INDEX entries exist, map pointer outside markers, and
docs/SKILLBOX_OVERVIEW.md operational note present (T047 deliverable,
determinate condition).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_index_has_map_entry():
    idx = (REPO_ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
    assert "AI_SDLC_LIFECYCLE_MAP.md" in idx


def test_index_has_protocol_doc_entry():
    idx = (REPO_ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
    assert "WIRING_HOOK_TELEMETRY.md" in idx


def test_map_pointer_present_outside_markers():
    t = (REPO_ROOT / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
    assert "AI_SDLC_LIFECYCLE_MAP.md" in t
    # must sit outside the SKILLBOX markers
    begin = t.find("SKILLBOX")
    if begin >= 0:
        # find the pointer position and the BEGIN/END markers
        ptr = t.find("AI_SDLC_LIFECYCLE_MAP.md")
        begin_pos = t.find("SKILLBOX")
        end_pos = t.find("SKILLBOX:END", begin_pos) if "SKILLBOX:END" in t else begin_pos
        assert not (begin_pos < ptr < end_pos), "map pointer must sit outside the SKILLBOX injection block"


def test_complementary_doc_links_map():
    doc = (REPO_ROOT / "docs" / "SPECKIT_EGM_COMPLEMENTARY_MODE.md").read_text(encoding="utf-8")
    assert "AI_SDLC_LIFECYCLE_MAP.md" in doc


def test_skillbox_overview_documents_capture_path():
    doc = (REPO_ROOT / "docs" / "SKILLBOX_OVERVIEW.md").read_text(encoding="utf-8")
    assert "wiring_capture_enabled" in doc or "wiring sweep" in doc.lower() or "wiring_sweep" in doc


def test_protocol_doc_ships():
    doc = (REPO_ROOT / "docs" / "WIRING_HOOK_TELEMETRY.md")
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    assert "record_hook_acceptance" in text
    assert "trigger" in text.lower()
