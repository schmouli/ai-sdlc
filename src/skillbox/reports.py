"""SkillBox reports: improvement report + dashboards (research R13; FR-042/FR-043).

Artifacts under `.skillbox/reports/`: improvement_report.md, pending_review.md,
skillbox_dashboard.html (dependency-free vanilla JS). Approval events are
marked on the version timeline (FR-042).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from src.skillbox.telemetry import atomic_write_json, now_et

logger = logging.getLogger("skillbox.reports")

ET = ZoneInfo("America/New_York")


def write_improvement_report(root: Path, metrics, baseline_days: Optional[int] = None) -> Path:
    """FR-042: baseline-vs-current window comparison (all measured subjects)."""
    report_data = metrics.analyze(baseline_days=baseline_days)
    lines = [
        "# SkillBox Improvement Report",
        "",
        f"Generated: {now_et()}",
        f"Window: {report_data['window_start']} -> {report_data['window_end']}",
        "",
        "## Per-Skill Changes (current window vs baseline)",
        "",
        "| Skill | Activations Δ | Success-rate change |",
        "|---|---|---|",
    ]
    for delta in report_data.get("deltas", {}).get("skills", []):
        lines.append(f"| {delta['subject']} | {delta['activations']:+d} | "
                     f"{delta['success_rate_change']:+.3f} |")
    lines += ["", "## Per-Overlay-Version Changes", "",
              "| Overlay@Version | Executions Δ | Success-rate change |", "|---|---|---|"]
    for delta in report_data.get("deltas", {}).get("overlays", []):
        lines.append(f"| {delta['subject']} | {delta['executions']:+d} | "
                     f"{delta['success_rate_change']:+.3f} |")

    approvals = _approval_markers(root)
    lines += ["", "## Human Approval Events (HITL gate, FR-034)", ""]
    if approvals:
        lines.append("| Timestamp | Overlay@Version | Operator | Decision |")
        lines.append("|---|---|---|---|")
        for marker in approvals:
            lines.append(f"| {marker['timestamp']} | {marker['overlay_id']}@{marker['version']} "
                         f"| {marker['operator']} | {marker['decision']} |")
    else:
        lines.append("*No approval events in the report window.*")

    pending_aged = _aged_pending(root)
    lines += ["", "## Pending Review Aging (FR-037)", ""]
    if pending_aged:
        for item in pending_aged:
            lines.append(f"- {item['overlay_id']}@{item['version']}: "
                         f"{item['age_days']} days pending "
                         f"{'**SLA EXCEEDED**' if item['sla_exceeded'] else ''}")
    else:
        lines.append("*No pending overlay versions.*")

    report_path = Path(root) / "reports" / "improvement_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    machine_path = Path(root) / "reports" / "improvement_report.json"
    atomic_write_json(machine_path, report_data)
    return report_path


def _approval_markers(root: Path, window_days: int = 30) -> list:
    audit = Path(root) / "telemetry" / "review_decisions.json"
    if not audit.exists():
        return []
    markers = []
    cutoff = datetime.now(ET).timestamp() - window_days * 86400
    with open(audit, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.fromisoformat(record["timestamp"])
                if ts.timestamp() >= cutoff:
                    markers.append(record)
            except (ValueError, TypeError, KeyError):
                continue
    return markers


def _aged_pending(root: Path) -> list:
    try:
        from src.skillbox.overlays import OverlayLibrary
        from src.skillbox.telemetry import default_store

        store = default_store(root)
        lib = OverlayLibrary(Path(root)).bind_store(store)
        pending = lib.list_pending_reviews(sla_days=7)
        store.close()
        return [p for p in pending if p["age_days"] > 0.05]
    except Exception as exc:
        logger.debug("pending digest deferred: %s", exc)
        return []


def write_pending_digest(root: Path) -> Path:
    """FR-037/FR-038 pending-review digest (scheduler refresh target)."""
    pending = _aged_pending(root)
    lines = ["# SkillBox Pending Review Digest", "",
             f"Generated: {now_et()}", ""]
    if pending:
        for item in pending:
            sla = "**SLA EXCEEDED** " if item["sla_exceeded"] else ""
            lines.append(f"- {sla}{item['overlay_id']}@{item['version']} — "
                         f"{item['age_days']} days pending; evidence: "
                         f"{json.dumps(item['evidence_summary'])[:200]}")
    else:
        lines.append("*Nothing pending review.*")
    path = Path(root) / "reports" / "pending_review.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def refresh_dashboards(root: Path, metrics) -> Dict[str, Any]:
    """FR-043: regenerate the HTML dashboard + JSON snapshots."""
    report_data = metrics.analyze()
    reports_dir = Path(root) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    dashboard_path = reports_dir / "skillbox_dashboard.html"
    html = _render_dashboard(report_data)
    dashboard_path.write_text(html, encoding="utf-8")
    latest = reports_dir / "latest.json"
    atomic_write_json(latest, {"generated_at": now_et(), "report": report_data})
    write_pending_digest(root)
    write_improvement_report(root, metrics)
    return {"dashboard": str(dashboard_path), "latest": str(latest)}


def _render_dashboard(data: Dict[str, Any]) -> str:
    skills_rows = "".join(
        f"<tr><td>{r['skill']}</td><td>{r['activations']}</td><td>{r['successes']}</td>"
        f"<td>{r['failures']}</td><td>{r['avg_duration_ms']}</td></tr>"
        for r in data.get("skills", []))
    overlay_rows = "".join(
        f"<tr><td>{r['overlay_id']}@{r['version']}</td><td>{r['executions_influenced']}</td>"
        f"<td>{r['success_rate']}</td><td>{r['promotion_state']}</td></tr>"
        for r in data.get("overlays", []))
    approvals = _approval_markers(Path(".").resolve() if not data else Path(".skillbox"))
    approval_rows = "".join(
        f"<tr><td>{a['timestamp']}</td><td>{a['overlay_id']}@{a['version']}</td>"
        f"<td>{a['operator']}</td><td>{a['decision']}</td></tr>" for a in approvals[:30])
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>SkillBox Dashboard</title>
<style>
body {{ font-family: 'Segoe UI', Tahoma, sans-serif; margin: 20px; background: #fafafa; }}
h1 {{ color: #1a3c6e; }} h2 {{ color: #2a5ca8; border-bottom: 2px solid #dde; padding-bottom: 4px; }}
table {{ border-collapse: collapse; margin: 12px 0; background: #fff; }}
th, td {{ border: 1px solid #ccd; padding: 6px 14px; text-align: left; }}
th {{ background: #eef3fa; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; background: #2a5ca8; color: #fff; font-size: 12px; }}
</style></head>
<body>
<h1>SkillBox Adaptive Learning Dashboard</h1>
<p>Generated {now_et()} — window {data.get('window_start', '')[:10]} → {data.get('window_end', '')[:10]}</p>
<h2>Per-Skill Performance <span class="badge">{len(data.get('skills', []))} skills</span></h2>
<table><tr><th>Skill</th><th>Activations</th><th>Successes</th><th>Failures</th><th>Avg Duration ms</th></tr>{skills_rows}</table>
<h2>Overlay Version Impact <span class="badge">{len(data.get('overlays', []))} versions</span></h2>
<table><tr><th>Overlay@Version</th><th>Executions</th><th>Success Rate</th><th>State</th></tr>{overlay_rows}</table>
<h2>Human Approval Events (HITL)</h2>
<table><tr><th>Timestamp</th><th>Overlay@Version</th><th>Operator</th><th>Decision</th></tr>{approval_rows}</table>
<p style="color: #888">Dependency-free offline dashboard — SkillBox runtime (specs/004-skillbox-adaptive-learning).</p>
</body></html>"""
