#!/usr/bin/env bash
set -euo pipefail

CODE_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/TokenAddressSummary"

INPUT_FOLDER_1="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable/Ethereum_TT_25191301_25198500"
INPUT_FOLDER_2="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable/Ethereum_TT_25198501_25205700"

RPC_URL="${ETHEREUM_RPC_URL:-http://10.112.249.200:8545}"
OUTPUT_CSV="${CODE_DIR}/token_address_frequency_metadata_25191301_25205700.csv"
METADATA_CACHE="${CODE_DIR}/token_metadata_cache.csv"
MIN_OCCURRENCE_COUNT="50"

mkdir -p "${CODE_DIR}"

python3 "${CODE_DIR}/generate_token_metadata_frequency.py" \
  --input-folders \
    "${INPUT_FOLDER_1}" \
    "${INPUT_FOLDER_2}" \
  --rpc-url "${RPC_URL}" \
  --output "${OUTPUT_CSV}" \
  --metadata-cache "${METADATA_CACHE}" \
  --min-occurrence-count "${MIN_OCCURRENCE_COUNT}"
