#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv-address-tracing}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  cat <<USAGE
Usage: $0 --source-address ADDRESS --csv-root PATH --index-dir PATH [options]

Builds a 5-hop send/receive neighborhood graph from extracted Ethereum CSVs
and address_block_index_*.sqlite files, then writes HTML + edge/node CSV files.

Required:
  --source-address ADDRESS  Center/source Ethereum address
  --csv-root PATH           Ethereum_TT_<start>_<end> folder, or parent data dir
  --index-dir PATH          Directory containing address_block_index_*.sqlite files

Common options passed through to Python:
  --token-metadata-csv PATH
  --start-block N --end-block N
  --output-dir PATH
  --output-prefix NAME
  --output-json PATH
  --skip-html              Write portable JSON/CSV only for transfer to another machine
  --max-depth N             Default: 5
  --max-nodes N             Default: 5000
  --max-edges N             Default: 25000
  --min-usd-value AMOUNT
  --exclude-unknown-usd
  --verbose

Environment:
  VENV_DIR                  Default: .venv-address-tracing
  PYTHON_BIN                Default: python3
USAGE
}

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 2
fi

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Error: ${PYTHON_BIN} was not found. Install Python 3 or set PYTHON_BIN." >&2
  exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "${SCRIPT_DIR}/requirements.txt"
python "${SCRIPT_DIR}/build_source_neighborhood_graph.py" "$@"
