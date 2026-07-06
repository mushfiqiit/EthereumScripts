#!/usr/bin/env python3
"""Build an index-assisted directed Ethereum address graph from per-block CSVs."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Iterable, Optional

getcontext().prec = 80

DEFAULT_CSV_PARENT = Path("/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NewCSVs")
DEFAULT_INDEX_PARENT = Path("/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/AddressIndexes")
DEFAULT_RANGES = ((25415601, 25422800), (25422801, 25430000), (25430001, 25437200), (25437201, 25444400))
CHUNK_SIZE = 100
ROOT_RE = re.compile(r"^Ethereum_TT_(\d+)_(\d+)$")
DB_RE = re.compile(r"^address_block_index_(\d+)_(\d+)\.sqlite$")


@dataclass(frozen=True)
class Occurrence:
    block_number: int
    role: str
    source: str


@dataclass
class Node:
    address: str
    display_label: str
    distance: int
    is_root: bool


@dataclass
class Edge:
    edge_id: str
    from_address: str
    to_address: str
    source: str
    asset_type: str
    token_address: str
    raw_value: str
    decimals: int
    normalized_amount: str
    block_number: int
    transaction_hash: str
    log_index: str
    display_label: str


def normalize_address(raw: object) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value or value in {"nan", "null", "none"} or not value.startswith("0x") or len(value) < 4:
        return None
    return value


def display_address(address: str) -> str:
    return f"{address[:5]}..{address[-5:]}" if len(address) > 10 else address


def decimal_amount(raw_value: str, decimals: int) -> str:
    try:
        value = Decimal(str(raw_value).strip() or "0")
    except InvalidOperation:
        value = Decimal(0)
    scaled = value / (Decimal(10) ** decimals)
    return format(scaled.normalize(), "f")


def short_token(address: str) -> str:
    return display_address(address)


def parse_int(raw: object) -> Optional[int]:
    try:
        return int(str(raw).strip())
    except Exception:
        return None


def norm_key(name: str) -> str:
    return name.strip().lower()


def pick(row: dict[str, str], *names: str) -> str:
    lookup = {norm_key(k): v for k, v in row.items()}
    for name in names:
        if name in lookup:
            return lookup[name]
    return ""


def load_token_metadata(path: Optional[Path]) -> dict[str, int]:
    if path is None:
        return {}
    metadata: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return metadata
        for row in reader:
            token = normalize_address(pick(row, "token_address", "address", "contract_address"))
            decimals_raw = pick(row, "decimals", "token_decimals")
            if token is None or decimals_raw == "":
                continue
            decimals = parse_int(decimals_raw)
            if decimals is None or decimals < 0:
                continue
            metadata[token] = decimals
    return metadata


def discover_range_dirs(parent: Path, ranges: Iterable[tuple[int, int]]) -> dict[tuple[int, int], Path]:
    result: dict[tuple[int, int], Path] = {}
    for start, end in ranges:
        path = parent / f"Ethereum_TT_{start}_{end}"
        if path.is_dir():
            result[(start, end)] = path
    return result


def discover_index_dbs(index_parent: Path, ranges: Iterable[tuple[int, int]]) -> list[Path]:
    dbs: list[Path] = []
    for start, end in ranges:
        range_dir = index_parent / f"Ethereum_TT_{start}_{end}"
        if not range_dir.is_dir():
            continue
        dbs.extend(sorted(p for p in range_dir.glob("address_block_index_*.sqlite") if p.is_file()))
    return dbs


def range_for_block(block: int, range_dirs: dict[tuple[int, int], Path]) -> tuple[int, int, Path]:
    for (start, end), root in range_dirs.items():
        if start <= block <= end:
            return start, end, root
    raise KeyError(f"block {block} is outside configured CSV ranges")


def csv_path_for_block(block: int, source: str, range_dirs: dict[tuple[int, int], Path]) -> Path:
    start, _end, root = range_for_block(block, range_dirs)
    chunk_start = start + ((block - start) // CHUNK_SIZE) * CHUNK_SIZE
    chunk_end = chunk_start + CHUNK_SIZE - 1
    prefix = "transaction" if source == "transaction" else "token_transfer"
    return root / f"Transaction_TokenTransfer_{chunk_start}_{chunk_end}" / f"{prefix}_{block}.csv"


class LruCsvCache:
    def __init__(self, max_files: int) -> None:
        self.max_files = max_files
        self._items: OrderedDict[Path, list[dict[str, str]]] = OrderedDict()

    def get(self, path: Path) -> list[dict[str, str]]:
        if path in self._items:
            self._items.move_to_end(path)
            return self._items[path]
        rows: list[dict[str, str]] = []
        if path.exists() and path.is_file():
            with path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    rows = list(reader)
        self._items[path] = rows
        self._items.move_to_end(path)
        while len(self._items) > self.max_files:
            self._items.popitem(last=False)
        return rows


class GraphBuilder:
    def __init__(self, csv_parent: Path, index_parent: Path, ranges: tuple[tuple[int, int], ...], token_metadata: dict[str, int], csv_cache_size: int) -> None:
        self.range_dirs = discover_range_dirs(csv_parent, ranges)
        missing_csv = [f"Ethereum_TT_{s}_{e}" for s, e in ranges if (s, e) not in self.range_dirs]
        if missing_csv:
            raise SystemExit(f"Missing CSV range folders: {', '.join(missing_csv)}")
        self.index_dbs = discover_index_dbs(index_parent, ranges)
        if not self.index_dbs:
            raise SystemExit(f"No SQLite indexes found under {index_parent}")
        self.token_metadata = token_metadata
        self.csv_cache = LruCsvCache(csv_cache_size)
        self.occurrence_cache: dict[str, list[Occurrence]] = {}
        self.missing_token_metadata: set[str] = set()

    def occurrences_for(self, address: str) -> list[Occurrence]:
        if address in self.occurrence_cache:
            return self.occurrence_cache[address]
        occurrences: set[Occurrence] = set()
        for db in self.index_dbs:
            conn = sqlite3.connect(db)
            try:
                cur = conn.execute(
                    "SELECT DISTINCT block_number, role, source FROM occurrences WHERE address = ? ORDER BY block_number",
                    (address,),
                )
                for block, role, source in cur.fetchall():
                    occurrences.add(Occurrence(int(block), str(role), str(source)))
            finally:
                conn.close()
        ordered = sorted(occurrences, key=lambda x: (x.block_number, x.source, x.role))
        self.occurrence_cache[address] = ordered
        return ordered

    def rows_for_occurrence(self, occurrence: Occurrence) -> list[dict[str, str]]:
        path = csv_path_for_block(occurrence.block_number, occurrence.source, self.range_dirs)
        return self.csv_cache.get(path)

    def edge_from_row(self, row: dict[str, str], source: str) -> Optional[Edge]:
        from_addr = normalize_address(pick(row, "from_address", "from"))
        to_addr = normalize_address(pick(row, "to_address", "to"))
        block = parse_int(pick(row, "block_number", "blocknumber", "block"))
        if from_addr is None or to_addr is None or block is None:
            return None
        raw_value = pick(row, "value") or "0"
        if source == "transaction":
            tx_hash = pick(row, "hash", "transaction_hash")
            if not tx_hash:
                return None
            normalized = decimal_amount(raw_value, 18)
            return Edge(
                edge_id=f"transaction:{tx_hash.lower()}", from_address=from_addr, to_address=to_addr,
                source="transaction", asset_type="ETH", token_address="", raw_value=raw_value, decimals=18,
                normalized_amount=normalized, block_number=block, transaction_hash=tx_hash, log_index="",
                display_label=f"{normalized} ETH | block {block}",
            )
        token = normalize_address(pick(row, "token_address"))
        tx_hash = pick(row, "transaction_hash", "hash")
        log_index = pick(row, "log_index")
        if token is None or not tx_hash or log_index == "":
            return None
        if token not in self.token_metadata:
            self.missing_token_metadata.add(token)
            return None
        decimals = self.token_metadata[token]
        normalized = decimal_amount(raw_value, decimals)
        return Edge(
            edge_id=f"token_transfer:{tx_hash.lower()}:{log_index}", from_address=from_addr, to_address=to_addr,
            source="token_transfer", asset_type="TOKEN", token_address=token, raw_value=raw_value, decimals=decimals,
            normalized_amount=normalized, block_number=block, transaction_hash=tx_hash, log_index=log_index,
            display_label=f"{normalized} {short_token(token)} | block {block}",
        )

    def build(self, source_address: str, max_depth: int) -> tuple[dict[str, Node], dict[str, Edge]]:
        source = normalize_address(source_address)
        if source is None:
            raise SystemExit("--address must be a non-empty Ethereum-style address starting with 0x")
        nodes = {source: Node(source, display_address(source), 0, True)}
        edges: dict[str, Edge] = {}
        queue: deque[str] = deque([source])
        while queue:
            current = queue.popleft()
            current_distance = nodes[current].distance
            if current_distance >= max_depth:
                continue
            for occurrence in self.occurrences_for(current):
                for row in self.rows_for_occurrence(occurrence):
                    edge = self.edge_from_row(row, occurrence.source)
                    if edge is None:
                        continue
                    if current not in {edge.from_address, edge.to_address}:
                        continue
                    edges.setdefault(edge.edge_id, edge)
                    other = edge.to_address if current == edge.from_address else edge.from_address
                    next_distance = current_distance + 1
                    if next_distance <= max_depth and other not in nodes:
                        nodes[other] = Node(other, display_address(other), next_distance, False)
                        queue.append(other)
        if self.missing_token_metadata:
            sample = ", ".join(sorted(self.missing_token_metadata)[:20])
            raise SystemExit(
                "Missing token decimals for token address(es): " + sample +
                ". Provide --token-metadata-csv with token_address,decimals columns."
            )
        return nodes, edges


def write_outputs(nodes: dict[str, Node], edges: dict[str, Edge], output_root: Path, source: str, args: argparse.Namespace) -> Path:
    graph_dir = output_root / source
    graph_dir.mkdir(parents=True, exist_ok=True)
    node_rows = sorted(nodes.values(), key=lambda n: (n.distance, n.address))
    edge_rows = sorted(edges.values(), key=lambda e: (e.block_number, e.edge_id))
    with (graph_dir / "nodes.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["address", "display_label", "distance", "is_root"])
        writer.writeheader(); writer.writerows(asdict(n) for n in node_rows)
    edge_fields = list(asdict(edge_rows[0]).keys()) if edge_rows else list(Edge("","","","","","","",0,"",0,"","","").__dict__.keys())
    with (graph_dir / "edges.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=edge_fields)
        writer.writeheader(); writer.writerows(asdict(e) for e in edge_rows)
    graph = {"nodes": [asdict(n) for n in node_rows], "edges": [asdict(e) for e in edge_rows]}
    (graph_dir / "graph.json").write_text(json.dumps(graph, indent=2), encoding="utf-8")
    manifest = {
        "source_address": source, "max_depth": args.max_depth, "csv_parent": str(args.csv_parent),
        "index_parent": str(args.index_parent), "token_metadata_csv": str(args.token_metadata_csv) if args.token_metadata_csv else None,
        "node_count": len(node_rows), "edge_count": len(edge_rows), "ranges": [list(r) for r in DEFAULT_RANGES],
    }
    (graph_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return graph_dir


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build an index-assisted directed address graph from Ethereum CSVs.")
    p.add_argument("--address", required=True)
    p.add_argument("--max-depth", type=int, default=10)
    p.add_argument("--csv-parent", type=Path, default=DEFAULT_CSV_PARENT)
    p.add_argument("--index-parent", type=Path, default=DEFAULT_INDEX_PARENT)
    p.add_argument("--output-dir", type=Path, default=Path("AddressTracing/output"))
    p.add_argument("--token-metadata-csv", type=Path, help="CSV with token_address,decimals columns")
    p.add_argument("--csv-cache-size", type=int, default=128)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_depth < 0:
        raise SystemExit("--max-depth must be non-negative")
    metadata = load_token_metadata(args.token_metadata_csv)
    builder = GraphBuilder(args.csv_parent, args.index_parent, DEFAULT_RANGES, metadata, args.csv_cache_size)
    source = normalize_address(args.address)
    if source is None:
        raise SystemExit("Invalid --address")
    nodes, edges = builder.build(source, args.max_depth)
    graph_dir = write_outputs(nodes, edges, args.output_dir, source, args)
    print(f"Graph written to {graph_dir}")
    print(f"Nodes: {len(nodes)} Edges: {len(edges)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
