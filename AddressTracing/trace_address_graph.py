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
import math
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

# Conservative default caps keep accidental full-component traversals from
# running indefinitely or producing HTML files too large for a browser. Use
# --no-limits for intentionally unbounded research runs, or override individual
# values with --max-depth/--max-nodes/--max-edges.
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_NODES = 2_000
DEFAULT_MAX_EDGES = 10_000


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
    skipped_unknown_usd_edges: int = 0
    skipped_max_node_edges: int = 0
    duplicate_edges: int = 0
    enqueued_addresses: int = 0
    traversal_stop_reason: str = "queue exhausted"


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
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH, help="Maximum BFS depth from root. Use --no-limits for no depth cap.")
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES, help="Maximum nodes to discover. Use --no-limits for no node cap.")
    parser.add_argument("--max-edges", type=int, default=DEFAULT_MAX_EDGES, help="Maximum edges to discover. Use --no-limits for no edge cap.")
    parser.add_argument("--no-limits", action="store_true", help="Disable default traversal caps. Use carefully: connected components can become huge.")
    parser.add_argument(
        "--min-usd-value",
        "--min-usd",
        dest="min_usd_value",
        type=Decimal,
        default=None,
        help="Only include edges with known USD value at or above this amount. Unknown token values are included unless --exclude-unknown-usd is set.",
    )
    parser.add_argument(
        "--exclude-unknown-usd",
        action="store_true",
        help="When --min-usd-value/--min-usd is set, drop token-transfer edges whose USD value is unknown.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)
    if args.no_limits:
        args.max_depth = None
        args.max_nodes = None
        args.max_edges = None
    return args


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


def min_usd_filter_reason(edge: TransferEdge, min_usd_value: Decimal | None, exclude_unknown_usd: bool) -> str | None:
    """Return a skip reason for the min-USD filter, or None when the edge passes."""
    if min_usd_value is None:
        return None
    if edge.transfer_value_USD == "":
        return "unknown_usd" if exclude_unknown_usd else None
    value = parse_decimal(edge.transfer_value_USD)
    if value is None:
        return "unknown_usd" if exclude_unknown_usd else None
    if value < min_usd_value:
        return "below_min_usd"
    return None


def trace_graph(args: argparse.Namespace, db_files: list[Path], token_metadata: dict[str, TokenMetadata]) -> tuple[dict[str, int], list[TransferEdge], TraceStats]:
    root = normalize_address(args.root_address)
    if root is None:
        raise SystemExit("Invalid --root-address. Expected 0x followed by exactly 40 hexadecimal characters.")

    if args.max_depth is None or args.max_nodes is None or args.max_edges is None:
        logging.warning("One or more traversal safety limits are unset. Full connected-component traversal may become very large.")
    else:
        logging.info(
            "Traversal safety limits active: max_depth=%s max_nodes=%s max_edges=%s",
            args.max_depth,
            args.max_nodes,
            args.max_edges,
        )

    stats = TraceStats()
    address_block_cache: dict[str, set[int]] = {}
    block_cache: dict[int, BlockRows] = {}
    discovered_depth: dict[str, int] = {root: 0}
    queued_or_queried: set[str] = {root}
    edges: list[TransferEdge] = []
    edge_keys: set[tuple[str, str, int, str, str, str, str]] = set()
    queue: deque[tuple[str, int]] = deque([(root, 0)])
    queried_addresses: set[str] = set()
    stats.enqueued_addresses = 1

    while queue:
        current, depth = queue.popleft()
        if current in queried_addresses:
            continue
        if args.max_depth is not None and depth >= args.max_depth:
            continue
        queried_addresses.add(current)

        blocks = sorted(
            block for block in query_address_blocks(current, db_files, address_block_cache, stats)
            if args.start_block <= block <= args.end_block
        )
        starting_edges = len(edges)
        starting_nodes = len(discovered_depth)
        starting_duplicates = stats.duplicate_edges
        starting_min_usd_skips = stats.skipped_min_usd_edges
        starting_unknown_usd_skips = stats.skipped_unknown_usd_edges
        starting_max_node_skips = stats.skipped_max_node_edges
        starting_enqueued = stats.enqueued_addresses

        logging.info(
            "Query %s depth=%s matched %s unique block(s); nodes=%s edges=%s queue=%s",
            short_address(current), depth, len(blocks), len(discovered_depth), len(edges), len(queue),
        )

        for block_number in blocks:
            if args.max_edges is not None and len(edges) >= args.max_edges:
                stats.traversal_stop_reason = f"--max-edges={args.max_edges} reached"
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
                min_usd_skip_reason = min_usd_filter_reason(
                    edge, args.min_usd_value, args.exclude_unknown_usd
                )
                if min_usd_skip_reason == "below_min_usd":
                    stats.skipped_min_usd_edges += 1
                    continue
                if min_usd_skip_reason == "unknown_usd":
                    stats.skipped_unknown_usd_edges += 1
                    continue
                key = edge.duplicate_key()
                if key in edge_keys:
                    stats.duplicate_edges += 1
                    continue
                if args.max_edges is not None and len(edges) >= args.max_edges:
                    stats.traversal_stop_reason = f"--max-edges={args.max_edges} reached"
                    logging.warning("Stopping traversal because --max-edges=%s was reached.", args.max_edges)
                    return discovered_depth, edges, stats

                missing_endpoints = [
                    address
                    for address in (edge.source_address, edge.target_address)
                    if address not in discovered_depth
                ]
                if args.max_nodes is not None and len(discovered_depth) + len(set(missing_endpoints)) > args.max_nodes:
                    stats.skipped_max_node_edges += 1
                    if stats.traversal_stop_reason == "queue exhausted":
                        stats.traversal_stop_reason = f"--max-nodes={args.max_nodes} reached"
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
                        stats.enqueued_addresses += 1

        logging.info(
            "Finished %s depth=%s: added_edges=%s added_nodes=%s enqueued_neighbors=%s "
            "duplicates_seen=%s min_usd_skips=%s unknown_usd_skips=%s max_node_skips=%s remaining_queue=%s",
            short_address(current),
            depth,
            len(edges) - starting_edges,
            len(discovered_depth) - starting_nodes,
            stats.enqueued_addresses - starting_enqueued,
            stats.duplicate_edges - starting_duplicates,
            stats.skipped_min_usd_edges - starting_min_usd_skips,
            stats.skipped_unknown_usd_edges - starting_unknown_usd_skips,
            stats.skipped_max_node_edges - starting_max_node_skips,
            len(queue),
        )

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



def build_static_graph_fallback(discovered_depth: dict[str, int], edges: list[TransferEdge], root: str) -> str:
    """Build a dependency-free radial SVG/list fallback so HTML never looks blank.

    Pyvis provides the interactive graph, but browser security settings, old pyvis
    versions, missing local assets, or GitHub's normal source viewer can prevent
    the JavaScript graph from rendering. This fallback is intentionally plain
    HTML/SVG and lays nodes out in depth rings rather than one long column.
    """
    max_preview_nodes = 350
    max_preview_edges = 600
    all_node_addresses = sorted(
        set(discovered_depth)
        | {edge.source_address for edge in edges}
        | {edge.target_address for edge in edges},
        key=lambda item: (discovered_depth.get(item, sys.maxsize), item),
    )
    if not all_node_addresses:
        return ""

    node_addresses = all_node_addresses[:max_preview_nodes]
    preview_node_set = set(node_addresses)
    hidden_node_count = max(0, len(all_node_addresses) - len(node_addresses))

    # Make the static preview physically large and scrollable. The previous
    # preview fit the whole graph into a small box, making dense rings hard to
    # inspect. A larger SVG plus in-page zoom controls is more useful for
    # large radial previews.
    width = 2200
    height = 1600
    center_x = width // 2
    center_y = height // 2
    min_radius = 115
    max_radius = min(width, height) // 2 - 90

    nodes_by_depth: dict[int, list[str]] = {}
    for address in node_addresses:
        depth = discovered_depth.get(address, max(discovered_depth.values(), default=0) + 1)
        nodes_by_depth.setdefault(depth, []).append(address)

    sorted_depths = sorted(nodes_by_depth)
    depth_to_ring: dict[int, int] = {depth: index for index, depth in enumerate(sorted_depths)}
    ring_count = max(1, len(sorted_depths) - 1)
    positions: dict[str, tuple[float, float]] = {}
    if root in preview_node_set:
        positions[root] = (center_x, center_y)

    for depth in sorted_depths:
        addresses = [address for address in nodes_by_depth[depth] if address != root]
        if not addresses:
            continue
        ring_index = max(1, depth_to_ring[depth])
        radius = min(max_radius, min_radius + (max_radius - min_radius) * ring_index / max(1, ring_count))
        angle_offset = (math.pi / len(addresses)) if depth % 2 else 0.0
        for index, address in enumerate(addresses):
            angle = angle_offset + (2 * math.pi * index / len(addresses))
            positions[address] = (center_x + radius * math.cos(angle), center_y + radius * math.sin(angle))

    # If all nodes were at depth 0 except the root, distribute them on one ring.
    missing_positions = [address for address in node_addresses if address not in positions]
    for index, address in enumerate(missing_positions):
        angle = 2 * math.pi * index / max(1, len(missing_positions))
        positions[address] = (center_x + min_radius * math.cos(angle), center_y + min_radius * math.sin(angle))

    svg_parts = [
        f'<svg id="address-trace-radial-svg" viewBox="0 0 {width} {height}" role="img" aria-label="Static radial Ethereum address graph preview" '
        'style="width:2200px;max-width:none;height:auto;border:1px solid #d0d7de;background:#fff;border-radius:8px;transform-origin:0 0;">',
        '<defs><marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">'
        '<polygon points="0 0, 10 3.5, 0 7" fill="#555"></polygon></marker></defs>',
    ]

    for depth in sorted_depths:
        if depth == 0:
            continue
        ring_index = max(1, depth_to_ring[depth])
        radius = min(max_radius, min_radius + (max_radius - min_radius) * ring_index / max(1, ring_count))
        svg_parts.append(
            f'<circle cx="{center_x}" cy="{center_y}" r="{radius:.1f}" fill="none" '
            'stroke="#d8dee4" stroke-dasharray="8 8" stroke-width="1"></circle>'
        )
        svg_parts.append(
            f'<text x="{center_x + radius + 8:.1f}" y="{center_y - 8}" font-size="12" fill="#57606a">'
            f'depth {html.escape(str(depth))}</text>'
        )

    rendered_edge_count = 0
    for edge in edges[:max_preview_edges]:
        source_pos = positions.get(edge.source_address)
        target_pos = positions.get(edge.target_address)
        if source_pos is None or target_pos is None:
            continue
        sx, sy = source_pos
        tx, ty = target_pos
        stroke = "#31a354" if edge.source_type == "token_transfer" else "#7f7f7f"
        svg_parts.append(
            f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{tx:.1f}" y2="{ty:.1f}" '
            f'stroke="{stroke}" stroke-opacity="0.45" stroke-width="1.6" marker-end="url(#arrowhead)"></line>'
        )
        rendered_edge_count += 1

    for address in node_addresses:
        x, y = positions[address]
        is_root = address == root
        total_degree = sum(1 for edge in edges if edge.source_address == address or edge.target_address == address)
        fill = "#ff6b35" if is_root else "#6baed6"
        radius = 24 if is_root else max(8, min(18, 8 + math.sqrt(total_degree)))
        label = f"ROOT: {short_address(address)}" if is_root else short_address(address)
        svg_parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{fill}" '
            'stroke="#24292f" stroke-width="1.5"></circle>'
        )
        # Label root and the highest-degree non-root nodes; too many labels make the preview unreadable.
        if is_root or total_degree >= 2 or len(node_addresses) <= 40:
            svg_parts.append(
                f'<text x="{x:.1f}" y="{y + radius + 14:.1f}" text-anchor="middle" font-size="12" fill="#24292f">'
                f'{html.escape(label)}</text>'
            )

    if hidden_node_count or len(edges) > rendered_edge_count:
        svg_parts.append(
            f'<text x="{center_x}" y="{height - 24}" text-anchor="middle" font-size="13" fill="#57606a">'
            f'Preview rendered {len(node_addresses)} of {len(all_node_addresses)} nodes and {rendered_edge_count} of {len(edges)} edges; CSV outputs contain the full graph.</text>'
        )
    svg_parts.append("</svg>")

    edge_rows = []
    for edge in edges[:25]:
        edge_rows.append(
            "<tr>"
            f"<td>{html.escape(short_address(edge.source_address))}</td>"
            f"<td>{html.escape(short_address(edge.target_address))}</td>"
            f"<td>{html.escape(edge.source_type)}</td>"
            f"<td>{edge.block_number}</td>"
            f"<td>{html.escape(edge.transfer_value_label)}</td>"
            "</tr>"
        )
    if not edge_rows:
        edge_rows.append('<tr><td colspan="5">No retained edges were discovered.</td></tr>')

    return f"""
<div id="address-trace-static-fallback" style="font-family:Arial, sans-serif; margin:16px; padding:16px; border:1px solid #d0d7de; border-radius:10px; background:#f6f8fa;">
  <h2 style="margin-top:0;">Address trace graph preview</h2>
  <p style="margin:0 0 10px 0; color:#57606a;">
    This radial SVG preview is always rendered and groups nodes by BFS depth rings around the root. The interactive pyvis graph is below it. If the interactive graph area is blank, the browser did not load or execute pyvis/vis-network JavaScript, but graph data was generated.
  </p>
  <p style="margin:0 0 12px 0;"><strong>Nodes:</strong> {len(all_node_addresses)} &nbsp; <strong>Edges:</strong> {len(edges)} &nbsp; <strong>Root:</strong> <code>{html.escape(root)}</code></p>
  <p style="margin:0 0 12px 0; color:#57606a;"><span style="color:#ff6b35;">●</span> root &nbsp; <span style="color:#6baed6;">●</span> address &nbsp; <span style="color:#31a354;">━</span> token transfer &nbsp; <span style="color:#7f7f7f;">━</span> ETH transaction</p>
  <div id="address-trace-zoom-controls" style="position:sticky;top:0;z-index:10;display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px;margin-bottom:8px;background:#ffffff;border:1px solid #d0d7de;border-radius:8px;box-shadow:0 1px 2px rgba(0,0,0,0.05);">
    <strong>Static preview zoom:</strong>
    <button type="button" id="address-trace-zoom-out">−</button>
    <input type="range" id="address-trace-zoom-slider" min="50" max="500" value="100" step="10" style="width:260px;">
    <button type="button" id="address-trace-zoom-in">+</button>
    <button type="button" id="address-trace-zoom-reset">Reset</button>
    <span id="address-trace-zoom-label">100%</span>
    <span style="color:#57606a;">Use the mouse wheel/trackpad inside the box after zooming, or drag the scrollbars.</span>
  </div>
  <div id="address-trace-svg-scroll" style="width:100%;height:78vh;overflow:auto;border:1px solid #d0d7de;border-radius:8px;background:#fff;">
    {''.join(svg_parts)}
  </div>
  <details style="margin-top:14px;">
    <summary>First retained edges</summary>
    <table style="border-collapse:collapse;margin-top:10px;background:#fff;">
      <thead><tr><th>Source</th><th>Target</th><th>Type</th><th>Block</th><th>Value</th></tr></thead>
      <tbody>{''.join(edge_rows)}</tbody>
    </table>
  </details>
</div>
<script>
(function() {{
  function setupStaticPreviewZoom() {{
    var svg = document.getElementById('address-trace-radial-svg');
    var slider = document.getElementById('address-trace-zoom-slider');
    var label = document.getElementById('address-trace-zoom-label');
    var scrollBox = document.getElementById('address-trace-svg-scroll');
    if (!svg || !slider || !label) return;
    function applyZoom(percent) {{
      percent = Math.max(50, Math.min(500, percent));
      slider.value = String(percent);
      label.textContent = percent + '%';
      var scale = percent / 100;
      svg.style.transform = 'scale(' + scale + ')';
      svg.style.marginRight = Math.max(0, (scale - 1) * svg.viewBox.baseVal.width) + 'px';
      svg.style.marginBottom = Math.max(0, (scale - 1) * svg.viewBox.baseVal.height) + 'px';
    }}
    slider.addEventListener('input', function() {{ applyZoom(parseInt(slider.value, 10)); }});
    document.getElementById('address-trace-zoom-in').addEventListener('click', function() {{ applyZoom(parseInt(slider.value, 10) + 25); }});
    document.getElementById('address-trace-zoom-out').addEventListener('click', function() {{ applyZoom(parseInt(slider.value, 10) - 25); }});
    document.getElementById('address-trace-zoom-reset').addEventListener('click', function() {{ applyZoom(100); if (scrollBox) {{ scrollBox.scrollLeft = 0; scrollBox.scrollTop = 0; }} }});
    if (scrollBox) {{
      scrollBox.addEventListener('wheel', function(event) {{
        if (!event.ctrlKey && !event.metaKey) return;
        event.preventDefault();
        applyZoom(parseInt(slider.value, 10) + (event.deltaY < 0 ? 25 : -25));
      }}, {{ passive: false }});
    }}
    applyZoom(140);
  }}
  function fitInteractiveNetwork() {{
    try {{
      if (typeof network !== 'undefined' && network && typeof network.fit === 'function') {{
        network.once('stabilized', function() {{ network.fit({{animation: true}}); }});
        setTimeout(function() {{ network.fit({{animation: true}}); }}, 1000);
      }}
    }} catch (error) {{
      console.warn('AddressTracing: interactive pyvis fit failed', error);
    }}
  }}
  function setupInteractiveZoomButtons() {{
    var networkDiv = document.getElementById('mynetwork');
    if (!networkDiv || document.getElementById('address-trace-pyvis-controls')) return;
    var controls = document.createElement('div');
    controls.id = 'address-trace-pyvis-controls';
    controls.style.cssText = 'position:sticky;top:0;z-index:10;display:flex;gap:8px;align-items:center;padding:8px;background:#fff;border:1px solid #d0d7de;border-radius:8px;margin:16px;';
    controls.innerHTML = '<strong>Interactive graph:</strong><button type="button" id="address-trace-pyvis-zoom-in">Zoom in</button><button type="button" id="address-trace-pyvis-zoom-out">Zoom out</button><button type="button" id="address-trace-pyvis-fit">Fit graph</button><span style="color:#57606a;">Mouse wheel and trackpad zoom should also work inside the graph canvas.</span>';
    networkDiv.parentNode.insertBefore(controls, networkDiv);
    function zoomBy(multiplier) {{
      try {{
        if (typeof network === 'undefined' || !network) return;
        var scale = network.getScale ? network.getScale() : 1;
        network.moveTo({{ scale: Math.max(0.02, Math.min(20, scale * multiplier)), animation: true }});
      }} catch (error) {{ console.warn('AddressTracing: pyvis zoom failed', error); }}
    }}
    document.getElementById('address-trace-pyvis-zoom-in').addEventListener('click', function() {{ zoomBy(1.35); }});
    document.getElementById('address-trace-pyvis-zoom-out').addEventListener('click', function() {{ zoomBy(0.75); }});
    document.getElementById('address-trace-pyvis-fit').addEventListener('click', function() {{ if (typeof network !== 'undefined' && network && network.fit) network.fit({{animation: true}}); }});
  }}
  function initializeAddressTraceView() {{
    setupStaticPreviewZoom();
    fitInteractiveNetwork();
    setupInteractiveZoomButtons();
  }}
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', initializeAddressTraceView);
  }} else {{
    initializeAddressTraceView();
  }}
}})();
</script>
"""


