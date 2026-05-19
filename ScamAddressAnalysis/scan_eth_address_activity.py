#!/usr/bin/env python3
"""Export Ethereum block data and collect rows related to known ETH addresses.

For each block in a range, this script runs ethereumetl to export transactions and
token transfers, searches both CSVs for addresses from eth_addresses_deduplicated.csv,
and appends every row with a matched transaction hash to an address-specific CSV.
Temporary per-block ethereumetl CSVs are deleted before the script moves to the next
block.
"""

from __future__ import annotations

import argparse
import csv
import sys
csv.field_size_limit(sys.maxsize)
import json
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable


DEFAULT_ADDRESS_FILE = "eth_addresses_deduplicated.csv"
DEFAULT_OUTPUT_DIR = "matched_eth_address_activity"
DEFAULT_RPC_URL = "http://10.112.249.200:8545"
DEFAULT_START_BLOCK = 25107100
DEFAULT_END_BLOCK = 25121500
ADDRESS_COLUMN = "Address"
TRANSACTION_HASH_FIELDS = ("hash", "transaction_hash", "transaction_id")


AddressMap = dict[str, str]
TransactionMatches = dict[str, set[str]]


def normalize_address(address: str) -> str:
    """Normalize an Ethereum address for case-insensitive matching."""
    return address.strip().lower()


def safe_address_filename(address: str) -> str:
    """Return the output filename for an address-specific result CSV."""
    safe_address = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in address.strip()
    )
    return f"output_{safe_address}.csv"


def load_addresses(address_file: Path) -> AddressMap:
    """Load addresses from eth_addresses_deduplicated.csv."""
    addresses: AddressMap = {}

    with address_file.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            raise ValueError(f"Address CSV is empty: {address_file}")
        if ADDRESS_COLUMN not in reader.fieldnames:
            raise ValueError(f"Address CSV must contain an '{ADDRESS_COLUMN}' column")

        for row in reader:
            original_address = row.get(ADDRESS_COLUMN, "").strip()
            normalized = normalize_address(original_address)
            if normalized:
                addresses.setdefault(normalized, original_address)

    if not addresses:
        raise ValueError(f"No addresses found in {address_file}")

    return addresses


def row_contains_address(row: dict[str, str], addresses: AddressMap) -> set[str]:
    """Return normalized target addresses found anywhere in a CSV row."""
    normalized_values = {normalize_address(value) for value in row.values() if value}
    return normalized_values.intersection(addresses)


def transaction_hash_from_row(row: dict[str, str]) -> str | None:
    """Read the transaction hash from a transaction or token-transfer CSV row."""
    for field in TRANSACTION_HASH_FIELDS:
        value = row.get(field, "").strip()
        if value:
            return value
    return None


def find_matching_transaction_hashes(
    csv_paths: Iterable[Path], addresses: AddressMap
) -> TransactionMatches:
    """Find transaction hashes from rows containing one or more target addresses."""
    matches: TransactionMatches = defaultdict(set)

    for csv_path in csv_paths:
        if not csv_path.exists():
            continue

        with csv_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
            reader = csv.DictReader(csv_file)
            if reader.fieldnames is None:
                continue

            for row in reader:
                matched_addresses = row_contains_address(row, addresses)
                if not matched_addresses:
                    continue

                transaction_hash = transaction_hash_from_row(row)
                if transaction_hash:
                    matches[transaction_hash].update(matched_addresses)

    return matches


def matching_rows_for_hashes(
    csv_path: Path, transaction_hashes: set[str]
) -> Iterable[tuple[str, dict[str, str]]]:
    """Yield rows whose transaction hash is in transaction_hashes."""
    if not csv_path.exists():
        return

    with csv_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            return

        for row in reader:
            transaction_hash = transaction_hash_from_row(row)
            if transaction_hash in transaction_hashes:
                yield transaction_hash, row


