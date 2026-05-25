#!/usr/bin/env python3
"""Build per-range SQLite address indexes from ethereumetl CSV exports.

B+ tree-like idea:
- We use SQLite as a disk-backed index engine.
- SQLite stores `occurrences` rows and a B-tree index on (address, block_number).
- Conceptually, each address key maps to an ordered posting list of
  (block_number, role, source).
- Query complexity is approximately O(log n + k), where n is indexed rows
  and k is matched rows for one address.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

FOLDER_RE = re.compile(r"^Transaction_TokenTransfer_(\d+)_(\d+)$")
DB_RE = re.compile(r"^address_block_index_(\d+)_(\d+)\.sqlite$")

BLOCK_KEYS = ("block_number", "blocknumber", "block")
FROM_KEYS = ("from_address", "from")
TO_KEYS = ("to_address", "to")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build SQLite address indexes from Transaction_TokenTransfer folders."
    )
    parser.add_argument("--root", required=True, help="Root folder, e.g. Ethereum_TT_25112101_25119300")
    parser.add_argument("--output", required=True, help="Output directory for .sqlite index files")
    parser.add_argument("--batch-size", type=int, default=5000, help="Insert batch size for executemany")
    parser.add_argument("--commit-every", type=int, default=20000, help="Commit every N inserted rows")
    parser.add_argument("--expected-folders", type=int, default=72, help="Expected number of Transaction_TokenTransfer_* folders (default: 72)")
    return parser.parse_args()


def norm_col(name: str) -> str:
    return name.strip().lower()


def normalize_address(raw: object) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value or value in {"nan", "null", "none"}:
        return None
    if not value.startswith("0x"):
        return None
    # Keep flexible but reject clearly invalid short values.
    if len(value) < 4:
        return None
    return value


def parse_block_number(raw: object) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except Exception:
        return None


def detect_columns(fieldnames: Iterable[str]) -> tuple[str, str, str]:
    mapping = {norm_col(c): c for c in fieldnames if c is not None}

    def pick(candidates: tuple[str, ...]) -> str:
        for c in candidates:
            if c in mapping:
                return mapping[c]
        raise ValueError(f"Missing required columns. expected one of: {candidates}")

    return pick(BLOCK_KEYS), pick(FROM_KEYS), pick(TO_KEYS)


def iter_subfolders(root: Path) -> list[tuple[Path, int, int]]:
    items: list[tuple[Path, int, int]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        m = FOLDER_RE.match(child.name)
        if not m:
            continue
        start, end = int(m.group(1)), int(m.group(2))
        items.append((child, start, end))
    items.sort(key=lambda x: x[1])
    return items


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS occurrences (
            address TEXT NOT NULL,
            block_number INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('from', 'to')),
            source TEXT NOT NULL CHECK(source IN ('transaction', 'token_transfer')),
            PRIMARY KEY (address, block_number, role, source)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_occurrences_address_block
        ON occurrences(address, block_number)
        """
    )


def upsert_metadata(conn: sqlite3.Connection, data: dict[str, str]) -> None:
    conn.executemany(
        "INSERT INTO metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        list(data.items()),
    )


def process_csv_file(
    csv_path: Path,
    source: str,
    conn: sqlite3.Connection,
    batch_size: int,
    commit_every: int,
) -> tuple[int, int]:
    inserted_total = 0
    buffered: list[tuple[str, int, str, str]] = []
    committed_since_last = 0

    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print(f"  [WARN] Empty/no-header CSV skipped: {csv_path}")
            return 0, 0

        try:
            block_col, from_col, to_col = detect_columns(reader.fieldnames)
        except ValueError as exc:
            print(f"  [WARN] {csv_path.name}: {exc}")
            return 0, 0

        for row in reader:
            block = parse_block_number(row.get(block_col))
            if block is None:
                continue

            from_addr = normalize_address(row.get(from_col))
            to_addr = normalize_address(row.get(to_col))

            if from_addr is not None:
                buffered.append((from_addr, block, "from", source))
            if to_addr is not None:
                buffered.append((to_addr, block, "to", source))

            if len(buffered) >= batch_size:
                before = conn.total_changes
                conn.executemany(
                    "INSERT OR IGNORE INTO occurrences(address, block_number, role, source) VALUES (?, ?, ?, ?)",
                    buffered,
                )
                inserted = conn.total_changes - before
                inserted_total += inserted
                committed_since_last += inserted
                buffered.clear()
                if committed_since_last >= commit_every:
                    conn.commit()
                    committed_since_last = 0

    if buffered:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO occurrences(address, block_number, role, source) VALUES (?, ?, ?, ?)",
            buffered,
        )
        inserted = conn.total_changes - before
        inserted_total += inserted
        committed_since_last += inserted

    if committed_since_last > 0:
        conn.commit()

    return inserted_total, 0


def main() -> int:
    args = parse_args()
    root = Path(args.root)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Root folder not found or not a directory: {root}")

    subfolders = iter_subfolders(root)
    if not subfolders:
        raise SystemExit(f"No matching subfolders found under: {root}")

    print(f"Found {len(subfolders)} range folders under {root}")
    if args.expected_folders > 0 and len(subfolders) != args.expected_folders:
        print(f"[WARN] Expected {args.expected_folders} folders but found {len(subfolders)}")

    for folder, start, end in subfolders:
        db_path = out_dir / f"address_block_index_{start}_{end}.sqlite"
        print(f"\n[Indexing] {folder.name} -> {db_path.name}")

        tx_files = sorted(folder.glob("transaction*.csv"))
        tt_files = sorted(folder.glob("token_transfer*.csv"))

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            init_db(conn)
            conn.commit()

            total_inserted = 0

            for p in tx_files:
                inserted, _ = process_csv_file(
                    p, "transaction", conn, args.batch_size, args.commit_every
                )
                total_inserted += inserted

            for p in tt_files:
                inserted, _ = process_csv_file(
                    p, "token_transfer", conn, args.batch_size, args.commit_every
                )
                total_inserted += inserted

            created_at = datetime.now(timezone.utc).isoformat()
            upsert_metadata(
                conn,
                {
                    "range_start": str(start),
                    "range_end": str(end),
                    "source_folder": str(folder.resolve()),
                    "created_at": created_at,
                    "number_of_transaction_files": str(len(tx_files)),
                    "number_of_token_transfer_files": str(len(tt_files)),
                    "number_of_inserted_occurrences": str(total_inserted),
                },
            )
            conn.commit()
            print(
                f"  Done: tx_files={len(tx_files)}, tt_files={len(tt_files)}, "
                f"inserted_occurrences={total_inserted}"
            )
        finally:
            conn.close()

    print(f"\nAll indexes built successfully. Created/updated {len(subfolders)} SQLite files in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
