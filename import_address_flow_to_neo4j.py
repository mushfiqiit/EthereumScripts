#!/usr/bin/env python3
"""Import a trace_address_flow_bfs.py graph into Neo4j as a directed graph, and
render an HTML visualization of whatever ends up in Neo4j.

Standalone script - does not import or depend on any other file in this
repository (including AddressTracing/import_indexed_graph_to_neo4j.py).
Requires the official Neo4j Python driver: pip install neo4j

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

Performance and time-boxing:
  Rows are written in batches of --batch-size using the driver's bound
  parameters (UNWIND $rows AS row ...) instead of embedding tens/hundreds of
  thousands of literal values as Cypher text - the latter forces the server
  to parse one enormous statement and run it as one giant transaction, which
  is what made earlier large imports take a very long time. Each batch also
  carries a server-enforced transaction timeout (neo4j.Query(..., timeout=)),
  and the whole run is bounded by a wall-clock --time-budget-minutes (default
  10): once the deadline passes, no further batches are sent.

  Edge relationships are created with MATCH (not MERGE) on both endpoints, so
  if the node phase was cut short by the time budget, edges referencing a
  node that was never written are simply skipped server-side rather than
  creating bare, property-less nodes.

  After writing (whether the run completed or was time-boxed), the HTML
  visualization is built by querying Neo4j itself for whatever currently
  exists under --graph-id - so the HTML always matches the database's real
  state, including a run that was interrupted partway.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    from neo4j import GraphDatabase, Query
    from neo4j.exceptions import Neo4jError
except ImportError:  # pragma: no cover - exercised via the error message below
    GraphDatabase = None
    Query = None
    Neo4jError = Exception

FLOW_COLORS = {
    "root": "#dc2626",
    "upstream": "#2563eb",
    "downstream": "#facc15",
    "both": "#a855f7",
    "connected": "#64748b",
}

NODE_WRITE_CYPHER = """
UNWIND $rows AS row
MERGE (a:Address {graph_id: $gid, address: row.address})
SET a.label = row.label,
    a.distance = row.distance,
    a.is_root = row.is_root,
    a.is_hub = row.is_hub,
    a.total_degree = row.total_degree,
    a.flow_role = row.flow_role,
    a.visual_label = row.visual_label,
    a.color = row.color,
    a.size = row.size
FOREACH (_ IN CASE WHEN row.is_root THEN [1] ELSE [] END | SET a:RootAddress)
FOREACH (_ IN CASE WHEN row.is_root THEN [] ELSE [1] END | SET a:TracedAddress)
FOREACH (_ IN CASE WHEN row.is_hub THEN [1] ELSE [] END | SET a:HubAddress)
"""

# MATCH (not MERGE) on both endpoints: if the node phase was time-boxed before
# reaching one of these addresses, this row silently matches nothing instead
# of creating a bare, property-less node.
EDGE_WRITE_CYPHER = """
UNWIND $rows AS row
MATCH (from:Address {graph_id: $gid, address: row.from_address})
MATCH (to:Address {graph_id: $gid, address: row.to_address})
MERGE (from)-[r:TRANSFER {graph_id: $gid, edge_id: row.edge_id}]->(to)
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
    r.visual_label = row.label
"""

READ_NODES_CYPHER = """
MATCH (n:Address {graph_id: $gid})
RETURN n.address AS address, n.label AS label, n.distance AS distance,
       n.is_root AS is_root, n.is_hub AS is_hub, n.color AS color,
       n.visual_label AS visual_label
"""

READ_EDGES_CYPHER = """
MATCH (a:Address {graph_id: $gid})-[r:TRANSFER {graph_id: $gid}]->(b:Address {graph_id: $gid})
RETURN r.edge_id AS edge_id, a.address AS from_address, b.address AS to_address,
       r.block_number AS block_number, r.label AS label
