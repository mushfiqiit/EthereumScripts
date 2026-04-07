curl -s -X POST http://10.112.249.200:8545   -H "Content-Type: application/json"   --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' | jq -r '.result' | xargs printf "%d\n"
