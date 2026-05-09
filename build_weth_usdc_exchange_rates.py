#!/usr/bin/env python3
"""Generate per-transaction USDC-per-WETH exchange-rate CSVs from chunked transfer exports.

For each input CSV in a source directory (e.g. weth_usdc_transfer_chunks), this script:
  1) Filters to transactions where address x sent USDC to address y AND
     address y sent WETH to address x in the same transaction hash.
  2) Computes exchange_rate = normalized_usdc_amount / normalized_weth_amount.
  3) Writes a corresponding output CSV with columns:
       block_number,from_address,to_address,transaction_id,exchange_rate

The output row uses:
  - from_address = x (USDC sender)
  - to_address   = y (USDC receiver)
  - transaction_id = transaction hash
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

getcontext().prec = 50

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC_DIVISOR = Decimal("1e6")
WETH_DIVISOR = Decimal("1e18")

REQUIRED_COLUMNS = {
    "token_address",
    "from_address",
    "to_address",
    "value",
    "transaction_hash",
    "block_number",
}

OUTPUT_FIELDS = [
    "block_number",
    "from_address",
    "to_address",
    "transaction_id",
    "exchange_rate",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build per-file exchange-rate CSVs from WETH/USDC transfer chunks "
            "exported by ethereumetl."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("weth_usdc_transfer_chunks"),
        help="Directory containing source transfer CSV files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("weth_usdc_exchange_rates"),
        help="Directory where transformed CSV files will be written.",
    )
    parser.add_argument(
        "--pattern",
        default="*.csv",
        help="Glob pattern used to select input files within --input-dir.",
    )
    return parser.parse_args()


def normalize_address(value: str) -> str:
    return (value or "").strip().lower()


def parse_decimal(raw: str, *, tx: str) -> Decimal:
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValueError(f"Invalid token value in transaction {tx}: {raw!r}") from exc


def decimal_to_string(value: Decimal) -> str:
    return format(value.normalize(), "f")


def ensure_columns(path: Path, fieldnames: Iterable[str] | None) -> None:
    if fieldnames is None:
        raise ValueError(f"{path} has no header row")

    missing = REQUIRED_COLUMNS.difference(fieldnames)
    if missing:
        missing_sorted = ", ".join(sorted(missing))
        raise ValueError(f"{path} missing required columns: {missing_sorted}")


def process_file(input_csv: Path, output_csv: Path) -> int:
    # tx -> ((x, y) for USDC x->y) => [usdc_raw_sum, weth_raw_sum, block_number]
    grouped: Dict[str, Dict[Tuple[str, str], List[Decimal | str | None]]] = defaultdict(dict)

    with input_csv.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        ensure_columns(input_csv, reader.fieldnames)

        for row in reader:
            token = normalize_address(row.get("token_address", ""))
            if token not in (USDC, WETH):
                continue

            tx = normalize_address(row.get("transaction_hash", ""))
            from_address = normalize_address(row.get("from_address", ""))
            to_address = normalize_address(row.get("to_address", ""))
            block_number = (row.get("block_number") or "").strip()

            if not tx or not from_address or not to_address:
                continue

            amount_raw = parse_decimal(row.get("value", ""), tx=tx)

            # For matching we store by (x,y) = direction of USDC x->y.
            if token == USDC:
                key = (from_address, to_address)
                usdc_raw, weth_raw, block_ref = grouped[tx].get(
                    key, [Decimal(0), Decimal(0), None]
                )
                grouped[tx][key] = [
                    Decimal(usdc_raw) + amount_raw,
                    Decimal(weth_raw),
                    block_ref or block_number,
                ]
            else:  # WETH row y->x maps to key (x,y)
                key = (to_address, from_address)
                usdc_raw, weth_raw, block_ref = grouped[tx].get(
                    key, [Decimal(0), Decimal(0), None]
                )
                grouped[tx][key] = [
                    Decimal(usdc_raw),
                    Decimal(weth_raw) + amount_raw,
                    block_ref or block_number,
                ]

    written = 0
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    with output_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()

        for tx, pairs in grouped.items():
            for (from_address, to_address), values in pairs.items():
                usdc_raw = Decimal(values[0])
                weth_raw = Decimal(values[1])
                block_number = str(values[2] or "")

                # Keep only tx/address pairs matching:
                # x -> y USDC and y -> x WETH.
                if usdc_raw <= 0 or weth_raw <= 0:
                    continue

                usdc_amount = usdc_raw / USDC_DIVISOR
                weth_amount = weth_raw / WETH_DIVISOR
                if usdc_amount == 0:
                    continue

                exchange_rate = usdc_amount / weth_amount

                writer.writerow(
                    {
                        "block_number": block_number,
                        "from_address": from_address,
                        "to_address": to_address,
                        "transaction_id": tx,
                        "exchange_rate": decimal_to_string(exchange_rate),
                    }
                )
                written += 1

    return written


def main() -> None:
    args = parse_args()

    if not args.input_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {args.input_dir}")

    input_files = sorted(
        path for path in args.input_dir.glob(args.pattern) if path.is_file()
    )
    if not input_files:
        raise FileNotFoundError(
            f"No input CSV files found in {args.input_dir} matching {args.pattern!r}"
        )

    total_rows = 0
    for input_csv in input_files:
        output_csv = args.output_dir / input_csv.name
        rows = process_file(input_csv, output_csv)
        total_rows += rows
        print(f"{input_csv} -> {output_csv} (rows: {rows})")

    print(f"Done. Processed {len(input_files)} files. Total output rows: {total_rows}")


if __name__ == "__main__":
    main()