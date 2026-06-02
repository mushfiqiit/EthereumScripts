#!/usr/bin/env python3
"""
Backward Ethereum address flow tracer.

This script performs backward flow tracing. Starting from a target address, it
finds incoming transfers where the target is in the "to" field. For each
incoming transfer, it extracts the "from" address and recursively traces where
that source address previously received funds/tokens. The result is a directed
flow graph showing historical value paths toward the target address.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
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
USD_VALUE_CANDIDATES = (
    "usd_value",
    "value_usd",
    "amount_usd",
    "usd_amount",
    "value_in_usd",
    "total_usd",
)


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
        min_usd_value: Optional[Decimal] = None,
    ):
        self.data_root = data_root
        self.max_branches = max_branches_per_address
        self.max_depth = max_depth
        self.source_filter = source
        self.token_metadata = token_metadata
        self.min_usd_value = min_usd_value

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
    s = str(value).strip().replace(",", "")
    if not s or s.lower() in {"nan", "null", "none"}:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def decimal_to_float(value: Any, default: float = 0.0) -> float:
    dec = parse_numeric_value(value)
    if dec is None:
        return default
    try:
        f = float(dec)
        if math.isfinite(f):
            return f
    except Exception:
        pass
    return default


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def format_amount(raw_value: Optional[Decimal], decimals: Optional[int]) -> str:
    if raw_value is None:
        return ""
    if decimals is None:
        return format(raw_value, "f")
    try:
        return format(raw_value / (Decimal(10) ** int(decimals)), "f")
    except Exception:
        return format(raw_value, "f")


def format_usd(value: Any) -> str:
    dec = parse_numeric_value(value)
    if dec is None:
        return ""
    try:
        return f"${float(dec):,.2f}"
    except Exception:
        return f"${format(dec, 'f')}"


def short_address(address: str, prefix: int = 8, suffix: int = 6) -> str:
    if not address:
        return ""
    if address.startswith("Other ") or len(address) <= prefix + suffix + 3:
        return address
    return f"{address[:prefix]}...{address[-suffix:]}"


def output_path_for_mode(base: Path, mode: str, layout_mode: str) -> Path:
    if layout_mode == "all":
        suffix = mode.replace("-", "_")
        return base.with_name(f"{base.stem}_{suffix}{base.suffix or '.html'}")
    return base


def depth_snapshot_path(base: Path, depth: int) -> Path:
    return base.with_name(f"{base.stem}_depth_{depth}{base.suffix or '.html'}")


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
                rows = conn.execute(
                    """
                    SELECT block_number, source
                    FROM occurrences
                    WHERE address = ? AND role = 'to' AND block_number <= ?
                    ORDER BY block_number DESC
                    """,
                    (address, cutoff_block),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT block_number, source
                    FROM occurrences
                    WHERE address = ? AND role = 'to' AND block_number <= ? AND source = ?
                    ORDER BY block_number DESC
                    """,
                    (address, cutoff_block, source_filter),
                ).fetchall()
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
    return (
        f"<b>FLOW: {edge.get('from_address','')} -&gt; {edge.get('to_address','')}</b><br>"
        f"Block: {edge.get('block_number','')}<br>"
        f"Depth: {edge.get('depth','')}<br>"
        f"Source: {edge.get('source','')}<br>"
        f"Asset: {edge.get('asset_symbol','')}<br>"
        f"Asset Address: {edge.get('asset_address','')}<br>"
        f"Token Name: {edge.get('token_name') or ''}<br>"
        f"Value: {edge.get('value_display','')} {edge.get('asset_symbol','')}<br>"
        f"Raw Value: {edge.get('raw_value','')}<br>"
        f"USD Value: {format_usd(edge.get('usd_value')) or edge.get('usd_value','')}<br>"
        f"Transfer Count: {edge.get('transfer_count', 1)}<br>"
        f"Transaction Hash: {edge.get('transaction_hash') or ''}<br>"
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
            usd_col = detect_column(reader.fieldnames, USD_VALUE_CANDIDATES)
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

                    usd_value = row.get(usd_col) if usd_col else ""
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
                            "usd_value": "" if parse_numeric_value(usd_value) is None else format(parse_numeric_value(usd_value), "f"),
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
    if ctx.min_usd_value is not None:
        usd = parse_numeric_value(edge.get("usd_value"))
        if usd is not None and usd < ctx.min_usd_value:
            return False
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
                "depth": depth + 1,
                "source": occ.source,
                "csv_file_path": str(csv_path),
            }
            if add_edge(ctx, edge):
                print(
                    f"  added edge depth={edge['depth']} {edge['from_address']} -> {edge['to_address']} @ {edge['block_number']} "
                    f"value={edge.get('value_display','')} {edge.get('asset_symbol','')} usd={edge.get('usd_value','')}"
                )
                expanded += 1
                trace_address(ctx, index_dbs, edge["from_address"], edge["block_number"], depth + 1)
                if expanded >= ctx.max_branches:
                    break


