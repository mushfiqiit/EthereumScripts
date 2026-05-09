#!/usr/bin/env bash
set -euo pipefail

# Extend existing WETH/USDC transfer chunk exports from block 24,965,301 to
# block 24,979,000 in 50-block CSV windows.
#
# Intended continuation after files ending at:
#   token_transfers_720_24965251_24965300.csv
#
# Default output naming starts at index 721, e.g.:
#   token_transfers_721_24965301_24965350.csv

USDC_TOKEN="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH_TOKEN="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

RPC_URL="http://10.112.249.200:8545"
START_BLOCK=24965301
END_BLOCK=24979000
CHUNK_SIZE=50
START_INDEX=721
BATCH_SIZE=100
MAX_WORKERS=5
OUTPUT_DIR="weth_usdc_transfer_chunks"

usage() {
  cat <<'EOF'
Usage: ./export_weth_usdc_extension_to_24979000.sh [options]

Options:
  --rpc-url URL            Ethereum JSON-RPC URL (default: http://10.112.249.200:8545)
  --start-block N          Start block (default: 24965301)
  --end-block N            End block (default: 24979000)
  --chunk-size N           Blocks per CSV (default: 50)
  --start-index N          Starting file index (default: 721)
  --batch-size N           ethereumetl batch-size (default: 100)
  --max-workers N          ethereumetl max workers (default: 5)
  --output-dir DIR         Output directory (default: weth_usdc_transfer_chunks)
  -h, --help               Show this help

Notes:
  * Defaults produce files from:
      token_transfers_721_24965301_24965350.csv
    through:
      token_transfers_994_24978951_24979000.csv
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rpc-url)
      RPC_URL="$2"
      shift 2
      ;;
    --start-block)
      START_BLOCK="$2"
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
    --start-index)
      START_INDEX="$2"
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

if [[ "$CHUNK_SIZE" -le 0 ]]; then
  echo "Error: --chunk-size must be a positive integer." >&2
  exit 1
fi

if [[ "$START_BLOCK" -gt "$END_BLOCK" ]]; then
  echo "Error: --start-block must be <= --end-block." >&2
  exit 1
fi

TOTAL_BLOCKS=$((END_BLOCK - START_BLOCK + 1))
if (( TOTAL_BLOCKS % CHUNK_SIZE != 0 )); then
  echo "Error: block range is not evenly divisible by chunk size." >&2
  echo "       total blocks: $TOTAL_BLOCKS, chunk size: $CHUNK_SIZE" >&2
  exit 1
fi

TOTAL_CHUNKS=$((TOTAL_BLOCKS / CHUNK_SIZE))
END_INDEX=$((START_INDEX + TOTAL_CHUNKS - 1))

mkdir -p "$OUTPUT_DIR"

echo "Exporting extension token transfers with ethereumetl"
echo "RPC URL      : $RPC_URL"
echo "Tokens       : $USDC_TOKEN, $WETH_TOKEN"
echo "Chunk size   : $CHUNK_SIZE blocks"
echo "Start block  : $START_BLOCK"
echo "End block    : $END_BLOCK"
echo "Total blocks : $TOTAL_BLOCKS"
echo "Total chunks : $TOTAL_CHUNKS"
echo "File index   : $START_INDEX -> $END_INDEX"
echo "Output dir   : $OUTPUT_DIR"

for ((i=0; i<TOTAL_CHUNKS; i++)); do
  chunk_start=$((START_BLOCK + i * CHUNK_SIZE))
  chunk_end=$((chunk_start + CHUNK_SIZE - 1))

  file_index=$(printf "%03d" "$((START_INDEX + i))")
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