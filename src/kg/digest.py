"""Graph digest renderer (feature 005, US3).

Produces the bounded, freshness-stamped, structural-facts-only companion artifact
(``output/kg/digest_for_reviews.md``) consumed by EGM review rounds whose consult
mode is ``digest``. Contract: specs/005-ai-sdlc-wiring/contracts/digest-artifact.md.

Design decisions (research.md D-6):
- ordering: file-major, PageRank-descending top-25 with top import partners,
  then the top-10 co-edit pairs (no community detection).
- PageRank is computed in-render via ``src.kg.pagerank.compute_pagerank`` over
  the file-level import graph (deterministic; synthetic dict-shaped snapshots
  and real kg snapshots are both accepted; cycles terminate by construction).
- content: structural facts only " paths, names, counts. No source code,
  no findings, no credentials (SC-011).
- cap: deterministic truncation at ``digest_max_bytes`` from the manifest
  token_budgets, truncation marked ``(truncated)``.
"""

from __future__ import annotations

import json
from pathlib import Path


def _coerce_snapshot(snapshot: dict) -> dict:
    """Normalize a synthetic-dict or real-kg snapshot into the internal shape:
    {files: {path: {pagerank, import_partners}}, coedit_pairs: [{a, b, weight}],
     commit_sha, built_at}."""
    if "files" in snapshot:
        return {
            "files": snapshot["files"],
            "coedit_pairs": snapshot.get("coedit_pairs") or [],
            "commit_sha": snapshot.get("commit_sha", ""),
            "built_at": snapshot.get("built_at", ""),
        }
    # real kg snapshot: nodes list + edges list
    from src.kg.pagerank import compute_pagerank

    nodes = snapshot.get("nodes") or []
    edges = snapshot.get("edges") or []
    file_ids = {
        n["id"]
        for n in nodes
        if n.get("kind") == "file" and n.get("status") == "active"
        and not (n.get("path") or "").startswith("external/")
    }
    path_of = {
        n["id"]: (n.get("path") or n.get("name") or n["id"])
        for n in nodes
        if n.get("kind") == "file" and n.get("status") == "active"
        and not (n.get("path") or "").startswith("external/")
    }

    weighted_edges = []
    for e in edges:
        if e.get("type") != "import":
            continue
        src, dst = e.get("src"), e.get("dst")
        w = float(e.get("weight", 1.0))
        src_file = "f:" + src.split("#", 1)[0].replace("s:", "", 1) if src and src.startswith("s:") else src
        dst_file = "f:" + dst.split("#", 1)[0].replace("s:", "", 1) if dst and dst.startswith("s:") else dst
        if src_file in file_ids and dst_file in file_ids:
            weighted_edges.append((src_file, dst_file, w))

    scores = compute_pagerank(sorted(file_ids), weighted_edges)

    files = {}
    for fid, pr in scores.items():
        files[path_of.get(fid, fid)] = {"pagerank": pr}

    # import partners carried with the importer side's own PageRank so the
    # renderer can order each list by the partner's rank (digest-artifact
    # contract: "ordered by the import side's own PageRank descending").
    import_partners: dict[str, list[tuple[str, float]]] = {}
    for src_f, dst_f, _w in weighted_edges:
        pa, pb = path_of.get(src_f, src_f), path_of.get(dst_f, dst_f)
        import_partners.setdefault(pa, []).append((pb, scores.get(dst_f if dst_f in file_ids else dst_f, 0.0)))

    for path, partners in import_partners.items():
        if path in files:
            files[path]["imports"] = partners

    coedit_pairs = []
    for e in edges:
        if e.get("type") != "coedit":
            continue
        a = path_of.get(e.get("src"), e.get("src"))
        b = path_of.get(e.get("dst"), e.get("dst"))
        coedit_pairs.append({"a": a, "b": b, "weight": int(float(e.get("weight", 0)))})

    return {
        "files": files,
        "coedit_pairs": coedit_pairs,
        "commit_sha": snapshot.get("commit_sha", ""),
        "built_at": snapshot.get("built_at", ""),
    }