def compute_node_metadata(edges: list[dict[str, Any]], root_address: str) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}

    def ensure(address: str, depth: int) -> dict[str, Any]:
        n = nodes.setdefault(
            address,
            {
                "address": address,
                "depth": depth,
                "total_incoming_value_usd": "",
                "total_outgoing_value_usd": "",
                "incoming_edge_count": 0,
                "outgoing_edge_count": 0,
                "first_seen_block": "",
                "last_seen_block": "",
                "node_type": "intermediate",
            },
        )
        if depth < int(n.get("depth", depth)):
            n["depth"] = depth
        return n

    ensure(root_address, 0)
    incoming_usd: defaultdict[str, Decimal] = defaultdict(Decimal)
    outgoing_usd: defaultdict[str, Decimal] = defaultdict(Decimal)
    blocks: defaultdict[str, list[int]] = defaultdict(list)

    for e in edges:
        edge_depth = int(e.get("depth") or 1)
        src, dst = e["from_address"], e["to_address"]
        ensure(src, edge_depth)
        ensure(dst, max(0, edge_depth - 1))
        nodes[src]["outgoing_edge_count"] += int(e.get("transfer_count", 1) or 1)
        nodes[dst]["incoming_edge_count"] += int(e.get("transfer_count", 1) or 1)
        block = e.get("block_number")
        if block not in (None, ""):
            try:
                blocks[src].append(int(block))
                blocks[dst].append(int(block))
            except Exception:
                pass
        usd = parse_numeric_value(e.get("usd_value"))
        if usd is not None:
            outgoing_usd[src] += usd
            incoming_usd[dst] += usd

    for address, n in nodes.items():
        if incoming_usd[address]:
            n["total_incoming_value_usd"] = format(incoming_usd[address], "f")
        if outgoing_usd[address]:
            n["total_outgoing_value_usd"] = format(outgoing_usd[address], "f")
        if blocks[address]:
            n["first_seen_block"] = min(blocks[address])
            n["last_seen_block"] = max(blocks[address])
        if address == root_address:
            n["node_type"] = "root"
        elif n["incoming_edge_count"] == 0:
            n["node_type"] = "leaf_source"
        else:
            n["node_type"] = "intermediate"
    return nodes


def node_value_score(node: dict[str, Any]) -> float:
    return decimal_to_float(node.get("total_incoming_value_usd")) + decimal_to_float(node.get("total_outgoing_value_usd"))


def edge_weight(edge: dict[str, Any]) -> float:
    usd = decimal_to_float(edge.get("usd_value"), 0.0)
    if usd > 0:
        return usd
    val = decimal_to_float(edge.get("value_display"), 0.0)
    if val > 0:
        return val
    raw = decimal_to_float(edge.get("raw_value"), 0.0)
    if raw > 0:
        return raw
    return float(edge.get("transfer_count", 1) or 1)


def aggregate_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in edges:
        key = (e.get("from_address", ""), e.get("to_address", ""), e.get("asset_symbol", ""))
        if key not in grouped:
            grouped[key] = dict(e)
            grouped[key]["transfer_count"] = int(e.get("transfer_count", 1) or 1)
            grouped[key]["csv_row_number"] = str(e.get("csv_row_number", ""))
            grouped[key]["transaction_hash"] = str(e.get("transaction_hash") or "")
            grouped[key]["block_number"] = str(e.get("block_number", ""))
            continue
        g = grouped[key]
        g["transfer_count"] = int(g.get("transfer_count", 1) or 1) + int(e.get("transfer_count", 1) or 1)
        g["depth"] = min(int(g.get("depth") or 0), int(e.get("depth") or 0))
        for field in ("raw_value", "value_display", "usd_value"):
            a = parse_numeric_value(g.get(field))
            b = parse_numeric_value(e.get(field))
            if a is not None and b is not None:
                g[field] = format(a + b, "f")
        g["block_number"] = f"{g.get('block_number','')}, {e.get('block_number','')}"
        g["transaction_hash"] = f"{g.get('transaction_hash','')}, {e.get('transaction_hash') or ''}"
        g["csv_row_number"] = f"{g.get('csv_row_number','')}, {e.get('csv_row_number','')}"
    return list(grouped.values())


