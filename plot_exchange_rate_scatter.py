#!/usr/bin/env python3
"""Create an interactive, zoomable scatter plot for exchange-rate CSV outputs.

Expected input CSV columns:
- block_number
- exchange_rate

The script scans a directory of CSV files (typically produced by
`build_weth_usdc_exchange_rates.py`), combines all rows, and writes an
interactive HTML scatter plot using Plotly WebGL (`Scattergl`) so large datasets
remain responsive when zooming/panning.
"""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Tuple


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an interactive scatter plot of exchange_rate vs block_number."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("weth_usdc_exchange_rates"),
        help="Directory containing exchange-rate CSVs.",
    )
    parser.add_argument(
        "--pattern",
        default="*.csv",
        help="Glob pattern to select CSV files from --input-dir.",
    )
    parser.add_argument(
        "--output-html",
        type=Path,
        default=Path("weth_usdc_exchange_rate_scatter.html"),
        help="Output HTML file path.",
    )
    parser.add_argument(
        "--x-tick-step",
        type=int,
        default=50,
        help="Spacing between x-axis tick marks in blocks (default: 50).",
    )
    parser.add_argument(
        "--marker-size",
        type=float,
        default=3.0,
        help="Scatter marker size.",
    )
    parser.add_argument(
        "--marker-opacity",
        type=float,
        default=0.55,
        help="Scatter marker opacity between 0 and 1.",
    )
    parser.add_argument(
        "--y-min",
        type=float,
        default=1000.0,
        help="Lower y-axis bound (USDC per 1 WETH). Points below this are ignored.",
    )
    parser.add_argument(
        "--y-max",
        type=float,
        default=3000.0,
        help="Upper y-axis bound (USDC per 1 WETH). Points above this are ignored.",
    )
    return parser.parse_args()


def load_points(
    input_dir: Path, pattern: str, *, y_min: float, y_max: float
) -> Tuple[List[int], List[float], int, int]:
    files = sorted(path for path in input_dir.glob(pattern) if path.is_file())
    if not files:
        raise FileNotFoundError(
            f"No CSV files found in {input_dir} matching pattern {pattern!r}"
        )

    x_values: List[int] = []
    y_values: List[float] = []
    skipped_rows = 0
    out_of_range_rows = 0

    for csv_path in files:
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                skipped_rows += 1
                continue
            if "block_number" not in reader.fieldnames or "exchange_rate" not in reader.fieldnames:
                raise ValueError(
                    f"{csv_path} is missing required columns 'block_number' and/or 'exchange_rate'"
                )

            for row in reader:
                raw_block = (row.get("block_number") or "").strip()
                raw_rate = (row.get("exchange_rate") or "").strip()
                if not raw_block or not raw_rate:
                    skipped_rows += 1
                    continue

                try:
                    block_number = int(raw_block)
                    exchange_rate = float(Decimal(raw_rate))
                except (ValueError, InvalidOperation):
                    skipped_rows += 1
                    continue
                if not (y_min <= exchange_rate <= y_max):
                    out_of_range_rows += 1
                    continue

                x_values.append(block_number)
                y_values.append(exchange_rate)

    if not x_values:
        raise ValueError(
            "No valid points were loaded. Check the input CSVs and required columns."
        )

    return x_values, y_values, skipped_rows, out_of_range_rows


def main() -> None:
    args = parse_args()

    if args.x_tick_step <= 0:
        raise ValueError("--x-tick-step must be a positive integer")
    if not (0 < args.marker_opacity <= 1):
        raise ValueError("--marker-opacity must be in (0, 1]")
    if args.marker_size <= 0:
        raise ValueError("--marker-size must be > 0")
    if args.y_min >= args.y_max:
        raise ValueError("--y-min must be smaller than --y-max")

    if not args.input_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {args.input_dir}")

    try:
        import plotly.graph_objects as go
    except ImportError as exc:
        raise ImportError(
            "Plotly is required. Install it with: pip install plotly"
        ) from exc

    blocks, rates, skipped_rows, out_of_range_rows = load_points(
        args.input_dir, args.pattern, y_min=args.y_min, y_max=args.y_max
    )

    fig = go.Figure(
        data=[
            go.Scattergl(
                x=blocks,
                y=rates,
                mode="markers",
                marker={
                    "size": args.marker_size,
                    "opacity": args.marker_opacity,
                    "color": "#1f77b4",
                },
                hovertemplate="Block: %{x:d}<br>Exchange rate: %{y}<extra></extra>",
                name="USDC per 1 WETH",
            )
        ]
    )

    fig.update_layout(
        title="USDC-per-WETH Exchange Rates by Block",
        template="plotly_white",
        hovermode="closest",
        dragmode="zoom",
        xaxis={
            "title": "Block number",
            "dtick": args.x_tick_step,
            "tickformat": ",d",
            "hoverformat": ",d",
            "showspikes": True,
            "spikemode": "across",
            "spikesnap": "cursor",
        },
        yaxis={
            "title": "exchange_rate (USDC per 1 WETH)",
            "range": [args.y_min, args.y_max],
            "showspikes": True,
            "spikemode": "across",
            "spikesnap": "cursor",
        },
    )

    # Extra UI helpers for interactive exploration.
    fig.update_layout(
        modebar_add=["zoom2d", "pan2d", "lasso2d", "select2d", "autoScale2d", "resetScale2d"]
    )

    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(args.output_html, include_plotlyjs="cdn")

    print(f"Saved interactive plot to: {args.output_html}")
    print(f"Total plotted points: {len(blocks)}")
    print(f"Skipped malformed/empty rows: {skipped_rows}")
    print(f"Skipped out-of-range rows ({args.y_min}..{args.y_max}): {out_of_range_rows}")


if __name__ == "__main__":
    main()