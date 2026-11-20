"""Co-edit edge derivation from git history (research R5).

Scans `git log --name-status -M` for non-merge commits, groups Python files
touched per commit, and emits C(n,2) pairs for commits at or under the bulk
threshold (--coedit-max-files, default 20). Pair weight = number of linking
commits capped at COEDIT_WEIGHT_CAP (5). Rename records (status R) feed node
identity migration. Git missing/unreadable -> KG-GIT-UNAVAILABLE; shallow
history -> KG-GIT-SHALLOW; both fail soft (import/call graph unaffected).

Co-edit pairs link Python files only in v1 (spec Edge Case "touched-file
set"): non-Python files count toward the bulk threshold but never form pair
endpoints.
"""
import os
import subprocess

from src.kg import report as kg_report

DEFAULT_MAX_FILES = 20
COEDIT_WEIGHT_CAP = 5

# Module-level alias (tests + callers use coedit.KG_GIT_UNAVAILABLE directly).
KG_GIT_UNAVAILABLE = kg_report.KG_GIT_UNAVAILABLE


class CommitScanOutcome:
    __slots__ = ("commits", "failures")

    def __init__(self, commits, failures):
        self.commits = commits
        self.failures = failures


def _run_git_log(repo_root):
    cmd = [
        "git", "log", "--name-status", "-M", "--no-merges",
        "--pretty=format:%H", "--",
    ]
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc


def scan_commit_history(repo_root, max_files=DEFAULT_MAX_FILES):
    """Return CommitScanOutcome with per-commit python file sets + renames."""
    if not os.path.isdir(os.path.join(repo_root, ".git")):
        return CommitScanOutcome([], [kg_report.emit_failure(
            kg_report.KG_GIT_UNAVAILABLE,
            f"{repo_root}: no .git directory; co-edit edges disabled for this run",
        )])
    try:
        proc = _run_git_log(repo_root)
    except OSError as exc:
        return CommitScanOutcome([], [kg_report.emit_failure(
            kg_report.KG_GIT_UNAVAILABLE, f"git invocation failed: {exc}"
        )])
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if "shallow" in stderr.lower():
            code = kg_report.KG_GIT_SHALLOW
        else:
            code = kg_report.KG_GIT_UNAVAILABLE
        return CommitScanOutcome([], [kg_report.emit_failure(
            code, f"git log failed ({proc.returncode}): {stderr[:200]}"
        )])

    commits, failures = [], []
    current = None
    seen_shallow_hint = False
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            if current is not None:
                commits.append(current)
            current = {"sha": line[:8], "py_files": [], "renames": []}
            continue
        if current is None:
            continue
        parts = line.split("\t")
        status = parts[0]
        path = parts[-1]
        if status.startswith("R") and len(parts) >= 3:
            old, new = parts[1], parts[2]
            current["renames"].append((old, new))
            if new.lower().endswith(".py"):
                current["py_files"].append(new)
            continue
        if path.lower().endswith(".py"):
            if status in ("A", "M", "C", "U"):
                current["py_files"].append(path)
    if current is not None:
        commits.append(current)
    if seen_shallow_hint:
        failures.append(kg_report.emit_failure(
            kg_report.KG_GIT_SHALLOW, "history flagged shallow; co-edit coverage limited"
        ))
    return CommitScanOutcome(commits, failures)


def derive_coedit_pairs(commits, max_files=DEFAULT_MAX_FILES):
    """Fold commit file sets into weighted pairs.

    Returns (commits_used, commits_skipped_bulk, pairs, renames) where pairs
    maps (fileA, fileB) sorted tuples -> capped commit count, and renames is
    a list of (old_path, new_path) observations.
    """
    used = skipped = 0
    pairs = {}
    renames = []
    for commit in commits:
        py = sorted(set(commit.get("py_files", [])))
        renames.extend(commit.get("renames", []))
        if len(py) > max_files:
            skipped += 1
            continue
        if len(py) < 2:
            used += 1 if py else 0
            continue
        used += 1
        for i in range(len(py)):
            for j in range(i + 1, len(py)):
                key = (py[i], py[j])
                pairs[key] = min(pairs.get(key, 0) + 1, COEDIT_WEIGHT_CAP)
    return used, skipped, pairs, renames
