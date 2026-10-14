"""refresh_before_visualizer.py — bring every visualizer feed to the latest
Binance Vision-published day, then (re)launch the TA+VSA visualizer.

One command, run every time before starting TA work (from project root):

    .venv\\Scripts\\python.exe BTC_True_Volume\\refresh_before_visualizer.py

What it refreshes (all free + keyless, resume-safe — safe to re-run):
  1. futures UM klines 1m/1h/1d (BTCUSDT + ETHUSDT) — Binance Vision daily
     ZIPs appended into the existing per-interval merged.parquet stores
  2. aggTrades tape -> extension/ dirs (BTCUSDT gets its own extension dir,
     mirroring the ETHUSDT resume pattern; no 3.39B-row rewrite)
  3. step0 rollup cache — new tape days appended (no 30-60 min rescan)
  4. processed/exchange_true_volume — step0 rebuild (triple-gate re-run,
     disputed days auto-adjudicated vs Binance Vision official)
  5. processed/vsa_klines — audit store rebuilt from the extended sources
  6. processed/large_trades — new days only; history upserted, never clobbered
  7. on-chain + positioning — phase1_btc / phase1_eth / phase5 (cheap full
     refetches) and phase2 (per-day resume-safe, fetches only missing ZIPs)
  8. processed/true_volume/{SYM}/merged.parquet — build_final, the file the
     visualizer's daily frame reads
  9. uvicorn restart on 127.0.0.1:8000 (clears the lru_cache, serves the
     fresh data; --no-launch skips this)

"Latest available" is probed against data.binance.vision (today's daily ZIP,
falling back a day at a time) — Vision publishes with ~1 day lag.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import subprocess
import sys
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

try:
    from .common import AGGTRADES_DIR, KLINE_STORE, PROCESSED, logger
    from .exchange_true_volume import (
        AGG_CSV_COLS, AGGT_COLS, VISION_AGGT_BASE,
        build_exchange_true_volume,
    )
    from . import build_vsa_klines, compute_large_trades, build_final
    from . import build_volume_profile
    from . import fetch_checkonchain_urpd
    from . import phase1_btc_network_metrics as p1_btc
    from . import phase1_eth_network_metrics as p1_eth
    from . import phase2_futures_positioning as p2
    from . import phase5_realized_cap as p5
    from . import exchange_true_volume as etv
    from . import common as _common
except ImportError:  # direct script execution (python BTC_True_Volume/refresh_before_visualizer.py)
    from common import AGGTRADES_DIR, KLINE_STORE, PROCESSED, logger
    from exchange_true_volume import (
        AGG_CSV_COLS, AGGT_COLS, VISION_AGGT_BASE,
        build_exchange_true_volume,
    )
    import build_vsa_klines, compute_large_trades, build_final
    import build_volume_profile
    import fetch_checkonchain_urpd
    import phase1_btc_network_metrics as p1_btc
    import phase1_eth_network_metrics as p1_eth
    import phase2_futures_positioning as p2
    import phase5_realized_cap as p5
    import exchange_true_volume as etv
    import common as _common

BASE = Path(__file__).resolve().parent                       # BTC_True_Volume

# The pipeline's processed tree is BTC_True_Volume/processed — the tree every
# phase writes and the visualizer reads. common.PROCESSED resolves there now;
# pin defensively anyway so a future drift can never silently split-brain
# (that drift happened 2026-08-21 and sent a 7-day rollup cache to the wrong
# tree — the app would have kept serving stale data forever).
REAL_PROCESSED = BASE / "processed"
if str(etv.ROLLUP_CACHE.resolve()).lower() == str((REAL_PROCESSED / "rollup_cache").resolve()).lower():
    ROLLUP_CACHE = etv.ROLLUP_CACHE
else:
    logger.warning("PROCESSED drift: %s -> pinning to %s",
                   etv.ROLLUP_CACHE.parent, REAL_PROCESSED)
    _common.PROCESSED = REAL_PROCESSED
    etv.ROLLUP_CACHE = REAL_PROCESSED / "rollup_cache"
    ROLLUP_CACHE = etv.ROLLUP_CACHE
    for _m in (build_vsa_klines, compute_large_trades, build_final,
               p1_btc, p1_eth, p2, p5):
        if getattr(_m, "PROCESSED", None) is not None:
            _m.PROCESSED = REAL_PROCESSED
PROJECT_ROOT = BASE.parent                                   # Dataset_Fetcher
TAPE = AGGTRADES_DIR
VISION_BASE = "https://data.binance.vision"
KLINE_URL = (VISION_BASE + "/data/futures/um/daily/klines/{sym}/{tf}/{sym}-{tf}-{date}.zip")
AGGT_URL = (VISION_BASE + "/data/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{date}.zip")

KLINE_CSV_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                  "quote_asset_volume", "num_trades", "taker_buy_base_vol",
                  "taker_buy_quote_vol", "ignore"]
STORE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
              "quote_asset_volume", "num_trades", "taker_buy_base_vol",
              "taker_buy_quote_vol", "ignore", "datetime_utc", "symbol", "market",
              "data_type", "interval", "is_zero_volume", "ohlc_integrity_flag"]

SYMS = ("BTCUSDT", "ETHUSDT")
INTERVALS = ("1m", "1h", "1d")
MAX_WORKERS = 8
RAW_ZIP_CACHE = BASE / "raw" / "vision_klines_zip"     # resume-safe raw ZIP cache


def _utc_today() -> datetime.date:
    return datetime.now(timezone.utc).date()


def _date_range(start: datetime.date, end: datetime.date) -> List[str]:
    out, d = [], start
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _download(url: str, timeout: int = 120) -> bytes:
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout)
        except requests.RequestException as exc:
            logger.warning("download error %s (attempt %d): %s", url, attempt + 1, exc)
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r.content
        if r.status_code == 404:
            raise FileNotFoundError(url)
        logger.warning("HTTP %s on %s (attempt %d)", r.status_code, url, attempt + 1)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"gave up downloading {url}")


def probe_latest() -> str:
    """Latest published date on Binance Vision (probe today, back off a day)."""
    today = _utc_today()
    for cand in (today, today - timedelta(days=1), today - timedelta(days=2)):
        url = KLINE_URL.format(sym="BTCUSDT", tf="1d", date=cand.strftime("%Y-%m-%d"))
        try:
            r = requests.head(url, timeout=20)
        except requests.RequestException:
            continue
        if r.status_code == 200:
            return cand.strftime("%Y-%m-%d")
    raise RuntimeError("Binance Vision unreachable — cannot determine latest available date")


# ---------------------------------------------------------------------------
# 1. futures klines (Vision daily ZIPs -> appended into the interval stores)
# ---------------------------------------------------------------------------

def _kline_frame(raw: bytes, sym: str, tf: str) -> pd.DataFrame:
    """Unzip a Vision daily klines ZIP and parse the CSV inside it."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in z.namelist() if n.endswith(".csv"))
    csv = z.read(name)
    first = csv.split(b"\n", 1)[0]
    df = pd.read_csv(io.BytesIO(csv),
                     names=None if first.startswith(b"open_time") else KLINE_CSV_COLS,
                     header=0 if first.startswith(b"open_time") else None)
    if len(df.columns) != len(KLINE_CSV_COLS):
        raise RuntimeError(f"{sym} {tf}: unexpected kline CSV shape {df.shape}")
    df.columns = KLINE_CSV_COLS
    for c in ("open", "high", "low", "close", "volume",
              "quote_asset_volume", "taker_buy_base_vol", "taker_buy_quote_vol"):
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
    return df[STORE_COLS]


