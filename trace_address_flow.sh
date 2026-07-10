#!/usr/bin/env bash
set -euo pipefail

# Trace the flow of ETH/token transfers to and from a source address, up to
# a configurable max hop distance, using SQLite edge indexes built by
# build_block_range_sqlite_index.py / index_block_range_to_sqlite.sh.
#
# Standalone script: does not source or depend on any other script in this
# repository.

readonly DEFAULT_INDEX_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/SqliteEdgeIndexes"
readonly DEFAULT_OUTPUT_DIR="./AddressFlow/output"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DEFAULT_TRACER_SCRIPT="${SCRIPT_DIR}/trace_address_flow_bfs.py"
readonly DEFAULT_MAX_DEPTH=4

ADDRESS=""
MAX_DEPTH="$DEFAULT_MAX_DEPTH"
INDEX_DIR="$DEFAULT_INDEX_DIR"
OUTPUT_DIR="$DEFAULT_OUTPUT_DIR"
TOKEN_METADATA_CSV=""
TRACER_SCRIPT="$DEFAULT_TRACER_SCRIPT"

usage() {
  cat <<'USAGE'
Usage:
  ./trace_address_flow.sh --address 0xSOURCE_ADDRESS [options]

Run a breadth-first search outward from --address over the SQLite edge
indexes produced by build_block_range_sqlite_index.py, up to --max-depth
hops in either direction (who sent to the address, and who the address sent
to, directly or through intermediate addresses). Writes nodes.csv,
edges.csv, graph.json, and manifest.json under --output-dir/<address>.

Required:
  --address ADDR          Source Ethereum address (0x...)

Options:
  --max-depth N            Maximum BFS hop distance (default: 4)
  --index-dir DIR           Directory containing *.sqlite edge indexes
                             (default: build_block_range_sqlite_index.py's default output dir)
  --token-metadata-csv FILE CSV with token_address,decimals columns, used to
                             normalize token_transfer values. ETH transfers
                             always use 18 decimals and are labeled "ETH".
                             Tokens missing from this file fall back to raw
                             units with a warning.
  --output-dir DIR          Base output directory (default: ./AddressFlow/output)
  -h, --help                Show this help and exit

Example:
  ./trace_address_flow.sh \
    --address 0xAbCdEf0123456789abcdef0123456789ABCDEF01 \
    --max-depth 4 \
    --index-dir "./SqliteEdgeIndexes" \
    --token-metadata-csv "./AddressTracing/token_metadata.csv"
USAGE
}

die() {
  echo "Error: $*" >&2
  exit 1
}

require_value() {
  local option="$1"
  local value="${2-}"
  [[ -n "$value" ]] || die "${option} requires a value."
}

while (( $# > 0 )); do
  case "$1" in
    --address)
      require_value "$1" "${2-}"
      ADDRESS="$2"
      shift 2
      ;;
    --max-depth)
      require_value "$1" "${2-}"
      MAX_DEPTH="$2"
      shift 2
      ;;
    --index-dir)
      require_value "$1" "${2-}"
      INDEX_DIR="$2"
      shift 2
      ;;
    --token-metadata-csv)
      require_value "$1" "${2-}"
      TOKEN_METADATA_CSV="$2"
      shift 2
      ;;
    --output-dir)
      require_value "$1" "${2-}"
      OUTPUT_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

[[ -n "$ADDRESS" ]] || die "--address is required."
[[ "$ADDRESS" =~ ^0x[0-9a-fA-F]+$ ]] || die "--address must start with 0x."
[[ "$MAX_DEPTH" =~ ^[0-9]+$ ]] || die "--max-depth must be a non-negative integer."
[[ -d "$INDEX_DIR" ]] || die "index directory not found: ${INDEX_DIR}"

command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH."
[[ -f "$TRACER_SCRIPT" ]] || die "tracer script not found: ${TRACER_SCRIPT}"

ARGS=(
  --address "$ADDRESS"
  --max-depth "$MAX_DEPTH"
  --index-dir "$INDEX_DIR"
  --output-dir "$OUTPUT_DIR"
)
if [[ -n "$TOKEN_METADATA_CSV" ]]; then
  [[ -f "$TOKEN_METADATA_CSV" ]] || die "token metadata CSV not found: ${TOKEN_METADATA_CSV}"
  ARGS+=(--token-metadata-csv "$TOKEN_METADATA_CSV")
fi

echo "Starting address flow trace"
echo "Address      : ${ADDRESS}"
echo "Max depth    : ${MAX_DEPTH}"
echo "Index dir    : ${INDEX_DIR}"
echo "Output dir   : ${OUTPUT_DIR}"

python3 "$TRACER_SCRIPT" "${ARGS[@]}"