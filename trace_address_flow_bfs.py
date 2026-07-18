#!/usr/bin/env python3
"""BFS-trace ETH/token flow to and from a source address using SQLite edge indexes.

Standalone script - does not import or depend on any other file in this
repository. It reads the `edges` (and, if present, `degree`) tables written
by build_block_range_sqlite_index.py (one SQLite file per 7200-block range).

Algorithm: breadth-first search outward from the source address, treating
the transfer graph as undirected for reachability (so both "who sent to the
source" and "who the source sent to" are explored, directly or through
intermediate addresses) up to --max-depth hops, while keeping each edge's
real direction (from_address -> to_address) in the output for visualization.

Two things make this scale to real Ethereum data instead of exploring an
unbounded number of nodes:

1. Batched, per-level queries instead of one query per address. Every
   address in the current BFS frontier is looked up in a single SQL
   statement per (open connection x direction), using WHERE address IN (...)
   - not a separate round trip per address. This cuts round trips from
   O(frontier size) to O(1) per level.

2. A hard cap on edges kept per address (--max-edges-per-node), enforced
   inside SQL with a ROW_NUMBER() window function so a hub address
   (exchange hot wallet, popular router/contract - anything with degree in
   the thousands or more) never returns more than the cap, most-recent-block
   first. Without this, a single hub touched at hop 2 can pull in its entire
   neighborhood, and hop 3-4 then has to expand all of that - this is what
   makes an uncapped --max-depth 4 search blow up. The optional `degree`
   table (built by build_block_range_sqlite_index.py) is used to flag which
   nodes actually got capped (is_hub) so that's visible in the output; the
   capping itself works even without that table.

A --max-nodes hard ceiling on total distinct addresses collected is also
enforced as a safety valve, so a run always terminates in bounded time
regardless of how the graph happens to be shaped.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Iterator, Optional

getcontext().prec = 80

ETH_LABEL = "ETH"
DEFAULT_INDEX_DIR = Path(
    "/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/SqliteEdgeIndexes"
)
DEFAULT_MAX_EDGES_PER_NODE = 1000
DEFAULT_MAX_NODES = 20000

# Addresses per SQL IN(...) batch. Comfortably under SQLite's parameter
# limit (999 on older builds, 32766 on modern ones) regardless of build.
QUERY_BATCH_SIZE = 500

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


def chunked(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


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
    is_hub: bool = False
    total_degree: int = -1  # -1 means unknown (no degree table available)


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
    """Holds one persistent read-only connection per SQLite edge-index file.

    Query methods operate on a whole BFS frontier (list of addresses) at
    once, not one address at a time.
    """

    def __init__(self, db_paths: list[Path]) -> None:
        if not db_paths:
            raise SystemExit("No SQLite edge-index files found.")
        self.conns: list[sqlite3.Connection] = []
        self.has_degree_table: list[bool] = []
        for path in db_paths:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.execute("PRAGMA query_only = ON")
            self.conns.append(conn)
            has_degree = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='degree'"
                ).fetchone()
                is not None
            )
            self.has_degree_table.append(has_degree)
        self.db_paths = db_paths
        self.any_degree_table = any(self.has_degree_table)

    def close(self) -> None:
        for conn in self.conns:
            conn.close()

    def edges_for_frontier(
        self, addresses: list[str], max_edges_per_node: int
    ) -> dict[str, list[tuple]]:
        """Return up to max_edges_per_node edges per address (most recent
        block first, both directions combined), merged across every open
        shard. One or two SQL statements per (connection, batch), not per
        address."""
        per_address: dict[str, dict[str, tuple]] = {addr: {} for addr in addresses}
        for conn in self.conns:
            for batch in chunked(addresses, QUERY_BATCH_SIZE):
                self._query_direction(conn, batch, "from_address", max_edges_per_node, per_address)
                self._query_direction(conn, batch, "to_address", max_edges_per_node, per_address)

        result: dict[str, list[tuple]] = {}
        for addr, edge_map in per_address.items():
            rows = sorted(edge_map.values(), key=lambda r: r[3], reverse=True)  # block_number desc
            result[addr] = rows[:max_edges_per_node]
        return result

    @staticmethod
    def _query_direction(
        conn: sqlite3.Connection,
        batch: list[str],
        column: str,
        limit: int,
        per_address: dict[str, dict[str, tuple]],
    ) -> None:
        placeholders = ",".join("?" * len(batch))
        sql = f"""
            WITH ranked AS (
                SELECT {EDGE_COLUMNS},
                       ROW_NUMBER() OVER (PARTITION BY {column} ORDER BY block_number DESC) AS rn
                FROM edges
                WHERE {column} IN ({placeholders})
            )
            SELECT {EDGE_COLUMNS} FROM ranked WHERE rn <= ?
        """
        params = (*batch, limit)
        key_index = 1 if column == "from_address" else 2
        for row in conn.execute(sql, params):
            key = row[key_index]
            bucket = per_address.get(key)
            if bucket is not None:
                bucket[row[0]] = row

    def degree_for(self, addresses: list[str]) -> dict[str, tuple[int, int]]:
        """Best-effort (out_count, in_count) per address, summed across
        shards that have a degree table. Returns {} entirely if no open
        shard has one (older index files built before this feature)."""
        if not self.any_degree_table:
            return {}
        totals: dict[str, list[int]] = {addr: [0, 0] for addr in addresses}
        for conn, has_degree in zip(self.conns, self.has_degree_table):
            if not has_degree:
                continue
            for batch in chunked(addresses, QUERY_BATCH_SIZE):
                placeholders = ",".join("?" * len(batch))
                sql = f"SELECT address, out_count, in_count FROM degree WHERE address IN ({placeholders})"
                for address, out_c, in_c in conn.execute(sql, batch):
                    totals[address][0] += out_c
                    totals[address][1] += in_c
        return {addr: (v[0], v[1]) for addr, v in totals.items()}


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
    max_edges_per_node: int,
    max_nodes: int,
) -> tuple[dict[str, Node], dict[str, Edge], set[str], bool]:
    nodes: dict[str, Node] = {source: Node(source, source, 0, True)}
    edges: dict[str, Edge] = {}
    missing_tokens: set[str] = set()
    nodes_capped = False

    frontier: list[str] = [source]
    for depth in range(1, max_depth + 1):
        if not frontier:
            break

        edges_by_addr = index.edges_for_frontier(frontier, max_edges_per_node)
        degrees = index.degree_for(frontier)
        for addr in frontier:
            out_c, in_c = degrees.get(addr, (-1, -1))
            if addr in nodes and out_c >= 0:
                total = out_c + in_c
                nodes[addr].total_degree = total
                nodes[addr].is_hub = total > max_edges_per_node

        next_frontier: list[str] = []
        for addr in frontier:
            for row in edges_by_addr.get(addr, []):
                from_addr, to_addr = row[1], row[2]
                other = to_addr if from_addr == addr else from_addr
                if other not in nodes:
                    if len(nodes) >= max_nodes:
                        nodes_capped = True
                        continue  # drop the edge too - its other endpoint won't be a node
                    nodes[other] = Node(other, other, depth, False)
                    next_frontier.append(other)
                edge_id = row[0]
                if edge_id not in edges:
                    edges[edge_id] = build_edge(row, token_decimals, missing_tokens)
        frontier = next_frontier

    return nodes, edges, missing_tokens, nodes_capped


def write_outputs(
    nodes: dict[str, Node],
    edges: dict[str, Edge],
    output_root: Path,
    source: str,
    max_depth: int,
    db_paths: list[Path],
    max_edges_per_node: int,
    max_nodes: int,
    nodes_capped: bool,
) -> Path:
    graph_dir = output_root / source
    graph_dir.mkdir(parents=True, exist_ok=True)

    node_rows = sorted(nodes.values(), key=lambda n: (n.distance, n.address))
    edge_rows = sorted(edges.values(), key=lambda e: (e.block_number, e.edge_id))

    with (graph_dir / "nodes.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["address", "label", "distance", "is_root", "is_hub", "total_degree"]
        )
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
        "max_edges_per_node": max_edges_per_node,
        "max_nodes": max_nodes,
        "nodes_capped": nodes_capped,
        "index_files": [str(p) for p in db_paths],
        "node_count": len(node_rows),
        "edge_count": len(edge_rows),
        "hub_node_count": sum(1 for n in node_rows if n.is_hub),
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
    parser.add_argument(
        "--max-edges-per-node",
        type=int,
        default=DEFAULT_MAX_EDGES_PER_NODE,
        help=(
            "Cap on edges kept per address, both directions combined, most-recent-block "
            f"first (default: {DEFAULT_MAX_EDGES_PER_NODE}). Bounds hub addresses "
            "(exchanges, routers, popular contracts) that would otherwise blow up the search."
        ),
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_MAX_NODES,
        help=(
            f"Hard cap on total distinct addresses collected (default: {DEFAULT_MAX_NODES}). "
            "Guarantees the run terminates in bounded time regardless of graph shape."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_depth < 0:
        raise SystemExit("--max-depth must be non-negative")
    if args.max_edges_per_node < 1:
        raise SystemExit("--max-edges-per-node must be at least 1")
    if args.max_nodes < 1:
        raise SystemExit("--max-nodes must be at least 1")

    source = normalize_address(args.address)
    if source is None:
        raise SystemExit("--address must be a non-empty Ethereum-style address starting with 0x")

    db_paths = discover_db_files(args.index_dir, args.db)
    if not db_paths:
        raise SystemExit(f"No *.sqlite edge-index files found under {args.index_dir}")

    token_decimals = load_token_decimals(args.token_metadata_csv)

    print(f"Source address    : {source}")
    print(f"Max depth         : {args.max_depth}")
    print(f"Max edges/node    : {args.max_edges_per_node}")
    print(f"Max nodes         : {args.max_nodes}")
    print(f"Edge indexes      : {len(db_paths)} file(s)")
    for p in db_paths:
        print(f"  - {p}")

    started = time.monotonic()
    index = EdgeIndex(db_paths)
    if not index.any_degree_table:
        print(
            "[NOTE] No index file has a degree table (built before this feature was added); "
            "edges are still capped per node, but hub nodes won't be flagged with is_hub/total_degree. "
            "Re-run build_block_range_sqlite_index.py to add it."
        )
    try:
        nodes, edges, missing_tokens, nodes_capped = bfs_trace(
            index, source, args.max_depth, token_decimals, args.max_edges_per_node, args.max_nodes
        )
    finally:
        index.close()
    elapsed = time.monotonic() - started

    graph_dir = write_outputs(
        nodes, edges, args.output_dir, source, args.max_depth, db_paths,
        args.max_edges_per_node, args.max_nodes, nodes_capped,
    )

    print(f"\nBFS complete in {elapsed:.2f}s")
    print(f"Nodes: {len(nodes)}  Edges: {len(edges)}")
    hub_count = sum(1 for n in nodes.values() if n.is_hub)
    if hub_count:
        print(f"Hub nodes (edges capped at {args.max_edges_per_node}): {hub_count}")
    if nodes_capped:
        print(f"[WARN] --max-nodes ({args.max_nodes}) was reached; the graph was truncated.")
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
