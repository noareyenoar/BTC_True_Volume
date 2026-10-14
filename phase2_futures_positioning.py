"""
phase2_futures_positioning.py — Phase 2: derivatives positioning backfill.

Binance Vision daily metrics ZIPs (free, no auth):

    https://data.binance.vision/data/futures/um/daily/metrics/{SYM}/{SYM}-metrics-YYYY-MM-DD.zip

Fields (verified): create_time, symbol, sum_open_interest, sum_open_interest_value,
count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
count_long_short_ratio, sum_taker_long_short_vol_ratio

Earliest published file: 2021-01-01 (2020 returns 404; a binary probe pins the
true earliest). Range: earliest -> last complete day.

Outputs:
  - raw ZIPs cached under raw/binance_metrics/{SYM}/{YYYYMM}/  (resume-safe)
  - minute-granularity merged store: binance_full_dataset/processed/futures_um/metrics/{SYM}/merged.parquet
  - daily rollup: processed/futures/positioning/{SYM}/merged.parquet with the
    plan-literal top-trader derivations:
        ratio      = daily mean of (sum_toptrader_ls / count_toptrader_ls)
        long_pct   = ratio / (ratio + 1)
        short_pct  = 1 / (ratio + 1)
        skew       = long_pct - 0.5

Top-trader ratios are the top-20%-accounts cohort — a SENTIMENT PROXY, not
exchange-wide OI (which nets to zero by construction). If the backfill fails
entirely, falls back to the 1-month dataset_1d/lake window (tier Low).
"""

from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

try:
    from .common import (KLINE_STORE, LAKE_DIR, PROCESSED, RAW, DS,
                         PhaseResult, append_log, logger, write_parquet_with_meta,
                         load_json_cache, save_json_cache)
except ImportError:  # direct script execution
    from common import (KLINE_STORE, LAKE_DIR, PROCESSED, RAW, DS,
                        PhaseResult, append_log, logger, write_parquet_with_meta,
                        load_json_cache, save_json_cache)

logger = logging.getLogger("tvp.phase2")

METRICS_BASE = "https://data.binance.vision/data/futures/um/daily/metrics"
EXPECTED_COLS = ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
                 "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
                 "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
MAX_RETRIES = 4
PROBE_DATES = ["2020-06-01", "2020-10-01", "2020-12-01"]

# Persistent known-holes cache: days confirmed missing on Binance Vision
# (404s) plus the cached BTC earliest-probe result. Consulted only for days
# <= last_scanned_end ("re-probe only when end extends"); the zip-cache check
# in fetch_metrics stays first so a cached day always wins over a hole entry.
HOLES_CACHE_PATH = RAW / "vision_metrics_holes.json"
# Publish-lag window: Vision may be late for the newest days, so holes inside
# the last TRAILING_DAYS of `end` are re-probed every run (and once published,
# fetched + removed from the hole set) instead of being skipped via the cache.
TRAILING_DAYS = 3


def _load_holes_cache() -> dict:
    cache = load_json_cache(HOLES_CACHE_PATH)
    return cache if isinstance(cache, dict) and cache.get("version") == 1 else {}


def _save_holes_cache(cache: dict) -> None:
    save_json_cache(HOLES_CACHE_PATH, {"version": 1, **cache})


def _download_zip(url: str) -> bytes:
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, timeout=120)
        except requests.RequestException as exc:
            logger.warning("req error %s (attempt %d): %s", url, attempt + 1, exc)
        else:
            if r.status_code == 200:
                return r.content
            if r.status_code == 404:
                raise FileNotFoundError(f"not found: {url}")
            logger.warning("HTTP %s on %s (attempt %d)", r.status_code, url, attempt + 1)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"gave up downloading {url}")


def probe_earliest() -> str:
    """Binary-probe for the earliest published metrics day (2020-06 .. 2021-01)."""
    lo, hi = "2020-06-01", "2021-01-01"  # 2021-01-01 verified present
    best = hi
    for probe in PROBE_DATES:
        url = f"{METRICS_BASE}/BTCUSDT/BTCUSDT-metrics-{probe}.zip"
        try:
            _download_zip(url)
            best = probe
        except FileNotFoundError:
            pass
    # refine around the best probe
    d = datetime.strptime(best, "%Y-%m-%d").date()
    for _ in range(8):
        d2 = d - timedelta(days=1)
        try:
            _download_zip(f"{METRICS_BASE}/BTCUSDT/BTCUSDT-metrics-{d2}.zip")
            d = d2
        except FileNotFoundError:
            break
    return d.strftime("%Y-%m-%d")