def simplify_for_render(
    edges: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    root_address: str,
    args: argparse.Namespace,
    max_depth_filter: Optional[int] = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
    render_edges = list(edges)
    if max_depth_filter is not None:
        render_edges = [e for e in render_edges if int(e.get("depth") or 0) <= max_depth_filter]
    if args.hide_small_edges_below_usd is not None:
        threshold = Decimal(str(args.hide_small_edges_below_usd))
        render_edges = [e for e in render_edges if parse_numeric_value(e.get("usd_value")) is None or parse_numeric_value(e.get("usd_value")) >= threshold]
    if args.aggregate_edges:
        render_edges = aggregate_edges(render_edges)

    if args.collapse_by_depth_after is not None:
        cutoff = int(args.collapse_by_depth_after)
        collapsed: list[dict[str, Any]] = []
        for e in render_edges:
            e = dict(e)
            d = int(e.get("depth") or 0)
            if d > cutoff:
                bucket = f"Other sources deeper than depth {cutoff} (depth {d})"
                e["from_address"] = bucket
                e["depth"] = cutoff + 1
                e["asset_symbol"] = e.get("asset_symbol") or "MULTI"
            collapsed.append(e)
        render_edges = aggregate_edges(collapsed) if args.aggregate_edges else collapsed

    render_nodes = compute_node_metadata(render_edges, root_address)

    if args.collapse_leaf_sources:
        leafs_by_depth: defaultdict[int, list[str]] = defaultdict(list)
        for addr, node in render_nodes.items():
            if node.get("node_type") == "leaf_source":
                leafs_by_depth[int(node.get("depth") or 0)].append(addr)
        leafs_to_collapse = {addr: depth for depth, addrs in leafs_by_depth.items() if len(addrs) > 10 for addr in addrs}
        if leafs_to_collapse:
            collapsed_edges = []
            for e in render_edges:
                e = dict(e)
                if e.get("from_address") in leafs_to_collapse:
                    d = leafs_to_collapse[e["from_address"]]
                    e["from_address"] = f"Other leaf sources at depth {d}"
                collapsed_edges.append(e)
            render_edges = aggregate_edges(collapsed_edges) if args.aggregate_edges else collapsed_edges
            render_nodes = compute_node_metadata(render_edges, root_address)
            for addr, node in render_nodes.items():
                if str(addr).startswith("Other leaf sources"):
                    node["node_type"] = "leaf_source"

    render_edges.sort(key=edge_weight, reverse=True)
    if args.top_k_edges_by_value:
        render_edges = render_edges[: int(args.top_k_edges_by_value)]
    if args.max_render_edges:
        render_edges = render_edges[: int(args.max_render_edges)]

    render_nodes = compute_node_metadata(render_edges, root_address)
    if args.top_k_nodes_by_value and len(render_nodes) > int(args.top_k_nodes_by_value):
        keep = {root_address}
        ranked = sorted(render_nodes.values(), key=node_value_score, reverse=True)
        keep.update(n["address"] for n in ranked[: int(args.top_k_nodes_by_value)])
        render_edges = [e for e in render_edges if e["from_address"] in keep and e["to_address"] in keep]
        render_nodes = compute_node_metadata(render_edges, root_address)
    if args.max_render_nodes and len(render_nodes) > int(args.max_render_nodes):
        keep = {root_address}
        ranked = sorted(render_nodes.values(), key=lambda n: (node_value_score(n), n.get("incoming_edge_count", 0) + n.get("outgoing_edge_count", 0)), reverse=True)
        keep.update(n["address"] for n in ranked[: int(args.max_render_nodes)])
        render_edges = [e for e in render_edges if e["from_address"] in keep and e["to_address"] in keep]
        render_nodes = compute_node_metadata(render_edges, root_address)

    note = (
        f"Rendered {len(render_nodes)} of {len(nodes)} nodes and {len(render_edges)} of {len(edges)} edges. "
        "Full data is available in edges.csv and trace_result.json."
    )
    return render_edges, render_nodes, note


def build_node_hover_text(node: dict[str, Any]) -> str:
    return (
        f"<b>Address:</b> {node.get('address','')}<br>"
        f"Depth: {node.get('depth','')}<br>"
        f"Node Type: {node.get('node_type','')}<br>"
        f"Incoming Edges: {node.get('incoming_edge_count', 0)}<br>"
        f"Outgoing Edges: {node.get('outgoing_edge_count', 0)}<br>"
        f"Total Incoming USD: {format_usd(node.get('total_incoming_value_usd')) or node.get('total_incoming_value_usd','')}<br>"
        f"Total Outgoing USD: {format_usd(node.get('total_outgoing_value_usd')) or node.get('total_outgoing_value_usd','')}<br>"
        f"First Seen Block: {node.get('first_seen_block','')}<br>"
        f"Last Seen Block: {node.get('last_seen_block','')}"
    )


def choose_visible_labels(nodes: dict[str, dict[str, Any]], root_address: str, label_mode: str, max_visible_labels: int) -> set[str]:
    if label_mode == "none":
        return set()
    if label_mode == "all":
        return set(nodes)
    keep = {root_address} if root_address in nodes else set()
    if label_mode == "root-only":
        return keep
    remaining = max(0, max_visible_labels - len(keep))
    if label_mode == "top-value":
        ranked = sorted(nodes.values(), key=node_value_score, reverse=True)
    else:
        ranked = sorted(nodes.values(), key=lambda n: n.get("incoming_edge_count", 0) + n.get("outgoing_edge_count", 0), reverse=True)
    for n in ranked:
        if len(keep) >= max_visible_labels:
            break
        if n["address"] != root_address and remaining > 0:
            keep.add(n["address"])
            remaining -= 1
    return keep


def depth_color(depth: int, node_type: str) -> str:
    if node_type == "root":
        return "#d62728"
    if node_type == "leaf_source":
        return "#2ca02c"
    palette = ["#1f77b4", "#ff7f0e", "#9467bd", "#17becf", "#bcbd22", "#8c564b", "#e377c2", "#7f7f7f"]
    return palette[max(depth - 1, 0) % len(palette)]


def hierarchical_layout(nodes: dict[str, dict[str, Any]]) -> dict[str, tuple[float, float]]:
    by_depth: defaultdict[int, list[str]] = defaultdict(list)
    for addr, n in nodes.items():
        by_depth[int(n.get("depth") or 0)].append(addr)
    pos: dict[str, tuple[float, float]] = {}
    for depth, addrs in sorted(by_depth.items()):
        addrs.sort(key=lambda a: (node_value_score(nodes[a]), nodes[a].get("outgoing_edge_count", 0)), reverse=True)
        count = len(addrs)
        for i, addr in enumerate(addrs):
            y = 0.0 if count == 1 else (count - 1) / 2 - i
            pos[addr] = (-float(depth), y)
    return pos


def radial_depth_layout(nodes: dict[str, dict[str, Any]]) -> dict[str, tuple[float, float]]:
    by_depth: defaultdict[int, list[str]] = defaultdict(list)
    for addr, n in nodes.items():
        by_depth[int(n.get("depth") or 0)].append(addr)
    pos: dict[str, tuple[float, float]] = {}
    for depth, addrs in sorted(by_depth.items()):
        addrs.sort()
        if depth == 0:
            for addr in addrs:
                pos[addr] = (0.0, 0.0)
            continue
        radius = float(depth)
        count = len(addrs)
        for i, addr in enumerate(addrs):
            theta = (2 * math.pi * i / max(count, 1)) + (depth * 0.17)
            pos[addr] = (radius * math.cos(theta), radius * math.sin(theta))
    return pos


def spring_layout(edges: list[dict[str, Any]], nodes: dict[str, dict[str, Any]]) -> dict[str, tuple[float, float]]:
    g = nx.DiGraph()
    for addr in nodes:
        g.add_node(addr)
    for e in edges:
        g.add_edge(e["from_address"], e["to_address"])
    try:
        raw = nx.spring_layout(g, seed=42, k=None, iterations=100)
        return {n: (float(p[0]), float(p[1])) for n, p in raw.items()}
    except Exception as exc:
        print(f"[WARN] spring layout failed: {exc}; falling back to hierarchical layout")
        return hierarchical_layout(nodes)


def edge_widths(edges: list[dict[str, Any]]) -> dict[int, float]:
    weights = [edge_weight(e) for e in edges]
    max_w = max(weights) if weights else 1.0
    out: dict[int, float] = {}
    for i, w in enumerate(weights):
        out[i] = 1.0 + 5.0 * math.sqrt(max(w, 0.0) / max(max_w, 1e-9))
    return out


def build_network_graph(
    path: Path,
    edges: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    root_address: str,
    layout_mode: str,
    label_mode: str,
    max_visible_labels: int,
    render_note: str,
    title_suffix: str = "",
) -> None:
    if not nodes:
        fig = go.Figure()
        fig.update_layout(title="Address Flow Trace (no nodes)")
        fig.write_html(str(path), include_plotlyjs="cdn")
        return

    if layout_mode == "radial-depth":
        pos = radial_depth_layout(nodes)
    elif layout_mode == "spring":
        pos = spring_layout(edges, nodes)
    else:
        pos = hierarchical_layout(nodes)

    widths = edge_widths(edges)
    data: list[Any] = []
    for i, e in enumerate(edges):
        u, v = e["from_address"], e["to_address"]
        if u not in pos or v not in pos:
            continue
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        data.append(
            go.Scatter(
                x=[x0, x1],
                y=[y0, y1],
                mode="lines",
                line=dict(width=widths.get(i, 1.0), color="rgba(80,80,80,0.45)"),
                hoverinfo="none",
                showlegend=False,
            )
        )

    mid_x: list[float] = []
    mid_y: list[float] = []
    mid_text: list[str] = []
    mid_label: list[str] = []
    arrow_x: list[float] = []
    arrow_y: list[float] = []
    for e in edges:
        u, v = e["from_address"], e["to_address"]
        if u not in pos or v not in pos:
            continue
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        mid_x.append(mx)
        mid_y.append(my)
        mid_text.append(build_edge_hover_text(e))
        val = e.get("value_display") or e.get("usd_value") or ""
        mid_label.append(f"{val} {e.get('asset_symbol','')}"[:42])
        arrow_x.append(x0 + 0.72 * (x1 - x0))
        arrow_y.append(y0 + 0.72 * (y1 - y0))

    data.append(
        go.Scatter(
            x=mid_x,
            y=mid_y,
            mode="markers+text",
            text=mid_label,
            textposition="middle center",
            marker=dict(size=8, color="rgba(0,0,0,0.30)"),
            hoverinfo="text",
            hovertext=mid_text,
            name="edge value / hover for transfer details",
        )
    )
    data.append(
        go.Scatter(
            x=arrow_x,
            y=arrow_y,
            mode="markers",
            marker=dict(symbol="triangle-right", size=10, color="#111"),
            hoverinfo="skip",
            name="flow direction arrows",
        )
    )

    visible_labels = choose_visible_labels(nodes, root_address, label_mode, max_visible_labels)
    node_x: list[float] = []
    node_y: list[float] = []
    node_text: list[str] = []
    node_label: list[str] = []
    node_color: list[str] = []
    node_size: list[float] = []
    for addr, n in sorted(nodes.items(), key=lambda kv: (int(kv[1].get("depth") or 0), kv[0])):
        if addr not in pos:
            continue
        x, y = pos[addr]
        node_x.append(x)
        node_y.append(y)
        node_text.append(build_node_hover_text(n))
        node_label.append(short_address(addr) if addr in visible_labels else "")
        node_color.append(depth_color(int(n.get("depth") or 0), str(n.get("node_type") or "")))
        degree = int(n.get("incoming_edge_count", 0)) + int(n.get("outgoing_edge_count", 0))
        node_size.append(26 if n.get("node_type") == "root" else 10 + min(18, degree * 2))

    data.append(
        go.Scatter(
            x=node_x,
            y=node_y,
            mode="markers+text",
            text=node_label,
            textposition="top center",
            hoverinfo="text",
            hovertext=node_text,
            marker=dict(size=node_size, color=node_color, line=dict(width=1, color="#222")),
            name="addresses colored by depth",
        )
    )

    max_depth = max((int(n.get("depth") or 0) for n in nodes.values()), default=0)
    title = f"Backward Address Flow Trace - {layout_mode}{title_suffix}"
    fig = go.Figure(data=data)
    annotations = [
        dict(text="FLOW DIRECTION: upstream sources → target/root", x=0.5, y=1.06, xref="paper", yref="paper", showarrow=False, font=dict(size=13)),
        dict(text=f"Root is red. Leaf sources are green. Other colors cycle by depth. Max rendered depth: {max_depth}.", x=0.5, y=1.02, xref="paper", yref="paper", showarrow=False, font=dict(size=12)),
        dict(text=render_note, x=0.5, y=-0.08, xref="paper", yref="paper", showarrow=False, font=dict(size=12)),
    ]
    fig.update_layout(
        title=title,
        showlegend=True,
        hovermode="closest",
        annotations=annotations,
        margin=dict(l=30, r=30, t=100, b=80),
        xaxis=dict(showgrid=True, zeroline=False, title="Depth (farther left = deeper upstream source)" if layout_mode == "hierarchical" else ""),
        yaxis=dict(showgrid=True, zeroline=False, title="Addresses spread within depth" if layout_mode == "hierarchical" else ""),
        height=900,
    )
    fig.write_html(str(path), include_plotlyjs="cdn")


def build_sankey_graph(path: Path, edges: list[dict[str, Any]], nodes: dict[str, dict[str, Any]], root_address: str, render_note: str) -> None:
    sankey_edges = aggregate_edges(edges)
    sankey_edges.sort(key=edge_weight, reverse=True)
    addresses = sorted({e["from_address"] for e in sankey_edges} | {e["to_address"] for e in sankey_edges}, key=lambda a: (int(nodes.get(a, {}).get("depth", 999)), a))
    idx = {addr: i for i, addr in enumerate(addresses)}
    values = [max(edge_weight(e), 1e-9) for e in sankey_edges]
    custom = [build_edge_hover_text(e).replace("<br>", "\n") for e in sankey_edges]
    colors = ["rgba(31,119,180,0.35)" for _ in sankey_edges]
    node_colors = [depth_color(int(nodes.get(a, {}).get("depth", 0)), str(nodes.get(a, {}).get("node_type", ""))) for a in addresses]

    fig = go.Figure(
        data=[
            go.Sankey(
                arrangement="snap",
                node=dict(label=[short_address(a) for a in addresses], color=node_colors, pad=15, thickness=16),
                link=dict(
                    source=[idx[e["from_address"]] for e in sankey_edges],
                    target=[idx[e["to_address"]] for e in sankey_edges],
                    value=values,
                    color=colors,
                    customdata=custom,
                    hovertemplate="%{customdata}<extra></extra>",
                ),
            )
        ]
    )
    fig.update_layout(
        title="Backward Address Flow Trace - Sankey (aggregated by from/to/asset)",
        annotations=[dict(text=render_note, x=0.5, y=-0.06, xref="paper", yref="paper", showarrow=False)],
        height=900,
        margin=dict(l=30, r=30, t=80, b=80),
    )
    fig.write_html(str(path), include_plotlyjs="cdn")


def write_edges_csv(path: Path, edges: list[dict[str, Any]]) -> None:
    fields = [
        "from_address",
        "to_address",
        "depth",
        "block_number",
        "source",
        "asset_symbol",
        "asset_address",
        "token_name",
        "token_decimals",
        "raw_value",
        "value_display",
        "usd_value",
        "transaction_hash",
        "csv_file_path",
        "csv_row_number",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for e in edges:
            w.writerow({k: e.get(k, "") for k in fields})


def write_nodes_csv(path: Path, nodes: dict[str, dict[str, Any]]) -> None:
    fields = [
        "address",
        "depth",
        "node_type",
        "incoming_edge_count",
        "outgoing_edge_count",
        "total_incoming_value_usd",
        "total_outgoing_value_usd",
        "first_seen_block",
        "last_seen_block",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for n in sorted(nodes.values(), key=lambda x: (int(x.get("depth") or 0), x["address"])):
            w.writerow({k: n.get(k, "") for k in fields})


def write_json(path: Path, start_address: str, edges: list[dict[str, Any]], nodes: dict[str, dict[str, Any]], args: argparse.Namespace) -> None:
    obj = {
        "start_address": start_address,
        "parameters": {
            "max_depth": args.max_depth,
            "max_branches_per_address": args.max_branches_per_address,
            "cutoff_block": args.cutoff_block,
            "source": args.source,
            "min_usd_value": args.min_usd_value,
            "token_metadata": args.token_metadata,
            "layout_mode": args.layout_mode,
            "aggregate_edges": args.aggregate_edges,
        },
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": list(nodes.values()),
        "edges": edges,
    }
    path.write_text(json.dumps(json_safe(obj), indent=2), encoding="utf-8")


def build_summary(root_address: str, edges: list[dict[str, Any]], nodes: dict[str, dict[str, Any]], args: argparse.Namespace, rendered_nodes: int, rendered_edges: int) -> dict[str, Any]:
    top_in = sorted(nodes.values(), key=lambda n: decimal_to_float(n.get("total_incoming_value_usd")), reverse=True)[:20]
    top_out = sorted(nodes.values(), key=lambda n: decimal_to_float(n.get("total_outgoing_value_usd")), reverse=True)[:20]
    top_edges = sorted(edges, key=edge_weight, reverse=True)[:20]
    unique_token_addresses = {e.get("asset_address") for e in edges if e.get("asset_address") and e.get("asset_address") != "ETH"}
    unique_assets = {e.get("asset_symbol") or e.get("asset_address") or "UNKNOWN" for e in edges}
    return {
        "root_address": root_address,
        "max_depth_requested": args.max_depth,
        "max_depth_actually_reached": max((int(e.get("depth") or 0) for e in edges), default=0),
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "rendered_nodes": rendered_nodes,
        "rendered_edges": rendered_edges,
        "number_of_leaf_source_nodes": sum(1 for n in nodes.values() if n.get("node_type") == "leaf_source"),
        "number_of_direct_source_nodes": sum(1 for n in nodes.values() if int(n.get("depth") or -1) == 1),
        "top_20_addresses_by_incoming_value": top_in,
        "top_20_addresses_by_outgoing_value": top_out,
        "top_20_edges_by_value": top_edges,
        "number_of_eth_edges": sum(1 for e in edges if e.get("asset_symbol") == "ETH" or e.get("asset_address") == "ETH"),
        "number_of_token_transfer_edges": sum(1 for e in edges if e.get("source") == "token_transfer"),
        "number_of_unique_token_addresses": len(unique_token_addresses),
        "number_of_unique_assets": len(unique_assets),
    }


def write_summary_files(txt_path: Path, json_path: Path, summary: dict[str, Any]) -> None:
    json_path.write_text(json.dumps(json_safe(summary), indent=2), encoding="utf-8")
    lines = [
        f"root_address: {summary['root_address']}",
        f"max_depth_requested: {summary['max_depth_requested']}",
        f"max_depth_actually_reached: {summary['max_depth_actually_reached']}",
        f"total_nodes: {summary['total_nodes']}",
        f"total_edges: {summary['total_edges']}",
        f"rendered_nodes: {summary['rendered_nodes']}",
        f"rendered_edges: {summary['rendered_edges']}",
        f"number_of_leaf_source_nodes: {summary['number_of_leaf_source_nodes']}",
        f"number_of_direct_source_nodes: {summary['number_of_direct_source_nodes']}",
        f"number_of_eth_edges: {summary['number_of_eth_edges']}",
        f"number_of_token_transfer_edges: {summary['number_of_token_transfer_edges']}",
        f"number_of_unique_token_addresses: {summary['number_of_unique_token_addresses']}",
        f"number_of_unique_assets: {summary['number_of_unique_assets']}",
        "",
        "Top 20 addresses by incoming USD value:",
    ]
    for n in summary["top_20_addresses_by_incoming_value"]:
        lines.append(f"  {n['address']} depth={n.get('depth')} incoming_usd={n.get('total_incoming_value_usd','')}")
    lines.append("\nTop 20 addresses by outgoing USD value:")
    for n in summary["top_20_addresses_by_outgoing_value"]:
        lines.append(f"  {n['address']} depth={n.get('depth')} outgoing_usd={n.get('total_outgoing_value_usd','')}")
    lines.append("\nTop 20 edges by value:")
    for e in summary["top_20_edges_by_value"]:
        lines.append(f"  {e.get('from_address')} -> {e.get('to_address')} depth={e.get('depth')} value={e.get('value_display')} {e.get('asset_symbol')} usd={e.get('usd_value')}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_depth_snapshots(raw: Optional[str]) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            depth = int(part)
            if depth >= 0:
                out.append(depth)
        except Exception:
            print(f"[WARN] ignoring invalid depth snapshot value: {part}")
    return sorted(set(out))


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
    p.add_argument("--min-usd-value", type=Decimal, default=None, help="Trace-filter rows with usd_value below this amount when a USD column exists")

    p.add_argument("--layout-mode", choices=["hierarchical", "sankey", "radial-depth", "spring", "all"], default="hierarchical")
    p.add_argument("--aggregate-edges", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--label-mode", choices=["none", "root-only", "top-value", "top-degree", "all"], default="root-only")
    p.add_argument("--max-visible-labels", type=int, default=50)
    p.add_argument("--max-render-nodes", type=int, default=500)
    p.add_argument("--max-render-edges", type=int, default=1000)
    p.add_argument("--top-k-edges-by-value", type=int, default=1000)
    p.add_argument("--top-k-nodes-by-value", type=int, default=500)
    p.add_argument("--depth-snapshots", help="Comma-separated max-depth snapshots to render, e.g. 1,2,3,5,10,20")
    p.add_argument("--hide-small-edges-below-usd", type=Decimal, default=None)
    p.add_argument("--collapse-leaf-sources", action="store_true")
    p.add_argument("--collapse-by-depth-after", type=int, default=None)
    return p.parse_args()


def render_outputs(output_html: Path, edges: list[dict[str, Any]], nodes: dict[str, dict[str, Any]], root_address: str, args: argparse.Namespace) -> tuple[int, int, list[Path]]:
    render_edges, render_nodes, render_note = simplify_for_render(edges, nodes, root_address, args)
    generated: list[Path] = []
    modes = ["hierarchical", "sankey", "radial-depth", "spring"] if args.layout_mode == "all" else [args.layout_mode]
    for mode in modes:
        out = output_path_for_mode(output_html, mode, args.layout_mode)
        if mode == "sankey":
            build_sankey_graph(out, render_edges, render_nodes, root_address, render_note)
        else:
            build_network_graph(out, render_edges, render_nodes, root_address, mode, args.label_mode, args.max_visible_labels, render_note)
        generated.append(out)
        print(f"[OUTPUT] {mode} graph: {out}")

    snapshots = parse_depth_snapshots(args.depth_snapshots)
    for depth in snapshots:
        snap_edges, snap_nodes, snap_note = simplify_for_render(edges, nodes, root_address, args, max_depth_filter=depth)
        out = depth_snapshot_path(output_html, depth)
        build_network_graph(out, snap_edges, snap_nodes, root_address, "hierarchical", args.label_mode, args.max_visible_labels, snap_note, title_suffix=f" (depth ≤ {depth})")
        generated.append(out)
        print(f"[OUTPUT] depth snapshot {depth}: {out}")
    return len(render_nodes), len(render_edges), generated


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

    print(f"[INFO] selected layout mode: {args.layout_mode}")
    print(f"[INFO] aggregate edges for visualization: {args.aggregate_edges}")
    print(f"[INFO] max render nodes/edges: {args.max_render_nodes}/{args.max_render_edges}")

    token_metadata = load_token_metadata(token_meta_path)

    index_dbs = load_index_databases(index_dir)
    if not index_dbs:
        raise SystemExit(f"No index DB files found in {index_dir}")
    print(f"Loaded {len(index_dbs)} index databases")

    ctx = TraceContext(data_root, args.max_branches_per_address, args.max_depth, args.source, token_metadata, args.min_usd_value)
    trace_address(ctx, index_dbs, addr, args.cutoff_block, 0)

    output_html = Path(args.output)
    output_dir = output_html.parent if output_html.parent != Path("") else Path(".")
    output_dir.mkdir(parents=True, exist_ok=True)

    nodes = compute_node_metadata(ctx.edge_rows, addr)
    edges_csv = output_dir / "edges.csv"
    nodes_csv = output_dir / "nodes.csv"
    json_out = output_dir / "trace_result.json"
    summary_txt = output_dir / "graph_summary.txt"
    summary_json = output_dir / "graph_summary.json"

    write_edges_csv(edges_csv, ctx.edge_rows)
    write_nodes_csv(nodes_csv, nodes)
    write_json(json_out, addr, ctx.edge_rows, nodes, args)
    rendered_nodes, rendered_edges, generated_graphs = render_outputs(output_html, ctx.edge_rows, nodes, addr, args)
    summary = build_summary(addr, ctx.edge_rows, nodes, args, rendered_nodes, rendered_edges)
    write_summary_files(summary_txt, summary_json, summary)

    print("\nDone.")
    print(f"[INFO] total traced nodes/edges: {len(nodes)}/{len(ctx.edge_rows)}")
    print(f"[INFO] max depth reached: {summary['max_depth_actually_reached']}")
    print(f"[INFO] rendered nodes/edges: {rendered_nodes}/{rendered_edges}")
    print(f"[INFO] generated depth snapshots: {parse_depth_snapshots(args.depth_snapshots)}")
    print(f"Edges CSV: {edges_csv}")
    print(f"Nodes CSV: {nodes_csv}")
    print(f"JSON: {json_out}")
    print(f"Summary TXT: {summary_txt}")
    print(f"Summary JSON: {summary_json}")
    print("Graphs:")
    for graph in generated_graphs:
        print(f"  {graph}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
