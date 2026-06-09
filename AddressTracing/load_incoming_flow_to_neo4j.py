#!/usr/bin/env python3
"""Trace incoming Ethereum value flows and load the resulting graph into Neo4j.

SQLite occurrence indexes are used only to locate candidate block numbers. Full
transaction and token-transfer properties always come from the original CSVs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import math
import os
import re
import sqlite3
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Iterable, Iterator

ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
DB_RE = re.compile(r"^address_block_index_(\d+)_(\d+)\.sqlite$")
OUTER_RE = re.compile(r"^Ethereum_TT_(\d+)_(\d+)$")
NULL_TEXT = {"", "nan", "none", "null", "n/a", "na"}
TRANSACTION_COLUMNS = {"block_number", "hash", "from_address", "to_address", "value"}
TOKEN_TRANSFER_COLUMNS = {
    "block_number",
    "transaction_hash",
    "token_address",
    "from_address",
    "to_address",
    "value",
}


def configure_csv_field_limit() -> None:
    """Raise the CSV parser limit, backing off on platforms with smaller C longs."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


@dataclass(frozen=True)
class TokenMetadata:
    symbol: str
    decimal: int | None
    median_exchange_rate_usd: Decimal | None


@dataclass
class TraceStats:
    addresses_queried: int = 0
    occurrence_rows_found: int = 0
    block_numbers_found: int = 0
    block_files_parsed: int = 0
    transaction_rows_considered: int = 0
    token_transfer_rows_considered: int = 0
    malformed_rows_skipped: int = 0
    invalid_address_rows_skipped: int = 0
    zero_value_eth_skipped: int = 0
    unknown_value_transfers: int = 0
    unknown_value_transfers_excluded: int = 0
    edges_skipped_by_min_usd_value: int = 0
    addresses_not_expanded_by_occurrence_limit: int = 0
    missing_csv_files: int = 0
    edges_collected: int = 0
    nodes_collected: int = 0
    neo4j_nodes_written: int = 0
    neo4j_relationships_written: int = 0
    stop_reason: str | None = None


@dataclass(frozen=True)
class Transfer:
    transfer_id: str
    graph_run_id: str
    from_address: str
    to_address: str
    source_type: str
    block_number: int
    transaction_hash: str
    token_address: str
    token_symbol: str
    raw_value: str
    decimal: int | None
    median_exchange_rate_USD: float | None
    transfer_value_USD: float | None
    transfer_value_label: str
    display_label: str
    log_index: str
    csv_file: str


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def non_negative_decimal(value: str) -> Decimal:
    parsed = parse_decimal(value)
    if parsed is None or parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative number")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an upstream incoming-flow graph and load it into Neo4j for Bloom."
    )
    parser.add_argument("--root-address", required=True)
    parser.add_argument("--data-base-dir", required=True, type=Path)
    parser.add_argument("--index-base-dir", required=True, type=Path)
    parser.add_argument("--token-metadata-csv", required=True, type=Path)
    parser.add_argument("--start-block", required=True, type=int)
    parser.add_argument("--end-block", required=True, type=int)
    parser.add_argument("--outer-folder-size", type=positive_int, default=7200)
    parser.add_argument("--inner-chunk-size", type=positive_int, default=100)
    parser.add_argument(
        "--eth-usd-price", type=non_negative_decimal, default=Decimal("2000.0")
    )
    parser.add_argument("--neo4j-uri", default="bolt://localhost:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument(
        "--neo4j-password", help="Overrides NEO4J_PASSWORD when both are set."
    )
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--graph-run-id")
    parser.add_argument("--batch-size", type=positive_int, default=500)
    parser.add_argument("--max-depth", type=non_negative_int, default=3)
    parser.add_argument("--max-nodes", type=positive_int, default=5000)
    parser.add_argument("--max-edges", type=positive_int, default=20000)
    parser.add_argument("--max-blocks-per-address", type=positive_int, default=1000)
    parser.add_argument("--max-runtime-seconds", type=positive_int, default=1800)
    parser.add_argument("--min-usd-value", type=non_negative_decimal)
    parser.add_argument("--skip-address-if-occurrence-over", type=positive_int)
    parser.add_argument("--include-zero-eth-transactions", action="store_true")
    parser.add_argument("--exclude-unknown-value-transfers", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"
    )
    args = parser.parse_args(argv)

    if args.start_block > args.end_block:
        parser.error("--start-block must be less than or equal to --end-block")
    root = normalize_address(args.root_address)
    if root is None:
        parser.error(
            "--root-address must be 0x followed by exactly 40 hexadecimal characters"
        )
    args.root_address = root
    if not args.graph_run_id:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.graph_run_id = f"incoming_{root}_{timestamp}"
    if not str(args.graph_run_id).strip():
        parser.error("--graph-run-id cannot be empty")
    args.graph_run_id = str(args.graph_run_id).strip()
    args.neo4j_password = args.neo4j_password or os.environ.get("NEO4J_PASSWORD")
    if not args.dry_run and not args.neo4j_password:
        parser.error(
            "Neo4j password is required via --neo4j-password or NEO4J_PASSWORD"
        )
    return args


