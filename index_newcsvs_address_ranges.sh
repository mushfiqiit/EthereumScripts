#!/usr/bin/env bash
set -euo pipefail

# Build SQLite address indexes for Ethereum transaction/token-transfer CSV exports.
#
# Defaults cover these four 7200-block NewCSVs folders:
#   Ethereum_TT_25415601_25422800
#   Ethereum_TT_25422801_25430000
#   Ethereum_TT_25430001_25437200
#   Ethereum_TT_25437201_25444400

readonly DEFAULT_BASE_INPUT_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NewCSVs"
readonly DEFAULT_BASE_OUTPUT_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/AddressIndexes"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DEFAULT_INDEXER_SCRIPT="${SCRIPT_DIR}/build_address_index.py"

START_BLOCK=25415601
END_BLOCK=25444400
BASE_INPUT_DIR="$DEFAULT_BASE_INPUT_DIR"
BASE_OUTPUT_DIR="$DEFAULT_BASE_OUTPUT_DIR"
INDEXER_SCRIPT="$DEFAULT_INDEXER_SCRIPT"
BATCH_SIZE=5000
COMMIT_EVERY=20000

readonly OUTER_RANGE_SIZE=7200
readonly EXPECTED_FOLDERS_PER_RANGE=72

usage() {
  cat <<'USAGE'
Usage:
  ./index_newcsvs_address_ranges.sh [options]

Build SQLite address indexes for 7200-block Ethereum_TT_* CSV folders generated
by extract_newcsvs_transactions_tokentransfers.sh.

Default input range:
  /media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NewCSVs/Ethereum_TT_25415601_25422800
through:
  /media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NewCSVs/Ethereum_TT_25437201_25444400

Default output base:
  /media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/AddressIndexes

Options:
  --start-block BLOCK    Inclusive first block (default: 25415601)
  --end-block BLOCK      Inclusive last block (default: 25444400)
  --input-dir DIR        Base NewCSVs directory
  --output-dir DIR       Base AddressIndexes directory
  --indexer-script FILE  Path to build_address_index.py
  --batch-size N         Insert batch size passed to build_address_index.py (default: 5000)
  --commit-every N       Commit interval passed to build_address_index.py (default: 20000)
  -h, --help             Show this help and exit

The inclusive range must divide evenly into 7200-block Ethereum_TT_* folders.
Each output folder mirrors the input folder name under the AddressIndexes base.
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
    --indexer-script)
      require_value "$1" "${2-}"
      INDEXER_SCRIPT="$2"
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

[[ "$START_BLOCK" =~ ^[0-9]+$ ]] || die "--start-block must be a non-negative integer."
[[ "$END_BLOCK" =~ ^[0-9]+$ ]] || die "--end-block must be a non-negative integer."
[[ "$BATCH_SIZE" =~ ^[0-9]+$ ]] || die "--batch-size must be a positive integer."
[[ "$COMMIT_EVERY" =~ ^[0-9]+$ ]] || die "--commit-every must be a positive integer."
(( START_BLOCK <= END_BLOCK )) || die "start block must be less than or equal to end block."
(( BATCH_SIZE > 0 )) || die "--batch-size must be greater than zero."
(( COMMIT_EVERY > 0 )) || die "--commit-every must be greater than zero."
[[ -f "$INDEXER_SCRIPT" ]] || die "indexer script not found: ${INDEXER_SCRIPT}"

TOTAL_BLOCKS=$((END_BLOCK - START_BLOCK + 1))
(( TOTAL_BLOCKS % OUTER_RANGE_SIZE == 0 )) || \
  die "global range (${TOTAL_BLOCKS} blocks) is not divisible by ${OUTER_RANGE_SIZE}."

TOTAL_RANGES=$((TOTAL_BLOCKS / OUTER_RANGE_SIZE))
mkdir -p "$BASE_OUTPUT_DIR" || die "could not create output directory: ${BASE_OUTPUT_DIR}"

cat <<INFO
Starting Ethereum address index build
Base input directory   : ${BASE_INPUT_DIR}
Base output directory  : ${BASE_OUTPUT_DIR}
Indexer script         : ${INDEXER_SCRIPT}
Global start block     : ${START_BLOCK}
Global end block       : ${END_BLOCK}
Total 7200-block ranges: ${TOTAL_RANGES}
Batch size             : ${BATCH_SIZE}
Commit every           : ${COMMIT_EVERY}
INFO

range_index=0
for ((range_start=START_BLOCK; range_start<=END_BLOCK; range_start+=OUTER_RANGE_SIZE)); do
  range_end=$((range_start + OUTER_RANGE_SIZE - 1))
  ((range_index += 1))

  range_name="Ethereum_TT_${range_start}_${range_end}"
  input_root="${BASE_INPUT_DIR}/${range_name}"
  output_root="${BASE_OUTPUT_DIR}/${range_name}"

  [[ -d "$input_root" ]] || die "input CSV folder not found: ${input_root}"
  mkdir -p "$output_root" || die "could not create index folder: ${output_root}"

  echo
  echo "[Range ${range_index}/${TOTAL_RANGES}] ${range_name}"
  echo "Input : ${input_root}"
  echo "Output: ${output_root}"

  python3 "$INDEXER_SCRIPT" \
    --root "$input_root" \
    --output "$output_root" \
    --batch-size "$BATCH_SIZE" \
    --commit-every "$COMMIT_EVERY" \
    --expected-folders "$EXPECTED_FOLDERS_PER_RANGE"
done

echo
echo "All requested address indexes built successfully."
