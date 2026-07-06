#!/usr/bin/env python3
"""Import an AddressTracing graph into Neo4j using cypher-shell."""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cypher_literal(value: Any) -> str:
    return json.dumps(value)


def map_literal(row: dict[str, Any]) -> str:
    parts = []
    for key, value in row.items():
        if key in {"distance", "block_number", "decimals"}:
            try:
                parts.append(f"{key}: {int(value)}")
            except Exception:
                parts.append(f"{key}: null")
        elif key == "is_root":
            parts.append(f"{key}: {str(parse_bool(str(value))).lower()}")
        else:
            parts.append(f"{key}: {cypher_literal(value)}")
    return "{" + ", ".join(parts) + "}"


def build_cypher(graph_id: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], clear_existing: bool) -> str:
    graph = cypher_literal(graph_id)
    node_maps = "[" + ",\n".join(map_literal(n) for n in nodes) + "]"
    edge_maps = "[" + ",\n".join(map_literal(e) for e in edges) + "]"
    statements = []
    if clear_existing:
        statements.append(f"MATCH (n:Address {{graph_id: {graph}}}) DETACH DELETE n;")
    statements.append(
        f"""
UNWIND {node_maps} AS row
MERGE (a:Address {{graph_id: {graph}, address: row.address}})
SET a.display_label = row.display_label,
    a.distance = row.distance,
    a.is_root = row.is_root,
    a.visual_label = CASE WHEN row.is_root THEN 'ROOT ' + row.display_label ELSE row.display_label END
FOREACH (_ IN CASE WHEN row.is_root THEN [1] ELSE [] END | SET a:RootAddress)
FOREACH (_ IN CASE WHEN row.is_root THEN [] ELSE [1] END | SET a:TracedAddress);
""".strip()
    )
    statements.append(
        f"""
UNWIND {edge_maps} AS row
MATCH (from:Address {{graph_id: {graph}, address: row.from_address}})
MATCH (to:Address {{graph_id: {graph}, address: row.to_address}})
CREATE (from)-[:TRANSFER {{
  graph_id: {graph},
  edge_id: row.edge_id,
  source: row.source,
  asset_type: row.asset_type,
  token_address: row.token_address,
  raw_value: row.raw_value,
  decimals: row.decimals,
  normalized_amount: row.normalized_amount,
  block_number: row.block_number,
  transaction_hash: row.transaction_hash,
  log_index: row.log_index,
  display_label: row.display_label,
  visual_label: row.display_label
}}]->(to);
""".strip()
    )
    statements.append(
        f"""
MATCH (n:Address {{graph_id: {graph}}})
OPTIONAL MATCH (:Address {{graph_id: {graph}}})-[r:TRANSFER {{graph_id: {graph}}}]->(:Address {{graph_id: {graph}}})
RETURN count(DISTINCT n) AS imported_nodes, count(DISTINCT r) AS imported_edges;
""".strip()
    )
    return "\n\n".join(statements) + "\n"


def run_cypher_shell(uri: str, user: str, password: str, database: str, cypher: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".cypher", delete=False, encoding="utf-8") as f:
        f.write(cypher)
        path = f.name
    try:
        subprocess.run(["cypher-shell", "-a", uri, "-u", user, "-p", password, "-d", database, "-f", path], check=True)
    finally:
        Path(path).unlink(missing_ok=True)


def print_queries(graph_id: str) -> None:
    graph = graph_id.replace("'", "\\'")
    print("\nNeo4j Browser URL: http://localhost:7474")
    print("\nRoot-centered visualization query:")
    print(f"MATCH p=(root:Address {{graph_id: '{graph}', is_root: true}})-[:TRANSFER*1..3]-(n:Address {{graph_id: '{graph}'}}) RETURN p;")
    print("\nIncoming transfers to root:")
    print(f"MATCH (src:Address {{graph_id: '{graph}'}})-[r:TRANSFER]->(root:Address {{graph_id: '{graph}', is_root: true}}) RETURN src,r,root ORDER BY r.block_number;")
    print("\nOutgoing transfers from root:")
    print(f"MATCH (root:Address {{graph_id: '{graph}', is_root: true}})-[r:TRANSFER]->(dst:Address {{graph_id: '{graph}'}}) RETURN root,r,dst ORDER BY r.block_number;")
    print("\nBlock-number filtering:")
    print(f"MATCH p=(:Address {{graph_id: '{graph}'}})-[r:TRANSFER]->(:Address {{graph_id: '{graph}'}}) WHERE r.block_number >= 25415601 AND r.block_number <= 25444400 RETURN p;")
    print("\nToken-address filtering:")
    print(f"MATCH p=(:Address {{graph_id: '{graph}'}})-[r:TRANSFER {{asset_type: 'TOKEN'}}]->(:Address {{graph_id: '{graph}'}}) WHERE r.token_address = '0xtoken...' RETURN p;")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import AddressTracing output files into Neo4j.")
    p.add_argument("--graph-dir", type=Path, required=True)
    p.add_argument("--graph-id", required=True)
    p.add_argument("--clear-existing", action="store_true")
    p.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    p.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    p.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD", ""))
    p.add_argument("--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    p.add_argument("--dry-run", action="store_true", help="Write no data; print the generated Cypher")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.password and not args.dry_run:
        raise SystemExit("Provide Neo4j password with --password or NEO4J_PASSWORD")
    nodes_path = args.graph_dir / "nodes.csv"
    edges_path = args.graph_dir / "edges.csv"
    if not nodes_path.is_file() or not edges_path.is_file():
        raise SystemExit(f"Expected nodes.csv and edges.csv in {args.graph_dir}")
    nodes = load_csv(nodes_path)
    edges = load_csv(edges_path)
    cypher = build_cypher(args.graph_id, nodes, edges, args.clear_existing)
    if args.dry_run:
        print(cypher)
    else:
        run_cypher_shell(args.uri, args.user, args.password, args.database, cypher)
    print(f"Imported node count: {len(nodes)}")
    print(f"Imported edge count: {len(edges)}")
    print_queries(args.graph_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
