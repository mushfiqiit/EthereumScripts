#!/usr/bin/env python3
"""BFS-trace ETH/token flow to and from a source address using SQLite edge indexes.

Standalone script - does not import or depend on any other file in this
repository. It reads the `edges` tables written by
build_block_range_sqlite_index.py (one SQLite file per 7200-block range,
each edges row already storing from_address, to_address, block_number,
asset_type, token_address, raw_value, tx_hash, log_index - see that script
for the schema).

Algorithm: breadth-first search outward from the source address, treating
the transfer graph as undirected for reachability (so both "who sent to the
source" and "who the source sent to" are explored, directly or through
intermediate addresses) up to --max-depth hops, while keeping each edge's
real direction (from_address -> to_address) in the output for visualization.

Each SQLite index is opened exactly once at startup (not reopened per BFS
node), and every address lookup is a single indexed query per open
connection (idx_edges_from / idx_edges_to), so retrieval cost per visited
address is O(sum over ranges of (log rows_in_range + matches_in_range))
instead of rescanning CSV files.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Optional

getcontext().prec = 80

ETH_LABEL = "ETH"
DEFAULT_INDEX_DIR = Path(
    "/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/SqliteEdgeIndexes"
)

EDGE_COLUMNS = (
    "edge_id, from_address, to_address, block_number, source, asset_type, "
    "token_address, raw_value, tx_hash, log_index"
)


def normalize_address(raw: object) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value or value in {"nan", "null", "none"} or not value.startswith("0x") or len(value) < 4:
        return None
    return value


def short_form(address: str) -> str:
    return f"{address[:5]}..{address[-5:]}" if len(address) > 10 else address


def decimal_amount(raw_value: str, decimals: int) -> str:
    try:
        value = Decimal(str(raw_value).strip() or "0")
    except InvalidOperation:
        value = Decimal(0)
    scaled = value / (Decimal(10) ** decimals)
    return format(scaled.normalize(), "f")


def norm_key(name: str) -> str:
    return name.strip().lower()


def pick(row: dict[str, str], *names: str) -> str:
    lookup = {norm_key(k): v for k, v in row.items() if k is not None}
    for name in names:
        if name in lookup and lookup[name] is not None:
            return lookup[name]
    return ""


def load_token_decimals(path: Optional[Path]) -> dict[str, int]:
    if path is None:
        return {}
    decimals_by_token: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return decimals_by_token
        for row in reader:
            token = normalize_address(pick(row, "token_address", "address", "contract_address"))
            decimals_raw = pick(row, "decimals", "token_decimals")
            if token is None or decimals_raw == "":
                continue
            try:
                decimals = int(str(decimals_raw).strip())
            except ValueError:
                continue
            if decimals < 0:
                continue
            decimals_by_token[token] = decimals
    return decimals_by_token


@dataclass
class Node:
    address: str
    label: str
    distance: int
    is_root: bool


@dataclass
class Edge:
    edge_id: str
    from_address: str
    to_address: str
    block_number: int
    source: str
    asset_type: str
    token_address: str
    raw_value: str
    decimals: int
    normalized_value: str
    tx_hash: str
    log_index: str
    token_label: str
    label: str


class EdgeIndex:
    """Holds one persistent read-only connection per SQLite edge-index file."""

    def __init__(self, db_paths: list[Path]) -> None:
        if not db_paths:
            raise SystemExit("No SQLite edge-index files found.")
        self.conns: list[sqlite3.Connection] = []
        for path in db_paths:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.execute("PRAGMA query_only = ON")
            self.conns.append(conn)
        self.db_paths = db_paths

    def close(self) -> None:
        for conn in self.conns:
            conn.close()

    def edges_touching(self, address: str) -> list[tuple]:
        rows: dict[str, tuple] = {}
        for conn in self.conns:
            for r in conn.execute(
                f"SELECT {EDGE_COLUMNS} FROM edges WHERE from_address = ?", (address,)
            ):
                rows[r[0]] = r
            for r in conn.execute(
                f"SELECT {EDGE_COLUMNS} FROM edges WHERE to_address = ?", (address,)
            ):
                rows[r[0]] = r
        return list(rows.values())


def discover_db_files(index_dir: Optional[Path], explicit_dbs: list[Path]) -> list[Path]:
    if explicit_dbs:
        return explicit_dbs
    if index_dir is None:
        raise SystemExit("Provide --index-dir or one or more --db paths.")
    if not index_dir.exists() or not index_dir.is_dir():
        raise SystemExit(f"Index directory not found: {index_dir}")
    db_files = sorted(p for p in index_dir.glob("*.sqlite") if p.is_file())
    return db_files


def build_edge(row: tuple, token_decimals: dict[str, int], missing_tokens: set[str]) -> Edge:
    (
        edge_id,
        from_addr,
        to_addr,
        block_number,
        source,
        asset_type,
        token_address,
        raw_value,
        tx_hash,
        log_index,
    ) = row

    if asset_type == "ETH":
        decimals = 18
        token_label = ETH_LABEL
        token_address_out = ETH_LABEL
    else:
        decimals = token_decimals.get(token_address)
        if decimals is None:
            missing_tokens.add(token_address)
            decimals = 0
        token_label = short_form(token_address)
        token_address_out = token_address

    normalized_value = decimal_amount(raw_value, decimals)
    label = f"block {block_number} | {normalized_value} {token_label}"

    return Edge(
        edge_id=edge_id,
        from_address=from_addr,
        to_address=to_addr,
        block_number=int(block_number),
        source=source,
        asset_type=asset_type,
        token_address=token_address_out,
        raw_value=raw_value,
        decimals=decimals,
        normalized_value=normalized_value,
        tx_hash=tx_hash,
        log_index="" if log_index is None else str(log_index),
        token_label=token_label,
        label=label,
    )


def bfs_trace(
    index: EdgeIndex,
    source: str,
    max_depth: int,
    token_decimals: dict[str, int],
) -> tuple[dict[str, Node], dict[str, Edge], set[str]]:
    nodes: dict[str, Node] = {source: Node(source, source, 0, True)}
    edges: dict[str, Edge] = {}
    missing_tokens: set[str] = set()

    frontier: deque[str] = deque([source])
    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        next_frontier: deque[str] = deque()
        for _ in range(len(frontier)):
            addr = frontier.popleft()
            for row in index.edges_touching(addr):
                edge_id = row[0]
                if edge_id not in edges:
                    edges[edge_id] = build_edge(row, token_decimals, missing_tokens)
                from_addr, to_addr = row[1], row[2]
                other = to_addr if from_addr == addr else from_addr
                if other not in nodes:
                    nodes[other] = Node(other, other, depth, False)
                    next_frontier.append(other)
        frontier = next_frontier

    return nodes, edges, missing_tokens


def write_outputs(
    nodes: dict[str, Node],
    edges: dict[str, Edge],
    output_root: Path,
    source: str,
    max_depth: int,
    db_paths: list[Path],
) -> Path:
    graph_dir = output_root / source
    graph_dir.mkdir(parents=True, exist_ok=True)

    node_rows = sorted(nodes.values(), key=lambda n: (n.distance, n.address))
    edge_rows = sorted(edges.values(), key=lambda e: (e.block_number, e.edge_id))

    with (graph_dir / "nodes.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["address", "label", "distance", "is_root"])
        writer.writeheader()
        writer.writerows(asdict(n) for n in node_rows)

    edge_fields = [
        "edge_id", "from_address", "to_address", "block_number", "source", "asset_type",
        "token_address", "raw_value", "decimals", "normalized_value", "tx_hash", "log_index",
        "token_label", "label",
    ]
    with (graph_dir / "edges.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=edge_fields)
        writer.writeheader()
        writer.writerows(asdict(e) for e in edge_rows)

    graph = {"nodes": [asdict(n) for n in node_rows], "edges": [asdict(e) for e in edge_rows]}
    (graph_dir / "graph.json").write_text(json.dumps(graph, indent=2), encoding="utf-8")

    manifest = {
        "source_address": source,
        "max_depth": max_depth,
        "index_files": [str(p) for p in db_paths],
        "node_count": len(node_rows),
        "edge_count": len(edge_rows),
    }
    (graph_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return graph_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BFS-trace ETH/token flow to and from a source address using SQLite edge indexes."
    )
    parser.add_argument("--address", required=True, help="Source Ethereum address")
    parser.add_argument("--max-depth", type=int, default=4, help="Maximum BFS hop distance (default: 4)")
    parser.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX_DIR, help="Directory of *.sqlite edge indexes")
    parser.add_argument("--db", type=Path, action="append", default=[], help="Explicit SQLite edge-index file (repeatable). Overrides --index-dir.")
    parser.add_argument("--token-metadata-csv", type=Path, help="CSV with token_address,decimals columns")
    parser.add_argument("--output-dir", type=Path, default=Path("AddressFlow/output"), help="Base output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_depth < 0:
        raise SystemExit("--max-depth must be non-negative")

    source = normalize_address(args.address)
    if source is None:
        raise SystemExit("--address must be a non-empty Ethereum-style address starting with 0x")

    db_paths = discover_db_files(args.index_dir, args.db)
    if not db_paths:
        raise SystemExit(f"No *.sqlite edge-index files found under {args.index_dir}")

    token_decimals = load_token_decimals(args.token_metadata_csv)

    print(f"Source address : {source}")
    print(f"Max depth      : {args.max_depth}")
    print(f"Edge indexes   : {len(db_paths)} file(s)")
    for p in db_paths:
        print(f"  - {p}")

    started = time.monotonic()
    index = EdgeIndex(db_paths)
    try:
        nodes, edges, missing_tokens = bfs_trace(index, source, args.max_depth, token_decimals)
    finally:
        index.close()
    elapsed = time.monotonic() - started

    graph_dir = write_outputs(nodes, edges, args.output_dir, source, args.max_depth, db_paths)

    print(f"\nBFS complete in {elapsed:.2f}s")
    print(f"Nodes: {len(nodes)}  Edges: {len(edges)}")
    print(f"Output written to: {graph_dir}")
    if missing_tokens:
        sample = ", ".join(sorted(missing_tokens)[:20])
        print(
            f"\n[WARN] {len(missing_tokens)} token address(es) had no decimals in "
            f"--token-metadata-csv; normalized_value falls back to raw units for: {sample}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
