#!/usr/bin/env bash
set -euo pipefail

# Export WETH + USDC token transfers for 3 days as 72 CSV files
# (24 * 3 chunks, each chunk = 300 blocks).
#
# Default block plan:
#   END_BLOCK=24958800
#   TOTAL_CHUNKS=72
#   CHUNK_SIZE=300
#   START_BLOCK=END_BLOCK - (TOTAL_CHUNKS * CHUNK_SIZE) + 1
#
# You can override defaults with flags.

USDC_TOKEN="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH_TOKEN="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

RPC_URL="http://10.112.249.200:8545"
END_BLOCK=24965300
TOTAL_CHUNKS=$((24 * 5 * 3 * 2))
CHUNK_SIZE=50
BATCH_SIZE=100
MAX_WORKERS=5
OUTPUT_DIR="weth_usdc_transfer_chunks"

usage() {
  cat <<'EOF'
Usage: ./export_weth_usdc_72_csvs.sh [options]

Options:
  --rpc-url URL            Ethereum JSON-RPC URL (default: http://10.112.249.200:8545)
  --end-block N            End block (default: 24958800)
  --chunk-size N           Blocks per CSV file (default: 300)
  --total-chunks N         Number of CSV files/chunks (default: 72)
  --batch-size N           ethereumetl batch-size (default: 100)
  --max-workers N          ethereumetl max workers (default: 5)
  --output-dir DIR         Output directory (default: weth_usdc_transfer_chunks)
  -h, --help               Show this help

Notes:
  * The script calculates START_BLOCK as:
      START_BLOCK = END_BLOCK - (TOTAL_CHUNKS * CHUNK_SIZE) + 1
  * Each generated CSV contains one non-overlapping 300-block window by default.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rpc-url)
      RPC_URL="$2"
      shift 2
      ;;
    --end-block)
      END_BLOCK="$2"
      shift 2
      ;;
    --chunk-size)
      CHUNK_SIZE="$2"
      shift 2
      ;;
    --total-chunks)
      TOTAL_CHUNKS="$2"
      shift 2
      ;;
    --batch-size)
      BATCH_SIZE="$2"
      shift 2
      ;;
    --max-workers)
      MAX_WORKERS="$2"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if ! command -v ethereumetl >/dev/null 2>&1; then
  echo "Error: ethereumetl command not found in PATH." >&2
  echo "Install ethereumetl or activate the environment where it is available." >&2
  exit 1
fi

if [[ "$CHUNK_SIZE" -le 0 || "$TOTAL_CHUNKS" -le 0 ]]; then
  echo "Error: --chunk-size and --total-chunks must be positive integers." >&2
  exit 1
fi

START_BLOCK=$((END_BLOCK - (TOTAL_CHUNKS * CHUNK_SIZE) + 1))
if [[ "$START_BLOCK" -lt 0 ]]; then
  echo "Error: calculated START_BLOCK is negative: $START_BLOCK" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

echo "Exporting token transfers with ethereumetl"
echo "RPC URL      : $RPC_URL"
echo "Tokens       : $USDC_TOKEN, $WETH_TOKEN"
echo "Chunk size   : $CHUNK_SIZE blocks"
echo "Total chunks : $TOTAL_CHUNKS"
echo "Block range  : $START_BLOCK -> $END_BLOCK"
echo "Output dir   : $OUTPUT_DIR"

for ((i=0; i<TOTAL_CHUNKS; i++)); do
  chunk_start=$((START_BLOCK + i * CHUNK_SIZE))
  chunk_end=$((chunk_start + CHUNK_SIZE - 1))
  if [[ "$chunk_end" -gt "$END_BLOCK" ]]; then
    chunk_end="$END_BLOCK"
  fi

  file_index=$(printf "%03d" "$((i + 1))")
  output_csv="$OUTPUT_DIR/token_transfers_${file_index}_${chunk_start}_${chunk_end}.csv"

  echo "[$((i + 1))/$TOTAL_CHUNKS] blocks ${chunk_start}-${chunk_end} -> ${output_csv}"

  ethereumetl export_token_transfers \
    --start-block "$chunk_start" \
    --end-block "$chunk_end" \
    --provider-uri "$RPC_URL" \
    --batch-size "$BATCH_SIZE" \
    --max-workers "$MAX_WORKERS" \
    --tokens "$USDC_TOKEN" \
    --tokens "$WETH_TOKEN" \
    --output "$output_csv"
done

echo "Done. Generated ${TOTAL_CHUNKS} CSV files in ${OUTPUT_DIR}."