def inject_static_graph_fallback(output_path: Path, fallback_html: str) -> None:
    if not fallback_html:
        return
    try:
        content = output_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logging.warning("Could not inject static graph fallback into %s: %s", output_path, exc)
        return

    if "address-trace-static-fallback" in content:
        return
    body_match = re.search(r"<body[^>]*>", content, flags=re.IGNORECASE)
    if body_match:
        insert_at = body_match.end()
        content = content[:insert_at] + "\n" + fallback_html + "\n" + content[insert_at:]
    else:
        content = fallback_html + "\n" + content
    try:
        output_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        logging.warning("Could not write static graph fallback into %s: %s", output_path, exc)


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

    # Use inline pyvis/vis-network resources so address_trace_graph.html is a
    # single portable file. Pyvis's default local resource mode can produce an
    # HTML file that points at sibling lib/ assets; if those assets are not
    # uploaded or served next to the HTML, the browser opens a blank page.
    try:
        net = Network(
            height="85vh",
            width="100%",
            directed=True,
            notebook=False,
            bgcolor="#ffffff",
            font_color="#222222",
            cdn_resources="in_line",
        )
    except TypeError:
        logging.warning(
            "Installed pyvis version does not support cdn_resources='in_line'; "
            "generated HTML may require pyvis local asset files next to the HTML."
        )
        net = Network(height="85vh", width="100%", directed=True, notebook=False, bgcolor="#ffffff", font_color="#222222")
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
            size=28 if is_root else max(10, min(22, 10 + math.sqrt(total))),
            borderWidth=4 if is_root else 1,
        )

    for edge in edges:
        net.add_edge(
            edge.source_address,
            edge.target_address,
            label=f"{edge.block_number}, {edge.transfer_value_label}" if len(edges) <= 250 else "",
            title=edge_title(edge),
            arrows="to",
            smooth={"enabled": True, "type": "continuous", "roundness": 0.25},
            color="#7f7f7f" if edge.source_type == "transaction" else "#31a354",
        )

    net.set_options(
        """
        {
          "interaction": {"hover": true, "navigationButtons": true, "keyboard": true, "dragNodes": true, "dragView": true, "zoomView": true, "hideEdgesOnDrag": true},
          "edges": {"font": {"size": 9, "align": "middle", "strokeWidth": 2}, "arrows": {"to": {"enabled": true, "scaleFactor": 0.55}}, "smooth": {"enabled": true, "type": "continuous", "roundness": 0.25}},
          "nodes": {"font": {"size": 11, "strokeWidth": 3}, "shape": "dot"},
          "physics": {
            "enabled": true,
            "solver": "barnesHut",
            "stabilization": {"enabled": true, "iterations": 600, "updateInterval": 25, "fit": true},
            "barnesHut": {"gravitationalConstant": -12000, "centralGravity": 0.08, "springLength": 180, "springConstant": 0.025, "damping": 0.2, "avoidOverlap": 0.8},
            "minVelocity": 0.75
          }
        }
        """
    )
    net.write_html(str(output_path), notebook=False, open_browser=False)
    inject_static_graph_fallback(output_path, build_static_graph_fallback(discovered_depth, edges, root))
    warn_if_html_references_local_pyvis_assets(output_path)


