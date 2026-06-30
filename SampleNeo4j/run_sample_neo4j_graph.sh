#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv-sample-neo4j}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
OUTPUT_HTML="${OUTPUT_HTML:-${SCRIPT_DIR}/sample_neo4j_graph.html}"

NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"
KEEP_EXISTING="${KEEP_EXISTING:-0}"

if [[ "$(pwd -P)" == "${SCRIPT_DIR}" ]]; then
  RUN_COMMAND="bash ./run_sample_neo4j_graph.sh"
elif [[ "$(pwd -P)" == "${REPO_ROOT}" ]]; then
  RUN_COMMAND="bash SampleNeo4j/run_sample_neo4j_graph.sh"
else
  RUN_COMMAND="bash ${SCRIPT_DIR}/run_sample_neo4j_graph.sh"
fi

usage() {
  cat <<USAGE
Usage: NEO4J_PASSWORD=<password> $0 [--keep-existing] [--no-open] [--output PATH]

Creates a Python virtual environment, installs the sample Neo4j dependencies,
loads a small demo graph into Neo4j, and exports an interactive HTML graph.

Environment variables:
  NEO4J_URI       Bolt URI, default: bolt://localhost:7687
  NEO4J_USER      Neo4j username, default: neo4j
  NEO4J_PASSWORD  Neo4j password; prompted when omitted in an interactive terminal
  NEO4J_DATABASE  Neo4j database, default: neo4j
  VENV_DIR        Virtualenv directory, default: .venv-sample-neo4j
  OUTPUT_HTML     HTML output path, default: SampleNeo4j/sample_neo4j_graph.html
  OPEN_BROWSER    Open HTML on macOS after export, default: 1
  KEEP_EXISTING   Keep previous SampleDemo nodes, default: 0
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-existing)
      KEEP_EXISTING=1
      shift
      ;;
    --no-open)
      OPEN_BROWSER=0
      shift
      ;;
    --output)
      if [[ $# -lt 2 ]]; then
        echo "Error: --output requires a path." >&2
        exit 2
      fi
      OUTPUT_HTML="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "${NEO4J_PASSWORD:-}" ]]; then
  if [[ -t 0 && -t 2 ]]; then
    read -r -s -p "Neo4j password for ${NEO4J_USER}@${NEO4J_URI}: " NEO4J_PASSWORD
    echo >&2
  else
    cat >&2 <<ERROR
Error: NEO4J_PASSWORD is not set and this shell is not interactive.

Neo4j is running, but the Python driver still needs your database password to
log in over Bolt. Start Neo4j first, then run for example:
  export NEO4J_PASSWORD='your-password'
  ${RUN_COMMAND}
ERROR
    exit 1
  fi
fi
export NEO4J_PASSWORD

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

if [[ "${KEEP_EXISTING}" =~ ^(1|true|TRUE|yes|YES|on|ON)$ ]]; then
  python "${SCRIPT_DIR}/load_sample_graph.py" \
    --uri "${NEO4J_URI}" \
    --user "${NEO4J_USER}" \
    --database "${NEO4J_DATABASE}" \
    --keep-existing
else
  python "${SCRIPT_DIR}/load_sample_graph.py" \
    --uri "${NEO4J_URI}" \
    --user "${NEO4J_USER}" \
    --database "${NEO4J_DATABASE}"
fi

python "${SCRIPT_DIR}/export_sample_graph_html.py" \
  --uri "${NEO4J_URI}" \
  --user "${NEO4J_USER}" \
  --database "${NEO4J_DATABASE}" \
  --output "${OUTPUT_HTML}"

cat <<DONE

Done.
Interactive HTML visualization: ${OUTPUT_HTML}
Neo4j Browser query:
  MATCH p = (:SampleDemo)-[*1..2]->(:SampleDemo) RETURN p LIMIT 100;
DONE

if [[ "${OPEN_BROWSER}" =~ ^(1|true|TRUE|yes|YES|on|ON)$ ]] && [[ "$(uname -s)" == "Darwin" ]]; then
  open "${OUTPUT_HTML}"
fi