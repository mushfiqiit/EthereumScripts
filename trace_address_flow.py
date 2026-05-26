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
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Optional

import networkx as nx
import plotly.graph_objects as go

getcontext().prec = 80

ETH_ROOT_RE = re.compile(r"^Ethereum_TT_(\d+)_(\d+)$")
INNER_RE = re.compile(r"^Transaction_TokenTransfer_(\d+)_(\d+)$")
DB_RE = re.compile(r"^address_block_index_(\d+)_(\d+)\.sqlite$")

BLOCK_CANDIDATES = ("block_number", "blocknumber", "block")
FROM_CANDIDATES = ("from_address", "from")
TO_CANDIDATES = ("to_address", "to")
TX_HASH_CANDIDATES = ("hash", "transaction_hash", "tx_hash")
TOKEN_CANDIDATES = ("token_address", "contract_address", "token")
VALUE_CANDIDATES = ("value", "amount")


@dataclass
class Occurrence:
    db_path: Path
    block_number: int
    source: str


class TraceContext:
    def __init__(
        self,
        data_root: Path,
        max_branches_per_address: int,
        max_depth: int,
        source: str,
        token_metadata: dict[str, dict[str, Any]],
    ):
        self.data_root = data_root
        self.max_branches = max_branches_per_address
        self.max_depth = max_depth
        self.source_filter = source
        self.token_metadata = token_metadata

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


def configure_csv_field_limit() -> int:
    limit = sys.maxsize
    while True:
        try:
            return csv.field_size_limit(limit)
        except OverflowError:
            limit //= 10


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


def detect_column(fieldnames: list[str], candidates: tuple[str, ...]) -> Optional[str]:
    norm = {c.strip().lower(): c for c in fieldnames if c}
    for c in candidates:
        if c in norm:
            return norm[c]
    return None


def parse_numeric_value(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "null", "none"}:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def format_amount(raw_value: Optional[Decimal], decimals: Optional[int]) -> str:
    if raw_value is None:
        return ""
    if decimals is None:
        return format(raw_value, "f")
    try:
        return format(raw_value / (Decimal(10) ** int(decimals)), "f")
    except Exception:
        return format(raw_value, "f")


def load_token_metadata(path: Optional[Path]) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    if not path.exists() or not path.is_file():
        print(f"[WARN] token metadata file not found: {path}")
        return {}

    out: dict[str, dict[str, Any]] = {}
    try:
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                print(f"[WARN] token metadata missing header: {path}")
                return out

            token_col = detect_column(reader.fieldnames, ("token_address", "contract_address", "token"))
            name_col = detect_column(reader.fieldnames, ("token_name", "name"))
            sym_col = detect_column(reader.fieldnames, ("token_symbol", "symbol"))
            dec_col = detect_column(reader.fieldnames, ("decimals", "token_decimals"))

            if not token_col:
                print(f"[WARN] token metadata missing token address column: {path}")
                return out

            for row in reader:
                token_addr = normalize_address(row.get(token_col))
                if not token_addr:
                    continue
                dec_raw = row.get(dec_col) if dec_col else None
                decimals: Optional[int] = None
                if dec_raw is not None and str(dec_raw).strip() != "":
                    try:
                        decimals = int(str(dec_raw).strip())
                    except Exception:
                        decimals = None
                out[token_addr] = {
                    "token_name": (str(row.get(name_col)).strip() if name_col and row.get(name_col) is not None else ""),
                    "token_symbol": (str(row.get(sym_col)).strip() if sym_col and row.get(sym_col) is not None else ""),
                    "decimals": decimals,
                }
    except Exception as exc:
        print(f"[WARN] failed to load token metadata {path}: {exc}")
    print(f"Loaded token metadata entries: {len(out)}")
    return out


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
        conn: Optional[sqlite3.Connection] = None
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
            if conn is not None:
                conn.close()
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


