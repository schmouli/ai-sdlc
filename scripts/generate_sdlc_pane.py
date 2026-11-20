"""SDLC measurement pane generator (features 007+008; 007 design § DIP steps 2+3, 008 design § Component 3).

One script, three modes:

*   Render (default): collect Layer-B review artifacts from ``specs/<3-digit>-*/`` +
    the hook-acceptance ledger + the KG benchmark series + the feature-008 size
    budgets/series, evaluate the convention doc's canonical integrity predicate
    table (18 predicates after the feature-008 amendment), and render the single
    tracked HTML pane ``dashboards/sdlc_dashboard.html`` — written only when the
    collected-content hash (recorded in an invisible pane footer) changes.
*   Record (``--record-kg-run``): the KG record command surface (spec FR-013 /
    contracts Contract 2) — validate ``output/kg/evaluation_report.json`` against
    the series field map, dedupe + staleness-refuse, atomically append one JSONL
    line to the tracked series ``data/kg_eval_series.jsonl``.
*   Budget series (render-time, feature-008): one keep-last record per render to
    ``data/context_budget_series.jsonl`` per measurement identity (D1).

Advisory-only boundary (spec FR-022): banners are data, not failures. The
generator exits 0 when the record/pane lands, non-zero only on unreadable inputs.
Storage UTC; display America/New_York converted in the renderer only.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from record_hook_acceptance import capture_enabled, state_root  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    from datetime import timedelta
    _ET = timezone(timedelta(hours=-5))

SPECS_DIR = REPO_ROOT / "specs"
PANE_PATH = REPO_ROOT / "dashboards" / "sdlc_dashboard.html"
REPORT_PATH = REPO_ROOT / "output" / "kg" / "evaluation_report.json"
EXTENSIONS_PATH = REPO_ROOT / ".specify" / "extensions.yml"
STANDING_PATH = REPO_ROOT / ".github" / "copilot-instructions.md"
MANIFEST_PATH = REPO_ROOT / ".specify" / "wiring-manifest.yml"
TOKEN_PROXY_DIVISOR = 4  # chars÷4 display estimate; bytes are the authoritative axis

TICKET_DIR_RE = re.compile(r"^specs/(\d{3})-([a-z0-9-]+)$")
TICKET_GLOB_RE = re.compile(r"^\d{3}-[a-z0-9-]+$")
BIRTH_NUMBER = 7  # 007 and later are convention-era (effective 2026-11-13)

WIRED_STAGES = ["after_specify", "after_plan", "after_tasks", "after_analyze",
                "after_implement", "after_converge"]
KNOWN_SCHEMA = "sdlc-v1"
FAMILIES = ("findings", "resolution", "readiness", "drift", "consistency")

# Canonical integrity predicate table (specs/SDLC_ARTIFACT_CONVENTION.md §5 governs —
# 18 predicates after the feature-008 amendment; this implementation is the doc's
# runtime mirror — SC-003 fixtures are the equivalence check).
TICKET_LEVEL_BANNERS = [
    "MISSING-ARTIFACT", "ORPHAN-ARTIFACT", "NULL-COUNT", "GAP",
    "DRIFT-FIRST-RUN-MISSING", "MISSED-CONVENTION", "INVALID-FRONTMATTER",
    "FAMILY-STAGE-MISMATCH", "INVALID-STAGE", "UNKNOWN-SCHEMA",
]
REPO_LEVEL_BANNERS = ["CAPTURE-DISABLED", "TRUNCATED-LINE", "KG-SERIES-LAG",
                      "TRACKED-OUTPUT-WARNING"]

_BACKOFF_SECONDS = 0.05

# ============================================================ small helpers


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_ts(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _git_head(repo: Path | None = None) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo or REPO_ROOT),
                             capture_output=True, text=True, timeout=10)
        h = out.stdout.strip()
        return h or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _is_git_ignored(target: str, repo: Path | None = None) -> bool:
    """True when ``git check-ignore`` excludes the path (TRACKED-OUTPUT-WARNING)."""
    try:
        out = subprocess.run(["git", "check-ignore", "-q", target], cwd=str(repo or REPO_ROOT),
                             capture_output=True, timeout=10)
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ledger_path() -> Path:
    """The recorder's ledger resolution (METATRADER_ENV → env JSON → state_root)."""
    return state_root() / "wiring" / "hook_ledger.json"


def report_path() -> Path:
    return REPORT_PATH


def series_path() -> Path:
    return REPO_ROOT / "data" / "kg_eval_series.jsonl"


def budget_series_path() -> Path:
    """Feature-008 size series (append-only, keep-last per measurement identity)."""
    return REPO_ROOT / "data" / "context_budget_series.jsonl"


def series_lock_path() -> Path:
    sp = series_path()
    return sp.parent / (sp.name + ".lock")


def budget_series_lock_path() -> Path:
    sp = budget_series_path()
    return sp.parent / (sp.name + ".lock")


def _load_manifest_budgets() -> dict:
    """Token-budget sub-keys from the normative manifest ({} when unreadable — fail-soft)."""
    text = _read(MANIFEST_PATH)
    if text is None:
        return {}
    try:
        data = yaml.safe_load(text) or {}
        tb = data.get("token_budgets") or {}
        return tb if isinstance(tb, dict) else {}
    except yaml.YAMLError:
        return {}


def extensions_hooks() -> dict[str, list[dict]]:
    text = _read(EXTENSIONS_PATH)
    if text is None:
        return {}
    try:
        data = yaml.safe_load(text) or {}
        return data.get("hooks") or {}
    except yaml.YAMLError:
        return {}


# ============================================================ collect: specs


def _stage_from_filename(fname: str) -> tuple[str | None, str | None, int, int, bool]:
    """(stage, family, round_or_run, attempt, is_drift) inferred from a filename."""
    m = re.match(r"^findings_round(\d+)(?:_attempt(\d+))?\.md$", fname)
    if m:
        return "after_specify", "findings", int(m.group(1)), int(m.group(2) or 1), False
    m = re.match(r"^resolution_round(\d+)(?:_attempt(\d+))?\.md$", fname)
    if m:
        return "after_specify", "resolution", int(m.group(1)), int(m.group(2) or 1), False
    m = re.match(r"^findings_tasks_r(\d+)(?:_attempt(\d+))?\.md$", fname)
    if m:
        return "after_tasks", "findings", int(m.group(1)), int(m.group(2) or 1), False
    m = re.match(r"^resolution_tasks_r(\d+)(?:_attempt(\d+))?\.md$", fname)
    if m:
        return "after_tasks", "resolution", int(m.group(1)), int(m.group(2) or 1), False
    m = re.match(r"^consistency_report_round(\d+)(?:_attempt(\d+))?\.md$", fname)
    if m:
        return "after_analyze", "consistency", int(m.group(1)), int(m.group(2) or 1), False
    m = re.match(r"^drift_report_run(\d+)\.md$", fname)
    if m:
        return "after_implement", "drift", int(m.group(1)), 1, True
    if fname == "implementation_readiness_report.md":
        return "after_plan", "readiness", 1, 1, False
    return None, None, 1, 1, False


# filename prefix → wired stages whose naming family it belongs to
_FILENAME_STAGES = [
    ("findings_round", {"after_specify", "after_converge"}),
    ("resolution_round", {"after_specify", "after_converge"}),
    ("findings_tasks_r", {"after_tasks", "after_converge"}),
    ("resolution_tasks_r", {"after_tasks", "after_converge"}),
    ("consistency_report_round", {"after_analyze"}),
    ("drift_report_run", {"after_implement"}),
]


