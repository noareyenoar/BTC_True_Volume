# TA+VSA Visualizer — Usage Guide

Local TradingView-style interactive chart for the **audit-confirmed** true-volume stack
(BTCUSDT + ETHUSDT only — see Scope below). Built per `ta_vsa_visualizer_prompt.md`, backed
by `independent_audit_report.md` (Final Verdict: ready for TA+VSA with conditions).

## Launch — always refresh first (one command)

```bash
# from D:\kp_ai_agent\Dataset_Fetcher
.venv\Scripts\python.exe BTC_True_Volume\refresh_before_visualizer.py
```

This is the **required pre-visualizer step**: it backfills everything to the latest
available data, then launches the server and waits until it is healthy (~5–10 min;
most of it is the daily aggTrades tape pull from Binance Vision, which lags ~1 day).
Run it **every time** before you start TA work — never launch the bare server without it.

What it does, in order (all incremental & resume-safe — re-running is cheap):

1. Probe Binance Vision for the latest published date and extend the six futures
   kline stores (BTCUSDT/ETHUSDT × 1d/1h/1m) to that date (ZIPs cached under
   `BTC_True_Volume/raw/vision_klines_zip/`).
2. Extend the aggTrades tape for both symbols into `…/aggtrades/{SYM}/extension/`
   (new days only, appended).
3. Roll up only the new tape days into the existing rollup cache
   (`processed/rollup_cache/`) — no full rescan.
4. Rebuild the audited chain: step0 triple gate (quarantine + tape-vs-kline
   ±0.2% + official-ZIP adjudication) → vsa_klines → large trades (upsert) →
   phases 1/5/2 → final `processed/true_volume/{SYM}/merged.parquet`.
5. Kill any server on port 8000, launch uvicorn, poll `/api/health` until green.

Flags: `--no-launch` (refresh only), `--dry-run` (show what would be fetched).

## Publish the shared page (phone bookmark)

After a refresh, push the phone-friendly static snapshot so your sister (or anyone
with the link) sees the latest data — she just re-opens the URL, nothing to install:

```bash
.venv\Scripts\python.exe BTC_True_Volume\publish_site.py          # export + push
.venv\Scripts\python.exe BTC_True_Volume\publish_site.py --no-push  # export + commit only
```

- **URL**: https://noareyenoar.github.io/BTC_True_Volume/ (Pages redeploys ~1-2 min after push)
- One-time setup (done 2026-08-21): public repo `BTC_True_Volume` (renamed from tv-vsa 2026-08-23) on GitHub, Pages enabled
  (Settings → Pages → Deploy from a branch → main). First push pops a one-time
  GitHub login (Git Credential Manager).
- The page serves **1h + 1d only** (1m stays local — ~150 MB per store is not a
  phone budget) via `export_static_site.py`, which reuses the app's own endpoint
  functions — same data, same flags, same gap/cross-fill markers.
- Phone: touch pan/zoom works (lightweight-charts is touch-ready); the volume
  source selector is remembered in localStorage.

Alternative (skip refresh if you know data is current — not recommended):

