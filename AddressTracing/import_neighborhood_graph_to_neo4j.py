#!/usr/bin/env python3
"""Import a generated source-neighborhood graph JSON/CSV into local Neo4j."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load a source-neighborhood graph JSON or node/edge CSV files into Neo4j.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--graph-json", type=Path, help="JSON produced by build_source_neighborhood_graph.py")
    input_group.add_argument("--edges-csv", type=Path, help="Edges CSV produced by build_source_neighborhood_graph.py")
    parser.add_argument("--nodes-csv", type=Path, help="Nodes CSV; required when --edges-csv is used")
    parser.add_argument("--graph-id", help="Graph ID stored on nodes/relationships. Defaults to JSON metadata or file stem.")
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--clear-graph", action="store_true", help="Delete existing imported data for this graph_id before loading.")
    args = parser.parse_args()
    if args.edges_csv and not args.nodes_csv:
        parser.error("--nodes-csv is required when --edges-csv is used")
    if not args.password:
        parser.error("Neo4j password is required via --password or NEO4J_PASSWORD")
    if args.batch_size <= 0:
        parser.error("--batch-size must be greater than zero")
    return args


def parse_bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_float(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def stable_transfer_id(edge: dict[str, Any]) -> str:
    parts = [
        str(edge.get("source_address", "")),
        str(edge.get("target_address", "")),
        str(edge.get("source_type", "")),
        str(edge.get("block_number", "")),
        str(edge.get("transaction_hash", "")),
        str(edge.get("token_address", "")),
        str(edge.get("raw_value", "")),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def normalize_node(row: dict[str, Any], graph_id: str) -> dict[str, Any]:
    address = str(row.get("address", "")).strip().lower()
    return {
        "graph_id": graph_id,
        "address": address,
        "is_source": parse_bool(row.get("is_source") if "is_source" in row else row.get("is_root")),
        "discovery_depth": parse_int(row.get("discovery_depth")),
        "in_degree": parse_int(row.get("in_degree")) or 0,
        "out_degree": parse_int(row.get("out_degree")) or 0,
        "total_degree": parse_int(row.get("total_degree")) or 0,
        "short_label": f"{address[:6]}...{address[-4:]}" if len(address) >= 10 else address,
    }


def normalize_edge(row: dict[str, Any], graph_id: str) -> dict[str, Any]:
    edge = dict(row)
    edge["source_address"] = str(edge.get("source_address", "")).strip().lower()
    edge["target_address"] = str(edge.get("target_address", "")).strip().lower()
    edge["graph_id"] = graph_id
    edge["block_number"] = parse_int(edge.get("block_number"))
    edge["transfer_value_USD_float"] = parse_float(edge.get("transfer_value_USD"))
    edge["transfer_id"] = edge.get("transfer_id") or stable_transfer_id(edge)
    return edge


def read_csv_dicts(path: Path) -> list[dict[str, Any]]:
    with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_payload(args: argparse.Namespace) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    if args.graph_json:
        payload = json.loads(args.graph_json.read_text(encoding="utf-8"))
        metadata = payload.get("metadata", {})
        graph_id = args.graph_id or metadata.get("graph_id") or f"{metadata.get('source_address', args.graph_json.stem)}_{metadata.get('range_label', '')}".strip("_")
        nodes = payload.get("nodes", [])
        edges = payload.get("edges", [])
    else:
        graph_id = args.graph_id or args.edges_csv.stem.removesuffix("_edges")
        nodes = read_csv_dicts(args.nodes_csv)
        edges = read_csv_dicts(args.edges_csv)
    return graph_id, [normalize_node(node, graph_id) for node in nodes], [normalize_edge(edge, graph_id) for edge in edges]


def batched(rows: list[dict[str, Any]], size: int):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def import_graph(args: argparse.Namespace) -> None:
    graph_id, nodes, edges = load_payload(args)
    with GraphDatabase.driver(args.uri, auth=(args.user, args.password)) as driver:
        driver.verify_connectivity()
        with driver.session(database=args.database) as session:
            session.run(
                "CREATE CONSTRAINT neighborhood_address_unique IF NOT EXISTS "
                "FOR (a:NeighborhoodAddress) REQUIRE (a.graph_id, a.address) IS UNIQUE"
            ).consume()
            session.run(
                "CREATE INDEX neighborhood_transfer_graph IF NOT EXISTS "
                "FOR ()-[r:NEIGHBORHOOD_TRANSFER]-() ON (r.graph_id)"
            ).consume()
            if args.clear_graph:
                session.run(
                    "MATCH (n:NeighborhoodAddress {graph_id: $graph_id}) DETACH DELETE n",
                    graph_id=graph_id,
                ).consume()
            for batch in batched(nodes, args.batch_size):
                session.run(
                    """
                    UNWIND $rows AS row
                    MERGE (n:NeighborhoodAddress {graph_id: row.graph_id, address: row.address})
                    SET n.is_source = row.is_source,
                        n.discovery_depth = row.discovery_depth,
                        n.in_degree = row.in_degree,
                        n.out_degree = row.out_degree,
                        n.total_degree = row.total_degree,
                        n.short_label = CASE WHEN row.is_source THEN 'SOURCE ' + row.short_label ELSE row.short_label END
                    """,
                    rows=batch,
                ).consume()
            for batch in batched(edges, args.batch_size):
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (source:NeighborhoodAddress {graph_id: row.graph_id, address: row.source_address})
                    MATCH (target:NeighborhoodAddress {graph_id: row.graph_id, address: row.target_address})
                    MERGE (source)-[r:NEIGHBORHOOD_TRANSFER {graph_id: row.graph_id, transfer_id: row.transfer_id}]->(target)
                    SET r.source_type = row.source_type,
                        r.block_number = row.block_number,
                        r.transaction_hash = row.transaction_hash,
                        r.token_address = row.token_address,
                        r.token_symbol = row.token_symbol,
                        r.raw_value = row.raw_value,
                        r.decimal = row.decimal,
                        r.median_exchange_rate_USD = row.median_exchange_rate_USD,
                        r.transfer_value_USD = row.transfer_value_USD,
                        r.transfer_value_USD_float = row.transfer_value_USD_float,
                        r.transfer_value_label = row.transfer_value_label,
                        r.csv_file = row.csv_file
                    """,
                    rows=batch,
                ).consume()
    print(f"Imported graph_id={graph_id} nodes={len(nodes)} edges={len(edges)}")
    print("Neo4j Browser query:")
    print(f"MATCH p=(:NeighborhoodAddress {{graph_id: '{graph_id}'}})-[:NEIGHBORHOOD_TRANSFER*1..5]-(:NeighborhoodAddress {{graph_id: '{graph_id}'}}) RETURN p LIMIT 200;")


def main() -> int:
    args = parse_args()
    import_graph(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
