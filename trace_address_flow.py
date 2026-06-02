#!/usr/bin/env python3
"""Backward Ethereum address flow tracer with depth-aware Plotly visualizations.

Starting from a target/root address, this script traces incoming transfers where
that address is in the `to` field. It then recursively traces where each source
address previously received funds/tokens. The result is a directed value-flow
trace from upstream sources toward the root/target address.
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

try:
    import networkx as nx
except ImportError:  # Optional: spring layout falls back to hierarchical.
    nx = None

try:
    import plotly.graph_objects as go
except ImportError:  # Optional for CSV/JSON-only runs and test environments.
    go = None

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

EDGE_FIELDS = [
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

NODE_FIELDS = [
    "address",
    "depth",
    "node_type",
    "total_incoming_value_usd",
    "total_outgoing_value_usd",
    "incoming_edge_count",
    "outgoing_edge_count",
    "first_seen_block",
    "last_seen_block",
]

LAYOUT_MODES = ("hierarchical", "sankey", "radial-depth", "spring", "all")
LABEL_MODES = ("none", "root-only", "top-value", "top-degree", "all")


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
        min_usd_value: Optional[Decimal],
        eth_usd_price: Decimal,
    ):
        self.data_root = data_root
        self.max_branches = max_branches_per_address
        self.max_depth = max_depth
        self.source_filter = source
        self.token_metadata = token_metadata
        self.min_usd_value = min_usd_value
        self.eth_usd_price = eth_usd_price

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


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s or s.lower() in {"nan", "null", "none", "unknown"}:
        return None
    if s.startswith("$"):
        s = s[1:]
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def decimal_to_str(value: Optional[Decimal]) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f")


def usd_to_float(value: Any) -> Optional[float]:
    d = parse_decimal(value)
    if d is None:
        return None
    try:
        return float(d)
    except (OverflowError, ValueError):
        return None


def short_address(address: str) -> str:
    if len(address) <= 14:
        return address
    return f"{address[:6]}...{address[-4:]}"


def detect_column(fieldnames: list[str], candidates: tuple[str, ...]) -> Optional[str]:
    norm = {c.strip().lower(): c for c in fieldnames if c}
    for c in candidates:
        if c in norm:
            return norm[c]
    return None


def parse_numeric_value(value: Any) -> Optional[Decimal]:
    return parse_decimal(value)


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
        with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                print(f"[WARN] token metadata missing header: {path}")
                return out

            token_col = detect_column(reader.fieldnames, ("token_address", "contract_address", "token"))
            name_col = detect_column(reader.fieldnames, ("token_name", "name"))
            sym_col = detect_column(reader.fieldnames, ("token_symbol", "symbol"))
            dec_col = detect_column(reader.fieldnames, ("decimals", "decimal", "token_decimals"))
            rate_col = detect_column(reader.fieldnames, ("median_exchange_rate_USD", "median_exchange_rate_usd", "usd_price", "price_usd"))

            if not token_col:
                print(f"[WARN] token metadata missing token address column: {path}")
                return out

            for row in reader:
                token_addr = normalize_address(row.get(token_col))
                if not token_addr:
                    continue
                decimals: Optional[int] = None
                dec_raw = row.get(dec_col) if dec_col else None
                if dec_raw is not None and str(dec_raw).strip() != "":
                    try:
                        decimals = int(str(dec_raw).strip())
                    except Exception:
                        decimals = None
                rate = parse_decimal(row.get(rate_col)) if rate_col else None
                out[token_addr] = {
                    "token_name": str(row.get(name_col) or "").strip() if name_col else "",
                    "token_symbol": str(row.get(sym_col) or "").strip() if sym_col else "",
                    "decimals": decimals,
                    "median_exchange_rate_USD": rate,
                }
    except Exception as exc:
        print(f"[WARN] failed to load token metadata {path}: {exc}")
    print(f"Loaded token metadata entries: {len(out)}")
    return out


def load_index_databases(index_dir: Path) -> list[Path]:
    dbs = [p for p in index_dir.rglob("address_block_index_*.sqlite") if p.is_file()]

    def key(p: Path):
        m = DB_RE.match(p.name)
        if not m:
            return (10**18, 10**18, str(p))
        return (int(m.group(1)), int(m.group(2)), str(p))

    return sorted(dbs, key=key)


def query_incoming_occurrences(index_dbs: list[Path], address: str, cutoff_block: int, source_filter: str) -> list[Occurrence]:
    out: list[Occurrence] = []
    for db in index_dbs:
        if not db.exists():
            print(f"[WARN] Missing DB: {db}")
            continue
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
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


def extract_edge_asset_metadata(
    row: dict[str, Any],
    source: str,
    token_metadata: dict[str, dict[str, Any]],
    eth_usd_price: Decimal,
) -> dict[str, Any]:
    raw_val_dec = parse_numeric_value(row.get("raw_value"))
    if source == "transaction":
        decimals = 18
        value_display = format_amount(raw_val_dec, decimals)
        usd_value = (raw_val_dec / (Decimal(10) ** decimals) * eth_usd_price) if raw_val_dec is not None else None
        return {
            "asset_address": "ETH",
            "asset_symbol": "ETH",
            "token_name": "Ether",
            "token_decimals": decimals,
            "raw_value": "" if raw_val_dec is None else format(raw_val_dec, "f"),
            "value_display": value_display,
            "usd_value": decimal_to_str(usd_value),
        }

    token_addr = normalize_address(row.get("token_address"))
    token_addr_out = token_addr if token_addr else ""
    meta = token_metadata.get(token_addr_out, {}) if token_addr_out else {}
    decimals = meta.get("decimals")
    decimals_int = decimals if isinstance(decimals, int) else None
    value_display = format_amount(raw_val_dec, decimals_int)
    symbol = (meta.get("token_symbol") or "").strip() if isinstance(meta.get("token_symbol"), str) else ""
    if not symbol:
        symbol = token_addr_out if token_addr_out else "UNKNOWN_TOKEN"
    rate = meta.get("median_exchange_rate_USD")
    usd_value = None
    if raw_val_dec is not None and decimals_int is not None and isinstance(rate, Decimal):
        usd_value = (raw_val_dec / (Decimal(10) ** decimals_int)) * rate

    return {
        "asset_address": token_addr_out,
        "asset_symbol": symbol,
        "token_name": (meta.get("token_name") or "") if isinstance(meta.get("token_name"), str) else "",
        "token_decimals": decimals_int if decimals_int is not None else "",
        "raw_value": "" if raw_val_dec is None else format(raw_val_dec, "f"),
        "value_display": value_display,
        "usd_value": decimal_to_str(usd_value),
    }


def build_edge_hover_text(edge: dict[str, Any]) -> str:
    return (
        f"<b>FLOW: {edge.get('from_address','')} → {edge.get('to_address','')}</b><br>"
        f"Block: {edge.get('block_number','')}<br>"
        f"Depth: {edge.get('depth','')}<br>"
        f"Source: {edge.get('source','')}<br>"
        f"Asset: {edge.get('asset_symbol','')}<br>"
        f"Asset Address: {edge.get('asset_address','')}<br>"
        f"Token Name: {edge.get('token_name') or ''}<br>"
        f"Value: {edge.get('value_display','')} {edge.get('asset_symbol','')}<br>"
        f"Raw Value: {edge.get('raw_value','')}<br>"
        f"USD Value: {edge.get('usd_value') or 'unknown'}<br>"
        f"Transaction Hash: {edge.get('transaction_hash') or ''}<br>"
        f"CSV File: {edge.get('csv_file_path','')}<br>"
        f"CSV Row: {edge.get('csv_row_number','')}<br>"
        f"Transfers Aggregated: {edge.get('transfer_count', 1)}"
    )


def scan_csv_for_incoming_rows(
    csv_path: Path,
    target_address: str,
    block_number: int,
    source: str,
    token_metadata: dict[str, dict[str, Any]],
    eth_usd_price: Decimal,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with csv_path.open("r", newline="", encoding="utf-8-sig", errors="replace") as f:
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
                    asset_meta = extract_edge_asset_metadata(edge_base, source, token_metadata, eth_usd_price)
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
                            "usd_value": asset_meta["usd_value"],
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


def edge_passes_min_usd(edge: dict[str, Any], min_usd_value: Optional[Decimal]) -> bool:
    if min_usd_value is None:
        return True
    usd_val = parse_decimal(edge.get("usd_value"))
    return usd_val is not None and usd_val >= min_usd_value


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

        matches = scan_csv_for_incoming_rows(
            csv_path, address, occ.block_number, occ.source, ctx.token_metadata, ctx.eth_usd_price
        )
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
            if not edge_passes_min_usd(edge, ctx.min_usd_value):
                continue
            if add_edge(ctx, edge):
                print(
                    f"  added edge {edge['from_address']} -> {edge['to_address']} depth={edge['depth']} "
                    f"@ {edge['block_number']} value={edge.get('value_display','')} {edge.get('asset_symbol','')} "
                    f"usd={edge.get('usd_value') or 'unknown'}"
                )
                expanded += 1
                trace_address(ctx, index_dbs, edge["from_address"], edge["block_number"], depth + 1)
                if expanded >= ctx.max_branches:
                    break


def compute_node_metadata(edges: list[dict[str, Any]], root: str) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}

    def ensure(address: str) -> dict[str, Any]:
        if address not in nodes:
            nodes[address] = {
                "address": address,
                "depth": 0 if address == root else sys.maxsize,
                "node_type": "root" if address == root else "intermediate",
                "total_incoming_value_usd": Decimal(0),
                "total_outgoing_value_usd": Decimal(0),
                "incoming_edge_count": 0,
                "outgoing_edge_count": 0,
                "first_seen_block": None,
                "last_seen_block": None,
            }
        return nodes[address]

    ensure(root)
    for edge in edges:
        f = edge["from_address"]
        t = edge["to_address"]
        depth = int(edge.get("depth") or 0)
        src = ensure(f)
        dst = ensure(t)
        src["depth"] = min(src["depth"], depth)
        dst["depth"] = min(dst["depth"], max(0, depth - 1))
        src["outgoing_edge_count"] += 1
        dst["incoming_edge_count"] += 1
        usd = parse_decimal(edge.get("usd_value"))
        if usd is not None:
            src["total_outgoing_value_usd"] += usd
            dst["total_incoming_value_usd"] += usd
        block = edge.get("block_number")
        try:
            block_int = int(block)
        except Exception:
            block_int = None
        if block_int is not None:
            for node in (src, dst):
                node["first_seen_block"] = block_int if node["first_seen_block"] is None else min(node["first_seen_block"], block_int)
                node["last_seen_block"] = block_int if node["last_seen_block"] is None else max(node["last_seen_block"], block_int)

    for address, node in nodes.items():
        if node["depth"] == sys.maxsize:
            node["depth"] = 0 if address == root else 1
        if address == root:
            node["node_type"] = "root"
        elif node["incoming_edge_count"] == 0 and node["outgoing_edge_count"] > 0:
            node["node_type"] = "leaf_source"
        else:
            node["node_type"] = "intermediate"
    return nodes


def serialize_node(node: dict[str, Any]) -> dict[str, Any]:
    out = dict(node)
    out["total_incoming_value_usd"] = decimal_to_str(out.get("total_incoming_value_usd"))
    out["total_outgoing_value_usd"] = decimal_to_str(out.get("total_outgoing_value_usd"))
    out["first_seen_block"] = "" if out.get("first_seen_block") is None else out.get("first_seen_block")
    out["last_seen_block"] = "" if out.get("last_seen_block") is None else out.get("last_seen_block")
    return out


def write_edges_csv(path: Path, edges: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EDGE_FIELDS)
        w.writeheader()
        for e in edges:
            w.writerow({k: e.get(k, "") for k in EDGE_FIELDS})


def write_nodes_csv(path: Path, nodes: dict[str, dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=NODE_FIELDS)
        w.writeheader()
        for n in sorted(nodes.values(), key=lambda x: (int(x["depth"]), x["address"])):
            row = serialize_node(n)
            w.writerow({k: row.get(k, "") for k in NODE_FIELDS})


def write_json(path: Path, start_address: str, edges: list[dict[str, Any]], nodes: dict[str, dict[str, Any]], args: argparse.Namespace) -> None:
    obj = {
        "start_address": start_address,
        "parameters": vars(args),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": [serialize_node(n) for n in nodes.values()],
        "edges": edges,
    }
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def edge_weight(edge: dict[str, Any]) -> float:
    usd = usd_to_float(edge.get("usd_value"))
    if usd is not None and usd > 0:
        return usd
    amount = usd_to_float(edge.get("value_display"))
    if amount is not None and amount > 0:
        return amount
    return float(edge.get("transfer_count", 1) or 1)


def aggregate_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in edges:
        key = (edge["from_address"], edge["to_address"], edge.get("asset_symbol") or "")
        if key not in grouped:
            grouped[key] = dict(edge)
            grouped[key]["transfer_count"] = 0
            grouped[key]["_usd_sum"] = Decimal(0)
            grouped[key]["_value_sum"] = Decimal(0)
            grouped[key]["_blocks"] = []
        g = grouped[key]
        g["transfer_count"] += 1
        usd = parse_decimal(edge.get("usd_value"))
        if usd is not None:
            g["_usd_sum"] += usd
        val = parse_decimal(edge.get("value_display"))
        if val is not None:
            g["_value_sum"] += val
        try:
            g["_blocks"].append(int(edge.get("block_number")))
        except Exception:
            pass
        g["depth"] = max(int(g.get("depth") or 0), int(edge.get("depth") or 0))
    out = []
    for g in grouped.values():
        if g["_usd_sum"] > 0:
            g["usd_value"] = decimal_to_str(g["_usd_sum"])
        if g["_value_sum"] > 0:
            g["value_display"] = decimal_to_str(g["_value_sum"])
        if g["_blocks"]:
            g["block_number"] = f"{min(g['_blocks'])}-{max(g['_blocks'])}" if len(set(g["_blocks"])) > 1 else str(g["_blocks"][0])
        g["transaction_hash"] = "aggregated"
        g["csv_file_path"] = "multiple" if g["transfer_count"] > 1 else g.get("csv_file_path", "")
        g["csv_row_number"] = ""
        g.pop("_usd_sum", None)
        g.pop("_value_sum", None)
        g.pop("_blocks", None)
        out.append(g)
    return out


def apply_visual_simplification(
    edges: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    visual_edges = []
    for edge in edges:
        if args.hide_small_edges_below_usd is not None:
            usd = usd_to_float(edge.get("usd_value"))
            if usd is not None and usd < float(args.hide_small_edges_below_usd):
                continue
        visual_edges.append(dict(edge))

    def mapped(address: str) -> str:
        node = nodes.get(address, {})
        depth = int(node.get("depth", 0) or 0)
        if args.collapse_by_depth_after is not None and depth > args.collapse_by_depth_after:
            return f"Collapsed sources deeper than depth {args.collapse_by_depth_after}"
        if args.collapse_leaf_sources and node.get("node_type") == "leaf_source":
            return f"Other leaf sources at depth {depth}"
        return address

    collapsed: list[dict[str, Any]] = []
    for edge in visual_edges:
        e = dict(edge)
        e["from_address"] = mapped(e["from_address"])
        e["to_address"] = mapped(e["to_address"])
        collapsed.append(e)
    if args.aggregate_edges:
        collapsed = aggregate_edges(collapsed)
    collapsed_nodes = compute_node_metadata(collapsed, args.address_normalized)
    return collapsed, collapsed_nodes


def select_render_subset(
    edges: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    # Keep top-value nodes and top-value edges in the visualization preview only.
    ranked_nodes = sorted(
        nodes.values(),
        key=lambda n: (parse_decimal(n.get("total_incoming_value_usd")) or Decimal(0)) + (parse_decimal(n.get("total_outgoing_value_usd")) or Decimal(0)),
        reverse=True,
    )
    keep_nodes = {args.address_normalized}
    keep_nodes.update(n["address"] for n in ranked_nodes[: min(args.max_render_nodes, args.top_k_nodes_by_value)])
    ranked_edges = sorted(edges, key=edge_weight, reverse=True)[: min(args.max_render_edges, args.top_k_edges_by_value)]
    for edge in ranked_edges:
        keep_nodes.add(edge["from_address"])
        keep_nodes.add(edge["to_address"])
    keep_nodes = set(list(keep_nodes)[: args.max_render_nodes])
    render_edges = [e for e in ranked_edges if e["from_address"] in keep_nodes and e["to_address"] in keep_nodes]
    render_nodes = {addr: nodes[addr] for addr in keep_nodes if addr in nodes}
    return render_edges[: args.max_render_edges], render_nodes


def node_hover_text(node: dict[str, Any]) -> str:
    return (
        f"<b>{node['address']}</b><br>"
        f"Depth: {node.get('depth','')}<br>"
        f"Node type: {node.get('node_type','')}<br>"
        f"Incoming edges: {node.get('incoming_edge_count',0)}<br>"
        f"Outgoing edges: {node.get('outgoing_edge_count',0)}<br>"
        f"Incoming USD: {decimal_to_str(node.get('total_incoming_value_usd'))}<br>"
        f"Outgoing USD: {decimal_to_str(node.get('total_outgoing_value_usd'))}<br>"
        f"First seen block: {node.get('first_seen_block') or ''}<br>"
        f"Last seen block: {node.get('last_seen_block') or ''}"
    )


def label_addresses(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], root: str, args: argparse.Namespace) -> set[str]:
    if args.label_mode == "none":
        return set()
    if args.label_mode == "all":
        return set(nodes)
    labels = {root} if root in nodes and args.label_mode in {"root-only", "top-value", "top-degree"} else set()
    if args.label_mode == "root-only":
        return labels
    if args.label_mode == "top-value":
        ranked = sorted(
            nodes.values(),
            key=lambda n: (n.get("total_incoming_value_usd") or Decimal(0)) + (n.get("total_outgoing_value_usd") or Decimal(0)),
            reverse=True,
        )
    else:
        ranked = sorted(nodes.values(), key=lambda n: n.get("incoming_edge_count", 0) + n.get("outgoing_edge_count", 0), reverse=True)
    for node in ranked:
        labels.add(node["address"])
        if len(labels) >= args.max_visible_labels:
            break
    return labels


def hierarchical_positions(nodes: dict[str, dict[str, Any]]) -> dict[str, tuple[float, float]]:
    by_depth: dict[int, list[str]] = defaultdict(list)
    for addr, node in nodes.items():
        by_depth[int(node.get("depth", 0) or 0)].append(addr)
    max_depth = max(by_depth) if by_depth else 0
    pos = {}
    for depth, addresses in by_depth.items():
        addresses.sort()
        n = len(addresses)
        for i, addr in enumerate(addresses):
            x = max_depth - depth  # deepest sources left, root/direct targets right
            y = (i - (n - 1) / 2) * max(0.8, min(2.5, 20 / max(1, n)))
            pos[addr] = (float(x), float(y))
    return pos


def radial_depth_positions(nodes: dict[str, dict[str, Any]], root: str) -> dict[str, tuple[float, float]]:
    by_depth: dict[int, list[str]] = defaultdict(list)
    for addr, node in nodes.items():
        by_depth[int(node.get("depth", 0) or 0)].append(addr)
    pos = {root: (0.0, 0.0)}
    for depth, addresses in sorted(by_depth.items()):
        addresses = [a for a in sorted(addresses) if a != root]
        if not addresses:
            continue
        radius = max(1.5, depth * 1.8)
        for i, addr in enumerate(addresses):
            angle = 2 * math.pi * i / len(addresses)
            pos[addr] = (radius * math.cos(angle), radius * math.sin(angle))
    return pos


def spring_positions(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    if nx is None:
        print("[WARN] networkx is not installed; spring layout falling back to hierarchical")
        return hierarchical_positions(nodes)
    g = nx.DiGraph()
    g.add_nodes_from(nodes)
    for e in edges:
        g.add_edge(e["from_address"], e["to_address"])
    try:
        layout = nx.spring_layout(g, seed=42, k=1 / math.sqrt(max(1, len(nodes))))
        return {k: (float(v[0]), float(v[1])) for k, v in layout.items()}
    except Exception as exc:
        print(f"[WARN] spring layout failed, falling back to hierarchical: {exc}")
        return hierarchical_positions(nodes)


def write_plotly_missing_html(output_path: Path, title: str, edges: list[dict[str, Any]], nodes: dict[str, dict[str, Any]]) -> None:
    rows = "".join(
        f"<tr><td>{short_address(e.get('from_address', ''))}</td><td>{short_address(e.get('to_address', ''))}</td>"
        f"<td>{e.get('depth', '')}</td><td>{e.get('asset_symbol', '')}</td><td>{e.get('value_display', '')}</td>"
        f"<td>{e.get('usd_value') or 'unknown'}</td></tr>"
        for e in edges[:100]
    )
    output_path.write_text(
        f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:Arial,sans-serif;margin:24px;">
<h1>{title}</h1>
<p><strong>Plotly is not installed in this Python environment.</strong> Install it with <code>python3 -m pip install plotly</code> to render the interactive graph.</p>
<p>Trace data was still generated: nodes={len(nodes)}, edges={len(edges)}. See <code>edges.csv</code>, <code>nodes.csv</code>, and <code>trace_result.json</code>.</p>
<table border="1" cellpadding="4" cellspacing="0"><thead><tr><th>From</th><th>To</th><th>Depth</th><th>Asset</th><th>Value</th><th>USD</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>
""",
        encoding="utf-8",
    )


