#!/usr/bin/env python3

"""
build_token_metadata_csv.py

Purpose:
    Build a token metadata CSV from Ethereum token_transfer CSV files.

What this script does:
    1. Recursively scans token_transfer_*.csv files.
    2. Extracts unique token_address values.
    3. Counts how many times each token appears.
    4. Tracks first_block and last_block where each token appears.
    5. Optionally matches token addresses against a common token list.
    6. Writes one output row per unique token_address.

Important:
    This script DOES NOT query missing token contracts on-chain.
    Unknown tokens are kept in the output CSV with blank metadata fields.

Output columns:
    token_address,
    token_name,
    token_symbol,
    decimals,
    metadata_source,
    occurrence_count,
    first_block,
    last_block

Example:
    python build_token_metadata_csv.py \
      --data-root "/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable/Ethereum_TT_25112101_25119300" \
      --output token_metadata.csv
"""

import argparse
import csv
import json
import sys
import urllib.request
from pathlib import Path
from collections import defaultdict


DEFAULT_TOKEN_LIST_URL = "https://tokens.uniswap.org"


def normalize_address(addr):
    """
    Normalize an Ethereum address.

    Returns:
        lowercase 0x-prefixed address if valid,
        otherwise None.
    """

    if addr is None:
        return None

    addr = str(addr).strip().lower()

    if addr in {"", "nan", "none", "null"}:
        return None

    if not addr.startswith("0x"):
        return None

    if len(addr) != 42:
        return None

    try:
        int(addr[2:], 16)
    except ValueError:
        return None

    return addr


def detect_column(fieldnames, candidates):
    """
    Detect a column from possible candidate names.

    Example:
        candidates = ["token_address", "contract_address"]

    Matching is case-insensitive and ignores surrounding whitespace.
    """

    if not fieldnames:
        return None

    normalized = {}

    for name in fieldnames:
        if name is None:
            continue
        normalized[name.strip().lower()] = name

    for candidate in candidates:
        key = candidate.strip().lower()
        if key in normalized:
            return normalized[key]

    return None


def extract_block_number_from_filename(path):
    """
    Extract block number from filenames such as:

        token_transfer_25112201.csv
        transaction_25112201.csv

    Returns:
        int block number if found,
        otherwise None.
    """

    stem = path.stem
    parts = stem.split("_")

    for part in reversed(parts):
        if part.isdigit():
            return int(part)

    return None


def safe_int(value):
    """
    Convert value to int safely.

    Returns:
        int if possible,
        otherwise None.
    """

    if value is None:
        return None

    try:
        return int(str(value).strip())
    except Exception:
        return None


def scan_token_transfer_csvs(data_root):
    """
    Recursively scan token_transfer_*.csv files and extract token_address values.

    Returns:
        token_stats[token_address] = {
            "occurrence_count": int,
            "first_block": int or None,
            "last_block": int or None
        }
    """

    data_root = Path(data_root)

    if not data_root.exists():
        raise FileNotFoundError(f"Data root does not exist: {data_root}")

    if not data_root.is_dir():
        raise NotADirectoryError(f"Data root is not a directory: {data_root}")

    token_stats = defaultdict(lambda: {
        "occurrence_count": 0,
        "first_block": None,
        "last_block": None,
    })

    csv_files = sorted(data_root.rglob("token_transfer_*.csv"))

    print(f"[INFO] Data root: {data_root}")
    print(f"[INFO] Found {len(csv_files)} token_transfer CSV files.")

    if not csv_files:
        print("[WARN] No token_transfer_*.csv files found.")
        return token_stats

    skipped_no_token_col = 0
    failed_files = 0

    for i, csv_path in enumerate(csv_files, start=1):
        if i % 500 == 0:
            print(f"[INFO] Processed {i}/{len(csv_files)} files...")

        block_from_filename = extract_block_number_from_filename(csv_path)

        try:
            with csv_path.open("r", newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)

                token_col = detect_column(
                    reader.fieldnames,
                    [
                        "token_address",
                        "contract_address",
                        "token",
                        "address",
                    ]
                )

                block_col = detect_column(
                    reader.fieldnames,
                    [
                        "block_number",
                        "blocknumber",
                        "block",
                    ]
                )

                if token_col is None:
                    skipped_no_token_col += 1
                    print(f"[WARN] No token_address column found in {csv_path}")
                    continue

                for row in reader:
                    token_address = normalize_address(row.get(token_col))

                    if not token_address:
                        continue

                    block_number = None

                    if block_col:
                        block_number = safe_int(row.get(block_col))

                    if block_number is None:
                        block_number = block_from_filename

                    stats = token_stats[token_address]
                    stats["occurrence_count"] += 1

                    if block_number is not None:
                        if stats["first_block"] is None or block_number < stats["first_block"]:
                            stats["first_block"] = block_number

                        if stats["last_block"] is None or block_number > stats["last_block"]:
                            stats["last_block"] = block_number

        except Exception as e:
            failed_files += 1
            print(f"[WARN] Failed to read {csv_path}: {e}")

    print(f"[INFO] Finished scanning token transfer CSV files.")
    print(f"[INFO] Unique token addresses found: {len(token_stats)}")
    print(f"[INFO] Files skipped due to missing token_address column: {skipped_no_token_col}")
    print(f"[INFO] Files failed due to read/parsing errors: {failed_files}")

    return token_stats


