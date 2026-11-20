"""Graph build + incremental update orchestration (research R8/R9/R13).

build_full: walk repo Python files, parse each (parser.py), derive co-edit
pairs (coedit.py), dedup nodes/edges (FR-006), assemble the snapshot with
reconciled stats + FR-007 deduplication_summary, write atomically (models.py).

incremental_update: load update_state (absent -> full rebuild; mismatch ->
KG-STATE-MISMATCH + treated as absent), scan the git-log delta after the last
folded SHA, re-parse only touched files, migrate renames (file id moves with
content per R8; symbol rename -> new node + retired provenance), retire
deleted files' nodes and their edges, write snapshot THEN advance state
(idempotency: no new commits -> zero-change no-op with byte-identical files).

Deterministic throughout: ordered walks, sorted collections, tie-breaks by id.
"""
import json
import os
import subprocess
from collections import defaultdict

from src.kg import coedit as kg_coedit
from src.kg import models as kg_models
from src.kg import parser as kg_parser
from src.kg import report as kg_report

PYTHON_EXTS = (".py",)
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".pytest_cache",
             ".specify", ".github", ".vscode", "mlruns", "sessions"}


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_python_files(repo_root, sample=None, import_graph=None):
    """Deterministic repo walk; optional import-degree-ranked sample (R13)."""
    files = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            if fn.endswith(PYTHON_EXTS):
                rel = os.path.relpath(os.path.join(dirpath, fn), repo_root)
                files.append(rel.replace("\\", "/"))
    files.sort()
    if sample is not None and import_graph:
        degree = defaultdict(int)
        for src, dst in import_graph:
            degree[src] += 1
            degree[dst] += 1
        ranked = sorted(files, key=lambda f: (-degree.get(f, 0), f))
        return sorted(ranked[:sample])
    return files


def _read_source(repo_root, rel_path):
    try:
        with open(os.path.join(repo_root, rel_path), "r", encoding="utf-8") as fh:
            return fh.read(), None
    except UnicodeDecodeError as exc:
        return None, kg_report.emit_failure(
            kg_report.KG_PARSE_ENCODING, f"{rel_path}: {exc}"
        )
    except OSError as exc:
        return None, kg_report.emit_failure(
            kg_report.KG_PARSE_SYNTAX, f"{rel_path}: unreadable ({exc})"
        )


# ---------------------------------------------------------------------------
# Node/edge assembly
# ---------------------------------------------------------------------------

def _display_text(name, rel_path):
    """Retrieval text: identifier + path segments (no docstring).

    Implement-time refinement of research R6 (evidence: including the
    docstring head inflated the token-set denominator and docked class-name
    queries below helpers with short docstrings; identifier+path tokens give
    precise anchoring). IMPORTANT: case is preserved - tokenize() performs
    camelCase splitting on the raw identifier BEFORE lowering, so
    'AdaptiveThresholdController' yields adaptive/threshold/controller
    subtokens; lowercased text would destroy those boundaries.
    """
    segs = [s.replace("\\", "/") for s in rel_path.split("/")]
    return " ".join([name] + segs)


def _short_sha(repo_root):
    try:
        proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root,
                              capture_output=True, text=True)
        return (proc.stdout or "").strip() or "unknown"
    except OSError:
        return "unknown"