def build_network_figure(
    output_path: Path,
    edges: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    all_node_count: int,
    all_edge_count: int,
    root: str,
    args: argparse.Namespace,
    mode: str,
) -> None:
    if go is None:
        write_plotly_missing_html(output_path, f"Address Flow Trace - {mode}", edges, nodes)
        return

    if mode == "radial-depth":
        pos = radial_depth_positions(nodes, root)
        title = "Address Flow Trace - Radial Depth Layout"
    elif mode == "spring":
        pos = spring_positions(nodes, edges)
        title = "Address Flow Trace - Spring Layout"
    else:
        pos = hierarchical_positions(nodes)
        title = "Address Flow Trace - Hierarchical Depth Layout"

    if not nodes:
        fig = go.Figure()
        fig.update_layout(title=f"{title} (no nodes)")
        fig.write_html(str(output_path), include_plotlyjs="cdn")
        return

    weights = [edge_weight(e) for e in edges] or [1.0]
    max_w = max(weights) if weights else 1.0
    edge_traces = []
    arrow_x, arrow_y, arrow_text, arrow_size, arrow_color = [], [], [], [], []
    annotations = []
    top_arrow_edges = sorted(edges, key=edge_weight, reverse=True)[: min(200, len(edges))]
    top_arrow_ids = {id(e) for e in top_arrow_edges}

    for edge in edges:
        u, v = edge["from_address"], edge["to_address"]
        if u not in pos or v not in pos:
            continue
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        weight = edge_weight(edge)
        width = 0.6 + 5.0 * math.sqrt(weight / max_w)
        color = f"rgba(49, 130, 189, {0.18 + 0.55 * math.sqrt(weight / max_w):.3f})"
        edge_traces.append(
            go.Scatter(
                x=[x0, x1],
                y=[y0, y1],
                mode="lines",
                line=dict(width=width, color=color),
                hoverinfo="none",
                showlegend=False,
            )
        )
        mx, my = (x0 * 0.35 + x1 * 0.65), (y0 * 0.35 + y1 * 0.65)
        arrow_x.append(mx)
        arrow_y.append(my)
        arrow_text.append(build_edge_hover_text(edge))
        arrow_size.append(7 + min(18, width * 2))
        arrow_color.append(color)
        if id(edge) in top_arrow_ids:
            annotations.append(
                dict(
                    ax=x0,
                    ay=y0,
                    x=x1,
                    y=y1,
                    xref="x",
                    yref="y",
                    axref="x",
                    ayref="y",
                    showarrow=True,
                    arrowhead=3,
                    arrowsize=1.2,
                    arrowwidth=max(1, min(4, width)),
                    arrowcolor=color,
                    opacity=0.75,
                )
            )

    labels = label_addresses(nodes, edges, root, args)
    node_x, node_y, node_text, node_label, node_color, node_size, node_symbol = [], [], [], [], [], [], []
    max_depth = max(int(n.get("depth", 0) or 0) for n in nodes.values()) if nodes else 1
    for addr, node in nodes.items():
        x, y = pos.get(addr, (0.0, 0.0))
        depth = int(node.get("depth", 0) or 0)
        value = float((node.get("total_incoming_value_usd") or Decimal(0)) + (node.get("total_outgoing_value_usd") or Decimal(0)))
        degree = int(node.get("incoming_edge_count", 0) or 0) + int(node.get("outgoing_edge_count", 0) or 0)
        node_x.append(x)
        node_y.append(y)
        node_text.append(node_hover_text(node))
        node_label.append(short_address(addr) if addr in labels else "")
        if addr == root:
            node_color.append("#d62728")
            node_size.append(30)
            node_symbol.append("star")
        elif node.get("node_type") == "leaf_source":
            node_color.append("#2ca02c")
            node_size.append(10 + min(14, math.sqrt(max(1, degree + value / 1000))))
            node_symbol.append("diamond")
        else:
            # Depth-based colors without requiring a numeric colorbar; this keeps
            # root/leaf colors distinctive and avoids mixed numeric/string color arrays.
            hue = int((depth * 47) % 360)
            node_color.append(f"hsl({hue}, 70%, 48%)")
            node_size.append(9 + min(16, math.sqrt(max(1, degree + value / 1000))))
            node_symbol.append("circle")

    edge_mid_trace = go.Scatter(
        x=arrow_x,
        y=arrow_y,
        mode="markers",
        marker=dict(size=arrow_size, color=arrow_color, symbol="triangle-right", line=dict(width=0)),
        hoverinfo="text",
        hovertext=arrow_text,
        name="edge direction / hover for flow",
        showlegend=False,
    )
    node_trace = go.Scatter(
        x=node_x,
        y=node_y,
        mode="markers+text",
        text=node_label,
        textposition="top center",
        hoverinfo="text",
        hovertext=node_text,
        marker=dict(
            size=node_size,
            color=node_color,
            line=dict(width=1.5, color="#222"),
            symbol=node_symbol,
        ),
        name="addresses",
        showlegend=False,
    )

    rendered_note = (
        f"Rendered {len(nodes)} of {all_node_count} nodes and {len(edges)} of {all_edge_count} edges. "
        "Full data is available in edges.csv and trace_result.json."
    )
    fig = go.Figure(data=edge_traces + [edge_mid_trace, node_trace])
    fig.update_layout(
        title=f"{title}<br><sup>{rendered_note}</sup>",
        annotations=annotations + [
            dict(
                text="Flow direction: upstream source addresses → root/target. Root is red star; leaf sources are green diamonds; other colors represent depth.",
                xref="paper",
                yref="paper",
                x=0,
                y=1.08,
                showarrow=False,
                align="left",
            )
        ],
        showlegend=False,
        hovermode="closest",
        xaxis=dict(showgrid=True, zeroline=False, title="Depth layer (deeper sources on left, root on right)" if mode == "hierarchical" else ""),
        yaxis=dict(showgrid=True, zeroline=False),
        margin=dict(l=30, r=30, t=100, b=30),
        height=900,
    )
    fig.write_html(str(output_path), include_plotlyjs="cdn")


