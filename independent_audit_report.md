# Independent Pipeline Audit

*Audited 2026-08-21 by an independent pass — every number below was re-derived from files on disk in this session (parquet metadata, `_meta.json` contents, registry YAML parse, kline recomputation). No claim is restated from the prior run's summary.*

## Summary Verdict

- Prior claim: "ALL PASS, 28 entries clean, clean shutdown"
- Actual finding: **PARTIALLY CONFIRMED — with one real remediation and several documented corrections**
  1. All 7 planned datasets exist on disk; the "28" is the registry's dataset-ID count (21 pre-existing + 7 new) and is re-confirmed by YAML parse. ✓
  2. Every gate the prior run claimed was re-computed independently and passes — with the Vision adjudication confirmed as a **deterministic numeric check against Binance's own primary source** (not a vision model; the audit prompt's special-scrutiny concern does not apply). ✓
  3. The VSA-specific checks found **spot 1d klines had 88 mid-series gaps (3.38%)** — above the audit's 1% bar. Root cause documented (monthly-file merge artifact); remediated by validated cross-fill **with flags** (never silent). Post-fill: 0.000% gaps, 0 OHLC violations. ✓
  4. One structural finding: `processed/` was relocated from `Dataset_Fetcher/processed` to `BTC_True_Volume/processed` (mtime 2026-08-21 16:28) **after** the pipeline run — code (`common.py`) still points at the root location. Not a data-quality issue, but a config drift that must be known before re-running the pipeline.

## Phase 1 — Registry Re-derivation

Filesystem walk (`processed/` = `BTC_True_Volume/processed`, the actual data root):

| Dataset (planned scope: 7) | Found on disk | Rows | Status |
|---|---|---|---|
| exchange_true_volume/{BTCUSDT,ETHUSDT}/merged.parquet | yes ×2 | 3,283 / 3,289 | OK |
| onchain/network_metrics/{BTC,ETH}/merged.parquet | yes ×2 | 6,438 / 4,039 | OK |
| onchain/realized_cap/{BTC,ETH}/merged.parquet | yes ×2 | 6,438 / 4,039 | OK |
| futures/positioning/{BTCUSDT,ETHUSDT}/merged.parquet | yes ×2 | 2,096 / 1,723 | OK |
| options/verification/{BTC,ETH}/merged.parquet | yes ×2 | 1 / 1 (single-snapshot, by design) | OK |
| true_volume/{BTC,ETH}/merged.parquet (FINAL) | yes ×2 | 6,438 / 4,039 | OK |
| rollup_cache/{BTCUSDT,ETHUSDT}.parquet | yes ×2 (internal cache) | 2,417 / 2,423 | OK |
| onchain/checkonchain/BTC/urpd_snapshot.parquet | yes (Phase 4 artifact) | 630 | OK |

