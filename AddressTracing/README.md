# Address tracing graph workflow

This directory contains an index-assisted workflow for tracing Ethereum address flows across the four supported 7,200-block CSV ranges:

- `Ethereum_TT_25415601_25422800`
- `Ethereum_TT_25422801_25430000`
- `Ethereum_TT_25430001_25437200`
- `Ethereum_TT_25437201_25444400`

The default CSV parent is:

```text
/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NewCSVs
```

The default SQLite index parent is:

```text
/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/AddressIndexes
```

## 1. Build an indexed graph

```bash
./AddressTracing/run_build_indexed_address_graph.sh \
  --address 0xSOURCE_ADDRESS \
  --max-depth 10 \
  --output-dir ./AddressTracing/output \
  --token-metadata-csv ./AddressTracing/token_metadata.csv
```

The output folder is source-specific and contains:

- `nodes.csv`
- `edges.csv`
- `graph.json`
- `manifest.json`

The builder uses the SQLite `occurrences` tables to locate relevant block numbers and then reads only the matching per-block `transaction_<block>.csv` or `token_transfer_<block>.csv` files. It does not scan every CSV file.

### Token decimals

Token-transfer CSV files do not contain decimals. Provide `--token-metadata-csv` with at least these columns:

```csv
token_address,decimals
0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48,6
```

The builder does not silently assume token decimals. If a token transfer is encountered without metadata, the command exits with a clear list of missing token addresses.

## 2. Import the graph to Neo4j

```bash
NEO4J_PASSWORD='your-password' ./AddressTracing/run_indexed_graph_neo4j.sh \
  --graph-dir ./AddressTracing/output/0xSOURCE_ADDRESS \
  --graph-id eth-25415601-25444400-source \
  --clear-existing
```

You can also pass `--uri`, `--user`, `--password`, and `--database`, or use `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, and `NEO4J_DATABASE`.

`--clear-existing` deletes only `Address` nodes with the specified `graph_id`; it does not clear the entire Neo4j database.

After import, the script prints the Neo4j Browser URL, imported counts, and useful visualization/filtering queries.