"""

READ_COUNTS_CYPHER = """
MATCH (n:Address {graph_id: $gid})
OPTIONAL MATCH (:Address {graph_id: $gid})-[r:TRANSFER {graph_id: $gid}]->(:Address {graph_id: $gid})
RETURN count(DISTINCT n) AS node_count, count(DISTINCT r) AS edge_count
"""


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_optional_int(value: Any) -> Optional[int]:
    text = str(value).strip() if value is not None else ""
    if text == "":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def compute_flow_roles(
    source: str, addresses: set[str], edges: list[dict[str, str]]
) -> dict[str, str]:
    """Classify each node by directed reachability from the source: money that
    reaches the source (upstream), money the source sends onward (downstream),
    both, or neither (connected only through some other path)."""
    outgoing: dict[str, set[str]] = {}
    incoming: dict[str, set[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge["from_address"], set()).add(edge["to_address"])
        incoming.setdefault(edge["to_address"], set()).add(edge["from_address"])

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


def coerce_node_row(row: dict[str, str], flow_roles: dict[str, str]) -> dict[str, Any]:
    address = row["address"]
    is_root = parse_bool(row.get("is_root", "false"))
    is_hub = parse_bool(row.get("is_hub", "false"))
    label = row.get("label") or address
    flow_role = flow_roles.get(address, "connected")
    return {
        "address": address,
        "label": label,
        "distance": parse_int(row.get("distance"), 0),
        "is_root": is_root,
        "is_hub": is_hub,
        "total_degree": parse_int(row.get("total_degree"), -1),
        "flow_role": flow_role,
        "color": FLOW_COLORS.get(flow_role, FLOW_COLORS["connected"]),
        "size": 48 if is_root else (44 if is_hub else 40),
        "visual_label": f"ROOT {label}" if is_root else label,
    }


def coerce_edge_row(row: dict[str, str]) -> dict[str, Any]:
    return {
        "edge_id": row["edge_id"],
        "from_address": row["from_address"],
        "to_address": row["to_address"],
        "block_number": parse_int(row.get("block_number"), 0),
        "source": row.get("source", ""),
        "asset_type": row.get("asset_type", ""),
        "token_address": row.get("token_address", ""),
        "raw_value": row.get("raw_value", "0"),
        "decimals": parse_int(row.get("decimals"), 0),
        "normalized_value": row.get("normalized_value", "0"),
        "tx_hash": row.get("tx_hash", ""),
        "log_index": parse_optional_int(row.get("log_index")),
        "token_label": row.get("token_label", ""),
        "label": row.get("label", ""),
    }


class TimeBudget:
    """Wall-clock deadline shared across the whole import run."""

    def __init__(self, budget_seconds: float) -> None:
        self.deadline = time.monotonic() + budget_seconds

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0


def run_write_batches(
    session: Any,
    cypher: str,
    rows: list[dict[str, Any]],
    graph_id: str,
    batch_size: int,
    budget: TimeBudget,
    label: str,
) -> tuple[int, bool]:
    """Send rows in batches until either all rows are sent or the time budget
    runs out. Returns (rows_sent, truncated)."""
    sent = 0
    for batch in chunked(rows, batch_size):
        remaining = budget.remaining()
        if remaining <= 0:
            print(f"[TIME BUDGET] Stopping {label} import - time budget exhausted "
                  f"({sent}/{len(rows)} rows sent).")
            return sent, True
        try:
            session.run(Query(cypher, timeout=remaining), rows=batch, gid=graph_id).consume()
        except Neo4jError as exc:
            print(f"[TIME BUDGET] Stopping {label} import - batch failed or timed out: {exc}")
            return sent, True
        sent += len(batch)
    return sent, False


def read_graph_from_neo4j(session: Any, graph_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Query Neo4j for whatever currently exists under graph_id - this is the
    source of truth for the HTML render, so it's always consistent with the
    database even if the write phase was cut short."""
    node_rows = [dict(r) for r in session.run(READ_NODES_CYPHER, gid=graph_id)]
    edge_rows = [dict(r) for r in session.run(READ_EDGES_CYPHER, gid=graph_id)]
    return node_rows, edge_rows


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

    print("\nHub addresses touched by this trace:")
    print(f"MATCH (n:Address {{graph_id: '{graph}'}}) WHERE n.is_hub = true RETURN n;")

    print("\nToken-address filtering (directed):")
    print(
        f"MATCH p=(a:Address {{graph_id: '{graph}'}})-[r:TRANSFER {{asset_type: 'TOKEN'}}]->"
        f"(b:Address {{graph_id: '{graph}'}}) WHERE r.token_address = '0xtoken...' RETURN p;"
    )


