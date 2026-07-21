#!/usr/bin/env bash
set -euo pipefail

# Import a trace_address_flow_bfs.py graph (nodes.csv + edges.csv) into
# Neo4j as a directed graph: every relationship points from_address ->
# to_address, matching the real direction the value moved on-chain.
#
# Writes are batched and bounded by a wall-clock time budget (default 10
# minutes); an HTML visualization is always rendered afterward by querying
# Neo4j directly for whatever exists under --graph-id, so it reflects a
# partial graph if the time budget was hit, or the full graph if not.
#
# Requires the official Neo4j Python driver: pip install neo4j
#
# Standalone script: does not source or depend on any other script in this
# repository.

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DEFAULT_IMPORTER_SCRIPT="${SCRIPT_DIR}/import_address_flow_to_neo4j.py"
readonly DEFAULT_TIME_BUDGET_MINUTES=10
readonly DEFAULT_BATCH_SIZE=2000

GRAPH_DIR=""
GRAPH_ID=""
MAX_DEPTH=4
CLEAR_EXISTING=0
DRY_RUN=0
NO_HTML=0
URI="${NEO4J_URI:-bolt://localhost:7687}"
USER_NAME="${NEO4J_USER:-neo4j}"
PASSWORD="${NEO4J_PASSWORD:-}"
DATABASE="${NEO4J_DATABASE:-neo4j}"
IMPORTER_SCRIPT="$DEFAULT_IMPORTER_SCRIPT"
TIME_BUDGET_MINUTES="$DEFAULT_TIME_BUDGET_MINUTES"
BATCH_SIZE="$DEFAULT_BATCH_SIZE"
HTML_OUTPUT=""

usage() {
  cat <<'USAGE'
Usage:
  ./import_address_flow_to_neo4j.sh --graph-dir DIR --graph-id ID [options]

Import the nodes.csv/edges.csv produced by trace_address_flow_bfs.py into
Neo4j as a directed graph. Every relationship is created with an explicit
direction, (from)-[:TRANSFER]->(to), so Neo4j Browser always draws an
arrowhead pointing the way the currency actually moved, and the printed
example queries use directed multi-hop patterns so a match follows a single
forward chain of transfers instead of zig-zagging across edge directions.

Required:
  --graph-dir DIR   Directory containing nodes.csv and edges.csv
                     (e.g. ./AddressFlow/output/0xSOURCE_ADDRESS)
  --graph-id ID     Namespace tag stored on every node/relationship, so
                     multiple traces can coexist in the same Neo4j database

Options:
  --max-depth N            Max depth used in the printed example queries (default: 4)
  --clear-existing         Delete existing nodes/relationships for this graph-id first
  --time-budget-minutes N  Hard wall-clock cap on the whole import (default: 10). Once
                            reached, no further batches are sent - Neo4j and the HTML
                            keep whatever was written so far.
  --batch-size N           Rows per write transaction (default: 2000)
  --html-output FILE       Output HTML path (default: <graph-dir>/graph_visualization.html)
  --no-html                Skip HTML generation
  --uri URI                Neo4j Bolt URI (default: bolt://localhost:7687, or $NEO4J_URI)
  --user USER              Neo4j username (default: neo4j, or $NEO4J_USER)
  --password PASSWORD      Neo4j password (or $NEO4J_PASSWORD)
  --database NAME          Neo4j database name (default: neo4j, or $NEO4J_DATABASE)
  --dry-run                Write nothing to Neo4j; render HTML straight from the CSVs
  -h, --help                Show this help and exit

Example:
  NEO4J_PASSWORD='your-password' ./import_address_flow_to_neo4j.sh \
    --graph-dir ./AddressFlow/output/0xSOURCE_ADDRESS \
    --graph-id eth-source-trace \
    --max-depth 4 \
    --time-budget-minutes 10 \
    --clear-existing
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

while (( $# > 0 )); do
  case "$1" in
    --graph-dir)
      require_value "$1" "${2-}"
      GRAPH_DIR="$2"
      shift 2
      ;;
    --graph-id)
      require_value "$1" "${2-}"
      GRAPH_ID="$2"
      shift 2
      ;;
    --max-depth)
      require_value "$1" "${2-}"
      MAX_DEPTH="$2"
      shift 2
      ;;
    --clear-existing)
      CLEAR_EXISTING=1
      shift
      ;;
    --uri)
      require_value "$1" "${2-}"
      URI="$2"
      shift 2
      ;;
    --user)
      require_value "$1" "${2-}"
      USER_NAME="$2"
      shift 2
      ;;
    --password)
      require_value "$1" "${2-}"
      PASSWORD="$2"
      shift 2
      ;;
    --database)
      require_value "$1" "${2-}"
      DATABASE="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --time-budget-minutes)
      require_value "$1" "${2-}"
      TIME_BUDGET_MINUTES="$2"
      shift 2
      ;;
    --batch-size)
      require_value "$1" "${2-}"
      BATCH_SIZE="$2"
      shift 2
      ;;
    --html-output)
      require_value "$1" "${2-}"
      HTML_OUTPUT="$2"
      shift 2
      ;;
    --no-html)
      NO_HTML=1
      shift
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

