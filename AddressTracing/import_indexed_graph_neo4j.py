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
    a.visual_label = CASE WHEN row.is_root THEN 'ROOT ' + row.display_label ELSE row.display_label END,
    a.color = CASE WHEN row.is_root THEN '#dc2626' ELSE '#2563eb' END,
    a.size = CASE WHEN row.is_root THEN 48 ELSE 40 END
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




def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def build_html_payload(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Prepare positioned graph data for a standalone scrollable HTML file."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for node in nodes:
        grouped.setdefault(_safe_int(node.get("distance")), []).append(node)
    for group in grouped.values():
        group.sort(key=lambda row: (str(row.get("is_root", "false")).lower() != "true", str(row.get("address", ""))))

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
                "label": str(node.get("display_label") or address),
                "caption": f"distance {distance}",
                "distance": distance,
                "is_root": str(node.get("is_root", "false")).lower() == "true",
                "x": left + distance * column_width,
                "y": top + index * row_height,
            })

    html_edges = [{
        "from": str(edge.get("from_address", "")),
        "to": str(edge.get("to_address", "")),
        "label": str(edge.get("display_label") or edge.get("edge_id", "transfer")),
        "source": str(edge.get("source", "")),
        "block_number": _safe_int(edge.get("block_number")),
    } for edge in edges]

    max_distance = max(grouped.keys(), default=0)
    max_rows = max((len(group) for group in grouped.values()), default=1)
    width = max(1400, left * 2 + (max_distance + 1) * column_width)
    height = max(900, top * 2 + max_rows * row_height)
    return html_nodes, html_edges, width, height


def write_html_visualization(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], graph_id: str, output_path: Path) -> None:
    html_nodes, html_edges, width, height = build_html_payload(nodes, edges)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    nodes_json = json.dumps(html_nodes)
    edges_json = json.dumps(html_edges)
    graph_title = json.dumps(f"AddressTracing graph: {graph_id}")
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
    #graph {{ width: {width}px; height: {height}px; display: block; cursor: grab; background: radial-gradient(circle at 24px 24px, rgba(148, 163, 184, 0.18) 2px, transparent 0) 0 0 / 48px 48px, linear-gradient(135deg, #0f172a 0%, #111827 100%); }}
    .edge {{ stroke: #94a3b8; stroke-width: 1.8; marker-end: url(#arrow); opacity: 0.82; }}
    .edge-label {{ fill: #e5e7eb; font-size: 11px; paint-order: stroke; stroke: #0f172a; stroke-width: 4px; stroke-linejoin: round; }}
    .node {{ cursor: move; filter: drop-shadow(0 7px 14px rgba(0, 0, 0, 0.35)); }}
    .node circle {{ stroke: #f8fafc; stroke-width: 2; }}
    .node.root circle {{ fill: #dc2626; }}
    .node.address circle {{ fill: #2563eb; }}
    .legend-root {{ color: #fca5a5; font-weight: 800; }}
    .legend-address {{ color: #93c5fd; font-weight: 800; }}
    .node text {{ fill: white; font-size: 12px; font-weight: 800; text-anchor: middle; dominant-baseline: middle; pointer-events: none; }}
    .caption {{ fill: #dbeafe; font-size: 11px; text-anchor: middle; pointer-events: none; }}
  </style>
</head>
<body>
  <header>
    <h1 id="title"></h1>
    <p>Standalone scrollable HTML export. Use browser scrollbars to reach the full canvas; drag nodes to rearrange locally. <span class="legend-root">Root/source node is red</span>; <span class="legend-address">other nodes are blue</span>. Nodes: {len(html_nodes)} | Edges: {len(html_edges)}</p>
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
        group.setAttribute("class", `node ${{node.is_root ? "root" : "address"}}`); group.setAttribute("transform", `translate(${{node.x}}, ${{node.y}})`);
        group.addEventListener("pointerdown", (event) => {{ selectedNode = node; const point = svgPoint(event); pointerOffset = {{ x: node.x - point.x, y: node.y - point.y }}; group.setPointerCapture(event.pointerId); }});
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle"); circle.setAttribute("r", node.is_root ? 48 : 40); group.appendChild(circle);
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
    p.add_argument("--html-output", type=Path, help="Standalone scrollable HTML graph path (default: <graph-dir>/graph_visualization.html)")
    p.add_argument("--no-html", action="store_true", help="Skip standalone HTML graph generation")
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
    if not args.no_html:
        html_output = args.html_output or (args.graph_dir / "graph_visualization.html")
        write_html_visualization(nodes, edges, args.graph_id, html_output)
        print(f"Standalone HTML graph: {html_output}")
    print(f"Imported node count: {len(nodes)}")
    print(f"Imported edge count: {len(edges)}")
    print_queries(args.graph_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