def warn_if_html_references_local_pyvis_assets(output_path: Path) -> None:
    try:
        content = output_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logging.warning("Could not inspect generated HTML %s: %s", output_path, exc)
        return

    local_asset_markers = ('src="lib/', "src='lib/", 'href="lib/', "href='lib/")
    if any(marker in content for marker in local_asset_markers):
        logging.warning(
            "Generated HTML references pyvis local lib/ assets. If the graph opens blank, "
            "copy the generated lib/ folder next to the HTML or upgrade pyvis so inline resources are supported."
        )


def validate_paths(args: argparse.Namespace) -> None:
    for option_name in ("max_depth", "max_nodes", "max_edges"):
        option_value = getattr(args, option_name)
        if option_value is not None and option_value < 0:
            raise SystemExit(f"--{option_name.replace('_', '-')} must be non-negative")

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
    logging.info("Edges skipped below --min-usd-value/--min-usd: %s", stats.skipped_min_usd_edges)
    logging.info("Unknown-USD edges skipped by --exclude-unknown-usd: %s", stats.skipped_unknown_usd_edges)
    logging.info("Edges skipped by --max-nodes: %s", stats.skipped_max_node_edges)
    logging.info("Addresses enqueued for BFS: %s", stats.enqueued_addresses)
    logging.info("Traversal stop reason: %s", stats.traversal_stop_reason)
    logging.info("Missing CSV file lookups: %s", stats.missing_csv_files)
    logging.info("Output HTML path: %s", args.output_html)
    logging.info("Output edges CSV path: %s", args.output_edges_csv)
    logging.info("Output nodes CSV path: %s", args.output_nodes_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
