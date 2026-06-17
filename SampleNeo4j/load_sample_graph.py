#!/usr/bin/env python3
"""Create a small sample graph in Neo4j.

The sample graph is intentionally unrelated to Ethereum data. It models a few
people, tools, and documents so you can quickly verify that Neo4j accepts writes
and relationships on your computer.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from neo4j import GraphDatabase

SAMPLE_NODES: list[dict[str, Any]] = [
    {"id": "alice", "label": "Person", "name": "Alice", "role": "Developer"},
    {"id": "bob", "label": "Person", "name": "Bob", "role": "Data analyst"},
    {"id": "carol", "label": "Person", "name": "Carol", "role": "Reviewer"},
    {"id": "neo4j", "label": "Tool", "name": "Neo4j", "role": "Graph database"},
    {"id": "python", "label": "Tool", "name": "Python", "role": "Scripting language"},
    {"id": "demo", "label": "Document", "name": "Demo graph", "role": "Test artifact"},
]

SAMPLE_RELATIONSHIPS: list[dict[str, Any]] = [
    {"source": "alice", "target": "neo4j", "type": "TESTS", "description": "loads sample data into"},
    {"source": "alice", "target": "python", "type": "USES", "description": "runs scripts with"},
    {"source": "bob", "target": "neo4j", "type": "QUERIES", "description": "explores graph data in"},
    {"source": "carol", "target": "demo", "type": "REVIEWS", "description": "checks the result of"},
    {"source": "python", "target": "demo", "type": "GENERATES", "description": "exports"},
    {"source": "demo", "target": "neo4j", "type": "VISUALIZES", "description": "shows data from"},
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load a small sample graph into Neo4j.")
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD", "password123"))
    parser.add_argument("--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Do not delete previous SampleDemo nodes before loading this graph.",
    )
    return parser.parse_args()


def load_graph(uri: str, user: str, password: str, database: str, keep_existing: bool) -> None:
    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            if not keep_existing:
                session.run("MATCH (n:SampleDemo) DETACH DELETE n").consume()

            session.run(
                """
                UNWIND $nodes AS node
                MERGE (n:SampleDemo {id: node.id})
                SET n.name = node.name,
                    n.role = node.role,
                    n.kind = node.label,
                    n:`SampleDemo`
                """,
                nodes=SAMPLE_NODES,
            ).consume()

            for label in {node["label"] for node in SAMPLE_NODES}:
                ids = [node["id"] for node in SAMPLE_NODES if node["label"] == label]
                session.run(
                    f"MATCH (n:SampleDemo) WHERE n.id IN $ids SET n:{label}",
                    ids=ids,
                ).consume()

            for relationship in SAMPLE_RELATIONSHIPS:
                session.run(
                    f"""
                    MATCH (source:SampleDemo {{id: $source}})
                    MATCH (target:SampleDemo {{id: $target}})
                    MERGE (source)-[r:{relationship['type']}]->(target)
                    SET r.description = $description
                    """,
                    source=relationship["source"],
                    target=relationship["target"],
                    description=relationship["description"],
                ).consume()

            counts = session.run(
                """
                MATCH (n:SampleDemo)
                OPTIONAL MATCH (n)-[r]->(:SampleDemo)
                RETURN count(DISTINCT n) AS nodes, count(r) AS relationships
                """
            ).single()
            print(
                f"Loaded sample graph: {counts['nodes']} nodes and "
                f"{counts['relationships']} relationships."
            )


if __name__ == "__main__":
    args = parse_args()
    load_graph(args.uri, args.user, args.password, args.database, args.keep_existing)