def build_sankey_figure(
    output_path: Path,
    edges: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    all_node_count: int,
    all_edge_count: int,
) -> None:
    if go is None:
        write_plotly_missing_html(output_path, "Address Flow Trace - Sankey", edges, nodes)
        return

    sankey_edges = aggregate_edges(edges)
    addresses = sorted({e["from_address"] for e in sankey_edges} | {e["to_address"] for e in sankey_edges}, key=lambda a: nodes.get(a, {}).get("depth", 0))
    idx = {addr: i for i, addr in enumerate(addresses)}
    labels = [short_address(a) for a in addresses]
    values = [max(0.001, edge_weight(e)) for e in sankey_edges]
    hover = [build_edge_hover_text(e) for e in sankey_edges]
    fig = go.Figure(
        data=[
            go.Sankey(
                arrangement="snap",
                node=dict(label=labels, pad=18, thickness=14, customdata=addresses, hovertemplate="%{customdata}<extra></extra>"),
                link=dict(
                    source=[idx[e["from_address"]] for e in sankey_edges],
                    target=[idx[e["to_address"]] for e in sankey_edges],
                    value=values,
                    customdata=hover,
                    hovertemplate="%{customdata}<extra></extra>",
                ),
            )
        ]
    )
    fig.update_layout(
        title=f"Address Flow Trace - Sankey<br><sup>Rendered {len(addresses)} of {all_node_count} nodes and {len(sankey_edges)} of {all_edge_count} edges.</sup>",
        height=900,
        margin=dict(l=30, r=30, t=80, b=30),
    )
    fig.write_html(str(output_path), include_plotlyjs="cdn")


