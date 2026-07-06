#!/usr/bin/env bash
set -euo pipefail

# Create a larger Ethereum-like sample graph in a local Neo4j database so it can
# be visualized in Neo4j Browser at http://localhost:7474.

NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-}"
NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}"
RESET_SAMPLE=false
RUN_VISUALIZATION_QUERY=true
GENERATE_HTML=true
HTML_OUTPUT="${HTML_OUTPUT:-sample_neo4j_graph.html}"

usage() {
  cat <<'USAGE'
Usage:
  ./create_neo4j_sample_graph.sh [options]

Creates a larger Ethereum-like graph in Neo4j for browser visualization.

Options:
  --uri URI          Bolt URI (default: bolt://localhost:7687, or NEO4J_URI)
  --user USER        Neo4j user (default: neo4j, or NEO4J_USER)
  --password PASS    Neo4j password (default: prompt, or NEO4J_PASSWORD)
  --database DB      Neo4j database (default: neo4j, or NEO4J_DATABASE)
  --reset-sample     Delete only previously-created sample nodes first
  --no-query         Create the sample graph but skip the final test query
  --html-output FILE Write a standalone interactive HTML graph (default: sample_neo4j_graph.html)
  --no-html          Do not write the standalone HTML graph file
  -h, --help         Show this help and exit

Examples:
  ./create_neo4j_sample_graph.sh
  NEO4J_PASSWORD='your-password' ./create_neo4j_sample_graph.sh --reset-sample

By default, this script also runs the sample graph query through cypher-shell,
so you do not need to paste Cypher into Bash manually. To see the graph
visually, open http://localhost:7474 and use the query shown in the final
script output if you want to inspect it in Neo4j Browser:
  MATCH p=(:SampleAddress)-[*1..2]-(:SampleAddress) RETURN p;

It also writes a standalone HTML visualization file that can be opened locally
or sent to someone else without requiring them to connect to your Neo4j server.
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
    --html-output)
      require_value "$1" "${2-}"
      HTML_OUTPUT="$2"
      shift 2
      ;;
    --no-html)
      GENERATE_HTML=false
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
MERGE (dave:SampleAddress {address: '0xdave000000000000000000000000000000000006'})
  SET dave.name = 'Dave Wallet'
MERGE (erin:SampleAddress {address: '0xerin000000000000000000000000000000000007'})
  SET erin.name = 'Erin Wallet'
MERGE (frank:SampleAddress {address: '0xfrank0000000000000000000000000000000008'})
  SET frank.name = 'Frank Wallet'
MERGE (grace:SampleAddress {address: '0xgrace000000000000000000000000000000009'})
  SET grace.name = 'Grace Wallet'
MERGE (heidi:SampleAddress {address: '0xheidi000000000000000000000000000000000a'})
  SET heidi.name = 'Heidi Wallet'
MERGE (bridge:SampleAddress {address: '0xbridge00000000000000000000000000000000b'})
  SET bridge.name = 'Sample Bridge Contract'
MERGE (staking:SampleAddress {address: '0xstaking0000000000000000000000000000000c'})
  SET staking.name = 'Sample Staking Contract'
MERGE (market:SampleAddress {address: '0xmarket00000000000000000000000000000000d'})
  SET market.name = 'Sample NFT Marketplace'
MERGE (miner:SampleAddress {address: '0xminer000000000000000000000000000000000e'})
  SET miner.name = 'Sample Miner/Validator'
MERGE (token:SampleToken {address: '0xtoken00000000000000000000000000000000005'})
  SET token.symbol = 'SAMP', token.name = 'Sample Token'
MERGE (usdc:SampleToken {address: '0xusdc0000000000000000000000000000000000f'})
  SET usdc.symbol = 'USDC', usdc.name = 'Sample USD Coin'
MERGE (nft:SampleToken {address: '0xnft000000000000000000000000000000000010'})
  SET nft.symbol = 'SNFT', nft.name = 'Sample NFT Collection'