def extend_klines(sym: str, tf: str, latest: str) -> int:
    """Append Vision klines for the days after the store's end into the store."""
    d = KLINE_STORE / "futures_um" / "klines" / sym / tf
    p = d / "merged.parquet"
    if not p.exists():
        cands = sorted(d.glob("*.parquet"))
        if not cands:
            raise FileNotFoundError(f"kline store missing: {d}")
        p = cands[0]
    # NOTE: read the FULL store frame — a columns=["open_time"] read here was
    # reused in the concat on 2026-08-21 and NaN-wiped the history (repaired
    # via repair_kline_stores.py). Never truncate the read.
    old = pd.read_parquet(p)
    last = pd.to_datetime(int(old["open_time"].iloc[-1]), unit="ms", utc=True).date()
    dates = _date_range(last + timedelta(days=1), datetime.strptime(latest, "%Y-%m-%d").date())
    if not dates:
        logger.info("%s %s: klines already to %s", sym, tf, last)
        return 0
    urls = {KLINE_URL.format(sym=sym, tf=tf, date=ds): ds for ds in dates}
    RAW_ZIP_CACHE.mkdir(parents=True, exist_ok=True)
    raw_by_date: Dict[str, bytes] = {}
    holes: List[str] = []

    def _get(url: str, ds: str) -> bytes:
        cached = RAW_ZIP_CACHE / f"{sym}_{tf}_{ds}.zip"
        if cached.exists():
            return cached.read_bytes()
        raw = _download(url)
        tmp = cached.with_name(cached.name + ".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, cached)
        return raw

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_get, u, ds): ds for u, ds in urls.items()}
        for fut in as_completed(futs):
            ds = futs[fut]
            try:
                raw_by_date[ds] = fut.result()
            except FileNotFoundError:
                holes.append(ds)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"{sym} {tf} klines failed at {ds}: {exc}") from exc
    if holes:
        logger.warning("%s %s klines: %d holes (unpublished): %s", sym, tf,
                       len(holes), holes[:5])
    if not raw_by_date:
        return 0
    frames = [_kline_frame(raw_by_date[ds], sym, tf) for ds in sorted(raw_by_date)]
    new = pd.concat(frames, ignore_index=True)
    overlap = old["open_time"].isin(new["open_time"]).sum()
    if overlap:
        logger.warning("%s %s: %d overlapping bars between store and new days "
                       "(dedup keeps store)", sym, tf, overlap)
    comb = pd.concat([old, new])\
        .drop_duplicates("open_time", keep="first")\
        .sort_values("open_time").reset_index(drop=True)
    if comb["volume"].isna().any() or comb["open"].isna().any():
        raise RuntimeError(f"{sym} {tf}: append produced NaN bars — aborting, "
                           "store untouched")
    tmp = p.with_name(p.name + ".tmp")
    pq.write_table(pa.Table.from_pandas(comb), tmp, compression="zstd", version="2.6")
    os.replace(tmp, p)
    n = int(len(new))
    logger.info("%s %s: klines extended %d days -> %s (%d rows total)",
                sym, tf, n, dates[-1], len(comb))
    return n