# --- HTML visualization -----------------------------------------------------


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_html_payload(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Prepare positioned graph data for a standalone scrollable HTML file."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for node in nodes:
        grouped.setdefault(_safe_int(node.get("distance")), []).append(node)
    for group in grouped.values():
        group.sort(key=lambda row: (not bool(row.get("is_root")), str(row.get("address", ""))))

    html_nodes: list[dict[str, Any]] = []
    column_width = 260
    row_height = 150
    left = 120
    top = 120
    for distance in sorted(grouped):
        for index, node in enumerate(grouped[distance]):
            address = str(node.get("address", ""))
            html_nodes.append({
                "id": address,
                "label": str(node.get("visual_label") or node.get("label") or address),
                "caption": f"distance {distance}",
                "distance": distance,
                "is_root": bool(node.get("is_root")),
                "is_hub": bool(node.get("is_hub")),
                "color": node.get("color") or FLOW_COLORS["connected"],
                "x": left + distance * column_width,
                "y": top + index * row_height,
            })

    html_edges = [{
        "from": str(edge.get("from_address", "")),
        "to": str(edge.get("to_address", "")),
        "label": str(edge.get("label") or edge.get("edge_id", "transfer")),
        "block_number": _safe_int(edge.get("block_number")),
    } for edge in edges]

    max_distance = max(grouped.keys(), default=0)
    max_rows = max((len(group) for group in grouped.values()), default=1)
    width = max(1400, left * 2 + (max_distance + 1) * column_width)
    height = max(900, top * 2 + max_rows * row_height)
    return html_nodes, html_edges, width, height


def write_html_visualization(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    graph_id: str,
    output_path: Path,
    truncated: bool,
    source_note: str,
) -> None:
    html_nodes, html_edges, width, height = build_html_payload(nodes, edges)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    nodes_json = json.dumps(html_nodes)
    edges_json = json.dumps(html_edges)
    graph_title = json.dumps(f"AddressTracing graph: {graph_id}")
    banner = (
        f'<p class="banner">PARTIAL GRAPH - the Neo4j import was time-boxed and stopped before '
        f"finishing; this shows exactly what's in Neo4j for this graph_id right now "
        f'({len(html_nodes)} nodes, {len(html_edges)} edges), not the full traced graph.</p>'
        if truncated else ""
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AddressTracing Graph</title>
  <style>
    :root {{ font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; overflow: auto; background: #0f172a; color: #f8fafc; }}
    header {{ position: sticky; top: 0; z-index: 10; padding: 14px 18px; border-bottom: 1px solid #334155; background: rgba(15, 23, 42, 0.96); }}
    h1 {{ margin: 0 0 4px; font-size: 19px; }}
    p {{ margin: 0; color: #cbd5e1; }}
    p.banner {{ margin-top: 8px; color: #fca5a5; font-weight: 700; }}
    p.source-note {{ margin-top: 4px; color: #94a3b8; font-size: 12px; }}
    #graph {{ width: {width}px; height: {height}px; display: block; cursor: grab; background: radial-gradient(circle at 24px 24px, rgba(148, 163, 184, 0.18) 2px, transparent 0) 0 0 / 48px 48px, linear-gradient(135deg, #0f172a 0%, #111827 100%); }}
    .edge {{ stroke: #94a3b8; stroke-width: 1.8; marker-end: url(#arrow); opacity: 0.82; }}
    .edge-label {{ fill: #e5e7eb; font-size: 11px; paint-order: stroke; stroke: #0f172a; stroke-width: 4px; stroke-linejoin: round; }}
    .node {{ cursor: move; filter: drop-shadow(0 7px 14px rgba(0, 0, 0, 0.35)); }}
    .node circle {{ stroke: #f8fafc; stroke-width: 2; }}
    .node.hub circle {{ stroke: #fbbf24; stroke-width: 4; }}
    .legend-hub {{ color: #fbbf24; font-weight: 800; }}
    .node text {{ fill: white; font-size: 12px; font-weight: 800; text-anchor: middle; dominant-baseline: middle; pointer-events: none; }}
    .caption {{ fill: #dbeafe; font-size: 11px; text-anchor: middle; pointer-events: none; }}
  </style>
</head>
<body>
  <header>
    <h1 id="title"></h1>
    <p>Standalone scrollable HTML export. Scroll to view the full canvas; drag nodes to rearrange locally.
       <span class="legend-hub">Gold-ringed nodes are hubs (edges capped)</span>.
       Nodes: {len(html_nodes)} | Edges: {len(html_edges)}</p>
    {banner}
    <p class="source-note">{source_note}</p>
  </header>
  <svg id="graph" role="img" aria-label="Scrollable AddressTracing graph">
    <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8"></path></marker></defs>
    <g id="edges"></g><g id="edge-labels"></g><g id="nodes"></g>
  </svg>
  <script>
    const title = {graph_title};
    const nodes = {nodes_json};
    const edges = {edges_json};
    document.getElementById("title").textContent = title;
    const svg = document.getElementById("graph");
    const edgeLayer = document.getElementById("edges");
    const labelLayer = document.getElementById("edge-labels");
    const nodeLayer = document.getElementById("nodes");
    const nodeById = new Map(nodes.map((node) => [node.id, node]));
    let selectedNode = null;
    let pointerOffset = {{ x: 0, y: 0 }};
    function svgPoint(event) {{ const point = svg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY; return point.matrixTransform(svg.getScreenCTM().inverse()); }}
    function render() {{
      edgeLayer.replaceChildren(); labelLayer.replaceChildren(); nodeLayer.replaceChildren();
      for (const edge of edges) {{
        const source = nodeById.get(edge.from); const target = nodeById.get(edge.to); if (!source || !target) continue;
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("class", "edge"); line.setAttribute("x1", source.x); line.setAttribute("y1", source.y); line.setAttribute("x2", target.x); line.setAttribute("y2", target.y); edgeLayer.appendChild(line);
        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("class", "edge-label"); label.setAttribute("x", (source.x + target.x) / 2); label.setAttribute("y", (source.y + target.y) / 2 - 8); label.textContent = edge.label; labelLayer.appendChild(label);
      }}
      for (const node of nodes) {{
        const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
        const classes = ["node"]; if (node.is_hub) classes.push("hub");
        group.setAttribute("class", classes.join(" ")); group.setAttribute("transform", `translate(${{node.x}}, ${{node.y}})`);
        group.addEventListener("pointerdown", (event) => {{ selectedNode = node; const point = svgPoint(event); pointerOffset = {{ x: node.x - point.x, y: node.y - point.y }}; group.setPointerCapture(event.pointerId); }});
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle"); circle.setAttribute("r", node.is_root ? 48 : (node.is_hub ? 44 : 40)); circle.setAttribute("fill", node.color); group.appendChild(circle);
        const text = document.createElementNS("http://www.w3.org/2000/svg", "text"); text.textContent = node.label; group.appendChild(text);
        const caption = document.createElementNS("http://www.w3.org/2000/svg", "text"); caption.setAttribute("class", "caption"); caption.setAttribute("y", node.is_root ? 68 : 60); caption.textContent = node.is_root ? "ROOT" : node.caption; group.appendChild(caption);
        nodeLayer.appendChild(group);
      }}
    }}
    svg.addEventListener("pointermove", (event) => {{ if (!selectedNode) return; const point = svgPoint(event); selectedNode.x = point.x + pointerOffset.x; selectedNode.y = point.y + pointerOffset.y; render(); }});
    svg.addEventListener("pointerup", () => {{ selectedNode = null; }}); svg.addEventListener("pointerleave", () => {{ selectedNode = null; }}); render();
  </script>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")


# --- CLI ---------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Import a trace_address_flow_bfs.py graph into Neo4j as a directed graph, "
        "time-boxed, and render an HTML visualization from whatever ends up in Neo4j."
    )
    p.add_argument("--graph-dir", type=Path, required=True, help="Directory with nodes.csv and edges.csv")
    p.add_argument("--graph-id", required=True, help="Namespace tag stored on every node/relationship")
    p.add_argument("--max-depth", type=int, default=4, help="Max depth used in the printed example queries")
    p.add_argument("--clear-existing", action="store_true", help="Delete existing nodes for this graph_id first")
    p.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    p.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    p.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD", ""))
    p.add_argument("--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    p.add_argument("--dry-run", action="store_true", help="Write nothing to Neo4j; render HTML straight from the CSVs")
    p.add_argument(
        "--time-budget-minutes",
        type=float,
        default=10.0,
        help="Hard wall-clock cap on the whole import run (default: 10 minutes). Once reached, no "
        "further batches are sent and the HTML is rendered from whatever Neo4j already has.",
    )
    p.add_argument("--batch-size", type=int, default=2000, help="Rows per write transaction (default: 2000)")
    p.add_argument("--html-output", type=Path, help="Output HTML path (default: <graph-dir>/graph_visualization.html)")
    p.add_argument("--no-html", action="store_true", help="Skip HTML generation")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dry_run:
        if not args.password:
            raise SystemExit("Provide Neo4j password with --password or NEO4J_PASSWORD")
        if GraphDatabase is None:
            raise SystemExit(
                "The 'neo4j' package is required for a real import (not needed for --dry-run). "
                "Install it with: pip install neo4j"
            )
    if args.time_budget_minutes <= 0:
        raise SystemExit("--time-budget-minutes must be greater than zero")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be at least 1")

    nodes_path = args.graph_dir / "nodes.csv"
    edges_path = args.graph_dir / "edges.csv"
    if not nodes_path.is_file() or not edges_path.is_file():
        raise SystemExit(f"Expected nodes.csv and edges.csv in {args.graph_dir}")

    raw_nodes = load_csv(nodes_path)
    raw_edges = load_csv(edges_path)
    source_rows = [n for n in raw_nodes if parse_bool(n.get("is_root", "false"))]
    source = source_rows[0]["address"] if source_rows else ""
    flow_roles = compute_flow_roles(source, {n["address"] for n in raw_nodes}, raw_edges)

    node_params = [coerce_node_row(n, flow_roles) for n in raw_nodes]
    edge_params = [coerce_edge_row(e) for e in raw_edges]

    html_path = args.html_output or (args.graph_dir / "graph_visualization.html")

    if args.dry_run:
        print(f"[DRY RUN] Would import {len(node_params)} nodes and {len(edge_params)} edges.")
        if not args.no_html:
            write_html_visualization(
                node_params, edge_params, args.graph_id, html_path,
                truncated=False,
                source_note="Dry run: rendered directly from nodes.csv/edges.csv, Neo4j was not touched.",
            )
            print(f"HTML written to: {html_path}")
        print_queries(args.graph_id, args.max_depth)
        return 0

    budget = TimeBudget(args.time_budget_minutes * 60)
    truncated = False
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        with driver.session(database=args.database) as session:
            if args.clear_existing:
                remaining = budget.remaining()
                if remaining > 0:
                    session.run(
                        Query("MATCH (n:Address {graph_id: $gid}) DETACH DELETE n", timeout=remaining),
                        gid=args.graph_id,
                    ).consume()
                else:
                    truncated = True

            if not budget.expired():
                session.run(
                    "CREATE INDEX address_lookup IF NOT EXISTS FOR (a:Address) ON (a.graph_id, a.address)"
                ).consume()
            else:
                truncated = True

            nodes_sent, nodes_truncated = run_write_batches(
                session, NODE_WRITE_CYPHER, node_params, args.graph_id, args.batch_size, budget, "node"
            )
            truncated = truncated or nodes_truncated
            print(f"Node rows submitted: {nodes_sent}/{len(node_params)}")

            edges_sent, edges_truncated = run_write_batches(
                session, EDGE_WRITE_CYPHER, edge_params, args.graph_id, args.batch_size, budget, "edge"
            )
            truncated = truncated or edges_truncated
            print(
                f"Edge rows submitted: {edges_sent}/{len(edge_params)} "
                "(a submitted row is dropped server-side, not an error, if either endpoint "
                "node was never written - see the actual relationship count below)"
            )

            counts = session.run(READ_COUNTS_CYPHER, gid=args.graph_id).single()
            print(f"Neo4j now actually has {counts['node_count']} nodes / {counts['edge_count']} relationships "
                  f"for graph_id={args.graph_id!r}")

            if not args.no_html:
                html_nodes, html_edges = read_graph_from_neo4j(session, args.graph_id)
                source_note = (
                    f"Read live from Neo4j (bolt://..., database={args.database}, "
                    f"graph_id={args.graph_id}) after the import run."
                )
                write_html_visualization(
                    html_nodes, html_edges, args.graph_id, html_path, truncated, source_note
                )
                print(f"HTML written to: {html_path}")
    finally:
        driver.close()

    if truncated:
        print(
            f"\n[TIME BUDGET] Import was time-boxed at {args.time_budget_minutes} minute(s) and did not "
            "finish. Neo4j (and the HTML) reflect a partial graph. Re-run with a larger "
            "--time-budget-minutes to continue, or a smaller --max-depth/graph to fit the budget."
        )

    print_queries(args.graph_id, args.max_depth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