MERGE (tx1:SampleTransaction {hash: '0xtxsample001'})
  SET tx1.block_number = 25415601, tx1.value_eth = 1.25
MERGE (tx2:SampleTransaction {hash: '0xtxsample002'})
  SET tx2.block_number = 25415642, tx2.value_eth = 0.40
MERGE (tx3:SampleTransaction {hash: '0xtxsample003'})
  SET tx3.block_number = 25415710, tx3.value_eth = 0.00
MERGE (tx4:SampleTransaction {hash: '0xtxsample004'})
  SET tx4.block_number = 25415788, tx4.value_eth = 3.20
MERGE (tx5:SampleTransaction {hash: '0xtxsample005'})
  SET tx5.block_number = 25415803, tx5.value_eth = 0.75
MERGE (tx6:SampleTransaction {hash: '0xtxsample006'})
  SET tx6.block_number = 25415844, tx6.value_eth = 0.00
MERGE (tx7:SampleTransaction {hash: '0xtxsample007'})
  SET tx7.block_number = 25415910, tx7.value_eth = 2.10
MERGE (tx8:SampleTransaction {hash: '0xtxsample008'})
  SET tx8.block_number = 25415955, tx8.value_eth = 0.15
MERGE (tx9:SampleTransaction {hash: '0xtxsample009'})
  SET tx9.block_number = 25416001, tx9.value_eth = 0.00
MERGE (tx10:SampleTransaction {hash: '0xtxsample010'})
  SET tx10.block_number = 25416077, tx10.value_eth = 4.60
MERGE (tx11:SampleTransaction {hash: '0xtxsample011'})
  SET tx11.block_number = 25416125, tx11.value_eth = 0.05
MERGE (tx12:SampleTransaction {hash: '0xtxsample012'})
  SET tx12.block_number = 25416190, tx12.value_eth = 0.00

MERGE (alice)-[:SENT {value_eth: 1.25}]->(tx1)
MERGE (tx1)-[:RECEIVED]->(bob)
MERGE (bob)-[:SENT {value_eth: 0.40}]->(tx2)
MERGE (tx2)-[:RECEIVED]->(dex)
MERGE (dex)-[:SENT]->(tx3)
MERGE (tx3)-[:RECEIVED]->(carol)
MERGE (token)-[:TOKEN_TRANSFER {amount: 2500, block_number: 25415710}]->(carol)
MERGE (bob)-[:APPROVED {token: 'SAMP'}]->(dex)
MERGE (carol)-[:SENT {value_eth: 3.20}]->(tx4)
MERGE (tx4)-[:RECEIVED]->(bridge)
MERGE (bridge)-[:SENT {value_eth: 2.95}]->(tx5)
MERGE (tx5)-[:RECEIVED]->(dave)
MERGE (dave)-[:SENT]->(tx6)
MERGE (tx6)-[:RECEIVED]->(staking)
MERGE (staking)-[:REWARD {value_eth: 0.18}]->(erin)
MERGE (erin)-[:SENT {value_eth: 2.10}]->(tx7)
MERGE (tx7)-[:RECEIVED]->(frank)
MERGE (frank)-[:SENT {value_eth: 0.15}]->(tx8)
MERGE (tx8)-[:RECEIVED]->(market)
MERGE (market)-[:SENT]->(tx9)
MERGE (tx9)-[:RECEIVED]->(grace)
MERGE (grace)-[:SENT {value_eth: 4.60}]->(tx10)
MERGE (tx10)-[:RECEIVED]->(dex)
MERGE (dex)-[:SENT {value_eth: 0.05}]->(tx11)
MERGE (tx11)-[:RECEIVED]->(heidi)
MERGE (heidi)-[:SENT]->(tx12)
MERGE (tx12)-[:RECEIVED]->(miner)
MERGE (usdc)-[:TOKEN_TRANSFER {amount: 1250000, block_number: 25415844}]->(dave)
MERGE (usdc)-[:TOKEN_TRANSFER {amount: 780000, block_number: 25416077}]->(dex)
MERGE (nft)-[:NFT_TRANSFER {token_id: 42, block_number: 25415955}]->(grace)
MERGE (alice)-[:WATCHES]->(market)
MERGE (miner)-[:VALIDATED]->(tx12)

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

