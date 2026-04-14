#!/usr/bin/env python3
"""Filter ERC-20 transfer CSV data for one-to-one WETH/USDC exchanges.

Input CSV columns:
    token_address,from_address,to_address,value,transaction_hash,log_index,block_number

The script identifies transfer pairs in the same transaction hash where:
  - WETH moves from address A to address B
  - USDC moves from address B to address A

For every matched pair, it writes both rows to an output CSV and appends:
  - normalized_value: raw value divided by token decimals
  - weth_to_usdc_rate: USDC amount per 1 WETH for that matched pair
Pairs are only kept when the computed WETH->USDC rate is within [2268, 2468].
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Dict, List, Tuple

getcontext().prec = 50

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
DECIMALS = {
    USDC: Decimal("1e6"),
    WETH: Decimal("1e18"),
}
TARGET_WETH_TO_USDC_RATE = Decimal("2368")
RATE_TOLERANCE = Decimal("100")
MIN_ALLOWED_RATE = TARGET_WETH_TO_USDC_RATE - RATE_TOLERANCE
MAX_ALLOWED_RATE = TARGET_WETH_TO_USDC_RATE + RATE_TOLERANCE

REQUIRED_COLUMNS = [
    "token_address",
    "from_address",
    "to_address",
    "value",
    "transaction_hash",
    "log_index",
    "block_number",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract one-to-one WETH/USDC exchange transfers from a CSV file."
    )
    parser.add_argument("input_csv", type=Path, help="Path to source CSV")
    parser.add_argument("output_csv", type=Path, help="Path to write filtered CSV")
    return parser.parse_args()


def normalize_address(value: str) -> str:
    return (value or "").strip().lower()


def parse_raw_value(row: Dict[str, str]) -> Decimal:
    try:
        return Decimal(str(row["value"]).strip())
    except (InvalidOperation, KeyError) as exc:
        tx = row.get("transaction_hash", "<missing tx>")
        log_index = row.get("log_index", "<missing log_index>")
        raise ValueError(f"Invalid value at tx={tx}, log_index={log_index}") from exc


def normalize_value(token_address: str, raw_value: Decimal) -> Decimal:
    return raw_value / DECIMALS[token_address]


def load_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV appears to have no header row")

        missing = [col for col in REQUIRED_COLUMNS if col not in reader.fieldnames]
        if missing:
            raise ValueError(f"Missing required columns: {', '.join(missing)}")

        rows: List[Dict[str, str]] = []
        for row in reader:
            row["token_address"] = normalize_address(row.get("token_address", ""))
            row["from_address"] = normalize_address(row.get("from_address", ""))
            row["to_address"] = normalize_address(row.get("to_address", ""))
            row["transaction_hash"] = normalize_address(row.get("transaction_hash", ""))
            rows.append(row)

    return rows


def find_exchange_pairs(rows: List[Dict[str, str]]) -> List[Tuple[Dict[str, str], Dict[str, str]]]:
    by_tx: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_tx[row["transaction_hash"]].append(row)

    pairs: List[Tuple[Dict[str, str], Dict[str, str]]] = []

    for tx_rows in by_tx.values():
        weth_rows = [r for r in tx_rows if r["token_address"] == WETH]
        usdc_rows = [r for r in tx_rows if r["token_address"] == USDC]

        used_usdc_idx = set()
        for weth_row in weth_rows:
            for idx, usdc_row in enumerate(usdc_rows):
                if idx in used_usdc_idx:
                    continue

                is_reverse = (
                    weth_row["from_address"] == usdc_row["to_address"]
                    and weth_row["to_address"] == usdc_row["from_address"]
                )
                if is_reverse:
                    pairs.append((weth_row, usdc_row))
                    used_usdc_idx.add(idx)
                    break

    return pairs


def to_string(value: Decimal) -> str:
    return format(value.normalize(), "f")


def write_output(path: Path, pairs: List[Tuple[Dict[str, str], Dict[str, str]]]) -> int:
    fieldnames = REQUIRED_COLUMNS + ["normalized_value", "weth_to_usdc_rate"]

    kept_pairs = 0

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for weth_row, usdc_row in pairs:
            weth_raw = parse_raw_value(weth_row)
            usdc_raw = parse_raw_value(usdc_row)

            weth_normalized = normalize_value(WETH, weth_raw)
            usdc_normalized = normalize_value(USDC, usdc_raw)

            if weth_normalized == 0:
                # Skip invalid exchange pair with zero WETH amount.
                continue

            rate = usdc_normalized / weth_normalized
            if not (MIN_ALLOWED_RATE <= rate <= MAX_ALLOWED_RATE):
                continue
            rate_str = to_string(rate)
            kept_pairs += 1

            out_weth = dict(weth_row)
            out_weth["normalized_value"] = to_string(weth_normalized)
            out_weth["weth_to_usdc_rate"] = rate_str
            writer.writerow(out_weth)

            out_usdc = dict(usdc_row)
            out_usdc["normalized_value"] = to_string(usdc_normalized)
            out_usdc["weth_to_usdc_rate"] = rate_str
            writer.writerow(out_usdc)

    return kept_pairs


def main() -> None:
    args = parse_args()
    rows = load_rows(args.input_csv)
    pairs = find_exchange_pairs(rows)
    kept_pairs = write_output(args.output_csv, pairs)
    print(f"Matched exchange pairs after rate filter: {kept_pairs}")
    print(f"Output written to: {args.output_csv}")


if __name__ == "__main__":
    main()