def extract_edge_asset_metadata(row: dict[str, Any], source: str, token_metadata: dict[str, dict[str, Any]]) -> dict[str, Any]:
    raw_val_dec = parse_numeric_value(row.get("raw_value"))
    if source == "transaction":
        decimals = 18
        value_display = format_amount(raw_val_dec, decimals)
        return {
            "asset_address": "ETH",
            "asset_symbol": "ETH",
            "token_name": "Ether",
            "token_decimals": decimals,
            "raw_value": "" if raw_val_dec is None else format(raw_val_dec, "f"),
            "value_display": value_display,
        }

    token_addr = normalize_address(row.get("token_address"))
    token_addr_out = token_addr if token_addr else ""
    meta = token_metadata.get(token_addr_out, {}) if token_addr_out else {}
    decimals = meta.get("decimals")
    value_display = format_amount(raw_val_dec, decimals if isinstance(decimals, int) else None)
    symbol = (meta.get("token_symbol") or "").strip() if isinstance(meta.get("token_symbol"), str) else ""
    if not symbol:
        symbol = token_addr_out if token_addr_out else "UNKNOWN_TOKEN"

    return {
        "asset_address": token_addr_out,
        "asset_symbol": symbol,
        "token_name": (meta.get("token_name") or "") if isinstance(meta.get("token_name"), str) else "",
        "token_decimals": decimals if isinstance(decimals, int) else "",
        "raw_value": "" if raw_val_dec is None else format(raw_val_dec, "f"),
        "value_display": value_display,
    }


def build_edge_hover_text(edge: dict[str, Any]) -> str:
    token_name = edge.get("token_name") or ""
    tx_hash = edge.get("transaction_hash") or ""
    return (
        f"From: {edge.get('from_address','')}<br>"
        f"To: {edge.get('to_address','')}<br>"
        f"Block: {edge.get('block_number','')}<br>"
        f"Source: {edge.get('source','')}<br>"
        f"Asset: {edge.get('asset_symbol','')}<br>"
        f"Token Address: {edge.get('asset_address','')}<br>"
        f"Token Name: {token_name}<br>"
        f"Raw Value: {edge.get('raw_value','')}<br>"
        f"Displayed Value: {edge.get('value_display','')} {edge.get('asset_symbol','')}<br>"
        f"Transaction Hash: {tx_hash}<br>"
        f"CSV File: {edge.get('csv_file_path','')}<br>"
        f"CSV Row: {edge.get('csv_row_number','')}"
    )


