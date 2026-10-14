"""repair_kline_stores.py — ONE-TIME repair for the 2026-08-21 kline store
corruption. Keep on disk as incident evidence; not part of the refresh loop.

What happened: refresh_before_visualizer.extend_klines read the existing
store with columns=["open_time"] (for the end check) and then reused that
truncated frame in the concat — so the six futures UM stores
(BTCUSDT/ETHUSDT x 1d/1h/1m) were rewritten with the FULL history rows
carrying only open_time (OHLCV = NaN). The 81 appended days were correct.
No other stores were touched (spot, tape, rollup cache, vsa all intact).

Repair sources (both verified intact, no network needed):
  history: processed/vsa_klines/futures_um/{SYM}/{TF}/merged.parquet — the
           audited gap-flagged copies of the SAME original store content
           (same source store, 0 NaN, ends 2026-05-31)
  new days: raw/vision_klines_zip/{SYM}_{TF}_{date}.zip — all 486 ZIPs
           cached by the refresh before the corruption

Column conventions restored to match the original store (verified against
surviving samples): close_time = open_time + interval_ms - 1, ignore = 0,
symbol/market/data_type/interval strings, is_zero_volume = (volume == 0),
ohlc_integrity_flag = violated OHLC containment.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

BASE = Path(__file__).resolve().parent
KLINE_STORE = BASE.parent / "binance_full_dataset" / "processed"
VSA = BASE / "processed" / "vsa_klines"
ZIP_CACHE = BASE / "raw" / "vision_klines_zip"

INTERVAL_MS = {"1d": 86_400_000, "1h": 3_600_000, "1m": 60_000}
SYMS = ("BTCUSDT", "ETHUSDT")
INTERVALS = ("1d", "1h", "1m")
STORE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
              "quote_asset_volume", "num_trades", "taker_buy_base_vol",
              "taker_buy_quote_vol", "ignore", "datetime_utc", "symbol", "market",
              "data_type", "interval", "is_zero_volume", "ohlc_integrity_flag"]


def _history_df(sym: str, tf: str) -> pd.DataFrame:
    """Original store history, rebuilt from the intact vsa_klines copy."""
    p = VSA / "futures_um" / sym / tf / "merged.parquet"
    if not p.exists():
        raise FileNotFoundError(f"vsa history source missing: {p}")
    df = pd.read_parquet(p)
    assert df["volume"].notna().all(), f"vsa {sym} {tf} has NaN volume — abort"
    df = df.drop(columns=["is_gap", "cross_filled"])
    ms = INTERVAL_MS[tf]
    df["close_time"] = df["open_time"] + ms - 1
    df["ignore"] = 0
    df["symbol"] = sym
    df["market"] = "futures_um"
    df["data_type"] = "klines"
    df["interval"] = tf
    df["is_zero_volume"] = df["volume"] == 0.0
    df["ohlc_integrity_flag"] = ~(
        (df["low"] <= df[["open", "close"]].min(axis=1))
        & (df["high"] >= df[["open", "close"]].max(axis=1)))
    return df[STORE_COLS]


def _new_days_df(sym: str, tf: str) -> pd.DataFrame:
    """The 81 appended days, re-parsed from the raw ZIP cache (same code as
    the refresh script's _kline_frame)."""
    import zipfile
    import io
    frames = []
    for z in sorted(ZIP_CACHE.glob(f"{sym}_{tf}_*.zip")):
        with zipfile.ZipFile(z) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".csv"))
            csv = zf.read(name)
        first = csv.split(b"\n", 1)[0]
        df = pd.read_csv(io.BytesIO(csv),
                         names=None if first.startswith(b"open_time") else
                         ["open_time", "open", "high", "low", "close", "volume",
                          "close_time", "quote_asset_volume", "num_trades",
                          "taker_buy_base_vol", "taker_buy_quote_vol", "ignore"],
                         header=0 if first.startswith(b"open_time") else None)
        df.columns = ["open_time", "open", "high", "low", "close", "volume",
                      "close_time", "quote_asset_volume", "num_trades",
                      "taker_buy_base_vol", "taker_buy_quote_vol", "ignore"]
        for c in ("open", "high", "low", "close", "volume", "quote_asset_volume",
                  "taker_buy_base_vol", "taker_buy_quote_vol"):
            df[c] = df[c].astype("float64")
        for c in ("open_time", "close_time", "num_trades", "ignore"):
            df[c] = df[c].astype("int64")
        df["datetime_utc"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["symbol"] = sym
        df["market"] = "futures_um"
        df["data_type"] = "klines"
        df["interval"] = tf
        df["is_zero_volume"] = df["volume"] == 0.0
        df["ohlc_integrity_flag"] = ~(
            (df["low"] <= df[["open", "close"]].min(axis=1))
            & (df["high"] >= df[["open", "close"]].max(axis=1)))
        frames.append(df[STORE_COLS])
    return pd.concat(frames, ignore_index=True)


def repair(sym: str, tf: str) -> None:
    d = KLINE_STORE / "futures_um" / "klines" / sym / tf
    p = d / "merged.parquet"
    if not p.exists():
        cands = sorted(d.glob("*.parquet"))
        if not cands:
            raise FileNotFoundError(f"store missing: {d}")
        p = cands[0]
    hist = _history_df(sym, tf)
    new = _new_days_df(sym, tf)
    comb = pd.concat([hist, new]).drop_duplicates("open_time", keep="first") \
        .sort_values("open_time").reset_index(drop=True)
    tmp = p.with_name(p.name + ".tmp")
    pq.write_table(pa.Table.from_pandas(comb), tmp, compression="zstd", version="2.6")
    os.replace(tmp, p)
    n_hist, n_new = len(hist), len(new)
    logging.info("%s %s: rebuilt (%s) = %d history + %d new days, %d rows, "
                 "0 NaN expected", sym, tf, p.name, n_hist, n_new, len(comb))
    assert comb["volume"].notna().all() and comb["open"].notna().all(), \
        f"{sym} {tf}: repair still has NaN — abort"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    for sym in SYMS:
        for tf in INTERVALS:
            repair(sym, tf)
    print("\n=== KLINE STORES REPAIRED ===")


if __name__ == "__main__":
    main()