def _filename_stages(fname: str) -> set[str]:
    if fname == "implementation_readiness_report.md":
        return {"after_plan"}
    for prefix, stages in _FILENAME_STAGES:
        if fname.startswith(prefix) and re.match(
                re.escape(prefix) + r"\d+(?:_(?:attempt|run)\d+)?\.md$", fname):
            return stages
    return set()


def parse_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse the YAML frontmatter block; BOM stripped silently; None when absent/unparseable."""
    text = _read(path)
    if text is None or not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    try:
        data = yaml.safe_load(text[3:end].strip("\r\n"))
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        return None


def _bad_typed(fm: dict) -> list[str]:
    """Required-field present-but-wrong-typed (the offending field names)."""
    bad = []
    if not isinstance(fm.get("schema"), str):
        bad.append("schema")
    if not isinstance(fm.get("stage"), str):
        bad.append("stage")
    if not isinstance(fm.get("family"), str):
        bad.append("family")
    for f in ("round", "attempt", "run", "findings_count", "resolved_count"):
        v = fm.get(f)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            bad.append(f)
    wa = fm.get("written_at")
    if wa is not None and not isinstance(wa, (str, datetime)):
        bad.append("written_at")
    return bad


def collect_artifacts(ticket_dir: Path) -> list[dict]:
    arts = []
    for f in sorted(ticket_dir.glob("*.md")):
        fm = parse_frontmatter(f)
        try:
            size = f.stat().st_size
        except OSError:
            size = None
        fstage, ffamily, frun, fattempt, is_drift = _stage_from_filename(f.name)
        rec: dict[str, Any] = {
            "path": f, "name": f.name, "size": size,
            "frontmatter": fm,
            "fm_stage": (fm or {}).get("stage"),
            "fm_family": (fm or {}).get("family"),
            "fm_round": (fm or {}).get("round"),
            "fm_run": (fm or {}).get("run"),
            "fm_attempt": (fm or {}).get("attempt"),
            "fm_findings": (fm or {}).get("findings_count"),
            "fm_resolved": (fm or {}).get("resolved_count"),
            "written_at": (fm or {}).get("written_at") if isinstance((fm or {}).get("written_at"), str) else None,
            "state": "ok", "missing_fields": [],
            "stage": fstage, "family": ffamily, "n": frun, "attempt": fattempt,
            "is_drift": is_drift,
        }
        if fm is None:
            rec["state"] = "INVALID-FRONTMATTER"      # absent or unparseable
        elif _bad_typed(fm):
            rec["state"] = "INVALID-FRONTMATTER"      # wrong-typed required field
            rec["missing_fields"] = _bad_typed(fm)
        else:
            stage, family = fm["stage"], fm["family"]
            if stage not in WIRED_STAGES:
                rec["state"] = "INVALID-STAGE"
            elif family not in FAMILIES:
                rec["state"] = "INVALID-FRONTMATTER"
            else:
                allowed = _filename_stages(f.name)
                if allowed and stage not in allowed:
                    rec["state"] = "FAMILY-STAGE-MISMATCH"
                else:
                    rec["stage"], rec["family"] = stage, family
                    if family == "drift":
                        rec["n"] = fm.get("run") if isinstance(fm.get("run"), int) else frun
                    else:
                        rec["n"] = fm.get("round") if isinstance(fm.get("round"), int) else frun
                    rec["attempt"] = fm.get("attempt") if isinstance(fm.get("attempt"), int) else fattempt
                    # requiredness matrix (known schema only)
                    miss = []
                    if family in ("findings", "resolution", "consistency") and fm.get("round") is None:
                        miss.append("round")
                    if family in ("findings", "readiness", "consistency", "drift") and fm.get("findings_count") is None:
                        miss.append("findings_count")
                    if family == "resolution":
                        if fm.get("resolved_count") is None:
                            miss.append("resolved_count")
                        if fm.get("findings_count") is None and fm.get("resolved_count") is not None:
                            miss.append("findings_count")  # combined-file form requires both
                    if family == "drift" and fm.get("run") is None:
                        miss.append("run")
                    rec["missing_fields"] = miss
                    if fm.get("schema") != KNOWN_SCHEMA:
                        rec["state"] = "UNKNOWN-SCHEMA"
                    elif miss:
                        rec["state"] = "NULL-COUNT"
        arts.append(rec)
    return arts


def collect() -> dict[str, Any]:
    """Collector: read-only walk of the three data sources (plus extensions.yml)."""
    tickets: list[dict] = []
    dirs = sorted(p for p in SPECS_DIR.iterdir() if p.is_dir() and TICKET_GLOB_RE.match(p.name)) \
        if SPECS_DIR.exists() else []
    for d in dirs:
        nnn = int(d.name[:3])
        arts = collect_artifacts(d)
        specs_arts = [a for a in arts if a["stage"] == "after_specify"]
        tasks_arts = [a for a in arts if a["stage"] in ("after_tasks", "after_converge")]
        analyze_arts = [a for a in arts if a["stage"] == "after_analyze"]
        readiness_arts = [a for a in arts if a["stage"] == "after_plan"]
        drift_arts = [a for a in arts if a["stage"] == "after_implement"]

        def _cell_rounds(items, ok_stages):
            if not items:
                return "\u2014"
            ok = [a for a in items if a["state"] == "ok" and (a["stage"] in ok_stages
                                                              if ok_stages else a["stage"] is not None)]
            if not ok:
                return f"{len(items)}*" if len(items) else "\u2014"  # fallback file-count
            rounds = {(a["stage"], a["n"], a["attempt"]) for a in ok}
            base = f"{len(rounds)}"
            if len(rounds) > 1:
                zero = [a for a in ok if a.get("fm_findings") == 0]
                withf = [a for a in ok if isinstance(a.get("fm_findings"), int) and a["fm_findings"] > 0]
                if zero or withf:
                    base += f" ({len(zero)}, {withf[0]['fm_findings'] if withf else 0})"
            return base

        def _count(items):
            if any(a["state"] == "NULL-COUNT" and "findings_count" in a.get("missing_fields", [])
                   for a in items):
                return "n/a"  # required count missing → honest n/a, never zero
            vals = {a["fm_findings"] for a in items
                    if a["state"] == "ok" and isinstance(a["fm_findings"], int)}
            return sum(vals) if vals else None

        run1 = next((a for a in drift_arts if a["state"] == "ok" and a["is_drift"]
                     and a["fm_run"] == 1), None)
        if run1 and isinstance(run1["fm_findings"], int):
            drift_metric = str(run1["fm_findings"])
        elif drift_arts:
            drift_metric = "n/a"   # drift present without a computable first run
        else:
            drift_metric = "\u2014"

        rd = [a for a in readiness_arts if a["state"] == "ok" and isinstance(a["fm_findings"], int)]
        readiness_cell = str(sum({a["fm_findings"] for a in rd})) if rd else \
            ("n/a" if readiness_arts else "\u2014")

        times = sorted(t for t in (_parse_ts(a["written_at"]) for a in arts) if t)
        window = (times[0].strftime("%Y-%m-%d"), times[-1].strftime("%Y-%m-%d")) if times else None

        sizes = [a["size"] for a in arts if isinstance(a["size"], int)]
        cum_bytes = sum(sizes)

        t = {
            "dir": d, "path": f"specs/{d.name}", "number": nnn,
            "name": d.name, "convention_era": nnn >= BIRTH_NUMBER,
            "cum_bytes": cum_bytes,
            "spec_rounds": _cell_rounds(specs_arts, {"after_specify"}),
            "spec_findings": _count(specs_arts),
            "tasks_rounds": _cell_rounds(tasks_arts, {"after_tasks", "after_converge"}),
            "tasks_findings": _count(tasks_arts),
            "analyze_rounds": _cell_rounds(analyze_arts, {"after_analyze"}),
            "analyze_findings": _count(analyze_arts),
            "readiness": readiness_cell,
            "drift": drift_metric,
            "window": window,
            "artifacts": arts, "ledger": [],
        }
        tickets.append(t)

    entries = read_ledger_retry()
    ghost, excluded, mismatched = [], [], []
    ext = extensions_hooks()
    all_wired = {e.get("command") for es in ext.values() for e in (es or [])
                 if e.get("enabled") is not False}
    for e in entries:
        fd = e.get("feature_dir", "")
        m = TICKET_DIR_RE.match(fd)
        if not m:
            excluded.append(e)
            continue
        if not (SPECS_DIR / f"{m.group(1)}-{m.group(2)}").exists():
            ghost.append(e)
        if e.get("command") not in all_wired:
            mismatched.append(e)
    by_path = {t["path"]: t for t in tickets}
    for e in entries:
        t = by_path.get(e.get("feature_dir"))
        if t is not None:
            t["ledger"].append(e)
    ctx = context_size(tickets)
    return {
    "tickets": tickets,
    "ledger_entries": entries,
    "excluded": excluded,
    "ghosts": ghost,
    "command_mismatch": mismatched,
    "series": read_series_retry(),
    "context": ctx,
    }

# ============================================================ context size (feature 008)


def _extract_marker_region(text: str) -> str | None:
    """The SKILLBOX:BEGIN…SKILLBOX:END region inclusive, or None when absent."""
    b = text.find("SKILLBOX:BEGIN")
    e = text.find("SKILLBOX:END")
    if b == -1 or e == -1 or e < b:
        return None
    end = text.find("\n", e)
    return text[b:end if end != -1 else len(text)]


def context_size(tickets: list[dict] | None = None) -> dict[str, Any]:
    """Standing-instruction size + budgets + breach computation (fail-soft)."""
    standing = STANDING_PATH
    standing_bytes = standing.stat().st_size if standing.exists() else None
    standing_text = _read(standing)
    tb = _load_manifest_budgets()
    standing_cap = tb.get("standing_instructions_max_bytes")
    enforce = bool(tb.get("enforce_standing_instructions", False))
    budgets = tb.get("artifact_budgets") or {}

    # Breaches: budgeted-family Layer-B artifacts over their family ceiling.
    # Applicability: only the MOST RECENT round within each naming series
    # (findings_round*, resolution_round*, findings_tasks_r*, resolution_tasks_r*,
    # drift_report_run*) per (ticket, attempt) fires; historical rounds are
    # never bannered and never edited.
    series_prefixes = ("findings_round", "resolution_round", "findings_tasks_r",
                       "resolution_tasks_r", "drift_report_run")
    breaches: list[dict[str, Any]] = []
    dirs = sorted(p for p in SPECS_DIR.iterdir() if p.is_dir() and TICKET_GLOB_RE.match(p.name)) \
        if SPECS_DIR.exists() else []
    for d in dirs:
        best: dict[tuple[str, int], tuple[int, int]] = {}
        meta: dict[Path, tuple[str, int, int, str]] = {}
        for f in d.glob("*.md"):
            fm = parse_frontmatter(f)
            if not fm or fm.get("schema") != KNOWN_SCHEMA:
                continue
            fam = fm.get("family")
            if fam not in ("findings", "resolution", "drift"):
                continue
            prefix = next((p for p in series_prefixes if f.name.startswith(p)), None)
            if prefix is None:
                continue
            n = fm.get("run") if fam == "drift" else fm.get("round")
            if not isinstance(n, int):
                continue
            att = fm.get("attempt") if isinstance(fm.get("attempt"), int) else 1
            meta[f] = (fam, n, att, prefix)
            k = (prefix, att)
            cur = best.get(k)
            if cur is None or n > cur[0]:
                best[k] = (n, att)
        for f, (fam, n, att, prefix) in meta.items():
            cap = budgets.get(f"{fam}_max_bytes")
            if not isinstance(cap, int) or isinstance(cap, bool):
                continue
            cur = best.get((prefix, att))
            if cur is None or (n, att) != (cur[0], cur[1]):
                continue  # historical round: applicability is most-recent-only
            try:
                sz = f.stat().st_size
            except OSError:
                continue
            if sz > cap:
                breaches.append({"ticket": d.name, "artifact": f.name, "family": fam,
                                 "bytes": sz, "budget": cap})

    return {
        "standing_bytes": standing_bytes,
        "standing_missing": not standing.exists(),
        "standing_cap": standing_cap if isinstance(standing_cap, int)
                        and not isinstance(standing_cap, bool) else None,
        "enforce": enforce,
        "token_proxy": (standing_bytes // TOKEN_PROXY_DIVISOR
                        if isinstance(standing_bytes, int) else None),
        "breaches": breaches,
        "marker_region_present": (_extract_marker_region(standing_text) is not None
                                  if standing_text is not None else False),
        "total_artifact_bytes": (sum(t["cum_bytes"] for t in tickets)
                                 if tickets is not None else None),
    }


# ============================================================ collect: ledger/series


def read_ledger_retry(retries: int = 3, backoff: float = _BACKOFF_SECONDS) -> list[dict]:
    """Read with retry/backoff; a torn read returns [] (rendered as a data warning)."""
    lp = ledger_path()
    for attempt in range(retries):
        try:
            data = json.loads(lp.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data["entries"]
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(backoff * (attempt + 1))
    return []


def read_series_retry(retries: int = 3, backoff: float = _BACKOFF_SECONDS) -> list[dict]:
    """Series read with retry; a truncated last line is flagged for TRUNCATED-LINE."""
    sp = series_path()
    for attempt in range(retries):
        try:
            raw = sp.read_text(encoding="utf-8")
            lines, truncated = [], False
            for l in raw.splitlines():
                if not l.strip():
                    continue
                try:
                    lines.append(json.loads(l))
                except json.JSONDecodeError:
                    truncated = True
            return lines + [{"__name__": "_flag", "__truncated__": truncated}]
        except OSError:
            time.sleep(backoff * (attempt + 1))
    return [{"__name__": "_flag", "__truncated__": False}]


def dedupe_ledger(entries: list[dict]) -> list[dict]:
    """10 s burst dedupe per (feature_dir, stage, command, decision); earliest kept.

    Rule property verified by test: N distinct keys in → N rows out.
    """
    buckets: dict[tuple, list[dict]] = {}
    for e in entries:
        key = (e.get("feature_dir"), e.get("event"), e.get("command"), e.get("decision"))
        buckets.setdefault(key, []).append(e)

    def key_ts(e):
        t = _parse_ts(e.get("ts"))
        return (t is None, t or datetime.min.replace(tzinfo=timezone.utc))

    out = []
    for key, items in buckets.items():
        items.sort(key=key_ts)
        kept: list[dict] = []
        for e in items:
            if kept:
                last = _parse_ts(kept[-1]["ts"])
                cur = _parse_ts(e["ts"])
                if cur and last and (cur - last).total_seconds() <= 10.0:
                    continue  # same burst bucket
            kept.append(e)
        out.extend(kept)
    return out


# ============================================================ integrity()


def repo_banners(collected: dict) -> list[dict]:
    """Repo-level predicates: CAPTURE-DISABLED, TRUNCATED-LINE, KG-SERIES-LAG, TRACKED-OUTPUT-WARNING."""
    banners = []
    flags = [e for e in collected["series"] if e.get("__name__") == "_flag"]
    if flags and flags[-1].get("__truncated__"):
        banners.append({"name": "TRUNCATED-LINE", "detail": "truncated series line skipped on render"})
    if capture_enabled() is False:
        banners.append({"name": "CAPTURE-DISABLED",
                        "detail": "capture is off (wiring_capture_enabled: false) — persistent watermark"})
    real_lines = [e for e in collected["series"] if not e.get("__name__")]
    rp = report_path()
    rep = None
    if rp.exists():
        try:
            rep = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            rep = None
    if isinstance(rep, dict) and rep.get("graph_commit_sha"):
        match = any(isinstance(l, dict) and l.get("snapshot_commit") == rep["graph_commit_sha"]
                    and l.get("suite_version") == rep.get("suite_version") for l in real_lines)
        if not match:
            banners.append({"name": "KG-SERIES-LAG",
                            "detail": f"report snapshot_commit={rep['graph_commit_sha']} matches no series line"})
    try:
        ignored = _is_git_ignored(str(PANE_PATH.relative_to(REPO_ROOT))) or \
            _is_git_ignored(str(series_path().relative_to(REPO_ROOT)))
    except ValueError:  # sandboxed paths outside REPO_ROOT
        ignored = False
    if ignored:
        banners.append({"name": "TRACKED-OUTPUT-WARNING",
                        "detail": "a pane output (pane or benchmark series) is excluded by version control"})
    return banners


def evaluate_integrity(collected: dict, repo_banners_: list[dict]) -> list[dict]:
    """Evaluate the canonical table; per-ticket predicates scoped to convention era."""
    banners: list[dict] = list(repo_banners_)
    deduped = dedupe_ledger(collected["ledger_entries"])
    orphan_exempt = {"INVALID-FRONTMATTER", "INVALID-STAGE", "NULL-COUNT",
                     "UNKNOWN-SCHEMA", "FAMILY-STAGE-MISMATCH"}
    stage_families = {"after_specify": ("findings", "resolution"),
                      "after_plan": ("readiness",),
                      "after_tasks": ("findings", "resolution"),
                      "after_analyze": ("consistency",),
                      "after_implement": ("drift",),
                      "after_converge": ("findings", "resolution")}
    for t in collected["tickets"]:
        if not t["convention_era"]:
            continue  # pre-convention: banner-free by canon
        mine = [e for e in dedupe_ledger(t["ledger"]) if e.get("decision") == "accepted"]
        events_seen = {e.get("event") for e in mine}
        for st in events_seen & set(WIRED_STAGES):
            fams = stage_families.get(st, ())
            matching = [a for a in t["artifacts"] if a["state"] == "ok"
                        and a["stage"] == st and a["family"] in fams]
            if not matching:
                banners.append({"name": "MISSING-ARTIFACT", "ticket": t["name"], "stage": st,
                                "detail": f"expected artifact family for stage {st}"})
        for a in t["artifacts"]:
            if a["state"] in orphan_exempt or a["stage"] not in WIRED_STAGES:
                continue
            if a["stage"] not in events_seen:
                banners.append({"name": "ORPHAN-ARTIFACT", "ticket": t["name"],
                                "path": a["name"], "stage": a["stage"],
                                "detail": "artifact with no accepted ledger row for (ticket, stage)"})
        for a in t["artifacts"]:
            if a["state"] in ("INVALID-FRONTMATTER", "NULL-COUNT", "INVALID-STAGE",
                              "FAMILY-STAGE-MISMATCH", "UNKNOWN-SCHEMA"):
                if a["state"] == "NULL-COUNT":
                    detail = "required field(s) missing/null: " + ", ".join(a.get("missing_fields", []))
                elif a["state"] == "INVALID-STAGE":
                    detail = f"frontmatter stage not wired: {a.get('fm_stage')}"
                elif a["state"] == "FAMILY-STAGE-MISMATCH":
                    detail = f"filename family contradicts stage {a.get('fm_stage')}"
                elif a["state"] == "UNKNOWN-SCHEMA":
                    detail = (f"schema {(parse_frontmatter(a['path']) or {}).get('schema')} is not "
                              f"known ({KNOWN_SCHEMA}) — known fields render, unknown-field data "
                              "renders as no data")
                else:
                    detail = "frontmatter absent/unparseable or wrong-typed"
                banners.append({"name": a["state"], "ticket": t["name"], "path": a["name"],
                                "detail": detail})
        if not any(a["state"] == "ok" for a in t["artifacts"]):
            banners.append({"name": "MISSED-CONVENTION", "ticket": t["name"],
                            "detail": "convention-era ticket lacks convention artifacts"})
        by_sa: dict[tuple, set[int]] = {}
        for a in t["artifacts"]:
            if a["state"] == "ok" and a["stage"] in WIRED_STAGES and not a["is_drift"] \
                    and isinstance(a["n"], int) and isinstance(a["attempt"], int):
                by_sa.setdefault((a["stage"], a["attempt"]), set()).add(a["n"])
        for (st, att), rounds in by_sa.items():
            missing = set(range(1, max(rounds) + 1)) - rounds
            if missing:
                banners.append({"name": "GAP", "ticket": t["name"], "stage": st, "attempt": att,
                                "detail": f"missing round(s): {sorted(missing)}"})
        drift_runs = {a["fm_run"] for a in t["artifacts"] if a["is_drift"] and isinstance(a["fm_run"], int)}
        if drift_runs and 1 not in drift_runs:
            banners.append({"name": "DRIFT-FIRST-RUN-MISSING", "ticket": t["name"],
                            "detail": f"drift series present without run: 1 (runs: {sorted(drift_runs)})"})
    return banners


# ============================================================ render

FOOTER_MARK = "collected-content-hash"


def _content_hash(collected: dict, banners: list[dict]) -> str:
    """Hash over the collected canon (parsed cells, banners, listings) — never decoration."""
    payload = {
        "tickets": [{k: v for k, v in t.items()
                     if k not in ("dir", "artifacts", "ledger")}
                    for t in collected["tickets"]],
        "artifact_states": [[a["name"], a["state"]] for t in collected["tickets"] for a in t["artifacts"]],
        "ledger_rows": [[e.get("ts"), e.get("event"), e.get("command"), e.get("decision"),
                         e.get("feature_dir")] for e in collected["ledger_entries"]],
        "banners": banners,
        "excluded": [[e.get("ts"), e.get("feature_dir"), e.get("event")] for e in collected["excluded"]],
        "ghosts": [[e.get("ts"), e.get("feature_dir")] for e in collected["ghosts"]],
        "command_mismatch": [[e.get("ts"), e.get("feature_dir"), e.get("command")] for e in collected["command_mismatch"]],
        "series": [e for e in collected["series"] if not e.get("__name__")],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _read_pane_hash(pane: Path) -> str | None:
    try:
        text = pane.read_text(encoding="utf-8")
        i = text.rfind(FOOTER_MARK + ":")
        if i >= 0:
            return text[i + len(FOOTER_MARK) + 1:].strip().split("-->")[0].strip()
    except OSError:
        pass
    return None


def _et(dt: datetime) -> str:
    return dt.astimezone(_ET).strftime("%Y-%m-%d %H:%M ET")


def _esc(s: Any) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cell(v: Any) -> str:
    if v is None:
        return "n/a"
    if v == "\u2014":
        return "\u2014"
    return _esc(v)


GLYPH_LEGEND = "&ldquo;<code>0</code>&rdquo; clean &middot; <code>n/a</code> no data &middot; <code>&mdash;</code> not captured"


def _fmt_bytes(v: Any) -> str:
    return f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else "n/a"


def _mean_num(vals: list) -> float | None:
    vs = [float(v) for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(vs) / len(vs) if vs else None


def _metric(rows: list[dict], key: str) -> float | None:
    ms = []
    for t in rows:
        v = t[key]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            ms.append(float(v))
        elif isinstance(v, str) and v and v[0].isdigit():
            try:
                ms.append(float(v.split(" ")[0].split("(")[0].rstrip("*")))
            except ValueError:
                pass
    return _mean_num(ms)


def _trend_block(convention_tickets: list[dict]) -> str:
    if len(convention_tickets) < 6:
        return ('<p class="trend-gate">Trend deltas: <strong>insufficient data</strong> '
                f"(gate: &ge;6 convention-era tickets; have {len(convention_tickets)}).</p>")
    first3, last3 = convention_tickets[:3], convention_tickets[-3:]
    rows = []
    for label, key in ("Mean spec rounds", "spec_rounds"), \
                      ("Mean findings/round", "spec_findings"), \
                      ("Mean drift (first run)", "drift"):
        a, b = _metric(first3, key), _metric(last3, key)
        if a is None or b is None:
            delta = "n/a"
        else:
            d = b - a
            delta = f"{'+' if d >= 0 else ''}{d:.2f}"
        ca = f"{a:.2f}" if a is not None else "n/a"
        cb = f"{b:.2f}" if b is not None else "n/a"
        rows.append(f"<tr><td>{label}</td><td>{ca}</td><td>{cb}</td><td>{delta}</td></tr>")
    return ('<table class="trend"><tr><th>Metric</th><th>first-3</th><th>last-3</th><th>&Delta;</th></tr>'
            + "".join(rows) + "</table>"
            + '<p class="footnote">Means over captured (non-n/a) cells only &mdash; never percentages; '
            "an explicit n= rides the consolidated cells when actual numbers are held.</p>")


def render_html(collected: dict, banners: list[dict] | None = None,
                now: datetime | None = None, content_hash: str | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    ctx = collected.get("context") or context_size(collected.get("tickets"))
    if banners is None:
        banners = evaluate_integrity(collected, repo_banners(collected)) \
            + budget_repo_banners(ctx)
    era = lambda t: "convention-era" if t["convention_era"] else "pre-convention"
    tickets = sorted(collected["tickets"], key=lambda t: t["number"])
    convention_tickets = [t for t in tickets if t["convention_era"]]

    def size_cells(t: dict) -> tuple[str, str]:
        per = " + ".join((f"{a['size']:,}" if isinstance(a["size"], int) else "n/a")
                         for a in t["artifacts"]) or "\u2014"
        return per, f"{t['cum_bytes']:,}"

    body_rows = []
    for t in tickets:
        stages = {e.get("event") for e in t["ledger"]}
        ledger_cell = ", ".join(s for s in WIRED_STAGES if s in stages) or "\u2014"
        win = f"{t['window'][0]} &rarr; {t['window'][1]}" if t["window"] else "(mtime-degraded&dagger;)"
        per_bytes, cum_bytes = size_cells(t)

        def findings_cell(v, rounds):
            if rounds in ("\u2014",):
                return "\u2014"
            if isinstance(v, int):
                return str(v)
            return "n/a"  # count missing (or no counted artifacts) → honest n/a

        body_rows.append(
            f"<tr><td>{_esc(t['path'])}</td><td>{era(t)}</td>"
            f"<td>{_cell(t['spec_rounds'])}</td><td>{findings_cell(t['spec_findings'], t['spec_rounds'])}</td>"
            f"<td>{_cell(t['tasks_rounds'])}</td><td>{findings_cell(t['tasks_findings'], t['tasks_rounds'])}</td>"
            f"<td>{_cell(t['analyze_rounds'])}</td><td>{findings_cell(t['analyze_findings'], t['analyze_rounds'])}</td>"
            f"<td>{_cell(t['readiness'])}</td><td>{_cell(t['drift'])}</td>"
            f"<td>{per_bytes}</td><td>{cum_bytes}</td>"
            f"<td>{_esc(ledger_cell)}</td><td>{win}</td></tr>")

    series = [e for e in collected["series"] if not e.get("__name__")]
    kg_rows = "".join(
        f"<tr><td>{_esc(e.get('ts_utc'))}</td><td><code>{_esc(e.get('recorded_head'))}</code></td>"
        f"<td><code>{_esc(e.get('snapshot_commit'))}</code></td><td>{_esc(e.get('suite_version'))}</td>"
        f"<td>{_esc(json.dumps(e.get('hit_rate', {})))}</td>"
        f"<td>{_esc(json.dumps(e.get('mean_cost', {})))}</td></tr>"
        for e in series)
    if kg_rows:
        segs: dict[str, int] = {}
        for e in series:
            segs[e.get("suite_version")] = segs.get(e.get("suite_version"), 0) + 1
        seg_note = "<p class='footnote'>Segmented by suite version: " + \
            "; ".join(f"{s} ({n} run{'s' if n != 1 else ''})" for s, n in segs.items()) + \
            " &mdash; a suite edit starts a new segment, never a spurious jump.</p>"
        kg_block = (f'<table><tr><th>ts_utc</th><th>recorded_head</th><th>snapshot_commit</th>'
                    f'<th>suite</th><th>hit_rate (per easy/hard)</th><th>mean_cost</th></tr>{kg_rows}</table>')
    else:
        kg_block = ('<p class="none">No benchmark runs recorded yet (the series materializes on the first '
                    '<code>--record-kg-run</code>).</p>')
        seg_note = ""

    pre_conv = [t for t in tickets if not t["convention_era"]]

    # --- feature-008: per-artifact bytes column + per-ticket cumulative + Context panel
    budget_series, _ = _read_series_lines(budget_series_path())

    def ctx_panel(ctx: dict) -> str:
        if ctx.get("standing_missing"):
            standing_cell = ('<span class="banner-inline">BUDGET-MISSING-STANDING</span>')
        else:
            sb = ctx.get("standing_bytes")
            proxy = ctx.get("token_proxy")
            standing_cell = f"{sb:,} B (token proxy, &divide;4: ~{proxy:,})" \
                if isinstance(sb, int) else "n/a"
        cap = ctx.get("standing_cap")
        mode = "enforced" if ctx.get("enforce") else "advisory"
        cap_cell = f"{cap:,} B ({mode})" if isinstance(cap, int) else "no budget declared (advisory)"
        trend_rows = "".join(
            f"<tr><td>{_esc(e.get('ts_utc'))}</td><td><code>{_esc(e.get('git_head'))}</code></td>"
            f"<td>{_fmt_bytes(e.get('standing_bytes'))}</td>"
            f"<td>{_fmt_bytes(e.get('artifact_total_bytes'))}</td>"
            f"<td>{len(e.get('breaches') or [])}</td></tr>"
            for e in budget_series) or '<tr><td colspan="5">no records yet</td></tr>'
        breach_items = "".join(
            f'<li><code>{_esc(b.get("artifact"))}</code> [{_esc(b.get("ticket"))}] '
            f"{b.get('bytes'):,} B &gt; {b.get('budget'):,} B</li>"
            for b in ctx.get("breaches", [])) or '<li class="none">None in recent rounds.</li>'
        return (
            f'<table><tr><th>Standing instructions</th><th>Budget</th><th>Artifact total (tracked)</th></tr>'
            f'<tr><td>{standing_cell}</td><td>{cap_cell}</td>'
            f'<td>{(ctx.get("total_artifact_bytes") or 0):,} B</td></tr></table>'
            f"<h3>Budget breaches (recent rounds only; advisory &mdash; disposition in the next round, "
            f"never edit history)</h3><ul>{breach_items}</ul>"
            f"<h3>Standing-size history (data/context_budget_series.jsonl)</h3>"
            f'<table><tr><th>ts_utc</th><th>git_head</th><th>standing_bytes</th>'
            f'<th>artifact_total_bytes</th><th>breaches</th></tr>{trend_rows}</table>')

    def _listing(title, items, render):
        if not items:
            return ""
        return f"<h3>{title}</h3><ul>" + "".join(f"<li>{render(i)}</li>" for i in items) + "</ul>"

    banner_parts = []
    for b in banners:
        bref = ""
        if b.get("ticket"):
            bref = (f' <span class="bref">[{_esc(b["ticket"])}'
                    + (f'/{_esc(b["stage"])}' if b.get("stage") else "") + "]</span>")
        banner_parts.append(
            f'<div class="banner b-{_esc(b["name"])}"><strong>{_esc(b["name"])}</strong>'
            f' &mdash; {_esc(b.get("detail", ""))}{bref}</div>')
    banner_html = "".join(banner_parts) or '<p class="none">No integrity banners fired.</p>'

    drilldowns = []
    for t in tickets:
        rows = "".join(
            f"<tr><td>{_esc(a['name'])}</td><td>{_esc(a['state'])}</td><td>{_esc(a['stage'])}</td>"
            f"<td>{_esc(a['family'])}</td><td>{_cell(a['fm_round'])}</td><td>{_cell(a['fm_attempt'])}</td>"
            f"<td>{_cell(a['fm_findings'])}</td><td>{_cell(a['fm_resolved'])}</td><td>{_cell(a['fm_run'])}</td>"
            f"<td>{_esc(a['written_at'])}</td></tr>"
            for a in t["artifacts"]) or '<tr><td colspan="10">no Layer-B artifacts</td></tr>'
        led = "".join(
            f"<tr><td>{_esc(e.get('ts'))}</td><td>{_esc(e.get('event'))}</td><td>{_esc(e.get('command'))}</td>"
            f"<td>{_esc(e.get('decision'))}</td></tr>"
            for e in t["ledger"]) or '<tr><td colspan="4">no ledger entries</td></tr>'
        drilldowns.append(
            f"<details><summary>{_esc(t['path'])} ({era(t)})</summary>"
            f"<table><tr><th>Artifact</th><th>State</th><th>Stage</th><th>Family</th><th>Round</th>"
            f"<th>Attempt</th><th>Findings</th><th>Resolved</th><th>Run</th><th>written_at</th></tr>{rows}</table>"
            f"<table><tr><th>Ledger ts</th><th>Stage</th><th>Command</th><th>Decision</th></tr>{led}</table></details>")

    watermark = ""
    if any(b["name"] == "CAPTURE-DISABLED" for b in banners):
        watermark = ('<div class="watermark">CAPTURE-DISABLED &mdash; capture is off; '
                     "ledger data may be stale/incomplete</div>")

    summary_counts = (
        f"tickets found: {len(tickets)} &middot; artifacts parsed: "
        f"{sum(len(t['artifacts']) for t in tickets)} &middot; ledger entries: "
        f"{len(collected['ledger_entries'])} &middot; exclusions: {len(collected['excluded'])}"
        + f" &middot; ghosts: {len(collected['ghosts'])} &middot; command mismatches: {len(collected['command_mismatch'])}")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SDLC Measurement Pane</title>
<style>
body {{ font-family: Segoe UI, sans-serif; margin: 2rem; color: #1a1a1a; }}
h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1.1rem; border-bottom: 1px solid #ccc; padding-bottom: .2rem; }}
h3 {{ font-size: 1rem; }}
table {{ border-collapse: collapse; margin: .6rem 0; font-size: .85rem; }}
th, td {{ border: 1px solid #bbb; padding: .25rem .5rem; text-align: left; }}
th {{ background: #f0f0f0; }}
.watermark {{ background: #ffe9a8; border: 1px solid #d9b44a; padding: .4rem .7rem; margin: .5rem 0; }}
.banner {{ border-left: 4px solid #b00; background: #fdecea; padding: .3rem .6rem; margin: .25rem 0; font-size: .85rem; }}
.banner.b-CAPTURE-DISABLED, .banner.b-TRACKED-OUTPUT-WARNING, .banner.b-BUDGET-BREACH {{ border-color: #d9b44a; }}
.banner.b-SERIES-UNCOMMITTED, .banner.b-SERIES-DUPLICATE, .banner.b-BUDGET-MISSING-STANDING {{ border-color: #d9b44a; }}
.banner-inline {{ color: #b00; font-weight: bold; }}
.none {{ color: #555; }}
details {{ margin: .35rem 0; }} summary {{ cursor: pointer; font-size: .9rem; }}
.trend-gate {{ color: #666; }} .footnote {{ color: #666; font-size: .78rem; }}
.bref {{ color: #666; }}
</style>
</head>
<body>
<h1>SDLC Measurement Pane</h1>
<p class="meta">Generated (ET): <strong>{_et(now)}</strong> &middot; {summary_counts}</p>
{watermark}
<h2>1. Consolidated cross-ticket table</h2>
<p class="legend">{GLYPH_LEGEND}; parenthetical = clean-round breakdown; &dagger;/postfixed * = file-count fallback (&ldquo;unverified, no frontmatter&rdquo;).</p>
<table>
<tr><th>Ticket</th><th>Convention status</th><th>Spec rounds</th><th>Spec findings</th><th>Tasks rounds</th><th>Tasks findings</th><th>Analyze rounds</th><th>Analyze findings</th><th>Readiness findings</th><th>Drift (first run)</th><th>Artifact bytes</th><th>Ticket bytes</th><th>Ledger stages</th><th>Ticket window (written_at)</th></tr>
{''.join(body_rows) or '<tr><td colspan="14">n/a</td></tr>'}
</table>
<h2>2. Trend deltas</h2>
{_trend_block(convention_tickets)}
<h2>3. KG benchmark series</h2>
{kg_block}
{seg_note}
<h2>4. Context size (feature 008 &mdash; standing instructions, budgets, breaches, history)</h2>
{ctx_panel(ctx)}
<h2>5. Integrity banners (canonical predicate table &mdash; the convention doc governs)</h2>
{banner_html}
<h2>6. Auxiliary listings</h2>
{_listing('Non-ticket ledger entries (excluded)', collected['excluded'], lambda e: f"{_esc(e.get('feature_dir'))} ({_esc(e.get('event'))} / {_esc(e.get('command'))})")}
{_listing('Ghost feature dirs (no directory on disk)', collected['ghosts'], lambda e: _esc(e.get('feature_dir')))}
{_listing('Command-vs-extensions.yml mismatches', collected['command_mismatch'], lambda e: f"{_esc(e.get('feature_dir'))} recorded {_esc(e.get('command'))} at {_esc(e.get('event'))}")}
{_listing('Pre-convention tickets (001&ndash;006, banner-free)', pre_conv, lambda t: _esc(t['path']))}
<h2>7. Per-ticket drill-downs</h2>
{''.join(drilldowns) or '<p class="none">No tickets found.</p>'}
<hr>
<p class="footnote">Generated by scripts/generate_sdlc_pane.py (features 007+008) &mdash; advisory evidence layer; the convention doc is normative.</p>
<!-- {FOOTER_MARK}: {content_hash or 'PLACEHOLDER'} -->
</body>
</html>
"""


