# True Volume Pipeline — Progress Report

*Updated 2026-08-20T16:48:34+00:00*
Pipeline state file: `BTC_True_Volume/pipeline_state.json`
Master plan: `BTC_True_Volume/master_true_volume_pipeline_plan.md`

## Phase status

| Phase | Status | Attempts | Finished (UTC) | Last error |
|---|---|---|---|---|
| Step 0 — exchange true volume (spot+futures tape rollup) | completed | 1 | 2026-08-20T16:43:52+00:00 |  |
| Phase 1-BTC — network metrics (Blockchain.com) | completed | 1 | 2026-08-20T16:43:58+00:00 |  |
| Phase 1-ETH — network metrics (Etherscan/CoinMetrics) | completed | 1 | 2026-08-20T16:44:03+00:00 |  |
| Phase 2 — futures positioning (Vision metrics backfill) | completed | 1 | 2026-08-20T16:47:11+00:00 |  |
| Phase 3 — options verification (Deribit) | completed | 1 | 2026-08-20T16:47:16+00:00 |  |
| Phase 4 — checkonchain investigation | completed | 1 | 2026-08-20T16:47:24+00:00 |  |
| Phase 5 — realized cap (CoinMetrics derived) | completed | 1 | 2026-08-20T16:47:29+00:00 |  |
| Phase 6 — MM research (link) | completed | 1 | 2026-08-20T16:47:34+00:00 |  |
| Phase 7 — forward-only reserves (DEFERRED) | completed | 1 | 2026-08-20T16:47:39+00:00 |  |
| Phase 8 — full node cost/benefit (approval gate) | completed | 1 | 2026-08-20T16:47:44+00:00 |  |
| Final — merged deliverable build | completed | 1 | 2026-08-20T16:47:50+00:00 |  |
| Register — data_registry.yaml entries | completed | 1 | 2026-08-20T16:47:55+00:00 |  |
| Verify — end-to-end checks | completed | 1 | 2026-08-20T16:48:01+00:00 |  |

## Final deliverables

- **BTC**: 6,438 rows, 2009-01-03 → 2026-08-19, 75 columns
  - `exch` block — binance_vision_aggtrades / High
  - `net` block — blockchain.com_charts_api / High
  - `rc` block — coinmetrics_community_derived / Medium
  - `pos` block — binance_vision_metrics / Medium
  - `pos_options` block — deribit_public_api / Low
  - `onch` block — onchain_dataset_local / Medium
- **ETH**: 4,039 rows, 2015-07-30 → 2026-08-19, 66 columns
  - `exch` block — binance_vision_aggtrades / High
  - `net` block — coinmetrics_community_api / Medium
  - `rc` block — coinmetrics_community_derived / Medium
  - `pos` block — binance_vision_metrics / Medium
  - `pos_options` block — deribit_public_api / Low
  - `onch` block — coinmetrics_community_derived / Medium

## Verification

- step0_exchange: PASS
- phase1_network: PASS
- phase2_positioning: PASS
- phase5_realized_cap: PASS
- final_deliverable: PASS
- registry_and_log: PASS

## Registry entries added