# ---------------------------------------------------------------------------
# 2. aggTrades tape (Vision daily ZIPs -> extension/ dirs)
# ---------------------------------------------------------------------------

def _last_tape_day(sym: str) -> datetime.date:
    """Tape end = max day over merged.parquet (if any) + the NEWEST day file of
    each of daily/ and extension/. Day files are per-day and named YYYYMMDD, so
    the lexically-last file in a dir carries that dir's max day — scanning only
    the tails avoids opening thousands of files (~20s+ vs ~0.1s for ETH)."""
    candidates: List[Path] = []
    p = TAPE / sym / "merged.parquet"
    if p.exists():
        candidates.append(p)
    for sub in ("daily", "extension"):
        d = TAPE / sym / sub
        if d.exists():
            fs = sorted(d.rglob("*.parquet"))
            if fs:
                candidates.append(fs[-1])
    if not candidates:
        raise RuntimeError(f"no tape files for {sym}")
    last = None
    for f in candidates:
        pf = pq.ParquetFile(f)
        t = pf.read_row_group(pf.num_row_groups - 1,
                              columns=["transact_time_utc"]).to_pandas()
        day = pd.to_datetime(t["transact_time_utc"].iloc[-1], utc=True).date()
        last = max(last, day) if last else day
    return last


def _aggt_frame(raw: bytes) -> pd.DataFrame:
    z = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in z.namelist() if n.endswith(".csv"))
    data = z.read(name)
    has_header = b"agg_trade_id" in data.split(b"\n", 1)[0]
    kwargs = {} if has_header else {"header": None, "names": AGG_CSV_COLS}
    df = pd.read_csv(io.BytesIO(data), **kwargs)
    df["is_buyer_maker"] = df["is_buyer_maker"].astype(str).str.lower().eq("true")
    df["transact_time_utc"] = pd.to_datetime(df["transact_time"], unit="ms", utc=True)
    return df.drop(columns=[c for c in df.columns if c not in AGGT_COLS])