def output_for_mode(base: Path, mode: str, multi: bool) -> Path:
    if not multi:
        return base
    suffix = {"radial-depth": "radial_depth"}.get(mode, mode)
    return base.with_name(f"{base.stem}_{suffix}{base.suffix}")


def filter_edges_to_depth(edges: list[dict[str, Any]], depth: int) -> list[dict[str, Any]]:
    return [e for e in edges if int(e.get("depth") or 0) <= depth]


def render_visualizations(base_output: Path, full_edges: list[dict[str, Any]], full_nodes: dict[str, dict[str, Any]], root: str, args: argparse.Namespace) -> dict[str, Any]:
    simplified_edges, simplified_nodes = apply_visual_simplification(full_edges, full_nodes, args)
    render_edges, render_nodes = select_render_subset(simplified_edges, simplified_nodes, args)
    mode_list = ["hierarchical", "sankey", "radial-depth", "spring"] if args.layout_mode == "all" else [args.layout_mode]
    multi = args.layout_mode == "all"
    generated = []
    for mode in mode_list:
        out = output_for_mode(base_output, mode, multi)
        print(f"[VIS] layout={mode} output={out} rendered_nodes={len(render_nodes)} rendered_edges={len(render_edges)} aggregate_edges={args.aggregate_edges}")
        if mode == "sankey":
            build_sankey_figure(out, render_edges, render_nodes, len(full_nodes), len(full_edges))
        else:
            build_network_figure(out, render_edges, render_nodes, len(full_nodes), len(full_edges), root, args, mode)
        generated.append(str(out))

    snapshot_outputs = []
    for depth in parse_depth_snapshots(args.depth_snapshots):
        snap_edges = filter_edges_to_depth(full_edges, depth)
        snap_nodes = compute_node_metadata(snap_edges, root)
        snap_simplified_edges, snap_simplified_nodes = apply_visual_simplification(snap_edges, snap_nodes, args)
        snap_render_edges, snap_render_nodes = select_render_subset(snap_simplified_edges, snap_simplified_nodes, args)
        out = base_output.with_name(f"{base_output.stem}_depth_{depth}{base_output.suffix}")
        build_network_figure(out, snap_render_edges, snap_render_nodes, len(snap_nodes), len(snap_edges), root, args, "hierarchical")
        print(f"[VIS] depth snapshot={depth} output={out} rendered_nodes={len(snap_render_nodes)} rendered_edges={len(snap_render_edges)}")
        snapshot_outputs.append(str(out))
    return {
        "rendered_nodes": len(render_nodes),
        "rendered_edges": len(render_edges),
        "outputs": generated,
        "depth_snapshot_outputs": snapshot_outputs,
    }


