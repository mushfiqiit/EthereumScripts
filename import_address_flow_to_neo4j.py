#!/usr/bin/env python3
"""Import a trace_address_flow_bfs.py graph into Neo4j as a directed graph.

Standalone script - does not import or depend on any other file in this
repository (including AddressTracing/import_indexed_graph_to_neo4j.py).

Reads nodes.csv and edges.csv written by trace_address_flow_bfs.py and loads
them into Neo4j with:

  - one :Address node per address (keyed by graph_id + address)
  - one directed (:Address)-[:TRANSFER]->(:Address) relationship per edge,
    pointing from_address -> to_address, matching the real direction the
    value moved on-chain

Directedness is enforced in two places:
  1. Every relationship is created with -> (never a plain undirected -),
     so Neo4j always renders an arrowhead pointing the way the money moved.
  2. The suggested Cypher queries this script prints use directed
     variable-length patterns (-[:TRANSFER*1..d]->), not undirected ones, so
     a multi-hop match follows a single forward chain of transfers (e.g.
     A->B->C->D) instead of zig-zagging back and forth across edges that
     happen to touch the same nodes in either direction.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def cypher_literal(value: Any) -> str:
    return json.dumps(value)


INT_FIELDS = {"distance", "block_number", "decimals"}
BOOL_FIELDS = {"is_root"}


def map_literal(row: dict[str, Any]) -> str:
    parts = []
    for key, value in row.items():
        if key in INT_FIELDS:
            try:
                parts.append(f"{key}: {int(value)}")
            except (TypeError, ValueError):
                parts.append(f"{key}: null")
        elif key in BOOL_FIELDS:
            parts.append(f"{key}: {str(parse_bool(value)).lower()}")
        else:
            parts.append(f"{key}: {cypher_literal(value)}")
    return "{" + ", ".join(parts) + "}"


def compute_flow_roles(
    source: str, addresses: set[str], edges: list[dict[str, Any]]
) -> dict[str, str]:
    """Classify each node by directed reachability from the source: money that
    reaches the source (upstream), money the source sends onward (downstream),
    both, or neither (connected only through some other path)."""
    outgoing: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        outgoing[edge["from_address"]].add(edge["to_address"])
        incoming[edge["to_address"]].add(edge["from_address"])

    def walk(adjacency: dict[str, set[str]]) -> set[str]:
        seen: set[str] = set()
        stack = list(adjacency.get(source, set()))
        while stack:
            addr = stack.pop()
            if addr in seen or addr == source:
                continue
            seen.add(addr)
            stack.extend(adjacency.get(addr, set()) - seen)
        return seen

    downstream = walk(outgoing)  # source -> ... -> node (money the source sent out)
    upstream = walk(incoming)  # node -> ... -> source (money that reached the source)

    roles: dict[str, str] = {}
    for addr in addresses:
        if addr == source:
            roles[addr] = "root"
        elif addr in upstream and addr in downstream:
            roles[addr] = "both"
        elif addr in upstream:
            roles[addr] = "upstream"
        elif addr in downstream:
            roles[addr] = "downstream"
        else:
            roles[addr] = "connected"
    return roles


def build_cypher(
    graph_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    clear_existing: bool,
) -> str:
    graph = cypher_literal(graph_id)

    source_rows = [n for n in nodes if parse_bool(n.get("is_root", "false"))]
    source = source_rows[0]["address"] if source_rows else ""
    flow_roles = compute_flow_roles(source, {n["address"] for n in nodes}, edges)

    node_maps_rows = []
    for n in nodes:
        row = dict(n)
        row["flow_role"] = flow_roles.get(n["address"], "connected")
        node_maps_rows.append(row)
    node_maps = "[" + ",\n".join(map_literal(r) for r in node_maps_rows) + "]"
    edge_maps = "[" + ",\n".join(map_literal(e) for e in edges) + "]"

    statements = []
    if clear_existing:
        statements.append(f"MATCH (n:Address {{graph_id: {graph}}}) DETACH DELETE n;")

    statements.append(
        f"CREATE INDEX address_lookup IF NOT EXISTS FOR (a:Address) ON (a.graph_id, a.address);"
    )

    statements.append(
        f"""
