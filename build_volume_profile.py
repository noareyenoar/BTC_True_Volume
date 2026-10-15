"""
build_volume_profile.py — traded volume bucketed by price level (volume profile).

Answers "where did volume actually trade?" from the futures aggTrades tape, as a
per-day x per-price-bucket reduction. Unlike URPD (on-chain supply by cost basis,
a single 2026-08-20 snapshot) this is *traded notional*, it has full history, and
it carries the taker split — so a level where sellers were absorbed can be told
apart from one where buyers were trapped.

Sources: reuse compute_large_trades._sources() — the proven-correct selection:
    BTCUSDT: .../aggTrades/BTCUSDT/merged.parquet (day from transact_time_utc)
           + .../BTCUSDT/extension/YYYY-MM/*.parquet
    ETHUSDT: .../aggTrades/ETHUSDT/daily/YYYY-MM/*.parquet
           + .../ETHUSDT/extension/YYYY-MM/*.parquet   (merged.parquet is the
             partial pre-extension merge — ignored to avoid double counting)
NOTE: ETH's daily/ (…→2026-08-14) and extension/ (2026-06-17→…) overlap by ~59
days. The two copies were verified byte-identical in content, and days are
deduped by day key here, so the overlap is harmless either way.

Reduction: one streaming pass, no per-day frame materialised. A fixed bucket
index over a fixed-width price grid turns the whole thing into np.bincount:
    vol_base       sum(quantity)
    vol_quote      sum(price * quantity)      <- the headline metric
    taker_buy_base sum(quantity) where the buyer was the taker (is_buyer_maker False)
    taker_sell_base sum(quantity) where the seller was the aggressor (is_buyer_maker True)
    trades         aggTrade count — NOT the raw trade count. aggTrades merges
                   same-price/same-side fills into one record, so this is very
                   roughly half the kline `num_trades` (BTC 2024-03-15: 4.27M
                   here vs 9.43M in the 1d kline). Do not compare them directly.
    (vol_quote agrees with the independent 1d kline product to ~1e-5 relative:
     BTC 2024-03-15 48,272,581,902 vs 48,272,589,072.)
Housekeeping on taker side: in aggTrades, is_buyer_maker=True means the BUYER was
resting, i.e. the SELLER was the aggressor.

Incremental: days already present in the output are skipped, finished source
files are skipped entirely, and the output is rewritten atomically. Re-running
with no new tape days is a no-op (this is the failure mode that bit
compute_large_trades — KeyError on an empty frame).

Output: processed/volume_profile/{SYM}/daily.parquet  [+ _meta.json]
    [date (date32, UTC day), price_bucket (lower edge, USD), vol_base,
     vol_quote, taker_buy_base, taker_sell_base, trades]
Checkpoint: raw/volume_profile_checkpoint/{SYM}.json

Usage:
    python build_volume_profile.py                      # incremental, both symbols
    python build_volume_profile.py --symbol BTCUSDT     # one symbol
    python build_volume_profile.py --backfill           # ignore checkpoints, redo
    python build_volume_profile.py --verify 2024-03-15  # single-day sanity check
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from compute_large_trades import BATCH, DAY_MS, TAPE, _day_str, _sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("tvp.volume_profile")

BASE = Path(__file__).resolve().parent
DATA_ROOT = BASE / "processed"
OUT_DIR = DATA_ROOT / "volume_profile"
CKPT_DIR = BASE / "raw" / "volume_profile_checkpoint"

# Fine bucket width. Chosen so the widest window still resolves at display
# width; the API re-aggregates upward from here, never downward.
# N_BUCKETS is a generous ceiling over the observed price range
# (BTC 3,621.8–126,208.5 | ETH 84.2–4,957.7) so a future high doesn't clip.
WIDTH: Dict[str, float] = {"BTCUSDT": 100.0, "ETHUSDT": 2.5}
N_BUCKETS: Dict[str, int] = {"BTCUSDT": 1500, "ETHUSDT": 2400}

SYMBOLS = ("BTCUSDT", "ETHUSDT")
FLUSH_EVERY_DAYS = 25       # checkpoint cadence during the long merged-file walk


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------
def _iter_day_chunks(path: Path, day_known: Optional[str]) -> Iterator[Tuple[str, np.ndarray, np.ndarray, np.ndarray]]:
    """Yield (day, price, quantity, is_buyer_maker) chunks.

    Sibling of compute_large_trades._iter_batches, which buffers a whole day and
    yields a single concatenated notional array. This yields the raw columns in
    chunks instead — the bincount accumulator needs no per-day materialisation —
    so the day-boundary walk is the only shared idea, not the buffering.
    A day may be yielded more than once (split across batches); the caller
    accumulates, so that is fine.
    """
    cols = ["price", "quantity", "is_buyer_maker"]
    pf = pq.ParquetFile(path)

    if day_known is not None:                      # a single-day file
        for batch in pf.iter_batches(batch_size=BATCH, columns=cols):
            yield (day_known,
                   batch.column("price").to_numpy().astype(np.float64),
                   batch.column("quantity").to_numpy().astype(np.float64),
                   batch.column("is_buyer_maker").to_numpy(zero_copy_only=False).astype(bool))
        return

    # merged file: rows are time-ordered, so walk day boundaries inside each batch
    for batch in pf.iter_batches(batch_size=BATCH, columns=cols + ["transact_time_utc"]):
        days_ms = batch.column("transact_time_utc").cast(pa.int64()).to_numpy() // DAY_MS * DAY_MS
        price = batch.column("price").to_numpy().astype(np.float64)
        qty = batch.column("quantity").to_numpy().astype(np.float64)
        ibm = batch.column("is_buyer_maker").to_numpy(zero_copy_only=False).astype(bool)
        i, n = 0, len(days_ms)
        while i < n:
            d = days_ms[i]
            j = i + 1
            while j < n and days_ms[j] == d:
                j += 1
            yield _day_str(d), price[i:j], qty[i:j], ibm[i:j]
            i = j


# --------------------------------------------------------------------------
# reduction
# --------------------------------------------------------------------------
def _new_acc(nb: int) -> List[np.ndarray]:
    return [np.zeros(nb, dtype=np.float64) for _ in range(5)]


def _accumulate(acc: List[np.ndarray], price: np.ndarray, qty: np.ndarray,
                ibm: np.ndarray, width: float, nb: int) -> None:
    """Fold one chunk into the day accumulator via five bincounts."""
    idx = np.floor(price / width).astype(np.int64)
    np.clip(idx, 0, nb - 1, out=idx)
    buy = ~ibm
    acc[0] += np.bincount(idx, weights=qty, minlength=nb)
    acc[1] += np.bincount(idx, weights=price * qty, minlength=nb)
    acc[2] += np.bincount(idx, weights=qty * buy, minlength=nb)
    acc[3] += np.bincount(idx, weights=qty * ibm, minlength=nb)
    acc[4] += np.bincount(idx, minlength=nb)


def _day_frame(day: str, acc: List[np.ndarray], width: float) -> Optional[pd.DataFrame]:
    """Non-zero buckets of one day -> frame. None if the day saw no trades."""
    nz = np.nonzero(acc[4])[0]
    if len(nz) == 0:
        return None
    return pd.DataFrame({
        "date_str": day,
        "price_bucket": nz.astype(np.float64) * width,
        "vol_base": acc[0][nz],
        "vol_quote": acc[1][nz],
        "taker_buy_base": acc[2][nz],
        "taker_sell_base": acc[3][nz],
        "trades": acc[4][nz].astype(np.int64),
    })


SCHEMA = pa.schema([
    pa.field("date", pa.date32()),
    pa.field("price_bucket", pa.float64()),
    pa.field("vol_base", pa.float64()),
    pa.field("vol_quote", pa.float64()),
    pa.field("taker_buy_base", pa.float64()),
    pa.field("taker_sell_base", pa.float64()),
    pa.field("trades", pa.int64()),
])


def _write(df: pd.DataFrame, out_path: Path, tmp_suffix: str = ".tmp") -> None:
    df = df.sort_values(["date_str", "price_bucket"], kind="stable").reset_index(drop=True)
    # date32 from ISO strings directly is not supported (pyarrow reads them as
    # integer day counts) — go via datetime.date objects.
    dates = pd.to_datetime(df["date_str"], format="%Y-%m-%d").dt.date.tolist()
    tbl = pa.table({
        "date": pa.array(dates, type=pa.date32()),
        "price_bucket": pa.array(df["price_bucket"].to_numpy(), type=pa.float64()),
        "vol_base": pa.array(df["vol_base"].to_numpy(), type=pa.float64()),
        "vol_quote": pa.array(df["vol_quote"].to_numpy(), type=pa.float64()),
        "taker_buy_base": pa.array(df["taker_buy_base"].to_numpy(), type=pa.float64()),
        "taker_sell_base": pa.array(df["taker_sell_base"].to_numpy(), type=pa.float64()),
        "trades": pa.array(df["trades"].to_numpy(), type=pa.int64()),
    }).cast(SCHEMA)
    tmp = out_path.with_name(out_path.name + tmp_suffix)
    pq.write_table(tbl, tmp, compression="zstd")
    tmp.replace(out_path)


def _load_existing(out_path: Path) -> Tuple[Optional[pd.DataFrame], set]:
    """Existing cache -> (frame, set of days already accumulated)."""
    if not out_path.exists():
        return None, set()
    df = pq.read_table(out_path).to_pandas()
    if df.empty:
        return None, set()
    df["date_str"] = df["date"].astype(str)
    df = df.drop(columns=["date"])
    return df, set(df["date_str"].unique())


def _load_ckpt(sym: str) -> Dict:
    p = CKPT_DIR / f"{sym}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("%s: unreadable checkpoint, starting fresh", sym)
    return {}


def _save_ckpt(sym: str, merged_done: bool, sources_done: set, n_rows: int) -> None:
    CKPT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": sym,
        "merged_done": bool(merged_done),
        "sources_done": sorted(sources_done),
        "rows": int(n_rows),
        "updated_utc": datetime.now(timezone.utc).isoformat(),
    }
    tmp = CKPT_DIR / f"{sym}.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(CKPT_DIR / f"{sym}.json")


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def run_symbol(sym: str, backfill: bool = False) -> Dict:
    """Reduce every not-yet-processed tape day for `sym` into the price-bucket cache.

    Incremental and resumable: finished days are skipped from the checkpoint, so a
    re-run with no new tape days is a no-op that leaves the parquet byte-identical.
    `backfill=True` reprocesses everything from scratch (and rewrites the cache).
    """
    if sym not in WIDTH:
        raise SystemExit(f"unknown symbol {sym} (known: {', '.join(SYMBOLS)})")
    width, nb = WIDTH[sym], N_BUCKETS[sym]
    out_dir = OUT_DIR / sym
    out_path = out_dir / "daily.parquet"
    out_dir.mkdir(parents=True, exist_ok=True)

    srcs = _sources(sym)
    if not srcs:
        logger.warning("%s: no tape sources found under %s", sym, TAPE / sym)
        return {"symbol": sym, "days_added": 0, "rows": 0}

    ckpt = {} if backfill else _load_ckpt(sym)
    merged_done = bool(ckpt.get("merged_done")) and not backfill
    sources_done = set() if backfill else set(ckpt.get("sources_done", []))

    existing, done = _load_existing(out_path)
    if backfill:
        done = set()
        existing = None

    logger.info("%s: %d sources, %d days already cached, merged_done=%s",
                sym, len(srcs), len(done), merged_done)

    new_frames: List[pd.DataFrame] = []
    cur_day: Optional[str] = None
    acc: Optional[List[np.ndarray]] = None
    dirty = False
    added = 0
    t0 = time.time()

    def close_day() -> None:
        nonlocal cur_day, acc, dirty, added
        if cur_day is not None and dirty and acc is not None:
            fr = _day_frame(cur_day, acc, width)
            if fr is not None:
                new_frames.append(fr)
                done.add(cur_day)
                added += 1
                if added % FLUSH_EVERY_DAYS == 0:
                    _checkpoint_now()
                    logger.info("%s: %d days added (%.1f min elapsed)", sym, added,
                                (time.time() - t0) / 60)
        cur_day, acc, dirty = None, None, False

    def _checkpoint_now() -> None:
        if new_frames:
            merged = [existing] if existing is not None else []
            _write(pd.concat(merged + new_frames, ignore_index=True), out_path)
        _save_ckpt(sym, merged_done, sources_done,
                   sum(len(f) for f in new_frames) + (len(existing) if existing is not None else 0))

    for key, path in srcs:
        if key == "merged":
            if merged_done:
                logger.info("%s: merged.parquet already processed — skipping", sym)
                continue
            day_known = None
        else:
            if key in sources_done:
                continue                       # finished file
            if key in done:
                sources_done.add(key)          # day already came from another source
                continue
            day_known = key

        for day, price, qty, ibm in _iter_day_chunks(path, day_known):
            if day != cur_day:
                close_day()
                cur_day, acc, dirty = day, _new_acc(nb), False
            if day in done:
                continue                        # resume: day already accumulated
            assert acc is not None
            _accumulate(acc, price, qty, ibm, width, nb)
            dirty = True

        if key != "merged":
            sources_done.add(key)
            if added and added % FLUSH_EVERY_DAYS == 0:
                _checkpoint_now()

    close_day()

    # Reaching here means any merged walk ran to completion, and a symbol with no
    # merged file will never have one — so True is right either way.
    merged_done = True

    if not new_frames:
        logger.info("%s: up to date — no new tape days (no-op)", sym)
        _save_ckpt(sym, merged_done, sources_done,
                   len(existing) if existing is not None else 0)
        return {"symbol": sym, "days_added": 0,
                "rows": len(existing) if existing is not None else 0}

    merged_frames = [existing] if existing is not None else []
    full = pd.concat(merged_frames + new_frames, ignore_index=True)
    _write(full, out_path)
    _save_ckpt(sym, merged_done, sources_done, len(full))

    days = full["date_str"].nunique()
    n_days = int((pd.to_datetime(full["date_str"]).max()
                  - pd.to_datetime(full["date_str"]).min()).days) + 1
    meta = {
        "symbol": sym,
        "bucket_width_usd": width,
        "n_buckets_max": nb,
        "rows": int(len(full)),
        "days": int(days),
        "day_span": [str(full["date_str"].min()), str(full["date_str"].max())],
        "coverage_pct": round(100.0 * days / max(n_days, 1), 1),
        "total_quote_usd": float(full["vol_quote"].sum()),
        "source": "binance_full_dataset futures_um aggTrades tape (Binance USD-M only)",
        "note": ("traded notional by price level; per day only buckets that traded. "
                 "cover one venue only (Binance USD-M futures)."),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    logger.info("%s: +%d days, %d rows total, span %s → %s (%.1f min)",
                sym, added, len(full), meta["day_span"][0], meta["day_span"][1],
                (time.time() - t0) / 60)
    return {"symbol": sym, "days_added": added, "rows": len(full)}


def _ms(v) -> int:
    """parquet timestamp statistics come back as datetime; normalise to ms."""
    return int(v.timestamp() * 1000) if isinstance(v, datetime) else int(v)


def _merged_day_chunks(path: Path, day_str: str):
    """Read only the row groups covering one day of the time-ordered merged file.

    The merged file is 29 GB / 4,448 row groups; walking it to reach 2024 would
    take ~30 min. Per-row-group `transact_time_utc` statistics let us jump
    straight to the day instead — binary search by row-group min/max.
    """
    start = int(datetime.strptime(day_str, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timestamp() * 1000)
    end = start + DAY_MS
    pf = pq.ParquetFile(path)
    ci = pf.schema_arrow.get_field_index("transact_time_utc")
    rgs = []
    for rg in range(pf.metadata.num_row_groups):
        st = pf.metadata.row_group(rg).column(ci).statistics
        if st is None or not st.has_min_max:          # can't rule it out
            rgs.append(rg)
            continue
        if _ms(st.max) < start or _ms(st.min) >= end:
            continue
        rgs.append(rg)
    if not rgs:
        raise SystemExit(f"{day_str}: no row groups in {path.name} cover that day")
    for batch in pf.read_row_groups(rgs, columns=["price", "quantity",
                                                  "is_buyer_maker", "transact_time_utc"]
                                    ).to_batches(max_chunksize=BATCH):
        ms = batch.column("transact_time_utc").cast(pa.int64()).to_numpy()
        keep = (ms >= start) & (ms < end)
        if not keep.any():
            continue
        yield (day_str,
               batch.column("price").to_numpy().astype(np.float64)[keep],
               batch.column("quantity").to_numpy().astype(np.float64)[keep],
               batch.column("is_buyer_maker").to_numpy(zero_copy_only=False).astype(bool)[keep])


def verify_day(sym: str, day: str) -> None:
    """Reduce a single day and print totals — for cross-checking vs klines."""
    width, nb = WIDTH[sym], N_BUCKETS[sym]
    want = day.strip()
    match = [p for k, p in _sources(sym) if k == want]
    if match:
        chunks, src_name = _iter_day_chunks(match[0], want), match[0].name
    else:
        merged = TAPE / sym / "merged.parquet"
        if not merged.exists():
            raise SystemExit(f"no tape source for {sym} {want}")
        chunks, src_name = _merged_day_chunks(merged, want), merged.name
    acc = _new_acc(nb)
    n_rows = 0
    for d, price, qty, ibm in chunks:
        _accumulate(acc, price, qty, ibm, width, nb)
        n_rows += len(price)
    fr = _day_frame(want, acc, width)
    print(f"{sym} {want}  ({src_name}, {n_rows:,} tape rows, {len(fr)} buckets)")
    print(f"  vol_base          {acc[0].sum():>18,.4f}")
    print(f"  vol_quote         {acc[1].sum():>18,.2f}")
    print(f"  taker_buy_base    {acc[2].sum():>18,.4f}")
    print(f"  taker_sell_base   {acc[3].sum():>18,.4f}")
    print(f"  buy+sell          {acc[2].sum() + acc[3].sum():>18,.4f}")
    print(f"  trades            {int(acc[4].sum()):>18,}")
    top = np.argsort(acc[1])[::-1][:5]
    print("  top buckets by notional:")
    for i in top:
        print(f"    ${i * width:>10,.0f}–${(i + 1) * width:>10,.0f}  "
              f"{acc[1][i]:>16,.0f} USD  {acc[0][i]:>12,.3f} base")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", action="append", choices=SYMBOLS, default=None,
                    help="symbol to build (repeatable; default: both)")
    ap.add_argument("--backfill", action="store_true",
                    help="ignore checkpoints and recompute from scratch")
    ap.add_argument("--verify", metavar="YYYY-MM-DD",
                    help="reduce one day and print totals, then exit")
    args = ap.parse_args()

    syms = args.symbol or list(SYMBOLS)
    if args.verify:
        for s in syms:
            verify_day(s, args.verify)
        return 0

    for s in syms:
        run_symbol(s, backfill=args.backfill)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