def scan_csv_for_incoming_rows(
    csv_path: Path,
    target_address: str,
    block_number: int,
    source: str,
    token_metadata: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
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

                    edge_base = {
                        "from_address": from_addr,
                        "to_address": target_address,
                        "block_number": row_block,
                        "csv_row_number": i,
                        "transaction_hash": row.get(tx_col) if tx_col else None,
                        "token_address": row.get(tok_col) if tok_col else None,
                        "raw_value": row.get(val_col) if val_col else None,
                    }
                    asset_meta = extract_edge_asset_metadata(edge_base, source, token_metadata)

                    rows.append(
                        {
                            "from_address": from_addr,
                            "to_address": target_address,
                            "block_number": row_block,
                            "csv_row_number": i,
                            "transaction_hash": row.get(tx_col) if tx_col else None,
                            "asset_address": asset_meta["asset_address"],
                            "asset_symbol": asset_meta["asset_symbol"],
                            "token_name": asset_meta["token_name"],
                            "token_decimals": asset_meta["token_decimals"],
                            "raw_value": asset_meta["raw_value"],
                            "value_display": asset_meta["value_display"],
                        }
                    )
                except Exception:
                    continue
    except FileNotFoundError:
        print(f"[WARN] CSV file missing: {csv_path}")
    except csv.Error as exc:
        print(f"[WARN] CSV parse error in {csv_path}: {exc}")
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

        matches = scan_csv_for_incoming_rows(csv_path, address, occ.block_number, occ.source, ctx.token_metadata)
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
                print(
                    f"  added edge {edge['from_address']} -> {edge['to_address']} @ {edge['block_number']} "
                    f"value={edge.get('value_display','')} {edge.get('asset_symbol','')}"
                )
                expanded += 1
                trace_address(ctx, index_dbs, edge["from_address"], edge["block_number"], depth + 1)
                if expanded >= ctx.max_branches:
                    break


def write_edges_csv(path: Path, edges: list[dict[str, Any]]) -> None:
    fields = [
        "from_address",
        "to_address",
        "block_number",
        "source",
        "asset_address",
        "asset_symbol",
        "token_name",
        "token_decimals",
        "raw_value",
        "value_display",
        "transaction_hash",
        "csv_file_path",
        "csv_row_number",
    ]
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
        nodes.add(f)
        nodes.add(t)
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
            "token_metadata": args.token_metadata,
        },
        "edge_count": len(edges),
        "edges": edges,
    }
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def build_plotly_graph(path: Path, edges: list[dict[str, Any]]) -> None:
    g = nx.DiGraph()
    for idx, e in enumerate(edges):
        g.add_node(e["from_address"])
        g.add_node(e["to_address"])
        g.add_edge(e["from_address"], e["to_address"], edge_idx=idx)

    if g.number_of_nodes() == 0:
        fig = go.Figure()
        fig.update_layout(title="Address Flow Trace (no edges)")
        fig.write_html(str(path), include_plotlyjs="cdn")
        return

    pos = nx.spring_layout(g, seed=42)

    edge_x: list[float] = []
    edge_y: list[float] = []
    mid_x: list[float] = []
    mid_y: list[float] = []
    mid_text: list[str] = []
    mid_label: list[str] = []

    for e in edges:
        u = e["from_address"]
        v = e["to_address"]
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]

        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        mid_x.append(mx)
        mid_y.append(my)
        mid_text.append(build_edge_hover_text(e))
        mid_label.append(f"{e.get('value_display','')} {e.get('asset_symbol','')}")

    edge_trace = go.Scatter(
        x=edge_x,
        y=edge_y,
        mode="lines",
        line=dict(width=1, color="#888"),
        hoverinfo="none",
    )

    edge_mid_trace = go.Scatter(
        x=mid_x,
        y=mid_y,
        mode="markers+text",
        text=mid_label,
        textposition="middle center",
        marker=dict(size=6, color="rgba(0,0,0,0.2)"),
        hoverinfo="text",
        hovertext=mid_text,
    )

    incoming = defaultdict(int)
    outgoing = defaultdict(int)
    for e in edges:
        outgoing[e["from_address"]] += 1
        incoming[e["to_address"]] += 1

    node_x, node_y, node_text, node_label = [], [], [], []
    for n in g.nodes():
        x, y = pos[n]
        node_x.append(x)
        node_y.append(y)
        node_label.append(n[:10] + "..." if len(n) > 12 else n)
        node_text.append(f"address={n}<br>incoming={incoming[n]}<br>outgoing={outgoing[n]}")

    node_trace = go.Scatter(
        x=node_x,
        y=node_y,
        mode="markers+text",
        text=node_label,
        textposition="top center",
        hoverinfo="text",
        hovertext=node_text,
        marker=dict(size=10, color="#1f77b4"),
    )

    fig = go.Figure(data=[edge_trace, edge_mid_trace, node_trace])
    fig.update_layout(title="Backward Address Flow Trace", showlegend=False)
    fig.write_html(str(path), include_plotlyjs="cdn")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Trace backward funding/source chain for an Ethereum address")
    p.add_argument("--address", required=True)
    p.add_argument("--data-root", required=True)
    p.add_argument("--index-dir", required=True)
    p.add_argument("--token-metadata", help="Optional token metadata CSV with token_address/token_symbol/decimals")
    p.add_argument("--output", default="address_flow_graph.html")
    p.add_argument("--max-depth", type=int, default=5)
    p.add_argument("--max-branches-per-address", type=int, default=20)
    p.add_argument("--cutoff-block", type=int, default=10**18)
    p.add_argument("--source", choices=["all", "transaction", "token_transfer"], default="all")
    return p.parse_args()


def main() -> int:
    configured_limit = configure_csv_field_limit()
    print(f"Configured csv.field_size_limit={configured_limit}")

    args = parse_args()
    addr = normalize_address(args.address)
    if not addr:
        raise SystemExit("Invalid --address. Must be non-empty and start with 0x")

    data_root = Path(args.data_root)
    index_dir = Path(args.index_dir)
    token_meta_path = Path(args.token_metadata) if args.token_metadata else None

    if not data_root.exists() or not data_root.is_dir():
        raise SystemExit(f"Invalid --data-root: {data_root}")
    if not index_dir.exists() or not index_dir.is_dir():
        raise SystemExit(f"Invalid --index-dir: {index_dir}")

    token_metadata = load_token_metadata(token_meta_path)

    index_dbs = load_index_databases(index_dir)
    if not index_dbs:
        raise SystemExit(f"No index DB files found in {index_dir}")
    print(f"Loaded {len(index_dbs)} index databases")

    ctx = TraceContext(data_root, args.max_branches_per_address, args.max_depth, args.source, token_metadata)
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
