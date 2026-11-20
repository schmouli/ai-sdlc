"""Core entities for the Code Knowledge Graph.

Implements the data contract in specs/003-code-knowledge-graph/data-model.md:
Node, Edge, GraphSnapshot, UpdateState + atomic snapshot IO with deterministic
(byte-stable) serialization. All constraints quoted from the data model:
node id schemes (f:<path> / s:<path>#<qualified-name>), status enums, edge
id format <type>:<src>:<dst>, coedit weight cap 5, bounded FIFO of 500.
"""
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1

SNAPSHOT_FILENAME = "graph_snapshot.json"
PREV_SNAPSHOT_FILENAME = "graph_snapshot_prev.json"
UPDATE_STATE_FILENAME = "update_state.json"

DEFAULT_KG_DIR = os.path.join("output", "kg")

STATS_KEYS = (
    "files_active",
    "symbols_active",
    "imports",
    "calls",
    "coedit_pairs",
    "retired_nodes",
    "parse_failures",
    "coedit_commits_used",
    "coedit_commits_skipped_bulk",
)

EDGE_TYPES = ("import", "calls", "coedit")
NODE_KINDS = ("file", "symbol")
NODE_STATUS = ("active", "retired")
SYMBOL_KINDS = ("function", "method", "class", "assignment")
EDGE_CONFIDENCE = ("high", "medium", "external")
CALL_TIERS = ("name", "method")

COEDIT_WEIGHT_CAP = 5
PROCESSED_COMMITS_MAX = 500

_EASTERN = ZoneInfo("America/New_York")


def now_eastern_iso():
    """Repo convention: timestamps in America/New_York (ISO-8601)."""
    return datetime.now(_EASTERN).isoformat(timespec="seconds")


def file_node_id(path):
    return "f:" + path.replace("\\", "/").lower()


def symbol_node_id(path, qualified_name):
    return "s:" + path.replace("\\", "/").lower() + "#" + qualified_name


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Node:
    id: str
    kind: str
    path: str
    name: str
    content_hash: str = ""
    display_text: str = ""
    status: str = "active"
    qualified_name: str = ""
    symbol_kind: str = ""
    renamed_from: str = ""
    renamed_to: str = ""
    first_seen_commit: str = ""

    def to_dict(self):
        d = {
            "id": self.id,
            "kind": self.kind,
            "path": self.path.replace("\\", "/").lower(),
            "name": self.name,
            "content_hash": self.content_hash,
            "display_text": self.display_text,
            "status": self.status,
        }
        if self.kind == "symbol":
            d["qualified_name"] = self.qualified_name
            d["symbol_kind"] = self.symbol_kind
        if self.first_seen_commit:
            d["first_seen_commit"] = self.first_seen_commit
        if self.renamed_from:
            d["renamed_from"] = self.renamed_from
        if self.renamed_to:
            d["renamed_to"] = self.renamed_to
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"],
            kind=d["kind"],
            path=d["path"],
            name=d["name"],
            content_hash=d.get("content_hash", ""),
            display_text=d.get("display_text", ""),
            status=d.get("status", "active"),
            qualified_name=d.get("qualified_name", ""),
            symbol_kind=d.get("symbol_kind", ""),
            renamed_from=d.get("renamed_from", ""),
            renamed_to=d.get("renamed_to", ""),
            first_seen_commit=d.get("first_seen_commit", ""),
        )


@dataclass
class Edge:
    id: str
    type: str
    src: str
    dst: str
    confidence: str = "high"
    weight: float = 1.0
    tier: str = ""
    provenance: list = field(default_factory=list)

    def to_dict(self):
        d = {
            "id": self.id,
            "type": self.type,
            "src": self.src,
            "dst": self.dst,
            "confidence": self.confidence,
            "weight": self.weight,
        }
        if self.type == "calls" and self.tier:
            d["tier"] = self.tier
        if self.provenance:
            d["provenance"] = self.provenance[:10]
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"],
            type=d["type"],
            src=d["src"],
            dst=d["dst"],
            confidence=d.get("confidence", "high"),
            weight=d.get("weight", 1.0),
            tier=d.get("tier", ""),
            provenance=list(d.get("provenance", [])),
        )


def empty_stats():
    return {key: 0 for key in STATS_KEYS}


