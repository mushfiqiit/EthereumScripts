#!/usr/bin/env bash
set -euo pipefail

EL_HTTP="http://10.112.249.200:8545"
EL_WS="ws://10.112.249.200:8546"
CL_REST="http://10.112.249.200:5052"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

hex_to_dec() {
  python3 - <<'PY' "$1"
import sys
print(int(sys.argv[1], 16))
PY
}

require_cmd curl
require_cmd jq
require_cmd python3

echo "Execution Layer - Reth HTTP"
el_http_hex=$(curl -s -X POST "$EL_HTTP" \
  -H "Content-Type: application/json" \
  --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
  | jq -r '.result')

if [[ -n "${el_http_hex:-}" && "$el_http_hex" != "null" ]]; then
  echo "$(hex_to_dec "$el_http_hex")"
else
  echo "Failed to fetch latest block number from $EL_HTTP"
fi

echo
echo "Execution Layer - Reth WS"
if command -v websocat >/dev/null 2>&1; then
  el_ws_hex=$(printf '%s\n' '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
    | websocat -n1 "$EL_WS" \
    | jq -r '.result')

  if [[ -n "${el_ws_hex:-}" && "$el_ws_hex" != "null" ]]; then
    echo "$(hex_to_dec "$el_ws_hex")"
  else
    echo "Failed to fetch latest block number from $EL_WS"
  fi
else
  echo "websocat is not installed, so WS endpoint was skipped."
  echo "Install with: sudo apt install websocat"
fi

echo
echo "Consensus Layer - Nimbus REST"
cl_slot=$(curl -s "$CL_REST/eth/v1/beacon/headers/head" | jq -r '.data.header.message.slot')

if [[ -n "${cl_slot:-}" && "$cl_slot" != "null" ]]; then
  echo "$cl_slot"
else
  echo "Failed to fetch head slot from $CL_REST"
fi