def build_graph_from_files(repo_root, rel_paths, parse_results=None):
    """Parse the given files into nodes + import/call edges (no coedit).

    Two-phase: (1) all file/symbol nodes created from every parsed file;
    (2) edges emitted only when both endpoints exist as nodes. This prevents
    phantom targets (dynamically/nested-defined symbols) and keeps the
    calls=symbol<->symbol contract (data-model producer obligations).
    parse_results: optional pre-computed {rel_path: ParseResult} (incremental).
    Returns (nodes, edges, parse_failures, results_by_path).
    """
    nodes_by_id = {}
    pending_imports = []
    pending_calls = []
    parse_failures = []
    if parse_results is None:
        parse_results = {}
    sources = {}
    for rel in rel_paths:
        result = parse_results.get(rel)
        source = None
        if result is None:
            source, failure = _read_source(repo_root, rel)
            if source is None:
                parse_failures.append(failure)
                continue
            result = kg_parser.parse_source(source, rel, repo_root=repo_root)
        else:
            # Precomputed result (incremental path): still read the current
            # source so the file node's content_hash reflects live content —
            # convergence T033 (a body edit must mark the file node touched
            # even when no def spans changed).
            source, failure = _read_source(repo_root, rel)
            if source is None:
                parse_failures.append(failure)
                continue
        parse_results[rel] = result
        parse_failures.extend(result.failures)
        sources[rel] = source

        lower_rel = rel.lower()
        file_id = kg_models.file_node_id(rel)
        if file_id not in nodes_by_id:
            f_hash = kg_models.content_hash(source) if source is not None else ""
            nodes_by_id[file_id] = kg_models.Node(
                id=file_id, kind="file", path=lower_rel, name=os.path.basename(rel),
                content_hash=f_hash,
                display_text=_display_text(os.path.basename(rel), rel),
                status="active",
            )
        for d in result.defs:
            sym_id = kg_models.symbol_node_id(rel, d.qualified_name)
            if sym_id not in nodes_by_id:
                nodes_by_id[sym_id] = kg_models.Node(
                    id=sym_id, kind="symbol", path=lower_rel, name=d.qualified_name.split(".")[-1],
                    qualified_name=d.qualified_name, symbol_kind=d.kind,
                    content_hash=kg_models.content_hash(f"{d.qualified_name}:{d.line}:{d.end_line}"),
                    display_text=_display_text(d.qualified_name, rel),
                    status="active",
                )
        for imp in result.imports:
            pending_imports.append((file_id, imp, rel))
        for call in result.calls:
            pending_calls.append((file_id, call, rel))

    # external module nodes (import context)
    for _file_id, imp, _rel in pending_imports:
        target = imp.resolved_target
        if not target:
            continue
        is_repo_file = target.lower().endswith(".py") and "/" in target
        if not is_repo_file:
            ext_id = "f:external/" + target.replace(".", "/").lower()
            if imp.module and ext_id not in nodes_by_id:
                nodes_by_id[ext_id] = kg_models.Node(
                    id=ext_id, kind="file",
                    path="external/" + target.replace(".", "/").lower(),
                    name=target, status="active", content_hash="external",
                )

    # ---- phase 2: edges only between existing nodes ----
    edges_by_id = {}
    for file_id, imp, rel in pending_imports:
        target = imp.resolved_target
        if not target:
            continue
        if target.lower().endswith(".py") and "/" in target:
            dst = None
            repo_target = file_node_ids_by_path(nodes_by_id).get(target.lower())
            if repo_target is not None:
                dst = repo_target
        else:
            dst = "f:external/" + target.replace(".", "/").lower()
        if dst is None or dst not in nodes_by_id:
            continue
        edge_id = f"import:{file_id}:{dst}"
        edges_by_id.setdefault(edge_id, kg_models.Edge(
            id=edge_id, type="import", src=file_id, dst=dst,
            confidence="high" if dst.startswith("f:") and not dst.startswith("f:external") else "external",
        ))
    for file_id, call, rel in pending_calls:
        if call.resolved_target is None:
            continue
        target_file, target_sym = call.resolved_target
        if not str(target_file).lower().endswith(".py"):
            continue
        scope = call.context_scope
        # calls must originate from a symbol (data-model: calls=symbol<->symbol);
        # module-level calls are import-time behavior already carried by import edges
        if not scope or scope == "module":
            continue
        src_sym = kg_models.symbol_node_id(rel, scope)
        if src_sym not in nodes_by_id:
            continue
        dst_sym = kg_models.symbol_node_id(target_file, target_sym)
        if dst_sym not in nodes_by_id:
            continue  # phantom target (dynamic/nested definition): no edge
        edge_id = f"calls:{src_sym}:{dst_sym}"
        if edge_id not in edges_by_id:
            edges_by_id[edge_id] = kg_models.Edge(
                id=edge_id, type="calls", src=src_sym, dst=dst_sym,
                confidence="high" if call.tier == "name" else "medium",
                tier=call.tier,
            )
    return list(nodes_by_id.values()), list(edges_by_id.values()), parse_failures, parse_results


def file_node_ids_by_path(nodes_by_id):
    return {n.path: n.id for n in nodes_by_id.values() if n.kind == "file" and n.status == "active"}


# ---------------------------------------------------------------------------
# Dedup + stats (FR-006 / FR-007)
# ---------------------------------------------------------------------------

