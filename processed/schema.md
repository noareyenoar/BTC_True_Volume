# Schema — True Volume final deliverables

*Generated 2026-08-20T16:48:34+00:00*

Per-symbol column dictionary generated from the merged parquet files. Blocks: `exch` (exchange tape volume), `net` (network metrics), `rc` (realized cap), `onch` (on-chain exchange flows/reserve), `pos` (futures positioning), `pos_options` (options verification).

## BTC

| Column | Type | Block | Sample |
|---|---|---|---|
| `exch_spot_volume` | float64 | exchange | 1200 |
| `exch_spot_quote_volume_usd` | float64 | exchange | 5.087e+06 |
| `exch_spot_taker_buy` | float64 | exchange | 972.9 |
| `exch_spot_taker_sell` | float64 | exchange | 227 |
| `exch_spot_taker_buy_quote_usd` | float64 | exchange | 4.129e+06 |
| `exch_spot_taker_buy_ratio` | float64 | exchange | 0.8108 |
| `exch_spot_num_trades` | float64 | exchange | 5233 |
| `exch_fut_volume` | float64 | exchange | 5.68e+04 |
| `exch_fut_quote_volume_usd` | float64 | exchange | 4.097e+08 |
| `exch_fut_taker_buy` | float64 | exchange | 2.883e+04 |
| `exch_fut_taker_sell` | float64 | exchange | 2.797e+04 |
| `exch_fut_taker_buy_quote_usd` | float64 | exchange | 2.08e+08 |
| `exch_fut_taker_buy_ratio` | float64 | exchange | 0.5076 |
| `exch_fut_num_trades` | float64 | exchange | 1.015e+05 |
| `exch_fut_aggtrade_records` | float64 | exchange | 7.136e+04 |
| `exch_combined_volume` | float64 | exchange | 1200 |
| `exch_combined_quote_volume_usd` | float64 | exchange | 5.087e+06 |
| `exch_combined_taker_buy_ratio` | float64 | exchange | 0.8108 |
| `exch_coverage_mode` | str | exchange | spot_only |
| `exch_spot_cross_filled` | object | exchange | False |
| `exch_fut_cross_filled` | object | exchange | False |
| `exch_fut_kline_artifact` | object | exchange | False |
| `exch_fut_numtrades_corrected` | object | exchange | False |
| `net_estimated_tx_volume_usd` | float64 | network | 861.9 |
| `net_tx_count` | float64 | network | 109 |
| `net_unique_addresses` | float64 | network | 2 |
| `net_hash_rate` | float64 | network | 4.971e-08 |
| `net_miners_revenue_usd` | float64 | network | 0 |
| `net_mempool_size_bytes` | float64 | network | 3.193e+06 |
| `rc_realized_cap_usd` | float64 | realized_cap | 2027 |
| `rc_realized_price_usd` | float64 | realized_cap | 0.0005878 |
| `rc_mvrv_ratio` | float64 | realized_cap | 146 |
| `rc_net_exchange_flow` | float64 | realized_cap | 140 |
| `rc_exchange_supply` | float64 | realized_cap | 140 |
| `rc_market_cap_usd` | float64 | realized_cap | 2.96e+05 |
| `rc_price_usd` | float64 | realized_cap | 0.08584 |
| `pos_oi_usd` | float64 | positioning | 7.74e+08 |
| `pos_top_trader_ls_ratio` | float64 | positioning | 0.8702 |
| `pos_taker_ls_vol_ratio` | float64 | positioning | 0.6054 |
| `pos_top_trader_long_pct` | float64 | positioning | 0.4653 |
| `pos_top_trader_short_pct` | float64 | positioning | 0.5347 |
| `pos_top_trader_skew` | float64 | positioning | -0.03471 |
| `pos_options_instruments` | float64 | positioning | 820 |
| `pos_options_n_calls` | float64 | positioning | 410 |
| `pos_options_n_puts` | float64 | positioning | 410 |
| `pos_options_call_oi` | float64 | positioning | 2.241e+05 |
| `pos_options_put_oi` | float64 | positioning | 1.247e+05 |
| `pos_options_total_oi` | float64 | positioning | 3.488e+05 |
| `pos_options_put_call_oi_ratio` | float64 | positioning | 0.5567 |
| `pos_options_call_vol_24h` | float64 | positioning | 8842 |
| `pos_options_put_vol_24h` | float64 | positioning | 7107 |
| `pos_options_total_vol_24h` | float64 | positioning | 1.595e+04 |
| `pos_options_put_call_vol_ratio` | float64 | positioning | 0.8037 |
| `pos_options_top_oi_strike` | float64 | positioning | 6e+04 |
| `pos_options_top_oi_value` | float64 | positioning | 2.197e+04 |
| `onch_exchange_reserve` | float64 | onchain | 2.966e+06 |
| `onch_exchange_inflow` | float64 | onchain | 1.125e+04 |
| `onch_exchange_outflow` | float64 | onchain | 8432 |
| `onch_realized_price` | float64 | onchain | 5570 |
| `onch_market_price` | float64 | onchain | 7171 |
| `onch_sopr` | float64 | onchain | None |
| `onch_estimated_leverage_ratio` | float64 | onchain | None |
| `exch_data_source` | str | exchange | binance_vision_aggtrades |
| `exch_confidence_tier` | str | exchange | High |
| `net_data_source` | str | network | blockchain.com_charts_api |
| `net_confidence_tier` | str | network | High |
| `rc_data_source` | str | realized_cap | coinmetrics_community_derived |
| `rc_confidence_tier` | str | realized_cap | Medium |
| `pos_data_source` | str | positioning | binance_vision_metrics |
| `pos_confidence_tier` | str | positioning | Medium |
| `pos_options_data_source` | str | positioning | deribit_public_api |
| `pos_options_confidence_tier` | str | positioning | Low |
| `onch_data_source` | str | onchain | onchain_dataset_local |
| `onch_confidence_tier` | str | onchain | Medium |
| `date` | datetime64[ns, UTC] | other | 2009-01-03 00:00:00+00:00 |