def run_render(pane: Path = PANE_PATH, now: datetime | None = None) -> bool:
    """Collect → hash-compare → write only on change (SC-004). Absent pane ⇒ write."""
    collected = collect()
    banners = evaluate_integrity(collected, repo_banners(collected))
    banners = banners + budget_repo_banners(collected["context"])
    status, msg = record_budget_series(now=now, ctx=collected["context"])
    print(f"BUDGET-SERIES: {status}: {msg}", file=sys.stderr if status != "RECORDED" else sys.stdout)
    h = _content_hash(collected, banners)
    if pane.exists() and _read_pane_hash(pane) == h:
        return False  # unchanged content: write nothing
    html = render_html(collected, banners, now=now, content_hash=h)
    pane.parent.mkdir(parents=True, exist_ok=True)
    pane.write_text(html, encoding="utf-8", newline="\n")
    return True


# ============================================================ record mode (T005)


def _lock_acquire(lock: Path, retries: int = 3, backoff: float = _BACKOFF_SECONDS,
                  stale_age: float = 10.0):
    """O_CREAT|O_EXCL lockfile; 3 retries; stale takeover by mtime age; None on exhaustion."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries):
        try:
            return os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as e:
            if e.errno == errno.EEXIST:
                try:
                    if time.time() - lock.stat().st_mtime > stale_age:
                        lock.unlink()
                        continue
                except OSError:
                    pass
            time.sleep(backoff * (attempt + 1))
    return None


def _read_series_lines(sp: Path) -> tuple[list[dict], bool]:
    """(parsed lines, truncated?) — blank lines skipped; bad JSON flags truncation."""
    lines: list[dict] = []
    truncated = False
    if sp.exists():
        for l in sp.read_text(encoding="utf-8").splitlines():
            if not l.strip():
                continue
            try:
                lines.append(json.loads(l))
            except json.JSONDecodeError:
                truncated = True
    return lines, truncated


def append_series_record(path: Path, lock_path: Path, line: dict, identity_fields: tuple,
                         identity_values: tuple, *, mode: str = "skip-on-match",
                         staleness_source=None) -> tuple[str, str]:
    """Shared series-append contract (feature-008 extraction; plan Contract 3).

    The validate→lock→append tail shared by the KG record path (first caller,
    identical behavior incl. pinned exit semantics) and the feature-008 budget
    record path (keep-last mode).

    Modes:
      "skip-on-match": any identity match → (SKIPPED, reason); nothing appended.
      "keep-last":     identity matching the MOST-RECENT record replaces that
                       record's ts_utc in place (single-record touch, atomic
                       temp-rename); a match on a non-recent record is refused
                       (earlier records are immutable); two+ matching lines is a
                       SERIES-DUPLICATE refusal.

    ``staleness_source``: callable → comparable datetime, or None (no-op). When the
    source datetime is older than the last record's ts_utc → (REFUSED, stale…).

    Returns (status, message); status in {RECORDED, SKIPPED, REFUSED}.
    """
    lines, truncated = _read_series_lines(path)
    prev_ts = _parse_ts(lines[-1].get("ts_utc")) if lines else None
    src_dt = staleness_source() if staleness_source is not None else None
    if prev_ts is not None and src_dt is not None and src_dt < prev_ts:
        return ("REFUSED", f"stale input — source datetime {_iso_z(src_dt)} is older "
                           f"than the previous series record {_iso_z(prev_ts)}; series unchanged")
    if truncated:
        return ("REFUSED", "existing series has a truncated line — resolve it before appending")
    matches = [i for i, l in enumerate(lines)
               if tuple(l.get(f) for f in identity_fields) == tuple(identity_values)]
    if matches:
        if mode == "keep-last":
            if len(matches) > 1:
                return ("SKIPPED", "duplicate identity lines already present (SERIES-DUPLICATE) — "
                                   "resolve the series before appending")
            idx = matches[-1]
            if idx == len(lines) - 1:
                lines[idx]["ts_utc"] = line["ts_utc"]
                fd = _lock_acquire(lock_path)
                if fd is None:
                    return ("REFUSED", "series lock unavailable after retries; series unchanged")
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_name(path.name + ".tmp")
                    tmp.write_text("".join(json.dumps(l, ensure_ascii=False) + "\n"
                                           for l in lines), encoding="utf-8", newline="\n")
                    os.replace(tmp, path)
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                return ("RECORDED", f"1 record superseded (keep-last) in {path}")
            return ("SKIPPED", "identity matches a non-recent record — earlier records are immutable")
        return ("SKIPPED", "dedupe — a line with this identity already exists")
    fd = _lock_acquire(lock_path)
    if fd is None:
        return ("REFUSED", "series lock unavailable after retries; series unchanged")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            lock_path.unlink()
        except OSError:
            pass
    return ("RECORDED", f"1 line appended to {path}")


KG_IDENTITY_FIELDS = ("recorded_head", "snapshot_commit", "suite_version", "report_content_hash")


def _validate_report(rep: Any) -> str | None:
    """Contract 2 schema check against the convention doc's field map; reason or None."""
    if not isinstance(rep, dict):
        return "report payload is not a JSON object"
    if not isinstance(rep.get("graph_commit_sha"), str) or not rep["graph_commit_sha"]:
        return "missing field(s): graph_commit_sha"
    if not isinstance(rep.get("suite_version"), str) or not rep["suite_version"]:
        return "missing field(s): suite_version"
    agg = rep.get("aggregate")
    if not isinstance(agg, dict) or not agg:
        return "report payload partial/invalid (aggregate must be a non-empty object of method objects)"
    for m, v in agg.items():
        if not isinstance(v, dict) or not v:
            return f"aggregate.{m} must be a non-empty object"
        for cat, cv in v.items():
            if not isinstance(cv, dict) or not isinstance(cv.get("hit_rate"), (int, float)) \
                    or isinstance(cv.get("hit_rate"), bool):
                return f"aggregate.{m}.{cat}.hit_rate must be numeric"
    return None


