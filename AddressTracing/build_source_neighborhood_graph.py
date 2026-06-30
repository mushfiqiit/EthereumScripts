#!/usr/bin/env python3
"""Build a 5-hop send/receive neighborhood graph for one Ethereum address.

This is a convenience entry point for CSVs produced by
extract_newcsvs_transactions_tokentransfers.sh and SQLite indexes produced by
build_address_index.py. It traces direct currency-transfer relationships around
one source address, expanding only through addresses that sent currency to the
current address or received currency from the current address.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from argparse import Namespace
from decimal import Decimal
from pathlib import Path

from trace_address_graph import (
    configure_csv_field_limit,
    configure_logging,
    degree_counts,
    discover_sqlite_files,
    load_token_metadata,
    normalize_address,
    trace_graph,
    write_edges_csv,
    write_html_graph,
    write_nodes_csv,
)

ETHEREUM_TT_RE = re.compile(r"^Ethereum_TT_(\d+)_(\d+)$")
DEFAULT_MAX_DEPTH = 5
DEFAULT_MAX_NODES = 5_000
DEFAULT_MAX_EDGES = 25_000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build an interactive 5-hop graph around a source Ethereum address "
            "from extracted transaction/token-transfer CSVs and address indexes."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--source-address",
        required=True,
        help="Ethereum address to place at the center of the graph.",
    )
    parser.add_argument(
        "--csv-root",
        required=True,
        type=Path,
        help=(
            "Either the 7200-block Ethereum_TT_<start>_<end> folder produced by "
            "extract_newcsvs_transactions_tokentransfers.sh, or its parent directory."
        ),
    )
    parser.add_argument(
        "--index-dir",
        required=True,
        type=Path,
        help="Directory containing address_block_index_*.sqlite files for the same block range.",
    )
    parser.add_argument(
        "--token-metadata-csv",
        type=Path,
        help="Optional token metadata CSV for token symbols, decimals, and USD estimates.",
    )
    parser.add_argument(
        "--start-block",
        type=int,
        help="First block in the range. Inferred when --csv-root is an Ethereum_TT_* folder.",
    )
    parser.add_argument(
        "--end-block",
        type=int,
        help="Last block in the range. Inferred when --csv-root is an Ethereum_TT_* folder.",
    )
    parser.add_argument("--outer-range-size", type=int, default=7200)
    parser.add_argument("--chunk-size", type=int, default=100)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument("--max-edges", type=int, default=DEFAULT_MAX_EDGES)
    parser.add_argument(
        "--min-usd-value",
        type=Decimal,
        help="Only retain transfers with known USD value at or above this threshold.",
    )
    parser.add_argument(
        "--exclude-unknown-usd",
        action="store_true",
        help="When --min-usd-value is set, drop transfers whose USD value is unknown.",
    )
    parser.add_argument(
        "--eth-usd-price",
        type=Decimal,
        default=Decimal("2000.0"),
        help="Fixed ETH/USD price used to estimate ETH transaction values.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("AddressTracing/source_neighborhood_outputs"),
        help="Directory for generated HTML and CSV graph outputs.",
    )
    parser.add_argument("--output-prefix", help="Optional prefix for output file names.")
    parser.add_argument("--output-json", type=Path, help="Optional explicit JSON graph output path.")
    parser.add_argument("--skip-html", action="store_true", help="Only write portable JSON/CSV graph data; do not render local HTML.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    source = normalize_address(args.source_address)
    if source is None:
        raise SystemExit("Invalid --source-address. Expected 0x followed by exactly 40 hexadecimal characters.")
    args.source_address = source

    if args.max_depth < 0:
        raise SystemExit("--max-depth must be non-negative")
    if args.max_nodes <= 0:
        raise SystemExit("--max-nodes must be greater than zero")
    if args.max_edges <= 0:
        raise SystemExit("--max-edges must be greater than zero")

    return args


def resolve_range_and_base(csv_root: Path, start_block: int | None, end_block: int | None) -> tuple[Path, int, int, str]:
    csv_root = csv_root.expanduser().resolve()
    if not csv_root.is_dir():
        raise SystemExit(f"CSV root directory not found: {csv_root}")

    match = ETHEREUM_TT_RE.match(csv_root.name)
    if match:
        inferred_start, inferred_end = int(match.group(1)), int(match.group(2))
        start = start_block if start_block is not None else inferred_start
        end = end_block if end_block is not None else inferred_end
        return csv_root.parent, start, end, csv_root.name

    if start_block is None or end_block is None:
        raise SystemExit(
            "--start-block and --end-block are required when --csv-root is not an Ethereum_TT_<start>_<end> folder."
        )
    return csv_root, start_block, end_block, f"Ethereum_TT_{start_block}_{end_block}"


def output_paths(output_dir: Path, output_prefix: str | None, source_address: str, range_label: str, output_json: Path | None) -> tuple[Path, Path, Path, Path]:
    prefix = output_prefix or f"source_neighborhood_{source_address[:10]}_{range_label}"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_json or output_dir / f"{prefix}.json"
    return (
        output_dir / f"{prefix}.html",
        output_dir / f"{prefix}_edges.csv",
        output_dir / f"{prefix}_nodes.csv",
        json_path,
    )



def write_graph_json(discovered_depth, edges, source_address: str, output_path: Path, metadata: dict[str, object]) -> None:
    in_degree, out_degree = degree_counts(edges)
    node_addresses = sorted(
        set(discovered_depth) | {edge.source_address for edge in edges} | {edge.target_address for edge in edges},
        key=lambda address: (discovered_depth.get(address, 10**18), address),
    )
    payload = {
        "metadata": metadata,
        "nodes": [
            {
                "address": address,
                "is_source": address == source_address,
                "discovery_depth": discovered_depth.get(address),
                "in_degree": in_degree[address],
                "out_degree": out_degree[address],
                "total_degree": in_degree[address] + out_degree[address],
            }
            for address in node_addresses
        ],
        "edges": [edge.__dict__ for edge in edges],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

def make_trace_args(args: argparse.Namespace, data_base_dir: Path, start_block: int, end_block: int, html_path: Path, edges_path: Path, nodes_path: Path) -> Namespace:
    metadata_csv = args.token_metadata_csv or Path("__missing_token_metadata__.csv")
    return Namespace(
        root_address=args.source_address,
        data_base_dir=str(data_base_dir),
        index_base_dir=str(args.index_dir),
        token_metadata_csv=str(metadata_csv),
        start_block=start_block,
        end_block=end_block,
        outer_range_size=args.outer_range_size,
        chunk_size=args.chunk_size,
        eth_usd_price=args.eth_usd_price,
        output_html=str(html_path),
        output_edges_csv=str(edges_path),
        output_nodes_csv=str(nodes_path),
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
        max_edges=args.max_edges,
        no_limits=False,
        min_usd_value=args.min_usd_value,
        exclude_unknown_usd=args.exclude_unknown_usd,
        verbose=args.verbose,
    )


def main(argv: list[str] | None = None) -> int:
    configure_csv_field_limit()
    args = parse_args(argv)
    configure_logging(args.verbose)

    data_base_dir, start_block, end_block, range_label = resolve_range_and_base(
        args.csv_root, args.start_block, args.end_block
    )
    if start_block > end_block:
        raise SystemExit("--start-block must be less than or equal to --end-block")
    if not args.index_dir.is_dir():
        raise SystemExit(f"Index directory not found: {args.index_dir}")

    html_path, edges_path, nodes_path, json_path = output_paths(
        args.output_dir, args.output_prefix, args.source_address, range_label, args.output_json
    )
    trace_args = make_trace_args(args, data_base_dir, start_block, end_block, html_path, edges_path, nodes_path)

    db_files = discover_sqlite_files(args.index_dir)
    if not db_files:
        raise SystemExit(f"No address_block_index_*.sqlite files found under {args.index_dir}")

    token_metadata = load_token_metadata(Path(trace_args.token_metadata_csv))
    discovered_depth, edges, stats = trace_graph(trace_args, db_files, token_metadata)

    write_edges_csv(edges, edges_path)
    write_nodes_csv(discovered_depth, edges, args.source_address, nodes_path)
    write_graph_json(
        discovered_depth,
        edges,
        args.source_address,
        json_path,
        {
            "graph_type": "source_send_receive_neighborhood",
            "source_address": args.source_address,
            "range_label": range_label,
            "start_block": start_block,
            "end_block": end_block,
            "max_depth": args.max_depth,
            "max_nodes": args.max_nodes,
            "max_edges": args.max_edges,
        },
    )
    if not args.skip_html:
        write_html_graph(discovered_depth, edges, args.source_address, html_path)

    logging.info("Source address: %s", args.source_address)
    logging.info("Block range: %s-%s", start_block, end_block)
    logging.info("SQLite indexes scanned: %s", len(db_files))
    logging.info("Graph distance limit: %s hop(s)", args.max_depth)
    logging.info("Nodes discovered: %s", len(discovered_depth))
    logging.info("Edges discovered: %s", len(edges))
    logging.info("Traversal stop reason: %s", stats.traversal_stop_reason)
    if not args.skip_html:
        logging.info("Output HTML: %s", html_path)
    logging.info("Output JSON: %s", json_path)
    logging.info("Output edges CSV: %s", edges_path)
    logging.info("Output nodes CSV: %s", nodes_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