def extend_tape(sym: str, latest: str) -> List[Path]:
    """Fetch missing aggTrades days into extension/ (resume-safe)."""
    last = _last_tape_day(sym)
    dates = _date_range(last + timedelta(days=1),
                        datetime.strptime(latest, "%Y-%m-%d").date())
    if not dates:
        logger.info("%s tape already to %s", sym, last)
        return []
    ext_root = TAPE / sym / "extension"
    written: List[Path] = []
    holes: List[str] = []
    for ds in dates:
        chunk = ext_root / ds[:7] / f"{ds.replace('-', '')}.parquet"
        if chunk.exists():
            continue
        try:
            df = _aggt_frame(_download(AGGT_URL.format(sym=sym, date=ds)))
        except FileNotFoundError:
            holes.append(ds)
            logger.warning("%s tape %s: no published file — hole", sym, ds)
            continue
        chunk.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(chunk, index=False, compression="zstd")
        written.append(chunk)
        logger.info("%s tape %s: %d trades", sym, ds, len(df))
    if holes:
        logger.warning("%s tape: %d holes: %s", sym, len(holes), holes[:5])
    return written


# ---------------------------------------------------------------------------
# 3. rollup cache extension (new tape days only — no full rescan)
# ---------------------------------------------------------------------------

def _rollup_days(sym: str, files: List[Path]) -> pd.DataFrame:
    acc: Dict[np.datetime64, np.ndarray] = defaultdict(lambda: np.zeros(5, dtype="float64"))
    for path in files:
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=2_000_000, columns=AGGT_COLS):
            ts = batch.column("transact_time_utc").to_numpy(zero_copy_only=False)
            qty = batch.column("quantity").to_numpy(zero_copy_only=False)
            px = batch.column("price").to_numpy(zero_copy_only=False)
            bm = batch.column("is_buyer_maker").to_numpy(zero_copy_only=False)
            day = ts.astype("datetime64[D]").astype("datetime64[ns]")
            notl = qty * px
            tb = np.where(bm, 0.0, qty)
            tbq = np.where(bm, 0.0, notl)
            g = pd.DataFrame({"d": day, "vb": qty, "vq": notl, "tb": tb,
                              "tbq": tbq,
                              "rc": np.ones(len(day), dtype="float64")})\
                   .groupby("d", observed=True).sum()
            for d, row in zip(g.index, g.to_numpy()):
                acc[d] += row
    out = pd.DataFrame(acc).T.sort_index()
    out.columns = ["vol_base", "vol_quote", "taker_buy_base",
                   "taker_buy_quote", "records"]
    idx = pd.DatetimeIndex(out.index.astype("datetime64[ns]"), name="date", tz="UTC")
    out.index = idx
    out["taker_sell_base"] = out["vol_base"] - out["taker_buy_base"]
    out["taker_sell_quote"] = out["vol_quote"] - out["taker_buy_quote"]
    out["taker_buy_ratio"] = out["taker_buy_base"] / out["vol_base"].replace(0, np.nan)
    return out.reset_index()


def new_tape_files(sym: str) -> List[Path]:
    """Tape files (daily/ + extension/) whose day is past the rollup cache end.

    Computed from the cache's own end (not from what extend_tape returned this
    run) so a resume never re-scans days already in the cache, and never
    misses days fetched by an earlier crashed run."""
    cache_p = ROLLUP_CACHE / f"{sym}.parquet"
    if cache_p.exists():
        cache_end = pd.to_datetime(
            pd.read_parquet(cache_p, columns=["date"])["date"], utc=True).max().date()
    else:
        cache_end = datetime(1970, 1, 1).date()
    out: List[Path] = []
    for sub in ("daily", "extension"):
        d = TAPE / sym / sub
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.parquet")):
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})|(\d{4})(\d{2})(\d{2})", f.name)
            if not m:
                continue
            ds = datetime.strptime(
                f"{m.group(1) or m.group(4)}-{m.group(2) or m.group(5)}-{m.group(3) or m.group(6)}",
                "%Y-%m-%d").date()
            if ds > cache_end:
                out.append(f)
    return sorted(out)


