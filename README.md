# EthereumScripts

Scripts for analyzing WETH/USDC transfers on Ethereum and plotting the derived exchange rates.

## Exchange-rate CSV fields

`build_weth_usdc_exchange_rates.py` reads WETH/USDC token-transfer CSV chunks and writes one row per matched exchange-like transaction/address pair with these columns:

```text
block_number,from_address,to_address,transaction_id,exchange_rate,weth_amount,max_priority_fee_per_gas,max_fee_per_gas
```

- `exchange_rate` is normalized USDC divided by normalized WETH.
- `weth_amount` is the normalized WETH amount transferred in the reverse direction of the USDC transfer.
- `max_priority_fee_per_gas` and `max_fee_per_gas` are transaction-level EIP-1559 fee cap fields. They are not emitted by `ethereumetl export_token_transfers`, so the script fills them only when you also provide transaction-export CSVs.

## Getting `max_priority_fee_per_gas` and `max_fee_per_gas`

Recommended approach: export transactions for the same block windows as your token-transfer chunks, then join by transaction hash:

```bash
ethereumetl export_transactions \
  --start-block 24965301 \
  --end-block 24965350 \
  --provider-uri http://10.112.249.200:8545 \
  --batch-size 100 \
  --max-workers 5 \
  --output weth_usdc_transaction_chunks/transactions_721_24965301_24965350.csv
```

Then build the exchange-rate CSVs with:

```bash
python3 build_weth_usdc_exchange_rates.py \
  --input-dir weth_usdc_transfer_chunks \
  --output-dir weth_usdc_exchange_rates \
  --transactions-dir weth_usdc_transaction_chunks
```

Alternative approach: call `eth_getTransactionByHash` for each matched `transaction_id` and read `maxPriorityFeePerGas` and `maxFeePerGas` from the JSON-RPC response. That avoids exporting full transaction CSV chunks, but it is usually slower and harder on the RPC node when you have many matched rows.