def normalize_address(value: object) -> str | None:
    text = str(value).strip().lower() if value is not None else ""
    return text if ADDRESS_RE.fullmatch(text) else None


def normalize_hash(value: object) -> str:
    text = str(value).strip().lower() if value is not None else ""
    return "" if text in NULL_TEXT else text


def clean_text(value: object) -> str:
    text = str(value).strip() if value is not None else ""
    return "" if text.lower() in NULL_TEXT else text


def parse_decimal(value: object) -> Decimal | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def parse_int(value: object) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            decimal_value = Decimal(text)
            return (
                int(decimal_value)
                if decimal_value == decimal_value.to_integral_value()
                else None
            )
        except (InvalidOperation, ValueError, OverflowError):
            return None


def decimal_as_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def format_usd(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    absolute = abs(value)
    if absolute == 0:
        return "$0.00"
    if absolute < Decimal("0.01"):
        rendered = f"{value:.8f}".rstrip("0").rstrip(".")
        return f"${rendered}"
    return f"${value:,.2f}"


def shortened_address(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}"


def load_token_metadata(path: Path) -> dict[str, TokenMetadata]:
    metadata: dict[str, TokenMetadata] = {}
    if not path.is_file():
        logging.warning(
            "Token metadata CSV not found; token USD values will be unknown: %s", path
        )
        return metadata
    try:
        with path.open(
            "r", newline="", encoding="utf-8-sig", errors="replace"
        ) as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            if "token_address" not in fields:
                logging.warning(
                    "Token metadata CSV lacks token_address; ignoring file: %s", path
                )
                return metadata
            for row in reader:
                address = normalize_address(row.get("token_address"))
                if address is None:
                    continue
                decimal = parse_int(row.get("decimal"))
                if decimal is not None and not 0 <= decimal <= 255:
                    decimal = None
                metadata[address] = TokenMetadata(
                    symbol=clean_text(row.get("token_symbol")) or "TOKEN",
                    decimal=decimal,
                    median_exchange_rate_usd=parse_decimal(
                        row.get("median_exchange_rate_USD")
                    ),
                )
    except (OSError, csv.Error) as exc:
        logging.warning("Could not read token metadata CSV %s: %s", path, exc)
    return metadata


def discover_sqlite_files(
    index_base_dir: Path, start_block: int, end_block: int
) -> list[Path]:
    files: list[tuple[int, int, Path]] = []
    if not index_base_dir.is_dir():
        return []
    for path in index_base_dir.rglob("address_block_index_*.sqlite"):
        if not path.is_file():
            continue
        match = DB_RE.fullmatch(path.name)
        if not match:
            continue
        db_start, db_end = int(match.group(1)), int(match.group(2))
        if db_end < start_block or db_start > end_block:
            continue
        files.append((db_start, db_end, path))
    return [
        item[2]
        for item in sorted(files, key=lambda item: (item[0], item[1], str(item[2])))
    ]


def query_address_occurrences(
    address: str,
    db_files: Iterable[Path],
    start_block: int,
    end_block: int,
    stats: TraceStats,
) -> tuple[list[int], int]:
    """Return candidate blocks and the occurrence-row count for an address."""
    blocks: set[int] = set()
    occurrence_count = 0
    for db_path in db_files:
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT block_number, role, source FROM occurrences WHERE address = ?",
                    (address,),
                )
                for raw_block, _role, _source in rows:
                    block = parse_int(raw_block)
                    if block is None or block < start_block or block > end_block:
                        continue
                    occurrence_count += 1
                    blocks.add(block)
            finally:
                connection.close()
        except sqlite3.Error as exc:
            logging.warning("Skipping unreadable SQLite index %s: %s", db_path, exc)
    stats.addresses_queried += 1
    stats.occurrence_rows_found += occurrence_count
    stats.block_numbers_found += len(blocks)
    return sorted(blocks), occurrence_count


