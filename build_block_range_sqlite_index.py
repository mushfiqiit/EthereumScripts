#!/usr/bin/env python3
"""Index a single 7200-block Ethereum_TT_<start>_<end> CSV folder into one SQLite file.

Standalone script - does not import or depend on build_address_index.py or any
other file in this repository.

Input layout (produced by extract_newcsvs_transactions_tokentransfers.sh):

  <root>/Transaction_TokenTransfer_<chunk_start>_<chunk_end>/transaction_<block>.csv
  <root>/Transaction_TokenTransfer_<chunk_start>_<chunk_end>/token_transfer_<block>.csv

Output: one SQLite database containing:

  - an `edges` table where each row is a single transfer (ETH transaction or
    ERC-20/ERC-721 token transfer) with both endpoints, indexed on
    from_address and to_address so "who did this address send to / receive
    from" is a single indexed lookup instead of a CSV scan.
  - a `degree` table with precomputed out_count/in_count per address, so a
    BFS trace can recognize hub addresses (exchanges, routers, popular
    contracts) with one indexed lookup instead of counting their edges on
    the fly - see trace_address_flow_bfs.py, which uses this to cap
    per-address fan-out and avoid exploring through hubs unbounded.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CHUNK_FOLDER_RE = re.compile(r"^Transaction_TokenTransfer_(\d+)_(\d+)$")

INSERT_SQL = (
    "INSERT OR IGNORE INTO edges "
    "(edge_id, from_address, to_address, block_number, source, asset_type, "
    " token_address, raw_value, tx_hash, log_index) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def configure_csv_field_limit() -> int:
    """Raise the CSV field size limit to handle large calldata/input fields."""
    limit = sys.maxsize
    while True:
        try:
            return csv.field_size_limit(limit)
        except OverflowError:
            limit //= 10


def normalize_address(raw: object) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value or value in {"nan", "null", "none"}:
        return None
    if not value.startswith("0x") or len(value) < 4:
        return None
    return value


def parse_int(raw: object) -> Optional[int]:
    try:
        return int(str(raw).strip())
    except Exception:
        return None


def norm_key(name: str) -> str:
    return name.strip().lower()


def pick(row: dict[str, str], *names: str) -> str:
    lookup = {norm_key(k): v for k, v in row.items() if k is not None}
    for name in names:
        if name in lookup and lookup[name] is not None:
            return lookup[name]
    return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index one Ethereum_TT_<start>_<end> CSV folder into a single SQLite edge index.",
    )
    parser.add_argument(
        "--root",
        required=True,
        help="Path to the Ethereum_TT_<start>_<end> folder containing Transaction_TokenTransfer_* subfolders",
    )
    parser.add_argument("--db", required=True, help="Output SQLite file path")
    parser.add_argument("--batch-size", type=int, default=5000, help="Rows buffered per executemany batch")
    parser.add_argument("--commit-every", type=int, default=20000, help="Commit after this many inserted rows")
    return parser.parse_args()


def iter_chunk_folders(root: Path) -> list[tuple[Path, int, int]]:
    items: list[tuple[Path, int, int]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        m = CHUNK_FOLDER_RE.match(child.name)
        if not m:
            continue
        items.append((child, int(m.group(1)), int(m.group(2))))
    items.sort(key=lambda x: x[1])
    return items


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edges (
            edge_id       TEXT PRIMARY KEY,
            from_address  TEXT NOT NULL,
            to_address    TEXT NOT NULL,
            block_number  INTEGER NOT NULL,
            source        TEXT NOT NULL CHECK(source IN ('transaction', 'token_transfer')),
            asset_type    TEXT NOT NULL CHECK(asset_type IN ('ETH', 'TOKEN')),
            token_address TEXT,
            raw_value     TEXT NOT NULL,
            tx_hash       TEXT NOT NULL,
            log_index     INTEGER
        ) WITHOUT ROWID
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_address, block_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_address, block_number)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS degree (
            address   TEXT PRIMARY KEY,
            out_count INTEGER NOT NULL DEFAULT 0,
            in_count  INTEGER NOT NULL DEFAULT 0
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )


def rebuild_degree_table(conn: sqlite3.Connection) -> int:
    """Recompute per-address out/in edge counts for this shard.

    Lets a tracer recognize hub addresses (exchanges, routers, popular
    contracts) with a single indexed lookup instead of counting their edges
    on the fly - counting a hub's rows just to decide whether to cap it
    would defeat the point of capping it.
    """
    conn.execute("DELETE FROM degree")
    conn.execute(
        """
        INSERT INTO degree(address, out_count, in_count)
        SELECT address,
               SUM(CASE WHEN dir = 'out' THEN cnt ELSE 0 END),
               SUM(CASE WHEN dir = 'in' THEN cnt ELSE 0 END)
        FROM (
            SELECT from_address AS address, 'out' AS dir, COUNT(*) AS cnt FROM edges GROUP BY from_address
            UNION ALL
            SELECT to_address AS address, 'in' AS dir, COUNT(*) AS cnt FROM edges GROUP BY to_address
        )
        GROUP BY address
        """
    )
    return conn.execute("SELECT COUNT(*) FROM degree").fetchone()[0]


def upsert_metadata(conn: sqlite3.Connection, data: dict[str, str]) -> None:
    conn.executemany(
        "INSERT INTO metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        list(data.items()),
    )


class BatchWriter:
    """Buffers edge rows and flushes them with executemany, committing periodically."""

    def __init__(self, conn: sqlite3.Connection, batch_size: int, commit_every: int) -> None:
        self.conn = conn
        self.batch_size = batch_size
        self.commit_every = commit_every
        self._buffer: list[tuple] = []
        self._since_commit = 0
        self.inserted_total = 0

    def add(self, row: tuple) -> None:
        self._buffer.append(row)
        if len(self._buffer) >= self.batch_size:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        before = self.conn.total_changes
        self.conn.executemany(INSERT_SQL, self._buffer)
        inserted = self.conn.total_changes - before
        self.inserted_total += inserted
        self._since_commit += inserted
        self._buffer.clear()
        if self._since_commit >= self.commit_every:
            self.conn.commit()
            self._since_commit = 0

    def close(self) -> None:
        self._flush()
        if self._since_commit > 0:
            self.conn.commit()
            self._since_commit = 0


def process_transaction_csv(path: Path, writer: BatchWriter) -> tuple[int, int]:
    rows_read = 0
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return 0, 0
        try:
            for row in reader:
                rows_read += 1
                from_addr = normalize_address(pick(row, "from_address", "from"))
                to_addr = normalize_address(pick(row, "to_address", "to"))
                block = parse_int(pick(row, "block_number", "blocknumber", "block"))
                tx_hash = pick(row, "hash", "transaction_hash")
                if from_addr is None or to_addr is None or block is None or not tx_hash:
                    continue
                raw_value = pick(row, "value") or "0"
                edge_id = f"tx:{tx_hash.lower()}"
                writer.add((edge_id, from_addr, to_addr, block, "transaction", "ETH", None, raw_value, tx_hash, None))
        except csv.Error as exc:
            print(f"  [WARN] CSV parse error in {path}: {exc}. Skipping remaining rows in this file.")
    return rows_read, 0


def process_token_transfer_csv(path: Path, writer: BatchWriter) -> tuple[int, int]:
    rows_read = 0
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return 0, 0
        try:
            for row in reader:
                rows_read += 1
                from_addr = normalize_address(pick(row, "from_address", "from"))
                to_addr = normalize_address(pick(row, "to_address", "to"))
                block = parse_int(pick(row, "block_number", "blocknumber", "block"))
                token = normalize_address(pick(row, "token_address"))
                tx_hash = pick(row, "transaction_hash", "hash")
                log_index = parse_int(pick(row, "log_index"))
                if (
                    from_addr is None
                    or to_addr is None
                    or block is None
                    or token is None
                    or not tx_hash
                    or log_index is None
                ):
                    continue
                raw_value = pick(row, "value") or "0"
                edge_id = f"tt:{tx_hash.lower()}:{log_index}"
                writer.add(
                    (edge_id, from_addr, to_addr, block, "token_transfer", "TOKEN", token, raw_value, tx_hash, log_index)
                )
        except csv.Error as exc:
            print(f"  [WARN] CSV parse error in {path}: {exc}. Skipping remaining rows in this file.")
    return rows_read, 0


def main() -> int:
    configured_limit = configure_csv_field_limit()
    print(f"Configured csv.field_size_limit={configured_limit}")

    args = parse_args()
    root = Path(args.root)
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Root folder not found or not a directory: {root}")

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    chunk_folders = iter_chunk_folders(root)
    if not chunk_folders:
        raise SystemExit(f"No Transaction_TokenTransfer_* folders found under: {root}")

    print(f"Found {len(chunk_folders)} chunk folders under {root}")
    print(f"Writing SQLite edge index to: {db_path}")

    started = time.monotonic()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        init_db(conn)
        conn.commit()

        writer = BatchWriter(conn, args.batch_size, args.commit_every)
        total_tx_rows = 0
        total_tt_rows = 0
        total_tx_files = 0
        total_tt_files = 0

        for folder, chunk_start, chunk_end in chunk_folders:
            for tx_file in sorted(folder.glob("transaction_*.csv")):
                total_tx_files += 1
                rows_read, _ = process_transaction_csv(tx_file, writer)
                total_tx_rows += rows_read
            for tt_file in sorted(folder.glob("token_transfer_*.csv")):
                total_tt_files += 1
                rows_read, _ = process_token_transfer_csv(tt_file, writer)
                total_tt_rows += rows_read

        writer.close()

        print("Computing per-address degree table...")
        degree_rows = rebuild_degree_table(conn)
        conn.commit()

        range_start = min(s for _, s, _ in chunk_folders)
        range_end = max(e for _, _, e in chunk_folders)
        created_at = datetime.now(timezone.utc).isoformat()
        upsert_metadata(
            conn,
            {
                "range_start": str(range_start),
                "range_end": str(range_end),
                "source_root": str(root.resolve()),
                "created_at": created_at,
                "transaction_files": str(total_tx_files),
                "token_transfer_files": str(total_tt_files),
                "transaction_rows_read": str(total_tx_rows),
                "token_transfer_rows_read": str(total_tt_rows),
                "edges_inserted": str(writer.inserted_total),
                "degree_rows": str(degree_rows),
            },
        )
        conn.commit()

        elapsed = time.monotonic() - started
        print(
            f"\nDone in {elapsed:.1f}s: tx_files={total_tx_files}, tt_files={total_tt_files}, "
            f"tx_rows_read={total_tx_rows}, tt_rows_read={total_tt_rows}, "
            f"edges_inserted={writer.inserted_total}, degree_rows={degree_rows}"
        )
        print(f"Range: {range_start}-{range_end}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
