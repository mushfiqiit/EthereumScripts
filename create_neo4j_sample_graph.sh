#!/usr/bin/env bash
set -euo pipefail

# Create a small Ethereum-like sample graph in a local Neo4j database so it can
# be visualized in Neo4j Browser at http://localhost:7474.

NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-}"
NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}"
RESET_SAMPLE=false
RUN_VISUALIZATION_QUERY=true

usage() {
  cat <<'USAGE'
Usage:
  ./create_neo4j_sample_graph.sh [options]

Creates a tiny Ethereum-like graph in Neo4j for browser visualization.

Options:
  --uri URI          Bolt URI (default: bolt://localhost:7687, or NEO4J_URI)
  --user USER        Neo4j user (default: neo4j, or NEO4J_USER)
  --password PASS    Neo4j password (default: prompt, or NEO4J_PASSWORD)
  --database DB      Neo4j database (default: neo4j, or NEO4J_DATABASE)
  --reset-sample     Delete only previously-created sample nodes first
  --no-query         Create the sample graph but skip the final test query
  -h, --help         Show this help and exit

Examples:
  ./create_neo4j_sample_graph.sh
  NEO4J_PASSWORD='your-password' ./create_neo4j_sample_graph.sh --reset-sample

By default, this script also runs the sample graph query through cypher-shell,
so you do not need to paste Cypher into Bash manually. To see the graph
visually, open http://localhost:7474 and use the query shown in the final
script output if you want to inspect it in Neo4j Browser:
  MATCH p=(:SampleAddress)-[*1..2]-(:SampleAddress) RETURN p;
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
    --uri)
      require_value "$1" "${2-}"
      NEO4J_URI="$2"
      shift 2
      ;;
    --user)
      require_value "$1" "${2-}"
      NEO4J_USER="$2"
      shift 2
      ;;
    --password)
      require_value "$1" "${2-}"
      NEO4J_PASSWORD="$2"
      shift 2
      ;;
    --database)
      require_value "$1" "${2-}"
      NEO4J_DATABASE="$2"
      shift 2
      ;;
    --reset-sample)
      RESET_SAMPLE=true
      shift
      ;;
    --no-query)
      RUN_VISUALIZATION_QUERY=false
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

command -v cypher-shell >/dev/null 2>&1 || \
  die "cypher-shell was not found in PATH. Install Neo4j client tools or check your Neo4j package installation."

if [[ -z "$NEO4J_PASSWORD" ]]; then
  read -r -s -p "Neo4j password for ${NEO4J_USER}: " NEO4J_PASSWORD
  echo
fi

CYPHER_FILE="$(mktemp)"
trap 'rm -f "$CYPHER_FILE"' EXIT

if [[ "$RESET_SAMPLE" == true ]]; then
  cat > "$CYPHER_FILE" <<'CYPHER'
MATCH (n)
WHERE n:SampleAddress OR n:SampleTransaction OR n:SampleToken
DETACH DELETE n;
CYPHER

  cypher-shell \
    -a "$NEO4J_URI" \
    -u "$NEO4J_USER" \
    -p "$NEO4J_PASSWORD" \
    -d "$NEO4J_DATABASE" \
    -f "$CYPHER_FILE"
fi

cat > "$CYPHER_FILE" <<'CYPHER'
MERGE (alice:SampleAddress {address: '0xalice000000000000000000000000000000000001'})
  SET alice.name = 'Alice Wallet'
MERGE (bob:SampleAddress {address: '0xbob00000000000000000000000000000000000002'})
  SET bob.name = 'Bob Wallet'
MERGE (carol:SampleAddress {address: '0xcarol00000000000000000000000000000000003'})
  SET carol.name = 'Carol Wallet'
MERGE (dex:SampleAddress {address: '0xdex0000000000000000000000000000000000004'})
  SET dex.name = 'Sample DEX Contract'
MERGE (token:SampleToken {address: '0xtoken00000000000000000000000000000000005'})
  SET token.symbol = 'SAMP', token.name = 'Sample Token'

MERGE (tx1:SampleTransaction {hash: '0xtxsample001'})
  SET tx1.block_number = 25415601, tx1.value_eth = 1.25
MERGE (tx2:SampleTransaction {hash: '0xtxsample002'})
  SET tx2.block_number = 25415642, tx2.value_eth = 0.40
MERGE (tx3:SampleTransaction {hash: '0xtxsample003'})
  SET tx3.block_number = 25415710, tx3.value_eth = 0.00

MERGE (alice)-[:SENT {value_eth: 1.25}]->(tx1)
MERGE (tx1)-[:RECEIVED]->(bob)
MERGE (bob)-[:SENT {value_eth: 0.40}]->(tx2)
MERGE (tx2)-[:RECEIVED]->(dex)
MERGE (dex)-[:SENT]->(tx3)
MERGE (tx3)-[:RECEIVED]->(carol)
MERGE (token)-[:TOKEN_TRANSFER {amount: 2500, block_number: 25415710}]->(carol)
MERGE (bob)-[:APPROVED {token: 'SAMP'}]->(dex)

WITH 1 AS ignored
MATCH (n)
WHERE n:SampleAddress OR n:SampleTransaction OR n:SampleToken
RETURN 'sample graph created' AS status, count(n) AS sample_nodes;
CYPHER

cypher-shell \
  -a "$NEO4J_URI" \
  -u "$NEO4J_USER" \
  -p "$NEO4J_PASSWORD" \
  -d "$NEO4J_DATABASE" \
  -f "$CYPHER_FILE"

if [[ "$RUN_VISUALIZATION_QUERY" == true ]]; then
  cat > "$CYPHER_FILE" <<'CYPHER'
MATCH p=(:SampleAddress)-[*1..2]-(:SampleAddress)
RETURN p;
CYPHER

  echo
  echo "Running the sample graph query through cypher-shell now:"
  echo "  MATCH p=(:SampleAddress)-[*1..2]-(:SampleAddress) RETURN p;"
  echo

  cypher-shell \
    -a "$NEO4J_URI" \
    -u "$NEO4J_USER" \
    -p "$NEO4J_PASSWORD" \
    -d "$NEO4J_DATABASE" \
    -f "$CYPHER_FILE"
fi

cat <<INFO

Sample graph loaded successfully.
INFO

if [[ "$RUN_VISUALIZATION_QUERY" == true ]]; then
  echo "The sample graph query has already been run by this script."
else
  echo "The sample graph query was skipped because --no-query was provided."
fi

cat <<INFO

Open Neo4j Browser:
  http://localhost:7474

If you want a visual graph view in Neo4j Browser, paste the same query into
the Neo4j Browser query editor, not into the Bash terminal:
  MATCH p=(:SampleAddress)-[*1..2]-(:SampleAddress) RETURN p;

If Neo4j Browser asks for connection details, use:
  Connect URL: ${NEO4J_URI}
  Username   : ${NEO4J_USER}
  Database   : ${NEO4J_DATABASE}

If you want to test the query from Bash instead, run it through cypher-shell:
  cypher-shell -a "${NEO4J_URI}" -u "${NEO4J_USER}" -d "${NEO4J_DATABASE}" \
    "MATCH p=(:SampleAddress)-[*1..2]-(:SampleAddress) RETURN p;"
INFO
