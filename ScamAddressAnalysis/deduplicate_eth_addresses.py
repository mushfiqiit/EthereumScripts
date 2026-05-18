#!/usr/bin/env python3
"""Remove duplicate addresses from eth_addresses.csv.

The script reads a CSV with at least an Address column, keeps the first row seen
for each address, and writes a new CSV where each address appears exactly once.
Address matching is case-insensitive so checksum-cased Ethereum addresses are
still treated as duplicates of their lowercase versions.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


DEFAULT_INPUT = "eth_addresses.csv"
DEFAULT_OUTPUT = "eth_addresses_deduplicated.csv"
ADDRESS_COLUMN = "Address"


def normalized_address(address: str) -> str:
    """Return the value used to detect duplicate Ethereum addresses."""
    return address.strip().lower()


def deduplicate_rows(input_path: Path) -> tuple[list[str], list[dict[str, str]], int]:
    """Read CSV rows and keep only the first row for each unique address."""
    seen_addresses: set[str] = set()
    unique_rows: list[dict[str, str]] = []
    total_rows = 0

    with input_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            raise ValueError(f"Input CSV is empty: {input_path}")
        if ADDRESS_COLUMN not in reader.fieldnames:
            raise ValueError(f"Input CSV must contain an '{ADDRESS_COLUMN}' column")

        for row in reader:
            total_rows += 1
            address = normalized_address(row.get(ADDRESS_COLUMN, ""))
            if not address or address in seen_addresses:
                continue

            seen_addresses.add(address)
            unique_rows.append(row)

    return reader.fieldnames, unique_rows, total_rows


def write_rows(output_path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    """Write deduplicated rows to a CSV file."""
    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read eth_addresses.csv and write a new CSV where each Address "
            "appears exactly once."
        )
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        default=DEFAULT_INPUT,
        help=f"Input CSV file path (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "output_file",
        nargs="?",
        default=DEFAULT_OUTPUT,
        help=f"Output CSV file path (default: {DEFAULT_OUTPUT})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input_file)
    output_path = Path(args.output_file)

    if not input_path.is_file():
        raise FileNotFoundError(f"Input CSV file not found: {input_path}")

    fieldnames, unique_rows, total_rows = deduplicate_rows(input_path)
    write_rows(output_path, fieldnames, unique_rows)

    duplicates_removed = total_rows - len(unique_rows)
    print(
        f"Wrote {len(unique_rows)} unique rows to {output_path} "
        f"({duplicates_removed} duplicate rows removed)"
    )


if __name__ == "__main__":
    main()