if [[ "$GENERATE_HTML" == true ]]; then
  mkdir -p "$(dirname "$HTML_OUTPUT")"
  cat > "$HTML_OUTPUT" <<'HTML'
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Neo4j Ethereum Sample Graph</title>
  <style>
    :root {
      color-scheme: light dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    body {
      margin: 0;
      background: #111827;
      color: #f9fafb;
    }
    header {
      padding: 16px 20px;
      border-bottom: 1px solid #374151;
      background: #0f172a;
    }
    h1 {
      margin: 0 0 6px;
      font-size: 20px;
    }
    p {
      margin: 0;
      color: #cbd5e1;
    }
    #graph {
      width: 100vw;
      height: calc(100vh - 86px);
      display: block;
      cursor: grab;
      background:
        radial-gradient(circle at 24px 24px, rgba(148, 163, 184, 0.14) 2px, transparent 0) 0 0 / 48px 48px,
        linear-gradient(135deg, #111827 0%, #1f2937 100%);
    }
    .edge {
      stroke: #94a3b8;
      stroke-width: 2.4;
      marker-end: url(#arrow);
    }
    .edge-label {
      fill: #e5e7eb;
      font-size: 12px;
      paint-order: stroke;
      stroke: #111827;
      stroke-width: 4px;
      stroke-linejoin: round;
    }
    .node {
      cursor: move;
      filter: drop-shadow(0 8px 16px rgba(0, 0, 0, 0.35));
    }
    .node circle {
      stroke: #f8fafc;
      stroke-width: 2;
    }
    .address circle {
      fill: #2563eb;
    }
    .transaction circle {
      fill: #16a34a;
    }
    .token circle {
      fill: #f97316;
    }
    .node text {
      fill: #ffffff;
      font-size: 12px;
      font-weight: 700;
      text-anchor: middle;
      dominant-baseline: middle;
      pointer-events: none;
    }
    .caption {
      fill: #e5e7eb;
      font-size: 12px;
      text-anchor: middle;
      pointer-events: none;
    }
  </style>
</head>
<body>
  <header>
    <h1>Neo4j Ethereum Sample Graph</h1>
    <p>Standalone interactive HTML export. Drag nodes to rearrange the graph. This file contains only sample data.</p>
  </header>
  <svg id="graph" role="img" aria-label="Interactive Ethereum-like graph visualization">
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8"></path>
      </marker>
    </defs>
    <g id="edges"></g>
    <g id="edge-labels"></g>
    <g id="nodes"></g>
  </svg>
  <script>
    const nodes = [
      { id: "alice", label: "Alice", caption: "SampleAddress", type: "address", x: 90, y: 130 },
      { id: "tx1", label: "tx1", caption: "SampleTransaction", type: "transaction", x: 240, y: 130 },
      { id: "bob", label: "Bob", caption: "SampleAddress", type: "address", x: 390, y: 130 },
      { id: "tx2", label: "tx2", caption: "SampleTransaction", type: "transaction", x: 540, y: 130 },
      { id: "dex", label: "DEX", caption: "SampleAddress", type: "address", x: 690, y: 130 },
      { id: "tx3", label: "tx3", caption: "SampleTransaction", type: "transaction", x: 840, y: 130 },
      { id: "carol", label: "Carol", caption: "SampleAddress", type: "address", x: 990, y: 130 },
      { id: "bridge", label: "Bridge", caption: "SampleAddress", type: "address", x: 90, y: 330 },
      { id: "tx4", label: "tx4", caption: "SampleTransaction", type: "transaction", x: 240, y: 330 },
      { id: "dave", label: "Dave", caption: "SampleAddress", type: "address", x: 390, y: 330 },
      { id: "tx5", label: "tx5", caption: "SampleTransaction", type: "transaction", x: 540, y: 330 },
      { id: "staking", label: "Stake", caption: "SampleAddress", type: "address", x: 690, y: 330 },
      { id: "tx6", label: "tx6", caption: "SampleTransaction", type: "transaction", x: 840, y: 330 },
      { id: "erin", label: "Erin", caption: "SampleAddress", type: "address", x: 990, y: 330 },
      { id: "frank", label: "Frank", caption: "SampleAddress", type: "address", x: 90, y: 530 },
      { id: "tx7", label: "tx7", caption: "SampleTransaction", type: "transaction", x: 240, y: 530 },
      { id: "market", label: "Market", caption: "SampleAddress", type: "address", x: 390, y: 530 },
      { id: "tx8", label: "tx8", caption: "SampleTransaction", type: "transaction", x: 540, y: 530 },
      { id: "grace", label: "Grace", caption: "SampleAddress", type: "address", x: 690, y: 530 },
      { id: "tx9", label: "tx9", caption: "SampleTransaction", type: "transaction", x: 840, y: 530 },
      { id: "heidi", label: "Heidi", caption: "SampleAddress", type: "address", x: 990, y: 530 },
      { id: "tx10", label: "tx10", caption: "SampleTransaction", type: "transaction", x: 1140, y: 330 },
      { id: "tx11", label: "tx11", caption: "SampleTransaction", type: "transaction", x: 1140, y: 130 },
      { id: "tx12", label: "tx12", caption: "SampleTransaction", type: "transaction", x: 1140, y: 530 },
      { id: "miner", label: "Miner", caption: "SampleAddress", type: "address", x: 1290, y: 530 },
      { id: "token", label: "SAMP", caption: "SampleToken", type: "token", x: 90, y: 730 },
      { id: "usdc", label: "USDC", caption: "SampleToken", type: "token", x: 240, y: 730 },
      { id: "nft", label: "SNFT", caption: "SampleToken", type: "token", x: 390, y: 730 }
    ];

    const edges = [
      { from: "alice", to: "tx1", label: "SENT 1.25 ETH" },
      { from: "tx1", to: "bob", label: "RECEIVED" },
      { from: "bob", to: "tx2", label: "SENT 0.40 ETH" },
      { from: "tx2", to: "dex", label: "RECEIVED" },
      { from: "dex", to: "tx3", label: "SENT" },
      { from: "tx3", to: "carol", label: "RECEIVED" },
      { from: "token", to: "carol", label: "TOKEN_TRANSFER 2500" },
      { from: "bob", to: "dex", label: "APPROVED SAMP" },
      { from: "carol", to: "tx4", label: "SENT 3.20 ETH" },
      { from: "tx4", to: "bridge", label: "RECEIVED" },
      { from: "bridge", to: "tx5", label: "SENT 2.95 ETH" },
      { from: "tx5", to: "dave", label: "RECEIVED" },
      { from: "dave", to: "tx6", label: "SENT" },
      { from: "tx6", to: "staking", label: "RECEIVED" },
      { from: "staking", to: "erin", label: "REWARD 0.18 ETH" },
      { from: "erin", to: "tx7", label: "SENT 2.10 ETH" },
      { from: "tx7", to: "frank", label: "RECEIVED" },
      { from: "frank", to: "tx8", label: "SENT 0.15 ETH" },
      { from: "tx8", to: "market", label: "RECEIVED" },
      { from: "market", to: "tx9", label: "SENT" },
      { from: "tx9", to: "grace", label: "RECEIVED" },
      { from: "grace", to: "tx10", label: "SENT 4.60 ETH" },
      { from: "tx10", to: "dex", label: "RECEIVED" },
      { from: "dex", to: "tx11", label: "SENT 0.05 ETH" },
      { from: "tx11", to: "heidi", label: "RECEIVED" },
      { from: "heidi", to: "tx12", label: "SENT" },
      { from: "tx12", to: "miner", label: "RECEIVED" },
      { from: "usdc", to: "dave", label: "TOKEN_TRANSFER 1.25M" },
      { from: "usdc", to: "dex", label: "TOKEN_TRANSFER 780K" },
      { from: "nft", to: "grace", label: "NFT_TRANSFER #42" },
      { from: "alice", to: "market", label: "WATCHES" },
      { from: "miner", to: "tx12", label: "VALIDATED" }
    ];

    const svg = document.getElementById("graph");
    const edgeLayer = document.getElementById("edges");
    const labelLayer = document.getElementById("edge-labels");
    const nodeLayer = document.getElementById("nodes");
    const nodeById = new Map(nodes.map((node) => [node.id, node]));
    let selectedNode = null;
    let pointerOffset = { x: 0, y: 0 };

    function svgPoint(event) {
      const point = svg.createSVGPoint();
      point.x = event.clientX;
      point.y = event.clientY;
      return point.matrixTransform(svg.getScreenCTM().inverse());
    }

    function render() {
      edgeLayer.replaceChildren();
      labelLayer.replaceChildren();
      nodeLayer.replaceChildren();

      for (const edge of edges) {
        const source = nodeById.get(edge.from);
        const target = nodeById.get(edge.to);
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("class", "edge");
        line.setAttribute("x1", source.x);
        line.setAttribute("y1", source.y);
        line.setAttribute("x2", target.x);
        line.setAttribute("y2", target.y);
        edgeLayer.appendChild(line);

        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("class", "edge-label");
        label.setAttribute("x", (source.x + target.x) / 2);
        label.setAttribute("y", (source.y + target.y) / 2 - 8);
        label.textContent = edge.label;
        labelLayer.appendChild(label);
      }

      for (const node of nodes) {
        const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
        group.setAttribute("class", `node ${node.type}`);
        group.setAttribute("transform", `translate(${node.x}, ${node.y})`);
        group.addEventListener("pointerdown", (event) => {
          selectedNode = node;
          const point = svgPoint(event);
          pointerOffset = { x: node.x - point.x, y: node.y - point.y };
          group.setPointerCapture(event.pointerId);
        });

        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circle.setAttribute("r", 42);
        group.appendChild(circle);

        const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
        text.textContent = node.label;
        group.appendChild(text);

        const caption = document.createElementNS("http://www.w3.org/2000/svg", "text");
        caption.setAttribute("class", "caption");
        caption.setAttribute("y", 62);
        caption.textContent = node.caption;
        group.appendChild(caption);

        nodeLayer.appendChild(group);
      }
    }

    svg.addEventListener("pointermove", (event) => {
      if (!selectedNode) return;
      const point = svgPoint(event);
      selectedNode.x = point.x + pointerOffset.x;
      selectedNode.y = point.y + pointerOffset.y;
      render();
    });

    svg.addEventListener("pointerup", () => {
      selectedNode = null;
    });

    svg.addEventListener("pointerleave", () => {
      selectedNode = null;
    });

    render();
  </script>
</body>
</html>
HTML
fi

cat <<INFO

Sample graph loaded successfully.
INFO

if [[ "$RUN_VISUALIZATION_QUERY" == true ]]; then
  echo "The sample graph query has already been run by this script."
else
  echo "The sample graph query was skipped because --no-query was provided."
fi

if [[ "$GENERATE_HTML" == true ]]; then
  echo "Standalone interactive HTML graph written to: ${HTML_OUTPUT}"
else
  echo "Standalone HTML graph generation was skipped because --no-html was provided."
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