def record_kg_run(force: bool = False) -> int:
    """Contract 2 producer: validate → hash → dedupe/staleness → atomic append.

    Exit semantics (pinned): a refusal exits 0 with the reason on stderr; a missing
    report file (unreadable input) exits non-zero; success exits 0.
    """
    rp = report_path()
    sp = series_path()
    if not rp.exists():
        print(f"REFUSED: report file not found: {rp}", file=sys.stderr)
        return 1  # non-zero: unreadable/absent input
    try:
        raw = rp.read_bytes()
        rep = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"REFUSED: report unreadable: {e}", file=sys.stderr)
        return 1
    reason = _validate_report(rep)
    if reason:
        print(f"REFUSED: report schema failure — {reason}; series unchanged", file=sys.stderr)
        return 0  # refusal exits 0

    lines, _truncated = _read_series_lines(sp)
    prev_ts = _parse_ts(lines[-1].get("ts_utc")) if lines else None
    mtime = datetime.fromtimestamp(rp.stat().st_mtime, tz=timezone.utc)
    if prev_ts is not None and mtime < prev_ts and not force:
        print(f"REFUSED: stale report — file mtime {_iso_z(mtime)} is older than the "
              f"previous series record {_iso_z(prev_ts)}; series unchanged", file=sys.stderr)
        return 0
    content_hash = hashlib.sha256(raw).hexdigest()
    rec_head = _git_head() or "unknown"
    key = (rec_head, rep["graph_commit_sha"], rep["suite_version"], content_hash)
    line = {
        "ts_utc": _iso_z(datetime.now(timezone.utc)),
        "recorded_head": rec_head,
        "snapshot_commit": rep["graph_commit_sha"],
        "suite_version": rep["suite_version"],
        "hit_rate": rep["aggregate"],
        "mean_cost": {m: round(sum((c.get("mean_cost") or 0) for c in v.values()) / len(v))
                      for m, v in rep["aggregate"].items()},
        "report_content_hash": content_hash,
    }

    status, msg = append_series_record(sp, series_lock_path(), line,
                                       KG_IDENTITY_FIELDS, key, mode="skip-on-match")
    if status == "SKIPPED" and force:
        # --force overrides the dedupe skip: append regardless (pinned test semantics).
        status, msg = append_series_record(sp, series_lock_path(), line,
                                           ("__none__",), ("\u0000",), mode="skip-on-match")
    print(f"{status}: {msg}", file=sys.stderr if status != "RECORDED" else sys.stdout)
    return 0