def load_json_from_url(url):
    """
    Load JSON from URL using Python standard library.

    This makes only one request to the token list URL.
    It does not make any per-token requests.
    """

    print(f"[INFO] Loading token list from URL: {url}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 token-metadata-builder"
        }
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read().decode("utf-8")

    return json.loads(raw)


def load_json_from_file(path):
    """
    Load token list JSON from local file.
    """

    path = Path(path)

    print(f"[INFO] Loading token list from local file: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_token_list(data, chain_id=1):
    """
    Parse a Uniswap-style token list JSON.

    Returns:
        metadata[address] = {
            "token_name": str,
            "token_symbol": str,
            "decimals": int or "",
            "metadata_source": str
        }
    """

    metadata = {}

    tokens = data.get("tokens", [])

    if not isinstance(tokens, list):
        print("[WARN] Token list JSON does not contain a valid 'tokens' list.")
        return metadata

    for token in tokens:
        if not isinstance(token, dict):
            continue

        if token.get("chainId") != chain_id:
            continue

        address = normalize_address(token.get("address"))

        if not address:
            continue

        decimals = token.get("decimals", "")

        if decimals != "":
            try:
                decimals = int(decimals)
            except Exception:
                decimals = ""

        metadata[address] = {
            "token_name": str(token.get("name", "") or ""),
            "token_symbol": str(token.get("symbol", "") or ""),
            "decimals": decimals,
            "metadata_source": "token_list",
        }

    print(f"[INFO] Loaded {len(metadata)} token metadata entries for chain_id={chain_id}.")
    return metadata


def load_token_metadata_from_list(token_list_url=None, token_list_json=None, chain_id=1):
    """
    Load metadata from either:
        1. local token list JSON file, or
        2. token list URL.

    This does not query missing tokens individually.
    """

    if token_list_json:
        data = load_json_from_file(token_list_json)
        return parse_token_list(data, chain_id=chain_id)

    if token_list_url:
        try:
            data = load_json_from_url(token_list_url)
            return parse_token_list(data, chain_id=chain_id)
        except Exception as e:
            print(f"[WARN] Failed to load token list from URL: {e}")
            print("[WARN] Continuing without external token metadata.")
            return {}

    return {}


def write_output_csv(token_stats, metadata, output_path):
    """
    Write one row per unique token address.

    Unknown tokens are still written, but their name/symbol/decimals are blank.
    """

    output_path = Path(output_path)

    rows = []

    for address, stats in token_stats.items():
        meta = metadata.get(address)

        if meta is None:
            meta = {
                "token_name": "",
                "token_symbol": "",
                "decimals": "",
                "metadata_source": "missing_from_token_list",
            }

        rows.append({
            "token_address": address,
            "token_name": meta.get("token_name", ""),
            "token_symbol": meta.get("token_symbol", ""),
            "decimals": meta.get("decimals", ""),
            "metadata_source": meta.get("metadata_source", ""),
            "occurrence_count": stats.get("occurrence_count", 0),
            "first_block": stats.get("first_block", ""),
            "last_block": stats.get("last_block", ""),
        })

    rows.sort(
        key=lambda r: int(r["occurrence_count"]),
        reverse=True
    )

    with output_path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "token_address",
            "token_name",
            "token_symbol",
            "decimals",
            "metadata_source",
            "occurrence_count",
            "first_block",
            "last_block",
        ]

        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    known = sum(1 for row in rows if row["metadata_source"] == "token_list")
    missing = sum(1 for row in rows if row["metadata_source"] == "missing_from_token_list")

    print(f"[INFO] Wrote output CSV: {output_path}")
    print(f"[INFO] Total unique token addresses written: {len(rows)}")
    print(f"[INFO] Tokens matched from token list: {known}")
    print(f"[INFO] Tokens missing from token list: {missing}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Build a token metadata CSV from Ethereum token_transfer CSV files. "
            "This script does not perform per-token on-chain RPC queries."
        )
    )

    parser.add_argument(
        "--data-root",
        required=True,
        help="Root folder containing Ethereum_TT_* folders or one Ethereum_TT_* folder."
    )

    parser.add_argument(
        "--output",
        default="token_metadata.csv",
        help="Output CSV path. Default: token_metadata.csv"
    )

    parser.add_argument(
        "--chain-id",
        type=int,
        default=1,
        help="Chain ID to filter token list entries. Ethereum mainnet = 1. Default: 1"
    )

    parser.add_argument(
        "--token-list-url",
        default=DEFAULT_TOKEN_LIST_URL,
        help=(
            "Token list URL to use for common token metadata. "
            "Default: https://tokens.uniswap.org"
        )
    )

    parser.add_argument(
        "--token-list-json",
        default=None,
        help=(
            "Optional local token list JSON file. "
            "If provided, this is used instead of --token-list-url."
        )
    )

    parser.add_argument(
        "--skip-token-list",
        action="store_true",
        help=(
            "Only extract token addresses from CSVs. "
            "Do not load any token list. Metadata fields will be blank."
        )
    )

    args = parser.parse_args()

    token_stats = scan_token_transfer_csvs(args.data_root)

    if not token_stats:
        print("[WARN] No token addresses found. Writing empty output CSV.")

    if args.skip_token_list:
        print("[INFO] Skipping token list loading. Metadata fields will be blank.")
        metadata = {}
    else:
        metadata = load_token_metadata_from_list(
            token_list_url=args.token_list_url,
            token_list_json=args.token_list_json,
            chain_id=args.chain_id,
        )

    write_output_csv(
        token_stats=token_stats,
        metadata=metadata,
        output_path=args.output,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user.")
        sys.exit(130)
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)