def extend_rollup_cache(sym: str, new_files: List[Path]) -> None:
    if not new_files:
        return
    cache_p = ROLLUP_CACHE / f"{sym}.parquet"
    new = _rollup_days(sym, new_files)
    if cache_p.exists():
        old = pd.read_parquet(cache_p)
        comb = pd.concat([old, new]).drop_duplicates("date", keep="first")\
            .sort_values("date").reset_index(drop=True)
    else:
        comb = new
    ROLLUP_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = cache_p.with_name(cache_p.name + ".tmp")
    pq.write_table(pa.Table.from_pandas(comb), tmp, compression="zstd", version="2.6")
    os.replace(tmp, cache_p)
    logger.info("%s rollup cache: +%d days -> %s (%d days)",
                sym, len(new), comb["date"].iloc[-1], len(comb))


# ---------------------------------------------------------------------------
# 4-8. rebuild chain
# ---------------------------------------------------------------------------

def rebuild_chain(force_adjudicate: bool = False, force_probe: bool = False,
                  backfill_cb: bool = False) -> None:
    for sym in SYMS:
        logger.info("== step0 %s ==", sym)
        # tape-gated, reads extended cache; adjudication verdicts are cached
        # (raw/triple_gate_adjudicated.json) — force re-verifies them
        build_exchange_true_volume(sym, force_adjudicate=force_adjudicate)
    logger.info("== vsa_klines ==")
    build_vsa_klines.run()
    for sym in SYMS:
        logger.info("== large trades %s ==", sym)
        compute_large_trades.run_symbol(sym)     # checkpointed; new days only
    for sym in SYMS:
        logger.info("== volume profile %s ==", sym)
        build_volume_profile.run_symbol(sym)     # checkpointed; new days only
    logger.info("== phase1 BTC (Blockchain.com) ==")
    p1_btc.run(force_refetch=True)
    logger.info("== phase1 ETH ==")
    p1_eth.run(force_refetch=True)
    logger.info("== phase5 realized cap (CoinMetrics) ==")
    p5.run(force_refetch=True)
    logger.info("== phase2 positioning (resume-safe) ==")
    p2.run(force_refetch=False, force_probe=force_probe)   # holes cached
    logger.info("== build_final ==")
    build_final.run()
    logger.info("== checkonchain cost basis (URPD) ==")
    try:
        # Sha-gated: the upstream repo's last DATA commit froze on 2026-02-19,
        # so once collected this downloads nothing and returns skipped=True. It
        # still re-checks the tree each run, which is one cheap request and is
        # how a future upstream update gets picked up automatically.
        #
        # The dated-curve archive syncs in tail mode by default: it re-reads only
        # the last few anchors, because the newest month is still restated
        # upstream while everything older is immutable. --backfill-cb is the
        # one-time pass that walks the archive back to 2016 -- and it is
        # resumable, so a throttled run is re-run rather than rebuilt.
        res = fetch_checkonchain_urpd.run(
            series_mode="backfill" if backfill_cb else "tail")
        logger.info("checkonchain cost basis: %s%s",
                    "unchanged, skipped" if res.get("skipped") else "collected",
                    "" if res.get("skipped") else f" ({len(res.get('outputs', []))} stores)")
        ser = res.get("series") or {}
        if ser.get("mode") and ser.get("mode") != "off":
            logger.info("cost-basis archive (%s): %d anchors, %d refetched, %d changed",
                        ser.get("mode"), len(ser.get("anchors") or []),
                        len(ser.get("refreshed") or []), len(ser.get("changed") or []))
    except Exception as exc:  # noqa: BLE001
        # never fatal: the cost-basis overlay is a context layer, not a
        # true-volume input, and a third-party source must not block the refresh
        logger.warning("checkonchain cost basis failed (non-fatal): %s", exc)
    logger.info("== rebuild chain complete ==")


