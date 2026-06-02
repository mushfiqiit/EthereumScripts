# AddressTracing

`AddressTracing` traces Ethereum address activity from local extracted CSV files and produces:

- `address_trace_graph.html` — an interactive directed HTML transaction graph.
- `address_trace_edges.csv` — machine-readable edge data for every retained transfer.
- `address_trace_nodes.csv` — machine-readable node data with discovery depth and degree counts.

The graph starts from one root address, finds blocks containing that address through the SQLite address indexes, reads the original transaction/token-transfer CSV files for those blocks, adds transfer edges, then recursively repeats the process for newly discovered neighboring addresses.

## How the SQLite indexes are used

The existing `build_address_index.py` script creates SQLite files named like:

```text
NotUploadable/index/indexes_25112101_25119300/address_block_index_25112101_25112200.sqlite
```

Each SQLite file has an `occurrences` table with:

```text
address, block_number, role, source
```

`trace_address_graph.py` discovers all `address_block_index_*.sqlite` files under `--index-base-dir` and runs:

```sql
SELECT block_number, role, source FROM occurrences WHERE address = ?
```

The SQLite lookup is intentionally used only to quickly find candidate block numbers. The graph code still reads the original CSV files afterward because the index does not contain all edge metadata needed for graph labels, tooltips, transaction hashes, token addresses, raw values, decimals, or USD value estimates.

## CSV path mapping

For a block number, the script first constructs the expected path mathematically:

```text
Ethereum_TT_<outer_start>_<outer_end>/
  Transaction_TokenTransfer_<chunk_start>_<chunk_end>/
    transaction_<block>.csv
    token_transfer_<block>.csv
```

The defaults are:

- `--outer-range-size 7200`
- `--chunk-size 100`
- `--start-block 25112101`
- `--end-block 25205700`

If the expected CSV path does not exist, the script searches recursively inside the matching `Ethereum_TT_*_*` folder for the needed file.

## Transfer value formulas

### ETH transactions

For transaction CSV rows, `value` is raw wei. ETH has 18 decimals, so:

```text
ETH amount = value / 10^18
transfer_value_USD = (value / 10^18) * eth_usd_price
```

The default ETH price is `2000.0` USD and can be changed with `--eth-usd-price`.

### Token transfers

For token-transfer CSV rows, the script loads token metadata from:

```text
/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/TokenAddressSummary/token_address_frequency_metadata_with_exchange_rate_25191301_25205700.csv
```

For tokens with both a decimal and `median_exchange_rate_USD`, the formula is:

```text
human token amount = value / 10^decimal
transfer_value_USD = (value / 10^decimal) * median_exchange_rate_USD
```

This formula is correct under the assumptions that token median exchange rates are USD prices and the ETH price is fixed at the configured `--eth-usd-price` value. If a token transfer is missing its decimal or exchange-rate metadata, the edge is still included and `transfer_value_label` is set to `unknown`.

## Installation

From this directory:

```bash
python3 -m pip install -r requirements.txt
```

Dependencies are intentionally small:

- `pyvis` for interactive standalone HTML graph output.
- `networkx` is listed for compatibility with downstream graph workflows, while the current script keeps runtime graph bookkeeping in standard-library data structures.

## Run with the Bash wrapper

The wrapper defines all default absolute paths and safely quotes paths containing spaces, including `/media/dheeman/Seagate Backup Plus Drive1/`.

```bash
./run_address_trace_graph.sh 0xabc0000000000000000000000000000000000000
```

Additional Python arguments can be passed after the address. The script has built-in safety caps by default, but you can tighten them for quick runs:

```bash
./run_address_trace_graph.sh 0xabc0000000000000000000000000000000000000 \
  --max-depth 2 \
  --max-nodes 1000 \
  --max-edges 5000 \
  --min-usd 100 \
  --exclude-unknown-usd
```

## Run the Python script directly

```bash
python3 trace_address_graph.py \
  --root-address "0xabc0000000000000000000000000000000000000" \
  --data-base-dir "/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable" \
  --index-base-dir "/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable/index" \
  --token-metadata-csv "/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/TokenAddressSummary/token_address_frequency_metadata_with_exchange_rate_25191301_25205700.csv" \
  --start-block 25112101 \
  --end-block 25205700 \
  --eth-usd-price 2000.0 \
  --output-html "/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/AddressTracing/address_trace_graph.html" \
  --output-edges-csv "/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/AddressTracing/address_trace_edges.csv" \
  --output-nodes-csv "/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/AddressTracing/address_trace_nodes.csv"
```

## Traversal controls

