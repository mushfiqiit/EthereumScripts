#!/usr/bin/env python3
"""Export the sample Neo4j graph to a small interactive HTML visualization."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from neo4j import GraphDatabase
from pyvis.network import Network

GROUP_COLORS = {
    "Person": "#6baed6",
    "Tool": "#74c476",
    "Document": "#fd8d3c",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export SampleDemo Neo4j data to HTML.")
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD", "password123"))
    parser.add_argument("--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("sample_neo4j_graph.html"))
    return parser.parse_args()


def fetch_graph(uri: str, user: str, password: str, database: str) -> tuple[list[dict], list[dict]]:
    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            nodes = [
                dict(record["n"])
                for record in session.run(
                    "MATCH (n:SampleDemo) RETURN n ORDER BY n.id"
                )
            ]
            relationships = [
                {
                    "source": record["source_id"],
                    "target": record["target_id"],
                    "type": record["type"],
                    "description": record["description"],
                }
                for record in session.run(
                    """
                    MATCH (source:SampleDemo)-[r]->(target:SampleDemo)
                    RETURN source.id AS source_id,
                           target.id AS target_id,
                           type(r) AS type,
                           r.description AS description
                    ORDER BY source_id, target_id, type
                    """
                )
            ]
    return nodes, relationships


def export_html(nodes: list[dict], relationships: list[dict], output: Path) -> None:
    if not nodes:
        raise SystemExit("No SampleDemo nodes found. Run load_sample_graph.py first.")

    network = Network(height="750px", width="100%", directed=True, notebook=False)
    network.force_atlas_2based(gravity=-30, central_gravity=0.01, spring_length=130)

    for node in nodes:
        kind = node.get("kind", "Unknown")
        title = f"{node.get('name')}<br>{kind}: {node.get('role')}"
        network.add_node(
            node["id"],
            label=node.get("name", node["id"]),
            title=title,
            group=kind,
            color=GROUP_COLORS.get(kind, "#bdbdbd"),
        )

    for relationship in relationships:
        label = relationship["type"]
        title = relationship.get("description") or label
        network.add_edge(
            relationship["source"],
            relationship["target"],
            label=label,
            title=title,
            arrows="to",
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    network.write_html(str(output), notebook=False, open_browser=False)
    print(f"Wrote {output}")


if __name__ == "__main__":
    args = parse_args()
    graph_nodes, graph_relationships = fetch_graph(
        args.uri, args.user, args.password, args.database
    )
    export_html(graph_nodes, graph_relationships, args.output)
