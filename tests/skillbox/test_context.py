"""Tests for SkillBox context detection (US4 — research R8 marker heuristics)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.skillbox.context import detect_context


class TestMarkers:
    def test_requirements_python(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("pandas\n", encoding="utf-8")
        ctx = detect_context(tmp_path)
        assert ctx["language"] == ["python"]
        assert ctx["detection_confidence"] == "marker-exact"

    def test_framework_sub_detection(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
        ctx = detect_context(tmp_path)
        assert "fastapi" in ctx["framework"]

    def test_pyproject_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        ctx = detect_context(tmp_path)
        assert "python" in ctx["language"]

    def test_pom_java(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        ctx = detect_context(tmp_path)
        assert "java" in ctx["language"]

    def test_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        ctx = detect_context(tmp_path)
        assert "go" in ctx["language"]

    def test_csproj_csharp(self, tmp_path):
        (tmp_path / "App.csproj").write_text("<Project/>", encoding="utf-8")
        ctx = detect_context(tmp_path)
        assert "csharp" in ctx["language"]

    def test_deployment_markers(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
        (tmp_path / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
        ctx = detect_context(tmp_path)
        assert "docker" in ctx["deployment_platform"]

    def test_unknown_when_nothing_matches(self, tmp_path):
        ctx = detect_context(tmp_path)
        assert ctx["language"] == []
        assert ctx["detection_confidence"] == "unknown"

    def test_node_framework_detections(self, tmp_path):
        import json

        (tmp_path / "package.json").write_text(json.dumps({
            "dependencies": {"react": "18", "@angular/core": "17"}}), encoding="utf-8")
        ctx = detect_context(tmp_path)
        assert set(ctx["framework"]) >= {"react", "angular"}
