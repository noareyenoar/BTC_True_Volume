# TA+VSA Visualizer — Multi-Source Volume Breakdown Chart

## QUEUE POSITION — DO NOT START YET

This task starts **only after** `independent_audit_report.md` (from the prior audit/remediation prompt) reaches a **Final Verdict of "ready for TA+VSA work"** with actual evidence, not a restated claim. If the audit is still running, still finding gaps, or still remediating — wait. If the audit report shows unresolved `gap_pct > 1%` or unexplained `ohlc_integrity_violations > 0` on the symbols you're about to visualize, halt and report back rather than building on top of known-bad bars.

Read `independent_audit_report.md` first. Extract the list of symbols/intervals that passed VSA readiness (Phase 3 of that audit) — only those are in scope for this visualizer initially.

---

## GOAL

Build a local, TradingView-style interactive chart application for OHLC price action, with a **volume panel that is fundamentally richer than a single bar** — because the entire point of this project was that raw exchange volume is unreliable. On hover, the volume panel must break down into its constituent sources, and the user must be able to **choose which volume definition drives the chart** via a selector, not be locked into one.

---

## SCOPE — SYMBOLS & MARKETS

Default symbol list (only include ones confirmed audit-ready):
```
BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, LINKUSDT, AVAXUSDT, DOTUSDT
```
Default interval: `1h` on load, with a timeframe switcher (`1m, 5m, 15m, 1h, 4h, 1d` — whichever the audit confirmed as gap-clean per symbol).

---

## ARCHITECTURE

```
┌─────────────────────────────────────────┐
│  Frontend: HTML + lightweight-charts     │
│  (TradingView's own free, MIT-licensed   │
│  charting library — the natural choice   │
│  since the goal IS to look/feel like     │
│  TradingView)                            │
└──────────────┬────────────────────────────┘
               │ REST (localhost)
┌──────────────▼────────────────────────────┐
│  Backend: FastAPI                        │
│  - Reads Parquet directly (pyarrow)      │
│  - Serves OHLC + multi-source volume     │
│    joined per bar                        │
│  - No data duplication — reads from      │
│    processed/ directly, read-only        │
└────────────────────────────────────────────┘
```

