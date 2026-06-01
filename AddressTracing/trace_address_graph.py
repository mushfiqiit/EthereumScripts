#!/usr/bin/env python3
"""Trace Ethereum address activity into an interactive directed transaction graph.

The SQLite indexes created by build_address_index.py are used only as a fast
address -> block-number lookup. Full transaction and token-transfer details are
then loaded from the original Transaction_TokenTransfer CSV files for those
blocks so graph edges can include hashes, token metadata, raw values, and USD
estimates.
"""

from __future__ import annotations

import argparse
import csv
import html
import logging
import re
import sqlite3
import sys
from collections import Counter, deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Iterable


getcontext().prec = 80

ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
DB_RE = re.compile(r"^address_block_index_(\d+)_(\d+)\.sqlite$")
ETHEREUM_TT_RE = re.compile(r"^Ethereum_TT_(\d+)_(\d+)$")
TRANSACTION_COLUMNS = ("block_number", "hash", "from_address", "to_address", "value")
TOKEN_TRANSFER_COLUMNS = (
    "block_number",
    "transaction_hash",
    "token_address",
    "from_address",
    "to_address",
    "value",
)
EDGE_CSV_COLUMNS = [
    "source_address",
    "target_address",
    "source_type",
    "block_number",
    "transaction_hash",
    "token_address",
    "token_symbol",
    "raw_value",
    "decimal",
    "median_exchange_rate_USD",
    "transfer_value_USD",
    "transfer_value_label",
    "csv_file",
]
NODE_CSV_COLUMNS = ["address", "is_root", "discovery_depth", "in_degree", "out_degree", "total_degree"]


@dataclass(frozen=True)
class TokenMetadata:
    token_symbol: str
    decimal: int | None
    median_exchange_rate_usd: Decimal | None


@dataclass(frozen=True)
class TransferEdge:
    source_address: str
    target_address: str
    source_type: str
    block_number: int
    transaction_hash: str
    token_address: str
    token_symbol: str
    raw_value: str
    decimal: str
    median_exchange_rate_USD: str
    transfer_value_USD: str
    transfer_value_label: str
    csv_file: str

    def duplicate_key(self) -> tuple[str, str, int, str, str, str, str]:
        return (
            self.source_address,
            self.target_address,
            self.block_number,
            self.transaction_hash,
            self.source_type,
            self.token_address,
            self.raw_value,
        )


@dataclass
class BlockRows:
    transactions: list[dict[str, str]]
    token_transfers: list[dict[str, str]]
    transaction_csv: Path | None
    token_transfer_csv: Path | None


@dataclass
class TraceStats:
    addresses_queried: int = 0
    block_files_parsed: int = 0
    transaction_rows_considered: int = 0
    token_transfer_rows_considered: int = 0
    unknown_transfer_value_edges: int = 0
    missing_csv_files: int = 0
    skipped_min_usd_edges: int = 0
    duplicate_edges: int = 0


