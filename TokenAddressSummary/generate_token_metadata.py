#!/usr/bin/env python3
"""Build token-address frequency and metadata CSVs from Ethereum token-transfer exports."""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from web3 import Web3

DEFAULT_RPC_URL = "http://10.112.249.200:8545"
OUTPUT_FIELDS = ["token_address", "token_symbol", "decimal", "token_occurrence_count"]
CACHE_FIELDS = ["token_address", "token_symbol", "decimal"]

ERC20_STRING_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_BYTES32_SYMBOL_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "bytes32"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    }
]


@dataclass
class ScanStats:
    files_found: int = 0
    files_processed: int = 0
    files_skipped: int = 0
    rows_scanned: int = 0
    valid_token_address_rows: int = 0
    invalid_token_address_rows: int = 0
    malformed_rows: int = 0


@dataclass(frozen=True)
class TokenMetadata:
    token_symbol: str = ""
    decimal: int = 0


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    default_rpc_url = os.environ.get("ETHEREUM_RPC_URL", DEFAULT_RPC_URL)

    parser = argparse.ArgumentParser(
        description="Count token_address occurrences in token_transfer_*.csv files and append ERC-20 metadata.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input-folders",
        nargs="+",
        required=True,
        help="One or more extracted Ethereum_TT_<start>_<end> folders to scan recursively.",
    )
    parser.add_argument(
        "--rpc-url",
        default=default_rpc_url,
        help="Ethereum JSON-RPC URL. Can also be set with ETHEREUM_RPC_URL.",
    )
    parser.add_argument(
        "--output",
        default=str(script_dir / "token_address_frequency_metadata_25191301_25205700.csv"),
        help="Final token frequency + metadata CSV path.",
    )
    parser.add_argument(
        "--metadata-cache",
        default=str(script_dir / "token_metadata_cache.csv"),
        help="CSV cache for token metadata lookups.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def normalize_token_address(value: object) -> str | None:
    if value is None:
        return None
    address = str(value).strip()
    if not address or address.lower() in {"null", "none", "nan"}:
        return None
    if not Web3.is_address(address):
        return None
    return address.lower()


def discover_token_transfer_csvs(input_folders: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for folder in input_folders:
        files.extend(path for path in folder.rglob("token_transfer_*.csv") if path.is_file())
    return sorted(files)


def validate_input_folders(input_folders: Iterable[Path]) -> None:
    missing = [str(folder) for folder in input_folders if not folder.is_dir()]
    if missing:
        raise FileNotFoundError("Missing input folder(s): " + "; ".join(missing))


def ensure_parent_directory(path: Path, label: str) -> None:
    parent = path.expanduser().resolve().parent
    if parent.exists() and not parent.is_dir():
        raise NotADirectoryError(f"{label} parent exists but is not a directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True)


def scan_csv_file(path: Path, counts: Counter[str], stats: ScanStats) -> None:
    try:
        with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                logging.warning("Skipping empty CSV with no header: %s", path)
                stats.files_skipped += 1
                return

            token_address_field = next(
                (field for field in reader.fieldnames if field and field.strip() == "token_address"),
                None,
            )
            if token_address_field is None:
                logging.warning("Skipping CSV missing token_address column: %s", path)
                stats.files_skipped += 1
                return

            for row_number, row in enumerate(reader, start=2):
                try:
                    stats.rows_scanned += 1
                    raw_address = row.get(token_address_field)
                    normalized = normalize_token_address(raw_address)
                    if normalized is None:
                        stats.invalid_token_address_rows += 1
                        continue
                    counts[normalized] += 1
                    stats.valid_token_address_rows += 1
                except (csv.Error, ValueError, TypeError) as exc:
                    stats.malformed_rows += 1
                    logging.warning("Skipping malformed row %s in %s: %s", row_number, path, exc)

            stats.files_processed += 1
    except (OSError, UnicodeError, csv.Error) as exc:
        stats.files_skipped += 1
        logging.warning("Skipping unreadable or malformed CSV %s: %s", path, exc)


def scan_token_addresses(csv_files: Iterable[Path]) -> tuple[Counter[str], ScanStats]:
    counts: Counter[str] = Counter()
    stats = ScanStats()
    files = list(csv_files)
    stats.files_found = len(files)

    for index, csv_file in enumerate(files, start=1):
        if index == 1 or index % 500 == 0 or index == len(files):
            logging.info("Scanning token transfer CSV %s/%s: %s", index, len(files), csv_file)
        scan_csv_file(csv_file, counts, stats)

    return counts, stats


def parse_decimal(value: object) -> int:
    try:
        decimal = int(str(value).strip())
        return decimal if decimal >= 0 else 0
    except (TypeError, ValueError):
        return 0


def load_metadata_cache(cache_path: Path) -> dict[str, TokenMetadata]:
    metadata: dict[str, TokenMetadata] = {}
    if not cache_path.exists():
        return metadata

    try:
        with cache_path.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "token_address" not in reader.fieldnames:
                logging.warning("Ignoring metadata cache without token_address column: %s", cache_path)
                return metadata
            for row in reader:
                address = normalize_token_address(row.get("token_address"))
                if address is None:
                    continue
                metadata[address] = TokenMetadata(
                    token_symbol=(row.get("token_symbol") or "").strip(),
                    decimal=parse_decimal(row.get("decimal")),
                )
    except (OSError, UnicodeError, csv.Error) as exc:
        logging.warning("Ignoring unreadable metadata cache %s: %s", cache_path, exc)
        return {}

    return metadata


def decode_symbol(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def fetch_symbol(web3: Web3, checksum_address: str) -> str:
    string_contract = web3.eth.contract(address=checksum_address, abi=ERC20_STRING_ABI)
    try:
        return decode_symbol(string_contract.functions.symbol().call())
    except Exception as exc:  # noqa: BLE001 - web3 providers raise many implementation-specific exceptions.
        logging.debug("symbol() string call failed for %s: %s", checksum_address, exc)

    bytes32_contract = web3.eth.contract(address=checksum_address, abi=ERC20_BYTES32_SYMBOL_ABI)
    try:
        return decode_symbol(bytes32_contract.functions.symbol().call())
    except Exception as exc:  # noqa: BLE001
        logging.debug("symbol() bytes32 call failed for %s: %s", checksum_address, exc)
        return ""


def fetch_decimals(web3: Web3, checksum_address: str) -> int:
    contract = web3.eth.contract(address=checksum_address, abi=ERC20_STRING_ABI)
    try:
        return parse_decimal(contract.functions.decimals().call())
    except Exception as exc:  # noqa: BLE001
        logging.debug("decimals() call failed for %s: %s", checksum_address, exc)
        return 0


def fetch_token_metadata(web3: Web3, token_address: str) -> TokenMetadata:
    checksum_address = Web3.to_checksum_address(token_address)
    symbol = fetch_symbol(web3, checksum_address)
    decimals = fetch_decimals(web3, checksum_address)
    return TokenMetadata(token_symbol=symbol, decimal=decimals)


def fetch_missing_metadata(
    token_addresses: Iterable[str],
    cache: dict[str, TokenMetadata],
    rpc_url: str,
) -> tuple[dict[str, TokenMetadata], int]:
    token_addresses = sorted(set(token_addresses))
    missing_addresses = [address for address in token_addresses if address not in cache]
    if not missing_addresses:
        return cache, 0

    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    try:
        connected = web3.is_connected()
    except Exception as exc:  # noqa: BLE001
        logging.warning("RPC connection check failed for %s: %s", rpc_url, exc)
        connected = False

    if not connected:
        logging.warning(
            "RPC endpoint is not reachable: %s. New addresses will use default metadata in output but will not be cached.",
            rpc_url,
        )
        return cache, 0

    rpc_calls = 0
    for index, address in enumerate(missing_addresses, start=1):
        if index == 1 or index % 100 == 0 or index == len(missing_addresses):
            logging.info("Fetching token metadata %s/%s: %s", index, len(missing_addresses), address)
        try:
            cache[address] = fetch_token_metadata(web3, address)
            rpc_calls += 1
        except Exception as exc:  # noqa: BLE001
            logging.warning("Metadata lookup failed for %s: %s", address, exc)
            cache[address] = TokenMetadata()
            rpc_calls += 1

    return cache, rpc_calls


def write_metadata_cache(cache_path: Path, metadata: dict[str, TokenMetadata]) -> None:
    ensure_parent_directory(cache_path, "Metadata cache")
    with cache_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CACHE_FIELDS)
        writer.writeheader()
        for address in sorted(metadata):
            item = metadata[address]
            writer.writerow(
                {
                    "token_address": address,
                    "token_symbol": item.token_symbol,
                    "decimal": item.decimal,
                }
            )


def write_output(output_path: Path, counts: Counter[str], metadata: dict[str, TokenMetadata]) -> None:
    ensure_parent_directory(output_path, "Output")
    sorted_rows = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for address, count in sorted_rows:
            item = metadata.get(address, TokenMetadata())
            writer.writerow(
                {
                    "token_address": address,
                    "token_symbol": item.token_symbol,
                    "decimal": item.decimal,
                    "token_occurrence_count": count,
                }
            )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)

    input_folders = [Path(folder).expanduser().resolve() for folder in args.input_folders]
    output_path = Path(args.output).expanduser().resolve()
    cache_path = Path(args.metadata_cache).expanduser().resolve()

    try:
        validate_input_folders(input_folders)
        ensure_parent_directory(output_path, "Output")
        ensure_parent_directory(cache_path, "Metadata cache")
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        logging.error("Path validation failed: %s", exc)
        return 2

    logging.info("Input folders: %s", ", ".join(str(folder) for folder in input_folders))
    logging.info("RPC URL: %s", args.rpc_url)
    logging.info("Output CSV path: %s", output_path)
    logging.info("Metadata cache path: %s", cache_path)

    csv_files = discover_token_transfer_csvs(input_folders)
    logging.info("Number of token_transfer CSV files found: %s", len(csv_files))

    counts, stats = scan_token_addresses(csv_files)
    logging.info("Number of files successfully processed: %s", stats.files_processed)
    logging.info("Number of files skipped: %s", stats.files_skipped)
    logging.info("Number of rows scanned: %s", stats.rows_scanned)
    logging.info("Number of valid token address rows: %s", stats.valid_token_address_rows)
    logging.info("Number of invalid/empty token address rows skipped: %s", stats.invalid_token_address_rows)
    logging.info("Number of malformed rows skipped: %s", stats.malformed_rows)
    logging.info("Number of unique token addresses found: %s", len(counts))

    metadata_cache = load_metadata_cache(cache_path)
    logging.info("Number of metadata entries loaded from cache: %s", len(metadata_cache))

    metadata_cache, rpc_calls = fetch_missing_metadata(counts.keys(), metadata_cache, args.rpc_url)
    logging.info("Number of new metadata RPC calls made: %s", rpc_calls)

    write_metadata_cache(cache_path, metadata_cache)
    write_output(output_path, counts, metadata_cache)

    logging.info("Wrote output CSV: %s", output_path)
    logging.info("Saved metadata cache: %s", cache_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