[[ -n "$GRAPH_DIR" ]] || die "--graph-dir is required."
[[ -n "$GRAPH_ID" ]] || die "--graph-id is required."
[[ -d "$GRAPH_DIR" ]] || die "graph directory not found: ${GRAPH_DIR}"
[[ -f "${GRAPH_DIR}/nodes.csv" ]] || die "nodes.csv not found in ${GRAPH_DIR}"
[[ -f "${GRAPH_DIR}/edges.csv" ]] || die "edges.csv not found in ${GRAPH_DIR}"
[[ "$MAX_DEPTH" =~ ^[0-9]+$ ]] || die "--max-depth must be a non-negative integer."
[[ "$BATCH_SIZE" =~ ^[0-9]+$ ]] && (( BATCH_SIZE >= 1 )) || die "--batch-size must be a positive integer."

if (( DRY_RUN == 0 )) && [[ -z "$PASSWORD" ]]; then
  die "Neo4j password required: pass --password or set NEO4J_PASSWORD (or use --dry-run)."
fi

command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH."
[[ -f "$IMPORTER_SCRIPT" ]] || die "importer script not found: ${IMPORTER_SCRIPT}"
if (( DRY_RUN == 0 )) && ! python3 -c "import neo4j" >/dev/null 2>&1; then
  die "The 'neo4j' Python package is required for a real import. Install it with: pip install neo4j (or use --dry-run)."
fi

ARGS=(
  --graph-dir "$GRAPH_DIR"
  --graph-id "$GRAPH_ID"
  --max-depth "$MAX_DEPTH"
  --time-budget-minutes "$TIME_BUDGET_MINUTES"
  --batch-size "$BATCH_SIZE"
  --uri "$URI"
  --user "$USER_NAME"
  --password "$PASSWORD"
  --database "$DATABASE"
)
(( CLEAR_EXISTING == 1 )) && ARGS+=(--clear-existing)
(( DRY_RUN == 1 )) && ARGS+=(--dry-run)
(( NO_HTML == 1 )) && ARGS+=(--no-html)
[[ -n "$HTML_OUTPUT" ]] && ARGS+=(--html-output "$HTML_OUTPUT")

echo "Importing address-flow graph into Neo4j"
echo "Graph dir          : ${GRAPH_DIR}"
echo "Graph id           : ${GRAPH_ID}"
echo "Neo4j URI          : ${URI}"
echo "Database           : ${DATABASE}"
echo "Time budget (min)  : ${TIME_BUDGET_MINUTES}"
echo "Batch size         : ${BATCH_SIZE}"

python3 "$IMPORTER_SCRIPT" "${ARGS[@]}"