def compute_stats(nodes, edges, parse_failures=0, coedit_commits_used=0, coedit_commits_skipped_bulk=0):
    stats = empty_stats()
    for n in nodes:
        if n.kind == "file" and n.status == "active":
            stats["files_active"] += 1
        elif n.kind == "symbol":
            if n.status == "active":
                stats["symbols_active"] += 1
            else:
                stats["retired_nodes"] += 1
        elif n.status != "active":
            stats["retired_nodes"] += 1
    for e in edges:
        if e.type == "import":
            stats["imports"] += 1
        elif e.type == "calls":
            stats["calls"] += 1
        elif e.type == "coedit":
            stats["coedit_pairs"] += 1
    stats["parse_failures"] = parse_failures
    stats["coedit_commits_used"] = coedit_commits_used
    stats["coedit_commits_skipped_bulk"] = coedit_commits_skipped_bulk
    return stats


@dataclass
class GraphSnapshot:
    built_at: str
    commit_sha: str
    stats: dict
    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    built_from_commit_count: int = 0
    schema_version: int = SCHEMA_VERSION

    def to_dict(self):
        nodes_sorted = sorted(self.nodes, key=lambda n: n.id)
        edges_sorted = sorted(self.edges, key=lambda e: e.id)
        return {
            "schema_version": self.schema_version,
            "built_at": self.built_at,
            "commit_sha": self.commit_sha,
            "built_from_commit_count": self.built_from_commit_count,
            "stats": self.stats,
            "nodes": [n.to_dict() for n in nodes_sorted],
            "edges": [e.to_dict() for e in edges_sorted],
        }

    @classmethod
    def from_dict(cls, d):
        version = d.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported snapshot schema_version: {version!r} (loader supports {SCHEMA_VERSION})"
            )
        return cls(
            built_at=d.get("built_at", ""),
            commit_sha=d.get("commit_sha", ""),
            stats=d.get("stats", empty_stats()),
            nodes=[Node.from_dict(x) for x in d.get("nodes", [])],
            edges=[Edge.from_dict(x) for x in d.get("edges", [])],
            built_from_commit_count=d.get("built_from_commit_count", 0),
            schema_version=version,
        )


def _atomic_write_json(path, payload):
    """Write temp file in same dir then os.replace (data-model atomicity rule).

    Raises nothing on failure path: caller decides. Guarantees the previous
    file at `path` survives any interrupted write (FR-014).
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def write_snapshot(snapshot, out_path):
    """Persist snapshot atomically; keep one generation as *_prev.json."""
    out_path = os.path.abspath(out_path)
    prev_path = os.path.join(os.path.dirname(out_path), PREV_SNAPSHOT_FILENAME)
    if os.path.exists(out_path):
        shutil.copyfile(out_path, prev_path)
    _atomic_write_json(out_path, snapshot.to_dict())
    return out_path


def load_snapshot(path):
    """Load a snapshot, raising ValueError with a schema reason on mismatch."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return GraphSnapshot.from_dict(raw)


@dataclass
class UpdateState:
    last_commit_sha: str = ""
    updated_at: str = ""
    processed_commits: list = field(default_factory=list)

    def to_dict(self):
        return {
            "last_commit_sha": self.last_commit_sha,
            "updated_at": self.updated_at,
            "processed_commits": self.processed_commits[-PROCESSED_COMMITS_MAX:],
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            last_commit_sha=d.get("last_commit_sha", ""),
            updated_at=d.get("updated_at", ""),
            processed_commits=list(d.get("processed_commits", [])),
        )

    def record_commit(self, sha, files_touched):
        """Bounded FIFO: newest last, at most PROCESSED_COMMITS_MAX entries."""
        self.processed_commits.append(
            {"sha": sha, "files_touched": list(files_touched), "folded_at": now_eastern_iso()}
        )
        if len(self.processed_commits) > PROCESSED_COMMITS_MAX:
            self.processed_commits = self.processed_commits[-PROCESSED_COMMITS_MAX:]


def write_update_state(state, kg_dir=DEFAULT_KG_DIR):
    path = os.path.join(os.path.abspath(kg_dir), UPDATE_STATE_FILENAME)
    _atomic_write_json(path, state.to_dict())
    return path


def load_update_state(kg_dir=DEFAULT_KG_DIR):
    path = os.path.join(os.path.abspath(kg_dir), UPDATE_STATE_FILENAME)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return UpdateState.from_dict(json.load(fh))