```
# ============================================================================
# Data Registry — single source of truth for dataset paths, quality flags, caveats
# ============================================================================
# Authoritative registry for every dataset in this project (both trees:
#   D:\kp_ai_agent\Dataset_Fetcher\  and  D:\kp_ai_agent\pytorch_NN_models_2\Dataset\)
# Every entry's path/rows/date_range was verified on disk 2026-08-14 (see
# Dataset_Fetcher/fetching/registry_verify_report.json for the machine-readable
# verification pass). Do NOT hardcode dataset paths in code — use the loader
# (Dataset_Fetcher/data_registry.py).
#
# Existing summary registries (binance_full_dataset/dataset_registry.json/.md,
# commodity-other/reports/dataset_registry.md) are regenerable statistics and are
# SECONDARY to this file for path/caveat/quality lookups.

registry_version: "1.0"
generated_utc: "2026-08-14T06:40:00+00:00"
loader: "D:\\kp_ai_agent\\Dataset_Fetcher\\data_registry.py"

datasets:

  # ---------------- Binance futures / spot kline stores ----------------
  futures_klines_17sym:
    description: "Binance USDT-M futures klines, 17 symbols x 15 timeframes, merged per symbol+timeframe"
    path: "D:\\kp_ai_agent\\Dataset_Fetcher\\binance_full_dataset\\processed\\futures_um\\klines"
    path_template: "{path}\\{SYM}\\{TF}\\merged.parquet"
    source: "Binance USDS-M futures /fapi/v1/klines"
    file_format: "parquet"
    symbols: ["AAVEUSDT","ADAUSDT","ASTERUSDT","AVAXUSDT","BNBUSDT","BTCUSDT","CRVUSDT","DOGEUSDT","DOTUSDT","ETHUSDT","HYPEUSDT","LINKUSDT","PAXGUSDT","SOLUSDT","SUIUSDT","UNIUSDT","XRPUSDT"]
    timeframe: ["1m","3m","5m","15m","30m","1h","2h","4h","6h","8h","12h","1d","3d","1w","1mo"]
    date_range: {"start": "2020-01-01", "end": "2026-06-30"}
    columns: ["open_time","open","high","low","close","volume","close_time","quote_asset_volume","num_trades","taker_buy_base_vol","taker_buy_quote_vol","ignore","datetime_utc","symbol","market","data_type","interval","is_zero_volume","ohlc_integrity_flag"]
    row_count: 74665865
    last_verified: "2026-08-14"
    contains_synthetic: false
    known_caveats:
      - "store not updated since 2026-06-30 (~6.5 weeks stale as of 2026-08-14)"
      - "245/255 symbol x TF merged files present; 10 combinations missing"
      - "per-symbol start = perp listing date (e.g. AAVEUSDT 1m starts 2020-10-16); BTCUSDT from 2020-01-01"
      - "gap check (AAVEUSDT 1m full read): 14 missing 1-min bars over 2020-10..2026-06"
    update_cadence: "static"
    cost_tier: "majors"

  spot_klines_16sym:
    description: "Binance spot klines, 16 symbols x 15 timeframes (no HYPE on spot), merged"
    path: "D:\\kp_ai_agent\\Dataset_Fetcher\\binance_full_dataset\\processed\\spot\\klines"
    path_template: "{path}\\{SYM}\\{TF}\\merged.parquet"
    source: "Binance spot /api/v3/klines"
    file_format: "parquet"
    symbols: ["AAVEUSDT","ADAUSDT","ASTERUSDT","AVAXUSDT","BNBUSDT","BTCUSDT","CRVUSDT","DOGEUSDT","DOTUSDT","ETHUSDT","LINKUSDT","PAXGUSDT","SOLUSDT","SUIUSDT","UNIUSDT","XRPUSDT"]
    timeframe: ["1m","3m","5m","15m","30m","1h","2h","4h","6h","8h","12h","1d","3d","1w","1mo"]
    date_range: {"start": "2020-01-01", "end": "2024-12-31"}
    columns: ["open_time","open","high","low","close","volume","close_time","quote_asset_volume","num_trades","taker_buy_base_vol","taker_buy_quote_vol","ignore","datetime_utc","symbol","market","data_type","interval","is_zero_volume","ohlc_integrity_flag"]
    row_count: 63025074
    last_verified: "2026-08-14"
    contains_synthetic: false
    known_caveats:
      - "STALE: spot store ends 2024-12-31 (~19 months behind as of 2026-08-14) — do not use for recent-regime work"
      - "HYPEUSDT absent (no spot listing)"
      - "226/240 symbol x TF merged files present"
    update_cadence: "static"
    cost_tier: "majors"

  futures_premiumIndexKlines_17sym:
    description: "Futures premium-index klines (perpetual premium/basis per candle), 17 symbols"
    path: "D:\\kp_ai_agent\\Dataset_Fetcher\\binance_full_dataset\\processed\\futures_um\\premiumIndexKlines"
    path_template: "{path}\\{SYM}\\{TF}\\merged.parquet"
    source: "Binance /fapi/v1/premiumIndexKlines"
    file_format: "parquet"
    symbols: ["AAVEUSDT","ADAUSDT","ASTERUSDT","AVAXUSDT","BNBUSDT","BTCUSDT","CRVUSDT","DOGEUSDT","DOTUSDT","ETHUSDT","HYPEUSDT","LINKUSDT","PAXGUSDT","SOLUSDT","SUIUSDT","UNIUSDT","XRPUSDT"]
    timeframe: ["1m","3m","5m","15m","30m","1h","2h","4h","6h","8h","12h","1d","3d","1w","1mo"]
    date_range: {"start": "2020-01-01", "end": "2026-06-30"}
    columns: ["open_time","premium_index_open","premium_index_high","premium_index_low","premium_index_close","close_time","datetime_utc","symbol"]
    row_count: null
    last_verified: "2026-08-14"
    contains_synthetic: false
    known_caveats:
      - "246 merged files present (9 missing of 255)"
      - "same stale end 2026-06-30 as futures klines"
    update_cadence: "static"
    cost_tier: "majors"

  futures_indexPriceKlines_17sym:
    description: "Futures index-price klines (underlying index per candle), 17 symbols"
    path: "D:\\kp_ai_agent\\Dataset_Fetcher\\binance_full_dataset\\processed\\futures_um\\indexPriceKlines"
    path_template: "{path}\\{SYM}\\{TF}\\merged.parquet"
    source: "Binance /fapi/v1/indexPriceKlines"
    file_format: "parquet"
    symbols: ["AAVEUSDT","ADAUSDT","ASTERUSDT","AVAXUSDT","BNBUSDT","BTCUSDT","CRVUSDT","DOGEUSDT","DOTUSDT","ETHUSDT","HYPEUSDT","LINKUSDT","PAXGUSDT","SOLUSDT","SUIUSDT","UNIUSDT","XRPUSDT"]
    timeframe: ["1m","3m","5m","15m","30m","1h","2h","4h","6h","8h","12h","1d","3d","1w","1mo"]
    date_range: {"start": "2020-01-01", "end": "2026-06-30"}
    columns: ["open_time","open","high","low","close","close_time","datetime_utc","symbol"]
    row_count: null
    last_verified: "2026-08-14"
    contains_synthetic: false
    known_caveats: ["245 merged files present; stale end 2026-06-30"]
    update_cad
```

## Pipeline log (embedded)

```
_no log yet_
```