def append_address_rows(
    output_dir: Path,
    addresses: AddressMap,
    transaction_matches: TransactionMatches,
    source_name: str,
    source_csv_path: Path,
    block_number: int,
) -> int:
    """Append rows matching transaction hashes to address-specific output CSVs."""
    if not transaction_matches:
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    transaction_hashes = set(transaction_matches)
    rows_written = 0

    for transaction_hash, row in matching_rows_for_hashes(source_csv_path, transaction_hashes):
        for normalized_address in sorted(transaction_matches[transaction_hash]):
            original_address = addresses[normalized_address]
            output_path = output_dir / safe_address_filename(original_address)
            write_header = not output_path.exists() or output_path.stat().st_size == 0

            with output_path.open("a", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(
                    csv_file,
                    fieldnames=[
                        "matched_address",
                        "block_number",
                        "source_csv",
                        "transaction_hash",
                        "row_data_json",
                    ],
                )
                if write_header:
                    writer.writeheader()
                writer.writerow(
                    {
                        "matched_address": original_address,
                        "block_number": block_number,
                        "source_csv": source_name,
                        "transaction_hash": transaction_hash,
                        "row_data_json": json.dumps(row, sort_keys=True),
                    }
                )
                rows_written += 1

    return rows_written


def run_command(command: list[str], dry_run: bool) -> None:
    """Run an ethereumetl command, or print it when dry-run mode is enabled."""
    printable_command = " ".join(command)
    if dry_run:
        print(f"DRY RUN: {printable_command}")
        return

    print(printable_command)
    subprocess.run(command, check=True)


def export_block_csvs(
    block_number: int,
    provider_uri: str,
    batch_size: int,
    max_workers: int,
    temp_dir: Path,
    dry_run: bool,
) -> tuple[Path, Path, Path]:
    """Export block, transaction, and token-transfer CSVs for one block."""
    blocks_csv = temp_dir / f"blocks_{block_number}.csv"
    transactions_csv = temp_dir / f"transactions_{block_number}.csv"
    token_transfers_csv = temp_dir / f"token_transfers_{block_number}.csv"

    run_command(
        [
            "ethereumetl",
            "export_blocks_and_transactions",
            "--start-block",
            str(block_number),
            "--end-block",
            str(block_number),
            "--provider-uri",
            provider_uri,
            "--batch-size",
            str(batch_size),
            "--max-workers",
            str(max_workers),
            "--blocks-output",
            str(blocks_csv),
            "--transactions-output",
            str(transactions_csv),
        ],
        dry_run=dry_run,
    )

    run_command(
        [
            "ethereumetl",
            "export_token_transfers",
            "--start-block",
            str(block_number),
            "--end-block",
            str(block_number),
            "--provider-uri",
            provider_uri,
            "--batch-size",
            str(batch_size),
            "--max-workers",
            str(max_workers),
            "--output",
            str(token_transfers_csv),
        ],
        dry_run=dry_run,
    )

    return blocks_csv, transactions_csv, token_transfers_csv


def process_block(
    block_number: int,
    addresses: AddressMap,
    output_dir: Path,
    provider_uri: str,
    batch_size: int,
    max_workers: int,
    dry_run: bool,
) -> None:
    """Export one block, collect matching rows, and delete temporary CSVs."""
    with tempfile.TemporaryDirectory(prefix=f"eth_block_{block_number}_") as temp_name:
        temp_dir = Path(temp_name)
        blocks_csv, transactions_csv, token_transfers_csv = export_block_csvs(
            block_number=block_number,
            provider_uri=provider_uri,
            batch_size=batch_size,
            max_workers=max_workers,
            temp_dir=temp_dir,
            dry_run=dry_run,
        )

        if dry_run:
            return

        transaction_matches = find_matching_transaction_hashes(
            [transactions_csv, token_transfers_csv], addresses
        )
        transaction_count = len(transaction_matches)
        rows_written = 0
        rows_written += append_address_rows(
            output_dir=output_dir,
            addresses=addresses,
            transaction_matches=transaction_matches,
            source_name="transactions",
            source_csv_path=transactions_csv,
            block_number=block_number,
        )
        rows_written += append_address_rows(
            output_dir=output_dir,
            addresses=addresses,
            transaction_matches=transaction_matches,
            source_name="token_transfers",
            source_csv_path=token_transfers_csv,
            block_number=block_number,
        )

        for temporary_csv in (blocks_csv, transactions_csv, token_transfers_csv):
            temporary_csv.unlink(missing_ok=True)

    print(
        f"Block {block_number}: matched {transaction_count} transaction hashes; "
        f"wrote {rows_written} rows"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export each block's transactions and token transfers, search for "
            "addresses from eth_addresses_deduplicated.csv, and write matching "
            "transaction/token-transfer rows to output_ADDRESS.csv files."
        )
    )
    parser.add_argument("--address-file", default=DEFAULT_ADDRESS_FILE)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--provider-uri", default=DEFAULT_RPC_URL)
    parser.add_argument("--start-block", type=int, default=DEFAULT_START_BLOCK)
    parser.add_argument("--end-block", type=int, default=DEFAULT_END_BLOCK)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-workers", type=int, default=5)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print ethereumetl commands without running exports or writing outputs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    address_file = Path(args.address_file)
    output_dir = Path(args.output_dir)

    if not address_file.is_file():
        raise FileNotFoundError(f"Address CSV not found: {address_file}")
    if args.start_block > args.end_block:
        raise ValueError("--start-block must be less than or equal to --end-block")
    if args.batch_size <= 0 or args.max_workers <= 0:
        raise ValueError("--batch-size and --max-workers must be positive integers")
    if not args.dry_run and shutil.which("ethereumetl") is None:
        raise RuntimeError("ethereumetl command not found in PATH")

    addresses = load_addresses(address_file)
    print(f"Loaded {len(addresses)} addresses from {address_file}")

    for block_number in range(args.start_block, args.end_block + 1):
        process_block(
            block_number=block_number,
            addresses=addresses,
            output_dir=output_dir,
            provider_uri=args.provider_uri,
            batch_size=args.batch_size,
            max_workers=args.max_workers,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
