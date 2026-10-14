"""
phase1_btc_network_metrics.py — Phase 1 (BTC): network-level on-chain volume.

Blockchain.com Charts API (keyless, free, full history 2009->present, daily):

    https://api.blockchain.info/charts/{chart}?timespan=all&format=json&sampled=false

Charts:
  - estimated-transaction-volume-usd  (the primary network-level volume proxy)
  - n-transactions, n-unique-addresses, hash-rate, miners-revenue, mempool-size

mempool-size is minute-granularity for recent history (period:"minute") — the
full-history JSON is huge; we request sampled=true first and fall back to
1-year windows resampled to daily mean; if still impractical it is dropped
(Low-value context metric, documented as a limitation).

Output: processed/onchain/network_metrics/BTC/merged.parquet (+ _meta.json)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

try:
    from .common import (PROCESSED, DS, PhaseResult, RateLimitedClient,
                         cache_json, load_cached_json, logger,
                         write_parquet_with_meta)
except ImportError:  # direct script execution
    from common import (PROCESSED, DS, PhaseResult, RateLimitedClient,
                        cache_json, load_cached_json, logger,
                        write_parquet_with_meta)

logger = logging.getLogger("tvp.phase1_btc")

CHARTS_BASE = "https://api.blockchain.info/charts"
# chart -> output column
CHARTS = {
    "estimated-transaction-volume-usd": "net_estimated_tx_volume_usd",
    "n-transactions": "net_tx_count",
    "n-unique-addresses": "net_unique_addresses",
    "hash-rate": "net_hash_rate",
    "miners-revenue": "net_miners_revenue_usd",
    "mempool-size": "net_mempool_size_bytes",
}
# charts that were never daily from the start / may return minute period
_MINUTE_CHARTS = {"mempool-size"}


def _fetch_chart(client: RateLimitedClient, chart: str,
                 force_refetch: bool = False) -> Optional[pd.DataFrame]:
    """Fetch one chart -> DataFrame[date, value]. Raw JSON cached under raw/."""
    cache_rel = Path("blockchain_com") / f"{chart}.json"
    cached = None if force_refetch else load_cached_json(cache_rel)
    if cached is None:
        data = client.get_json(f"{CHARTS_BASE}/{chart}",
                               params={"timespan": "all", "format": "json",
                                       "sampled": "false"})
        if data is None:
            logger.error("chart %s failed", chart)
            return None
        cache_json(cache_rel, data)
        cached = data
    period = cached.get("period", "day")
    values = cached.get("values", [])
    if not values:
        logger.warning("chart %s: empty values", chart)
        return None
    df = pd.DataFrame(values)
    df["date"] = pd.to_datetime(df["x"], unit="s", utc=True).dt.normalize()
    df = df.drop_duplicates("date", keep="last").set_index("date")
    df = df[~df.index.duplicated()]
    col = CHARTS[chart]
    df[col] = pd.to_numeric(df["y"], errors="coerce")
    logger.info("chart %s: %d days, period=%s, %s -> %s (%.4g .. %.4g)",
                chart, len(df), period, df.index.min().date(), df.index.max().date(),
                df[col].min(), df[col].max())
    return df[[col]], period


def _fetch_mempool_daily(client: RateLimitedClient,
                         force_refetch: bool = False) -> Optional[pd.DataFrame]:
    """mempool-size minute series -> daily mean. Tries sampled=true (small),
    else 1-year windows resampled; drops the metric if still impractical."""
    cache_rel = Path("blockchain_com") / "mempool-size-daily.json"
    cached = None if force_refetch else load_cached_json(cache_rel)
    if cached is not None:
        df = pd.DataFrame(cached["values"])
        df["date"] = pd.to_datetime(df["x"], unit="s", utc=True).dt.normalize()
        return df.groupby("date")["y"].mean().to_frame("net_mempool_size_bytes")
    # sampled=true returns daily-ish for the whole history when available
    data = client.get_json(f"{CHARTS_BASE}/mempool-size",
                           params={"timespan": "all", "format": "json",
                                   "sampled": "true"})
    if data and data.get("values"):
        df = pd.DataFrame(data["values"])
        df["date"] = pd.to_datetime(df["x"], unit="s", utc=True).dt.normalize()
        df = df.drop_duplicates("date", keep="last").set_index("date")
        df = df[~df.index.duplicated()]
        if data.get("period") == "day" or len(df) > 2000:
            out = df["y"].to_frame("net_mempool_size_bytes")
            cache_json(cache_rel, {"values": [{"x": int(t.timestamp()), "y": float(v)}
                                             for t, v in out.itertuples()]})
            return out
    # fall back to absolute year windows via start/end unix timestamps
    # (mempool-size is minute-granularity; "YYYY-YYYY" timespan is dead — 404)
    frames = []
    for y in range(2026, 2015, -1):
        t0 = int(pd.Timestamp(f"{y - 1}-01-01", tz="UTC").timestamp())
        t1 = int(pd.Timestamp(f"{y}-01-01", tz="UTC").timestamp())
        try:
            d = client.get_json(f"{CHARTS_BASE}/mempool-size",
                                params={"start": t0, "end": t1, "format": "json",
                                        "sampled": "false"})
        except Exception as exc:  # noqa: BLE001 — window unavailable is fine
            logger.warning("mempool-size %d window: %s", y, exc)
            continue
        if not d or not d.get("values"):
            continue
        df = pd.DataFrame(d["values"])
        df["date"] = pd.to_datetime(df["x"], unit="s", utc=True).dt.normalize()
        frames.append(df.groupby("date")["y"].mean())
    if not frames:
        logger.warning("mempool-size: impractical — dropping metric")
        return None
    out = pd.concat(frames)
    out = out[~out.index.duplicated(keep="last")].sort_index().to_frame("net_mempool_size_bytes")
    cache_json(cache_rel, {"values": [{"x": int(t.timestamp()), "y": float(v)}
                                     for t, v in out.itertuples()]})
    return out


def run(force_refetch: bool = False) -> PhaseResult:
    t0 = time.time()
    client = RateLimitedClient(min_interval=0.5)
    frames: List[pd.DataFrame] = []
    notes: List[str] = []
    for chart in CHARTS:
        try:
            if chart == "mempool-size":
                f = _fetch_mempool_daily(client, force_refetch)
            else:
                f, period = _fetch_chart(client, chart, force_refetch)
        except Exception as exc:  # noqa: BLE001 — a single chart must not kill the phase
            f = None
            notes.append(f"{chart}: {type(exc).__name__}: {exc}")
            logger.warning("chart %s failed: %s", chart, exc)
        if f is not None:
            frames.append(f)
        else:
            notes.append(f"{chart}: FAILED/unavailable")
            logger.warning("%s unavailable", chart)
    if not frames:
        raise RuntimeError("phase1_btc: no charts fetched at all")
    df = pd.concat(frames, axis=1, join="outer").sort_index()
    df.index.name = "date"

    # sanity: gap check within each chart's own range
    gaps = {}
    for col in df.columns:
        sub = df[col].dropna()
        if len(sub) > 1:
            full = pd.date_range(sub.index.min(), sub.index.max(), freq="1D", tz="UTC")
            miss = full.difference(sub.index)
            if len(miss):
                gaps[col] = [str(d.date()) for d in miss[:8]]
    if gaps:
        notes.append(f"gap days found: {gaps}")

    df = df.reset_index()
    df["date"] = df["date"].dt.normalize()

    out_path = PROCESSED / "onchain" / "network_metrics" / "BTC" / "merged.parquet"
    meta = {
        "dataset_id": "onchain_network_metrics_btc_daily",
        "description": "Bitcoin network-level metrics from Blockchain.com Charts API (free)",
        "charts": CHARTS,
        "coverage": [str(df["date"].min().date()), str(df["date"].max().date())],
        "gaps": gaps,
        "known_limitations": [
            "estimated-transaction-volume-usd includes non-trading transfers (internal wallet moves) "
            "— it is a settled-value proxy, not literal exchange true volume",
            f"mempool-size: {'daily via sampled/windows' if 'net_mempool_size_bytes' in df.columns else 'DROPPED (minute-only, impractical)'}",
            "network-level aggregates only; no exchange segmentation possible at this source",
        ],
    }
    write_parquet_with_meta(df, out_path, meta, DS["blockchain_com"], "High")
    return PhaseResult(
        phase="Phase 1", name="Network-level on-chain volume (Blockchain.com)", asset="BTC",
        status="COMPLETE",
        output=f"{out_path} ({len(df):,} rows)",
        coverage=f"{df['date'].min().date()} -> {df['date'].max().date()}, {len(df):,} days",
        confidence="High as a network-level volume proxy (keyless official chart API)",
        limitations=[
            "estimated transaction volume includes non-trading transfers",
            "mempool-size minute granularity — daily mean; dropped if impractical",
            *(notes or ["none"]),
        ],
        outputs=[out_path])


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Phase 1 BTC: Blockchain.com network metrics")
    ap.add_argument("--force-refetch", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    res = run(force_refetch=args.force_refetch)
    from .common import append_log
    append_log(res.phase, res.name, res.asset, res.status, res.output,
               res.coverage, res.confidence, res.limitations)
    print("\n=== PHASE 1 BTC DONE ===")


if __name__ == "__main__":
    main()