- **15 parquet files on disk** vs "28" — the 28 is the registry's dataset count, not a file count. Registry YAML parses: **28 datasets, all 7 new IDs present** with row counts matching disk (e.g. true_volume_btc_daily rows=6,438). Pre-existing 21 entries intact. This is CONFIRMED, with the corrected interpretation.
- Planned scope = 7 new datasets (the master plan's true-volume stack). All present. No missing deliverables.
- Extras beyond the 7: 2 rollup caches (internal) + 1 URPD snapshot (Phase 4 investigation artifact, single 2026-08 snapshot — logged as COMPLETE, Medium).

## Phase 2 — Gate-by-Gate Re-verification

### 2.1 Network gate
What it checks (`verify_all.verify_network_metrics`, re-derived independently): non-zero columns, midnight-UTC dates, no duplicates, gap-free within each chart's range. Independent recomputation: **0 calendar gaps** in BTC (6,438 rows, 2009-01-03→2026-08-19) and ETH (4,039 rows, 2015-07-30→2026-08-19) network files; no duplicates; all columns non-zero. Note the caveat this gate implies: BTC `net_estimated_tx_volume_usd` includes non-trading transfers (documented in `_meta.json` + usage guide) — it is a settled-value proxy, not exchange volume.

### 2.2 Positioning gate — actual numbers (not just "passed")
| Column | BTCUSDT nulls | ETHUSDT nulls | Range check |
|---|---|---|---|
| pos_oi_usd | **0.00%** | **0.00%** | 596M–12.4B / 854M–11.1B USD, all > 0 |
| pos_top_trader_ls_ratio | **15.03%** | **18.28%** | within [1e-3, 1e3] |
| pos_taker_ls_vol_ratio | **6.11%** | **7.43%** | — |
| pos_top_trader_long_pct / short_pct / skew | **15.03%** / 18.28% | long+short = 1 exact | — |

The 15–18% nulls on the top-trader ratio family are genuine archive holes (metric not published those days / Vision gaps) — the files' date coverage starts 2020-11-23 (BTC) / 2021-12-01 (ETH). OI has zero nulls. Positioning is a **top-20% cohort sentiment proxy, not exchange-wide OI** — re-confirmed from `_meta.json` and the log entry.

### 2.3 Realized-cap cross-check
`_meta.json` evidence (re-read, not restated): BTC cross_check done=True, **2,387 days** overlap, mean rel diff **0.00016%**, max **0.20%**, pass_2pct=True. Comparison logic verified: compares `rc_realized_price_usd` (derived CapMrktCurUSD/CapMVRVCur ÷ SplyCur) against the local onchain store's `realized_price` — **same units (USD/coin), inner-joined on normalized UTC date**; both derivations trace to the same CoinMetrics pair, so ~0% deviation is expected and 0.20% max is the noise floor. ETH: done=False **by design** (the local onchain store is BTC-only); ETH is covered by the mvrv identity instead — re-derived: `rc_mvrv_ratio − market_cap/realized_cap` max deviation < 1e-6.

### 2.4 Vision adjudication — special note
The audit prompt asks what model/judgment this gate uses. **It uses no model at all** — verified from source (`exchange_true_volume.py:190` `adjudicate_against_vision`): for each disputed day it downloads Binance's **official** `data.binance.vision` daily aggTrades ZIP, sums `quantity` (vol_base) and taker-buy quantity, and compares numerically against the local tape within **±0.1% on both** (deterministic, keyless, primary-source evidence). Reproducibility: fully deterministic — same inputs give the same answer; the only runtime dependency is Vision's archive availability (documented as failed if a fetch fails).
Recorded evidence (`_meta.json` `tape_gate.adjudicated`): **BTC tape_validated=True, 7 verified days** (volume_days 2021-02-09, 2021-05-19, 2022-09-06, 2024-10-28; taker_days +2026-05-09/15/20); **ETH tape_validated=True, 9 verified days** (+2022-09-11/12/13, 2026-05-19/20). Gate thresholds re-read: volume pct_ok 99.83% (BTC) / 99.61% (ETH), taker 99.70% — below 99.9% but **adjudicated by primary-source verification, which is the stronger evidence**. The kline dumps (both encodings, shared per-day source) are the contaminated side — this is a documented, verified finding of this session, not a claim.

### 2.5 Final + registry/log gates
Final deliverables re-loaded: BTC 6,438 rows × 75 cols, ETH 4,039 × 66, **0 calendar gaps**, 0 negatives outside the two signed-by-construction families (`pos_*_skew`, `*_net_exchange_flow` — excluded with justification), all per-block `*_data_source` / `*_confidence_tier` columns populated from the label constant set (exch=High, net=High/Medium, rc=Medium, pos=Medium, onch=Medium). Pipeline log: `## step0`×1, `## Phase 1`×2, Phases 2–8 ×1 each, `## final`/`## register`/`## verify` ×1 each — exactly the plan template. Registry: all 7 IDs present under `datasets:` (28 total), YAML parses clean.

## Phase 3 — VSA Readiness

### 3.1 Volume trustworthiness — the core question
The final deliverables are **volume/metrics companion datasets, not OHLC bars** (verified: no open/high/low/close columns in `true_volume/*/merged.parquet`). What VSA runs on, per symbol:
- **Volume that is NOT raw kline volume**: `exch_combined_volume` / `exch_fut_volume` come from the **aggTrades tape rollup** (the improved source, gated + adjudicated). No raw `volume` column is presented as the truth.
- **On-chain proxy IS joined into the same file**: `net_estimated_tx_volume_usd` (Blockchain.com settled-value) present in the final BTC deliverable — the master plan's core requirement is met. ETH carries CoinMetrics TxCnt (`net_tx_count`) in the net block.
- Taker buy/sell split present (`exch_*_taker_buy_ratio` + taker_buy_base columns in the exchange block); top-trader positioning present (`pos_*`); OI present (`pos_oi_usd` — delta per bar is derivable).
- **Verdict: VSA will run on the improved proxy stack, not on the raw exchange volume the project set out to replace** — as long as the consuming tool joins the volume/metrics columns onto OHLC bars, which the visualizer prompt mandates.

### 3.2 Gap analysis (recomputed per dataset; bar: gap_pct ≤ 1%)
All 13 daily datasets: **0 calendar gaps, 0 irregular diffs** (listed in the table below for the kline side). Kline stores as found on disk:

| Source | gaps | gap_pct | Bar |
|---|---|---|---|
| spot 1d BTC/ETH (raw store) | 88 / 88 | **3.378%** | ❌ EXCEEDS — remediated ↓ |
| futures 1d BTC/ETH (raw store) | 23 / 23 | 0.992% | ✓ (under, flagged) |
| spot 1h | 116 | 0.180% | ✓ |
| futures 1h | 23 | 0.041% | ✓ |
| spot 1m | 122 | 0.003% | ✓ |
| futures 1m | 23 | 0.001% | ✓ |
| **vsa_klines (remediated store, all 12 series)** | **0 unflagged** | **spot 1d 0.000%**, rest ≤ 0.992% | ✓ ALL |

### 3.3 OHLC integrity (recomputed; violations on any bar invalidate VSA on that bar)
- Source klines: **0 violations on every file checked** (spot+futures 1d/1h/1m, BTC+ETH; the stores' own pre-existing `ohlc_integrity_flag` columns are all False).
- Filled bars (cross-fill source `dataset_1d/symbols`, itself gap-free with **0 violations**): **0 violations** on the 623 filled rows.
- **vsa_klines final: 0 OHLC violations on all 12 series.**

### 3.4 Interval consistency
Every series: diffs are exactly the claimed interval except the flagged gap positions (is_gap=True, 23–123 per series — every flagged position re-verified to correspond to a genuine missing slot; no mix of intervals, no unflagged irregular bars).

## Phase 4 — Silent Gap Audit

| Artifact | mtime | State |
|---|---|---|
| progress_report.md | 2026-08-20 23:48 | All 13 phase rows `completed` (verified cell-by-cell) |
| dataset_usage_guide.md | 2026-08-20 23:48 | — |
| schema.md | 2026-08-20 23:48 | — |
| verification_report.json | 2026-08-20 23:48 | All 6 checks PASS |
| pipeline_state.json | 2026-08-20 23:48 | All 13 phases `completed` |
| true_volume_pipeline_log.md | 2026-08-20 23:48 | One entry per phase, plan template |

- The prior run's admission ("report did not auto-refresh from the supervisor terminal path; regenerated manually") is **resolved and in sync**: all six artifacts share the terminal-state minute, content matches state.
- **Other silent gaps found:**
  1. **`processed/` relocation drift** (the one real silent change): directory moved to `BTC_True_Volume/processed` at 2026-08-21 16:28, after the run. `common.py`'s `PROCESSED = Dataset_Fetcher/processed` would recreate a second, empty root location if the pipeline were re-run. Not data corruption — the data root moved; code and log entries still reference the old path (e.g. Phase 4 log entry points at `Dataset_Fetcher\processed\...`). Recommend: keep the data where it is and re-point `common.py` (one line), or move it back — user decision, flagged rather than silently changed.
  2. **`final_state` key absent** from `pipeline_state.json` (cosmetic — per-phase statuses are the ground truth and are complete).
  3. Futures 1d kline filenames are asymmetric in the store (`BTCUSDT-1d.parquet` vs `merged.parquet`) — glob-resolved in `build_vsa_klines.py`; no data issue.
  4. Registry entry for `exchange_true_volume_daily` reports rows=6,572 (directory-level 3,283+3,289 combined) — correct, but a reader scanning "rows" column must know it spans two symbols.

## Phase 5 — Remediation Actions Taken

1. **Spot 1d gap_pct 3.378% → 0.000%**: root cause documented (monthly-file merge artifact: 88 month-first days 2017-09→2024-12). Remediated per the project convention (**flag, don't silently fill**): built `processed/vsa_klines/` (12 series, spot+futures × BTC/ETH × 1d/1h/1m) with
   - `is_gap=True` on every genuine missing slot (never interpolated — the visualizer must render them as gaps),
   - `cross_filled=True` on 623 spot-1d bars filled from `dataset_1d/symbols` (the step0-validated source: gap-free, 0 OHLC violations, same official Binance source) — composed of the 88 artifact days + 534 days beyond the spot store's 2024-12-31 end (extends spot 1d coverage to 2026-06-18),
   - `_meta.json` provenance per series.
2. **Futures BTCUSDT 1d** was missing from the first build due to the nonstandard filename — loader glob-resolved; all 12 series present.
3. **Re-verified after fixing** (audit guardrail): all 12 series — 0 unflagged gaps, 0 OHLC violations, intervals exact, gap flags 100% matching actual missing slots. Spot 1d 0.000%, everything else ≤ 0.992%.

## Final Verdict

**Ready for TA + VSA work — for BTCUSDT and ETHUSDT — with explicit conditions:**

1. **Volume must be consumed from the proxy stack** (exch tape volume / on-chain settled value / taker splits / top-trader positioning / OI delta — all present in `true_volume/{BTC,ETH}/merged.parquet`), **not** from raw kline `volume`. The on-chain proxy the master plan required IS joined into the final deliverable. Evidence: column inventories in §3.1.
2. **Bars come from `processed/vsa_klines/`**: gap-clean (0–0.99% per series, every gap flagged), 0 OHLC violations, intervals exact. Evidence: §3.2–3.4 tables.
3. **Coverage constraints to respect** (not defects, but load-bearing): spot OHLC ends 2024-12-31 for 1h/1m; futures 1h/1m end 2026-05-31; 1d OHLC ends 2026-06-18 (spot, after cross-fill) / 2026-05-31 (futures). There is **no OHLC after 2026-06-18** — the futures 1h store ends 2026-05-31, so recent daily bars cannot be resampled from 1h; the visualizer must state this rather than interpolate. (Futures tape volume continues to 2026-08-13 (BTC) / 2026-08-19 (ETH), and on-chain metrics to 2026-08-19, but there are no klines to chart them against.)
4. **Filled bars are marked, not silent** — `cross_filled=True` / `is_gap=True` must be rendered as markers/gaps by any consuming tool.

The prior "ALL PASS" claim is CONFIRMED for the gates and corrected in interpretation where the label overstated scope (28 = dataset IDs; spot-1d gaps existed and are now remediated with evidence). Not ready: the 8 additional symbols in the visualizer's default list (BNBUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, LINKUSDT, AVAXUSDT, DOTUSDT) — they have raw klines but **no true-volume proxy stack**; they are out of scope for VSA-on-proxy until the stack is extended (do not claim audit coverage for them).
