#!/usr/bin/env bash
set -euo pipefail

# Export one Ethereum transaction CSV and one token-transfer CSV per block.

readonly DEFAULT_BASE_OUTPUT_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NewCSVs"
readonly DEFAULT_RPC_URL="http://10.112.249.200:8545"

GLOBAL_START_BLOCK=25458801
GLOBAL_END_BLOCK=25473200
BASE_OUTPUT_DIR="$DEFAULT_BASE_OUTPUT_DIR"
RPC_URL="${ETHEREUM_RPC_URL:-$DEFAULT_RPC_URL}"

readonly OUTER_RANGE_SIZE=7200
readonly CHUNK_SIZE=100
readonly BATCH_SIZE=100
readonly MAX_WORKERS=5

usage() {
  cat <<'USAGE'
Usage:
  ./extract_newcsvs_transactions_tokentransfers.sh [options]

Extract Ethereum transactions and token transfers into one CSV of each type per
block. With no options, blocks 25242001 through 25306800 are exported under:

  /media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NewCSVs

Options:
  --start-block BLOCK  Inclusive first block (default: 25242001)
  --end-block BLOCK    Inclusive last block (default: 25306800)
  --output-dir DIR     Base output directory
  --rpc-url URL        Ethereum JSON-RPC URL
  -h, --help           Show this help and exit

The RPC URL can also be overridden with the ETHEREUM_RPC_URL environment
variable. An explicit --rpc-url option takes precedence.

The inclusive range must divide evenly into 7200-block outer ranges, and every
outer range must divide evenly into 100-block chunks. Existing non-empty output
files are preserved, so interrupted exports can be resumed by rerunning this
script.
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

ensure_non_empty_csv() {
  local csv_file="$1"
  local header="$2"
  local item_name="$3"
  local block="$4"

  if [[ -s "$csv_file" ]]; then
    return
  fi

  printf '%s\n' "$header" > "$csv_file" ||
    die "could not write ${item_name} CSV header for block ${block}: ${csv_file}"
  echo "      ${item_name} export for block ${block} contained 0 rows; wrote header-only CSV."
}

readonly TRANSACTION_CSV_HEADER="hash,nonce,block_hash,block_number,transaction_index,from_address,to_address,value,gas,gas_price,input,block_timestamp,max_fee_per_gas,max_priority_fee_per_gas,transaction_type"
readonly TOKEN_TRANSFER_CSV_HEADER="token_address,from_address,to_address,value,transaction_hash,log_index,block_number"

while (( $# > 0 )); do
  case "$1" in
    --start-block)
      require_value "$1" "${2-}"
      GLOBAL_START_BLOCK="$2"
      shift 2
      ;;
    --end-block)
      require_value "$1" "${2-}"
      GLOBAL_END_BLOCK="$2"
      shift 2
      ;;
    --output-dir)
      require_value "$1" "${2-}"
      BASE_OUTPUT_DIR="$2"
      shift 2
      ;;
    --rpc-url)
      require_value "$1" "${2-}"
      RPC_URL="$2"
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

[[ "$GLOBAL_START_BLOCK" =~ ^[0-9]+$ ]] ||
  die "--start-block must be a non-negative integer."
[[ "$GLOBAL_END_BLOCK" =~ ^[0-9]+$ ]] ||
  die "--end-block must be a non-negative integer."
(( GLOBAL_START_BLOCK <= GLOBAL_END_BLOCK )) ||
  die "start block must be less than or equal to end block."

command -v ethereumetl >/dev/null 2>&1 ||
  die "ethereumetl command not found in PATH. Install ethereum-etl or activate its environment."

TOTAL_BLOCKS=$((GLOBAL_END_BLOCK - GLOBAL_START_BLOCK + 1))
(( TOTAL_BLOCKS % OUTER_RANGE_SIZE == 0 )) ||
  die "global range (${TOTAL_BLOCKS} blocks) is not divisible by OUTER_RANGE_SIZE (${OUTER_RANGE_SIZE})."
(( OUTER_RANGE_SIZE % CHUNK_SIZE == 0 )) ||
  die "OUTER_RANGE_SIZE (${OUTER_RANGE_SIZE}) is not divisible by CHUNK_SIZE (${CHUNK_SIZE})."

TOTAL_OUTER_RANGES=$((TOTAL_BLOCKS / OUTER_RANGE_SIZE))
CHUNKS_PER_OUTER_RANGE=$((OUTER_RANGE_SIZE / CHUNK_SIZE))
TOTAL_CHUNKS=$((TOTAL_BLOCKS / CHUNK_SIZE))

mkdir -p "$BASE_OUTPUT_DIR" ||
  die "could not create base output directory: ${BASE_OUTPUT_DIR}"