def fetch_metrics(sym: str, start: str, end: str, hole_set: Set[str],
                  last_scanned_end: str, force_probe: bool = False) -> Tuple[List[Path], List[str]]:
    """Download daily metrics ZIPs (cached under raw/), return (sorted paths,
    recovered). `hole_set` is MUTATED in place: new 404s are added,
    re-fetched cached holes are removed. Days in hole_set are skipped (no
    probe, no log) unless force_probe — but ONLY up to last_scanned_end;
    newer days are always probed. The zip-cache check stays first, so a
    cached day always wins over a hole entry."""
    out: List[Path] = []
    recovered: List[str] = []
    skipped = 0
    d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    trail = end_d - timedelta(days=TRAILING_DAYS)
    total = (end_d - d).days + 1
    done, holes = 0, []
    t0 = time.time()
    while d <= end_d:
        ds = d.strftime("%Y-%m-%d")
        month = d.strftime("%Y-%m")
        chunk = RAW / "binance_metrics" / sym / month / f"{ds.replace('-', '')}.zip"
        if chunk.exists():
            out.append(chunk)
            done += 1
            d += timedelta(days=1)
            continue
        if ds in hole_set and not force_probe and ds <= last_scanned_end \
                and d < trail:
            # older than the publish-lag window AND inside last_scanned_end:
            # genuinely missing day — silent skip (cached hole)
            skipped += 1
            done += 1
            d += timedelta(days=1)
            continue
        chunk.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = _download_zip(f"{METRICS_BASE}/{sym}/{sym}-metrics-{ds}.zip")
            chunk.write_bytes(raw)
            out.append(chunk)
            if ds in hole_set:                 # previously-missing day now published
                hole_set.discard(ds)
                recovered.append(ds)
        except FileNotFoundError:
            hole_set.add(ds)
            holes.append(ds)
            if d >= trail:
                logger.info("%s %s: metrics not yet published (trailing window; "
                            "will re-probe next run)", sym, ds)
            else:
                logger.warning("%s %s: no published file — hole", sym, ds)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{sym} metrics fetch failed at {ds}: {exc}") from exc
        done += 1
        if done % 200 == 0 or done == total:
            el = time.time() - t0
            rate = done / el if el else 0
            eta = (total - done) / rate / 3600 if rate else float("nan")
            logger.info("%s: %d/%d days (%.1f days/min, ETA %.2fh, %d holes)",
                        sym, done, total, rate * 60, eta, len(holes))
        d += timedelta(days=1)
    if skipped:
        logger.info("%s: %d days skipped (cached holes)", sym, skipped)
    if holes:
        logger.warning("%s: %d holes: %s", sym, len(holes), holes[:8])
    return sorted(out), recovered


def _read_zip_day(path: Path) -> pd.DataFrame:
    z = zipfile.ZipFile(path)
    name = next(n for n in z.namelist() if n.endswith(".csv"))
    df = pd.read_csv(io.BytesIO(z.read(name)))
    if list(df.columns) != EXPECTED_COLS:
        raise RuntimeError(f"{path.name}: schema mismatch {list(df.columns)} != {EXPECTED_COLS}")
    df["create_time"] = pd.to_datetime(df["create_time"], utc=True)
    return df


def merge_metrics(sym: str, zip_paths: List[Path]) -> Path:
    """Stream daily CSVs into the minute-granularity merged store.

    Written to a .tmp path and atomically renamed so a crash mid-merge never
    leaves a corrupt final file that would poison a resume.
    """
    merged_path = KLINE_STORE / "futures_um" / "metrics" / sym / "merged.parquet"
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = merged_path.with_name(merged_path.name + ".tmp")
    first = _read_zip_day(zip_paths[0])
    schema = pa.Table.from_pandas(first, preserve_index=False).schema
    total = len(first)
    with pq.ParquetWriter(tmp, schema, compression="zstd", version="2.6") as writer:
        writer.write_table(pa.Table.from_pandas(first, preserve_index=False))
        for p in zip_paths[1:]:
            df = _read_zip_day(p)
            total += len(df)
            writer.write_table(pa.Table.from_pandas(df, preserve_index=False))
    os.replace(tmp, merged_path)
    logger.info("%s: merged %d rows -> %s (%.1f MB)", sym, total, merged_path,
                merged_path.stat().st_size / 1e6)
    return merged_path


