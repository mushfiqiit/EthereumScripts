#!/usr/bin/env bash
set -euo pipefail

CODE_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/AddressTracing"
DATA_BASE_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable"
INDEX_BASE_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable/index"
TOKEN_METADATA_CSV="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/TokenAddressSummary/token_address_frequency_metadata_with_exchange_rate_25191301_25205700.csv"
OUTPUT_HTML="${CODE_DIR}/address_trace_graph.html"
OUTPUT_EDGES_CSV="${CODE_DIR}/address_trace_edges.csv"
OUTPUT_NODES_CSV="${CODE_DIR}/address_trace_nodes.csv"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <root-address> [additional trace_address_graph.py args...]" >&2
  echo "Example: $0 0x0000000000000000000000000000000000000000 --max-depth 2 --max-nodes 500" >&2
  exit 1
fi

ROOT_ADDRESS="$1"
shift

mkdir -p "${CODE_DIR}"

python3 "${CODE_DIR}/trace_address_graph.py" \
  --root-address "${ROOT_ADDRESS}" \
  --data-base-dir "${DATA_BASE_DIR}" \
  --index-base-dir "${INDEX_BASE_DIR}" \
  --token-metadata-csv "${TOKEN_METADATA_CSV}" \
  --start-block 25112101 \
  --end-block 25205700 \
  --eth-usd-price 2000.0 \
  --output-html "${OUTPUT_HTML}" \
  --output-edges-csv "${OUTPUT_EDGES_CSV}" \
  --output-nodes-csv "${OUTPUT_NODES_CSV}" \
  "$@"