def configure_csv_field_limit() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trace an Ethereum address component and generate interactive HTML/CSV graph outputs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--root-address", required=True, help="Root Ethereum address to trace.")
    parser.add_argument("--data-base-dir", required=True, help="Directory containing Ethereum_TT_* folders.")
    parser.add_argument("--index-base-dir", required=True, help="Directory containing indexes_* folders.")
    parser.add_argument("--token-metadata-csv", required=True, help="Token metadata CSV with median_exchange_rate_USD.")
    parser.add_argument("--start-block", type=int, default=25112101, help="First block in the available data range.")
    parser.add_argument("--end-block", type=int, default=25205700, help="Last block in the available data range.")
    parser.add_argument("--outer-range-size", type=int, default=7200, help="Number of blocks per Ethereum_TT/indexes outer folder.")
    parser.add_argument("--chunk-size", type=int, default=100, help="Number of blocks per Transaction_TokenTransfer/index SQLite chunk.")
    parser.add_argument("--eth-usd-price", type=Decimal, default=Decimal("2000.0"), help="Fixed ETH/USD price used for transaction value estimates.")
    parser.add_argument("--output-html", required=True, help="Interactive HTML graph output path.")
    parser.add_argument("--output-edges-csv", required=True, help="Machine-readable edge CSV output path.")
    parser.add_argument("--output-nodes-csv", required=True, help="Machine-readable node CSV output path.")
    parser.add_argument("--max-depth", type=int, default=None, help="Maximum BFS depth from root. Omit for full traversal.")
    parser.add_argument("--max-nodes", type=int, default=None, help="Maximum nodes to discover. Omit for no node limit.")
    parser.add_argument("--max-edges", type=int, default=None, help="Maximum edges to discover. Omit for no edge limit.")
    parser.add_argument("--min-usd-value", type=Decimal, default=None, help="Only include edges with known USD value at or above this amount. Unknown token values are still included.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def normalize_address(value: object) -> str | None:
    text = str(value).strip().lower() if value is not None else ""
    if ADDRESS_RE.fullmatch(text):
        return text
    return None


def normalize_hash(value: object) -> str:
    text = str(value).strip().lower() if value is not None else ""
    return "" if text in {"", "nan", "none", "null"} else text


def parse_decimal(value: object) -> Decimal | None:
    text = str(value).strip() if value is not None else ""
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def parse_int(value: object) -> int | None:
    text = str(value).strip() if value is not None else ""
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def decimal_to_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    normalized = value.normalize()
    return format(normalized, "f") if normalized == normalized.to_integral() else format(normalized, "f")


def format_usd(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "$0.00"
    if abs(value) < Decimal("0.01"):
        return f"${value:.8f}"
    return f"${value:,.2f}"


def short_address(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}"


def discover_sqlite_files(index_base_dir: Path) -> list[Path]:
    files = [path for path in index_base_dir.rglob("address_block_index_*.sqlite") if path.is_file()]

    def sort_key(path: Path) -> tuple[int, int, str]:
        match = DB_RE.match(path.name)
        if not match:
            return (sys.maxsize, sys.maxsize, str(path))
        return (int(match.group(1)), int(match.group(2)), str(path))

    return sorted(files, key=sort_key)


def query_address_blocks(address: str, db_files: list[Path], cache: dict[str, set[int]], stats: TraceStats) -> set[int]:
    if address in cache:
        return cache[address]

    blocks: set[int] = set()
    for db_path in db_files:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cursor = conn.execute(
                    "SELECT block_number, role, source FROM occurrences WHERE address = ?",
                    (address,),
                )
                for block_number, _role, _source in cursor:
                    if block_number is not None:
                        blocks.add(int(block_number))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logging.warning("Skipping SQLite file after query error: %s (%s)", db_path, exc)
    cache[address] = blocks
    stats.addresses_queried += 1
    return blocks


def range_start_for_block(block_number: int, global_start: int, size: int) -> int:
    offset = block_number - global_start
    return global_start + (offset // size) * size


def range_end_for_start(start: int, size: int) -> int:
    return start + size - 1


def expected_csv_path(data_base_dir: Path, start_block: int, outer_size: int, chunk_size: int, block_number: int, prefix: str) -> Path:
    outer_start = range_start_for_block(block_number, start_block, outer_size)
    outer_end = range_end_for_start(outer_start, outer_size)
    chunk_start = range_start_for_block(block_number, start_block, chunk_size)
    chunk_end = range_end_for_start(chunk_start, chunk_size)
    return (
        data_base_dir
        / f"Ethereum_TT_{outer_start}_{outer_end}"
        / f"Transaction_TokenTransfer_{chunk_start}_{chunk_end}"
        / f"{prefix}_{block_number}.csv"
    )


def find_matching_outer_folder(data_base_dir: Path, block_number: int) -> Path | None:
    for child in data_base_dir.glob("Ethereum_TT_*_*"):
        if not child.is_dir():
            continue
        match = ETHEREUM_TT_RE.match(child.name)
        if not match:
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if start <= block_number <= end:
            return child
    return None


def resolve_csv_path(data_base_dir: Path, start_block: int, outer_size: int, chunk_size: int, block_number: int, prefix: str) -> Path | None:
    expected = expected_csv_path(data_base_dir, start_block, outer_size, chunk_size, block_number, prefix)
    if expected.is_file():
        return expected
    outer_folder = expected.parent.parent
    if not outer_folder.is_dir():
        outer_folder = find_matching_outer_folder(data_base_dir, block_number)
    if outer_folder is None or not outer_folder.is_dir():
        return None
    matches = sorted(outer_folder.rglob(f"{prefix}_{block_number}.csv"))
    return matches[0] if matches else None


def read_csv_rows(csv_path: Path, required_columns: Iterable[str]) -> list[dict[str, str]]:
    try:
        with csv_path.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                logging.warning("CSV is empty or has no header: %s", csv_path)
                return []
            field_set = set(reader.fieldnames)
            missing = [column for column in required_columns if column not in field_set]
            if missing:
                logging.warning("CSV missing expected column(s) %s: %s", missing, csv_path)
            return [dict(row) for row in reader]
    except OSError as exc:
        logging.warning("Failed reading CSV %s: %s", csv_path, exc)
        return []


def load_block_rows(
    block_number: int,
    args: argparse.Namespace,
    block_cache: dict[int, BlockRows],
    stats: TraceStats,
) -> BlockRows:
    if block_number in block_cache:
        return block_cache[block_number]

    data_base_dir = Path(args.data_base_dir)
    tx_path = resolve_csv_path(data_base_dir, args.start_block, args.outer_range_size, args.chunk_size, block_number, "transaction")
    tt_path = resolve_csv_path(data_base_dir, args.start_block, args.outer_range_size, args.chunk_size, block_number, "token_transfer")

    if tx_path is None:
        stats.missing_csv_files += 1
        logging.debug("Missing transaction CSV for block %s", block_number)
    if tt_path is None:
        stats.missing_csv_files += 1
        logging.debug("Missing token transfer CSV for block %s", block_number)

    transactions = read_csv_rows(tx_path, TRANSACTION_COLUMNS) if tx_path else []
    token_transfers = read_csv_rows(tt_path, TOKEN_TRANSFER_COLUMNS) if tt_path else []
    if tx_path or tt_path:
        stats.block_files_parsed += int(tx_path is not None) + int(tt_path is not None)

    rows = BlockRows(transactions=transactions, token_transfers=token_transfers, transaction_csv=tx_path, token_transfer_csv=tt_path)
    block_cache[block_number] = rows
    return rows


def load_token_metadata(metadata_csv: Path) -> dict[str, TokenMetadata]:
    metadata: dict[str, TokenMetadata] = {}
    if not metadata_csv.is_file():
        logging.warning("Token metadata CSV not found. Token USD values will be unknown: %s", metadata_csv)
        return metadata

    with metadata_csv.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            logging.warning("Token metadata CSV is empty or missing a header: %s", metadata_csv)
            return metadata
        for row in reader:
            token_address = normalize_address(row.get("token_address"))
            if token_address is None:
                continue
            decimal = parse_int(row.get("decimal"))
            rate = parse_decimal(row.get("median_exchange_rate_USD"))
            symbol = str(row.get("token_symbol") or "").strip()
            metadata[token_address] = TokenMetadata(token_symbol=symbol, decimal=decimal, median_exchange_rate_usd=rate)
    return metadata


def transaction_edge(row: dict[str, str], csv_file: Path | None, eth_usd_price: Decimal) -> TransferEdge | None:
    source = normalize_address(row.get("from_address"))
    target = normalize_address(row.get("to_address"))
    raw_value = str(row.get("value") or "").strip()
    value = parse_decimal(raw_value)
    block_number = parse_int(row.get("block_number"))
    if source is None or target is None or value is None or block_number is None or value == 0:
        return None

    # ETH uses 18 decimals: raw wei / 10^18 = ETH, then ETH * configured USD price.
    usd_value = (value / (Decimal(10) ** 18)) * eth_usd_price
    return TransferEdge(
        source_address=source,
        target_address=target,
        source_type="transaction",
        block_number=block_number,
        transaction_hash=normalize_hash(row.get("hash")),
        token_address="",
        token_symbol="ETH",
        raw_value=raw_value,
        decimal="18",
        median_exchange_rate_USD=decimal_to_text(eth_usd_price),
        transfer_value_USD=decimal_to_text(usd_value),
        transfer_value_label=format_usd(usd_value),
        csv_file=str(csv_file or ""),
    )


def token_transfer_edge(row: dict[str, str], csv_file: Path | None, token_metadata: dict[str, TokenMetadata]) -> TransferEdge | None:
    source = normalize_address(row.get("from_address"))
    target = normalize_address(row.get("to_address"))
    token_address = normalize_address(row.get("token_address"))
    raw_value = str(row.get("value") or "").strip()
    value = parse_decimal(raw_value)
    block_number = parse_int(row.get("block_number"))
    if source is None or target is None or value is None or block_number is None:
        return None

    metadata = token_metadata.get(token_address or "")
    decimal = metadata.decimal if metadata else None
    rate = metadata.median_exchange_rate_usd if metadata else None
    symbol = metadata.token_symbol if metadata and metadata.token_symbol else ""
    usd_value: Decimal | None = None
    if decimal is not None and rate is not None:
        # Token raw value / 10^decimal = human amount, then amount * USD median rate.
        usd_value = (value / (Decimal(10) ** decimal)) * rate

    return TransferEdge(
        source_address=source,
        target_address=target,
        source_type="token_transfer",
        block_number=block_number,
        transaction_hash=normalize_hash(row.get("transaction_hash")),
        token_address=token_address or "",
        token_symbol=symbol,
        raw_value=raw_value,
        decimal="" if decimal is None else str(decimal),
        median_exchange_rate_USD="" if rate is None else decimal_to_text(rate),
        transfer_value_USD="" if usd_value is None else decimal_to_text(usd_value),
        transfer_value_label=format_usd(usd_value),
        csv_file=str(csv_file or ""),
    )


def passes_min_usd(edge: TransferEdge, min_usd_value: Decimal | None) -> bool:
    if min_usd_value is None or edge.transfer_value_USD == "":
        return True
    value = parse_decimal(edge.transfer_value_USD)
    return value is not None and value >= min_usd_value


def trace_graph(args: argparse.Namespace, db_files: list[Path], token_metadata: dict[str, TokenMetadata]) -> tuple[dict[str, int], list[TransferEdge], TraceStats]:
    root = normalize_address(args.root_address)
    if root is None:
        raise SystemExit("Invalid --root-address. Expected 0x followed by exactly 40 hexadecimal characters.")

    if args.max_depth is None or args.max_nodes is None or args.max_edges is None:
        logging.warning("One or more traversal safety limits are unset. Full connected-component traversal may become very large.")

    stats = TraceStats()
    address_block_cache: dict[str, set[int]] = {}
    block_cache: dict[int, BlockRows] = {}
    discovered_depth: dict[str, int] = {root: 0}
    queued_or_queried: set[str] = {root}
    edges: list[TransferEdge] = []
    edge_keys: set[tuple[str, str, int, str, str, str, str]] = set()
    queue: deque[tuple[str, int]] = deque([(root, 0)])
    queried_addresses: set[str] = set()

    while queue:
        current, depth = queue.popleft()
        if current in queried_addresses:
            continue
        if args.max_depth is not None and depth > args.max_depth:
            continue
        queried_addresses.add(current)

        blocks = sorted(
            block for block in query_address_blocks(current, db_files, address_block_cache, stats)
            if args.start_block <= block <= args.end_block
        )
        logging.info(
            "Query %s depth=%s matched %s unique block(s); nodes=%s edges=%s queue=%s",
            short_address(current), depth, len(blocks), len(discovered_depth), len(edges), len(queue),
        )

        for block_number in blocks:
            if args.max_edges is not None and len(edges) >= args.max_edges:
                logging.warning("Stopping traversal because --max-edges=%s was reached.", args.max_edges)
                return discovered_depth, edges, stats

            block_rows = load_block_rows(block_number, args, block_cache, stats)
            candidate_edges: list[TransferEdge] = []

            for row in block_rows.transactions:
                stats.transaction_rows_considered += 1
                if normalize_address(row.get("from_address")) != current and normalize_address(row.get("to_address")) != current:
                    continue
                edge = transaction_edge(row, block_rows.transaction_csv, args.eth_usd_price)
                if edge is not None:
                    candidate_edges.append(edge)

            for row in block_rows.token_transfers:
                stats.token_transfer_rows_considered += 1
                if normalize_address(row.get("from_address")) != current and normalize_address(row.get("to_address")) != current:
                    continue
                edge = token_transfer_edge(row, block_rows.token_transfer_csv, token_metadata)
                if edge is not None:
                    candidate_edges.append(edge)

            for edge in candidate_edges:
                if not passes_min_usd(edge, args.min_usd_value):
                    stats.skipped_min_usd_edges += 1
                    continue
                key = edge.duplicate_key()
                if key in edge_keys:
                    stats.duplicate_edges += 1
                    continue
                if args.max_edges is not None and len(edges) >= args.max_edges:
                    logging.warning("Stopping traversal because --max-edges=%s was reached.", args.max_edges)
                    return discovered_depth, edges, stats

                missing_endpoints = [
                    address
                    for address in (edge.source_address, edge.target_address)
                    if address not in discovered_depth
                ]
                if args.max_nodes is not None and len(discovered_depth) + len(set(missing_endpoints)) > args.max_nodes:
                    continue
                for address in missing_endpoints:
                    discovered_depth[address] = depth + 1

                edge_keys.add(key)
                edges.append(edge)
                if edge.transfer_value_label == "unknown":
                    stats.unknown_transfer_value_edges += 1

                for neighbor in (edge.source_address, edge.target_address):
                    next_depth = depth + 1
                    if (
                        (args.max_depth is None or next_depth <= args.max_depth)
                        and neighbor not in queued_or_queried
                        and neighbor not in queried_addresses
                    ):
                        queued_or_queried.add(neighbor)
                        queue.append((neighbor, next_depth))

    return discovered_depth, edges, stats


def write_edges_csv(edges: list[TransferEdge], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EDGE_CSV_COLUMNS)
        writer.writeheader()
        for edge in edges:
            writer.writerow({column: getattr(edge, column) for column in EDGE_CSV_COLUMNS})


def degree_counts(edges: list[TransferEdge]) -> tuple[Counter[str], Counter[str]]:
    in_degree: Counter[str] = Counter()
    out_degree: Counter[str] = Counter()
    for edge in edges:
        out_degree[edge.source_address] += 1
        in_degree[edge.target_address] += 1
    return in_degree, out_degree


def write_nodes_csv(discovered_depth: dict[str, int], edges: list[TransferEdge], root: str, output_path: Path) -> None:
    in_degree, out_degree = degree_counts(edges)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=NODE_CSV_COLUMNS)
        writer.writeheader()
        for address in sorted(discovered_depth, key=lambda item: (discovered_depth[item], item)):
            indeg = in_degree[address]
            outdeg = out_degree[address]
            writer.writerow(
                {
                    "address": address,
                    "is_root": str(address == root).lower(),
                    "discovery_depth": discovered_depth[address],
                    "in_degree": indeg,
                    "out_degree": outdeg,
                    "total_degree": indeg + outdeg,
                }
            )


def edge_title(edge: TransferEdge) -> str:
    rows = [
        ("source_type", edge.source_type),
        ("block_number", str(edge.block_number)),
        ("transaction_hash", edge.transaction_hash),
        ("token_symbol", edge.token_symbol),
        ("token_address", edge.token_address),
        ("raw_value", edge.raw_value),
        ("decimal", edge.decimal),
        ("median_exchange_rate_USD", edge.median_exchange_rate_USD),
        ("transfer_value_USD", edge.transfer_value_USD or "unknown"),
        ("csv_file", edge.csv_file),
    ]
    return "<br>".join(f"<b>{html.escape(k)}</b>: {html.escape(v)}" for k, v in rows)


def write_html_graph(discovered_depth: dict[str, int], edges: list[TransferEdge], root: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    in_degree, out_degree = degree_counts(edges)

    try:
        from pyvis.network import Network
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency 'pyvis'. Install dependencies with: python3 -m pip install -r requirements.txt"
        ) from exc

    node_addresses = set(discovered_depth)
    for edge in edges:
        node_addresses.add(edge.source_address)
        node_addresses.add(edge.target_address)

    net = Network(height="1200px", width="100%", directed=True, notebook=False, bgcolor="#ffffff", font_color="#222222")
    net.force_atlas_2based(gravity=-80, central_gravity=0.01, spring_length=260, spring_strength=0.04, damping=0.4, overlap=0.5)
    net.show_buttons(filter_=["physics", "interaction", "layout", "edges", "nodes"])

    for address in sorted(node_addresses, key=lambda item: (discovered_depth.get(item, sys.maxsize), item)):
        is_root = address == root
        indeg = in_degree[address]
        outdeg = out_degree[address]
        total = indeg + outdeg
        net.add_node(
            address,
            label=f"ROOT: {short_address(address)}" if is_root else short_address(address),
            title=(
                f"<b>{'ROOT ' if is_root else ''}Address</b>: {html.escape(address)}<br>"
                f"<b>Discovery depth</b>: {discovered_depth.get(address, '')}<br>"
                f"<b>In degree</b>: {indeg}<br><b>Out degree</b>: {outdeg}<br><b>Total degree</b>: {total}"
            ),
            color="#ff6b35" if is_root else "#6baed6",
            size=34 if is_root else max(12, min(30, 12 + total)),
            borderWidth=4 if is_root else 1,
        )

    for edge in edges:
        net.add_edge(
            edge.source_address,
            edge.target_address,
            label=f"{edge.block_number}, {edge.transfer_value_label}",
            title=edge_title(edge),
            arrows="to",
            smooth={"enabled": True, "type": "dynamic"},
            color="#7f7f7f" if edge.source_type == "transaction" else "#31a354",
        )

    net.set_options(
        """
        {
          "interaction": {"hover": true, "navigationButtons": true, "keyboard": true, "dragNodes": true, "dragView": true, "zoomView": true},
          "edges": {"font": {"size": 11, "align": "middle"}, "arrows": {"to": {"enabled": true, "scaleFactor": 0.8}}},
          "nodes": {"font": {"size": 15}},
          "physics": {
            "enabled": true,
            "solver": "forceAtlas2Based",
            "stabilization": {"enabled": true, "iterations": 250, "updateInterval": 25},
            "forceAtlas2Based": {"gravitationalConstant": -80, "centralGravity": 0.01, "springLength": 260, "springConstant": 0.04, "damping": 0.4, "avoidOverlap": 0.5}
          }
        }
        """
    )
    net.write_html(str(output_path), notebook=False, open_browser=False)


def validate_paths(args: argparse.Namespace) -> None:
    data_base_dir = Path(args.data_base_dir)
    index_base_dir = Path(args.index_base_dir)
    if not data_base_dir.is_dir():
        raise SystemExit(f"Missing data base directory: {data_base_dir}")
    if not index_base_dir.is_dir():
        raise SystemExit(f"Missing index base directory: {index_base_dir}")
    for output in (args.output_html, args.output_edges_csv, args.output_nodes_csv):
        Path(output).parent.mkdir(parents=True, exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    configure_csv_field_limit()
    args = parse_args(argv)
    configure_logging(args.verbose)
    validate_paths(args)

    root = normalize_address(args.root_address)
    if root is None:
        raise SystemExit("Invalid --root-address. Expected 0x followed by exactly 40 hexadecimal characters.")

    logging.info("Root address: %s", root)
    db_files = discover_sqlite_files(Path(args.index_base_dir))
    if not db_files:
        raise SystemExit(f"No address_block_index_*.sqlite files found under {args.index_base_dir}")
    logging.info("SQLite files discovered: %s", len(db_files))

    token_metadata = load_token_metadata(Path(args.token_metadata_csv))
    logging.info("Token metadata rows loaded: %s", len(token_metadata))

    discovered_depth, edges, stats = trace_graph(args, db_files, token_metadata)

    root = normalize_address(args.root_address) or args.root_address.lower()
    write_edges_csv(edges, Path(args.output_edges_csv))
    write_nodes_csv(discovered_depth, edges, root, Path(args.output_nodes_csv))
    write_html_graph(discovered_depth, edges, root, Path(args.output_html))

    logging.info("Addresses queried in SQLite: %s", stats.addresses_queried)
    logging.info("Block CSV files parsed: %s", stats.block_files_parsed)
    logging.info("Transaction rows considered: %s", stats.transaction_rows_considered)
    logging.info("Token transfer rows considered: %s", stats.token_transfer_rows_considered)
    logging.info("Graph nodes discovered: %s", len(discovered_depth))
    logging.info("Graph edges discovered: %s", len(edges))
    logging.info("Unknown transfer-value edges: %s", stats.unknown_transfer_value_edges)
    logging.info("Duplicate edges skipped: %s", stats.duplicate_edges)
    logging.info("Edges skipped by --min-usd-value: %s", stats.skipped_min_usd_edges)
    logging.info("Missing CSV file lookups: %s", stats.missing_csv_files)
    logging.info("Output HTML path: %s", args.output_html)
    logging.info("Output edges CSV path: %s", args.output_edges_csv)
    logging.info("Output nodes CSV path: %s", args.output_nodes_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
