# Dataset Usage Guide — BTC & ETH True Volume (proxy stack)

*Generated 2026-08-20T16:48:34+00:00*

## How to load anything

```python
import pandas as pd
df = pd.read_parquet('processed/true_volume/BTC/merged.parquet')
df['date'] = pd.to_datetime(df['date'], utc=True)   # daily index, UTC midnight
df.set_index('date', inplace=True)
df['exch_combined_volume'].tail()                   # daily traded volume (tape)
```

## Golden rules

1. **These are proxy metrics, never literal true volume.** Each block column pair `{block}_data_source` / `{block}_confidence_tier` states what the block actually is and how much to trust it.
2. Check `exch_coverage_mode` before comparing volumes across years — spot coverage ends 2024-12-31 (futures_only days after).
3. NaN is a real state: block sources end on different dates (spot 2024-12-31, onch BTC 2026-07-14, aggTrades ETH 2026-06-16).
4. `true_volume_pipeline_log.md` records every build's coverage, tier and limitations — read it before using a dataset for the first time.

## Datasets

### exchange_true_volume_daily

BTC+ETH daily exchange true volume from the aggTrades tape (futures) cross-validated against kline stores, with num_trades arbitration.

- **Path**: `processed/exchange_true_volume/{BTCUSDT,ETHUSDT}/merged.parquet`
- **Confidence**: High (tape) / Medium (cross-fill days)
- **Load**: ```pd.read_parquet('processed/exchange_true_volume/BTCUSDT/merged.parquet')```

Notes:
  - exch_combined_volume = spot+futures where both exist, else single market (never NaN-sum)
  - exch_coverage_mode: spot_only | spot+futures | futures_only | gap
  - kline-artifact days quarantined (exch_fut_kline_artifact=True) — tape value stands
  - spot store stale after 2024-12-31; boundary month-first days cross-filled with flag
  - refresh: python -m BTC_True_Volume.exchange_true_volume --force

### onchain_network_metrics_btc_daily

BTC network metrics from Blockchain.com free charts API: estimated-transaction-volume-usd, n-transactions, n-unique-addresses, hash-rate, miners-revenue.

- **Path**: `processed/onchain/network_metrics/BTC/merged.parquet`
- **Confidence**: High
- **Load**: ```pd.read_parquet('processed/onchain/network_metrics/BTC/merged.parquet')```

Notes:
  - net_est_tx_volume_usd is the primary true-volume proxy (settled value moved on-chain, incl. non-trading transfers)
  - gap-free daily index, 00:00 UTC
  - refresh: python -m BTC_True_Volume.phase1_btc_network_metrics

### onchain_network_metrics_eth_daily

ETH network activity: Etherscan dailytx (if ETHERSCAN_API_KEY present in .env) else CoinMetrics TxCnt fallback.

- **Path**: `processed/onchain/network_metrics/ETH/merged.parquet`
- **Confidence**: Medium
- **Load**: ```pd.read_parquet('processed/onchain/network_metrics/ETH/merged.parquet')```

Notes:
  - without an Etherscan key the metric is tx count, not value
  - add ETHERSCAN_API_KEY to .env to upgrade (presence-only check — the key is never read)
  - refresh: python -m BTC_True_Volume.phase1_eth_network_metrics

### onchain_realized_cap_btc_daily

BTC realized cap derived from CoinMetrics community API: rc_realized_cap_usd = CapMrktCurUSD / CapMVRVCur (validated vs local onchain store, <2% over 2,387 days).

- **Path**: `processed/onchain/realized_cap/BTC/merged.parquet`
- **Confidence**: Medium
- **Load**: ```pd.read_parquet('processed/onchain/realized_cap/BTC/merged.parquet')```

Notes:
  - aggregate only — no LTH/STH split (CapRealUSD needs paid tier)
  - also carries SplyExNtv / FlowInExNtv / FlowOutExNtv (exchange supply & flows)
  - refresh: python -m BTC_True_Volume.phase5_realized_cap

### onchain_realized_cap_eth_daily

ETH realized cap derived the same way from CoinMetrics (eth).

- **Path**: `processed/onchain/realized_cap/ETH/merged.parquet`
- **Confidence**: Medium
- **Load**: ```pd.read_parquet('processed/onchain/realized_cap/ETH/merged.parquet')```

Notes:
  - same derivations as BTC; flow columns feed the onch block in the final
  - refresh: python -m BTC_True_Volume.phase5_realized_cap

### true_volume_btc_daily

FINAL BTC deliverable — outer-join of exch + net + rc + onch (local store) + pos + pos_options blocks.

- **Path**: `processed/true_volume/BTC/merged.parquet`
- **Confidence**: per-block (exch High, net High, rc Medium, onch Medium, pos Medium, pos_options Low)
- **Load**: ```pd.read_parquet('processed/true_volume/BTC/merged.parquet')```

Notes:
  - every block carries {block}_data_source and {block}_confidence_tier columns — read them
  - PROXY METRICS — never literal true volume
  - BTC onch block ends 2026-07-14 (local onchain_dataset); NaN after
  - pos block: top-20% cohort sentiment proxy, NOT exchange-wide OI
  - pos_options block: single-snapshot verification (2026-08-14), NaN elsewhere

### true_volume_eth_daily

FINAL ETH deliverable — outer-join of exch + net + rc + onch (CoinMetrics flows) + pos + pos_options.

- **Path**: `processed/true_volume/ETH/merged.parquet`
- **Confidence**: per-block (exch High, net Medium, rc Medium, onch Medium, pos Medium, pos_options Low)
- **Load**: ```pd.read_parquet('processed/true_volume/ETH/merged.parquet')```

Notes:
  - ETH aggTrades tape ends 2026-06-16 unless extended with --resume-aggtrades (Vision ZIPs)
  - PROXY METRICS — never literal true volume
  - ETH onch block aliases CoinMetrics exchange flows
  - pos block: top-20% cohort sentiment proxy, NOT exchange-wide OI

## Refresh / extend

| Goal | Command |
|---|---|
| Full rebuild (all phases) | `python -m BTC_True_Volume.supervisor --auto --no-shutdown` |
| Force refetch of API caches | `python -m BTC_True_Volume.run_pipeline --force-refetch` |
| Extend ETH tape to 2026-08-13 | `python -m BTC_True_Volume.exchange_true_volume --resume-aggtrades` |
| Upgrade ETH network metric | add `ETHERSCAN_API_KEY` to `.env` (presence-only check), re-run phase1_eth |
| Re-register registry entries | `python -m BTC_True_Volume.register_outputs` (idempotent) |

## Schema dictionary

See `schema.md` (per-symbol column dictionary) and `_meta.json` next to every parquet for build provenance.
