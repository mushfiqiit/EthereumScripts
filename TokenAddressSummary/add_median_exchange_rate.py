#!/usr/bin/env python3
"""Add median USD exchange-rate estimates to token metadata frequency CSVs.

This script uses only local CSV files. It does not call Web3 or any Ethereum RPC
endpoint. It detects very simple two-transfer swaps where token X moves in one
direction and USDC/USDT moves back between the same two addresses in the same
transaction hash. The USD price per token X is computed as:

    ER = (stable_raw / 10^6) / (token_raw / 10^token_decimals)

USDC and USDT both use 6 decimals on Ethereum mainnet, so stable_raw / 10^6 is
human-readable USD-like stablecoin value. token_raw / 10^token_decimals is the
human-readable token amount. Dividing stablecoin value by token amount gives USD
per 1 token X, assuming USDC/USDT are approximately 1 USD.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from statistics import median
from typing import Iterable

getcontext().prec = 80

USDC_ADDRESS = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT_ADDRESS = "0xdac17f958d2ee523a2206206994597c13d831ec7"
STABLECOIN_ADDRESSES = {USDC_ADDRESS, USDT_ADDRESS}
STABLECOIN_DECIMALS = 6
REQUIRED_METADATA_COLUMNS = ["token_address", "token_symbol", "decimal", "token_occurrence_count"]
REQUIRED_TRANSFER_COLUMNS = ["transaction_hash", "token_address", "from_address", "to_address", "value"]
OUTPUT_EXCHANGE_RATE_FIELD = "median_exchange_rate_USD"


@dataclass(frozen=True)
class MetadataRow:
    row: dict[str, str]
    token_address: str
    decimal: int | None


@dataclass(frozen=True)
class TransferRow:
    transaction_hash: str
    token_address: str
    from_address: str
    to_address: str
    value: int


@dataclass
class MetadataStats:
    rows_loaded: int = 0
    rows_skipped_missing_token_address: int = 0
    rows_skipped_invalid_token_address: int = 0
    duplicate_token_address_rows: int = 0
    rows_with_invalid_decimal: int = 0
    rows_with_decimal_zero: int = 0
    stablecoin_rows: int = 0


@dataclass
class ScanStats:
    files_found: int = 0
    files_processed: int = 0
    files_skipped: int = 0
    rows_scanned: int = 0
    malformed_rows: int = 0
    invalid_address_rows: int = 0
    invalid_integer_rows: int = 0
    transaction_hash_groups_scanned: int = 0
    groups_skipped_not_exactly_two_rows: int = 0
    groups_skipped_malformed_rows: int = 0
    valid_direct_exchange_patterns: int = 0


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Add median_exchange_rate_USD to a token metadata frequency CSV using local token transfers.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--metadata-csv",
        default=str(script_dir / "token_address_frequency_metadata_25191301_25205700.csv"),
        help="Input token metadata frequency CSV from generate_token_metadata_frequency.py.",
    )
    parser.add_argument(
        "--input-folders",
        nargs="+",
        required=True,
        help="One or more extracted Ethereum_TT_<start>_<end> folders to scan recursively.",
    )
    parser.add_argument(
        "--output",
        default=str(script_dir / "token_address_frequency_metadata_with_exchange_rate_25191301_25205700.csv"),
        help="Output CSV path with median_exchange_rate_USD appended.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def normalize_hex(value: object) -> str:
    return str(value).strip().lower() if value is not None else ""


def is_hex_string(value: str, expected_hex_chars: int) -> bool:
    if not value.startswith("0x") or len(value) != expected_hex_chars + 2:
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def normalize_address(value: object) -> str | None:
    address = normalize_hex(value)
    if not is_hex_string(address, 40):
        return None
    return address


def normalize_transaction_hash(value: object) -> str | None:
    transaction_hash = normalize_hex(value)
    if not is_hex_string(transaction_hash, 64):
        return None
    return transaction_hash


def parse_decimal_value(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        decimal = int(text)
    except (TypeError, ValueError):
        return None
    if decimal < 0:
        return None
    return decimal


def parse_int_value(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def discover_token_transfer_csvs(input_folders: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for folder in input_folders:
        files.extend(path for path in folder.rglob("token_transfer_*.csv") if path.is_file())
    return sorted(files)


def validate_paths(metadata_csv: Path, input_folders: Iterable[Path], output_path: Path) -> None:
    if not metadata_csv.is_file():
        raise FileNotFoundError(f"Missing metadata CSV: {metadata_csv}")

    missing_folders = [str(folder) for folder in input_folders if not folder.is_dir()]
    if missing_folders:
        raise FileNotFoundError("Missing input folder(s): " + "; ".join(missing_folders))

    output_parent = output_path.expanduser().resolve().parent
    if output_parent.exists() and not output_parent.is_dir():
        raise NotADirectoryError(f"Output parent exists but is not a directory: {output_parent}")
    output_parent.mkdir(parents=True, exist_ok=True)


def field_lookup(fieldnames: list[str] | None) -> dict[str, str]:
    if not fieldnames:
        return {}
    return {field.strip(): field for field in fieldnames if field is not None}


def load_metadata_rows(metadata_csv: Path) -> tuple[list[MetadataRow], dict[str, int | None], list[str], MetadataStats]:
    rows: list[MetadataRow] = []
    decimals_by_token: dict[str, int | None] = {}
    stats = MetadataStats()
    seen_tokens: set[str] = set()

    try:
        with metadata_csv.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError(f"Metadata CSV is empty or missing a header: {metadata_csv}")

            fields = field_lookup(reader.fieldnames)
            missing_columns = [column for column in REQUIRED_METADATA_COLUMNS if column not in fields]
            if missing_columns:
                raise ValueError(
                    f"Metadata CSV missing required column(s) {missing_columns}: {metadata_csv}"
                )

            output_fieldnames = list(reader.fieldnames)
            if OUTPUT_EXCHANGE_RATE_FIELD not in output_fieldnames:
                output_fieldnames.append(OUTPUT_EXCHANGE_RATE_FIELD)

            for row_number, raw_row in enumerate(reader, start=2):
                raw_address = raw_row.get(fields["token_address"])
                if raw_address is None or str(raw_address).strip() == "":
                    stats.rows_skipped_missing_token_address += 1
                    logging.warning("Skipping metadata row %s missing token_address", row_number)
                    continue

                token_address = normalize_address(raw_address)
                if token_address is None:
                    stats.rows_skipped_invalid_token_address += 1
                    logging.warning("Skipping metadata row %s with invalid token_address: %r", row_number, raw_address)
                    continue

                is_duplicate = token_address in seen_tokens
                if is_duplicate:
                    stats.duplicate_token_address_rows += 1
                    logging.warning(
                        "Metadata row %s duplicates token_address %s; preserving the row but using the first decimal value for analysis",
                        row_number,
                        token_address,
                    )
                else:
                    seen_tokens.add(token_address)

                raw_row[fields["token_address"]] = token_address
                decimal = parse_decimal_value(raw_row.get(fields["decimal"]))
                if decimal is None:
                    stats.rows_with_invalid_decimal += 1
                    logging.warning(
                        "Metadata row %s for token %s has invalid decimal %r; exchange-rate calculation will be skipped for this token",
                        row_number,
                        token_address,
                        raw_row.get(fields["decimal"]),
                    )
                elif decimal == 0 and token_address not in STABLECOIN_ADDRESSES:
                    stats.rows_with_decimal_zero += 1

                if token_address in STABLECOIN_ADDRESSES:
                    stats.stablecoin_rows += 1

                rows.append(MetadataRow(row=dict(raw_row), token_address=token_address, decimal=decimal))
                if not is_duplicate:
                    decimals_by_token[token_address] = decimal

    except (OSError, csv.Error, UnicodeError) as exc:
        raise ValueError(f"Unable to read metadata CSV {metadata_csv}: {exc}") from exc

    if not rows:
        raise ValueError(f"No valid token metadata rows loaded from: {metadata_csv}")

    stats.rows_loaded = len(rows)
    return rows, decimals_by_token, output_fieldnames, stats


def parse_transfer_row(row: dict[str, str], fields: dict[str, str], row_number: int, path: Path, stats: ScanStats) -> tuple[str | None, TransferRow | None]:
    transaction_hash = normalize_transaction_hash(row.get(fields["transaction_hash"]))
    if transaction_hash is None:
        stats.malformed_rows += 1
        logging.debug("Skipping row %s in %s with invalid transaction_hash", row_number, path)
        return None, None

    token_address = normalize_address(row.get(fields["token_address"]))
    from_address = normalize_address(row.get(fields["from_address"]))
    to_address = normalize_address(row.get(fields["to_address"]))
    if token_address is None or from_address is None or to_address is None:
        stats.invalid_address_rows += 1
        return transaction_hash, None

    value = parse_int_value(row.get(fields["value"]))
    if value is None:
        stats.invalid_integer_rows += 1
        return transaction_hash, None

    return transaction_hash, TransferRow(
        transaction_hash=transaction_hash,
        token_address=token_address,
        from_address=from_address,
        to_address=to_address,
        value=value,
    )


def calculate_exchange_rate(
    x_row: TransferRow,
    stable_row: TransferRow,
    token_decimals: dict[str, int | None],
) -> Decimal | None:
    token_decimal = token_decimals.get(x_row.token_address)
    if token_decimal is None or token_decimal < 0:
        return None
    if x_row.value <= 0 or stable_row.value <= 0:
        return None
    if x_row.from_address != stable_row.to_address or x_row.to_address != stable_row.from_address:
        return None

    try:
        stable_amount = Decimal(stable_row.value) / (Decimal(10) ** STABLECOIN_DECIMALS)
        token_amount = Decimal(x_row.value) / (Decimal(10) ** token_decimal)
        if token_amount <= 0:
            return None
        return stable_amount / token_amount
    except (InvalidOperation, ZeroDivisionError, OverflowError):
        return None


def maybe_record_exchange_rate(
    first: TransferRow,
    second: TransferRow,
    eligible_tokens: set[str],
    token_decimals: dict[str, int | None],
    exchange_rates: dict[str, list[Decimal]],
) -> bool:
    candidates: tuple[tuple[TransferRow, TransferRow], ...] = ((first, second), (second, first))
    for x_row, stable_row in candidates:
        if x_row.token_address not in eligible_tokens:
            continue
        if stable_row.token_address not in STABLECOIN_ADDRESSES:
            continue
        exchange_rate = calculate_exchange_rate(x_row, stable_row, token_decimals)
        if exchange_rate is None:
            continue
        exchange_rates[x_row.token_address].append(exchange_rate)
        return True
    return False


def process_transfer_file(
    path: Path,
    eligible_tokens: set[str],
    token_decimals: dict[str, int | None],
    exchange_rates: dict[str, list[Decimal]],
    stats: ScanStats,
) -> None:
    groups: dict[str, list[TransferRow | None]] = defaultdict(list)

    try:
        with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                logging.warning("Skipping empty token transfer CSV with no header: %s", path)
                stats.files_skipped += 1
                return

            fields = field_lookup(reader.fieldnames)
            missing_columns = [column for column in REQUIRED_TRANSFER_COLUMNS if column not in fields]
            if missing_columns:
                logging.warning("Skipping %s because it is missing required column(s): %s", path, missing_columns)
                stats.files_skipped += 1
                return

            for row_number, row in enumerate(reader, start=2):
                stats.rows_scanned += 1
                try:
                    transaction_hash, transfer = parse_transfer_row(row, fields, row_number, path, stats)
                    if transaction_hash is not None:
                        groups[transaction_hash].append(transfer)
                except (csv.Error, TypeError, ValueError) as exc:
                    stats.malformed_rows += 1
                    logging.warning("Skipping malformed row %s in %s: %s", row_number, path, exc)

        stats.files_processed += 1
    except (OSError, UnicodeError, csv.Error) as exc:
        stats.files_skipped += 1
        logging.warning("Skipping unreadable or malformed token transfer CSV %s: %s", path, exc)
        return

    for transfers in groups.values():
        stats.transaction_hash_groups_scanned += 1
        if len(transfers) != 2:
            stats.groups_skipped_not_exactly_two_rows += 1
            continue
        if transfers[0] is None or transfers[1] is None:
            stats.groups_skipped_malformed_rows += 1
            continue
        if maybe_record_exchange_rate(transfers[0], transfers[1], eligible_tokens, token_decimals, exchange_rates):
            stats.valid_direct_exchange_patterns += 1


def scan_exchange_rates(
    csv_files: Iterable[Path],
    eligible_tokens: set[str],
    token_decimals: dict[str, int | None],
) -> tuple[dict[str, list[Decimal]], ScanStats]:
    exchange_rates: dict[str, list[Decimal]] = defaultdict(list)
    stats = ScanStats()
    files = list(csv_files)
    stats.files_found = len(files)

    for index, csv_file in enumerate(files, start=1):
        if index == 1 or index % 500 == 0 or index == len(files):
            logging.info("Scanning token transfer CSV %s/%s: %s", index, len(files), csv_file)
        process_transfer_file(csv_file, eligible_tokens, token_decimals, exchange_rates, stats)

    return exchange_rates, stats


def format_decimal(value: Decimal, max_decimal_places: int = 18) -> str:
    quantizer = Decimal(1).scaleb(-max_decimal_places)
    try:
        quantized = value.quantize(quantizer)
    except InvalidOperation:
        normalized = value.normalize()
        return format(normalized, "f")
    text = format(quantized.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    return text


def build_output_rows(metadata_rows: list[MetadataRow], exchange_rates: dict[str, list[Decimal]]) -> tuple[list[dict[str, str]], int]:
    output_rows: list[dict[str, str]] = []
    stablecoin_assigned = 0

    for metadata_row in metadata_rows:
        output_row = dict(metadata_row.row)
        token_address = metadata_row.token_address
        if token_address in STABLECOIN_ADDRESSES:
            output_row[OUTPUT_EXCHANGE_RATE_FIELD] = "1.0"
            stablecoin_assigned += 1
        else:
            rates = exchange_rates.get(token_address, [])
            if rates:
                output_row[OUTPUT_EXCHANGE_RATE_FIELD] = format_decimal(median(rates))
            else:
                output_row[OUTPUT_EXCHANGE_RATE_FIELD] = ""
        output_rows.append(output_row)

    return output_rows, stablecoin_assigned


def write_output(output_path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    output_parent = output_path.expanduser().resolve().parent
    output_parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)

    metadata_csv = Path(args.metadata_csv).expanduser().resolve()
    input_folders = [Path(folder).expanduser().resolve() for folder in args.input_folders]
    output_path = Path(args.output).expanduser().resolve()

    try:
        validate_paths(metadata_csv, input_folders, output_path)
        metadata_rows, token_decimals, output_fieldnames, metadata_stats = load_metadata_rows(metadata_csv)
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError) as exc:
        logging.error("Input validation failed: %s", exc)
        return 2

    eligible_tokens = {
        row.token_address
        for row in metadata_rows
        if row.token_address not in STABLECOIN_ADDRESSES and row.decimal is not None and row.decimal >= 0
    }

    logging.info("Metadata CSV path: %s", metadata_csv)
    logging.info("Input folders: %s", ", ".join(str(folder) for folder in input_folders))
    logging.info("Number of token metadata rows loaded: %s", metadata_stats.rows_loaded)
    logging.info("Number of metadata rows skipped due to missing token_address: %s", metadata_stats.rows_skipped_missing_token_address)
    logging.info("Number of metadata rows skipped due to invalid token_address: %s", metadata_stats.rows_skipped_invalid_token_address)
    logging.info("Number of duplicate token metadata rows preserved: %s", metadata_stats.duplicate_token_address_rows)
    logging.info("Number of metadata rows with invalid decimals: %s", metadata_stats.rows_with_invalid_decimal)
    logging.info("Number of non-stable tokens using decimal 0: %s", metadata_stats.rows_with_decimal_zero)
    logging.info("Number of token addresses eligible for exchange-rate analysis: %s", len(eligible_tokens))

    token_transfer_csvs = discover_token_transfer_csvs(input_folders)
    logging.info("Number of token_transfer CSV files found: %s", len(token_transfer_csvs))

    exchange_rates, scan_stats = scan_exchange_rates(token_transfer_csvs, eligible_tokens, token_decimals)
    tokens_with_exchange_rates = sum(1 for rates in exchange_rates.values() if rates)

    output_rows, stablecoin_assigned = build_output_rows(metadata_rows, exchange_rates)
    write_output(output_path, output_fieldnames, output_rows)

    logging.info("Number of token_transfer CSV files processed: %s", scan_stats.files_processed)
    logging.info("Number of token_transfer CSV files skipped: %s", scan_stats.files_skipped)
    logging.info("Number of rows scanned: %s", scan_stats.rows_scanned)
    logging.info("Number of malformed rows skipped: %s", scan_stats.malformed_rows)
    logging.info("Number of rows skipped due to invalid Ethereum addresses: %s", scan_stats.invalid_address_rows)
    logging.info("Number of rows skipped due to invalid integer values: %s", scan_stats.invalid_integer_rows)
    logging.info("Number of transaction_hash groups scanned: %s", scan_stats.transaction_hash_groups_scanned)
    logging.info(
        "Number of transaction_hash groups skipped because they did not have exactly two transfer rows: %s",
        scan_stats.groups_skipped_not_exactly_two_rows,
    )
    logging.info("Number of exactly-two-row groups skipped due to malformed rows: %s", scan_stats.groups_skipped_malformed_rows)
    logging.info("Number of valid direct exchange patterns found: %s", scan_stats.valid_direct_exchange_patterns)
    logging.info("Number of tokens with at least one exchange rate: %s", tokens_with_exchange_rates)
    logging.info("Number of USDT/USDC rows directly assigned 1.0: %s", stablecoin_assigned)
    logging.info("Output CSV path: %s", output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
