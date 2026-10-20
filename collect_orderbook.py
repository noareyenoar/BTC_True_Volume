"""Daily order-book depth collectors (no live loop).

Two stores, both written once per daily refresh run and read by the visualizer:

  processed/orderbook_spot/{SYM}/daily/YYYY-MM-DD.parquet
      ONE Binance spot L2 snapshot per UTC day (top 5,000 levels per side,
      bucketed by price).  GitHub-hosted runners are US-based and Binance's
      spot REST API is geo-blocked there, so this collector is skipped under
      GITHUB_ACTIONS; the PC refresh is the producer and the exported artifact
      is carried across publishers through the site repo.

  processed/orderbook_depth/{SYM}/daily/YYYY-MM-DD.parquet
      Binance Vision's daily futures-UM bookDepth zip: ~30-second snapshots of
      CUMULATIVE depth at +/-0.2/1/2/3/4/5% from mid.  Reduced here to the last
      snapshot per 10-minute bin, cumulative values differenced into per-band
      increments, and a mid attached (1m futures close, implied-mid fallback).

Both collectors are best-effort: a source outage logs a warning and returns;
it never fails the refresh chain.  Storage follows common.atomic_write_parquet;
day files are immutable once written (the current day's file is rewritten only
when a second snapshot for the same day is forced).
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import requests

try:
    from .common import (PROCESSED, DS, RateLimitedClient,
                         atomic_write_parquet, save_json_cache, logger)
except ImportError:  # direct script execution
    from common import (PROCESSED, DS, RateLimitedClient,
                        atomic_write_parquet, save_json_cache, logger)

logger = logging.getLogger("tvp.orderbook")

SPOT_BASE = "https://api.binance.com"
SPOT_DEPTH = SPOT_BASE + "/api/v3/depth"
SPOT_LIMIT = 5000
# Bucket widths are per symbol and stored per row: $10 gives ETH only ~21
# buckets across the whole book, so ETH uses $1.
SPOT_BUCKET_USD = {"BTCUSDT": 10.0, "ETHUSDT": 1.0}
SPOT_RETAIN_DAYS = 400

VISION_BOOKDEPTH = ("https://data.binance.vision/data/futures/um/daily/bookDepth/"
                    "{sym}/{sym}-bookDepth-{day}.zip")
DEPTH_BIN_MS = 10 * 60 * 1000
DEPTH_RETAIN_DAYS = 120
DEFAULT_BACKFILL_DAYS = 90
VISION_FIRST_DAY = date(2023, 1, 1)
# Older Vision files carry only +/-1,2,3,4,5; 2026 files add +/-0.2.
DEPTH_BANDS = (0.2, 1.0, 2.0, 3.0, 4.0, 5.0)

PUBLISHER = "gh" if os.environ.get("GITHUB_ACTIONS") else "pc"

SPOT_ROOT = PROCESSED / "orderbook_spot"
DEPTH_ROOT = PROCESSED / "orderbook_depth"


# ---------------------------------------------------------------------------
# Spot snapshot
# ---------------------------------------------------------------------------

def _bucket_levels(levels: List[List[str]], width: float) -> Dict[float, List[float]]:
    """[(price, qty), ...] -> {bucket_lower_edge: [qty, notional, n_levels]}."""
    agg: Dict[float, List[float]] = {}
    for price_s, qty_s in levels:
        try:
            price, qty = float(price_s), float(qty_s)
        except (TypeError, ValueError):
            continue
        if not (price > 0 and qty > 0):
            continue
        bucket = float(np.floor(price / width) * width)
        rec = agg.setdefault(bucket, [0.0, 0.0, 0])
        rec[0] += qty
        rec[1] += price * qty
        rec[2] += 1
    return agg


def collect_spot_once(symbols: Optional[List[str]] = None,
                      force: bool = False,
                      client: Optional[RateLimitedClient] = None) -> List[Dict[str, Any]]:
    """One L2 snapshot per UTC day, PC only.  Returns one summary dict/symbol."""
    if os.environ.get("GITHUB_ACTIONS"):
        logger.info("orderbook spot: skipped under GITHUB_ACTIONS "
                    "(Binance spot REST is geo-blocked on US runners)")
        return []
    symbols = list(symbols or SPOT_BUCKET_USD)
    client = client or RateLimitedClient(min_interval=1.0)
    today = datetime.now(timezone.utc).date()
    out: List[Dict[str, Any]] = []
    for sym in symbols:
        width = float(SPOT_BUCKET_USD.get(sym, 10.0))
        day_dir = SPOT_ROOT / sym / "daily"
        day_path = day_dir / f"{today.isoformat()}.parquet"
        if day_path.exists() and not force:
            logger.info("orderbook spot %s: already captured %s", sym, today)
            out.append({"symbol": sym, "status": "already", "day": str(today)})
            continue
        try:
            resp = client.get(SPOT_DEPTH, params={"symbol": sym, "limit": SPOT_LIMIT})
            if resp is None or resp.status_code != 200:
                code = getattr(resp, "status_code", None)
                logger.warning("orderbook spot %s: HTTP %s (geo-blocked or "
                               "unavailable) — skipped", sym, code)
                out.append({"symbol": sym, "status": "http", "code": code})
                continue
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — never fail the refresh
            logger.warning("orderbook spot %s failed: %s", sym, exc)
            out.append({"symbol": sym, "status": "error", "error": str(exc)})
            continue
        bids = _bucket_levels(payload.get("bids") or [], width)
        asks = _bucket_levels(payload.get("asks") or [], width)
        if not bids or not asks:
            logger.warning("orderbook spot %s: empty book — skipped", sym)
            out.append({"symbol": sym, "status": "empty"})
            continue
        # Use the true best levels (bucket floors can tie across a side).
        best_bid = max(float(p) for p, _ in payload["bids"])
        best_ask = min(float(p) for p, _ in payload["asks"])
        mid = (best_bid + best_ask) / 2.0
        ts = int(time.time() * 1000)
        rows = []
        for side, agg in (("bid", bids), ("ask", asks)):
            for bucket, (qty, notional, n) in agg.items():
                rows.append({
                    "symbol": sym, "snapshot_ts": ts, "day": today.isoformat(),
                    "side": side, "price_bucket": bucket, "bucket_usd": width,
                    "qty_base": qty, "notional_usd": notional, "n_levels": n,
                    "mid": mid, "best_bid": best_bid, "best_ask": best_ask,
                    "last_update_id": int(payload.get("lastUpdateId") or 0),
                    "source": "binance_spot_rest_depth",
                })
        df = pd.DataFrame(rows)
        # A forced second snapshot the same day replaces the day's file.
        atomic_write_parquet(df, day_path, {
            "data_source": DS["binance_spot_depth"],
            "confidence_tier": "Medium",
            "dataset_id": "binance_spot_l2_daily_snapshot",
        })
        save_json_cache(SPOT_ROOT / sym / "_meta.json", {
            "dataset_id": "binance_spot_l2_daily_snapshot",
            "data_source": DS["binance_spot_depth"],
            "confidence_tier": "Medium",
            "bucket_usd": width,
            "levels_per_side": SPOT_LIMIT,
            "coverage_pct": {"bid": round((best_bid / mid - 1) * 100, 3),
                             "ask": round((best_ask / mid - 1) * 100, 3)},
            "last_snapshot_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "limitations": ["one snapshot per daily PC run; not continuous",
                            "top 5,000 levels/side: BTC ~+/-1%, ETH ~+/-4%",
                            "Binance spot only"],
        })
        logger.info("orderbook spot %s: %d buckets, best %.2f/%.2f, mid %.2f",
                    sym, len(df), best_bid, best_ask, mid)
        out.append({"symbol": sym, "status": "ok", "rows": int(len(df)),
                    "buckets": int(df["price_bucket"].nunique()), "mid": mid})
    _prune_days(SPOT_ROOT, SPOT_RETAIN_DAYS)
    return out


# ---------------------------------------------------------------------------
# Vision futures bookDepth
# ---------------------------------------------------------------------------

def _download_bytes(url: str, timeout: int = 60) -> Optional[bytes]:
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            logger.warning("download %s (attempt %d): %s", url, attempt + 1, exc)
            time.sleep(3 * (attempt + 1))
    return None


def _mid_lookup(sym: str, start_ms: int, end_ms: int) -> Optional[pd.DataFrame]:
    """Futures 1m close (open_time, close) — cheap column-projected read."""
    p = (PROCESSED / "vsa_klines" / "futures_um" / sym / "1m" / "merged.parquet")
    if not p.exists():
        return None
    try:
        import pyarrow.parquet as pq
        k = pq.read_table(p, columns=["open_time", "close"]).to_pandas()
    except Exception as exc:  # noqa: BLE001
        logger.warning("orderbook depth: 1m kline read failed: %s", exc)
        return None
    k = k[(k["open_time"] >= start_ms) & (k["open_time"] <= end_ms)]
    if k.empty:
        return None
    return k.sort_values("open_time").reset_index(drop=True)


def _parse_depth_day(raw: bytes, sym: str, day: date,
                     klines: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        df = pd.read_csv(io.BytesIO(zf.read(name)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("bookDepth %s %s: parse failed: %s", sym, day, exc)
        return None
    need = {"timestamp", "percentage", "depth", "notional"}
    if not need.issubset(df.columns):
        logger.warning("bookDepth %s %s: unexpected columns %s", sym, day,
                       list(df.columns))
        return None
    df = df[df["percentage"].astype(float) != 0].copy()
    if df.empty:
        return None
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
    # pandas may parse second-resolution strings into datetime64[s]; scaling
    # by the dtype unit is the only safe way to get milliseconds.
    unit = getattr(df["ts"].dtype, "unit", "ns")
    scale = {"s": 1000, "ms": 1, "us": 0.001, "ns": 0.000001}[unit]
    df["ts_ms"] = (df["ts"].astype("int64") * scale).astype("int64")
    df["percentage"] = df["percentage"].astype(float)
    df["pct_abs"] = df["percentage"].abs()
    df["side"] = np.where(df["percentage"] < 0, "bid", "ask")
    df["cum_depth_base"] = pd.to_numeric(df["depth"], errors="coerce")
    df["cum_notional_usd"] = pd.to_numeric(df["notional"], errors="coerce")
    df = df.dropna(subset=["cum_depth_base", "cum_notional_usd"])

    # Keep the LAST source snapshot in each 10-minute bin.
    df["bin_ms"] = (df["ts_ms"] // DEPTH_BIN_MS) * DEPTH_BIN_MS
    last_ts = df.groupby(["bin_ms"])["ts_ms"].max()
    df = df.merge(last_ts.rename("keep_ts"), left_on="bin_ms",
                  right_index=True, how="left")
    df = df[df["ts_ms"] == df["keep_ts"]].drop(columns=["keep_ts"])
    df = df.sort_values(["ts_ms", "side", "pct_abs"])

    # Cumulative -> per-band increments.  A missing intermediate band simply
    # widens the difference across the gap.
    df["band_depth"] = (df.groupby(["ts_ms", "side"])["cum_depth_base"]
                          .diff().fillna(df["cum_depth_base"]))
    df["band_notional"] = (df.groupby(["ts_ms", "side"])["cum_notional_usd"]
                             .diff().fillna(df["cum_notional_usd"]))
    # Monotonicity guard: a non-cumulative source row would show as a negative
    # increment; drop that snapshot rather than paint a lie.
    bad = df[df["band_depth"] < -1e-6]
    if len(bad):
        bad_ts = set(bad["ts_ms"].unique())
        logger.warning("bookDepth %s %s: %d non-monotonic snapshots dropped",
                       sym, day, len(bad_ts))
        df = df[~df["ts_ms"].isin(bad_ts)]
    df = df[(df["band_depth"] > 0) & (df["band_notional"] > 0)]
    if df.empty:
        return None

    # Mid: 1m close is the primary (sub-minute accurate); the implied mid from
    # the innermost cumulative band is the fallback and a cross-check.  The
    # innermost row per (snapshot, side) is vectorized with drop_duplicates.
    inner = (df.sort_values(["ts_ms", "side", "pct_abs"])
               .drop_duplicates(["ts_ms", "side"], keep="first"))
    inner = inner[(inner["cum_depth_base"] > 0) & (inner["cum_notional_usd"] > 0)]
    if inner.empty:
        return None
    sign = np.where(inner["side"] == "bid", -1.0, 1.0)
    inner = inner.assign(est=inner["cum_notional_usd"] / inner["cum_depth_base"]
                              / (1.0 + sign * inner["pct_abs"] / 200.0))
    snap = (inner.groupby("ts_ms")["est"].mean()
                 .rename("mid_implied").reset_index())
    if klines is not None and len(klines):
        s = snap.sort_values("ts_ms")
        k = klines.rename(columns={"open_time": "ts_ms", "close": "mid_kline"})
        s = pd.merge_asof(s, k.sort_values("ts_ms"), on="ts_ms",
                          direction="backward")
        s["mid"] = s["mid_kline"].where(s["mid_kline"].notna(), s["mid_implied"])
        both = s.dropna(subset=["mid_kline", "mid_implied"])
        if len(both):
            error_bps = ((both["mid_kline"] - both["mid_implied"]).abs()
                         / both["mid_kline"] * 1e4).median()
            logger.info("bookDepth %s %s: implied-vs-1m mid median %.1f bps",
                        sym, day, error_bps)
    else:
        s = snap.assign(mid=snap["mid_implied"])
    s = s.dropna(subset=["mid"])[["ts_ms", "mid"]]
    df = df.merge(s, on="ts_ms", how="inner")
    if df.empty:
        return None

    out = pd.DataFrame({
        "symbol": sym,
        "day": day.isoformat(),
        "bin_ms": df["bin_ms"].astype("int64"),
        "snapshot_ts": df["ts_ms"].astype("int64"),
        "percentage": df["percentage"],
        "pct_abs": df["pct_abs"],
        "side": df["side"],
        "cum_depth_base": df["cum_depth_base"],
        "cum_notional_usd": df["cum_notional_usd"],
        "band_depth": df["band_depth"],
        "band_notional": df["band_notional"],
        "mid": df["mid"].astype(float),
        "source": "binance_vision_futures_um_bookDepth",
    })
    out = out.drop_duplicates(["snapshot_ts", "side", "percentage"], keep="last")
    return out.reset_index(drop=True)


def collect_vision_depth(symbols: Optional[List[str]] = None,
                         latest: Optional[date] = None,
                         backfill_days: int = DEFAULT_BACKFILL_DAYS,
                         workers: int = 4,
                         budget_min: float = 10.0,
                         force: bool = False) -> List[Dict[str, Any]]:
    """Download missing daily bookDepth zips and write one parquet per day."""
    symbols = list(symbols or ("BTCUSDT", "ETHUSDT"))
    today = datetime.now(timezone.utc).date()
    end = latest or (today - timedelta(days=1))
    start = max(VISION_FIRST_DAY, today - timedelta(days=max(1, backfill_days)))
    deadline = time.time() + max(1.0, budget_min) * 60
    results: List[Dict[str, Any]] = []

    for sym in symbols:
        day_dir = DEPTH_ROOT / sym / "daily"
        day_dir.mkdir(parents=True, exist_ok=True)
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        missing = [d for d in days
                   if force or not (day_dir / f"{d.isoformat()}.parquet").exists()]
        if not missing:
            results.append({"symbol": sym, "status": "current",
                            "days": len(days)})
            continue
        logger.info("orderbook depth %s: fetching %d day(s), %s .. %s",
                    sym, len(missing), missing[0], missing[-1])
        start_ms = int(datetime.combine(start, datetime.min.time(),
                                        tzinfo=timezone.utc).timestamp() * 1000)
        end_ms = int(datetime.combine(end, datetime.max.time(),
                                      tzinfo=timezone.utc).timestamp() * 1000)
        klines = _mid_lookup(sym, start_ms, end_ms)
        ok = holes = 0

        def fetch(d: date) -> Optional[bytes]:
            return _download_bytes(VISION_BOOKDEPTH.format(sym=sym,
                                                           day=d.isoformat()))

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(fetch, d): d for d in missing}
            for fut in as_completed(futures):
                d = futures[fut]
                if time.time() > deadline:
                    logger.warning("orderbook depth %s: budget reached, "
                                   "stopping (re-run to continue)", sym)
                    for f2 in futures:
                        f2.cancel()
                    break
                raw = fut.result()
                if raw is None:
                    holes += 1
                    logger.info("bookDepth %s %s: not published (404)", sym, d)
                    continue
                frame = _parse_depth_day(raw, sym, d, klines)
                if frame is None or frame.empty:
                    holes += 1
                    continue
                atomic_write_parquet(frame, day_dir / f"{d.isoformat()}.parquet", {
                    "data_source": DS["binance_vision_bookdepth"],
                    "confidence_tier": "Medium",
                    "dataset_id": "binance_vision_bookdepth_10m",
                })
                ok += 1
        save_json_cache(DEPTH_ROOT / sym / "_meta.json", {
            "dataset_id": "binance_vision_bookdepth_10m",
            "data_source": DS["binance_vision_bookdepth"],
            "confidence_tier": "Medium",
            "source_url": VISION_BOOKDEPTH.format(sym=sym, day="YYYY-MM-DD"),
            "bands_pct": list(DEPTH_BANDS),
            "bin_ms": DEPTH_BIN_MS,
            "days_written": ok,
            "holes": holes,
            "last_run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mid_method": "1m futures close, implied-mid fallback",
            "limitations": ["Binance USD-M futures only",
                            "distance bands, not exact price levels",
                            "last 30s source snapshot per 10-minute bin"],
        })
        logger.info("orderbook depth %s: %d day(s) written, %d hole(s)",
                    sym, ok, holes)
        results.append({"symbol": sym, "status": "ok", "written": ok,
                        "holes": holes, "days": len(days)})
    _prune_days(DEPTH_ROOT, DEPTH_RETAIN_DAYS)
    return results


def _prune_days(root: Path, retain_days: int) -> None:
    """Delete day files older than retain_days (best-effort)."""
    if not root.exists():
        return
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=retain_days)
    for f in root.glob("*/daily/*.parquet"):
        try:
            d = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if d < cutoff:
            try:
                f.unlink()
            except OSError as exc:
                logger.warning("orderbook: could not prune %s: %s", f, exc)


def run(latest: Optional[date] = None,
        symbols: Optional[List[str]] = None,
        backfill_days: int = DEFAULT_BACKFILL_DAYS,
        skip_spot: bool = False,
        skip_depth: bool = False,
        force: bool = False,
        budget_min: float = 10.0) -> Dict[str, Any]:
    """Entry point for refresh_before_visualizer (never raises)."""
    out: Dict[str, Any] = {"spot": [], "depth": []}
    started = time.time()
    if not skip_spot:
        try:
            out["spot"] = collect_spot_once(symbols=symbols, force=force)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orderbook spot collection failed (non-fatal): %s", exc)
            out["spot"] = [{"status": "error", "error": str(exc)}]
    if not skip_depth:
        try:
            out["depth"] = collect_vision_depth(
                symbols=symbols, latest=latest, backfill_days=backfill_days,
                force=force, budget_min=budget_min)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orderbook depth collection failed (non-fatal): %s", exc)
            out["depth"] = [{"status": "error", "error": str(exc)}]
    out["elapsed_s"] = round(time.time() - started, 1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily order-book depth collectors")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--backfill-days", type=int, default=DEFAULT_BACKFILL_DAYS)
    ap.add_argument("--skip-spot", action="store_true")
    ap.add_argument("--skip-depth", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--budget-min", type=float, default=10.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s")
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    res = run(symbols=symbols, backfill_days=args.backfill_days,
              skip_spot=args.skip_spot, skip_depth=args.skip_depth,
              force=args.force, budget_min=args.budget_min)
    print(res)


if __name__ == "__main__":
    main()