def dedup_and_stats(nodes, edges, parse_failures, coedit_used, coedit_skipped):
    nodes_before = len(nodes)
    edges_before = len(edges)
    seen_nodes = {}
    for n in nodes:
        seen_nodes.setdefault(n.id, n)
    seen_edges = {}
    for e in edges:
        prev = seen_edges.get(e.id)
        if prev is None:
            seen_edges[e.id] = e
        elif e.type == "coedit" and prev.weight < kg_models.COEDIT_WEIGHT_CAP:
            prev.weight = min(prev.weight + e.weight, kg_models.COEDIT_WEIGHT_CAP)
    merged_nodes = list(seen_nodes.values())
    merged_edges = list(seen_edges.values())
    stats = kg_models.compute_stats(
        merged_nodes, merged_edges,
        parse_failures=len(parse_failures),
        coedit_commits_used=coedit_used,
        coedit_commits_skipped_bulk=coedit_skipped,
    )
    summary = {
        "duplicate_file_nodes_merged": nodes_before - len(merged_nodes),
        "duplicate_symbol_nodes_merged": 0,
        "duplicate_edges_merged": edges_before - len(merged_edges),
        "nodes_before_dedup": nodes_before,
        "edges_before_dedup": edges_before,
    }
    return merged_nodes, merged_edges, stats, summary


# ---------------------------------------------------------------------------
# Full build
# ---------------------------------------------------------------------------

def build_full(repo_root=".", out_path=None, sample=None, coedit_max_files=kg_coedit.DEFAULT_MAX_FILES,
               commit_count=None):
    out_path = out_path or os.path.join(kg_models.DEFAULT_KG_DIR, kg_models.SNAPSHOT_FILENAME)
    failures = []

    # co-edit scan (fail soft)
    outcome = kg_coedit.scan_commit_history(repo_root, max_files=coedit_max_files)
    failures.extend(outcome.failures)
    used, skipped, pairs, renames = kg_coedit.derive_coedit_pairs(
        outcome.commits, max_files=coedit_max_files
    )

    # cheap pre-scan of import graph for sample ranking (R13); None when full
    import_graph_for_rank = None
    if sample is not None:
        import_graph_for_rank = []
        for rel in discover_python_files(repo_root):
            source, fail = _read_source(repo_root, rel)
            if source is None:
                continue
            res = kg_parser.parse_source(source, rel, repo_root=repo_root)
            for imp in res.imports:
                if imp.resolved_target and imp.resolved_target.lower().endswith(".py") and "/" in imp.resolved_target:
                    import_graph_for_rank.append((kg_models.file_node_id(rel),
                                                  kg_models.file_node_id(imp.resolved_target)))

    rel_paths = discover_python_files(repo_root, sample=sample, import_graph=import_graph_for_rank)
    nodes, edges, parse_failures, _ = build_graph_from_files(repo_root, rel_paths)

    allowed = set(rel_paths) if sample is not None else None
    node_paths = {n.path for n in nodes if n.kind == "file" and n.status == "active"}
    for (a, b), weight in sorted(pairs.items()):
        if allowed is not None and (a not in allowed or b not in allowed):
            continue  # sample mode: pairs scoped to the sampled files (FR-005)
        if a.lower() not in node_paths or b.lower() not in node_paths:
            continue  # file deleted/absent from current tree: no edge without its node
        src_id = kg_models.file_node_id(a)
        dst_id = kg_models.file_node_id(b)
        edge_id = f"coedit:{src_id}:{dst_id}"
        edges.append(kg_models.Edge(
            id=edge_id, type="coedit", src=src_id, dst=dst_id,
            confidence="high", weight=weight,
        ))

    nodes, edges, stats, dedup_summary = dedup_and_stats(
        nodes, edges, parse_failures, used, skipped
    )
    for n in nodes:
        if n.first_seen_commit == "":
            n.first_seen_commit = _short_sha(repo_root)

    snapshot = kg_models.GraphSnapshot(
        built_at=kg_models.now_eastern_iso(),
        commit_sha=_short_sha(repo_root),
        stats=stats,
        nodes=nodes,
        edges=edges,
        built_from_commit_count=len(outcome.commits) if outcome.commits is not None else 0,
    )
    kg_models.write_snapshot(snapshot, out_path)

    state = kg_models.UpdateState(
        last_commit_sha=snapshot.commit_sha, updated_at=snapshot.built_at
    )
    for c in (outcome.commits or []):
        state.record_commit(c["sha"], c["py_files"])
    state_dir = os.path.dirname(os.path.abspath(out_path))
    kg_models.write_update_state(state, state_dir)

    return {
        "mode": "sample" if sample is not None else "full",
        "commit_sha": snapshot.commit_sha,
        "stats": stats,
        "failures": failures + format_parse_failures(parse_failures),
        "deduplication_summary": dedup_summary,
        "snapshot_path": out_path,
    }