## ETH

| Column | Type | Block | Sample |
|---|---|---|---|
| `exch_spot_volume` | float64 | exchange | 9538 |
| `exch_spot_quote_volume_usd` | float64 | exchange | 2.859e+06 |
| `exch_spot_taker_buy` | float64 | exchange | 7452 |
| `exch_spot_taker_sell` | float64 | exchange | 2085 |
| `exch_spot_taker_buy_quote_usd` | float64 | exchange | 2.241e+06 |
| `exch_spot_taker_buy_ratio` | float64 | exchange | 0.7814 |
| `exch_spot_num_trades` | float64 | exchange | 5658 |
| `exch_fut_volume` | float64 | exchange | 4.661e+05 |
| `exch_fut_quote_volume_usd` | float64 | exchange | 6.104e+07 |
| `exch_fut_taker_buy` | float64 | exchange | 2.325e+05 |
| `exch_fut_taker_sell` | float64 | exchange | 2.336e+05 |
| `exch_fut_taker_buy_quote_usd` | float64 | exchange | 3.045e+07 |
| `exch_fut_taker_buy_ratio` | float64 | exchange | 0.4988 |
| `exch_fut_num_trades` | float64 | exchange | 4.015e+04 |
| `exch_fut_aggtrade_records` | float64 | exchange | 2.467e+04 |
| `exch_combined_volume` | float64 | exchange | 9538 |
| `exch_combined_quote_volume_usd` | float64 | exchange | 2.859e+06 |
| `exch_combined_taker_buy_ratio` | float64 | exchange | 0.7814 |
| `exch_coverage_mode` | str | exchange | spot_only |
| `exch_spot_cross_filled` | object | exchange | False |
| `exch_fut_cross_filled` | object | exchange | False |
| `exch_fut_kline_artifact` | object | exchange | False |
| `exch_fut_numtrades_corrected` | object | exchange | False |
| `net_tx_count` | float64 | network | 0 |
| `rc_realized_cap_usd` | float64 | realized_cap | 1.5e+07 |
| `rc_realized_price_usd` | float64 | realized_cap | 0.2075 |
| `rc_mvrv_ratio` | float64 | realized_cap | 5.783 |
| `rc_net_exchange_flow` | float64 | realized_cap | 0 |
| `rc_exchange_supply` | float64 | realized_cap | 0 |
| `rc_market_cap_usd` | float64 | realized_cap | 8.677e+07 |
| `rc_price_usd` | float64 | realized_cap | 1.2 |
| `rc_tx_count` | float64 | realized_cap | 0 |
| `pos_oi_usd` | float64 | positioning | 1.71e+09 |
| `pos_top_trader_ls_ratio` | float64 | positioning | 0.5086 |
| `pos_taker_ls_vol_ratio` | float64 | positioning | 0.6039 |
| `pos_top_trader_long_pct` | float64 | positioning | 0.3371 |
| `pos_top_trader_short_pct` | float64 | positioning | 0.6629 |
| `pos_top_trader_skew` | float64 | positioning | -0.1629 |
| `pos_options_instruments` | float64 | positioning | 690 |
| `pos_options_n_calls` | float64 | positioning | 345 |
| `pos_options_n_puts` | float64 | positioning | 345 |
| `pos_options_call_oi` | float64 | positioning | 1.044e+06 |
| `pos_options_put_oi` | float64 | positioning | 5.579e+05 |
| `pos_options_total_oi` | float64 | positioning | 1.602e+06 |
| `pos_options_put_call_oi_ratio` | float64 | positioning | 0.5344 |
| `pos_options_call_vol_24h` | float64 | positioning | 3.462e+04 |
| `pos_options_put_vol_24h` | float64 | positioning | 3.353e+04 |
| `pos_options_total_vol_24h` | float64 | positioning | 6.815e+04 |
| `pos_options_put_call_vol_ratio` | float64 | positioning | 0.9685 |
| `pos_options_top_oi_strike` | float64 | positioning | 2000 |
| `pos_options_top_oi_value` | float64 | positioning | 1.005e+05 |
| `onch_exchange_net_flow` | float64 | onchain | 0 |
| `onch_exchange_supply` | float64 | onchain | 0 |
| `exch_data_source` | str | exchange | binance_vision_aggtrades |
| `exch_confidence_tier` | str | exchange | High |
| `net_data_source` | str | network | coinmetrics_community_api |
| `net_confidence_tier` | str | network | Medium |
| `rc_data_source` | str | realized_cap | coinmetrics_community_derived |
| `rc_confidence_tier` | str | realized_cap | Medium |
| `pos_data_source` | str | positioning | binance_vision_metrics |
| `pos_confidence_tier` | str | positioning | Medium |
| `pos_options_data_source` | str | positioning | deribit_public_api |
| `pos_options_confidence_tier` | str | positioning | Low |
| `onch_data_source` | str | onchain | coinmetrics_community_derived |
| `onch_confidence_tier` | str | onchain | Medium |
| `date` | datetime64[ns, UTC] | other | 2015-07-30 00:00:00+00:00 |