def daily_rollup(merged_path: Path) -> pd.DataFrame:
    """Minute rows -> daily positioning block with plan-literal skew math."""
    df = pq.read_table(merged_path).to_pandas()
    df["date"] = df["create_time"].dt.normalize()
    ratio = df["sum_toptrader_long_short_ratio"] / df["count_toptrader_long_short_ratio"].replace(0, np.nan)
    taker = df["sum_taker_long_short_vol_ratio"] / df["count_long_short_ratio"].replace(0, np.nan)
    daily = pd.DataFrame({
        "date": df["date"],
        "pos_oi_usd": df["sum_open_interest_value"],
        "pos_top_trader_ls_ratio": ratio,
        "pos_taker_ls_vol_ratio": taker,
    })
    daily = daily.groupby("date", observed=True).mean()
    daily["pos_top_trader_long_pct"] = daily["pos_top_trader_ls_ratio"] / (daily["pos_top_trader_ls_ratio"] + 1)
    daily["pos_top_trader_short_pct"] = 1 / (daily["pos_top_trader_ls_ratio"] + 1)
    daily["pos_top_trader_skew"] = daily["pos_top_trader_long_pct"] - 0.5
    return daily


def _lake_fallback(sym: str) -> Tuple[pd.DataFrame, str]:
    """1-month lake window fallback (only if the backfill failed entirely)."""
    ls = pd.read_parquet(LAKE_DIR / "binance_top_long_short.parquet")
    tv = pd.read_parquet(LAKE_DIR / "binance_taker_volume.parquet")
    ls = ls[ls["symbol"] == sym].copy()
    tv = tv[tv["symbol"] == sym].copy()
    ls["date"] = pd.to_datetime(ls["timestamp"], utc=True).dt.normalize()
    tv["date"] = pd.to_datetime(tv["timestamp"], utc=True).dt.normalize()
    daily = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))
    daily["pos_top_trader_ls_ratio"] = ls.groupby("date")["longShortRatio"].mean()
    daily["pos_taker_ls_vol_ratio"] = tv.groupby("date")["buySellRatio"].mean()
    daily["pos_oi_usd"] = np.nan
    daily["pos_top_trader_long_pct"] = daily["pos_top_trader_ls_ratio"] / (daily["pos_top_trader_ls_ratio"] + 1)
    daily["pos_top_trader_short_pct"] = 1 / (daily["pos_top_trader_ls_ratio"] + 1)
    daily["pos_top_trader_skew"] = daily["pos_top_trader_long_pct"] - 0.5
    daily.index.name = "date"
    return daily, "FALLBACK: 1-month lake window only (2026-05-18 -> 06-18)"