class CsvPathResolver:
    """Resolve mathematical CSV paths first and use cached recursive fallback searches."""

    def __init__(
        self, data_base_dir: Path, global_start: int, outer_size: int, chunk_size: int
    ):
        self.data_base_dir = data_base_dir
        self.global_start = global_start
        self.outer_size = outer_size
        self.chunk_size = chunk_size
        self._outer_folders: list[tuple[int, int, Path]] | None = None
        self._cache: dict[tuple[str, int], Path | None] = {}

    def _range_start(self, block: int, size: int) -> int:
        return self.global_start + ((block - self.global_start) // size) * size

    def expected_path(self, source_type: str, block: int) -> Path:
        outer_start = self._range_start(block, self.outer_size)
        outer_end = outer_start + self.outer_size - 1
        chunk_start = self._range_start(block, self.chunk_size)
        chunk_end = chunk_start + self.chunk_size - 1
        prefix = "transaction" if source_type == "transaction" else "token_transfer"
        return (
            self.data_base_dir
            / f"Ethereum_TT_{outer_start}_{outer_end}"
            / f"Transaction_TokenTransfer_{chunk_start}_{chunk_end}"
            / f"{prefix}_{block}.csv"
        )

    def _matching_outer(self, block: int) -> Path | None:
        if self._outer_folders is None:
            folders: list[tuple[int, int, Path]] = []
            for child in self.data_base_dir.glob("Ethereum_TT_*_*"):
                match = OUTER_RE.fullmatch(child.name)
                if child.is_dir() and match:
                    folders.append((int(match.group(1)), int(match.group(2)), child))
            self._outer_folders = sorted(folders)
        return next(
            (path for start, end, path in self._outer_folders if start <= block <= end),
            None,
        )

    def resolve(self, source_type: str, block: int) -> Path | None:
        key = (source_type, block)
        if key in self._cache:
            return self._cache[key]
        expected = self.expected_path(source_type, block)
        if expected.is_file():
            self._cache[key] = expected
            return expected
        prefix = "transaction" if source_type == "transaction" else "token_transfer"
        filename = f"{prefix}_{block}.csv"
        search_root = self._matching_outer(block) or self.data_base_dir
        matches = sorted(path for path in search_root.rglob(filename) if path.is_file())
        result = matches[0] if matches else None
        if result:
            logging.debug("Used fallback CSV path for block %s: %s", block, result)
        self._cache[key] = result
        return result


def calculate_token_usd(
    raw_value: Decimal, metadata: TokenMetadata | None
) -> Decimal | None:
    if (
        metadata is None
        or metadata.decimal is None
        or metadata.median_exchange_rate_usd is None
    ):
        return None
    # Raw token value / 10^decimals gives token units. Multiplying by a USD-per-token
    # median exchange rate gives an approximate USD amount.
    with localcontext() as context:
        context.prec = 80
        return (
            raw_value / (Decimal(10) ** metadata.decimal)
        ) * metadata.median_exchange_rate_usd


def calculate_eth_usd(raw_value: Decimal, eth_usd_price: Decimal) -> Decimal:
    # Ethereum transaction value is wei. 10^18 wei = 1 ETH, so division by 10^18
    # gives ETH; multiplying by the fixed ETH/USD assumption gives approximate USD.
    with localcontext() as context:
        context.prec = 80
        return (raw_value / (Decimal(10) ** 18)) * eth_usd_price


def make_transfer_id(transfer: dict[str, Any]) -> str:
    fields = (
        transfer["graph_run_id"],
        transfer["source_type"],
        transfer["block_number"],
        transfer["transaction_hash"],
        transfer["from_address"],
        transfer["to_address"],
        transfer["token_address"],
        transfer["raw_value"],
        transfer["log_index"],
        transfer["csv_file"],
    )
    return hashlib.sha256(
        "\x1f".join(str(item) for item in fields).encode("utf-8")
    ).hexdigest()


def iter_incoming_transfers(
    csv_path: Path,
    source_type: str,
    current_address: str,
    args: argparse.Namespace,
    token_metadata: dict[str, TokenMetadata],
    stats: TraceStats,
) -> Iterator[Transfer]:
    required = (
        TRANSACTION_COLUMNS if source_type == "transaction" else TOKEN_TRANSFER_COLUMNS
    )
    try:
        with csv_path.open(
            "r", newline="", encoding="utf-8-sig", errors="replace"
        ) as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            missing = sorted(required - fields)
            if missing:
                logging.warning(
                    "Skipping %s; missing required columns: %s",
                    csv_path,
                    ", ".join(missing),
                )
                return
            stats.block_files_parsed += 1
            for row in reader:
                if source_type == "transaction":
                    stats.transaction_rows_considered += 1
                else:
                    stats.token_transfer_rows_considered += 1
                try:
                    receiver = normalize_address(row.get("to_address"))
                    if receiver != current_address:
                        continue
                    sender = normalize_address(row.get("from_address"))
                    if sender is None:
                        stats.invalid_address_rows_skipped += 1
                        continue
                    block = parse_int(row.get("block_number"))
                    raw_decimal = parse_decimal(row.get("value"))
                    if (
                        block is None
                        or raw_decimal is None
                        or block < args.start_block
                        or block > args.end_block
                    ):
                        stats.malformed_rows_skipped += 1
                        continue
                    raw_value = clean_text(row.get("value"))
                    if source_type == "transaction":
                        if raw_decimal == 0 and not args.include_zero_eth_transactions:
                            stats.zero_value_eth_skipped += 1
                            continue
                        transaction_hash = normalize_hash(row.get("hash"))
                        token_address = ""
                        token_symbol = "ETH"
                        decimal_places: int | None = 18
                        exchange_rate = args.eth_usd_price
                        usd_value = calculate_eth_usd(raw_decimal, args.eth_usd_price)
                        log_index = ""
                    else:
                        transaction_hash = normalize_hash(row.get("transaction_hash"))
                        token_address = (
                            normalize_address(row.get("token_address")) or ""
                        )
                        metadata = token_metadata.get(token_address)
                        token_symbol = metadata.symbol if metadata else "TOKEN"
                        decimal_places = metadata.decimal if metadata else None
                        exchange_rate = (
                            metadata.median_exchange_rate_usd if metadata else None
                        )
                        usd_value = calculate_token_usd(raw_decimal, metadata)
                        log_index = clean_text(row.get("log_index"))
                    if usd_value is None:
                        if args.exclude_unknown_value_transfers:
                            stats.unknown_value_transfers_excluded += 1
                            continue
                    elif (
                        args.min_usd_value is not None
                        and usd_value < args.min_usd_value
                    ):
                        stats.edges_skipped_by_min_usd_value += 1
                        continue
                    label = format_usd(usd_value)
                    values: dict[str, Any] = {
                        "graph_run_id": args.graph_run_id,
                        "from_address": sender,
                        "to_address": receiver,
                        "source_type": source_type,
                        "block_number": block,
                        "transaction_hash": transaction_hash,
                        "token_address": token_address,
                        "token_symbol": token_symbol,
                        "raw_value": raw_value,
                        "decimal": decimal_places,
                        "median_exchange_rate_USD": decimal_as_float(exchange_rate),
                        "transfer_value_USD": decimal_as_float(usd_value),
                        "transfer_value_label": label,
                        "display_label": f"{block} | {token_symbol} | {label}",
                        "log_index": log_index,
                        "csv_file": str(csv_path),
                    }
                    if usd_value is not None and values["transfer_value_USD"] is None:
                        values["transfer_value_label"] = "unknown"
                        values["display_label"] = f"{block} | {token_symbol} | unknown"
                    values["transfer_id"] = make_transfer_id(values)
                    yield Transfer(**values)
                except (ArithmeticError, ValueError, TypeError) as exc:
                    stats.malformed_rows_skipped += 1
                    logging.debug("Skipping malformed row in %s: %s", csv_path, exc)
    except (OSError, csv.Error) as exc:
        logging.warning("Could not parse CSV %s: %s", csv_path, exc)


def runtime_exceeded(started_at: float, limit_seconds: int) -> bool:
    return time.monotonic() - started_at >= limit_seconds


def trace_incoming_flow(
    args: argparse.Namespace,
    db_files: list[Path],
    token_metadata: dict[str, TokenMetadata],
    stats: TraceStats,
) -> tuple[dict[str, int], list[Transfer]]:
    queue: deque[tuple[str, int]] = deque([(args.root_address, 0)])
    queried_addresses: set[str] = set()
    node_depth = {args.root_address: 0}
    transfers_by_id: dict[str, Transfer] = {}
    resolver = CsvPathResolver(
        args.data_base_dir,
        args.start_block,
        args.outer_folder_size,
        args.inner_chunk_size,
    )
    started_at = time.monotonic()

    while queue:
        if runtime_exceeded(started_at, args.max_runtime_seconds):
            stats.stop_reason = "max-runtime-seconds"
            break
        current_address, depth = queue.popleft()
        if current_address in queried_addresses:
            continue
        if depth >= args.max_depth:
            continue
        queried_addresses.add(current_address)
        blocks, occurrence_count = query_address_occurrences(
            current_address, db_files, args.start_block, args.end_block, stats
        )
        if (
            args.skip_address_if_occurrence_over
            and occurrence_count > args.skip_address_if_occurrence_over
        ):
            stats.addresses_not_expanded_by_occurrence_limit += 1
            logging.info(
                "Not expanding %s: %d occurrence rows exceed limit %d",
                current_address,
                occurrence_count,
                args.skip_address_if_occurrence_over,
            )
            continue
        if len(blocks) > args.max_blocks_per_address:
            logging.info(
                "Limiting %s from %d candidate blocks to the first %d",
                current_address,
                len(blocks),
                args.max_blocks_per_address,
            )
            blocks = blocks[: args.max_blocks_per_address]

        for block in blocks:
            if runtime_exceeded(started_at, args.max_runtime_seconds):
                stats.stop_reason = "max-runtime-seconds"
                break
            for source_type in ("transaction", "token_transfer"):
                csv_path = resolver.resolve(source_type, block)
                if csv_path is None:
                    stats.missing_csv_files += 1
                    logging.debug("Missing %s CSV for block %s", source_type, block)
                    continue
                for transfer in iter_incoming_transfers(
                    csv_path, source_type, current_address, args, token_metadata, stats
                ):
                    if transfer.transfer_id in transfers_by_id:
                        continue
                    sender = transfer.from_address
                    if sender not in node_depth:
                        if len(node_depth) >= args.max_nodes:
                            if stats.stop_reason is None:
                                stats.stop_reason = "max-nodes"
                            continue
                        sender_depth = depth + 1
                        node_depth[sender] = sender_depth
                        if sender_depth < args.max_depth:
                            queue.append((sender, sender_depth))
                    transfers_by_id[transfer.transfer_id] = transfer
                    if len(transfers_by_id) >= args.max_edges:
                        stats.stop_reason = "max-edges"
                        break
                if stats.stop_reason == "max-edges":
                    break
            if stats.stop_reason in {"max-edges", "max-runtime-seconds"}:
                break
        if stats.stop_reason in {"max-edges", "max-runtime-seconds"}:
            break

    transfers = list(transfers_by_id.values())
    stats.nodes_collected = len(node_depth)
    stats.edges_collected = len(transfers)
    stats.unknown_value_transfers = sum(
        item.transfer_value_USD is None for item in transfers
    )
    return node_depth, transfers


def batched(items: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def create_schema(session: Any) -> None:
    statements = [
        (
            "composite Address uniqueness constraint",
            "CREATE CONSTRAINT address_graph_run_unique IF NOT EXISTS "
            "FOR (a:Address) REQUIRE (a.graph_run_id, a.address) IS UNIQUE",
        ),
        (
            "TRANSFERRED graph_run_id relationship index",
            "CREATE INDEX transfer_graph_run_id IF NOT EXISTS "
            "FOR ()-[r:TRANSFERRED]-() ON (r.graph_run_id)",
        ),
        (
            "TRANSFERRED block_number relationship index",
            "CREATE INDEX transfer_block_number IF NOT EXISTS "
            "FOR ()-[r:TRANSFERRED]-() ON (r.block_number)",
        ),
    ]
    for description, statement in statements:
        try:
            session.run(statement).consume()
        except (
            Exception
        ) as exc:  # Neo4j versions differ in composite/relationship index support.
            logging.warning("Could not create %s: %s", description, exc)


def load_into_neo4j(
    args: argparse.Namespace,
    node_depth: dict[str, int],
    transfers: list[Transfer],
    stats: TraceStats,
) -> None:
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise SystemExit(
            "The neo4j package is required. Run: pip install -r requirements.txt"
        ) from exc

    nodes = [
        {
            "address": address,
            "graph_run_id": args.graph_run_id,
            "is_root": address == args.root_address,
            "root_label": "ROOT" if address == args.root_address else "",
            "node_type": "root" if address == args.root_address else "address",
            "discovery_depth": depth,
            "short_label": (
                f"ROOT {shortened_address(address)}"
                if address == args.root_address
                else shortened_address(address)
            ),
        }
        for address, depth in sorted(
            node_depth.items(), key=lambda item: (item[1], item[0])
        )
    ]
    relationships = [asdict(item) for item in transfers]
    node_query = """
    UNWIND $rows AS row
    MERGE (a:Address {graph_run_id: row.graph_run_id, address: row.address})
    SET a.is_root = row.is_root,
        a.root_label = row.root_label,
        a.node_type = row.node_type,
        a.discovery_depth = row.discovery_depth,
        a.short_label = row.short_label
    """
    relationship_query = """
    UNWIND $rows AS row
    MATCH (a:Address {graph_run_id: row.graph_run_id, address: row.from_address})
    MATCH (b:Address {graph_run_id: row.graph_run_id, address: row.to_address})
    MERGE (a)-[r:TRANSFERRED {
        graph_run_id: row.graph_run_id,
        transfer_id: row.transfer_id
    }]->(b)
    SET r.source_type = row.source_type,
        r.block_number = row.block_number,
        r.transaction_hash = row.transaction_hash,
        r.token_address = row.token_address,
        r.token_symbol = row.token_symbol,
        r.raw_value = row.raw_value,
        r.decimal = row.decimal,
        r.median_exchange_rate_USD = row.median_exchange_rate_USD,
        r.transfer_value_USD = row.transfer_value_USD,
        r.transfer_value_label = row.transfer_value_label,
        r.display_label = row.display_label,
        r.log_index = row.log_index,
        r.csv_file = row.csv_file
    """

    with GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    ) as driver:
        driver.verify_connectivity()
        with driver.session(database=args.database) as session:
            create_schema(session)
            for batch in batched(nodes, args.batch_size):
                session.run(node_query, rows=batch).consume()
                stats.neo4j_nodes_written += len(batch)
            for batch in batched(relationships, args.batch_size):
                session.run(relationship_query, rows=batch).consume()
                stats.neo4j_relationships_written += len(batch)


def log_summary(
    args: argparse.Namespace,
    db_files: list[Path],
    metadata_count: int,
    stats: TraceStats,
) -> None:
    logging.info("Traversal summary")
    logging.info("  root address: %s", args.root_address)
    logging.info("  graph_run_id: %s", args.graph_run_id)
    logging.info("  SQLite files discovered: %d", len(db_files))
    logging.info("  token metadata rows loaded: %d", metadata_count)
    logging.info("  addresses queried: %d", stats.addresses_queried)
    logging.info("  SQLite occurrence rows found: %d", stats.occurrence_rows_found)
    logging.info("  block numbers found: %d", stats.block_numbers_found)
    logging.info("  block CSV files parsed: %d", stats.block_files_parsed)
    logging.info("  transaction rows considered: %d", stats.transaction_rows_considered)
    logging.info(
        "  token transfer rows considered: %d", stats.token_transfer_rows_considered
    )
    logging.info("  nodes collected: %d", stats.nodes_collected)
    logging.info("  edges collected: %d", stats.edges_collected)
    logging.info(
        "  unknown value transfers retained: %d", stats.unknown_value_transfers
    )
    logging.info(
        "  edges skipped by min USD value: %d", stats.edges_skipped_by_min_usd_value
    )
    logging.info(
        "  addresses not expanded by occurrence limit: %d",
        stats.addresses_not_expanded_by_occurrence_limit,
    )
    logging.info("  stop limit reached: %s", stats.stop_reason or "none")
    logging.info(
        "  Neo4j write summary: nodes=%d relationships=%d%s",
        stats.neo4j_nodes_written,
        stats.neo4j_relationships_written,
        " (dry-run; no writes attempted)" if args.dry_run else "",
    )
    logging.info(
        'Example Cypher: MATCH p=(:Address {graph_run_id: "%s"})-[:TRANSFERRED*1..%d]->'
        '(:Address {graph_run_id: "%s", is_root: true}) RETURN p',
        args.graph_run_id,
        args.max_depth,
        args.graph_run_id,
    )


def validate_input_paths(args: argparse.Namespace) -> None:
    if not args.data_base_dir.is_dir():
        raise SystemExit(f"Data base directory not found: {args.data_base_dir}")
    if not args.index_base_dir.is_dir():
        raise SystemExit(f"Index base directory not found: {args.index_base_dir}")


def main(argv: list[str] | None = None) -> int:
    configure_csv_field_limit()
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    validate_input_paths(args)
    logging.info("Root address: %s", args.root_address)
    logging.info("graph_run_id: %s", args.graph_run_id)
    db_files = discover_sqlite_files(
        args.index_base_dir, args.start_block, args.end_block
    )
    if not db_files:
        raise SystemExit(
            f"No address_block_index_*.sqlite files found under {args.index_base_dir}"
        )
    logging.info("Discovered %d SQLite index files", len(db_files))
    token_metadata = load_token_metadata(args.token_metadata_csv)
    logging.info("Loaded %d token metadata rows", len(token_metadata))
    stats = TraceStats()
    node_depth, transfers = trace_incoming_flow(args, db_files, token_metadata, stats)
    if args.dry_run:
        logging.info("Dry-run enabled; skipping Neo4j writes")
    else:
        load_into_neo4j(args, node_depth, transfers, stats)
    log_summary(args, db_files, len(token_metadata), stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