By default, the script now uses conservative safety caps so an unexpectedly large connected component can still finish:

- `--max-depth 4`
- `--max-nodes 2000`
- `--max-edges 10000`

Use these options to tune runs:

- `--max-depth N` limits recursive BFS expansion to `N` hops from the root.
- `--max-nodes N` stops adding newly discovered addresses after `N` nodes.
- `--max-edges N` stops traversal after `N` retained edges.
- `--no-limits` disables the default caps and attempts full connected-component traversal. Use this carefully because a large component may run for a long time and produce an HTML file too large for a browser.
- `--min-usd-value N` or the shorter alias `--min-usd N` keeps only edges with a known USD value greater than or equal to `N`; known-value edges below `N` are not added to the graph or edge CSV.
- Token transfers with unknown USD value are still included by default so missing metadata does not hide activity. Add `--exclude-unknown-usd` together with `--min-usd`/`--min-usd-value` if you want the graph to contain only edges with known USD values at or above the threshold.

When a cap is reached, the final logs include the stop reason, such as `Traversal stop reason: --max-edges=10000 reached` or `Traversal stop reason: --max-nodes=2000 reached`. Even when capped, the script still writes the HTML, edge CSV, and node CSV for the partial graph discovered so far.

## Interpreting the graph

- Nodes are Ethereum addresses from `from_address` or `to_address` fields.
- Directed edges point from sender to receiver.
- The root node is larger, orange, and labeled `ROOT: <short-address>`.
- Other nodes use shortened labels such as `0x1234...abcd`.
- Edge labels show `block_number, transfer_value_label`.
- Hover over nodes and edges for full metadata, including full addresses, transaction hash, token symbol/address, raw value, decimal, exchange rate, CSV file, and USD estimate or `unknown`.
- The HTML graph supports zooming, panning, node dragging, directed arrows, hover tooltips, physics layout, and interaction controls. It also injects explicit `Zoom in`, `Zoom out`, and `Fit graph` buttons above the pyvis canvas. For larger graphs, edge labels are hidden in the interactive view to reduce clutter; full edge details remain available in hover tooltips and `address_trace_edges.csv`.
- The generated HTML includes a dependency-free radial SVG preview above the interactive pyvis canvas. It places the root in the center and groups discovered nodes by BFS-depth rings, which avoids the previous long single-column preview. The preview has its own zoom slider, zoom buttons, reset button, and scrollable viewport so dense graphs can be inspected more closely. If the interactive canvas is blank, the SVG preview and edge table still prove that graph data was generated and show the retained nodes/edges.
- Open the generated HTML as a downloaded/local file or through a web server/GitHub Pages. GitHub's normal repository file viewer displays HTML source and does not execute the graph JavaScript.
- The script asks pyvis to inline its JavaScript/CSS resources so `address_trace_graph.html` is portable. If your installed pyvis is too old to support inline resources and the browser opens a blank interactive canvas, upgrade pyvis with `python3 -m pip install --upgrade pyvis` or copy pyvis's generated `lib/` asset folder next to the HTML file.


## Understanding a very small trace

A successful run can still produce only one or two graph edges. That means the BFS queue was exhausted after the script followed every retained transfer it could find from the root within the configured block range and filters. It is not automatically an error if the final logs show small counts such as `Graph nodes discovered: 2` and `Graph edges discovered: 1`.

Useful log fields for diagnosing this are:

- `Query ... matched N unique block(s)`: how many indexed blocks contained the currently queried address.
- `Finished ... added_edges=... added_nodes=... enqueued_neighbors=... remaining_queue=...`: whether that address expanded the BFS frontier.
- `Duplicate edges skipped`: expected when the same transfer is seen once from the sender side and again from the receiver side.
- `Unknown transfer-value edges`: token transfers retained in the graph even though token decimal or median exchange-rate metadata was unavailable. Use `--exclude-unknown-usd` if you are applying `--min-usd` and want these removed too.
- `Traversal stop reason`: `queue exhausted` means normal completion; `--max-edges=... reached` or `--max-nodes=... reached` means a safety cap stopped or constrained traversal.

For example, if the root has one transfer to a neighbor, the neighbor's SQLite lookup returns only the same block, and that block contains no additional transfers involving the neighbor, traversal ends normally with one edge. Open `address_trace_edges.csv` to see exactly which transfer was retained and why its value is known or `unknown`.

## Large graph warning

Very large connected components can create HTML files that are slow or impractical in a browser. The CSV outputs are always generated and are often better inputs for large-scale tools such as Gephi or Cytoscape when the component has many thousands of nodes or edges.
