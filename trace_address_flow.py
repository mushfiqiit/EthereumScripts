#!/usr/bin/env python3
"""
Backward Ethereum address flow tracer.

This script performs backward flow tracing.
Starting from a target address, it finds incoming transfers where the target is in
the "to" field. For each incoming transfer, it extracts the "from" address and
recursively traces where that source address previously received funds/tokens.
The result is a directed graph showing historical value flow paths.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Optional

import networkx as nx
import plotly.graph_objects as go

ETH_ROOT_RE = re.compile(r"^Ethereum_TT_(\d+)_(\d+)$")
INNER_RE = re.compile(r"^Transaction_TokenTransfer_(\d+)_(\d+)$")
DB_RE = re.compile(r"^address_block_index_(\d+)_(\d+)\.sqlite$")

BLOCK_CANDIDATES = ("block_number", "blocknumber", "block")
FROM_CANDIDATES = ("from_address", "from")
TO_CANDIDATES = ("to_address", "to")
TX_HASH_CANDIDATES = ("hash", "transaction_hash", "tx_hash")
TOKEN_CANDIDATES = ("token_address", "contract_address")
VALUE_CANDIDATES = ("value", "amount")


@dataclass
class Occurrence:
    db_path: Path
    block_number: int
    source: str


class TraceContext:
    def __init__(self, data_root: Path, max_branches_per_address: int, max_depth: int, source: str):
        self.data_root = data_root
        self.max_branches = max_branches_per_address
        self.max_depth = max_depth
        self.source_filter = source

        self.edge_rows: list[dict[str, Any]] = []
        self.seen_edges: set[tuple[Any, ...]] = set()
        self.visited_states: set[tuple[str, int, int]] = set()

        self.folder_index = self._build_folder_index(data_root)
        self.csv_path_cache: dict[tuple[int, str], Optional[Path]] = {}

    def _build_folder_index(self, data_root: Path) -> list[tuple[int, int, list[tuple[int, int, Path]]]]:
        roots = []
        for p in data_root.iterdir():
            if not p.is_dir():
                continue
            m = ETH_ROOT_RE.match(p.name)
            if not m:
                continue
            rs, re_ = int(m.group(1)), int(m.group(2))
            inner = []
            for sp in p.iterdir():
                if not sp.is_dir():
                    continue
                sm = INNER_RE.match(sp.name)
                if not sm:
                    continue
                ss, se = int(sm.group(1)), int(sm.group(2))
                inner.append((ss, se, sp))
            inner.sort(key=lambda x: x[0])
            roots.append((rs, re_, inner))
        roots.sort(key=lambda x: x[0])
        return roots


def normalize_address(address: Any) -> Optional[str]:
    if address is None:
        return None
    a = str(address).strip().lower()
    if not a or a in {"nan", "null", "none"}:
        return None
    if not a.startswith("0x"):
        return None
    if len(a) < 4:
        return None
    return a


def is_valid_address(address: Any) -> bool:
    return normalize_address(address) is not None


def parse_range_from_name(name: str, prefix: str) -> Optional[tuple[int, int]]:
    m = re.match(rf"^{re.escape(prefix)}_(\d+)_(\d+)$", name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def load_index_databases(index_dir: Path) -> list[Path]:
    dbs = [p for p in index_dir.glob("address_block_index_*.sqlite") if p.is_file()]

    def key(p: Path):
        m = DB_RE.match(p.name)
        if not m:
            return (10**18, 10**18)
        return (int(m.group(1)), int(m.group(2)))

    return sorted(dbs, key=key)


def query_incoming_occurrences(index_dbs: list[Path], address: str, cutoff_block: int, source_filter: str) -> list[Occurrence]:
    out: list[Occurrence] = []
    for db in index_dbs:
        if not db.exists():
            print(f"[WARN] Missing DB: {db}")
            continue
        try:
            conn = sqlite3.connect(db)
            if source_filter == "all":
                q = """
                SELECT block_number, source
                FROM occurrences
                WHERE address = ? AND role = 'to' AND block_number <= ?
                ORDER BY block_number DESC
                """
                rows = conn.execute(q, (address, cutoff_block)).fetchall()
            else:
                q = """
                SELECT block_number, source
                FROM occurrences
                WHERE address = ? AND role = 'to' AND block_number <= ? AND source = ?
                ORDER BY block_number DESC
                """
                rows = conn.execute(q, (address, cutoff_block, source_filter)).fetchall()
            for b, s in rows:
                out.append(Occurrence(db_path=db, block_number=int(b), source=str(s)))
        except sqlite3.Error as exc:
            print(f"[WARN] Could not query {db}: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    out.sort(key=lambda x: x.block_number, reverse=True)
    return out


def find_csv_file(ctx: TraceContext, block_number: int, source: str) -> Optional[Path]:
    key = (block_number, source)
    if key in ctx.csv_path_cache:
        return ctx.csv_path_cache[key]

    filename = f"transaction_{block_number}.csv" if source == "transaction" else f"token_transfer_{block_number}.csv"

    for rs, re_, inner in ctx.folder_index:
        if rs <= block_number <= re_:
            for ss, se, path in inner:
                if ss <= block_number <= se:
                    p = path / filename
                    if p.exists():
                        ctx.csv_path_cache[key] = p
                        return p
    ctx.csv_path_cache[key] = None
    return None


def detect_column(fieldnames: list[str], candidates: tuple[str, ...]) -> Optional[str]:
    norm = {c.strip().lower(): c for c in fieldnames if c}
    for c in candidates:
        if c in norm:
            return norm[c]
    return None


def scan_csv_for_incoming_rows(csv_path: Path, target_address: str, block_number: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                print(f"[WARN] Missing headers: {csv_path}")
                return rows
            to_col = detect_column(reader.fieldnames, TO_CANDIDATES)
            from_col = detect_column(reader.fieldnames, FROM_CANDIDATES)
            block_col = detect_column(reader.fieldnames, BLOCK_CANDIDATES)
            tx_col = detect_column(reader.fieldnames, TX_HASH_CANDIDATES)
            tok_col = detect_column(reader.fieldnames, TOKEN_CANDIDATES)
            val_col = detect_column(reader.fieldnames, VALUE_CANDIDATES)
            if not to_col or not from_col:
                print(f"[WARN] Missing to/from cols in {csv_path}")
                return rows

            for i, row in enumerate(reader, start=2):
                try:
                    to_addr = normalize_address(row.get(to_col))
                    if to_addr != target_address:
                        continue
                    row_block = block_number
                    if block_col:
                        try:
                            row_block = int(str(row.get(block_col, block_number)).strip())
                        except Exception:
                            row_block = block_number
                    from_addr = normalize_address(row.get(from_col))
                    if not from_addr:
                        continue
                    rows.append(
                        {
                            "from_address": from_addr,
                            "to_address": target_address,
                            "block_number": row_block,
                            "csv_row_number": i,
                            "transaction_hash": row.get(tx_col) if tx_col else None,
                            "token_address": row.get(tok_col) if tok_col else None,
                            "value": row.get(val_col) if val_col else None,
                        }
                    )
                except Exception:
                    continue
    except FileNotFoundError:
        print(f"[WARN] CSV file missing: {csv_path}")
    except Exception as exc:
        print(f"[WARN] Failed reading CSV {csv_path}: {exc}")
    return rows


def add_edge(ctx: TraceContext, edge: dict[str, Any]) -> bool:
    key = (
        edge["from_address"],
        edge["to_address"],
        edge["block_number"],
        edge["source"],
        edge.get("transaction_hash") or "",
        edge.get("csv_row_number") or -1,
    )
    if key in ctx.seen_edges:
        return False
    ctx.seen_edges.add(key)
    ctx.edge_rows.append(edge)
    return True


def trace_address(ctx: TraceContext, index_dbs: list[Path], address: str, cutoff_block: int, depth: int) -> None:
    state = (address, cutoff_block, depth)
    if state in ctx.visited_states:
        return
    ctx.visited_states.add(state)

    print(f"[TRACE] address={address} depth={depth} cutoff={cutoff_block}")
    if depth >= ctx.max_depth:
        print("  stop: max depth reached")
        return

    candidates = query_incoming_occurrences(index_dbs, address, cutoff_block, ctx.source_filter)
    print(f"  found {len(candidates)} candidate index entries")
    if not candidates:
        print("  stop: no incoming source found")
        return

    expanded = 0
    for occ in candidates:
        if expanded >= ctx.max_branches:
            print(f"  stop: max branches per address ({ctx.max_branches}) reached")
            break
        csv_path = find_csv_file(ctx, occ.block_number, occ.source)
        if not csv_path:
            print(f"  [WARN] Could not locate CSV for block={occ.block_number}, source={occ.source}")
            continue
        print(f"  file: {csv_path}")

        matches = scan_csv_for_incoming_rows(csv_path, address, occ.block_number)
        print(f"  matching rows: {len(matches)}")
        for m in matches:
            if m["block_number"] > cutoff_block:
                continue
            edge = {
                **m,
                "source": occ.source,
                "csv_file_path": str(csv_path),
            }
            if add_edge(ctx, edge):
                print(f"  added edge {edge['from_address']} -> {edge['to_address']} @ {edge['block_number']}")
                expanded += 1
                trace_address(ctx, index_dbs, edge["from_address"], edge["block_number"], depth + 1)
                if expanded >= ctx.max_branches:
                    break


def write_edges_csv(path: Path, edges: list[dict[str, Any]]) -> None:
    fields = ["from_address", "to_address", "block_number", "source", "csv_file_path", "csv_row_number", "transaction_hash", "token_address", "value"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for e in edges:
            w.writerow({k: e.get(k) for k in fields})


def write_nodes_csv(path: Path, edges: list[dict[str, Any]]) -> None:
    incoming = defaultdict(int)
    outgoing = defaultdict(int)
    nodes = set()
    for e in edges:
        f, t = e["from_address"], e["to_address"]
        nodes.add(f); nodes.add(t)
        outgoing[f] += 1
        incoming[t] += 1
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["address", "incoming_edges", "outgoing_edges"])
        w.writeheader()
        for n in sorted(nodes):
            w.writerow({"address": n, "incoming_edges": incoming[n], "outgoing_edges": outgoing[n]})


def write_json(path: Path, start_address: str, edges: list[dict[str, Any]], args: argparse.Namespace) -> None:
    obj = {
        "start_address": start_address,
        "parameters": {
            "max_depth": args.max_depth,
            "max_branches_per_address": args.max_branches_per_address,
            "cutoff_block": args.cutoff_block,
            "source": args.source,
        },
        "edge_count": len(edges),
        "edges": edges,
    }
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def build_plotly_graph(path: Path, edges: list[dict[str, Any]]) -> None:
    g = nx.DiGraph()
    for e in edges:
        g.add_edge(e["from_address"], e["to_address"], **e)
    if g.number_of_nodes() == 0:
        fig = go.Figure()
        fig.update_layout(title="Address Flow Trace (no edges)")
        fig.write_html(str(path), include_plotlyjs="cdn")
        return

    pos = nx.spring_layout(g, seed=42)
    edge_x, edge_y, edge_text = [], [], []
    for u, v, data in g.edges(data=True):
        x0, y0 = pos[u]; x1, y1 = pos[v]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]
        edge_text.append(
            f"{u} -> {v}<br>block={data.get('block_number')}<br>source={data.get('source')}<br>"
            f"value={data.get('value')}<br>token={data.get('token_address')}<br>tx={data.get('transaction_hash')}<br>"
            f"csv={data.get('csv_file_path')}<br>row={data.get('csv_row_number')}"
        )

    edge_trace = go.Scatter(x=edge_x, y=edge_y, mode="lines", line=dict(width=1), hoverinfo="none")

    incoming = defaultdict(int)
    outgoing = defaultdict(int)
    for u, v in g.edges():
        outgoing[u] += 1
        incoming[v] += 1

    node_x, node_y, node_text, node_label = [], [], [], []
    for n in g.nodes():
        x, y = pos[n]
        node_x.append(x); node_y.append(y)
        node_label.append(n[:10] + "..." if len(n) > 12 else n)
        node_text.append(f"address={n}<br>incoming={incoming[n]}<br>outgoing={outgoing[n]}")

    node_trace = go.Scatter(
        x=node_x, y=node_y, mode="markers+text", text=node_label, textposition="top center",
        hoverinfo="text", hovertext=node_text, marker=dict(size=10, color="#1f77b4")
    )

    fig = go.Figure(data=[edge_trace, node_trace])
    fig.update_layout(title="Backward Address Flow Trace", showlegend=False)
    fig.write_html(str(path), include_plotlyjs="cdn")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Trace backward funding/source chain for an Ethereum address")
    p.add_argument("--address", required=True)
    p.add_argument("--data-root", required=True)
    p.add_argument("--index-dir", required=True)
    p.add_argument("--output", default="address_flow_graph.html")
    p.add_argument("--max-depth", type=int, default=5)
    p.add_argument("--max-branches-per-address", type=int, default=20)
    p.add_argument("--cutoff-block", type=int, default=10**18)
    p.add_argument("--source", choices=["all", "transaction", "token_transfer"], default="all")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    addr = normalize_address(args.address)
    if not addr:
        raise SystemExit("Invalid --address. Must be non-empty and start with 0x")

    data_root = Path(args.data_root)
    index_dir = Path(args.index_dir)
    if not data_root.exists() or not data_root.is_dir():
        raise SystemExit(f"Invalid --data-root: {data_root}")
    if not index_dir.exists() or not index_dir.is_dir():
        raise SystemExit(f"Invalid --index-dir: {index_dir}")

    index_dbs = load_index_databases(index_dir)
    if not index_dbs:
        raise SystemExit(f"No index DB files found in {index_dir}")
    print(f"Loaded {len(index_dbs)} index databases")

    ctx = TraceContext(data_root, args.max_branches_per_address, args.max_depth, args.source)
    trace_address(ctx, index_dbs, addr, args.cutoff_block, 0)

    output_html = Path(args.output)
    output_dir = output_html.parent if output_html.parent != Path("") else Path(".")
    output_dir.mkdir(parents=True, exist_ok=True)

    edges_csv = output_dir / "edges.csv"
    nodes_csv = output_dir / "nodes.csv"
    json_out = output_dir / "trace_result.json"

    write_edges_csv(edges_csv, ctx.edge_rows)
    write_nodes_csv(nodes_csv, ctx.edge_rows)
    write_json(json_out, addr, ctx.edge_rows, args)
    build_plotly_graph(output_html, ctx.edge_rows)

    print(f"\nDone. edges={len(ctx.edge_rows)}")
    print(f"Graph: {output_html}")
    print(f"Edges CSV: {edges_csv}")
    print(f"Nodes CSV: {nodes_csv}")
    print(f"JSON: {json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