def assert_segments(lines: list[dict], segments: dict) -> None:
    """Render-side segmentation helper (suite-version segments)."""
    built: dict[str, list] = {}
    for l in lines:
        built.setdefault(l.get("suite_version"), []).append(l)
    assert built == segments, f"segment mismatch: {built} != {segments}"


# ============================================================ feature-008 budget series

BUDGET_IDENTITY_FIELDS = ("git_head", "standing_bytes", "artifact_total_bytes", "breach_set_hash")


def _breach_set_hash(breaches: list[dict]) -> str:
    payload = json.dumps(sorted(json.dumps(b, sort_keys=True) for b in breaches),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def record_budget_series(now: datetime | None = None, ctx: dict | None = None) -> tuple[str, str]:
    """Feature-008 producer: one keep-last record per render (called from run_render).

    Uses the caller's collected context when supplied (sandbox-safe); staleness is
    a deliberate no-op on this path (no mtime analogue); duplicates surface via the
    pane's SERIES-DUPLICATE predicate, never a silent drop. The stored record
    carries `breach_set_hash` (the identity's fingerprint component) alongside the
    human-readable `breaches` list.
    """
    ctx = ctx if ctx is not None else context_size()
    if ctx.get("standing_missing") or not isinstance(ctx.get("standing_bytes"), int):
        return ("SKIPPED", "standing instructions file absent/unreadable — no record (BUDGET-MISSING-STANDING)")
    if not isinstance(ctx.get("total_artifact_bytes"), int):
        return ("SKIPPED", "artifact total unavailable — no record")
    head = _git_head() or "unknown"
    bhash = _breach_set_hash(ctx["breaches"])
    identity = (head, ctx["standing_bytes"], ctx["total_artifact_bytes"], bhash)
    line = {
        "ts_utc": _iso_z(now or datetime.now(timezone.utc)),
        "git_head": head,
        "standing_bytes": ctx["standing_bytes"],
        "artifact_total_bytes": ctx["total_artifact_bytes"],
        "breach_set_hash": bhash,
        "breaches": ctx["breaches"],
    }
    return append_series_record(budget_series_path(), budget_series_lock_path(), line,
                                BUDGET_IDENTITY_FIELDS, identity, mode="keep-last")


def _series_uncommitted() -> bool:
    """SERIES-UNCOMMITTED predicate: series file modified or untracked per git status."""
    sp = budget_series_path()
    if not sp.exists():
        return False
    try:
        rel = str(sp.relative_to(REPO_ROOT))
        out = subprocess.run(["git", "status", "--porcelain", rel], cwd=str(REPO_ROOT),
                             capture_output=True, text=True, timeout=10)
        return bool(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _series_duplicates() -> int:
    """SERIES-DUPLICATE count: retained lines sharing an identity key (>0 = banner)."""
    lines, _ = _read_series_lines(budget_series_path())
    seen: dict[tuple, int] = {}
    for l in lines:
        k = tuple(l.get(f) for f in BUDGET_IDENTITY_FIELDS)
        seen[k] = seen.get(k, 0) + 1
    return sum(1 for n in seen.values() if n > 1)


def budget_repo_banners(ctx: dict) -> list[dict]:
    """Feature-008 repo-level banners: breach, series data-quality, missing standing."""
    banners: list[dict] = []
    if ctx.get("standing_missing"):
        banners.append({"name": "BUDGET-MISSING-STANDING",
                        "detail": ".github/copilot-instructions.md absent — standing size unknown"})
    for b in ctx.get("breaches", []):
        banners.append({"name": "BUDGET-BREACH",
                        "detail": (f"{b['artifact']} ({b['family']}): {b['bytes']:,} B > budget "
                                   f"{b['budget']:,} B — disposition in the next round's artifact"),
                        "ticket": b["ticket"]})
    if _series_uncommitted():
        banners.append({"name": "SERIES-UNCOMMITTED",
                        "detail": "context_budget_series.jsonl has uncommitted changes — "
                                  "commit the render to advance the tracked trend"})
    dup = _series_duplicates()
    if dup:
        banners.append({"name": "SERIES-DUPLICATE",
                        "detail": f"{dup} identity key(s) with duplicate retained lines — "
                                  "resolve the series before trusting the trend"})
    return banners


# ============================================================ CLI


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SDLC measurement pane generator (feature 007)")
    ap.add_argument("--record-kg-run", action="store_true",
                    help="record output/kg/evaluation_report.json onto the tracked KG series")
    ap.add_argument("--force", action="store_true",
                    help="with --record-kg-run: override the dedupe-key skip")
    args = ap.parse_args(argv)
    if args.record_kg_run:
        return record_kg_run(force=args.force)
    try:
        changed = run_render()
    except OSError as e:
        print(f"ERROR: unreadable input: {e}", file=sys.stderr)
        return 1
    print("PANE: written (content changed)" if changed else "PANE: unchanged (write skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