def parse_depth_snapshots(value: str) -> list[int]:
    if not value:
        return []
    out = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            depth = int(part)
            if depth >= 0:
                out.append(depth)
        except ValueError:
            print(f"[WARN] ignoring invalid depth snapshot: {part}")
    return sorted(set(out))


def top_nodes(nodes: dict[str, dict[str, Any]], field: str, limit: int = 20) -> list[dict[str, Any]]:
    ranked = sorted(nodes.values(), key=lambda n: n.get(field) or Decimal(0), reverse=True)[:limit]
    return [{"address": n["address"], "depth": n["depth"], field: decimal_to_str(n.get(field))} for n in ranked]


def graph_summary(root: str, edges: list[dict[str, Any]], nodes: dict[str, dict[str, Any]], args: argparse.Namespace, render_info: dict[str, Any]) -> dict[str, Any]:
    token_addresses = {e.get("asset_address") for e in edges if e.get("source") == "token_transfer" and e.get("asset_address")}
    assets = {e.get("asset_symbol") for e in edges if e.get("asset_symbol")}
    top_edges = sorted(edges, key=edge_weight, reverse=True)[:20]
    return {
        "root_address": root,
        "max_depth_requested": args.max_depth,
        "max_depth_reached": max((int(n.get("depth", 0) or 0) for n in nodes.values()), default=0),
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "rendered_nodes": render_info.get("rendered_nodes", 0),
        "rendered_edges": render_info.get("rendered_edges", 0),
        "leaf_source_nodes": sum(1 for n in nodes.values() if n.get("node_type") == "leaf_source"),
        "direct_source_nodes": sum(1 for n in nodes.values() if int(n.get("depth", 0) or 0) == 1),
        "top_20_addresses_by_incoming_value": top_nodes(nodes, "total_incoming_value_usd"),
        "top_20_addresses_by_outgoing_value": top_nodes(nodes, "total_outgoing_value_usd"),
        "top_20_edges_by_value": [
            {
                "from_address": e.get("from_address"),
                "to_address": e.get("to_address"),
                "depth": e.get("depth"),
                "asset_symbol": e.get("asset_symbol"),
                "usd_value": e.get("usd_value"),
                "value_display": e.get("value_display"),
                "block_number": e.get("block_number"),
            }
            for e in top_edges
        ],
        "eth_edges": sum(1 for e in edges if e.get("source") == "transaction" or e.get("asset_symbol") == "ETH"),
        "token_transfer_edges": sum(1 for e in edges if e.get("source") == "token_transfer"),
        "unique_token_addresses": len(token_addresses),
        "unique_assets": len(assets),
        "layout_mode": args.layout_mode,
        "aggregate_edges": args.aggregate_edges,
        "visualization_outputs": render_info.get("outputs", []),
        "depth_snapshot_outputs": render_info.get("depth_snapshot_outputs", []),
    }


