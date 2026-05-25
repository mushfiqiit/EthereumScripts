#!/usr/bin/env python3
"""Query SQLite Ethereum address occurrence indexes.

B+ tree-like idea:
- SQLite index idx_occurrences_address_block is a B-tree over (address, block_number).
- Lookup by address is logarithmic in index size, then rows are scanned in block order.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path
from typing import Optional

DB_RE = re.compile(r"^address_block_index_(\d+)_(\d+)\.sqlite$")


def normalize_address(raw: str) -> Optional[str]:
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value or value in {"nan", "null", "none"}:
        return None
    if not value.startswith("0x"):
        return None
    if len(value) < 4:
        return None
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query one or many SQLite address indexes")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--db", help="Single SQLite index file")
    group.add_argument("--index-dir", help="Directory containing address_block_index_*.sqlite")
    parser.add_argument("--address", required=True, help="Ethereum address to search")
    return parser.parse_args()


def query_one(db_path: Path, address: str) -> list[tuple[int, str, str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT block_number, role, source
            FROM occurrences
            WHERE address = ?
            ORDER BY block_number ASC
            """,
            (address,),
        )
        return [(row[0], row[1], row[2], db_path.name) for row in cur.fetchall()]
    finally:
        conn.close()


def gather_dbs(index_dir: Path) -> list[Path]:
    files = [p for p in index_dir.glob("address_block_index_*.sqlite") if p.is_file()]

    def sort_key(p: Path) -> tuple[int, int]:
        m = DB_RE.match(p.name)
        if not m:
            return (10**18, 10**18)
        return (int(m.group(1)), int(m.group(2)))

    files.sort(key=sort_key)
    return files


def main() -> int:
    args = parse_args()
    address = normalize_address(args.address)
    if address is None:
        raise SystemExit("Invalid address: must be non-empty and start with 0x")

    if args.db:
        db_files = [Path(args.db)]
    else:
        idx_dir = Path(args.index_dir)
        if not idx_dir.exists() or not idx_dir.is_dir():
            raise SystemExit(f"Index directory not found: {idx_dir}")
        db_files = gather_dbs(idx_dir)
        if not db_files:
            raise SystemExit(f"No address_block_index_*.sqlite files found in {idx_dir}")

    all_rows: list[tuple[int, str, str, str]] = []
    for db in db_files:
        if not db.exists() or not db.is_file():
            print(f"[WARN] Skipping missing DB: {db}")
            continue
        try:
            rows = query_one(db, address)
            all_rows.extend(rows)
        except sqlite3.Error as exc:
            print(f"[WARN] Failed querying {db}: {exc}")

    all_rows.sort(key=lambda x: (x[0], x[3], x[1], x[2]))

    if args.db:
        print(f"Address: {address}")
        print(f"Database: {Path(args.db).name}\n")
        print("block_number, role, source")
        for block, role, source, _db in all_rows:
            print(f"{block}, {role}, {source}")
    else:
        print(f"Address: {address}")
        print(f"Index directory: {args.index_dir}\n")
        print("block_number, role, source, database")
        for block, role, source, db_name in all_rows:
            print(f"{block}, {role}, {source}, {db_name}")

    if not all_rows:
        print("(no matches found)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