UNWIND {node_maps} AS row
MERGE (a:Address {{graph_id: {graph}, address: row.address}})
SET a.label = row.label,
    a.distance = row.distance,
    a.is_root = row.is_root,
    a.flow_role = row.flow_role,
    a.visual_label = CASE WHEN row.is_root THEN 'ROOT ' + row.label ELSE row.label END,
    a.color = CASE row.flow_role
        WHEN 'root' THEN '#dc2626'
        WHEN 'upstream' THEN '#2563eb'
        WHEN 'downstream' THEN '#facc15'
        WHEN 'both' THEN '#a855f7'
        ELSE '#64748b'
    END,
    a.size = CASE WHEN row.is_root THEN 48 ELSE 40 END
FOREACH (_ IN CASE WHEN row.is_root THEN [1] ELSE [] END | SET a:RootAddress)
FOREACH (_ IN CASE WHEN row.is_root THEN [] ELSE [1] END | SET a:TracedAddress);
""".strip()
    )

    # Directed relationship: always (from)-[:TRANSFER]->(to), never undirected.
    statements.append(
        f"""
UNWIND {edge_maps} AS row
MERGE (from:Address {{graph_id: {graph}, address: row.from_address}})
MERGE (to:Address {{graph_id: {graph}, address: row.to_address}})
MERGE (from)-[r:TRANSFER {{graph_id: {graph}, edge_id: row.edge_id}}]->(to)
SET r.block_number = row.block_number,
    r.source = row.source,
    r.asset_type = row.asset_type,
    r.token_address = row.token_address,
    r.raw_value = row.raw_value,
    r.decimals = row.decimals,
    r.normalized_value = row.normalized_value,
    r.tx_hash = row.tx_hash,
    r.log_index = row.log_index,
    r.token_label = row.token_label,
    r.label = row.label,
    r.visual_label = row.label;
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


def print_queries(graph_id: str, max_depth: int) -> None:
    graph = graph_id.replace("'", "\\'")
    print("\nNeo4j Browser URL: http://localhost:7474")

    print("\nEverything (all directed transfers in this graph):")
    print(
        f"MATCH p=(a:Address {{graph_id: '{graph}'}})-[:TRANSFER]->(b:Address {{graph_id: '{graph}'}}) "
        "RETURN p;"
    )

    print("\nMoney flowing INTO the root, 1 hop (direct senders):")
    print(
        f"MATCH p=(sender:Address {{graph_id: '{graph}'}})-[r:TRANSFER]->(root:Address "
        f"{{graph_id: '{graph}', is_root: true}}) RETURN p ORDER BY r.block_number;"
    )

    print("\nMoney flowing OUT of the root, 1 hop (direct recipients):")
    print(
        f"MATCH p=(root:Address {{graph_id: '{graph}', is_root: true}})-[r:TRANSFER]->"
        f"(recipient:Address {{graph_id: '{graph}'}}) RETURN p ORDER BY r.block_number;"
    )

    print(f"\nMoney flowing INTO the root, up to {max_depth} hops (directed, follows a single forward chain):")
    print(
        f"MATCH p=(sender:Address {{graph_id: '{graph}'}})-[:TRANSFER*1..{max_depth}]->"
        f"(root:Address {{graph_id: '{graph}', is_root: true}}) RETURN p;"
    )

    print(f"\nMoney flowing OUT of the root, up to {max_depth} hops (directed, follows a single forward chain):")
    print(
        f"MATCH p=(root:Address {{graph_id: '{graph}', is_root: true}})-[:TRANSFER*1..{max_depth}]->"
        f"(recipient:Address {{graph_id: '{graph}'}}) RETURN p;"
    )

    print("\nToken-address filtering (directed):")
    print(
        f"MATCH p=(a:Address {{graph_id: '{graph}'}})-[r:TRANSFER {{asset_type: 'TOKEN'}}]->"
        f"(b:Address {{graph_id: '{graph}'}}) WHERE r.token_address = '0xtoken...' RETURN p;"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Import a trace_address_flow_bfs.py graph into Neo4j as a directed graph."
    )
    p.add_argument("--graph-dir", type=Path, required=True, help="Directory with nodes.csv and edges.csv")
    p.add_argument("--graph-id", required=True, help="Namespace tag stored on every node/relationship")
    p.add_argument("--max-depth", type=int, default=4, help="Max depth used in the printed example queries")
    p.add_argument("--clear-existing", action="store_true", help="Delete existing nodes for this graph_id first")
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
    print_queries(args.graph_id, args.max_depth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
