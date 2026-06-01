#!/usr/bin/env bash
set -euo pipefail

CODE_DIR="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/TokenAddressSummary"

INPUT_METADATA_CSV="${CODE_DIR}/token_address_frequency_metadata_25191301_25205700.csv"
OUTPUT_CSV="${CODE_DIR}/token_address_frequency_metadata_with_exchange_rate_25191301_25205700.csv"

INPUT_FOLDER_1="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable/Ethereum_TT_25191301_25198500"
INPUT_FOLDER_2="/media/dheeman/Seagate Backup Plus Drive1/EthereumCode_Mushfiq/EthereumScripts/NotUploadable/Ethereum_TT_25198501_25205700"

mkdir -p "${CODE_DIR}"

python3 "${CODE_DIR}/add_median_exchange_rate.py" \
  --metadata-csv "${INPUT_METADATA_CSV}" \
  --input-folders \
    "${INPUT_FOLDER_1}" \
    "${INPUT_FOLDER_2}" \
  --output "${OUTPUT_CSV}"