echo "Starting Ethereum transaction and token-transfer extraction"
echo "RPC URL                : ${RPC_URL}"
echo "Base output directory  : ${BASE_OUTPUT_DIR}"
echo "Global start block     : ${GLOBAL_START_BLOCK}"
echo "Global end block       : ${GLOBAL_END_BLOCK}"
echo "Total blocks           : ${TOTAL_BLOCKS}"
echo "Number of outer ranges : ${TOTAL_OUTER_RANGES}"
echo "Chunks per outer range : ${CHUNKS_PER_OUTER_RANGE}"
echo "Total 100-block chunks : ${TOTAL_CHUNKS}"

outer_index=0
for ((outer_start=GLOBAL_START_BLOCK; outer_start<=GLOBAL_END_BLOCK; outer_start+=OUTER_RANGE_SIZE)); do
  outer_end=$((outer_start + OUTER_RANGE_SIZE - 1))
  ((outer_index += 1))

  outer_block_count=$((outer_end - outer_start + 1))
  (( outer_block_count % CHUNK_SIZE == 0 )) ||
    die "outer range ${outer_start}-${outer_end} is not divisible into exact ${CHUNK_SIZE}-block chunks."

  outer_dir="${BASE_OUTPUT_DIR}/Ethereum_TT_${outer_start}_${outer_end}"
  mkdir -p "$outer_dir" ||
    die "could not create outer range directory: ${outer_dir}"

  echo
  echo "[Outer range ${outer_index}/${TOTAL_OUTER_RANGES}] ${outer_start}-${outer_end}"
  echo "Output directory: ${outer_dir}"

  chunk_index=0
  for ((chunk_start=outer_start; chunk_start<=outer_end; chunk_start+=CHUNK_SIZE)); do
    chunk_end=$((chunk_start + CHUNK_SIZE - 1))
    ((chunk_index += 1))

    chunk_dir="${outer_dir}/Transaction_TokenTransfer_${chunk_start}_${chunk_end}"
    mkdir -p "$chunk_dir" ||
      die "could not create chunk directory: ${chunk_dir}"

    echo "  [Chunk ${chunk_index}/${CHUNKS_PER_OUTER_RANGE}] ${chunk_start}-${chunk_end}"

    for ((block=chunk_start; block<=chunk_end; block++)); do
      tx_file="${chunk_dir}/transaction_${block}.csv"
      tt_file="${chunk_dir}/token_transfer_${block}.csv"
      blocks_file="/tmp/eth_blocks_${block}.csv"

      if [[ -s "$tx_file" && -s "$tt_file" ]]; then
        echo "    Skipping block ${block}: transaction and token_transfer CSVs already exist."
        continue
      fi

      echo "    Processing block ${block}"

      if [[ -s "$tx_file" ]]; then
        echo "      Transaction CSV already exists: ${tx_file}"
      else
        echo "      Generating transaction CSV: ${tx_file}"
        rm -f "$tx_file" "$blocks_file"
        if ! ethereumetl export_blocks_and_transactions \
          --start-block "$block" \
          --end-block "$block" \
          --provider-uri "$RPC_URL" \
          --batch-size "$BATCH_SIZE" \
          --max-workers "$MAX_WORKERS" \
          --blocks-output "$blocks_file" \
          --transactions-output "$tx_file"; then
          rm -f "$tx_file" "$blocks_file"
          die "transaction export failed for block ${block}; output file: ${tx_file}"
        fi
        rm -f "$blocks_file"
        ensure_non_empty_csv "$tx_file" "$TRANSACTION_CSV_HEADER" "transaction" "$block"
      fi

      if [[ -s "$tt_file" ]]; then
        echo "      Token-transfer CSV already exists: ${tt_file}"
      else
        echo "      Generating token-transfer CSV: ${tt_file}"
        rm -f "$tt_file"
        if ! ethereumetl export_token_transfers \
          --start-block "$block" \
          --end-block "$block" \
          --provider-uri "$RPC_URL" \
          --batch-size "$BATCH_SIZE" \
          --max-workers "$MAX_WORKERS" \
          --output "$tt_file"; then
          rm -f "$tt_file"
          die "token_transfer export failed for block ${block}; output file: ${tt_file}"
        fi
        ensure_non_empty_csv "$tt_file" "$TOKEN_TRANSFER_CSV_HEADER" "token_transfer" "$block"
      fi
    done
  done
done

echo
echo "Extraction complete. Processed ${TOTAL_BLOCKS} blocks across ${TOTAL_OUTER_RANGES} outer ranges."
echo "Done. Data exported to: ${BASE_OUTPUT_DIR}"
