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
readonly RANGE_SIZE=7200

ADDRESS=""
MAX_DEPTH="$DEFAULT_MAX_DEPTH"
INDEX_DIR="$DEFAULT_INDEX_DIR"
OUTPUT_DIR="$DEFAULT_OUTPUT_DIR"
TOKEN_METADATA_CSV=""
TRACER_SCRIPT="$DEFAULT_TRACER_SCRIPT"
START_BLOCK=""
END_BLOCK=""

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
  --start-block BLOCK       Inclusive first block to trace over. Must be paired
                             with --end-block. Restricts the trace to exactly the
                             edge_index_<chunk_start>_<chunk_end>.sqlite file(s)
                             covering [--start-block, --end-block] - no other
                             .sqlite file under --index-dir is opened or queried.
  --end-block BLOCK         Inclusive last block to trace over. (end - start + 1)
                             must be a positive multiple of 7200, and each
                             7200-block chunk in the range must already have a
                             matching edge_index_<chunk_start>_<chunk_end>.sqlite
                             file (built by index_block_range_to_sqlite.sh) in
                             --index-dir.
  --token-metadata-csv FILE CSV with token_address,decimals columns, used to
                             normalize token_transfer values. ETH transfers
                             always use 18 decimals and are labeled "ETH".
                             Tokens missing from this file fall back to raw
                             units with a warning.
  --output-dir DIR          Base output directory (default: ./AddressFlow/output)
  -h, --help                Show this help and exit

Examples:
  # Use every *.sqlite file found under --index-dir
  ./trace_address_flow.sh \
    --address 0xAbCdEf0123456789abcdef0123456789ABCDEF01 \
    --max-depth 4 \
    --index-dir "./SqliteEdgeIndexes" \
    --token-metadata-csv "./AddressTracing/token_metadata.csv"

  # Restrict to exactly the 14400-block span 25415601-25430000
  # (i.e. edge_index_25415601_25422800.sqlite + edge_index_25422801_25430000.sqlite)
  ./trace_address_flow.sh \
    --address 0xAbCdEf0123456789abcdef0123456789ABCDEF01 \
    --max-depth 4 \
    --start-block 25415601 --end-block 25430000 \
    --index-dir "./SqliteEdgeIndexes"
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
    --start-block)
      require_value "$1" "${2-}"
      START_BLOCK="$2"
      shift 2
      ;;
    --end-block)
      require_value "$1" "${2-}"
      END_BLOCK="$2"
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

if [[ -n "$START_BLOCK" || -n "$END_BLOCK" ]]; then
  [[ -n "$START_BLOCK" ]] || die "--end-block was given without --start-block."
  [[ -n "$END_BLOCK" ]] || die "--start-block was given without --end-block."
fi
[[ -z "$START_BLOCK" || "$START_BLOCK" =~ ^[0-9]+$ ]] || die "--start-block must be a non-negative integer."
[[ -z "$END_BLOCK" || "$END_BLOCK" =~ ^[0-9]+$ ]] || die "--end-block must be a non-negative integer."

command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH."
[[ -f "$TRACER_SCRIPT" ]] || die "tracer script not found: ${TRACER_SCRIPT}"

DB_ARGS=()
if [[ -n "$START_BLOCK" ]]; then
  (( START_BLOCK <= END_BLOCK )) || die "--start-block must be less than or equal to --end-block."
  BLOCK_COUNT=$((END_BLOCK - START_BLOCK + 1))
  (( BLOCK_COUNT % RANGE_SIZE == 0 )) ||
    die "range ${START_BLOCK}-${END_BLOCK} spans ${BLOCK_COUNT} blocks; expected a positive multiple of ${RANGE_SIZE}."

  MISSING=()
  for ((chunk_start=START_BLOCK; chunk_start<=END_BLOCK; chunk_start+=RANGE_SIZE)); do
    chunk_end=$((chunk_start + RANGE_SIZE - 1))
    db_path="${INDEX_DIR}/edge_index_${chunk_start}_${chunk_end}.sqlite"
    if [[ -f "$db_path" ]]; then
      DB_ARGS+=(--db "$db_path")
    else
      MISSING+=("$db_path")
    fi
  done
  if (( ${#MISSING[@]} > 0 )); then
    echo "Error: missing SQLite edge index file(s) for the requested range:" >&2
    printf '  %s\n' "${MISSING[@]}" >&2
    echo "Build them first with index_block_range_to_sqlite.sh." >&2
    exit 1
  fi
fi

ARGS=(
  --address "$ADDRESS"
  --max-depth "$MAX_DEPTH"
  --output-dir "$OUTPUT_DIR"
)
if [[ -n "$START_BLOCK" ]]; then
  ARGS+=("${DB_ARGS[@]}")
else
  ARGS+=(--index-dir "$INDEX_DIR")
fi
if [[ -n "$TOKEN_METADATA_CSV" ]]; then
  [[ -f "$TOKEN_METADATA_CSV" ]] || die "token metadata CSV not found: ${TOKEN_METADATA_CSV}"
  ARGS+=(--token-metadata-csv "$TOKEN_METADATA_CSV")
fi

echo "Starting address flow trace"
echo "Address      : ${ADDRESS}"
echo "Max depth    : ${MAX_DEPTH}"
if [[ -n "$START_BLOCK" ]]; then
  echo "Block range  : ${START_BLOCK}-${END_BLOCK} ($(( ${#DB_ARGS[@]} / 2 )) index file(s), restricted)"
  for ((i=1; i<${#DB_ARGS[@]}; i+=2)); do
    echo "  - ${DB_ARGS[$i]}"
  done
else
  echo "Index dir    : ${INDEX_DIR}"
fi
echo "Output dir   : ${OUTPUT_DIR}"

python3 "$TRACER_SCRIPT" "${ARGS[@]}"