Use `lightweight-charts` (https://github.com/tradingview/lightweight-charts, free, MIT license — built by TradingView themselves for embedding, this is legitimate to use). Do not attempt to reproduce TradingView's proprietary full platform — this library gives the authentic candlestick/volume look for free.

For the rich hover tooltip (structured multi-line breakdown, not a single value), use `lightweight-charts`'s `subscribeCrosshairMove` API to drive a custom HTML tooltip overlay — the library's built-in tooltip is single-value only, so the breakdown panel needs custom rendering triggered on crosshair move.

---

## DATA CONTRACT — WHAT FEEDS THE VOLUME BREAKDOWN

For each bar (open_time), the backend must be able to serve this joined structure, pulling from the datasets already built across the prior pipeline work:

```python
VolumeBreakdown = {
    "open_time": int,
    "sources": {
        "exchange_volume": {
            "value": float,             # raw exchange volume, klines "volume" column
            "label": "Exchange Reported (unreliable — wash-trading prone)",
            "confidence": "low",
        },
        "onchain_network_volume": {
            "value": float,             # from Blockchain.com estimated-transaction-volume-usd,
                                         # resampled/aligned to this bar's time window
            "label": "On-Chain Settled Value (network-wide, USD)",
            "confidence": "high",
        },
        "taker_buy_sell_split": {
            "taker_buy": float,         # klines taker_buy_base_vol
            "taker_sell": float,        # volume - taker_buy_base_vol
            "label": "Exchange Taker Buy/Sell Split",
            "confidence": "low",        # inherits exchange volume's reliability issue
        },
        "large_trade_notional": {
            "value": float,             # sum of trade notional above the Tier-3 large-block
                                         # threshold from the MM proxy criteria report
            "trade_count": int,
            "label": "Large Block Trades (>99.9th percentile size)",
            "confidence": "medium",     # Tier 3 proxy — statistical inference, not identity
        },
        "top_trader_positioning": {
            "long_pct": float,          # from futures_um/metrics derived top_trader_long_pct
            "short_pct": float,
            "skew": float,
            "label": "Top-Trader Long/Short Skew (Binance top 20% by margin)",
            "confidence": "medium",
        },
        "open_interest_delta": {
            "value": float,             # change in sum_open_interest since previous bar
            "label": "Open Interest Change (position build/unwind)",
            "confidence": "high",
        },
    },
}
```

Only include sources that actually exist and passed the audit for the given symbol/interval — do not show a source with fabricated or missing data. If `onchain_network_volume` is daily-resolution only and the chart is on a 5m interval, forward-fill within the day and clearly label it `"resolution": "daily, forward-filled"` in the tooltip so the user isn't misled into thinking it's 5m-granular.

---

## ON-CHAIN HOLDER BREAKDOWN (separate panel, not just tooltip)

The user specifically wants "who holds how much on-chain" visible, not just volume. Add a **secondary collapsible panel** (not the main volume bars — a distinct panel below or in a side drawer) showing, for the symbol currently in view (BTC/ETH only, since on-chain holder data is chain-specific, not applicable to e.g. XRPUSDT the same way):

```python
OnchainHolderSnapshot = {
    "as_of": str,  # ISO date — this is snapshot data, not per-bar
    "lth_supply_pct": float,      # if URPD/LTH-STH data was built (audit-dependent)
    "sth_supply_pct": float,
    "exchange_reserve_estimate": float,  # if DIY tracking (forward-only) has enough history
    "data_availability": str,     # explicit note: "Historical" or "Forward-collecting since {date}"
}
```

If the audit found LTH/STH cohort data was deferred (Phase 8 gate not approved) or exchange reserve tracking is forward-only with insufficient history yet, **show this panel with an honest "not yet available" state** rather than hiding it silently — the user should see what's planned vs. what's live.

---

## VOLUME SOURCE SELECTOR — USER CONTROL

Render a control (dropdown or button group) above the volume panel:

```
[ Exchange Volume ▾ ]  ← default, but visually flagged as low-confidence
   ├─ Exchange Volume (raw)
   ├─ On-Chain Network Volume  ← recommend as default instead, see note below
   ├─ Large Trade Notional Only
   ├─ Taker Buy/Sell Split (stacked)
   └─ Open Interest Delta
```

Switching the selector changes what the **main volume bar series** renders (color-coded, e.g., green/red for buy/sell-dominant where applicable), while the **hover tooltip always shows the full breakdown across all sources regardless of which one is selected** — the selector controls the primary visual, the tooltip is always comprehensive.

**Recommendation to implement, not force**: default the chart to `onchain_network_volume` rather than `exchange_volume` on first load, with a one-line UI note explaining why ("Exchange volume is prone to wash trading; on-chain settled value is shown by default. Switch anytime."). Let the user override and remember their choice (local state, no account system needed).

---

## VSA-SPECIFIC RENDERING REQUIREMENTS

Since this is explicitly for VSA work:

1. **Spread visibility**: candle wicks must be rendered clearly (High-Low range is core to VSA reads) — use `lightweight-charts`'s standard candlestick series, do not use a simplified OHLC bar that compresses the wick visually.
2. **Volume bar coloring**: color each volume bar by whether that bar's close was up or down relative to open (standard VSA convention: up-close volume vs down-close volume), for whichever source is selected.
3. **No gap-filling that hides real gaps**: if the underlying data has a genuine missing bar (flagged `is_gap=True` from the audit), render it as an actual visual gap in the chart, not silently interpolated — VSA readings on interpolated bars would be meaningless. Add a subtle marker (e.g., a small icon on the time axis) for gap bars so the user knows not to read VSA signals there.
4. **Anomaly/large-trade markers**: overlay small markers on bars where `large_trade_notional` exceeds a visible threshold — this directly supports the "who's active here" reading the user wants, visible without needing to hover every bar.

---

## BACKEND ENDPOINTS

```python
# FastAPI app — read-only, local only, no auth needed for a local research tool

GET /api/symbols
    -> list of audit-confirmed symbols with available intervals

GET /api/ohlc?symbol=BTCUSDT&interval=1h&start=...&end=...
    -> array of {open_time, open, high, low, close, is_gap}

GET /api/volume_breakdown?symbol=BTCUSDT&interval=1h&start=...&end=...
    -> array of VolumeBreakdown objects (see Data Contract above)

GET /api/onchain_snapshot?symbol=BTCUSDT
    -> OnchainHolderSnapshot (latest available, or historical series if built)
```

Read directly from the existing `processed/` Parquet files via `pyarrow`/`pandas` — do not copy data into a separate database, keep this as a thin read layer so it always reflects the current state of the audited dataset.

---

## EXECUTION ORDER

1. Confirm `independent_audit_report.md` final verdict is positive before starting anything
2. Scaffold FastAPI backend with the three endpoints above, backed by direct Parquet reads
3. Verify each endpoint manually (curl/browser) returns sane joined data for BTCUSDT 1h before touching the frontend
4. Build the `lightweight-charts` frontend: candlesticks + volume panel + timeframe/symbol switcher
5. Add the custom crosshair-driven tooltip with the full multi-source breakdown
6. Add the volume source selector control
7. Add the on-chain holder panel (with honest "not available yet" states where applicable)
8. Add gap markers and large-trade overlay markers
9. Smoke test across at least BTCUSDT and ETHUSDT before considering this done
10. Write a short `visualizer_usage.md`: how to launch it (`uvicorn` command, port), what each panel means, and which data sources are live vs. placeholder

---

## GUARDRAILS

- Do not build this against unaudited or known-bad data — re-check the audit report's verdict, don't assume it's still valid if time has passed and the audit might have found new issues
- Never show a volume number without its source label and confidence tier visible on hover — the entire point of this tool is to stop treating volume as a single trustworthy number
- Do not fabricate on-chain holder data if it wasn't actually built (Phase 8 of the master plan may have been deferred) — show the honest "not yet collected" state instead of a fake-looking empty chart
- Keep this a local, read-only tool — no write access to `processed/`, no external network calls at chart-render time (all data should already be fetched/processed by prior pipeline stages)
