"""
compute_large_trades.py — daily large-block notional series from the aggTrades tape.

Tier-3 definition (Market_Maker_datasets/mm_proxy_criteria_report.md §5.2):
    "Large block trade  |  binance aggTrades  |  Trade notional > 99.9th
     percentile of rolling 30d distribution"

The report's rolling-30d window is approximated per DAY from that day's own
trade distribution (a strided sample capped at MAX_SAMPLE trades/day for the
percentile estimate; the full day is then used for the sums). This is a
documented approximation — the tooltip/usage guide say so; the threshold is
never silently treated as exact.

Sources (on disk + incremental extension/ dirs from refresh_before_visualizer):
    BTCUSDT: binance_full_dataset/processed/futures_um/aggtrades/BTCUSDT/merged.parquet
             (single time-ordered file; day derived from transact_time_utc)
           + .../BTCUSDT/extension/YYYY-MM/*.parquet (post-merge refresh days,
             same compact naming as the ETHUSDT extension)
    ETHUSDT: binance_full_dataset/processed/futures_um/aggtrades/ETHUSDT/daily/YYYY-MM/*.parquet
           + .../ETHUSDT/extension/YYYY-MM/*.parquet  (its merged.parquet is
             the partial pre-extension merge — ignored to avoid double counts)

Two streaming passes per symbol (peak RAM bounded by the current day's trades):
    pass 1 — per-day 99.9th percentile of trade notional (threshold)
    pass 2 — per-day sum of notional + count of trades above the threshold
Per-source checkpoint JSON in raw/large_trades_checkpoint/{SYM}.json (resume).

Output: processed/large_trades/{SYM}.parquet
    [date (UTC day), large_notional_usd, large_trade_count, threshold_usd, sample_size]
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("tvp.large_trades")

BASE = Path(__file__).resolve().parent                       # BTC_True_Volume
DATA_ROOT = BASE / "processed"                               # actual data root
TAPE = BASE.parent / "binance_full_dataset" / "processed" / "futures_um" / "aggtrades"
OUT_DIR = DATA_ROOT / "large_trades"
CKPT_DIR = BASE / "raw" / "large_trades_checkpoint"

MAX_SAMPLE = 250_000        # strided sample cap for the per-day percentile estimate
BATCH = 8_000_000           # merged-file batch size (rows)
DAY_MS = 86_400_000


def _day_str(ms: int) -> str:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc)
            + pd.Timedelta(milliseconds=int(ms))).strftime("%Y-%m-%d")


def _sources(sym: str) -> List[Tuple[str, Path]]:
    """Return [(day_str or 'merged', path)] in chronological order."""
    srcs = []
    merged = TAPE / sym / "merged.parquet"
    if sym == "BTCUSDT" and merged.exists():
        srcs.append(("merged", merged))
    for sub in ("daily", "extension"):
        for p in sorted((TAPE / sym / sub).rglob("*.parquet")):
            # both dashed (2026-08-19) and compact (20260819) names exist
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})|(\d{4})(\d{2})(\d{2})", p.name)
            if m:
                srcs.append((f"{m.group(1) or m.group(4)}-{m.group(2) or m.group(5)}"
                             f"-{m.group(3) or m.group(6)}", p))
            else:
                logger.warning("unparseable tape filename: %s", p)
    return srcs


def _iter_batches(path: Path, day_known: Optional[str]):
    """Yield (day_str, notional_float32_array) per day, streaming."""
    pf = pq.ParquetFile(path)
    if day_known is not None:
        for batch in pf.iter_batches(batch_size=BATCH, columns=["price", "quantity"]):
            notional = (batch.column("price").to_numpy().astype(np.float64)
                        * batch.column("quantity").to_numpy().astype(np.float64))
            yield day_known, notional.astype(np.float32)
        return
    # merged file: rows are time-ordered; walk day boundaries within each batch
    cur_day: Optional[int] = None
    buf: List[np.ndarray] = []
    buf_n = 0
    for batch in pf.iter_batches(batch_size=BATCH,
                                 columns=["price", "quantity", "transact_time_utc"]):
        days = batch.column("transact_time_utc").cast(pa.int64()).to_numpy()
        notional = (batch.column("price").to_numpy().astype(np.float64)
                    * batch.column("quantity").to_numpy().astype(np.float64)).astype(np.float32)
        days_ms = days // DAY_MS * DAY_MS
        i, n = 0, len(days)
        while i < n:
            d = days_ms[i]
            j = i + 1
            while j < n and days_ms[j] == d:
                j += 1
            if cur_day is None:
                cur_day = d
            if d != cur_day:
                yield _day_str(cur_day), np.concatenate(buf)
                cur_day = d
                buf, buf_n = [], 0
            buf.append(notional[i:j])
            buf_n += j - i
            i = j
    if cur_day is not None and buf:
        yield _day_str(cur_day), np.concatenate(buf)


def _sample_threshold(arr: np.ndarray) -> Tuple[float, int]:
    """99.9th percentile of trade notional from a strided sample."""
    n = len(arr)
    if n > MAX_SAMPLE:
        stride = (n + MAX_SAMPLE - 1) // MAX_SAMPLE
        arr = arr[::stride]
    return float(np.percentile(arr, 99.9)), n


def _load_ckpt(sym: str) -> Dict:
    p = CKPT_DIR / f"{sym}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"pass1_done": [], "pass2_done": []}


def _save_ckpt(sym: str, ckpt: Dict) -> None:
    CKPT_DIR.mkdir(parents=True, exist_ok=True)
    p = CKPT_DIR / f"{sym}.json"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(ckpt), encoding="utf-8")
    for attempt in range(5):
        try:
            tmp.replace(p)
            return
        except PermissionError:  # Windows transient lock (AV scan etc.)
            time.sleep(1.0 + attempt)
    # last resort: direct overwrite (the tmp write is already done)
    p.write_text(json.dumps(ckpt), encoding="utf-8")


def run_symbol(sym: str) -> Path:
    srcs = _sources(sym)
    ckpt = _load_ckpt(sym)
    thresholds: Dict[str, Tuple[float, int]] = {}

    # ---- pass 1: per-day thresholds -------------------------------------
    for key, path in srcs:
        if key in ckpt["pass1_done"]:
            continue
        day_known = None if key == "merged" else key
        for day, arr in _iter_batches(path, day_known):
            thr, n = _sample_threshold(arr)
            thresholds[day] = (thr, n)
        ckpt["pass1_done"].append(key)
        _save_ckpt(sym, ckpt)
        logger.info("%s pass1 %s done (%d days so far)", sym, path.name, len(thresholds))

    # ---- pass 2: per-day sums above threshold ----------------------------
    sums: Dict[str, List[float]] = {}
    for key, path in srcs:
        if key in ckpt["pass2_done"]:
            continue
        day_known = None if key == "merged" else key
        for day, arr in _iter_batches(path, day_known):
            thr, _ = thresholds.get(day, (np.inf, 0))
            sel = arr > thr
            n_large = int(sel.sum())
            s = float(np.sum(arr[sel], dtype=np.float64)) if n_large else 0.0
            if day in sums:
                sums[day][0] += s
                sums[day][1] += n_large
            else:
                sums[day] = [s, n_large]
        ckpt["pass2_done"].append(key)
        _save_ckpt(sym, ckpt)
        logger.info("%s pass2 %s done", sym, path.name)

    rows = []
    for day in sorted(sums):
        thr, n = thresholds[day]
        s, c = sums[day]
        rows.append({"date": pd.Timestamp(day, tz="UTC"),
                     "large_notional_usd": s, "large_trade_count": int(c),
                     "threshold_usd": thr, "sample_size": n})
    if not rows:
        # all sources already in the checkpoint (re-run with no new tape days)
        out = OUT_DIR / f"{sym}.parquet"
        if out.exists():
            logger.info("%s: no new tape days to process — %s already current", sym, out.name)
            return out
        raise RuntimeError(f"{sym}: pass produced no days and no existing output")
    df = pd.DataFrame(rows).set_index("date")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{sym}.parquet"
    if out.exists():
        # incremental runs only cover the newly-added sources' days — upsert
        # into the existing series, never clobber history with new days only
        old = pd.read_parquet(out)
        df = pd.concat([old, df])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        logger.info("%s: upserted %d new days into existing %d-day series",
                    sym, len(rows), len(df))
    tmp = out.with_name(out.name + ".tmp")
    df.to_parquet(tmp, compression="zstd")
    tmp.replace(out)
    meta = {
        "dataset_id": f"large_trades_{sym}",
        "description": "Daily large-block notional (Tier-3 proxy, mm_proxy_criteria_report.md §5.2): "
                       f"sum of futures aggTrades notional above the day's 99.9th percentile",
        "data_source": "binance_vision_aggtrades (tape)",
        "confidence_tier": "Medium (Tier-3 statistical proxy — not identity)",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "row_count": int(len(df)),
        "coverage": f"{df.index.min().date()} -> {df.index.max().date()}",
        "known_limitations": [
            "threshold is the day's own 99.9th percentile of trade notional from a strided "
            "sample (max 250k trades/day) — the criteria report specifies a rolling 30d "
            "window; day-level percentile is the computable approximation",
            "futures UM tape only (spot tape not collected)",
            "Tier-3 proxy: aggregated retail routed through one API key can mimic block trades",
        ],
    }
    (OUT_DIR / f"_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    logger.info("%s: wrote %s (%d days, %s)", sym, out, len(df), meta["coverage"])
    return out


def main() -> None:
    syms = sys.argv[1:] or ["BTCUSDT", "ETHUSDT"]
    for sym in syms:
        run_symbol(sym)
    print("\n=== LARGE TRADES DONE ===")


if __name__ == "__main__":
    main()