# ---------------------------------------------------------------------------
# 9. visualizer (re)launch
# ---------------------------------------------------------------------------

def kill_port(port: int = 8000) -> None:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception as exc:  # noqa: BLE001
        logger.warning("netstat failed: %s", exc)
        return
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            pid = line.split()[-1]
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, timeout=30)
            logger.info("killed old visualizer on port %d (pid %s)", port, pid)


def launch() -> None:
    kill_port(8000)
    time.sleep(1.0)
    log_path = BASE / "visualizer.log"
    logf = open(log_path, "ab", buffering=0)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "BTC_True_Volume.visualizer.app:app",
         "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(PROJECT_ROOT), stdout=logf, stderr=logf,
        creationflags=flags, close_fds=True)
    logger.info("visualizer starting (pid %d) -> http://127.0.0.1:8000", proc.pid)
    for _ in range(30):
        time.sleep(1.5)
        try:
            r = requests.get("http://127.0.0.1:8000/api/health", timeout=3)
            if r.status_code == 200:
                logger.info("visualizer healthy: %s", r.text[:80])
                return
        except requests.RequestException:
            continue
    raise RuntimeError("visualizer did not become healthy within 45s — see visualizer.log")


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Refresh all visualizer feeds to latest, then launch")
    ap.add_argument("--no-launch", action="store_true", help="refresh only, do not start server")
    ap.add_argument("--dry-run", action="store_true", help="probe and report what would be fetched")
    ap.add_argument("--force-adjudicate", action="store_true",
                    help="re-verify disputed tape days against Binance Vision even when the tape fingerprint is unchanged")
    ap.add_argument("--force-probe", action="store_true",
                    help="re-probe the earliest published metrics day and cached 404 holes")
    ap.add_argument("--backfill-cb", action="store_true",
                    help="one-time: walk the weekly dated-curve archive back to 2016 "
                         "(~560 fetches at 1/s, resumable) instead of refreshing only "
                         "the newest anchors — needed once before window=custom can "
                         "reach further back than the last few weeks")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler()])
    logger.info("=== refresh_before_visualizer ===")
    t0 = time.time()

    latest = probe_latest()
    logger.info("latest available on Binance Vision: %s", latest)

    if args.dry_run:
        for sym in SYMS:
            for tf in INTERVALS:
                d = KLINE_STORE / "futures_um" / "klines" / sym / tf
                p = d / "merged.parquet"
                if not p.exists():
                    cands = sorted(d.glob("*.parquet"))
                    p = cands[0] if cands else p
                last = pd.to_datetime(int(pd.read_parquet(p, columns=["open_time"])["open_time"].iloc[-1]),
                                      unit="ms", utc=True).date()
                nd = (datetime.strptime(latest, "%Y-%m-%d").date() - last).days
                if nd > 0:
                    print(f"  klines {sym} {tf}: {last} -> {latest} ({nd} days)")
            tl = _last_tape_day(sym)
            nd = (datetime.strptime(latest, "%Y-%m-%d").date() - tl).days
            print(f"  tape   {sym}: {tl} -> {latest} ({nd} days)")
        print("dry run: nothing written")
        return

    # 1-3: fetch + append (skips when already current)
    for sym in SYMS:
        for tf in INTERVALS:
            extend_klines(sym, tf, latest)
    for sym in SYMS:
        extend_tape(sym, latest)
        extend_rollup_cache(sym, new_tape_files(sym))

    # 4-8: rebuild the processed stack
    rebuild_chain(force_adjudicate=args.force_adjudicate,
                  force_probe=args.force_probe,
                  backfill_cb=args.backfill_cb)

    if not args.no_launch:
        launch()

    logger.info("=== refresh complete in %.1f min ===", (time.time() - t0) / 60)
    logger.info("data through %s (Vision publish lag ~1 day)", latest)


if __name__ == "__main__":
    main()
