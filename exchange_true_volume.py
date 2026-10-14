"""
exchange_true_volume.py — Step 0: daily exchange true volume from local stores.

Builds per-market (spot, futures) and combined daily volume series for
BTCUSDT / ETHUSDT from:
  - spot 1d klines      (binance_full_dataset/processed/spot/klines/{SYM}/1d/)  — 2017-08 -> 2024-12-31
  - futures 1d klines   (binance_full_dataset/processed/futures_um/klines/{SYM}/1d/) — 2020-01 -> 2026-05-31
  - futures aggTrades   (binance_full_dataset/processed/futures_um/aggTrades/{SYM}/)   — full tape rollup
    (BTC -> 2026-08-13, ETH -> 2026-06-16; --resume-aggtrades extends ETH via Vision ZIPs)

The aggTrades rollup is the authoritative futures volume source (kline 1d files
carry a monthly-merge artifact: missing first-of-month days). Futures klines are
used only for num_trades and for cross-validation of the rollup.

Spot boundary days (88 month-firsts) are cross-filled from dataset_1d/symbols
with an explicit `exch_cross_filled` flag; combined columns never double-count
or sum-with-NaN — coverage mode is tracked per row.

Output: processed/exchange_true_volume/{SYM}/merged.parquet (+ _meta.json)
"""

from __future__ import annotations

import argparse
import io
import logging
import shutil
import sys
import time
import os
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

try:
    from .common import (
        AGGTRADES_DIR, KLINE_STORE, PROCESSED, RAW, SYMBOLS_STORE, DS,
        PhaseResult, append_log, logger, write_parquet_with_meta,
        load_json_cache, save_json_cache,
    )
except ImportError:  # direct script execution (not via -m)
    from common import (
        AGGTRADES_DIR, KLINE_STORE, PROCESSED, RAW, SYMBOLS_STORE, DS,
        PhaseResult, append_log, logger, write_parquet_with_meta,
        load_json_cache, save_json_cache,
    )

logger = logging.getLogger("tvp.step0")

SPOT_KLINE_COLS = ["datetime_utc", "volume", "quote_asset_volume",
                   "taker_buy_base_vol", "taker_buy_quote_vol", "num_trades"]
SYMBOLS_COLS = ["timestamp", "volume", "quote_volume", "trades",
                "taker_buy_base", "taker_buy_quote"]
AGGT_COLS = ["transact_time_utc", "quantity", "price", "is_buyer_maker"]

VISION_AGGT_BASE = "https://data.binance.vision/data/futures/um/daily/aggTrades"
MAX_RETRIES = 4

# Persistent verdict cache for disputed-day adjudication: keyed (sym, date),
# valid while the tape day's vol/taker_buy values are unchanged (see
# adjudicate_against_vision_cached). Lets re-runs skip the ~4.5 min of official
# ZIP re-downloads for the same deterministic disputed set.
ADJUDICATION_CACHE = RAW / "triple_gate_adjudicated.json"


# ---------------------------------------------------------------------------
# aggTrades daily rollup (chunked, peak RAM << 2 GB)
# ---------------------------------------------------------------------------
ROLLUP_CACHE = PROCESSED / "rollup_cache"


def rollup_aggtrades_daily(sym: str, files: List[Path], force: bool = False) -> pd.DataFrame:
    """Streaming daily rollup of the aggTrades tape. Never loads the whole
    tape into RAM; per-batch groupby accumulates into a small day->array map.

    The result is cached to processed/rollup_cache/{sym}.parquet (atomic
    tmp+rename) so a later run — or a resume after a crash — skips the
    30-60 min tape scan entirely. --force recomputes."""
    cache = ROLLUP_CACHE / f"{sym}.parquet"
    if cache.exists() and not force:
        df = pd.read_parquet(cache)
        df["date"] = pd.to_datetime(df["date"], utc=True)
        df = df.set_index("date").sort_index()
        logger.info("%s: rollup loaded from cache (%d days)", sym, len(df))
        return df
    acc: Dict[np.datetime64, np.ndarray] = defaultdict(
        lambda: np.zeros(5, dtype="float64"))  # [vol_base, vol_quote, taker_buy_base, taker_buy_quote, records]
    total_rows = 0
    for path in files:
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=2_000_000, columns=AGGT_COLS):
            ts = batch.column("transact_time_utc").to_numpy(zero_copy_only=False)
            qty = batch.column("quantity").to_numpy(zero_copy_only=False)
            px = batch.column("price").to_numpy(zero_copy_only=False)
            bm = batch.column("is_buyer_maker").to_numpy(zero_copy_only=False)
            # floor to UTC day (kline boundary convention: day = open_time)
            day = ts.astype("datetime64[D]").astype("datetime64[ns]")
            notl = qty * px
            tb = np.where(bm, 0.0, qty)
            tbq = np.where(bm, 0.0, notl)
            g = pd.DataFrame({"d": day, "vb": qty, "vq": notl, "tb": tb,
                              "tbq": tbq, "rc": np.ones(len(day), dtype="float64")}) \
                   .groupby("d", observed=True).sum()
            for d, row in zip(g.index, g.to_numpy()):
                acc[d] += row
            total_rows += len(day)
        logger.info("%s: streamed %s (%.2f GB on disk)", sym, path.name,
                    path.stat().st_size / 1e9)
    if not acc:
        raise RuntimeError(f"{sym}: aggTrades rollup produced no rows")
    out = pd.DataFrame(acc).T.sort_index()
    out.columns = ["vol_base", "vol_quote", "taker_buy_base", "taker_buy_quote", "records"]
    idx = pd.DatetimeIndex(out.index.astype("datetime64[ns]"), name="date", tz="UTC")
    out.index = idx
    out["taker_sell_base"] = out["vol_base"] - out["taker_buy_base"]
    out["taker_sell_quote"] = out["vol_quote"] - out["taker_buy_quote"]
    out["taker_buy_ratio"] = out["taker_buy_base"] / out["vol_base"].replace(0, np.nan)
    logger.info("%s: rollup done — %d days, %d aggTrades records",
                sym, len(out), int(out["records"].sum()))
    ROLLUP_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(cache.name + ".tmp")
    pq.write_table(pa.Table.from_pandas(out.reset_index()), tmp,
                   compression="zstd", version="2.6")
    os.replace(tmp, cache)
    logger.info("%s: rollup cached -> %s", sym, cache)
    return out


