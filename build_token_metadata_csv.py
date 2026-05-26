#!/usr/bin/env python3

import argparse
import csv
import json
import time
from pathlib import Path
from collections import defaultdict

import requests
from web3 import Web3


ERC20_METADATA_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]


# Some old/non-standard ERC-20 contracts return bytes32 instead of string.
ERC20_BYTES32_METADATA_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"name": "", "type": "bytes32"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "bytes32"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]


TOKEN_LIST_URLS = [
    # Uniswap default token list
    "https://tokens.uniswap.org",
]


def normalize_address(addr):
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
    if not fieldnames:
        return None

    normalized = {name.strip().lower(): name for name in fieldnames}

    for candidate in candidates:
        candidate = candidate.lower()
        if candidate in normalized:
            return normalized[candidate]

    return None


def extract_block_number_from_filename(path):
    stem = path.stem

    # Examples:
    # token_transfer_25112201.csv
    # transaction_25112201.csv
    parts = stem.split("_")

    for part in reversed(parts):
        if part.isdigit():
            return int(part)

    return None


def scan_token_transfer_csvs(data_root):
    """
    Recursively scan token_transfer_*.csv files and extract token_address values.

    Returns:
        token_stats[address] = {
            "occurrence_count": int,
            "first_block": int or None,
            "last_block": int or None
        }
    """

    data_root = Path(data_root)
    token_stats = defaultdict(lambda: {
        "occurrence_count": 0,
        "first_block": None,
        "last_block": None,
    })

    csv_files = sorted(data_root.rglob("token_transfer_*.csv"))

    print(f"[INFO] Found {len(csv_files)} token_transfer CSV files.")

    for i, csv_path in enumerate(csv_files, start=1):
        if i % 500 == 0:
            print(f"[INFO] Processed {i}/{len(csv_files)} files...")

        block_from_filename = extract_block_number_from_filename(csv_path)

        try:
            with csv_path.open("r", newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)

                token_col = detect_column(
                    reader.fieldnames,
                    ["token_address", "contract_address", "token", "address"]
                )

                block_col = detect_column(
                    reader.fieldnames,
                    ["block_number", "blockNumber", "block"]
                )

                if token_col is None:
                    print(f"[WARN] No token_address column found in {csv_path}")
                    continue

                for row in reader:
                    token_address = normalize_address(row.get(token_col))
                    if not token_address:
                        continue

                    if block_col and row.get(block_col):
                        try:
                            block_number = int(row.get(block_col))
                        except ValueError:
                            block_number = block_from_filename
                    else:
                        block_number = block_from_filename

                    stats = token_stats[token_address]
                    stats["occurrence_count"] += 1

                    if block_number is not None:
                        if stats["first_block"] is None or block_number < stats["first_block"]:
                            stats["first_block"] = block_number

                        if stats["last_block"] is None or block_number > stats["last_block"]:
                            stats["last_block"] = block_number

        except Exception as e:
            print(f"[WARN] Failed to read {csv_path}: {e}")

    print(f"[INFO] Unique token addresses found: {len(token_stats)}")
    return token_stats


def load_token_lists():
    """
    Load curated token metadata from public token lists.

    Returns:
        metadata[address] = {
            "token_name": str,
            "token_symbol": str,
            "decimals": int,
            "source": "token_list"
        }
    """

    metadata = {}

    for url in TOKEN_LIST_URLS:
        print(f"[INFO] Loading token list: {url}")

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"[WARN] Failed to load token list {url}: {e}")
            continue

        tokens = data.get("tokens", [])

        for token in tokens:
            # Ethereum mainnet = chainId 1
            if token.get("chainId") != 1:
                continue

            address = normalize_address(token.get("address"))
            if not address:
                continue

            metadata[address] = {
                "token_name": token.get("name", ""),
                "token_symbol": token.get("symbol", ""),
                "decimals": token.get("decimals", ""),
                "source": "token_list",
            }

    print(f"[INFO] Loaded {len(metadata)} Ethereum mainnet tokens from token lists.")
    return metadata


def bytes32_to_text(value):
    if isinstance(value, bytes):
        return value.rstrip(b"\x00").decode("utf-8", errors="replace")
    return str(value)


