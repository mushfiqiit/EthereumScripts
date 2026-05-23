#!/usr/bin/env bash
set -euo pipefail

# Extract Ethereum transactions + token transfers per block using ethereumetl.
#
# Usage:
#   ./extract_Ethereum_transactions_tokentransfer.sh <start_block> <end_block>
#
# Example:
#   ./extract_Ethereum_transactions_tokentransfer.sh 25119301 25126500
#
# Output structure:
# /media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable/
#   Ethereum_TT_<start>_<end>/
#     Transaction_TokenTransfer_<chunk_start>_<chunk_end>/
#       transaction_<block>.csv
#       token_transfer_<block>.csv

RPC_URL="http://10.112.249.200:8545"
BATCH_SIZE=100
MAX_WORKERS=5
CHUNK_SIZE=100
BASE_OUTPUT_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable"

usage() {
  cat <<'USAGE'
Usage:
  ./extract_Ethereum_transactions_tokentransfer.sh <start_block> <end_block>

Arguments:
  start_block   Inclusive start block number (integer)
  end_block     Inclusive end block number (integer)

Notes:
  * The block range must be divisible into 100-block chunks.
  * For each block N, two files are created:
      transaction_N.csv
      token_transfer_N.csv
USAGE
}

if [[ $# -ne 2 ]]; then
  echo "Error: exactly 2 arguments are required." >&2
  usage
  exit 1
fi

START_BLOCK="$1"
END_BLOCK="$2"

if ! [[ "$START_BLOCK" =~ ^[0-9]+$ && "$END_BLOCK" =~ ^[0-9]+$ ]]; then
  echo "Error: start_block and end_block must be integers." >&2
  exit 1
fi

if (( START_BLOCK > END_BLOCK )); then
  echo "Error: start_block must be <= end_block." >&2
  exit 1
fi

if ! command -v ethereumetl >/dev/null 2>&1; then
  echo "Error: ethereumetl command not found in PATH." >&2
  echo "Install ethereumetl or activate the environment where it is available." >&2
  exit 1
fi

TOTAL_BLOCKS=$((END_BLOCK - START_BLOCK + 1))
if (( TOTAL_BLOCKS % CHUNK_SIZE != 0 )); then
  echo "Error: total blocks (${TOTAL_BLOCKS}) is not divisible by ${CHUNK_SIZE}." >&2
  echo "For this script, use a range that forms exact 100-block chunks." >&2
  exit 1
fi

TOP_DIR="${BASE_OUTPUT_DIR}/Ethereum_TT_${START_BLOCK}_${END_BLOCK}"
mkdir -p "$TOP_DIR"

TOTAL_CHUNKS=$((TOTAL_BLOCKS / CHUNK_SIZE))

echo "Starting extraction with ethereumetl"
echo "RPC URL      : ${RPC_URL}"
echo "Start block  : ${START_BLOCK}"
echo "End block    : ${END_BLOCK}"
echo "Total blocks : ${TOTAL_BLOCKS}"
echo "Chunk size   : ${CHUNK_SIZE}"
echo "Total chunks : ${TOTAL_CHUNKS}"
echo "Output root  : ${TOP_DIR}"

chunk_index=0
for ((chunk_start=START_BLOCK; chunk_start<=END_BLOCK; chunk_start+=CHUNK_SIZE)); do
  chunk_end=$((chunk_start + CHUNK_SIZE - 1))
  chunk_index=$((chunk_index + 1))

  chunk_dir="${TOP_DIR}/Transaction_TokenTransfer_${chunk_start}_${chunk_end}"
  mkdir -p "$chunk_dir"

  echo "[Chunk ${chunk_index}/${TOTAL_CHUNKS}] ${chunk_start}-${chunk_end} -> ${chunk_dir}"

  for ((block=chunk_start; block<=chunk_end; block++)); do
    tx_file="${chunk_dir}/transaction_${block}.csv"
    tt_file="${chunk_dir}/token_transfer_${block}.csv"

    echo "  Block ${block}: transaction + token_transfer"

    ethereumetl export_blocks_and_transactions \
      --start-block "$block" \
      --end-block "$block" \
      --provider-uri "$RPC_URL" \
      --batch-size "$BATCH_SIZE" \
      --max-workers "$MAX_WORKERS" \
      --blocks-output /tmp/eth_blocks_${block}.csv \
      --transactions-output "$tx_file" >/dev/null

    rm -f /tmp/eth_blocks_${block}.csv

    ethereumetl export_token_transfers \
      --start-block "$block" \
      --end-block "$block" \
      --provider-uri "$RPC_URL" \
      --batch-size "$BATCH_SIZE" \
      --max-workers "$MAX_WORKERS" \
      --output "$tt_file" >/dev/null
  done
done

echo "Done. Data exported to: ${TOP_DIR}"