def write_summary_files(output_dir: Path, summary: dict[str, Any]) -> tuple[Path, Path]:
    json_path = output_dir / "graph_summary.json"
    txt_path = output_dir / "graph_summary.txt"
    json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    lines = [
        f"root_address: {summary['root_address']}",
        f"max_depth_requested: {summary['max_depth_requested']}",
        f"max_depth_reached: {summary['max_depth_reached']}",
        f"total_nodes: {summary['total_nodes']}",
        f"total_edges: {summary['total_edges']}",
        f"rendered_nodes: {summary['rendered_nodes']}",
        f"rendered_edges: {summary['rendered_edges']}",
        f"leaf_source_nodes: {summary['leaf_source_nodes']}",
        f"direct_source_nodes: {summary['direct_source_nodes']}",
        f"eth_edges: {summary['eth_edges']}",
        f"token_transfer_edges: {summary['token_transfer_edges']}",
        f"unique_token_addresses: {summary['unique_token_addresses']}",
        f"unique_assets: {summary['unique_assets']}",
        f"layout_mode: {summary['layout_mode']}",
        f"aggregate_edges: {summary['aggregate_edges']}",
        "",
        "visualization_outputs:",
        *[f"  - {p}" for p in summary.get("visualization_outputs", [])],
        "",
        "depth_snapshot_outputs:",
        *[f"  - {p}" for p in summary.get("depth_snapshot_outputs", [])],
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, json_path


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Trace backward funding/source chain for an Ethereum address")
    # Existing arguments preserved.
    p.add_argument("--address", required=True)
    p.add_argument("--data-root", required=True)
    p.add_argument("--index-dir", required=True)
    p.add_argument("--token-metadata", help="Optional token metadata CSV with token_address/token_symbol/decimals")
    p.add_argument("--output", default="address_flow_graph.html")
    p.add_argument("--max-depth", type=positive_int, default=5)
    p.add_argument("--max-branches-per-address", type=positive_int, default=20)
    p.add_argument("--cutoff-block", type=int, default=10**18)
    p.add_argument("--source", choices=["all", "transaction", "token_transfer"], default="all")
    p.add_argument("--min-usd-value", type=Decimal, default=None)

    # New visualization/simplification controls.
    p.add_argument("--eth-usd-price", type=Decimal, default=Decimal("2000.0"))
    p.add_argument("--layout-mode", choices=LAYOUT_MODES, default="hierarchical")
    p.add_argument("--aggregate-edges", dest="aggregate_edges", action="store_true", default=True)
    p.add_argument("--no-aggregate-edges", dest="aggregate_edges", action="store_false")
    p.add_argument("--label-mode", choices=LABEL_MODES, default="root-only")
    p.add_argument("--max-visible-labels", type=positive_int, default=50)
    p.add_argument("--max-render-nodes", type=positive_int, default=500)
    p.add_argument("--max-render-edges", type=positive_int, default=1000)
    p.add_argument("--top-k-edges-by-value", type=positive_int, default=1000)
    p.add_argument("--top-k-nodes-by-value", type=positive_int, default=500)
    p.add_argument("--depth-snapshots", default="")
    p.add_argument("--hide-small-edges-below-usd", type=Decimal, default=None)
    p.add_argument("--collapse-leaf-sources", action="store_true")
    p.add_argument("--collapse-by-depth-after", type=positive_int, default=None)
    args = p.parse_args()
    args.address_normalized = normalize_address(args.address) or args.address.lower()
    return args


def main() -> int:
    configured_limit = configure_csv_field_limit()
    print(f"Configured csv.field_size_limit={configured_limit}")

    args = parse_args()
    addr = normalize_address(args.address)
    if not addr:
        raise SystemExit("Invalid --address. Must be non-empty and start with 0x")
    args.address_normalized = addr

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
    print(
        f"Selected layout_mode={args.layout_mode} aggregate_edges={args.aggregate_edges} "
        f"max_render_nodes={args.max_render_nodes} max_render_edges={args.max_render_edges}"
    )

    ctx = TraceContext(
        data_root,
        args.max_branches_per_address,
        args.max_depth,
        args.source,
        token_metadata,
        args.min_usd_value,
        args.eth_usd_price,
    )
    trace_address(ctx, index_dbs, addr, args.cutoff_block, 0)

    output_html = Path(args.output)
    output_dir = output_html.parent if output_html.parent != Path("") else Path(".")
    output_dir.mkdir(parents=True, exist_ok=True)

    edges_csv = output_dir / "edges.csv"
    nodes_csv = output_dir / "nodes.csv"
    json_out = output_dir / "trace_result.json"

    nodes = compute_node_metadata(ctx.edge_rows, addr)
    write_edges_csv(edges_csv, ctx.edge_rows)
    write_nodes_csv(nodes_csv, nodes)
    write_json(json_out, addr, ctx.edge_rows, nodes, args)
    render_info = render_visualizations(output_html, ctx.edge_rows, nodes, addr, args)
    summary = graph_summary(addr, ctx.edge_rows, nodes, args, render_info)
    summary_txt, summary_json = write_summary_files(output_dir, summary)

    print("\nDone.")
    print(f"Total traced nodes={len(nodes)} edges={len(ctx.edge_rows)} max_depth_reached={summary['max_depth_reached']}")
    print(f"Rendered nodes={summary['rendered_nodes']} edges={summary['rendered_edges']}")
    print(f"Graph outputs: {', '.join(render_info['outputs'])}")
    if render_info["depth_snapshot_outputs"]:
        print(f"Depth snapshots: {', '.join(render_info['depth_snapshot_outputs'])}")
    print(f"Edges CSV: {edges_csv}")
    print(f"Nodes CSV: {nodes_csv}")
    print(f"JSON: {json_out}")
    print(f"Summary TXT: {summary_txt}")
    print(f"Summary JSON: {summary_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
