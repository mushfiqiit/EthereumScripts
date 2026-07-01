#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv-address-tracing}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
NODES_CSV="${NODES_CSV:-./nodes.csv}"
EDGES_CSV="${EDGES_CSV:-./edges.csv}"
GRAPH_ID="${GRAPH_ID:-nodes_edges_graph}"
CLEAR_GRAPH=0
EXTRA_ARGS=()

usage() {
  cat <<USAGE
Usage: $0 [--nodes-csv PATH] [--edges-csv PATH] [--graph-id ID] [--clear-graph] [import options]

Imports node/edge CSV files directly into local Neo4j. By default it reads
./nodes.csv and ./edges.csv from your current directory.

Options:
  --nodes-csv PATH   Nodes CSV path, default: ./nodes.csv
  --edges-csv PATH   Edges CSV path, default: ./edges.csv
  --graph-id ID      Graph ID in Neo4j, default: nodes_edges_graph
  --clear-graph      Delete existing Neo4j data with this graph ID before import

Common passthrough options for import_neighborhood_graph_to_neo4j.py:
  --uri URI          Default: NEO4J_URI or bolt://localhost:7687
  --user USER        Default: NEO4J_USER or neo4j
  --database DB      Default: NEO4J_DATABASE or neo4j
  --batch-size N     Default: 500

Environment:
  NEO4J_PASSWORD     Neo4j password; prompted when omitted in an interactive terminal
  VENV_DIR           Default: .venv-address-tracing
  PYTHON_BIN         Default: python3
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --nodes-csv)
      if [[ $# -lt 2 ]]; then echo "Error: --nodes-csv requires a path." >&2; exit 2; fi
      NODES_CSV="$2"
      shift 2
      ;;
    --edges-csv)
      if [[ $# -lt 2 ]]; then echo "Error: --edges-csv requires a path." >&2; exit 2; fi
      EDGES_CSV="$2"
      shift 2
      ;;
    --graph-id)
      if [[ $# -lt 2 ]]; then echo "Error: --graph-id requires a value." >&2; exit 2; fi
      GRAPH_ID="$2"
      shift 2
      ;;
    --clear-graph)
      CLEAR_GRAPH=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ ! -f "${NODES_CSV}" ]]; then
  echo "Error: nodes CSV not found: ${NODES_CSV}" >&2
  exit 1
fi
if [[ ! -f "${EDGES_CSV}" ]]; then
  echo "Error: edges CSV not found: ${EDGES_CSV}" >&2
  exit 1
fi

NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
if [[ -z "${NEO4J_PASSWORD:-}" ]]; then
  if [[ -t 0 && -t 2 ]]; then
    read -r -s -p "Neo4j password for ${NEO4J_USER}@${NEO4J_URI}: " NEO4J_PASSWORD
    echo >&2
  else
    cat >&2 <<ERROR
Error: NEO4J_PASSWORD is not set and this shell is not interactive.

Set it first, for example:
  export NEO4J_PASSWORD='your-local-neo4j-password'
  $0 --nodes-csv ./nodes.csv --edges-csv ./edges.csv --graph-id ${GRAPH_ID}
ERROR
    exit 1
  fi
fi
export NEO4J_PASSWORD NEO4J_URI NEO4J_USER

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

IMPORT_ARGS=(
  --nodes-csv "${NODES_CSV}"
  --edges-csv "${EDGES_CSV}"
  --graph-id "${GRAPH_ID}"
)
if [[ "${CLEAR_GRAPH}" == "1" ]]; then
  IMPORT_ARGS+=(--clear-graph)
fi
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  IMPORT_ARGS+=("${EXTRA_ARGS[@]}")
fi

python "${SCRIPT_DIR}/import_neighborhood_graph_to_neo4j.py" "${IMPORT_ARGS[@]}"
