"""
app.py — TA+VSA visualizer backend: read-only FastAPI layer over processed/.

Serves the audit-confirmed BTCUSDT/ETHUSDT true-volume stack directly from
parquet (no duplication, no writes, no external network calls at request time):

    GET /api/health              read-only layer check
    GET /api/symbols             audited symbols, intervals, coverage, sources
    GET /api/ohlc                merged gap-flagged OHLC bars + gap slots
    GET /api/volume_breakdown    per-bar multi-source volume breakdown
    GET /api/onchain_snapshot    holder panel (honest not-available states)
    GET /api/volume_profile      traded volume bucketed by price level (volume profile)

Market merge rule (audit-verified, surfaced in the UI):
    - 1m/1h : spot bars where they exist (to 2024-12-31), then futures_um bars.
    - 1d    : spot 1d bars to 2026-06-18 (incl. flagged cross-fills), then daily
              bars resampled from futures 1h (resampled=True) to 2026-08-19.
    Missing slots stay empty (natural visual gaps) and are listed in gap_slots.

Sources the breakdown serves per bar (values only where real data exists;
daily-resolution metrics are labeled "daily, forward-filled" on intraday bars):
    exchange_volume          raw kline volume (low confidence — the unreliable one)
    tape_rollup_volume       aggTrades tape rollup, adjudicated (High)
    onchain_network_volume   BTC: Blockchain.com settled USD volume / ETH: CoinMetrics TxCnt
    taker_buy_sell_split     from the bar's own kline taker-buy column
    large_trade_notional     Tier-3 99.9th-pct daily sum (if computed)
    top_trader_positioning   Binance top-20% long/short/skew (daily)
    open_interest_delta      pos_oi_usd change (daily; intraday: on the day's first bar)
"""

from __future__ import annotations

import bisect
import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

BASE = Path(__file__).resolve().parents[1]          # BTC_True_Volume
DATA_ROOT = BASE / "processed"                      # actual data root (audited)
VSA = DATA_ROOT / "vsa_klines"                      # gap-flagged kline store (audit remediation)
TV = DATA_ROOT / "true_volume"                      # final proxy deliverables
LT = DATA_ROOT / "large_trades"                     # large-trade notional series
URPD = DATA_ROOT / "onchain" / "checkonchain" / "BTC" / "urpd_snapshot.parquet"
AUDIT_REF = "BTC_True_Volume/independent_audit_report.md (2026-08-21)"

INTERVAL_MS = {"1m": 60_000, "1h": 3_600_000, "1d": 86_400_000}
DAY_MS = 86_400_000
INTERVALS = ("1m", "1h", "1d")                     # the audit-confirmed gap-clean set

SYMBOLS = {
    "BTCUSDT": {"base": "BTC", "name": "Bitcoin"},
    "ETHUSDT": {"base": "ETH", "name": "Ethereum"},
}
NOT_AUDITED = [
    {"symbol": s, "reason": "no true-volume proxy stack (raw klines only) — "
     "audit verdict: out of scope for VSA-on-proxy until the stack is extended"}
    for s in ("BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
              "LINKUSDT", "AVAXUSDT", "DOTUSDT")
]

app = FastAPI(title="TA+VSA Visualizer", version="1.0.0",
              description="Local read-only chart over the audited true-volume stack")


# --------------------------------------------------------------------------- #
#  bar layer                                                                  #
# --------------------------------------------------------------------------- #

def _load_vsa(market: str, sym: str, interval: str) -> pd.DataFrame:
    p = VSA / market / sym / interval / "merged.parquet"
    if not p.exists():
        raise HTTPException(500, f"vsa_klines missing: {p}")
    df = pd.read_parquet(p, columns=["open_time", "open", "high", "low", "close",
                                     "volume", "taker_buy_base_vol", "cross_filled"])
    df["market"] = market
    return df


@lru_cache(maxsize=6)
def _bars(symbol: str, interval: str) -> pd.DataFrame:
    """Merged gap-flagged bars for one symbol/interval (audited sources only)."""
    ms = INTERVAL_MS[interval]
    spot = _load_vsa("spot", symbol, interval)
    fut = _load_vsa("futures_um", symbol, interval)
    if interval == "1d":
        spot_end = spot["open_time"].max()
        tail = _load_vsa("futures_um", symbol, "1h")
        tail = tail[tail["open_time"] > spot_end].copy()
        if len(tail):
            tail["day"] = tail["open_time"] // DAY_MS * DAY_MS
            agg = tail.groupby("day").agg(
                open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), volume=("volume", "sum"),
                taker_buy_base_vol=("taker_buy_base_vol", "sum")).reset_index()
            agg = agg.rename(columns={"day": "open_time"})
            agg["market"] = "futures_um"
            agg["resampled"] = True
            agg["cross_filled"] = False
            spot["resampled"] = False
            bars = pd.concat([spot, agg], ignore_index=True)
        else:
            spot["resampled"] = False
            bars = spot
    else:
        spot_end = spot["open_time"].max()
        fut_after = fut[fut["open_time"] > spot_end].copy()
        spot["resampled"] = False
        fut_after["resampled"] = False
        bars = pd.concat([spot, fut_after], ignore_index=True)
    bars = bars.sort_values("open_time").drop_duplicates("open_time", keep="first")
    bars = bars.reset_index(drop=True)
    d = bars["open_time"].diff().fillna(ms)
    bars["follows_gap"] = d > ms * 1.5
    return bars