```bash
.venv\Scripts\python.exe -m uvicorn BTC_True_Volume.visualizer.app:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000/**. Fully local — no external network calls at
render time (the charting library is vendored in `visualizer/static/vendor/`).

| Endpoint | Purpose |
|---|---|
| `GET /api/symbols` | audited symbols, intervals, per-interval coverage |
| `GET /api/ohlc?symbol=&interval=&start=&end=&limit=` | merged gap-flagged OHLC bars + gap slots |
| `GET /api/volume_breakdown?symbol=&interval=&start=&end=&limit=` | per-bar multi-source volume |
| `GET /api/onchain_snapshot?symbol=` | holder panel data |

Backend is a **thin read-only layer over `processed/`** — no database, no writes.

## Panels

1. **Price chart** — standard candlesticks (wick-first for VSA reads). Bars after a
   genuine missing slot carry a gray `gap` marker; cross-filled bars carry an amber
   `cross-filled` marker; days in the top decile of large-trade notional carry a purple
   `large` arrow (threshold shown in the status bar, recomputed per loaded range).
   Hover shows the full OHLC + source breakdown. A **click on the chart turns that
   readout off** (another click turns it back on): the box is as tall as the breakdown
   table, so on a small chart it covers the candles it describes. The choice is
   remembered (localStorage `vsa_hover_info_v1`), and the status strip says so while it
   is off. Mouse only — on a touch screen a tap is the gesture that raises the
   crosshair, so it is left alone.
2. **Volume panel** — driven by the **Volume Source selector** (top right):
   | Option | What it renders | Confidence |
   |---|---|---|
   | ★ On-Chain Network Volume (default) | BTC: Blockchain.com settled USD value; ETH: CoinMetrics TxCnt | High / Medium |
   | Exchange Reported (raw klines) | the unreliable number — shown for comparison only | Low |
   | Tape Rollup (futures aggTrades) | the audited exchange-volume proxy (adjudicated vs Binance Vision) | High |
   | Large Trade Notional | daily sum of trades > 99.9th pct of that day's notional | Medium (Tier-3 proxy) |
   | Taker Buy/Sell Split (stacked) | green buy / red sell from the bar's taker-buy column | Low |
   | Open Interest Delta | pos_oi_usd change; intraday: attached to the day's first bar | High |

   Bars are colored by close≥open (VSA convention), except OI delta (sign) and the taker
   stack (buy/sell). Your selection is remembered (localStorage). **The hover tooltip
   always shows every source regardless of selection** — that is the point of the tool.
3. **On-Chain Holder Panel** (collapsible) — honest states:
   - **LTH/STH supply shares: not yet available** — UTXO age-cohort analysis was deferred
     (Phase 8 approval gate). Shown, not hidden.
   - **Exchange reserve estimate**: BTC = historical series from the local on-chain store
     through **2026-07-14** (forward collection not started — Phase 7 deferred);
     ETH = CoinMetrics exchange-supply proxy through 2026-08-20 (Medium confidence, not a
     wallet-level tracker).
   - **URPD snapshot**: BTC only (URPD is BTC-UTXO), single **2026-08-20** capture, 630
     price buckets; top buckets displayed. ETH: no equivalent exists — stated.

## Data contract notes

- Daily-resolution sources are labeled `daily, forward-filled` on intraday bars — never
  implied to be 1h/1m granularity.
- **No fabricated numbers**: a source appears only where real data exists; per-bar gaps
  show `n/a`; ETH's on-chain source is transaction count (its USD volume is not available
  from free sources and is not invented).
- Gap slots are **real missing bars rendered as visual whitespace** with a marker — never
  interpolated. VSA must not read signals there.

## Coverage (refresh-run 2026-08-21, data through 2026-08-20)

| Interval | Coverage | Notes |
|---|---|---|
| 1m / 1h / 1d | 2017-08-17 → **2026-08-20** | futures UM stores extended to the latest Vision publish each refresh; gap-flagged slots retained |
| tape volume (exch block) | BTC & ETH → **2026-08-20** | rollup cache 2,424 days; tape-vs-kline ±0.2% gate passed on 2,396 days; 4 disputed days auto-adjudicated vs official Vision ZIPs (tape matched exactly) |
| large trades (Tier-3) | → **2026-08-20** | per-day 99.9th-pct threshold from the tape |
| on-chain / realized cap | → **2026-08-20** | ETH network metrics: 373 gap days before 2021-12-01 (Vision metrics ZIPs start then) — expected, not a defect |

Refresh updates all of the above to the latest available day (Vision publish lag ~1
day behind UTC today). Data before the previous refresh is untouched — extensions
are additive.

## Scope — why only two symbols

`/api/symbols` returns the audit-confirmed set only. BNBUSDT, SOLUSDT, XRPUSDT, DOGEUSDT,
ADAUSDT, LINKUSDT, AVAXUSDT, DOTUSDT are **listed as not-audited**: they have raw klines
but no true-volume proxy stack — VSA-on-proxy for them is out of scope until the stack is
extended (audit verdict, `independent_audit_report.md` Final Verdict).

## Live vs. placeholder sources

| Source | State |
|---|---|
| exchange_volume | LIVE (raw klines) |
| tape_rollup_volume | LIVE (audited, adjudicated) |
| onchain_network_volume | LIVE (BTC USD value; ETH tx count) |
| large_trade_notional | LIVE (computed from the aggTrades tape, 99.9th-pct day-level) |
| taker_buy_sell_split | LIVE (bar klines) |
| top_trader_positioning | LIVE (daily, forward-filled) |
| open_interest_delta | LIVE (daily, first-bar-of-day intraday) |
| LTH/STH supply | PLACEHOLDER — honest "not yet available" state |
| URPD | LIVE for BTC (single snapshot) / n/a for ETH |
| exchange reserve | LIVE series (BTC to 2026-07-14; ETH CoinMetrics proxy) |