def _aggtrade_files(sym: str) -> List[Path]:
    files = sorted((AGGTRADES_DIR / sym).glob("merged.parquet"))
    ext = AGGTRADES_DIR / sym / "extension"
    if ext.exists():
        files = files + sorted(ext.rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no aggTrades files for {sym} at {AGGTRADES_DIR / sym}")
    return files


# ---------------------------------------------------------------------------
# kline daily frames (small — 1d files only)
# ---------------------------------------------------------------------------
def kline_daily_frame(market: str, sym: str) -> pd.DataFrame:
    """Daily frame from 1d kline parquet(s): volume/quote/taker/num_trades."""
    files = sorted((KLINE_STORE / market / "klines" / sym / "1d").glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no 1d klines for {market} {sym}")
    frames = []
    for f in files:
        df = pq.read_table(f, columns=SPOT_KLINE_COLS).to_pandas()
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["datetime_utc"], utc=True).dt.normalize()
    df = df.drop_duplicates("date", keep="first").set_index("date")
    out = df[["volume", "quote_asset_volume", "taker_buy_base_vol",
              "taker_buy_quote_vol", "num_trades"]].astype("float64")
    out["num_trades"] = out["num_trades"].astype("float64")
    out.columns = ["volume", "quote_volume", "taker_buy_base",
                   "taker_buy_quote", "num_trades"]
    return out


def symbols_daily_frame(sym: str) -> pd.DataFrame:
    """Spot daily frame from dataset_1d/symbols (cross-fill source)."""
    p = SYMBOLS_STORE / f"{sym}.parquet"
    if not p.exists():
        raise FileNotFoundError(f"symbols store missing: {p}")
    df = pq.read_table(p, columns=SYMBOLS_COLS).to_pandas()
    df["date"] = pd.to_datetime(df["timestamp"], utc=True).dt.normalize()
    df = df.drop_duplicates("date", keep="first").set_index("date")
    out = df[["volume", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote"]]
    return out


# ---------------------------------------------------------------------------
# Cross-validation gates
# ---------------------------------------------------------------------------
def validate_ratio_gate(series_a: pd.Series, series_b: pd.Series,
                        tol: float, min_ok_pct: float) -> dict:
    j = pd.concat([series_a, series_b], axis=1, keys=["a", "b"]).dropna()
    ratio = j["a"] / j["b"].replace(0, np.nan)
    ratio = ratio.dropna()
    ok = ((ratio >= 1 - tol) & (ratio <= 1 + tol)).sum()
    return {"days": int(len(ratio)),
            "days_outside_tol": int(len(ratio) - ok),
            "pct_ok": 100.0 * ok / len(ratio) if len(ratio) else float("nan"),
            "worst_ratio": float(ratio.min()) if len(ratio) else float("nan"),
            "best_ratio": float(ratio.max()) if len(ratio) else float("nan")}


def adjudicate_against_vision(sym: str, days: List[str], tape: pd.DataFrame,
                              tol: float = 0.001) -> Tuple[List[str], List[str]]:
    """Verify disputed tape days against Binance's OWN official Vision daily
    aggTrades (free, keyless) — the primary source for this store.

    Empirically (2026-08-20, BTC + ETH, 11 + 5 days): every day where the tape
    disagreed with BOTH kline encodings (which agreed with each other), the
    tape matched the official Vision dump EXACTLY — the kline dumps are the
    contaminated side (both encodings derive from the same per-day dump
    source). Returns (adjudicated_days, failed_days); a day is adjudicated
    only when the tape matches official within tol on volume AND taker-buy.
    """
    import io
    import urllib.request
    import zipfile
    import time as _time

    adjudicated: List[str] = []
    failed: List[str] = []
    for ds in days:
        url = f"{VISION_AGGT_BASE}/{sym}/{sym}-aggTrades-{ds}.zip"
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                raw = r.read()
            z = zipfile.ZipFile(io.BytesIO(raw))
            name = next(n for n in z.namelist() if n.endswith(".csv"))
            data = z.read(name)
            has_header = b"agg_trade_id" in data.split(b"\n", 1)[0]
            kwargs = {} if has_header else {"header": None, "names": AGG_CSV_COLS}
            df = pd.read_csv(io.BytesIO(data), **kwargs)
            df["is_buyer_maker"] = df["is_buyer_maker"].astype(str).str.lower().eq("true")
            df["quantity"] = pd.to_numeric(df["quantity"])
            df["price"] = pd.to_numeric(df["price"])
            vol = float(df["quantity"].sum())
            tb = float(df.loc[~df["is_buyer_maker"], "quantity"].sum())
            key = pd.Timestamp(ds, tz="UTC")
            t_vol = float(tape.loc[key, "vol_base"])
            t_tb = float(tape.loc[key, "taker_buy_base"])
            ok = (abs(vol - t_vol) / max(vol, 1e-9) <= tol
                  and abs(tb - t_tb) / max(tb, 1e-9) <= tol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s adjudication %s failed to fetch: %s", sym, ds, exc)
            failed.append(ds)
            continue
        if ok:
            adjudicated.append(ds)
            logger.info("%s adjudicated %s: tape matches official Vision exactly "
                        "(vol %.3f / tb %.3f)", sym, ds, vol, tb)
        else:
            failed.append(ds)
            logger.warning("%s adjudication %s: tape MISMATCH official Vision "
                           "(official vol %.3f vs tape %.3f)", sym, ds, vol, t_vol)
        _time.sleep(0.5)  # polite pacing
    return adjudicated, failed


# ---------------------------------------------------------------------------
# Adjudication verdict cache (persistent; tape fingerprints, no re-downloads)
# ---------------------------------------------------------------------------
def _tape_day_values(tape: pd.DataFrame, ds: str) -> Optional[Tuple[float, float]]:
    """Day tape vol/taker_buy EXACTLY as adjudication compares them.

    Must mirror the .loc reads in adjudicate_against_vision (vol_base /
    taker_buy_base). None when the day is absent from the tape."""
    try:
        key = pd.Timestamp(ds, tz="UTC")
        return float(tape.loc[key, "vol_base"]), float(tape.loc[key, "taker_buy_base"])
    except (KeyError, TypeError):
        return None


def _load_adjudication_cache() -> Dict[str, Dict[str, Dict[str, Any]]]:
    cache = load_json_cache(ADJUDICATION_CACHE)
    if cache is None:
        if ADJUDICATION_CACHE.exists():
            logger.warning("adjudication cache corrupt or unreadable — "
                           "re-adjudicating disputed days")
        return {}
    if cache.get("version") != 1:
        logger.warning("adjudication cache version %s unsupported — ignoring",
                       cache.get("version"))
        return {}
    return {sym: days for sym, days in cache.items() if sym != "version"}


def _save_adjudication_cache(cache: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
    save_json_cache(ADJUDICATION_CACHE, {"version": 1, **cache})


def _day_source_fingerprint(sym: str, ds: str) -> Dict[str, Any]:
    """Informational provenance (file, mtime_ns, size) of the tape day's source
    file — never consulted for cache validity (value equality is the test)."""
    ext = AGGTRADES_DIR / sym / "extension" / ds[:7] / f"{ds.replace('-', '')}.parquet"
    p = ext if ext.exists() else AGGTRADES_DIR / sym / "merged.parquet"
    try:
        st = p.stat()
        return {"file": str(p), "mtime_ns": st.st_mtime_ns, "size": st.st_size}
    except OSError:
        return {}


def adjudicate_against_vision_cached(sym: str, days: List[str], tape: pd.DataFrame,
                                     force: bool = False
                                     ) -> Tuple[List[str], List[str],
                                                Dict[str, Dict[str, Any]],
                                                Dict[str, Dict[str, Any]]]:
    """Cache-aware adjudication. Returns (adjudicated, failed, fresh, entries):
      fresh   — days adjudicated THIS run (caller persists these to the cache)
      entries — verdict entries for EVERY adjudicated day (hits reuse the stored
                entry, fresh days get a new one) — the caller builds
                xval['adjudicated'] from this so cache hits and fresh runs
                produce identical tape_gate state (verify_all contract).

    A day is a cache hit iff: a cached entry exists with verdict
    'tape_matches_official' AND the day's tape vol/taker_buy recomputed from the
    CURRENT tape equal the stored values float-exactly (bit-identical: the
    rollup cache never recomputes historical days). Re-adjudication happens only
    on tape change, missing entry, or force. Cached-hit days are never
    re-downloaded and log nothing per-day."""
    cache = _load_adjudication_cache().get(sym, {})
    entries: Dict[str, Dict[str, Any]] = {}
    to_fetch: List[str] = []
    adjudicated: List[str] = []
    for ds in days:
        entry = cache.get(ds)
        vals = _tape_day_values(tape, ds)
        if (not force and entry is not None
                and entry.get("verdict") == "tape_matches_official"
                and vals is not None
                and entry.get("tape_vol") == vals[0]
                and entry.get("tape_taker_buy") == vals[1]):
            adjudicated.append(ds)
            entries[ds] = entry
            continue
        to_fetch.append(ds)
    fresh: Dict[str, Dict[str, Any]] = {}
    failed: List[str] = []
    if to_fetch:
        ok, failed = adjudicate_against_vision(sym, to_fetch, tape)  # unchanged downloader
        adjudicated.extend(ok)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for ds in ok:
            v = _tape_day_values(tape, ds)
            if v is None:
                continue  # day vanished from tape; not cacheable
            entry = {"verdict": "tape_matches_official", "tape_vol": v[0],
                     "tape_taker_buy": v[1], "verified_utc": now,
                     "source": _day_source_fingerprint(sym, ds)}
            fresh[ds] = entry
            entries[ds] = entry
    n_hits = len(adjudicated) - len(fresh)
    if n_hits:
        logger.info("%s: %d disputed days skipped (tape fingerprint unchanged)",
                    sym, n_hits)
    return adjudicated, failed, fresh, entries


def triple_gate(tape: pd.DataFrame, k1d: pd.DataFrame, k1m: pd.DataFrame,
                sym: str, tol: float = 0.002, min_ok_pct: float = 99.9) -> Dict[str, Any]:
    """Gate the tape rollup against the kline family, artifact-quarantined.

    Both kline encodings can be contaminated (proven on 2023-08-16 where the
    1d file is ~3% low, and 2023-11-10 where the 1m store is ~14% low while
    the 1d file agrees with the tape). Gate only on days where the two kline
    encodings AGREE with each other (self-consistent reference): the tape must
    match those days ~exactly (kline volume is the sum of its constituent
    aggTrades). Days where 1d and 1m disagree are quarantined: the tape value
    stands, the day is flagged — never silently dropped. The bad-day lists are
    returned so the caller can adjudicate them against Binance Vision official
    dumps (see adjudicate_against_vision).
    """
    j = pd.concat([k1d["volume"], k1m["volume"]], axis=1, keys=["d", "m"]).dropna()
    r = j["d"] / j["m"].replace(0, np.nan)
    ref_ok = j.index[r.ge(1 - tol) & r.le(1 + tol)]
    artifact = sorted(str(d.date()) for d in j.index.difference(ref_ok))
    vol = validate_ratio_gate(tape["vol_base"].reindex(ref_ok), k1m["volume"].reindex(ref_ok),
                              tol, min_ok_pct)
    tb = validate_ratio_gate(tape["taker_buy_base"].reindex(ref_ok),
                             k1m["taker_buy_base"].reindex(ref_ok), tol, min_ok_pct)
    v_rat = tape["vol_base"].reindex(ref_ok) / k1m["volume"].reindex(ref_ok).replace(0, np.nan)
    t_rat = tape["taker_buy_base"].reindex(ref_ok) / k1m["taker_buy_base"].reindex(ref_ok).replace(0, np.nan)
    vol_bad = sorted(str(d.date()) for d in v_rat.index[~v_rat.dropna().between(1 - tol, 1 + tol)])
    tb_bad = sorted(str(d.date()) for d in t_rat.index[~t_rat.dropna().between(1 - tol, 1 + tol)])
    logger.info("%s triple gate: ref_ok=%d days, quarantined=%d, volume %s, taker-buy %s",
                sym, len(ref_ok), len(artifact), vol, tb)
    if artifact:
        logger.warning("%s quarantined days (1d/1m disagree): %s", sym, artifact)
    return {"volume": vol, "taker_buy": tb, "ref_ok_days": int(len(ref_ok)),
            "artifact_days": artifact,
            "volume_bad_days": vol_bad, "taker_bad_days": tb_bad}


def arbitrate_num_trades(rollup: pd.DataFrame, k1d: pd.DataFrame, k1m: pd.DataFrame,
                         sym: str) -> pd.Series:
    """Pick the num_trades source per day: prefer the kline encoding whose
    volume matches the tape (the tape is ground truth)."""
    ratio_m = rollup["vol_base"] / k1m["volume"].reindex(rollup.index).replace(0, np.nan)
    ratio_d = rollup["vol_base"] / k1d["volume"].reindex(rollup.index).replace(0, np.nan)
    use_m = ratio_m.between(0.998, 1.002, inclusive="both")
    use_d = ratio_d.between(0.998, 1.002, inclusive="both")
    corrected = use_m & ~use_d
    nt = k1m["num_trades"].reindex(rollup.index)  # full-store default
    nt[use_d] = k1d["num_trades"].reindex(rollup.index)[use_d]  # 1d where it matches the tape
    n_corrected = int(corrected.sum())
    if n_corrected:
        logger.info("%s: num_trades taken from 1m store on %d days where the 1d file "
                    "disagrees with the tape (%s)", sym, n_corrected,
                    ", ".join(str(d.date()) for d in corrected[corrected].index[:8]))
    return nt, corrected


# ---------------------------------------------------------------------------
# Cross-fill from 1m klines (the 1d store drops first-of-month days — a
# monthly-merge artifact; the 1m store is complete. dataset_1d/symbols was
# investigated as an alternative source but its volume column is corrupted
# (frozen constant values on unrelated dates) — NOT used.)
# ---------------------------------------------------------------------------
def roll_1m_all(market: str, sym: str) -> pd.DataFrame:
    """Full-scan 1m kline store -> daily aggregates (complete; no day filter).

    The 1m store is the only kline encoding with full coverage (the 1d store
    drops month-first days AND carries rare wrong values — see triple_gate).
    Full scan of ~34M rows is a few minutes, well within the overnight budget.
    """
    files = sorted((KLINE_STORE / market / "klines" / sym / "1m").glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no 1m klines for {market} {sym}")
    cols = ["open_time", "volume", "quote_asset_volume",
            "taker_buy_base_vol", "taker_buy_quote_vol", "num_trades"]
    frames = []
    for f in files:
        t = pq.read_table(f, columns=cols)
        if t.num_rows == 0:
            continue
        df = t.to_pandas()
        df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.normalize()
        frames.append(df.groupby("date")[["volume", "quote_asset_volume", "taker_buy_base_vol",
                                          "taker_buy_quote_vol", "num_trades"]].sum())
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames)
    out = out[~out.index.duplicated(keep="first")].sort_index()
    out.columns = ["volume", "quote_volume", "taker_buy_base", "taker_buy_quote", "num_trades"]
    logger.info("%s: 1m full rollup — %d days (%d rows scanned)", sym, len(out), int(out["num_trades"].count()))
    return out


def roll_1m_to_daily(market: str, sym: str, days: pd.DatetimeIndex) -> pd.DataFrame:
    """Sum 1m klines into daily aggregates for the given days (pyarrow filter
    pushdown keeps reads tiny — only the target minutes are loaded).

    Note: Binance omits zero-trade minutes, so the day-start minute is often
    absent — the filter must match every minute of each day, not just day-start."""
    files = sorted((KLINE_STORE / market / "klines" / sym / "1m").glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no 1m klines for {market} {sym}")
    day_ms = []
    for d in days:
        day_ms.extend(range(int(d.timestamp() * 1000),
                            int(d.timestamp() * 1000) + 86_400_000, 60_000))
    frames = []
    cols = ["open_time", "volume", "quote_asset_volume",
            "taker_buy_base_vol", "taker_buy_quote_vol", "num_trades"]
    for f in files:
        t = pq.read_table(f, columns=cols, filters=[("open_time", "in", day_ms)])
        if t.num_rows == 0:
            continue
        df = t.to_pandas()
        df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.normalize()
        g = df.groupby("date")[["volume", "quote_asset_volume", "taker_buy_base_vol",
                                "taker_buy_quote_vol", "num_trades"]].sum()
        frames.append(g)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames)
    out = out[~out.index.duplicated(keep="first")].sort_index()
    out.columns = ["volume", "quote_volume", "taker_buy_base", "taker_buy_quote", "num_trades"]
    return out


def reindex_full_calendar(daily: pd.DataFrame) -> pd.DataFrame:
    """The 1d store simply lacks the artifact days (no NaN rows) — reindex to
    the full UTC calendar so missing days become NaN and can be filled."""
    idx = pd.date_range(daily.index.min(), daily.index.max(), freq="1D", tz="UTC")
    return daily.reindex(idx)


def cross_fill_from_1m(daily: pd.DataFrame, market: str, sym: str,
                       market_label: str) -> Tuple[pd.DataFrame, str]:
    """Fill days missing from the 1d store using 1m rollup. The rollup
    methodology is first validated against the 1d store on a sample of days
    that DO exist (must agree within ±0.2 %), otherwise the fill is rejected."""
    daily = reindex_full_calendar(daily)
    missing = daily.index[daily["volume"].isna()]
    if len(missing) == 0:
        return daily, f"{market_label}: no missing days"
    # validation sample: up to 60 days spread across the present 1d range
    present = daily.index[~daily["volume"].isna()]
    n_val = min(60, len(present))
    step = max(1, len(present) // n_val)
    val_days = pd.DatetimeIndex(present[::step])
    roll_all = roll_1m_to_daily(market, sym, val_days.union(missing))
    if roll_all.empty:
        return daily, f"{market_label}: 1m rollup empty — {len(missing)} days left NaN"
    gate = validate_ratio_gate(daily["volume"].reindex(val_days),
                               roll_all["volume"].reindex(val_days),
                               tol=0.002, min_ok_pct=99.9)
    if gate["pct_ok"] < 99.9:
        return daily, (f"{market_label}: 1m rollup REJECTED (agrees on {gate['pct_ok']:.1f}% "
                       f"of {len(val_days)} sample days) — {len(missing)} days left NaN")
    got = roll_all.loc[roll_all.index.intersection(missing)]
    daily = daily.combine_first(got)
    n_filled = int(got.index.isin(missing).sum())
    return daily, f"{market_label}: cross-filled {n_filled}/{len(missing)} days from 1m klines"


# ---------------------------------------------------------------------------
# ETH aggTrades resume (Vision ZIPs -> extension/ dir)
# ---------------------------------------------------------------------------
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


AGG_CSV_COLS = ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
                "transact_time", "is_buyer_maker"]


def resume_aggtrades(sym: str, start: str, end: str) -> List[Path]:
    """Fetch Vision daily ZIPs for [start, end] into the extension/ dir."""
    ext_root = AGGTRADES_DIR / sym / "extension"
    ext_root.mkdir(parents=True, exist_ok=True)
    d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    holes: List[str] = []
    while d <= end_d:
        ds = d.strftime("%Y-%m-%d")
        chunk = ext_root / d.strftime("%Y-%m") / f"{ds.replace('-', '')}.parquet"
        if chunk.exists():
            d += timedelta(days=1)
            continue
        url = f"{VISION_AGGT_BASE}/{sym}/{sym}-aggTrades-{ds}.zip"
        try:
            raw = _download_zip(url)
            z = zipfile.ZipFile(io.BytesIO(raw))
            name = next(n for n in z.namelist() if n.endswith(".csv"))
            data = z.read(name)
            has_header = b"agg_trade_id" in data.split(b"\n", 1)[0]
            kwargs = {} if has_header else {"header": None, "names": AGG_CSV_COLS}
            df = pd.read_csv(io.BytesIO(data), **kwargs)
            df["is_buyer_maker"] = df["is_buyer_maker"].astype(str).str.lower().eq("true")
            df["transact_time_utc"] = pd.to_datetime(df["transact_time"], unit="ms", utc=True)
            df = df.drop(columns=[c for c in df.columns if c not in AGGT_COLS])
            chunk.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(chunk, index=False, compression="zstd")
        except FileNotFoundError:
            holes.append(ds)
            logger.warning("%s %s: no published file — hole", sym, ds)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{sym} resume failed at {ds}: {exc}") from exc
        d += timedelta(days=1)
    if holes:
        logger.warning("%s resume: %d holes: %s", sym, len(holes), holes[:5])
    return sorted(ext_root.rglob("*.parquet"))


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------
def build_exchange_true_volume(sym: str, resume: bool = False, force: bool = False,
                               force_adjudicate: bool = False) -> PhaseResult:
    t0 = time.time()
    asset = "BTC" if sym == "BTCUSDT" else "ETH"

    # --- futures: aggTrades rollup (authoritative) -------------------------
    if resume and sym == "ETHUSDT":
        # extend ETH tape to match BTC coverage end
        start = "2026-06-17"
        end = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        logger.info("resuming ETH aggTrades %s -> %s", start, end)
        resume_aggtrades(sym, start, end)
    files = _aggtrade_files(sym)
    rollup = rollup_aggtrades_daily(sym, files, force)

    # --- futures klines: 1d file + full 1m store ---------------------------
    fut_kline_raw = kline_daily_frame("futures_um", sym)       # artifact-prone 1d file
    raw_fut_dates = set(fut_kline_raw.index)
    k1m = roll_1m_all("futures_um", sym)                       # complete store
    fut_kline = reindex_full_calendar(fut_kline_raw)
    n_fut_missing = int(fut_kline["volume"].isna().sum())
    fut_kline = fut_kline.combine_first(k1m)
    fut_fill_note = f"futures: cross-filled {n_fut_missing} month-first days from 1m klines"

    # --- spot klines + cross-fill (1m rollup; symbols store is corrupt) -----
    spot = kline_daily_frame("spot", sym)
    raw_spot_dates = set(spot.index)
    spot, spot_fill_note = cross_fill_from_1m(spot, "spot", sym, "spot")

    # --- tape gate: artifact-quarantined triple comparison ------------------
    xval = triple_gate(rollup, fut_kline_raw, k1m, sym)
    gate_vol = xval["volume"]["pct_ok"] >= 99.9
    gate_tb = xval["taker_buy"]["pct_ok"] >= 99.9
    if not (gate_vol and gate_tb):
        # every outside-tolerance day must be adjudicated against Binance's own
        # official Vision dump before the tape may stand (hard gate otherwise);
        # verdicts are cached so unchanged days skip the re-download entirely
        disputed = sorted(set(xval["volume_bad_days"]) | set(xval["taker_bad_days"]))
        adjudicated, failed, fresh, entries = adjudicate_against_vision_cached(
            sym, disputed, rollup, force=force_adjudicate)
        if fresh:
            cache = _load_adjudication_cache()
            cache.setdefault(sym, {}).update(fresh)   # only fresh days persist
            _save_adjudication_cache(cache)
        if failed:
            raise RuntimeError(
                f"{sym}: tape-vs-klines gate FAILED on {xval['ref_ok_days']} "
                f"self-consistent days — volume {xval['volume']}, taker "
                f"{xval['taker_buy']}; unverified/refuted bad days: {failed}. "
                "Do not proceed with a suspect tape (INCOMPLETE by design).")
        logger.warning(
            "%s: tape-vs-klines gate outside tolerance ONLY on days verified "
            "against Binance Vision official aggTrades (%d days, tape matches "
            "exactly) — kline dumps are the contaminated side; tape stands, "
            "days flagged", sym, len(adjudicated))
        verified_uts = sorted({e["verified_utc"][:19] for e in entries.values() if e})
        xval["adjudicated"] = {
            "tape_validated": True,
            "evidence": "official daily aggTrades fetched from data.binance.vision "
                        "for every disputed day — tape matches Binance exactly "
                        f"(volume and taker-buy); verified {', '.join(verified_uts)}",
            "volume_days": sorted(xval["volume_bad_days"]),
            "taker_days": sorted(xval["taker_bad_days"]),
            "verified_days": adjudicated,
        }

    # --- num_trades: 1d-vs-1m arbitration, tape volume as arbiter -----------
    fut_nt, nt_corrected_mask = arbitrate_num_trades(rollup, fut_kline_raw, k1m, sym)
    nt_corrected_days = sorted(str(d.date()) for d in nt_corrected_mask[nt_corrected_mask].index)

    # --- assemble daily frame ----------------------------------------------
    start_d = min(spot.index.min(), rollup.index.min())
    end_d = max(spot.index.max(), rollup.index.max())
    idx = pd.date_range(start_d, end_d, freq="1D", tz="UTC")
    df = pd.DataFrame(index=idx)
    df.index.name = "date"

    df["exch_spot_volume"] = spot["volume"]
    df["exch_spot_quote_volume_usd"] = spot["quote_volume"]
    df["exch_spot_taker_buy"] = spot["taker_buy_base"]
    df["exch_spot_taker_sell"] = spot["volume"] - spot["taker_buy_base"]
    df["exch_spot_taker_buy_quote_usd"] = spot["taker_buy_quote"]
    df["exch_spot_taker_buy_ratio"] = spot["taker_buy_base"] / spot["volume"].replace(0, np.nan)
    df["exch_spot_num_trades"] = spot["num_trades"]

    df["exch_fut_volume"] = rollup["vol_base"]
    df["exch_fut_quote_volume_usd"] = rollup["vol_quote"]
    df["exch_fut_taker_buy"] = rollup["taker_buy_base"]
    df["exch_fut_taker_sell"] = rollup["taker_sell_base"]
    df["exch_fut_taker_buy_quote_usd"] = rollup["taker_buy_quote"]
    df["exch_fut_taker_buy_ratio"] = rollup["taker_buy_ratio"]
    df["exch_fut_num_trades"] = fut_nt
    df["exch_fut_aggtrade_records"] = rollup["records"]

    # combined — never sum-with-NaN: both-present -> sum, else single market
    both = df["exch_spot_volume"].notna() & df["exch_fut_volume"].notna()
    df["exch_combined_volume"] = np.where(
        both, df["exch_spot_volume"] + df["exch_fut_volume"],
        df[["exch_spot_volume", "exch_fut_volume"]].sum(axis=1, skipna=True))
    df["exch_combined_quote_volume_usd"] = np.where(
        both, df["exch_spot_quote_volume_usd"] + df["exch_fut_quote_volume_usd"],
        df[["exch_spot_quote_volume_usd", "exch_fut_quote_volume_usd"]].sum(axis=1, skipna=True))
    tb_sum = np.where(both, df["exch_spot_taker_buy"] + df["exch_fut_taker_buy"],
                      df[["exch_spot_taker_buy", "exch_fut_taker_buy"]].sum(axis=1, skipna=True))
    vol_sum = df["exch_combined_volume"]
    df["exch_combined_taker_buy_ratio"] = tb_sum / vol_sum.replace(0, np.nan)

    df["exch_coverage_mode"] = np.select(
        [df["exch_spot_volume"].notna() & df["exch_fut_volume"].notna(),
         df["exch_spot_volume"].notna(),
         df["exch_fut_volume"].notna()],
        ["spot+futures", "spot_only", "futures_only"],
        default="gap")
    # days filled from the 1m rollup (present in 1m, absent in the raw 1d store)
    df["exch_spot_cross_filled"] = df.index.isin(spot.index) & ~df.index.isin(raw_spot_dates)
    df["exch_fut_cross_filled"] = df.index.isin(fut_kline.index) & ~df.index.isin(raw_fut_dates)
    # kline-artifact quarantine: days where the two kline encodings disagreed
    # (tape vs at least one encoding is fine) OR both encodings were adjudicated
    # contaminated vs Binance Vision official (tape stands, day flagged)
    kline_bad = sorted(set(xval["artifact_days"]) | set(xval["volume_bad_days"])
                       | set(xval["taker_bad_days"]))
    df["exch_fut_kline_artifact"] = df.index.isin(
        pd.DatetimeIndex(pd.to_datetime(kline_bad, utc=True)))
    # num_trades days where the 1m store replaced the 1d file (tape-arbitrated)
    df["exch_fut_numtrades_corrected"] = df.index.isin(
        pd.DatetimeIndex(pd.to_datetime(nt_corrected_days, utc=True)))

    df = df.reset_index()
    df["date"] = df["date"].dt.normalize()

    # --- sanity -------------------------------------------------------------
    num_cols = [c for c in df.columns if c.startswith("exch_") and c != "exch_coverage_mode"
                and "ratio" not in c and c != "exch_cross_filled"]
    assert (df[num_cols].select_dtypes("number").fillna(0) >= 0).all().all(), "negative volumes"
    ratios = [c for c in df.columns if "taker_buy_ratio" in c]
    assert (df[ratios].dropna() >= 0).all().all() and (df[ratios].dropna() <= 1).all().all(), \
        "taker_buy_ratio outside [0,1]"

    out_path = PROCESSED / "exchange_true_volume" / sym / "merged.parquet"
    n_filled = int(df["exch_spot_cross_filled"].sum())
    n_fut_filled = int(df["exch_fut_cross_filled"].sum())
    meta = {
        "dataset_id": f"exchange_true_volume_{sym}_1d",
        "description": f"{sym} daily exchange true volume — spot+futures combined, taker flows",
        "data_source": DS["binance_vision_aggtrades"],
        "confidence_tier": "High",
        "sources": {
            "futures_volume": DS["binance_vision_aggtrades"],
            "futures_num_trades": DS["binance_vision_klines"],
            "spot": DS["binance_vision_klines"],
            "spot_cross_fill": "binance_vision_klines (1m rollup)",
            "futures_num_trades_cross_fill": "binance_vision_klines (1m rollup)",
        },
        "coverage": [str(start_d.date()), str(end_d.date())],
        "tape_gate": xval,
        "num_trades_corrected_days": nt_corrected_days,
        "spot_cross_fill": spot_fill_note,
        "futures_cross_fill": fut_fill_note,
        "spot_cross_filled_days": n_filled,
        "futures_cross_filled_days": n_fut_filled,
        "aggtrade_files": [str(f) for f in files],
        "known_limitations": [
            "exchange-reported volume includes wash-trading noise — proxy, not literal true volume",
            "disputed days adjudicated vs Binance Vision official aggTrades "
            "(tape verified exact; kline dumps contaminated) — see "
            "tape_gate.adjudicated + exch_fut_kline_artifact column",
            f"spot klines stale after 2024-12-31; coverage mode flag per row ({sorted(df['exch_coverage_mode'].unique())})",
            "futures klines (num_trades) stale after 2026-05-31; futures volume from aggTrades tape",
            "aggTrades-derived records count is aggTrade records, not individual trades (num_trades from klines)",
            "dataset_1d/symbols rejected as cross-fill source: volume column corrupted (frozen constant values)",
            f"{len(xval['artifact_days'])} kline-artifact days quarantined from the tape gate "
            f"(1d file vs 1m store disagree; tape value stands): {xval['artifact_days'][:10]}",
            f"{len(nt_corrected_days)} days where num_trades was taken from the 1m store "
            f"(1d file disagreed with the tape): {nt_corrected_days[:10]}",
        ],
    }
    write_parquet_with_meta(df, out_path, meta, DS["binance_vision_aggtrades"], "High")
    n_fut_only = int((df["exch_coverage_mode"] == "futures_only").sum())
    n_sp_only = int((df["exch_coverage_mode"] == "spot_only").sum())
    return PhaseResult(
        phase="step0", name="Exchange true volume rollup", asset=asset,
        status="COMPLETE",
        output=f"{out_path} ({len(df):,} rows)",
        coverage=f"{start_d.date()} -> {end_d.date()}, {len(df):,} days "
                 f"(spot_only {n_sp_only:,}, spot+futures {both.sum():,}, futures_only {n_fut_only:,})",
        confidence=f"High as a volume proxy (aggTrades rollup; xval vol {xval['volume']['pct_ok']:.2f}%, "
                   f"taker {xval['taker_buy']['pct_ok']:.2f}% within tolerance)",
        limitations=[
            f"spot stale after 2024-12-31 ({n_fut_only:,} futures_only days in combined)",
            f"spot cross-fill: {spot_fill_note}",
            f"futures num_trades cross-fill: {fut_fill_note}",
            f"cross-validation: {xval}",
            "exchange-reported volume proxy, not network-level true volume",
        ],
        outputs=[out_path])


def main() -> None:
    ap = argparse.ArgumentParser(description="Step 0: exchange true volume rollup")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--resume-aggtrades", action="store_true",
                    help="extend ETH aggTrades tape via Vision ZIPs")
    ap.add_argument("--force", action="store_true",
                    help="recompute the aggTrades rollup instead of loading the disk cache")
    ap.add_argument("--force-adjudicate", action="store_true",
                    help="re-verify disputed days against Binance Vision even when the tape fingerprint is unchanged")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    results = []
    for sym in args.symbols.split(","):
        sym = sym.strip().upper()
        if not sym:
            continue
        results.append(build_exchange_true_volume(sym, resume=args.resume_aggtrades,
                                                  force=args.force,
                                                  force_adjudicate=args.force_adjudicate))
    if results:
        # one log entry per phase (plan template): merge per-symbol results
        res = results[0] if len(results) == 1 else PhaseResult(
            phase="step0", name="Exchange true volume rollup (BTC+ETH)", asset="BOTH",
            status="COMPLETE",
            output="; ".join(r.output for r in results),
            coverage="; ".join(r.coverage for r in results),
            confidence="; ".join(r.confidence for r in results),
            limitations=[l for r in results for l in r.limitations],
            outputs=[p for r in results for p in r.outputs])
        append_log(res.phase, res.name, res.asset, res.status, res.output,
                   res.coverage, res.confidence, res.limitations)
    print("\n=== STEP 0 EXCHANGE TRUE VOLUME DONE ===")


if __name__ == "__main__":
    main()