def _gap_slots(bars: pd.DataFrame, interval: str) -> List[int]:
    """open_times of slots where the store had no bar (natural gaps)."""
    ms = INTERVAL_MS[interval]
    d = bars["open_time"].diff().fillna(ms)
    slots: List[int] = []
    for i in d[d > ms * 1.5].index:
        prev = bars["open_time"].iat[bars.index.get_loc(i) - 1]
        n_missing = int(d[i] // ms) - 1
        for k in range(1, min(n_missing, 10) + 1):
            slots.append(int(prev + k * ms))
        if len(slots) > 5_000:
            break
    return slots


# --------------------------------------------------------------------------- #
#  daily proxy join                                                           #
# --------------------------------------------------------------------------- #

def _daily_frame(symbol: str) -> pd.DataFrame:
    base = SYMBOLS[symbol]["base"]
    df = pd.read_parquet(TV / base / "merged.parquet")
    df = df.set_index(pd.to_datetime(df["date"], utc=True).dt.normalize())
    return df


def _align_daily(daily: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """Exact-day alignment of daily proxies onto bars (no cross-day ffill)."""
    # normalize index resolution to ns — parquet round-trips can give us/ms
    idx = pd.DatetimeIndex(daily.index).as_unit("ns")
    dms = (idx.astype("int64") // 10**6).values                  # daily day-ms
    bar_dms = bars["open_time"].values // DAY_MS * DAY_MS
    pos = np.searchsorted(dms, bar_dms, side="right") - 1
    exact = (pos >= 0) & (dms[np.maximum(pos, 0)] == bar_dms)
    aligned = daily.iloc[np.maximum(pos, 0)].copy()
    aligned[~exact] = np.nan
    aligned.index = bars.index
    return aligned


# --------------------------------------------------------------------------- #
#  endpoints                                                                  #
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health() -> Dict:
    return {"ok": True, "data_root": str(DATA_ROOT), "read_only": True,
            "audit_ref": AUDIT_REF}


@app.get("/api/symbols")
def symbols() -> Dict:
    out = []
    for sym, info in SYMBOLS.items():
        cov = {}
        for interval in INTERVALS:
            bars = _bars(sym, interval)
            cov[interval] = {"start": int(bars["open_time"].iloc[0]),
                             "end": int(bars["open_time"].iloc[-1]),
                             "n": int(len(bars)),
                             "n_follows_gap": int(bars["follows_gap"].sum())}
        out.append({"symbol": sym, **info, "intervals": list(INTERVALS),
                    "coverage": cov, "audit_status": "audited",
                    "audit_ref": AUDIT_REF})
    # default_interval is what the frontend opens on (see app.js boot): daily
    # bars, so a visit starts on the range the VSA read is actually made over.
    return {"audited": out, "not_audited": NOT_AUDITED,
            "default_interval": "1d", "default_source": "onchain_network_volume"}


@app.get("/api/ohlc")
def ohlc(symbol: str, interval: str, start: Optional[int] = None,
         end: Optional[int] = None, limit: int = 250_000) -> Dict:
    if symbol not in SYMBOLS:
        raise HTTPException(404, f"symbol {symbol} not in audited set")
    if interval not in INTERVAL_MS:
        raise HTTPException(400, f"interval {interval} not audited (use 1m/1h/1d)")
    bars = _bars(symbol, interval)
    if start is not None:
        bars = bars[bars["open_time"] >= start]
    if end is not None:
        bars = bars[bars["open_time"] <= end]
    truncated = False
    if len(bars) > limit:
        bars = bars.tail(limit)
        truncated = True
    recs = [{"open_time": int(r.open_time), "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume,
             "market": r.market, "resampled": bool(r.resampled),
             "cross_filled": bool(r.cross_filled), "follows_gap": bool(r.follows_gap)}
            for r in bars.itertuples(index=False)]
    full = _bars(symbol, interval)
    gaps = [g for g in _gap_slots(full, interval)
            if (start is None or g >= start) and (end is None or g <= end)]
    return {"symbol": symbol, "interval": interval, "bars": recs, "gap_slots": gaps,
            "truncated": truncated,
            "coverage": {"start": int(bars["open_time"].iloc[0]) if len(bars) else None,
                         "end": int(bars["open_time"].iloc[-1]) if len(bars) else None,
                         "n": len(bars)}}


def _source_specs(symbol: str) -> List[Dict]:
    """Static per-source metadata; large_trade included only when computed."""
    base = SYMBOLS[symbol]["base"]
    specs = [
        {"key": "exchange_volume", "label": "Exchange Reported (raw kline volume)",
         "confidence": "low", "units": f"{base} base", "resolution": "bar",
         "note": "wash-trading prone — shown only for comparison"},
        {"key": "tape_rollup_volume",
         "label": "Tape Rollup (futures aggTrades, adjudicated vs Binance Vision)",
         "confidence": "High", "units": f"{base} base", "resolution": "daily"},
        {"key": "onchain_network_volume",
         "label": ("On-Chain Settled Value (Blockchain.com estimated transaction "
                   "volume, network-wide)" if base == "BTC"
                   else "On-Chain Transactions (CoinMetrics TxCnt — ETH USD volume "
                        "not available from free sources)"),
         "confidence": "High" if base == "BTC" else "Medium",
         "units": "USD" if base == "BTC" else "txn/day", "resolution": "daily"},
        {"key": "large_trade_notional",
         "label": "Large Block Trades (>99.9th percentile of the day's trade notional)",
         "confidence": "Medium", "units": "USD", "resolution": "daily",
         "note": "Tier-3 statistical proxy, not identity; day-level percentile "
                 "approximates the criteria report's rolling-30d window"},
        {"key": "taker_buy_sell_split", "label": "Exchange Taker Buy/Sell Split",
         "confidence": "low", "units": f"{base} base", "resolution": "bar"},
        {"key": "top_trader_positioning",
         "label": "Top-Trader Long/Short Skew (Binance top 20% by margin)",
         "confidence": "Medium", "units": "fraction", "resolution": "daily"},
        {"key": "open_interest_delta", "label": "Open Interest Change (pos_oi_usd)",
         "confidence": "High", "units": "USD", "resolution": "daily"},
    ]
    if not (LT / f"{symbol}.parquet").exists():
        specs = [s for s in specs if s["key"] != "large_trade_notional"]
    return specs


@app.get("/api/volume_breakdown")
def volume_breakdown(symbol: str, interval: str, start: Optional[int] = None,
                     end: Optional[int] = None, limit: int = 30_000) -> Dict:
    if symbol not in SYMBOLS:
        raise HTTPException(404, f"symbol {symbol} not in audited set")
    if interval not in INTERVAL_MS:
        raise HTTPException(400, "interval not audited (use 1m/1h/1d)")
    bars = _bars(symbol, interval)
    if start is not None:
        bars = bars[bars["open_time"] >= start]
    if end is not None:
        bars = bars[bars["open_time"] <= end]
    if len(bars) > limit:
        bars = bars.tail(limit)
    base = SYMBOLS[symbol]["base"]
    daily = _daily_frame(symbol)
    aligned = _align_daily(daily, bars)

    # --- open interest delta: daily diff; intraday -> first bar of each day ---
    oi_delta = aligned["pos_oi_usd"].diff() if interval == "1d" else None
    if interval != "1d":
        first_of_day = ~(bars["open_time"] // DAY_MS).duplicated(keep="first")
        oi_delta = aligned["pos_oi_usd"].diff().where(first_of_day)

    # large-trade alignment is per-day like the rest — reuse _align_daily
    lt_aligned = None
    if (LT / f"{symbol}.parquet").exists():
        lt = pd.read_parquet(LT / f"{symbol}.parquet")
        lt.index = pd.to_datetime(lt.index, utc=True).normalize()
        lt_aligned = _align_daily(lt, bars)

    taker_buy = bars["taker_buy_base_vol"].fillna(0.0)
    taker_sell = (bars["volume"] - taker_buy).clip(lower=0.0)

    # BTC settled value: the build_final union (Blockchain.com where present,
    # checkonchain transfer volume where it is not).  Older final frames lack
    # the union column, so fall back to the primary one.
    onch_col = ("net_settled_volume_usd"
                if "net_settled_volume_usd" in aligned.columns
                else "net_estimated_tx_volume_usd")
    onch_src_col = ("net_settled_volume_source"
                    if "net_settled_volume_source" in aligned.columns else None)

    recs = []
    for i, r in enumerate(bars.itertuples(index=False)):
        srcs = {
            "exchange_volume": r.volume,
            "tape_rollup_volume": aligned["exch_combined_volume"].iloc[i],
            "onchain_network_volume": (aligned[onch_col].iloc[i]
                                       if base == "BTC" else aligned["net_tx_count"].iloc[i]),
            "taker_buy_sell_split": {"buy": float(taker_buy.iloc[i]),
                                     "sell": float(taker_sell.iloc[i])},
            "top_trader_positioning": {"long_pct": aligned["pos_top_trader_long_pct"].iloc[i],
                                       "short_pct": aligned["pos_top_trader_short_pct"].iloc[i],
                                       "skew": aligned["pos_top_trader_skew"].iloc[i]},
            "open_interest_delta": None if oi_delta is None or pd.isna(oi_delta.iloc[i])
                                   else float(oi_delta.iloc[i]),
        }
        if lt_aligned is not None:
            srcs["large_trade_notional"] = lt_aligned["large_notional_usd"].iloc[i]
        if base == "BTC" and onch_src_col:
            prov = aligned[onch_src_col].iloc[i]
            srcs["onchain_network_volume_source"] = None if pd.isna(prov) else prov
        recs.append({"open_time": int(r.open_time), "sources": srcs})

    specs = _source_specs(symbol)
    # per-source availability within the returned range
    for s in specs:
        key = s["key"]
        vals = [b["sources"][key] for b in recs if key in b["sources"]]
        present = [b for b, v in zip(recs, vals) if pd.notna(v)]
        s["available_from"] = present[0]["open_time"] if present else None
        s["available_to"] = present[-1]["open_time"] if present else None
    notes = ["daily-resolution sources are labeled 'daily, forward-filled' on intraday bars",
             "onchain_network_volume: BTC = settled-value USD proxy (includes non-trading "
             "transfers); ETH = transaction count. BTC falls back to checkonchain transfer "
             "volume on days Blockchain.com has not published; per-bar provenance is in "
             "sources.onchain_network_volume_source",
             "open_interest_delta on intraday bars is attached to the day's first bar "
             "(OI is daily granularity)"]
    return {"symbol": symbol, "interval": interval, "sources": specs,
            "bars": recs, "notes": notes}


@app.get("/api/onchain_snapshot")
def onchain_snapshot(symbol: str) -> Dict:
    if symbol not in SYMBOLS:
        raise HTTPException(404, f"symbol {symbol} not in audited set")
    base = SYMBOLS[symbol]["base"]
    daily = _daily_frame(symbol)
    holder = _holder_block() if base == "BTC" else {
        "available": False, "data_vintage": None,
        "note": "age-cohort data is BTC-UTXO-only — no ETH equivalent exists"}
    lth_sth = {
        "lth_supply_pct": holder.get("lth_supply_pct"),
        "sth_supply_pct": holder.get("sth_supply_pct"),
        "holder": holder,
        "note": holder.get("note", ""),
    }
    resp = {"as_of": None, **lth_sth, "urpd": None,
            "exchange_reserve_estimate": None,
            "data_availability": ""}
    if base == "BTC":
        urpd = None
        u, vintage, frozen = _urpd_overlay()
        if u is not None:
            top = u.sort_values("supply_btc", ascending=False).head(10)
            # `captured_utc` is the FETCH date; `vintage` is the date the curve
            # describes. The old label conflated them because the source was
            # frozen, which made a seven-month-old curve look freshly captured.
            urpd = {
                "available": True,
                "captured_utc": str(u["captured_utc"].iloc[0]),
                "data_vintage": vintage,
                "n_buckets": int(len(u)),
                "total_supply_btc": float(u["supply_btc"].sum()),
                "top_buckets": [{"price_bucket_usd": _f(r.price_bucket_usd),
                                 "supply_btc": _f(r.supply_btc)}
                                for r in top.itertuples(index=False)],
                "note": (f"data to {vintage}" if vintage else
                         "frozen mirror generation (the live curve store is missing)")
                        + "; BTC-UTXO only. The /api/cost_basis overlay carries "
                          "every dated curve behind this.",
            }
        ser = daily["onch_exchange_reserve"].dropna()
        reserve = None
        if len(ser):
            xs = ser.index.astype("int64") // 10**6
            ys = ser.values
            if len(xs) > 1_500:
                step = int(np.ceil(len(xs) / 1_500))
                xs, ys = xs[::step], ys[::step]
            reserve = {
                "value": float(ser.iloc[-1]), "as_of": str(ser.index[-1].date()),
                "series": [[int(x), float(y)] for x, y in zip(xs, ys)],
                "units": "BTC",
                "availability": "Historical series through 2026-07-14 (local "
                                "on-chain store); forward collection not started "
                                "(Phase 7 deferred)",
            }
        resp.update({
            "as_of": vintage or "",
            "urpd": urpd,
            "exchange_reserve_estimate": reserve,
            "data_availability": "URPD and LTH/STH cohorts: checkonchain live charts, "
                                 f"data to {vintage or 'unavailable'}"
                                 + (" (frozen mirror generation — the live curve "
                                    "store is missing)" if frozen else "")
                                 + ". Exchange reserve: local, historical to "
                                   "2026-07-14.",
        })
    else:  # ETH — no local on-chain store; CoinMetrics proxy only
        ser = daily["onch_exchange_supply"].dropna()
        reserve = None
        if len(ser):
            xs = ser.index.astype("int64") // 10**6
            ys = ser.values
            if len(xs) > 1_500:
                step = int(np.ceil(len(xs) / 1_500))
                xs, ys = xs[::step], ys[::step]
            reserve = {
                "value": float(ser.iloc[-1]), "as_of": str(ser.index[-1].date()),
                "series": [[int(x), float(y)] for x, y in zip(xs, ys)],
                "units": "ETH",
                "availability": "CoinMetrics exchange-supply proxy (Medium "
                                "confidence), historical to 2026-08-19. Not a "
                                "wallet-level tracker.",
            }
        resp.update({
            "as_of": None,
            "urpd": {"available": False,
                     "note": "URPD is BTC-UTXO-only — no ETH equivalent exists"},
            "exchange_reserve_estimate": reserve,
            "data_availability": "ETH on-chain holder data not built (chain-specific; "
                                 "no ETH URPD). CoinMetrics exchange-supply proxy shown.",
        })
    return resp


@app.get("/api/etf_flows")
def etf_flows(symbol: str = "BTCUSDT") -> Dict:
    """Daily net flows of the US spot ETFs, in USD — the demand behind price.

    One point per calendar day from the first session (2024-01-11) to today.
    Two properties the reader has to be told rather than shown:

    * A **0.0 is a day the US market did not open** (weekend/holiday), not a day
      with no flow — the series is session-dated, not trading-day-indexed.
    * **The newest day is provisional.** Upstream publishes it through the
      session and restates it; on the collection day it is usually still 0.0.

    BTC-only by construction: these are US spot ETFs holding BTC. ETH gets the
    honest-unavailable body, never BTC's numbers under an ETH label.
    """
    if symbol not in SYMBOLS:
        raise HTTPException(404, f"symbol {symbol} not in audited set")
    base = SYMBOLS[symbol]["base"]
    empty = {"symbol": symbol, "available": False, "metric": "etf_net_flow_usd",
             "units": "USD", "bucket": "1d", "points": [], "first": None,
             "last": None, "last_session": None, "n": 0, "source_chart": None,
             "data_vintage": None}
    if base != "BTC":
        return {**empty, "note": "spot BTC ETFs hold BTC — no ETH equivalent "
                                 "exists; this is not a proxy for ETH flows."}
    s = _cb_series()
    if s is None:
        return {**empty, "note": "series store not built — run "
                                 "fetch_checkonchain_urpd.py"}
    e = s[s["metric"] == "etf_net_flow_usd"]
    if e.empty:
        return {**empty, "note": "etf_net_flow_usd missing from the series store "
                                 "— run fetch_checkonchain_urpd.py"}
    e = e.assign(_d=e["date"].astype(str).str[:10]).sort_values("_d")
    points = [[d, _f(v)] for d, v in zip(e["_d"], e["value"])]
    # The last day the market actually opened, which is what "the flow right now"
    # means — the newest stamps are 0.0 until upstream publishes the session.
    sessions = [[d, v] for d, v in points if v]
    return {
        "symbol": symbol, "available": True, "metric": "etf_net_flow_usd",
        "units": "USD", "bucket": "1d", "n": len(points), "points": points,
        "first": points[0][0], "last": points[-1][0],
        "last_session": sessions[-1] if sessions else None,
        "source_chart": str(e["source_chart"].iloc[-1]),
        "data_vintage": points[-1][0],
        "note": f"Daily net flows across the US spot BTC ETFs, {points[0][0]} "
                f"(first session) to {points[-1][0]}, in USD. A 0.0 means the US "
                f"market did not open that day (weekend/holiday), not zero flow; "
                f"the newest day is provisional until upstream restates it.",
    }


# --------------------------------------------------------------------------- #
#  volume profile — traded volume by price level                              #
# --------------------------------------------------------------------------- #

PROFILE = DATA_ROOT / "volume_profile"
PROFILE_WINDOWS = {"30d": 30, "90d": 90, "1y": 365, "all": None}
DISPLAY_LADDER = {                       # 'nice' widths, coarse enough to read
    "BTCUSDT": (10, 25, 50, 100, 250, 500, 1000, 2500),
    "ETHUSDT": (0.5, 1, 2.5, 5, 10, 25, 50, 100),
}
TARGET_BUCKETS = 400                     # pick the finest width under this
VALUE_AREA_PCT = 0.70                    # classic market-profile value area


@lru_cache(maxsize=2)
def _profile(symbol: str) -> pd.DataFrame:
    """Per-day price-bucket cache built by build_volume_profile.py.

    lru_cached for the same reason as _bars: the refresh flow restarts uvicorn,
    so a rebuild is picked up on the next launch.
    """
    p = PROFILE / symbol / "daily.parquet"
    if not p.exists():
        raise HTTPException(503, f"volume profile not built yet: {p} "
                                 f"(run build_volume_profile.py)")
    df = pd.read_parquet(p)
    # stored as date32 (compact, and the right type for a daily cache), which
    # pandas surfaces as datetime.date objects — normalise for comparisons
    df["date"] = pd.to_datetime(df["date"])
    return df


def _display_width(symbol: str, lo: float, hi: float) -> float:
    """Finest ladder width that keeps the drawn bucket count under TARGET_BUCKETS."""
    want = max(hi - lo, 1e-9) / TARGET_BUCKETS
    for w in DISPLAY_LADDER[symbol]:
        if w >= want:
            return float(w)
    return float(DISPLAY_LADDER[symbol][-1])


# --------------------------------------------------------------------------- #
#  order-book depth heat                                                       #
# --------------------------------------------------------------------------- #
# Two daily stores (written by collect_orderbook.py in the refresh chain):
#   orderbook_spot/{SYM}/daily/*.parquet   one Binance spot L2 snapshot / day
#   orderbook_depth/{SYM}/daily/*.parquet  Vision futures bookDepth, 10-minute bins
# The payload builders are plain functions so export_static_site can call them
# with a merged DataFrame from the site repo's cross-publisher shared store.
ORDERBOOK_SPOT = DATA_ROOT / "orderbook_spot"
ORDERBOOK_DEPTH = DATA_ROOT / "orderbook_depth"
OB_SPOT_WINDOWS = {"30d": 30, "90d": 90, "1y": 365}
OB_DEPTH_WINDOWS = {"1d": 1, "7d": 7, "30d": 30}


def _ob_unavailable(symbol: str, window: str, note: str) -> Dict:
    return {"symbol": symbol, "window": window, "available": False,
            "as_of": None, "first_ts": None, "last_ts": None,
            "columns": [], "n_cells": 0, "n_snapshots": 0, "n_bins": 0,
            "p99_notional": 0.0, "max_notional": 0.0, "max_cell": None,
            "bands_pct": [], "note": note}


def _ob_store(root: Path, symbol: str) -> Optional[pd.DataFrame]:
    daily = root / symbol / "daily"
    files = sorted(daily.glob("*.parquet")) if daily.exists() else []
    if not files:
        files = sorted((root / symbol).glob("*.parquet"))
    if not files:
        return None
    try:
        return pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)
    except Exception as exc:  # noqa: BLE001 — a bad store must not 500 the page
        logger.warning("orderbook: unreadable store %s/%s: %s", root, symbol, exc)
        return None


def _ob_p99(values: List[float]) -> float:
    v = np.asarray([float(x) for x in values
                    if x is not None and np.isfinite(float(x)) and float(x) > 0])
    return float(np.percentile(v, 99)) if len(v) else 0.0


def orderbook_spot_payload(symbol: str, window: str = "90d",
                           df: Optional[pd.DataFrame] = None) -> Dict:
    """Sparse spot L2 heat: one column per daily snapshot, price buckets."""
    days = OB_SPOT_WINDOWS.get(window)
    if days is None:
        window, days = "90d", OB_SPOT_WINDOWS["90d"]
    if df is None:
        df = _ob_store(ORDERBOOK_SPOT, symbol)
    if df is None or df.empty:
        return _ob_unavailable(symbol, window,
                               "No spot L2 snapshot has been collected yet. Cloud "
                               "runs skip Binance spot REST (geo-blocked); the PC "
                               "refresh carries this layer.")
    d = df[df["symbol"] == symbol].copy()
    if d.empty:
        return _ob_unavailable(symbol, window,
                               f"No spot L2 snapshots for {symbol} in the store.")
    d["day_dt"] = pd.to_datetime(d["day"], utc=True, errors="coerce")
    d = d.dropna(subset=["day_dt"])
    if d.empty:
        return _ob_unavailable(symbol, window, "Spot store has no parseable days.")
    as_of = d["day_dt"].max()
    d = d[d["day_dt"] >= as_of - pd.Timedelta(days=days - 1)]
    # One column per UTC day: keep the day's last snapshot — ALL of its rows,
    # not one row (the snapshot is the unit, not the bucket).
    last = d.groupby("day")["snapshot_ts"].transform("max")
    d = d[d["snapshot_ts"] == last]
    columns: List[Dict] = []
    all_vals: List[float] = []
    max_q, max_cell = 0.0, None
    for _, g in d.groupby("day", sort=True):
        mid = float(g["mid"].iloc[0])
        t = int(g["snapshot_ts"].iloc[0])
        sides = {}
        for side in ("bid", "ask"):
            s = g[g["side"] == side].sort_values("price_bucket")
            sides[side] = [[float(r.price_bucket), float(r.qty_base),
                            float(r.notional_usd), int(r.n_levels)]
                           for r in s.itertuples()]
            for c in sides[side]:
                if c[2] > 0:
                    all_vals.append(c[2])
                if c[2] > max_q:
                    max_q, max_cell = c[2], {"t": t, "side": side,
                                             "p": c[0], "q": c[2]}
        vals = [c[2] for c in sides["bid"] + sides["ask"] if c[2] > 0]
        columns.append({"t": t, "mid": mid, "w99": _ob_p99(vals),
                        "bids": sides["bid"], "asks": sides["ask"]})
    if not columns:
        return _ob_unavailable(symbol, window, "Spot store has no snapshots in window.")
    widths = sorted({float(x) for x in d["bucket_usd"].dropna().unique()})
    n_cells = sum(len(c["bids"]) + len(c["asks"]) for c in columns)
    return {
        "symbol": symbol, "window": window, "available": True,
        "as_of": str(as_of.date()), "first_ts": columns[0]["t"],
        "last_ts": columns[-1]["t"], "bucket_usd": widths[-1] if widths else 10.0,
        "limit_levels": 5000, "source": "binance_spot_rest_depth",
        "n_snapshots": len(columns), "n_cells": int(n_cells),
        "p99_notional": _ob_p99(all_vals), "max_notional": max_q,
        "max_cell": max_cell, "columns": columns,
        "note": "One Binance spot L2 snapshot per daily PC refresh; 5,000 "
                "levels/side cover about +/-1% for BTC and +/-4% for ETH. "
                "Not continuous; not traded volume.",
    }


def orderbook_depth_payload(symbol: str, window: str = "30d",
                            df: Optional[pd.DataFrame] = None) -> Dict:
    """Sparse futures depth-band heat: 10-minute bins x distance bands."""
    days = OB_DEPTH_WINDOWS.get(window)
    if days is None:
        window, days = "30d", OB_DEPTH_WINDOWS["30d"]
    if df is None:
        df = _ob_store(ORDERBOOK_DEPTH, symbol)
    if df is None or df.empty:
        return _ob_unavailable(symbol, window,
                               "No Vision bookDepth days have been collected yet. "
                               "Run the refresh (90-day backfill on first run).")
    d = df[df["symbol"] == symbol].copy()
    if d.empty:
        return _ob_unavailable(symbol, window,
                               f"No Vision bookDepth data for {symbol} in the store.")
    d["bin_dt"] = pd.to_datetime(d["bin_ms"], unit="ms", utc=True)
    as_of = d["bin_dt"].dt.normalize().max()
    d = d[d["bin_dt"] >= as_of - pd.Timedelta(days=days - 1)]
    # One column per bin: keep the last snapshot's rows (all bands), not one row.
    last = d.groupby("bin_ms")["snapshot_ts"].transform("max")
    d = d[d["snapshot_ts"] == last]
    columns: List[Dict] = []
    all_vals: List[float] = []
    max_q, max_cell = 0.0, None
    for bin_ms, g in d.groupby("bin_ms", sort=True):
        mid = float(np.nanmedian(g["mid"].to_numpy(dtype=float)))
        t = int(bin_ms)
        sides = {}
        for side in ("bid", "ask"):
            s = g[g["side"] == side].sort_values("pct_abs")
            cells, prev = [], 0.0
            for r in s.itertuples():
                if float(r.band_notional) > 0 and float(r.pct_abs) > prev:
                    cells.append([float(prev), float(r.pct_abs),
                                  float(r.band_notional), float(r.band_depth)])
                    if float(r.band_notional) > max_q:
                        max_q, max_cell = (float(r.band_notional),
                                           {"t": t, "side": side,
                                            "inner": float(prev),
                                            "outer": float(r.pct_abs),
                                            "q": float(r.band_notional)})
                prev = float(r.pct_abs)
            sides[side] = cells
            all_vals.extend(c[2] for c in cells if c[2] > 0)
        vals = [c[2] for c in sides["bid"] + sides["ask"] if c[2] > 0]
        columns.append({"t": t, "mid": mid, "w99": _ob_p99(vals),
                        "bids": sides["bid"], "asks": sides["ask"]})
    if not columns:
        return _ob_unavailable(symbol, window, "No depth bins in window.")
    n_cells = sum(len(c["bids"]) + len(c["asks"]) for c in columns)
    bands = sorted({round(float(x), 2) for x in d["pct_abs"].dropna().unique()})
    return {
        "symbol": symbol, "window": window, "available": True,
        "as_of": str(as_of.date()), "first_ts": columns[0]["t"],
        "last_ts": columns[-1]["t"], "bin_ms": 600000,
        "source": "binance_vision_futures_um_bookDepth",
        "mid_method": "1m futures close (implied-mid fallback)",
        "bands_pct": bands, "n_bins": len(columns), "n_cells": int(n_cells),
        "p99_notional": _ob_p99(all_vals), "max_notional": max_q,
        "max_cell": max_cell, "columns": columns,
        "note": "Binance USD-M futures only; 30s source snapshots reduced to the "
                "last per 10-minute bin; cumulative depth differenced into bands; "
                "price interval = mid x (1 +/- pct/100). Not exact price levels.",
    }


@app.get("/api/orderbook_spot")
def orderbook_spot(symbol: str = "BTCUSDT", window: str = "90d") -> Dict:
    if symbol not in SYMBOLS:
        raise HTTPException(404, f"symbol {symbol} not in audited set")
    return orderbook_spot_payload(symbol, window)


@app.get("/api/orderbook_depth")
def orderbook_depth(symbol: str = "BTCUSDT", window: str = "30d") -> Dict:
    if symbol not in SYMBOLS:
        raise HTTPException(404, f"symbol {symbol} not in audited set")
    return orderbook_depth_payload(symbol, window)


@app.get("/api/volume_profile")
def volume_profile(symbol: str, window: str = "90d") -> Dict:
    """Where volume actually traded, bucketed by price.

    Deliberately independent of the chart's time zoom: `window` is the span the
    profile is aggregated over, so panning the chart does not change the levels.
    """
    if symbol not in SYMBOLS:
        raise HTTPException(404, f"symbol {symbol} not in audited set")
    if window not in PROFILE_WINDOWS:
        raise HTTPException(400, f"window must be one of {sorted(PROFILE_WINDOWS)}")
    base = SYMBOLS[symbol]["base"]
    df = _profile(symbol)
    if df.empty:
        raise HTTPException(503, "volume profile cache is empty")

    as_of = pd.Timestamp(df["date"].max())
    days = PROFILE_WINDOWS[window]
    window_start = None if days is None else as_of - pd.Timedelta(days=days - 1)
    if window_start is not None:
        df = df[df["date"] >= window_start]
    if df.empty:
        raise HTTPException(503, f"volume profile covers no days in window {window}")

    # regrid the fine cached buckets up to the display width
    w = _display_width(symbol, float(df["price_bucket"].min()),
                       float(df["price_bucket"].max()))
    idx = np.floor(df["price_bucket"].to_numpy() / w).astype(np.int64)
    g = pd.DataFrame({
        "b": idx,
        "q": df["vol_quote"].to_numpy(),
        "v": df["vol_base"].to_numpy(),
        "buy": df["taker_buy_base"].to_numpy(),
        "sell": df["taker_sell_base"].to_numpy(),
        "t": df["trades"].to_numpy(),
    }).groupby("b", sort=True).sum()
    # fill untraded gaps so the value-area walk and the drawn bars stay contiguous
    full = np.arange(int(g.index.min()), int(g.index.max()) + 1)
    g = g.reindex(full, fill_value=0.0)
    qa, va = g["q"].to_numpy(), g["v"].to_numpy()
    ba, sa, ta = g["buy"].to_numpy(), g["sell"].to_numpy(), g["t"].to_numpy()
    prices = full.astype(np.float64) * w

    total = float(qa.sum())
    poc_i = int(np.argmax(qa))
    lo_i = hi_i = poc_i
    acc = float(qa[poc_i])
    while acc < VALUE_AREA_PCT * total and (lo_i > 0 or hi_i < len(qa) - 1):
        left = float(qa[lo_i - 1]) if lo_i > 0 else -1.0
        right = float(qa[hi_i + 1]) if hi_i < len(qa) - 1 else -1.0
        if right >= left:
            hi_i += 1
            acc += right
        else:
            lo_i -= 1
            acc += left

    # --- URPD overlay: on-chain supply by cost basis (BTC only) ---
    urpd = None
    if base == "BTC":
        u, vintage, frozen = _urpd_overlay()
        if u is not None:
            zero_btc = float(u.loc[u["price_bucket_usd"] <= 0, "supply_btc"].sum())
            u = u[u["price_bucket_usd"] > 0]
            steps = np.diff(np.sort(u["price_bucket_usd"].unique()))
            urpd = {
                "available": True,
                "captured_utc": str(u["captured_utc"].iloc[0]),
                "bucket_usd": _f(np.median(steps)) if len(steps) else 200.0,
                "buckets": [{"p": float(r.price_bucket_usd), "s": float(r.supply_btc)}
                            for r in u.itertuples(index=False)],
                "excluded_zero_price_btc": zero_btc,
                "data_vintage": vintage,
                "note": (f"supply by cost basis — one curve per vintage"
                         + (f", data to {vintage}" if vintage else
                            ", frozen mirror generation (the live curve store is "
                            "missing)")
                         + " — and a different measure from the traded-volume bars; "
                           "scaled independently. For the change between two dates "
                           "see the cost-basis overlay (/api/cost_basis)."),
            }

    daily = _daily_frame(symbol)
    real = None
    if "rc_realized_price_usd" in daily.columns:
        ser = daily["rc_realized_price_usd"].dropna()
        if len(ser):
            real = {"value": float(ser.iloc[-1]),
                    "as_of": str(ser.index[-1].date()),
                    "label": "realized price — market-wide average cost basis"}

    return {
        "symbol": symbol, "base": base, "window": window,
        "window_start": None if window_start is None else str(window_start.date()),
        "as_of": str(as_of.date()),
        "bucket_usd": float(w),
        "metric": "traded notional USD — Binance USD-M futures tape (one venue)",
        "total_quote_usd": total,
        "n_days": int(df["date"].nunique()),
        "buckets": [{"p": float(prices[i]), "q": float(qa[i]), "v": float(va[i]),
                     "buy": float(ba[i]), "sell": float(sa[i]), "t": int(ta[i])}
                    for i in range(len(full)) if ta[i] > 0],
        "poc": float(prices[poc_i]),
        "value_area": {"low": float(prices[lo_i]),
                       "high": float(prices[hi_i] + w),
                       "pct": VALUE_AREA_PCT},
        "urpd": urpd,
        "realized_price": real,
        "note": ("bars = traded notional at that price over the window; POC = "
                 "heaviest level, value area = the 70% band around it. buy/sell "
                 "are taker-side base volume. Covers Binance USD-M futures only, "
                 "not the whole market."),
    }


# --------------------------------------------------------------------------- #
#  cost basis — on-chain supply bucketed by acquisition price (URPD)          #
# --------------------------------------------------------------------------- #
# URPD answers "who is holding at what cost, and who is leaving" — a different
# measure from the traded-volume-by-price profile above. It is BTC-UTXO-only.
CHECKONCHAIN = DATA_ROOT / "onchain" / "checkonchain" / "BTC"
CB_DIFFS = CHECKONCHAIN / "urpd_diffs.parquet"
CB_SERIES = CHECKONCHAIN / "checkonchain_series.parquet"
CB_SOURCE = "checkonchain_public_github_pages"
# These windows are live: fetch_checkonchain_urpd.py reads charts.checkonchain.com
# (charts-cdn) and each diff carries its own vintage, so every response names the
# dates it actually spans instead of a global "frozen" stamp. The 14-day window is
# gone -- the live host 404s it -- so it is not offered here either.
COST_BASIS_WINDOWS = {"30d": 30, "90d": 90, "180d": 180, "365d": 365}
# A 4-year comparison is the one long horizon the publisher's own charts cannot
# express: there is no `urpd_diff_1460` upstream, and the live host serves no
# dated curve older than its two fixed-date charts (2024-11-01, 2025-03-01).
# The dated curve archive still holds a 2022 curve, so this window is the only
# one computed here -- by subtracting two archived curves -- while every other
# window stays the publisher's own subtraction.
CB_DATED_WINDOWS = {"4y": 1460}
COST_BASIS_ALL = {**COST_BASIS_WINDOWS, **CB_DATED_WINDOWS}
CB_CURVES = CHECKONCHAIN / "urpd_curves.parquet"
# The per-date archive (fetch_checkonchain_urpd.py --backfill-series).  This is
# what makes a window of the reader's own choosing possible: ~1,200 dated curves
# on ONE uniform $200 grid, 2016-01-03 to today -- one date a week back to the
# start, and every single day from SERIES_DAILY_START on -- so any two of them can
# be subtracted.  Its own grid, deliberately: both ends of such a pair come from
# this store, so the $200-does-not-nest-in-$500 problem that forces CB_ZONE_USD
# on the mixed-store paths cannot arise, and $200 is fine enough to be worth
# having (at $1,000 a 14-day window would be three chunky rows).
CB_ARCHIVE = CHECKONCHAIN / "urpd_archive.parquet"
CB_ARCHIVE_ZONE_USD = 200.0
# The publisher's dated curves (the store the fixed windows and `4y` read) are on
# the charts' own $500 grid. Kept as a named constant because a custom pair reads
# them on that native grid — regrouping it would stop reproducing the prebuilt
# diffs, which are literally this subtraction on this grid.
CB_DATED_ZONE_USD = 500.0
# The archive comes from checkonchain's own series API (bitview.space), not the
# charts CDN the fixed windows come from, so an archive pair names THAT host in
# its payload. Same operator either way -- but a reader checking where a number
# came from should be sent to the host that actually served it.
CB_ARCHIVE_SOURCE = "checkonchain_bitview_series_api"
# A requested date may sit this far outside the archive and still snap to its
# nearest stored date (in the weekly region the anchors are a week apart, so half
# a cadence plus a day; inside the daily region every date is stored and the snap
# is exact).  Further out is not a date we happen to lack -- it is a different
# question, and it is refused rather than answered with somebody else's window.
CB_SNAP_TOLERANCE_DAYS = 4
# A requested END date may sit past the newest stored curve by up to this many
# days and still be answered from that curve.  The fresh end is "not published
# yet", not "a different question", and the reader's most natural gesture -- the
# pencil dragged to the chart's right edge -- always lands there.  Inside the
# daily region the gap to today is a day or two, but this still has to cover a
# store that is behind for its own reasons (the host down, a machine that has not
# finished its catch-up), so it stays at a week.  The days beyond the curve come
# back as the payload's `missing` so the page names them instead of quietly
# drawing a shorter window.  The START end gets no grace: nothing new ever
# arrives at the old end of the axis.
CB_FRESH_GRACE_DAYS = 7
CB_CUSTOM = "custom"
# Finest width that both archive grids divide exactly: the mirror era wrote $200
# buckets, the live host writes $500, and $1,000 holds whole numbers of both --
# so regrouping is a sum, never an interpolation between bucket edges.
#
# The charts' top bucket makes this stricter than the arithmetic suggests: it is
# a catch-all for [its floor, inf), so on the $500 charts the last bucket spans
# past the edge of the grid it is labelled with (measured 2026-06-22: chart
# "$125,500" = 285.48 BTC = our $125,600 bucket plus the upper half of our
# $125,400 one). Clip the fine grid at the chart's top and the two disagree by
# 251.49 BTC in the top zone; at $1,000 zones the catch-all spans the whole zone
# and they agree to 0.0000.
CB_ZONE_USD = 1000.0


def _urpd_overlay():
    """Supply-by-cost-basis buckets for the overlays, with their vintage.

    Returns ``(frame, vintage, frozen)`` with columns
    ``price_bucket_usd / supply_btc / captured_utc``.

    Primary source is the live curve store -- ``urpd_curves.parquet``, rebuilt by
    ``fetch_checkonchain_urpd.py`` from charts.checkonchain.com and current to
    the day. The phase-4 snapshot, captured from the mirror that stopped on
    2026-02-19, is the last-resort fallback and comes back flagged ``frozen`` so
    the two generations can never be mistaken for one another.
    """
    if CB_CURVES.exists():
        df = pd.read_parquet(CB_CURVES)
        df = df[(df["variant"] == "supply") & df["cohort"].isna()]
        if len(df):
            vintage = str(df["as_of_date"].max())
            cur = (df[df["as_of_date"] == vintage]
                   .drop_duplicates(subset=["price_bucket_usd"], keep="first")
                   .sort_values("price_bucket_usd"))
            return (cur.rename(columns={"value": "supply_btc"})
                       [["price_bucket_usd", "supply_btc", "captured_utc"]],
                    vintage, False)
    if URPD.exists():
        return pd.read_parquet(URPD), None, True
    return None, None, False


def _f(v):
    """numpy scalar / NaN / inf -> a plain JSON-safe value.

    pydantic v2 cannot serialize numpy.int64 and a NaN payload 500s the
    response, so every number crossing this boundary goes through here.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else f


@lru_cache(maxsize=8)
def _cb_diffs(window: int):
    if not CB_DIFFS.exists():
        return None
    df = pd.read_parquet(CB_DIFFS)
    df = df[(df["variant"] == "supply") & (df["window_days"] == window)]
    return None if df.empty else df.sort_values("price_bucket_usd")


@lru_cache(maxsize=1)
def _cb_dated_curves():
    """The publisher's own dated curves as one frame, or None when not built.

    Filtered to `variant == "supply"` with a null cohort, as every other URPD
    reader here is: the store also carries the realized/unrealized variants and
    the LTH/STH breakdown, and only the supply rows sum to circulating supply.

    A different generation from `_cb_archive`, and a different grid: these are the
    charts-CDN snapshots the fixed windows and `4y` are built from, on the
    publisher's own $500 buckets and current to the publishing day, where the
    archive here is the bitview series on $200: one curve a day for its recent
    tail, one a week before that.
    """
    if not CB_CURVES.exists():
        return None
    df = pd.read_parquet(CB_CURVES)
    df = df[(df["variant"] == "supply") & df["cohort"].isna()].copy()
    if df.empty:
        return None
    df["_d"] = df["as_of_date"].astype(str).str[:10]
    return df.drop_duplicates(subset=["_d", "price_bucket_usd"], keep="first")


def _dated_native_zone(df: pd.DataFrame, prev: str, cur: str) -> Optional[float]:
    """The single $ step that BOTH of these curves are dense on, or None.

    Dense means 0, w, 2w, ... with no holes: the modern charts publish that way
    at $500, the pre-2024 ones at $200, each up to a clipped top bucket.  Only
    when two ends share one such grid can they be subtracted bucket for bucket,
    and only then is the result the publisher's own subtraction -- which is what
    lets a pair footed on two preset endpoints reproduce the preset exactly.
    Mixing generations instead leaves zeros wherever one curve has no row, and a
    zero in the past reads as coins arriving; the caller regroups at $1,000
    there, where both grids land on whole numbers.
    """
    def width(day: str) -> Optional[float]:
        p = np.sort(df.loc[df["_d"] == day, "price_bucket_usd"]
                    .to_numpy(dtype="float64"))
        if p.size < 2 or p[0] != 0.0:
            return None
        w = float(p[1] - p[0])
        if w <= 0 or not np.allclose(p, np.arange(p.size) * w, rtol=0, atol=1e-6):
            return None
        return w

    a, b = width(prev), width(cur)
    return a if (a is not None and a == b) else None


def _cb_dated_frame(prev: str, cur: str,
                    zone_usd: Optional[float] = None) -> Optional[pd.DataFrame]:
    """Subtract two dated curves from the publisher's own curve store.

    Returns the same columns `_cb_diffs` returns, so no endpoint can tell the
    sources apart, or None when the store is not built.

    Both ends are read from the same store, which is what makes the subtraction
    meaningful. `zone_usd=None` keeps those curves' own grid when they share one
    (see `_dated_native_zone`): the publisher's prebuilt diffs are this same
    subtraction on the native buckets, so a pair that lands on two stored dates
    reproduces the preset row for row -- asserted in
    verify_all.verify_cb_dated_custom, measured across the four shipped windows as
    delta/past/current/pct_accumulated bit-identical and pct_distributed within
    1e-16 (the publisher's own cumsum rounds differently in the last bit; nothing
    downstream can see it). The `4y` window passes `CB_ZONE_USD` explicitly, which
    is a summing step, not an estimate.

    Coverage is *not* symmetric: the 2022 curve stops at $68,600 because the
    publisher clipped its axis to the price range of that day, so a zone above
    that compares against an absent past. The endpoint reports where the past
    curve ends rather than letting the gap read as pure accumulation.
    """
    df = _cb_dated_curves()
    if df is None:
        return None
    if zone_usd is None:
        zone_usd = _dated_native_zone(df, prev, cur) or CB_ZONE_USD

    def zones(day: str) -> pd.Series:
        rows = df[df["_d"] == day]
        p = rows["price_bucket_usd"].to_numpy(dtype="float64")
        v = rows["value"].to_numpy(dtype="float64")
        return pd.Series(v, index=np.floor(p / zone_usd) * zone_usd).groupby(level=0).sum()

    past, now = zones(prev), zones(cur)
    grid = past.index.union(now.index).sort_values()
    past = past.reindex(grid, fill_value=0.0)
    now = now.reindex(grid, fill_value=0.0)
    delta = now - past
    pos, neg = delta.clip(lower=0.0), (-delta).clip(lower=0.0)
    # Same definition the publisher uses: cumulative share ascending by price,
    # verified against 252 stored rows of the live 90d diff (max|diff| = 0.0).
    acc = pos.cumsum() / pos.sum() if pos.sum() else pos * 0.0
    dis = neg.cumsum() / neg.sum() if neg.sum() else neg * 0.0
    out = pd.DataFrame({
        "price_bucket_usd": grid.to_numpy(dtype="float64"),
        "delta": delta.to_numpy(),
        "past_value": past.to_numpy(),
        "current_value": now.to_numpy(),
        "pct_accumulated": acc.to_numpy(),
        "pct_distributed": dis.to_numpy(),
    })
    out["as_of_date"] = cur
    out["prev_as_of_date"] = prev
    out["span_days"] = (pd.Timestamp(cur) - pd.Timestamp(prev)).days
    return out.sort_values("price_bucket_usd").reset_index(drop=True)


@lru_cache(maxsize=4)
def _cb_dated_pair(target_days: int):
    """Synthesise a diff between the newest dated curve and the one nearest
    `target_days` before it -- the `4y` window, which the publisher never built.
    """
    df = _cb_dated_curves()
    if df is None:
        return None
    days = _archive_dates(df)
    as_of = days[-1]
    earlier = [d for d in days if d < as_of]
    if not earlier:
        return None
    target = pd.Timestamp(as_of) - pd.Timedelta(days=target_days)
    prev = min(earlier, key=lambda d: abs((pd.Timestamp(d) - target).days))
    return _cb_dated_frame(prev, as_of, CB_ZONE_USD)


CB_ARCHIVE_META = CB_ARCHIVE.with_name("urpd_archive_meta.json")
# What to say when the sidecar is missing or predates the field: the step this
# archive was built on for years, and still the honest answer for a store that
# has not been rebuilt since the daily region was introduced.
CB_ARCHIVE_CADENCE_FALLBACK = 7


@lru_cache(maxsize=1)
def _cb_archive_meta() -> Dict[str, Any]:
    """The archive store's own sidecar: how fine its grid is, and from where.

    `cadence_days` is the FINEST step the store reaches (1 once the daily region
    is in, 7 while it is still being fetched -- the collector's store_dates() is
    all-or-nothing), and `daily_from` names where that daily region starts.  Read
    from the sidecar rather than assumed, because the published page has to say
    what the server says: the exporter ships these same two fields into the
    artifact and the manifest, and probe_dated_pair.js diffs the two payloads
    field for field.
    """
    try:
        return json.loads(CB_ARCHIVE_META.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def _cb_archive():
    """The $200 per-date archive as one frame, or None when it is not built.

    Filtered to `variant == "supply"` with a null cohort, as every other URPD
    reader here is: the store also carries cohort rows, and only the supply rows
    sum to circulating supply.
    """
    if not CB_ARCHIVE.exists():
        return None
    df = pd.read_parquet(CB_ARCHIVE)
    df = df[(df["variant"] == "supply") & df["cohort"].isna()].copy()
    if df.empty:
        return None
    df["_d"] = df["as_of_date"].astype(str).str[:10]
    return df.drop_duplicates(subset=["_d", "price_bucket_usd"], keep="first")


def _archive_dates(df: pd.DataFrame) -> List[str]:
    """Ascending ISO dates the archive holds.  ISO sorts chronologically."""
    return sorted(df["_d"].unique().tolist())


def _archive_snap(days: List[str], target: str) -> Optional[str]:
    """The archived date closest to `target`, ties going to the EARLIER date.

    The browser implements this exact rule (nearestDate in app.js): if the two
    ever disagreed, the same window would read differently depending on whether
    the server or the shipped copy answered it.
    """
    i = bisect.bisect_left(days, target)
    cands = days[max(0, i - 1):i + 1]
    if not cands:
        return None
    return min(cands, key=lambda d: (abs((date.fromisoformat(d)
                                          - date.fromisoformat(target)).days), d))


def _cb_archive_pair(req_from: str, req_to: str
                     ) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
    """Subtract two dated curves from the per-date archive.

    Returns ``(frame, info)``.  The frame carries exactly the columns `_cb_diffs`
    returns, so the endpoint cannot tell the sources apart; ``info`` records what
    was asked for and what was served -- including when the archive has no date
    at the requested one and the nearest stored date was used instead.
    """
    info: Dict[str, Any] = {"requested_from": req_from, "requested_to": req_to,
                            "from_snapped": False, "to_snapped": False,
                            "archive": None, "missing": None, "reason": None}
    df = _cb_archive()
    if df is None:
        info["reason"] = ("dated-curve archive not built — run "
                          "fetch_checkonchain_urpd.py --backfill-series")
        return None, info
    days = _archive_dates(df)
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    meta = _cb_archive_meta()
    info["archive"] = {"first": days[0], "last": days[-1], "n_dates": len(days),
                       "bucket_usd": CB_ARCHIVE_ZONE_USD,
                       "cadence_days": meta.get("cadence_days", CB_ARCHIVE_CADENCE_FALLBACK),
                       "daily_from": meta.get("daily_from")}
    a, b = date.fromisoformat(req_from), date.fromisoformat(req_to)
    lo = (first - timedelta(days=CB_SNAP_TOLERANCE_DAYS)).isoformat()
    hi = (last + timedelta(days=CB_SNAP_TOLERANCE_DAYS)).isoformat()
    for label, d in (("from", a), ("to", b)):
        if not lo <= d.isoformat() <= hi:
            # The fresh end may sit past the newest stored curve: within the
            # grace it is answered from that curve (CB_FRESH_GRACE_DAYS) and the
            # days the curve cannot reach are reported as missing.
            if not (label == "to" and d.isoformat() > days[-1]
                    and (d - last).days <= CB_FRESH_GRACE_DAYS):
                info["reason"] = (f"{label} {d.isoformat()} is outside the archive, "
                                  f"which covers {days[0]} to {days[-1]}")
                return None, info
    prev = _archive_snap(days, a.isoformat())
    cur = _archive_snap(days, b.isoformat())
    info["from_snapped"] = prev != a.isoformat()
    info["to_snapped"] = cur != b.isoformat()
    info["requested_span_days"] = (b - a).days
    if prev is None or cur is None or prev >= cur:
        info["reason"] = (f"the archive has no date between {req_from} and "
                          f"{req_to} — both ends land on the same stored curve "
                          f"({prev}); pick a wider window")
        return None, info
    if b.isoformat() > days[-1]:
        # Days past the newest stored curve: the nearest anchor is that curve,
        # and the tail it cannot reach is named, not silently dropped.
        info["missing"] = {"from": (last + timedelta(days=1)).isoformat(),
                           "to": b.isoformat()}

    def curve(day: str) -> pd.Series:
        rows = df[df["_d"] == day]
        return pd.Series(rows["value"].to_numpy(dtype="float64"),
                         index=rows["price_bucket_usd"].to_numpy(dtype="float64")
                         ).groupby(level=0).sum()

    past, now = curve(prev), curve(cur)
    grid = past.index.union(now.index).sort_values()
    past = past.reindex(grid, fill_value=0.0)
    now = now.reindex(grid, fill_value=0.0)
    delta = now - past
    pos, neg = delta.clip(lower=0.0), (-delta).clip(lower=0.0)
    # Same definition the publisher uses: cumulative share ascending by price,
    # verified against 252 stored rows of the live 90d diff (max|diff| = 0.0).
    acc = pos.cumsum() / pos.sum() if pos.sum() else pos * 0.0
    dis = neg.cumsum() / neg.sum() if neg.sum() else neg * 0.0
    out = pd.DataFrame({
        "price_bucket_usd": grid.to_numpy(dtype="float64"),
        "delta": delta.to_numpy(),
        "past_value": past.to_numpy(),
        "current_value": now.to_numpy(),
        "pct_accumulated": acc.to_numpy(),
        "pct_distributed": dis.to_numpy(),
    })
    out["as_of_date"] = cur
    out["prev_as_of_date"] = prev
    out["span_days"] = (date.fromisoformat(cur) - date.fromisoformat(prev)).days
    info.update(as_of=cur, prev_as_of=prev, span_days=int(out["span_days"].iloc[0]))
    return out.sort_values("price_bucket_usd").reset_index(drop=True), info


@lru_cache(maxsize=64)
def _cb_dated_custom(req_from: str, req_to: str
                     ) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
    """Answer a reader's own date pair from the publisher's dated curves.

    Same contract as `_cb_archive_pair` and the same snap rule, but the other
    store -- and that is the point of preferring it. `urpd_curves.parquet` holds
    the dated snapshots the fixed windows are built from, so a pair whose ends
    both foot on stored dates IS the preset (or the nearest one), not a lookalike
    on a different grid; and it reaches the newest *published* date, where the
    archive reaches the newest stored curve whatever weekday that is.  A reader asking for "the
    last month" was being served a window ending four or five days earlier than
    the panel beside it, and the two disagreed about which way the newest coins
    moved -- precisely the days that move them.

    Unusable pairs return ``(None, info)`` with the reason, and the caller falls
    back to `_cb_archive_pair`; the choice between the two is not the reader's to
    make, but the payload names the one that answered.
    """
    info: Dict[str, Any] = {"requested_from": req_from, "requested_to": req_to,
                            "from_snapped": False, "to_snapped": False,
                            "dated_store": None, "missing": None, "reason": None}
    df = _cb_dated_curves()
    if df is None:
        info["reason"] = ("dated curve store not built — run "
                          "fetch_checkonchain_urpd.py")
        return None, info
    days = _archive_dates(df)
    info["dated_store"] = {"first": days[0], "last": days[-1],
                           "n_dates": len(days),
                           "bucket_usd": CB_DATED_ZONE_USD, "cadence_days": None}
    a, b = date.fromisoformat(req_from), date.fromisoformat(req_to)
    prev = _archive_snap(days, a.isoformat())
    cur = _archive_snap(days, b.isoformat())
    info["requested_span_days"] = (b - a).days
    # The tolerance is per endpoint, never a window: this store's dates are
    # irregular (25 of them across ten years, monthly at the recent end), so
    # "nearest stored date" can be months away, and answering a 2023 question
    # with a 2022 curve because the store happens to span both would be a silent
    # substitution of somebody else's window. Inside 4 days it is the same
    # measurement off by the publication lag; outside it, the archive --
    # never more than 3.5 days from any date in its weekly region, and exact in
    # its daily one -- is the honest source.
    for label, want, got in (("from", a, prev), ("to", b, cur)):
        if got is None:
            info["reason"] = f"no dated curves at all for {label} {want.isoformat()}"
            return None, info
        gap = abs((date.fromisoformat(got) - want).days)
        if gap > CB_SNAP_TOLERANCE_DAYS:
            # The fresh end may sit past the newest dated curve: within the
            # grace it is answered from that curve -- "not published yet", not
            # a different question -- and the days it cannot reach come back as
            # missing.  The start end gets no grace.
            if not (label == "to" and want.isoformat() > days[-1]
                    and (want - date.fromisoformat(days[-1])).days
                    <= CB_FRESH_GRACE_DAYS):
                info["reason"] = (f"{label} {want.isoformat()} is {gap} days from the "
                                  f"nearest dated curve ({got}) — too far to stand in "
                                  f"for it, so the archive answers instead")
                return None, info
    info["from_snapped"] = prev != a.isoformat()
    info["to_snapped"] = cur != b.isoformat()
    if prev is None or cur is None or prev >= cur:
        info["reason"] = (f"{req_from} → {req_to} resolves to a single dated "
                          f"curve ({prev}) — pick a window that spans two")
        return None, info
    if b.isoformat() > days[-1]:
        # Days past the newest stored curve: the nearest anchor is that curve,
        # and the tail it cannot reach is named, not silently dropped.
        info["missing"] = {"from": (date.fromisoformat(days[-1])
                                    + timedelta(days=1)).isoformat(),
                           "to": b.isoformat()}
    frame = _cb_dated_frame(prev, cur)
    if frame is None:
        info["reason"] = "no dated curve pair available"
        return None, info
    native = _dated_native_zone(df, prev, cur)
    info["grid"] = {"zone_usd": native or CB_ZONE_USD, "regrouped": native is None}
    info.update(as_of=cur, prev_as_of=prev, span_days=int(frame["span_days"].iloc[0]))
    return frame, info


@lru_cache(maxsize=1)
def _cb_series():
    if not CB_SERIES.exists():
        return None
    return pd.read_parquet(CB_SERIES)


def _latest_metric(metric: str):
    s = _cb_series()
    if s is None:
        return None, None
    x = s[s["metric"] == metric]
    if x.empty:
        return None, None
    x = x.sort_values("date")
    return _f(x["value"].iloc[-1]), str(x["date"].iloc[-1])


def _metric_on(metric: str, date: str):
    """`metric` stamped exactly `date`, else the latest row at or before it.

    The cost-basis curves and the price series come from the same upstream run,
    so an exact hit is the normal case; the fallback exists because a curve can
    be stamped a day the price series skipped. Never falls FORWARD in time --
    a price from after the curve would silently date the distribution wrong.
    """
    s = _cb_series()
    if s is None or date is None:
        return None
    x = s[s["metric"] == metric]
    if x.empty:
        return None
    d = str(date)[:10]
    x = x.assign(_d=x["date"].astype(str).str[:10]).sort_values("_d")
    hit = x[x["_d"] == d]
    if hit.empty:
        hit = x[x["_d"] <= d]
    return _f(hit["value"].iloc[-1]) if len(hit) else None


def _holder_block() -> Dict:
    """LTH/STH cohort panel data, stamped with the vintage it really has."""
    pct = {"lth_supply_pct": _latest_metric("lth_supply_share")[0],
           "sth_supply_pct": _latest_metric("sth_supply_share")[0]}
    if all(v is None for v in pct.values()):
        return {"available": False, "data_vintage": None,
                "note": "cohort store not built — run fetch_checkonchain_urpd.py"}
    fields = {}
    for key, metric in (("lth_supply_btc", "lth_supply_btc"),
                        ("sth_supply_btc", "sth_supply_btc"),
                        ("lth_profit_btc", "lth_supply_profit_btc"),
                        ("lth_loss_btc", "lth_supply_loss_btc"),
                        ("sth_profit_btc", "sth_supply_profit_btc"),
                        ("sth_loss_btc", "sth_supply_loss_btc"),
                        ("lth_profit_share", "lth_profit_share"),
                        ("sth_profit_share", "sth_profit_share"),
                        ("sopr", "sopr"), ("sopr_ema_7d", "sopr_ema_7d"),
                        ("sth_mvrv", "sth_mvrv"),
                        ("sth_realized_price_usd", "sth_realized_price_usd"),
                        ("price_usd", "price_usd")):
        fields[key] = _latest_metric(metric)[0]
    vint = _latest_metric("lth_supply_btc")[1]
    sth, sth_p = fields["sth_supply_btc"], fields["sth_profit_btc"]
    fields["sth_underwater_pct"] = (
        _f(1 - sth_p / sth) if sth and sth_p is not None and sth > 0 else None)
    return {"available": True, "data_vintage": vint, "source": CB_SOURCE,
            "as_of": vint, **pct, **fields,
            "note": (f"Computed by checkonchain from their own node; this project "
                     f"never ran one. Data vintage {vint} — the upstream repo "
                     f"froze there, so this is history, not a live reading.")}


def _cb_payload(df: pd.DataFrame, symbol: str, window: str, empty: Dict,
                kind: str, info: Optional[Dict[str, Any]] = None) -> Dict:
    """The response body for one cost-basis pair, whichever source produced it.

    One copy on purpose.  The publisher's own diffs, the locally computed 4y pair
    and an archive pair differ in where the two curves came from, never in how
    they are read -- and a second implementation would be a second set of numbers
    for the same question.  `kind` only decides the provenance sentence.
    """
    zero = df[df["price_bucket_usd"] <= 0]
    body = df[df["price_bucket_usd"] > 0]
    if body.empty:
        return {**empty, "note": f"no {window} cost-basis diff in the store"}

    def curve(col):
        out = []
        for r in body.itertuples(index=False):
            v = _f(getattr(r, col))
            if v is not None:
                out.append({"p": float(r.price_bucket_usd), "s": v})
        return out

    as_of = str(df["as_of_date"].iloc[0])
    prev = str(df["prev_as_of_date"].iloc[0])
    span = _f(df["span_days"].iloc[0])
    # The cost-basis grid is its own ($200 here), NOT the tape profile's bucket
    # width -- the overlay must size bars by this or the geometry lies.
    px = np.sort(body["price_bucket_usd"].to_numpy(dtype="float64"))
    step = np.diff(px)
    bucket_usd = _f(step[step > 0].min()) if step.size else None
    # highest price at which the *earlier* curve still holds supply
    held = body.loc[body["past_value"].fillna(0) > 0, "price_bucket_usd"]
    prev_covers_to = _f(held.max()) if len(held) else None
    if kind == "dated-pair" and prev_covers_to:
        provenance = (f"COMPUTED LOCALLY from two archived curves ({prev} minus "
                      f"{as_of}), regrouped to ${CB_ZONE_USD:,.0f} zones: the "
                      f"publisher never built this window, and the {prev} curve "
                      f"covers only to ${prev_covers_to:,.0f}, so everything above "
                      f"that line is supply that arrived since, not supply that "
                      f"was already there. ")
    elif kind == "dated-pair-custom":
        grid = (info or {}).get("grid") or {}
        if not bucket_usd:
            grid_note = "on the publisher's own buckets"
        elif grid.get("regrouped"):
            grid_note = (f"regrouped to ${bucket_usd:,.0f} zones because the two "
                         f"curves come from different grid generations, which is a "
                         f"sum, not an estimate")
        else:
            grid_note = (f"on the ${bucket_usd:,.0f} grid both curves are "
                         f"published on — the same subtraction the publisher's "
                         f"own fixed windows are")
        provenance = (f"COMPUTED LOCALLY from two of the publisher's own dated "
                      f"curves ({prev} minus {as_of}), {grid_note}: the publisher "
                      f"never built this window, but these are the curves its "
                      f"fixed windows are built from, so a pair landing on stored "
                      f"dates reads the same as the preset beside it."
                      # "each end snapped" would overstate: only one may have moved
                      + (f" Those are the stored dates nearest the ones asked for, "
                         f"{info['requested_from']} → {info['requested_to']}."
                         if info and (info["from_snapped"] or info["to_snapped"])
                         else "")
                      # Only when the earlier curve really does stop short of the
                      # frame: the archive branch fires this clause whenever the
                      # past has a top at all, which for two curves of the same
                      # generation is always, and there it says nothing. A caveat
                      # that is always printed is a caveat nobody reads.
                      + (f" The {prev} curve covers only to "
                         f"${prev_covers_to:,.0f}, so every zone above that line "
                         f"is supply that arrived since, not a like-for-like pair."
                         if prev_covers_to and prev_covers_to < px.max() else "")
                      + f" The newest dated curves are still being restated "
                        f"upstream, so a window ending at {as_of} is provisional. ")
    elif kind == "archive-pair":
        provenance = (f"COMPUTED LOCALLY from two dated curves in the archive "
                      f"({prev} minus {as_of}), on the archive's own "
                      f"${CB_ARCHIVE_ZONE_USD:,.0f} grid: the publisher never "
                      f"built this window. Each end snaps to the nearest stored "
                      f"date — one curve a day for the recent tail, one a week "
                      f"before that"
                      + (f" — asked {info['requested_from']} → "
                         f"{info['requested_to']}." if info else ".")
                      + (f" The {prev} curve covers only to "
                         f"${prev_covers_to:,.0f}, so every zone above that line "
                         f"is supply that arrived since, not a like-for-like "
                         f"pair." if prev_covers_to else " ")
                      + f" The newest archive dates are still being restated "
                        f"upstream, so a window ending at {as_of} is provisional. ")
    else:
        provenance = ""
    extra: Dict[str, Any] = {}
    if kind in ("archive-pair", "dated-pair-custom") and info:
        extra = {
            "requested_from": info["requested_from"],
            "requested_to": info["requested_to"],
            "requested_span_days": info["requested_span_days"],
            "from_snapped": info["from_snapped"],
            "to_snapped": info["to_snapped"],
            # The days past the answering store's newest curve, when the
            # requested end runs past it: the pair is still drawn (from that
            # curve), and the page names the tail in red rather than quietly
            # showing a shorter window.  None when the end was inside the store.
            "missing": info.get("missing"),
            # which store answered: the fixed windows are the publisher's own
            # prebuilt diffs, a dated pair is its dated curves subtracted here,
            # and the archive is the bitview series -- a reader must be
            # able to tell that apart from the panel beside it
            "estimate_kind": kind,
            "window_label": f"{prev} → {as_of}",
        }
        if kind == "archive-pair":
            extra["archive"] = info["archive"]
        else:
            extra["dated_store"] = info["dated_store"]
            extra["grid"] = info.get("grid")
    return {
        "symbol": symbol, "window": window, "available": True,
        "as_of": as_of, "prev_as_of": prev, "data_vintage": as_of,
        "source": (CB_ARCHIVE_SOURCE if kind == "archive-pair" else CB_SOURCE),
        "units": "BTC", "bucket_usd": bucket_usd,
        "span_days": int(span) if span is not None else None,
        "delta": [{"p": float(r.price_bucket_usd), "d": _f(r.delta),
                   "past": _f(r.past_value), "now": _f(r.current_value),
                   "cum_acc": _f(r.pct_accumulated),
                   "cum_dist": _f(r.pct_distributed)}
                  for r in body.itertuples(index=False)],
        "past": {"date": prev, "buckets": curve("past_value")},
        "current": {"date": as_of, "buckets": curve("current_value")},
        # Spot at each end of the comparison. This is what lets the panel say
        # "above the price" vs "below it" -- i.e. whether a cohort being spent is
        # realising a loss (capitulation) or a profit (distribution into
        # strength). Per-window, not a constant: each window is stamped with its
        # own pair of dates, and they need not share an endpoint.
        "price_usd": _metric_on("price_usd", as_of),
        "price_usd_prev": _metric_on("price_usd", prev),
        "net_delta_btc": _f(body["delta"].sum()),
        # the $0 bucket is ~14% of supply and has no meaningful price, so it is
        # held out of the bars but reported rather than silently dropped
        "excluded_zero_price_btc": {
            "supply_btc": _f(zero["current_value"].sum()) if len(zero) else None,
            "delta_btc": _f(zero["delta"].sum()) if len(zero) else None},
        # Where the earlier curve stops. For a publisher diff it is the top of
        # the grid; for a locally computed pair it is the edge of that day's
        # chart axis, and above it the change reads as pure accumulation.
        "prev_curve_covers_to_usd": prev_covers_to,
        "computed": kind != "publisher",
        "note": (f"Change in supply by acquisition price, {prev} to {as_of}. "
                 f"Green = coins whose cost basis is here grew (accumulation); "
                 f"red = that cohort was spent/moved (distribution). Coins leaving "
                 f"a bucket at a price above their cost basis is realised profit, "
                 f"below it is realised loss. cum_acc/cum_dist are cumulative "
                 f"shares measured UP the price axis (large at the top of the "
                 f"range), not per bucket. "
                 + provenance
                 + f"DATA VINTAGE {as_of} — a dated measurement, not a live tape."),
        **extra,
    }


@app.get("/api/cost_basis")
# The Query() marker rides in Annotated rather than as the parameter default on
# purpose: export_static_site.py calls this function directly, and there a bare
# `from_: Optional[str] = Query(None)` default is a *truthy* Query object that
# the validation below reads as a caller-supplied date.
def cost_basis(symbol: str, window: str = "90d",
               from_: Annotated[Optional[str], Query(alias="from")] = None,
               to: Annotated[Optional[str], Query()] = None) -> Dict:
    """Supply bucketed by the price each UTXO last moved at, and its change.

    Trace invariant checked at collection time (trace0 == trace2 - trace1), so
    `delta` is the source's own subtraction of the two curves rather than a
    re-derivation here.

    `window=custom&from=&to=` is the one window nobody ever published, so the
    reader picks both ends.  It is answered from the publisher's OWN dated curves
    when both ends foot on them (each within CB_SNAP_TOLERANCE_DAYS days of a
    stored date) -- those are the curves the fixed windows are built from, so
    such a pair agrees with the preset beside it and reaches the newest published
    date -- and from the $200 archive otherwise, which is the only store
    that can reach further back than the dated ones.  The payload names which one
    answered (`estimate_kind`), and `from`/`to` are ISO dates, not the epoch-ms
    bounds that /api/ohlc and /api/volume_profile call start/end.
    """
    if symbol not in SYMBOLS:
        raise HTTPException(404, f"symbol {symbol} not in audited set")
    custom = window == CB_CUSTOM
    if not custom and window not in COST_BASIS_ALL:
        raise HTTPException(400, "window must be one of "
                                 + ", ".join([*COST_BASIS_ALL, CB_CUSTOM]))
    if not custom and (from_ or to):
        raise HTTPException(400, "from/to apply to window=custom only")
    if custom and not (from_ and to):
        raise HTTPException(400, "window=custom needs both "
                                 "from=YYYY-MM-DD and to=YYYY-MM-DD")
    base = SYMBOLS[symbol]["base"]
    empty = {"symbol": symbol, "window": window, "available": False, "as_of": None,
             "data_vintage": None, "delta": [], "past": None, "current": None,
             "price_usd": None, "price_usd_prev": None,
             "excluded_zero_price_btc": None}
    if base != "BTC":
        # Honest-unavailable, matching the ETH branch of /api/onchain_snapshot.
        return {**empty, "note": "URPD buckets supply by the price each UTXO last "
                                 "moved at, so it is a BTC-UTXO concept — no ETH "
                                 "equivalent exists."}
    if custom:
        try:
            a, b = date.fromisoformat(from_), date.fromisoformat(to)
        except ValueError:
            raise HTTPException(400, "from/to must be YYYY-MM-DD dates")
        if a >= b:
            raise HTTPException(400, f"from ({from_}) must be earlier than "
                                     f"to ({to})")
        dated_df, dated_info = _cb_dated_custom(a.isoformat(), b.isoformat())
        if dated_df is not None:
            # Preferred when it can answer: these are the curves the presets are
            # built from, so a pair footed on stored dates reads the same as the
            # fixed window next to it -- and it ends at the newest PUBLISHED
            # date, where the archive can end on any stored day.
            return _cb_payload(dated_df, symbol, window, empty,
                               "dated-pair-custom", dated_info)
        df, info = _cb_archive_pair(a.isoformat(), b.isoformat())
        if df is None:
            # Honest-unavailable with the archive's own span, never a silently
            # substituted window: the reader asked for specific dates.
            return {**empty, "requested_from": info["requested_from"],
                    "requested_to": info["requested_to"],
                    "archive": info["archive"],
                    # why the preferred store could not take it, so the refusal
                    # names both cases rather than only the fallback's
                    "dated_reason": dated_info.get("reason"),
                    "note": info["reason"] or "no archived curve pair available"}
        return _cb_payload(df, symbol, window, empty, "archive-pair", info)
    computed = window in CB_DATED_WINDOWS
    df = (_cb_dated_pair(CB_DATED_WINDOWS[window]) if computed
          else _cb_diffs(COST_BASIS_WINDOWS[window]))
    if df is None:
        return {**empty, "note": ("cost-basis store not built — run "
                                  "fetch_checkonchain_urpd.py") if not computed else
                                 (f"no archived curve pair spans {window} — run "
                                  f"fetch_checkonchain_urpd.py to rebuild the "
                                  f"dated curve store")}
    return _cb_payload(df, symbol, window, empty,
                       "dated-pair" if computed else "publisher")


STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
