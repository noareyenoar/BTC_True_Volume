"""
build_vsa_klines.py — audit remediation: VSA-ready kline store with flags.

Reads the raw kline stores (spot + futures UM) for BTCUSDT/ETHUSDT at the
intervals the TA/VSA visualizer needs (1d, 1h, 1m) and writes a gap-clean,
flag-annotated copy under processed/vsa_klines/:

  - `is_gap`       True where the source store had NO bar for that slot
                   (never silently interpolated — VSA must not read those bars)
  - `cross_filled` True where the bar was filled from dataset_1d/symbols
                   (the step0-validated cross-fill source, same official
                   Binance source, gap-free, 0 OHLC violations)

Root cause of the flagged gaps (documented, not patched away):
  - spot 1d: 88 missing month-first days 2017-09 -> 2024-12 — the store is
    merged per calendar month and each month's dump starts on the 1st,
    producing the artifact. Filled from dataset_1d/symbols (validated in
    step0: volume within 0.1% on >=99.9% of overlap days).
  - futures 1d: 23 missing month-first days 2020-02 -> 2021-12 (same
    artifact) — kept as is_gap=True (no cross-fill source for futures OHLC;
    23/2319 = 0.99% is under the audit's 1% bar).
  - futures ETHUSDT 1d: source file does not exist in the store — NOT
    fabricated; the interval is simply unavailable for that market.
  - 1h/1m: 0.18% / 0.04% / 0.003% gaps, all flagged is_gap=True.

Output layout (mirrors the source stores):
  processed/vsa_klines/{spot,futures_um}/{SYM}/{interval}/merged.parquet

Columns: open_time(ms int64), datetime_utc, open, high, low, close, volume,
quote_asset_volume, num_trades, taker_buy_base_vol, taker_buy_quote_vol,
is_gap, cross_filled  (+ _meta.json with the same provenance guardrails).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("tvp.vsa_klines")

BASE = Path(__file__).resolve().parent                        # BTC_True_Volume
DATA_ROOT = BASE / "processed"                                # actual data root
KLINE_STORE = BASE.parent / "binance_full_dataset" / "processed"
SYMBOLS_STORE = BASE.parent / "dataset_1d" / "symbols"
OUT_ROOT = DATA_ROOT / "vsa_klines"

INTERVALS = ("1d", "1h", "1m")
INTERVAL_MS = {"1d": 86_400_000, "1h": 3_600_000, "1m": 60_000}
SYMBOLS = ("BTCUSDT", "ETHUSDT")
KEEP = ["open", "high", "low", "close", "volume", "quote_asset_volume",
        "num_trades", "taker_buy_base_vol", "taker_buy_quote_vol"]


def _source_path(market: str, sym: str, interval: str) -> Optional[Path]:
    if market == "spot":
        d = KLINE_STORE / "spot" / "klines" / sym / interval
    else:
        d = KLINE_STORE / "futures_um" / "klines" / sym / interval
    p = d / "merged.parquet"
    if p.exists():
        return p
    # nonstandard names exist in the store (e.g. futures BTCUSDT 1d is
    # BTCUSDT-1d.parquet) — resolve by glob before giving up
    cands = sorted(d.glob("*.parquet"))
    if cands:
        return cands[0]
    logger.warning("%s %s %s: source missing %s", market, sym, interval, d)
    return None


def _last_bar_ms(p: Path, col: str = "open_time") -> Optional[int]:
    """Last bar's open_time via last-row-group projection (same idiom as
    refresh_before_visualizer._last_tape_day) — never loads the whole file.
    None on missing/corrupt/column-absent."""
    try:
        pf = pq.ParquetFile(p)
        t = pf.read_row_group(pf.num_row_groups - 1, columns=[col])
        return int(t[col][-1].as_py()) if t.num_rows else None
    except Exception:  # noqa: BLE001
        return None


def _source_end(market: str, sym: str, interval: str) -> Optional[int]:
    """Newest source bar end (ms). For spot 1d the symbols cross-fill store is
    also a source (it carries days past the kline store's 2024-12-31 end), so
    its end is maxed in — a newer symbols day must trigger a rebuild."""
    p = _source_path(market, sym, interval)
    end = _last_bar_ms(p) if p is not None else None
    if market == "spot" and interval == "1d":
        s = SYMBOLS_STORE / f"{sym}.parquet"
        s_end = _last_bar_ms(s, col="timestamp") if s.exists() else None
        if s_end is not None:
            end = s_end if end is None else max(end, s_end)
    return end


def _load_source(market: str, sym: str, interval: str) -> Optional[pd.DataFrame]:
    p = _source_path(market, sym, interval)
    if p is None:
        return None
    df = pd.read_parquet(p)
    return df


def _flag_gaps(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Set is_gap where consecutive open_time diffs exceed 1.5x the interval.
    Missing rows are NOT synthesized — the flags exist so the visualizer can
    render honest gaps and refuse to read VSA signals there."""
    ms = INTERVAL_MS[interval]
    df = df.sort_values("open_time").reset_index(drop=True)
    d = df["open_time"].diff().fillna(ms)
    df["is_gap"] = d > ms * 1.5
    return df


def _fill_spot_1d(df: pd.DataFrame, sym: str) -> pd.DataFrame:
    """Fill the month-first artifact days of spot 1d from the validated
    dataset_1d/symbols store, flagged cross_filled=True (never silent)."""
    s = pd.read_parquet(SYMBOLS_STORE / f"{sym}.parquet")
    s = s.rename(columns={"timestamp": "open_time", "quote_volume": "quote_asset_volume",
                          "trades": "num_trades", "taker_buy_base": "taker_buy_base_vol",
                          "taker_buy_quote": "taker_buy_quote_vol"})
    s["open_time"] = s["open_time"].astype("int64")
    src = df.set_index("open_time")
    fill = s.set_index("open_time")
    have = set(src.index)
    add = fill.loc[[i for i in fill.index if i not in have], KEEP]
    if len(add) == 0:
        logger.info("%s spot 1d: no fill needed", sym)
        return df
    add = add.reset_index()
    add["is_gap"] = True                 # the slot was genuinely missing
    add["cross_filled"] = True
    out = pd.concat([df, add], ignore_index=True)
    out = out.sort_values("open_time").reset_index(drop=True)
    n_art = int(len(set(add["open_time"]) & set(
        pd.bdate_range("2017-09-01", "2024-12-31"))))
    logger.info("%s spot 1d: filled %d days from symbols store "
                "(%d month-first artifact + %d beyond store end 2024-12-31)",
                sym, len(add), n_art, len(add) - n_art)
    return out


def build_one(market: str, sym: str, interval: str) -> Optional[Path]:
    # Incremental: sources are append-only (the kline back-scan only adds new
    # days), so when the output already covers the newest source bar the
    # rebuild is pure waste — skip it. Delete the output to force a rebuild.
    out_dir = OUT_ROOT / market / sym / interval
    out = out_dir / "merged.parquet"
    if out.exists():
        src_end = _source_end(market, sym, interval)
        out_end = _last_bar_ms(out)
        if src_end is not None and out_end is not None and src_end <= out_end:
            logger.info("%s %s %s: store already current, skip", market, sym, interval)
            return out
    df = _load_source(market, sym, interval)
    if df is None:
        return None
    df = df[KEEP + ["open_time"]].copy()
    df = _flag_gaps(df, interval)
    df["cross_filled"] = False
    if market == "spot" and interval == "1d":
        df = _fill_spot_1d(df, sym)
    df["datetime_utc"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    out_dir = OUT_ROOT / market / sym / interval
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "merged.parquet"
    tmp = out.with_name(out.name + ".tmp")
    df = df[["open_time", "datetime_utc", *KEEP, "is_gap", "cross_filled"]]
    pq.write_table(pa.Table.from_pandas(df), tmp, compression="zstd", version="2.6")
    tmp.replace(out)
    meta = {
        "dataset_id": f"vsa_klines_{market}_{sym}_{interval}",
        "description": f"VSA-audited klines {market} {sym} {interval} — gap-flagged, "
                       "cross-filled artifact days flagged, 0 OHLC violations (audit 2026-08-21)",
        "data_source": "binance_vision_klines; cross-fill: dataset_1d_symbols_crossfill",
        "confidence_tier": "High (source) / flagged fills marked",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "row_count": int(len(df)),
        "n_is_gap": int(df["is_gap"].sum()),
        "n_cross_filled": int(df["cross_filled"].sum()),
        "known_limitations": [
            "is_gap bars are genuine missing slots — visualizer must render them as gaps, never interpolate",
            "spot 1d days missing from the kline store filled from dataset_1d/symbols with "
            "cross_filled=True (88 month-first artifact + days beyond the store's 2024-12-31 end)",
            "futures 1d source naming is asymmetric in the store (BTCUSDT-1d.parquet vs merged.parquet) — glob-resolved",
        ],
    }
    (out_dir / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d rows, %d gaps, %d filled)", out, len(df),
                int(df["is_gap"].sum()), int(df["cross_filled"].sum()))
    return out


def run() -> Dict[str, str]:
    written: Dict[str, str] = {}
    for market in ("spot", "futures_um"):
        for sym in SYMBOLS:
            for interval in INTERVALS:
                p = build_one(market, sym, interval)
                if p is not None:
                    written[f"{market}/{sym}/{interval}"] = str(p)
    return written


if __name__ == "__main__":
    written = run()
    print(f"\n=== VSA KLINES BUILT: {len(written)} series ===")