def format_parse_failures(parse_failures):
    """Parse-failure records already logged durably; surface in stdout too."""
    records = []
    for f in parse_failures:
        if isinstance(f, dict) and "reason_code" in f:
            records.append(f)
        elif isinstance(f, dict) and "detail" in f:
            records.append(f)
    return records


# ---------------------------------------------------------------------------
# Incremental update
# ---------------------------------------------------------------------------

def _rebuild_edge_id(edge):
    return f"{edge.type}:{edge.src}:{edge.dst}"


def _match_renamed_symbol(old_qualified, new_names):
    """Heuristic successor match for same-file renames (FR-013).

    Ranks candidates by lexical closeness of the leaf name: shared-subtoken
    Jaccard, long common-prefix ratio, and bigram (character-pair) overlap —
    shared_helper vs renamed_helper shares the 'helper' token and the
    '_helper' tail bigram, so near-renames match while unrelated names
    (compute_totals vs calculate_totals share only 'totals'... still matched
    only above the floor) stay apart. Deterministic: ties break
    lexicographically via the sorted iteration.
    """
    from src.kg import similarity as kg_similarity

    old_leaf = old_qualified.split(".")[-1].lower()
    old_toks = kg_similarity.tokenize(old_leaf)
    old_bigrams = _bigrams(old_leaf)
    best, best_key = None, None
    for name in sorted(new_names):
        leaf = name.split(".")[-1].lower()
        toks = kg_similarity.tokenize(leaf)
        bigrams = _bigrams(leaf)
        overlap = len(old_toks & toks)
        union = len(old_toks | toks) or 1
        jaccard = overlap / union
        ratio = _common_prefix_ratio(old_leaf, leaf)
        bigram_j = (len(old_bigrams & bigrams) / len(old_bigrams | bigrams)
                     if old_bigrams and bigrams else 0.0)
        key = (jaccard, bigram_j, ratio)
        # conservative floor: must share a subtoken OR ≥45% bigram structure
        if overlap == 0 and bigram_j < 0.45:
            continue
        if jaccard < 0.20 and ratio < 0.5 and bigram_j < 0.6:
            continue
        if best_key is None or key > best_key:
            best, best_key = name, key
    return best


def _bigrams(text):
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _common_prefix_ratio(a, b):
    run = 0
    for x, y in zip(a, b):
        if x != y:
            break
        run += 1
    return run / max(len(a), len(b), 1)


def export_networkx(snapshot):
    """Emit the snapshot in networkx-native importable form (FR-016, research R2).

    Shape pinned to `nx.readwrite.json_graph.node_link_data()` compatibility:
    directed=True graph with weighted edges; node attributes preserved.
    """
    nodes = []
    for n in sorted(snapshot.nodes, key=lambda x: x.id):
        nodes.append({
            "id": n.id,
            "kind": n.kind,
            "path": n.path,
            "name": n.name,
            "status": n.status,
        })
    links = []
    for e in sorted(snapshot.edges, key=lambda x: x.id):
        links.append({
            "source": e.src,
            "target": e.dst,
            "type": e.type,
            "weight": e.weight,
            "confidence": e.confidence,
        })
    return {
        "directed": True,
        "multigraph": False,
        "graph": {
            "commit_sha": snapshot.commit_sha,
            "built_at": snapshot.built_at,
            "schema_version": snapshot.schema_version,
        },
        "nodes": nodes,
        # networkx>=3.4 reads `edges` (node_link_data default); `links` kept as
        # the legacy alias so older consumers still load the same payload.
        "edges": links,
        "links": links,
    }


