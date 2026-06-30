#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv-address-tracing}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  cat <<USAGE
Usage: NEO4J_PASSWORD=<password> $0 --graph-json PATH [options]
   or: NEO4J_PASSWORD=<password> $0 --edges-csv PATH --nodes-csv PATH [options]

Imports a generated source-neighborhood graph into local Neo4j.
Common options: --graph-id ID --uri URI --user USER --database DB --clear-graph
USAGE
}

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

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
python "${SCRIPT_DIR}/import_neighborhood_graph_to_neo4j.py" "$@"