def render_digest(snapshot: dict, budgets: dict, head_sha: str | None = None) -> str:
    """Render the review digest. Accepts a synthetic-dict snapshot (files dict)
    or a real kg snapshot (nodes+edges lists). See module docstring.

    Budget semantics (pinned by tests/kg/test_digest.py): ``digest_max_bytes``
    bounds the WHOLE rendered document; at budget the renderer stops at the last
    complete line and appends ``(truncated)``. The ``bytes: <n> / cap <cap>``
    header line reports the final document byte count (self-referential value
    resolved by a bounded fixed-point substitution - deterministic).
    """
    cap = int(budgets.get("digest_max_bytes", 8192))
    norm = _coerce_snapshot(snapshot)
    commit = norm["commit_sha"]
    built_at = norm["built_at"]

    files = norm["files"]
    ranked = sorted(files.items(), key=lambda kv: (-float(kv[1].get("pagerank", 0.0)), kv[0]))[:25]
    pairs = norm["coedit_pairs"]
    top_pairs = sorted(pairs, key=lambda p: -int(p.get("weight", 0)))[:10]

    header_lines = [
        "# KG Digest for Reviews",
        "",
        f"source_commit: {commit}",
        f"built_at: {built_at}",
        (f"freshness: STALE (snapshot={commit} vs HEAD={head_sha})"
         if head_sha and head_sha != commit else "freshness: FRESH"),
    ]
    body_lines: list[str] = [""]
    body_lines.append("## Top files (PageRank)")
    for path, info in ranked:
        pr = info.get("pagerank", 0.0)
        partners = info.get("imports") or []
        if partners and isinstance(partners[0], (list, tuple)):
            # (path, pagerank) pairs: order by the import side's own PageRank
            partners = [p[0] for p in sorted(partners, key=lambda t: -float(t[1]))]
        partner_txt = ", ".join(partners[:8]) if partners else "(none)"
        body_lines.append(f"- {path} (pagerank {pr:.4g}) " + chr(0x2014) + f" top import partners: {partner_txt}")

    if top_pairs:
        body_lines.append("")
        body_lines.append("## Top co-edit pairs")
        for pp in top_pairs:
            body_lines.append(f"- {pp['a']} " + chr(0x2194) + f" {pp['b']} ({pp.get('weight', 0)} shared commits)")
    body_text = chr(10).join(body_lines) + ("" if body_lines[-1] == "" else chr(10))

    truncated = False
    placeholder = "bytes: NNNN / cap " + str(cap)
    candidate = ""
    for _attempt in range(3):
        cand_lines = header_lines + [placeholder]
        if truncated:
            cand_lines.append("[TRUNCATED at budget; content below is a deterministic prefix]")
        candidate = chr(10).join(cand_lines) + chr(10) + body_text
        if len(candidate.encode("utf-8")) > cap:
            truncated = True
            candidate = _truncate_utf8(candidate, cap)

    # resolve the self-referential bytes value (digit-width stable via zero pad)
    n = len(candidate.encode("utf-8"))
    for _pass in range(3):
        line_b = f"bytes: {n:04d} / cap {cap}"
        final = candidate.replace(placeholder, line_b)
        n2 = len(final.encode("utf-8"))
        if n2 == n:
            break
        n = n2
    return final


def _truncate_utf8(text: str, cap: int) -> str:
    """Deterministic UTF-8-safe truncation at the last complete line + marker."""
    encoded = text.encode("utf-8")
    trimmed = encoded[:cap]
    out = ""
    while trimmed:
        try:
            out = trimmed.decode("utf-8")
            break
        except UnicodeDecodeError:
            trimmed = trimmed[:-1]
    if chr(10) in out:
        out = out[: out.rfind(chr(10)) + 1]
    return out + "(truncated)" + chr(10)


def load_snapshot(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