def _git_files_changed(repo_root, after_sha):
    """Commits after after_sha: [{sha, py_files, all_files, renames}]."""
    cmd = ["git", "log", "--name-status", "-M", "--no-merges", "--pretty=format:%H",
           f"{after_sha}..HEAD", "--"]
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    if proc.returncode != 0:
        return [], kg_report.emit_failure(
            kg_report.KG_GIT_UNAVAILABLE, f"git log delta failed: {(proc.stderr or '').strip()[:200]}"
        )
    commits = []
    current = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            if current is not None:
                commits.append(current)
            current = {"sha": line[:8], "py_files": [], "all_files": [], "renames": []}
            continue
        if current is None:
            continue
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        current["all_files"].append(path)
        if status.startswith("R") and len(parts) >= 3:
            current["renames"].append((parts[1], parts[2]))
            if parts[2].lower().endswith(".py"):
                current["py_files"].append(parts[2])
        elif path.lower().endswith(".py") and status in ("A", "M", "C"):
            current["py_files"].append(path)
    if current is not None:
        commits.append(current)
    return commits, None


def incremental_update(repo_root=".", out_path=None, coedit_max_files=kg_coedit.DEFAULT_MAX_FILES):
    out_path = out_path or os.path.join(kg_models.DEFAULT_KG_DIR, kg_models.SNAPSHOT_FILENAME)
    kg_dir = os.path.dirname(os.path.abspath(out_path))

    snap_path = out_path
    if not os.path.exists(snap_path):
        result = build_full(repo_root, out_path, sample=None, coedit_max_files=coedit_max_files)
        result["mode"] = "update_fallback_full"
        return result

    snapshot = kg_models.load_snapshot(snap_path)
    # Pre-fold status capture BEFORE any in-place mutation: nodes_by_id aliases
    # snapshot.nodes objects, and rename/retire loops mutate statuses in place —
    # the changes counters must compare against pre-fold state (convergence fix).
    pre_fold_status = {n.id: n.status for n in snapshot.nodes}
    state = kg_models.load_update_state(kg_dir)
    last_sha = state.last_commit_sha if state else snapshot.commit_sha
    state_repaired = False
    if state and state.last_commit_sha != snapshot.commit_sha:
        kg_report.emit_failure(
            kg_report.KG_STATE_MISMATCH,
            f"update_state.last_commit_sha={state.last_commit_sha} != "
            f"snapshot.commit_sha={snapshot.commit_sha}; state re-paired with snapshot",
        )
        state.last_commit_sha = snapshot.commit_sha
        state.updated_at = kg_models.now_eastern_iso()
        kg_models.write_update_state(state, kg_dir)
        state_repaired = True
        last_sha = snapshot.commit_sha

    commits, failure = _git_files_changed(repo_root, last_sha)
    failures = [failure] if failure else []
    if not commits:
        # idempotent no-op: nothing to fold in
        return {
            "mode": "update",
            "commits_consumed": [],
            "changes": {"nodes_added": 0, "nodes_updated": 0, "nodes_retired": 0,
                         "edges_added": 0, "edges_updated": 0, "edges_retired": 0, "renames": []},
            "deduplication_summary": {"duplicate_file_nodes_merged": 0,
                                       "duplicate_symbol_nodes_merged": 0,
                                       "duplicate_edges_merged": 0,
                                       "nodes_before_dedup": len(snapshot.nodes),
                                       "edges_before_dedup": len(snapshot.edges)},
            "stats": snapshot.stats,
            "snapshot_path": out_path,
        }

    # ---- fold commits in ----
    nodes_by_id = {n.id: n for n in snapshot.nodes}
    edges = list(snapshot.edges)
    parse_results = {}
    symbol_renames = {}
    changes_renames = []
    touched_node_ids = set()  # nodes actually changed/added/migrated (T033)
    touched = set()
    for c in commits:
        for old, new in c["renames"]:
            old_id = kg_models.file_node_id(old)
            new_id = kg_models.file_node_id(new)
            node = nodes_by_id.get(old_id)
            if node is not None:
                node.id = new_id
                node.path = new.lower()
                node.name = os.path.basename(new)
                node.renamed_from = old_id
                touched_node_ids.add(node.id)
            # symbol nodes embed the file path: migrate ids + re-parent edges
            old_sym_prefix = "s:" + old.lower() + "#"
            for sym in [n for n in list(nodes_by_id.values())
                        if n.kind == "symbol" and n.id.startswith(old_sym_prefix)]:
                old_sym_id = sym.id
                sym.id = "s:" + new.lower() + sym.id[len(old_sym_prefix):]
                sym.path = new.lower()
                sym.renamed_from = old_sym_id
                touched_node_ids.add(sym.id)
            if node is not None or any(sym.renamed_from for sym in nodes_by_id.values()
                                       if sym.kind == "symbol" and sym.path == new.lower()):
                for e in edges:
                    if e.src == old_id or e.dst == old_id:
                        if e.src == old_id:
                            e.src = new_id
                        if e.dst == old_id:
                            e.dst = new_id
                        e.id = _rebuild_edge_id(e)
                    sym_ids_swapped = False
                    if e.src.startswith(old_sym_prefix):
                        e.src = "s:" + new.lower() + e.src[len(old_sym_prefix):]
                        sym_ids_swapped = True
                    if e.dst.startswith(old_sym_prefix):
                        e.dst = "s:" + new.lower() + e.dst[len(old_sym_prefix):]
                        sym_ids_swapped = True
                    if sym_ids_swapped:
                        e.id = _rebuild_edge_id(e)
            touched.add(new)
        for py in c["py_files"]:
            touched.add(py)
        for anyf in c["all_files"]:
            if anyf.lower().endswith(".py"):
                touched.add(anyf)  # touched OR deleted (D-status only appears in all_files)

    # deleted files: py files referenced in snapshot but no longer on disk
    for rel in sorted(touched):
        exists = os.path.exists(os.path.join(repo_root, rel))
        file_id = kg_models.file_node_id(rel)
        node = nodes_by_id.get(file_id)
        if not exists and node is not None and node.status == "active":
            node.status = "retired"
            touched_node_ids.add(node.id)
            for e in edges:
                if e.src == file_id or e.dst == file_id:
                    e.confidence = "external" if e.type != "coedit" else e.confidence
                    e.weight = 0.0
            # retire symbols owned by the file (prefix match on s:<path>#) and
            # zero import/calls edges touching them (review F4 tombstone contract:
            # retired symbol nodes carry only weight-0 import/calls audit records)
            prefix = "s:" + rel.lower() + "#"
            for sym in nodes_by_id.values():
                if sym.kind == "symbol" and sym.id.startswith(prefix) and sym.status == "active":
                    sym.status = "retired"
                    for e in edges:
                        if (e.src == sym.id or e.dst == sym.id) and e.type != "coedit":
                            e.weight = 0.0
            continue
        if exists:
            source, fail = _read_source(repo_root, rel)
            if source is None:
                failures.append(fail)
                continue  # preservation: leave prior nodes/edges untouched
            result = kg_parser.parse_source(source, rel, repo_root=repo_root)
            parse_results[rel] = result
            # FR-013 symbol-rename detection (convergence T032): same-file renames
            # are symbol-node identity moves. Any prior active symbol of this file
            # whose qualified name is absent from the re-parsed defs is retired with
            # renamed_to provenance once its successor is known (below).
            prefix = "s:" + rel.lower() + "#"
            prior_syms = {
                n.id[len(prefix):]: n
                for n in nodes_by_id.values()
                if n.kind == "symbol" and n.id.startswith(prefix) and n.status == "active"
            }
            new_names = {d.qualified_name for d in result.defs}
            vanished = {q: n for q, n in prior_syms.items() if q not in new_names}
            if vanished and new_names:
                symbol_renames[rel] = (vanished, sorted(new_names))
            elif vanished:
                # defs removed without successor -> retire outright (deletion in file)
                for q, n in vanished.items():
                    n.status = "retired"

    if parse_results:
        new_nodes, new_edges, parse_failures, _ = build_graph_from_files(
            repo_root, sorted(parse_results.keys()), parse_results=parse_results
        )
        failures.extend(parse_failures)
        by_id = {n.id: n for n in new_nodes}
        for nid, n in by_id.items():
            prev = nodes_by_id.get(nid)
            if prev is None:
                n.first_seen_commit = commits[-1]["sha"]
                nodes_by_id[nid] = n
                touched_node_ids.add(nid)
            else:
                if prev.content_hash != n.content_hash:
                    prev.content_hash = n.content_hash
                    prev.display_text = n.display_text
                    touched_node_ids.add(nid)
        new_edge_map = {e.id: e for e in new_edges}
        existing_ids = {e.id for e in edges}
        for eid, e in new_edge_map.items():
            if eid not in existing_ids:
                edges.append(e)

    # FR-013 same-file symbol renames (convergence T032): link successor and
    # retire the predecessor with provenance; re-parent or zero edges touching
    # the retired symbol so no active edge touches a retired node.
    for rel, (vanished, new_names) in symbol_renames.items():
        prefix = "s:" + rel.lower() + "#"
        for old_q, old_node in vanished.items():
            successor = _match_renamed_symbol(old_q, new_names)
            if successor is None:
                old_node.status = "retired"  # def removed, no successor
                for e in edges:
                    if old_node.id in (e.src, e.dst):
                        e.weight = 0.0
                continue
            new_id = prefix + successor
            new_node = nodes_by_id.get(new_id)
            old_node.status = "retired"
            old_node.renamed_to = new_id
            if new_node is not None:
                new_node.renamed_from = old_node.id
            touched_node_ids.add(old_node.id)
            touched_node_ids.add(new_id)
            for e in edges:
                if old_node.id in (e.src, e.dst):
                    if new_node is not None:
                        if e.src == old_node.id:
                            e.src = new_id
                        if e.dst == old_node.id:
                            e.dst = new_id
                        e.id = _rebuild_edge_id(e)
                    else:
                        e.weight = 0.0
            changes_renames.append(
                {"from": old_node.id, "to": new_id}  # contract shape (review F2)
            )

    # fresh co-edit delta pairs from the consumed commits (active nodes only)
    used, skipped, pairs, renames = kg_coedit.derive_coedit_pairs(commits, max_files=coedit_max_files)
    node_paths = {n.path for n in nodes_by_id.values()
                  if n.kind == "file" and n.status == "active"}
    for (a, b), weight in sorted(pairs.items()):
        if a.lower() not in node_paths or b.lower() not in node_paths:
            continue  # endpoint absent from current tree (deleted/never-in-scope)
        src_id = kg_models.file_node_id(a)
        dst_id = kg_models.file_node_id(b)
        edge_id = f"coedit:{src_id}:{dst_id}"
        if not any(e.id == edge_id for e in edges):
            edges.append(kg_models.Edge(id=edge_id, type="coedit", src=src_id, dst=dst_id,
                                        confidence="high", weight=weight))

    node_list = list(nodes_by_id.values())
    merged_nodes, merged_edges, stats, dedup_summary = dedup_and_stats(
        node_list, edges, failures, used, skipped
    )

    new_snapshot = kg_models.GraphSnapshot(
        built_at=kg_models.now_eastern_iso(),
        commit_sha=_short_sha(repo_root),
        stats=stats,
        nodes=merged_nodes,
        edges=merged_edges,
        built_from_commit_count=snapshot.built_from_commit_count + len(commits),
    )
    prev_nodes = pre_fold_status
    added = sum(1 for n in merged_nodes if n.id not in prev_nodes)
    retired_ids = {
        n.id for n in merged_nodes
        if n.id in prev_nodes and prev_nodes[n.id] == "active" and n.status == "retired"
    }
    retired = len(retired_ids)
    added_ids = {n.id for n in merged_nodes if n.id not in prev_nodes}
    # T033: touched-only semantics - nodes whose content/display changed or
    # whose identity migrated via rename. Newly added and retired nodes are
    # reported in their own counters, not double-counted here.
    nodes_updated = len(touched_node_ids - added_ids - retired_ids)
    prev_edge_ids = {e.id for e in snapshot.edges}
    edge_added = sum(1 for e in merged_edges if e.id not in prev_edge_ids)
    changes = {
        "nodes_added": added,
        "nodes_updated": nodes_updated,
        "nodes_retired": retired,
        "edges_added": edge_added,
        "edges_updated": 0,
        "edges_retired": max(0, len(snapshot.edges) - (len(merged_edges) - edge_added) - edge_added),
        "renames": [{"from": kg_models.file_node_id(o), "to": kg_models.file_node_id(nw)}
                     for c in commits for (o, nw) in c["renames"]] + changes_renames,
    }

    kg_models.write_snapshot(new_snapshot, out_path)
    state = state or kg_models.UpdateState()
    for c in commits:
        state.record_commit(c["sha"], c["py_files"])
    state.last_commit_sha = new_snapshot.commit_sha
    state.updated_at = new_snapshot.built_at
    kg_models.write_update_state(state, kg_dir)

    return {
        "mode": "update",
        "commits_consumed": [{"sha": c["sha"], "files_touched": c["py_files"]}
                              for c in commits],
        "changes": changes,
        "deduplication_summary": dedup_summary,
        "stats": stats,
        "snapshot_path": out_path,
    }


def _rekey_edge(edge_id, old, new, position):
    parts = edge_id.split(":")
    if len(parts) == 3 and parts[position] == old:
        parts[position] = new
        return ":".join(parts)
    return edge_id
