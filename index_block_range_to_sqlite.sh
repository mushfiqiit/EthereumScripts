#!/usr/bin/env bash
set -euo pipefail

# Index a single 7200-block CSV range (produced by
# extract_newcsvs_transactions_tokentransfers.sh) into one SQLite database.
#
# Standalone script: does not source, call, or otherwise depend on
# index_newcsvs_address_ranges.sh or build_address_index.py.

readonly DEFAULT_BASE_INPUT_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NewCSVs"
readonly DEFAULT_BASE_OUTPUT_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/SqliteEdgeIndexes"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DEFAULT_INDEXER_SCRIPT="${SCRIPT_DIR}/build_block_range_sqlite_index.py"
readonly RANGE_SIZE=7200

START_BLOCK=""
END_BLOCK=""
BASE_INPUT_DIR="$DEFAULT_BASE_INPUT_DIR"
BASE_OUTPUT_DIR="$DEFAULT_BASE_OUTPUT_DIR"
INDEXER_SCRIPT="$DEFAULT_INDEXER_SCRIPT"
BATCH_SIZE=5000
COMMIT_EVERY=20000

usage() {
  cat <<'USAGE'
Usage:
  ./index_block_range_to_sqlite.sh --start-block BLOCK --end-block BLOCK [options]

Index one Ethereum_TT_<start>_<end> CSV folder (as produced by
extract_newcsvs_transactions_tokentransfers.sh) into a single SQLite database
of transfer edges (ETH transactions + token transfers), indexed on both the
from_address and to_address columns.

Required:
  --start-block BLOCK  Inclusive first block of the 7200-block range
  --end-block BLOCK    Inclusive last block of the 7200-block range

Options:
  --input-dir DIR      Base NewCSVs directory containing Ethereum_TT_* folders
                        (default: extract_newcsvs_transactions_tokentransfers.sh's default output dir)
  --output-dir DIR     Base directory for the output .sqlite file
  --db-name NAME       Output file name (default: edge_index_<start>_<end>.sqlite)
  --batch-size N       Insert batch size passed to the Python indexer (default: 5000)
  --commit-every N     Commit interval passed to the Python indexer (default: 20000)
  -h, --help           Show this help and exit

Example:
  ./index_block_range_to_sqlite.sh \
    --start-block 25415601 --end-block 25422800 \
    --input-dir "/path/to/NewCSVs" \
    --output-dir "./SqliteEdgeIndexes"
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

DB_NAME=""

while (( $# > 0 )); do
  case "$1" in
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
    --input-dir)
      require_value "$1" "${2-}"
      BASE_INPUT_DIR="$2"
      shift 2
      ;;
    --output-dir)
      require_value "$1" "${2-}"
      BASE_OUTPUT_DIR="$2"
      shift 2
      ;;
    --db-name)
      require_value "$1" "${2-}"
      DB_NAME="$2"
      shift 2
      ;;
    --batch-size)
      require_value "$1" "${2-}"
      BATCH_SIZE="$2"
      shift 2
      ;;
    --commit-every)
      require_value "$1" "${2-}"
      COMMIT_EVERY="$2"
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

[[ -n "$START_BLOCK" ]] || die "--start-block is required."
[[ -n "$END_BLOCK" ]] || die "--end-block is required."
[[ "$START_BLOCK" =~ ^[0-9]+$ ]] || die "--start-block must be a non-negative integer."
[[ "$END_BLOCK" =~ ^[0-9]+$ ]] || die "--end-block must be a non-negative integer."
[[ "$BATCH_SIZE" =~ ^[0-9]+$ ]] || die "--batch-size must be a positive integer."
[[ "$COMMIT_EVERY" =~ ^[0-9]+$ ]] || die "--commit-every must be a positive integer."
(( START_BLOCK <= END_BLOCK )) || die "start block must be less than or equal to end block."
(( BATCH_SIZE > 0 )) || die "--batch-size must be greater than zero."
(( COMMIT_EVERY > 0 )) || die "--commit-every must be greater than zero."

BLOCK_COUNT=$((END_BLOCK - START_BLOCK + 1))
(( BLOCK_COUNT == RANGE_SIZE )) ||
  die "range ${START_BLOCK}-${END_BLOCK} spans ${BLOCK_COUNT} blocks; expected exactly ${RANGE_SIZE} (one Ethereum_TT_* folder)."

command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH."
[[ -f "$INDEXER_SCRIPT" ]] || die "indexer script not found: ${INDEXER_SCRIPT}"

RANGE_NAME="Ethereum_TT_${START_BLOCK}_${END_BLOCK}"
INPUT_ROOT="${BASE_INPUT_DIR}/${RANGE_NAME}"
[[ -d "$INPUT_ROOT" ]] || die "input CSV folder not found: ${INPUT_ROOT}"

[[ -n "$DB_NAME" ]] || DB_NAME="edge_index_${START_BLOCK}_${END_BLOCK}.sqlite"
mkdir -p "$BASE_OUTPUT_DIR" || die "could not create output directory: ${BASE_OUTPUT_DIR}"
DB_PATH="${BASE_OUTPUT_DIR}/${DB_NAME}"

cat <<INFO
Starting SQLite edge-index build for a single 7200-block range
Range         : ${RANGE_NAME}
Input folder  : ${INPUT_ROOT}
Output SQLite : ${DB_PATH}
Batch size    : ${BATCH_SIZE}
Commit every  : ${COMMIT_EVERY}
INFO

python3 "$INDEXER_SCRIPT" \
  --root "$INPUT_ROOT" \
  --db "$DB_PATH" \
  --batch-size "$BATCH_SIZE" \
  --commit-every "$COMMIT_EVERY"

echo
echo "SQLite edge index ready at: ${DB_PATH}"