def run(force_refetch: bool = False, force_probe: bool = False) -> PhaseResult:
    # NOTE: force_refetch is historical/dead — nothing in run() honors it
    # (kept for run_pipeline.py keyword compat). force_probe is the live
    # trigger: re-probes the earliest published day and re-404s every cached
    # hole.
    t0 = time.time()
    end = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    results: List[PhaseResult] = []
    holes_cache = _load_holes_cache()
    earliest = holes_cache.get("earliest")
    if earliest is None or force_probe:
        earliest = probe_earliest()
        holes_cache["earliest"] = earliest
        holes_cache["earliest_verified_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    else:
        logger.info("earliest published metrics day (cached): %s", earliest)
    last_scanned_end = holes_cache.get("last_scanned_end", "")   # "" -> no holes consultable
    holes_by_sym = holes_cache.setdefault("holes", {})
    scan_complete = True
    for sym in ("BTCUSDT", "ETHUSDT"):
        hole_set = set(holes_by_sym.get(sym, []))
        try:
            zips, recovered = fetch_metrics(sym, earliest, end, hole_set=hole_set,
                                            last_scanned_end=last_scanned_end,
                                            force_probe=force_probe)
            if recovered:
                logger.info("%s: %d previously-missing days now published — fetched",
                            sym, len(recovered))
            holes_by_sym[sym] = sorted(hole_set)
            if not zips:
                raise RuntimeError(f"{sym}: no metrics ZIPs downloaded")
            merged = merge_metrics(sym, zips)
            daily = daily_rollup(merged)
            daily = daily.reset_index()
            daily["date"] = daily["date"].dt.normalize()
            out_path = PROCESSED / "futures" / "positioning" / sym / "merged.parquet"
            asset = "BTC" if sym == "BTCUSDT" else "ETH"
            meta = {
                "dataset_id": f"futures_positioning_{sym}_1d",
                "description": f"{sym} daily futures positioning — OI + top-trader L/S skew (Binance Vision metrics)",
                "data_source": DS["binance_vision_metrics"],
                "confidence_tier": "Medium",
                "coverage": [str(daily["date"].min().date()), str(daily["date"].max().date())],
                "earliest_published": earliest,
                "source_merged": str(merged),
                "known_limitations": [
                    "top-trader ratio is the top-20%-accounts cohort — a sentiment proxy, "
                    "NOT exchange-wide long/short OI (which nets to zero by construction)",
                    "metrics backfill starts at the earliest published day (2021-01-01-ish); "
                    "no earlier history exists on Binance Vision",
                ],
            }
            write_parquet_with_meta(daily, out_path, meta, DS["binance_vision_metrics"], "Medium")
            results.append(PhaseResult(
                phase="Phase 2", name="Derivatives positioning (OI + top-trader skew)", asset=asset,
                status="COMPLETE",
                output=f"{out_path} ({len(daily):,} rows)",
                coverage=f"{daily['date'].min().date()} -> {daily['date'].max().date()}, {len(daily):,} days",
                confidence="Medium as a futures-context sentiment proxy (top-20% cohort, not exchange-wide)",
                limitations=[
                    "top-trader cohort proxy, not exchange-wide OI",
                    "history starts at earliest published metrics day (2021-01-01)",
                ],
                outputs=[out_path]))
        except Exception as exc:  # noqa: BLE001
            scan_complete = False
            logger.error("%s backfill failed: %s — trying lake fallback", sym, exc)
            lake, note = _lake_fallback(sym)
            out_path = PROCESSED / "futures" / "positioning" / sym / "merged.parquet"
            asset = "BTC" if sym == "BTCUSDT" else "ETH"
            meta = {"dataset_id": f"futures_positioning_{sym}_1d", "data_source": DS["binance_rest_top_trader"],
                    "confidence_tier": "Low", "coverage": [str(lake.index.min().date()), str(lake.index.max().date())],
                    "known_limitations": ["1-month lake window only — Binance API serves <=30 days", note]}
            write_parquet_with_meta(lake.reset_index(), out_path, meta, DS["binance_rest_top_trader"], "Low")
            results.append(PhaseResult(
                phase="Phase 2", name="Derivatives positioning (lake fallback)", asset=asset,
                status="COMPLETE",
                output=f"{out_path} ({len(lake):,} rows)",
                coverage=f"{lake.index.min().date()} -> {lake.index.max().date()}, {len(lake):,} days",
                confidence="Low as a sentiment proxy (1-month window)",
                limitations=[note, "backfill error: " + str(exc)[:200]],
                outputs=[out_path]))
    if scan_complete:
        holes_cache["last_scanned_end"] = end   # trust holes only after a full scan
    _save_holes_cache(holes_cache)
    if not results:
        raise RuntimeError("phase2: no results")
    return results[0] if len(results) == 1 else PhaseResult(
        phase="Phase 2", name="Derivatives positioning (BTC+ETH)", asset="BOTH",
        status="COMPLETE",
        output=", ".join(r.output for r in results),
        coverage="; ".join(r.coverage for r in results),
        confidence="Medium (BTC/ETH futures metrics backfill)",
        limitations=[l for r in results for l in r.limitations],
        outputs=[p for r in results for p in r.outputs])


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Phase 2: futures positioning backfill")
    ap.add_argument("--force-refetch", action="store_true")
    ap.add_argument("--force-probe", action="store_true",
                    help="re-probe the earliest published metrics day and cached 404 holes")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    res = run(force_refetch=args.force_refetch, force_probe=args.force_probe)
    append_log(res.phase, res.name, res.asset, res.status, res.output,
               res.coverage, res.confidence, res.limitations)
    print("\n=== PHASE 2 DONE ===")


if __name__ == "__main__":
    main()