def query_contract_metadata(w3, token_address):
    """
    Query ERC-20 name(), symbol(), decimals() directly from the token contract.
    Tries normal string ABI first, then bytes32 fallback.
    """

    checksum_address = to_checksum_address_compat(token_address)
    # First try standard string-returning metadata ABI.
    try:
        contract = w3.eth.contract(
            address=checksum_address,
            abi=ERC20_METADATA_ABI
        )

        name = contract.functions.name().call()
        symbol = contract.functions.symbol().call()
        decimals = contract.functions.decimals().call()

        return {
            "token_name": str(name),
            "token_symbol": str(symbol),
            "decimals": int(decimals),
            "source": "onchain",
        }

    except Exception:
        pass

    # Then try old/non-standard bytes32-returning ABI.
    try:
        contract = w3.eth.contract(
            address=checksum_address,
            abi=ERC20_BYTES32_METADATA_ABI
        )

        name = bytes32_to_text(contract.functions.name().call())
        symbol = bytes32_to_text(contract.functions.symbol().call())
        decimals = contract.functions.decimals().call()

        return {
            "token_name": name,
            "token_symbol": symbol,
            "decimals": int(decimals),
            "source": "onchain_bytes32",
        }

    except Exception as e:
        return {
            "token_name": "",
            "token_symbol": "",
            "decimals": "",
            "source": f"failed: {type(e).__name__}",
        }

def web3_is_connected(w3):
    if hasattr(w3, "is_connected"):
        return w3.is_connected()
    if hasattr(w3, "isConnected"):
        return w3.isConnected()
    raise AttributeError("This Web3 version has neither is_connected() nor isConnected().")


def get_latest_block_number(w3):
    if hasattr(w3.eth, "block_number"):
        return w3.eth.block_number
    if hasattr(w3.eth, "blockNumber"):
        return w3.eth.blockNumber
    raise AttributeError("This Web3 version has neither eth.block_number nor eth.blockNumber.")


def to_checksum_address_compat(address):
    if hasattr(Web3, "to_checksum_address"):
        return Web3.to_checksum_address(address)
    if hasattr(Web3, "toChecksumAddress"):
        return Web3.toChecksumAddress(address)
    raise AttributeError("This Web3 version has neither to_checksum_address() nor toChecksumAddress().")


def enrich_with_onchain_metadata(token_stats, metadata, rpc_url, sleep_seconds=0.0):
    w3 = Web3(Web3.HTTPProvider(rpc_url))

    if not web3_is_connected(w3):
        raise RuntimeError(f"Could not connect to Ethereum RPC: {rpc_url}")

    print(f"[INFO] Connected to Ethereum RPC. Latest block: {get_latest_block_number(w3)}")
    
    missing = [addr for addr in token_stats.keys() if addr not in metadata]

    print(f"[INFO] Tokens missing from token list: {len(missing)}")
    print("[INFO] Querying missing token contracts on-chain...")

    for i, token_address in enumerate(missing, start=1):
        if i % 100 == 0:
            print(f"[INFO] Queried {i}/{len(missing)} missing tokens...")

        metadata[token_address] = query_contract_metadata(w3, token_address)

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return metadata


def write_output_csv(token_stats, metadata, output_path):
    output_path = Path(output_path)

    rows = []

    for address, stats in token_stats.items():
        meta = metadata.get(address, {})

        rows.append({
            "token_address": address,
            "token_name": meta.get("token_name", ""),
            "token_symbol": meta.get("token_symbol", ""),
            "decimals": meta.get("decimals", ""),
            "source": meta.get("source", "missing"),
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
            "source",
            "occurrence_count",
            "first_block",
            "last_block",
        ]

        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[INFO] Wrote token metadata CSV: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Build a token metadata CSV from Ethereum token_transfer CSV files."
    )

    parser.add_argument(
        "--data-root",
        required=True,
        help="Root folder containing Ethereum_TT_* folders."
    )

    parser.add_argument(
        "--rpc",
        required=True,
        help="Ethereum JSON-RPC URL, for example http://10.112.249.200:8545"
    )

    parser.add_argument(
        "--output",
        default="token_metadata.csv",
        help="Output CSV path. Default: token_metadata.csv"
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Sleep seconds between on-chain RPC calls. Useful for public RPC rate limits."
    )

    parser.add_argument(
        "--skip-onchain",
        action="store_true",
        help="Only use token lists. Do not query token contracts on-chain."
    )

    args = parser.parse_args()

    token_stats = scan_token_transfer_csvs(args.data_root)

    metadata = load_token_lists()

    if not args.skip_onchain:
        metadata = enrich_with_onchain_metadata(
            token_stats=token_stats,
            metadata=metadata,
            rpc_url=args.rpc,
            sleep_seconds=args.sleep,
        )

    write_output_csv(
        token_stats=token_stats,
        metadata=metadata,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()