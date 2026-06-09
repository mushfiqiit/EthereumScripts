#!/usr/bin/env bash
set -euo pipefail

CODE_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/AddressTracing"
DATA_BASE_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable"
INDEX_BASE_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable/index"
TOKEN_METADATA_CSV="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/TokenAddressSummary/token_address_frequency_metadata_with_exchange_rate_25191301_25205700.csv"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <root-address> [additional load_incoming_flow_to_neo4j.py arguments...]" >&2
  exit 1
fi
if [[ -z "${NEO4J_PASSWORD:-}" ]]; then
  echo "Error: NEO4J_PASSWORD must be set before running this script." >&2
  exit 1
fi

ROOT_ADDRESS="$1"
shift
ARGS=(
  --root-address "${ROOT_ADDRESS}"
  --data-base-dir "${DATA_BASE_DIR}"
  --index-base-dir "${INDEX_BASE_DIR}"
  --token-metadata-csv "${TOKEN_METADATA_CSV}"
  --start-block 25112101
  --end-block 25205700
  --neo4j-uri "${NEO4J_URI:-bolt://localhost:7687}"
  --neo4j-user "${NEO4J_USER:-neo4j}"
  --database "${NEO4J_DATABASE:-neo4j}"
)

append_value_if_set() {
  local variable_name="$1"
  local option_name="$2"
  if [[ -n "${!variable_name:-}" ]]; then
    ARGS+=("${option_name}" "${!variable_name}")
  fi
}

append_flag_if_true() {
  local variable_name="$1"
  local option_name="$2"
  case "${!variable_name:-}" in
    1|true|TRUE|yes|YES|on|ON) ARGS+=("${option_name}") ;;
  esac
}

append_value_if_set GRAPH_RUN_ID --graph-run-id
append_value_if_set MAX_DEPTH --max-depth
append_value_if_set MAX_NODES --max-nodes
append_value_if_set MAX_EDGES --max-edges
append_value_if_set MAX_BLOCKS_PER_ADDRESS --max-blocks-per-address
append_value_if_set MAX_RUNTIME_SECONDS --max-runtime-seconds
append_value_if_set MIN_USD_VALUE --min-usd-value
append_value_if_set SKIP_ADDRESS_IF_OCCURRENCE_OVER --skip-address-if-occurrence-over
append_value_if_set ETH_USD_PRICE --eth-usd-price
append_flag_if_true DRY_RUN --dry-run
append_flag_if_true INCLUDE_ZERO_ETH_TRANSACTIONS --include-zero-eth-transactions
append_flag_if_true EXCLUDE_UNKNOWN_VALUE_TRANSFERS --exclude-unknown-value-transfers

python3 "${CODE_DIR}/load_incoming_flow_to_neo4j.py" "${ARGS[@]}" "$@"
