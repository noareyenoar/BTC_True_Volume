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
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

try:
    from .common import (PROCESSED, RAW, DS, PhaseResult, RateLimitedClient,
                         atomic_write_json, atomic_write_parquet,
                         cache_json, load_cached_json, logger,
                         write_parquet_with_meta)
    from .fetch_checkonchain_urpd import decode_arr, parse_traces
except ImportError:  # direct script execution
    from common import (PROCESSED, RAW, DS, PhaseResult, RateLimitedClient,
                        atomic_write_json, atomic_write_parquet,
                        cache_json, load_cached_json, logger,
                        write_parquet_with_meta)
    from fetch_checkonchain_urpd import decode_arr, parse_traces

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

# ---------------------------------------------------------------------------
# Fallback: on-chain settled value from checkonchain
# ---------------------------------------------------------------------------
# Blockchain.com's chart API freezes for days at a time (measured 2026-10-15:
# every chart stopped at 15/10 while Vision was current through 17/10).  The
# checkonchain "Bitcoin Transfer Volume [USD]" chart is the same economic
# settled-value family, keyless, and current to the day -- it is a self-
# contained Plotly page on the CDN the URPD collector already reads, so the
# decode path is the one proven in fetch_checkonchain_urpd.
CC_TRANSFER_CHART = ("https://charts-cdn.checkonchain.com/btconchain/adoption/"
                     "transfer_volumes_usd/transfer_volumes_usd_light.html")
CC_TRANSFER_TRACE = "Transfer Volume [USD]"
CC_CACHE_REL = Path("checkonchain") / "transfer_volumes_usd_light.html"
# Value guard: daily network settled value is ~$1e9; anything outside this
# band means a mis-decoded trace or a changed chart, not a market move.
CC_VALUE_MIN, CC_VALUE_MAX = 1e7, 1e11


def _load_cc_html(client: RateLimitedClient, force_refetch: bool) -> Optional[str]:
    """Fetch (or reuse) the checkonchain chart page.  Cache is the fallback."""
    cache_path = RAW / CC_CACHE_REL
    if not force_refetch and cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    resp = client.get(CC_TRANSFER_CHART)
    if resp is not None and resp.status_code == 200 and len(resp.text) > 10_000:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        tmp.write_text(resp.text, encoding="utf-8")
        os.replace(tmp, cache_path)
        return resp.text
    if cache_path.exists():
        logger.warning("checkonchain transfer volume: fetch failed, using cached HTML")
        return cache_path.read_text(encoding="utf-8")
    return None


def fetch_transfer_volume_fallback(
        client: RateLimitedClient,
        force_refetch: bool = False) -> Optional[pd.DataFrame]:
    """Daily BTC transfer volume [USD] from checkonchain -> date/value frame.

    The chart's last point is the current (partial) UTC day; it is dropped so
    the fallback only ever answers with complete days.  The trace name is
    matched exactly -- the same page carries Price and Exchange Flows traces.
    """
    html = _load_cc_html(client, force_refetch)
    if html is None:
        logger.warning("checkonchain transfer volume: no page and no cache")
        return None
    traces = parse_traces(html)
    trace = next((t for t in traces if t.get("name") == CC_TRANSFER_TRACE), None)
    if trace is None:
        logger.warning("checkonchain transfer volume: trace %r not found (have %r)",
                       CC_TRANSFER_TRACE, [t.get("name") for t in traces])
        return None
    x = trace.get("x") or []
    y = decode_arr(trace.get("y"))
    if y is None:
        logger.warning("checkonchain transfer volume: y not decodable")
        return None
    if len(x) != len(y):
        logger.warning("checkonchain transfer volume: x/y length mismatch %d/%d",
                       len(x), len(y))
        return None
    df = pd.DataFrame({
        "date": pd.to_datetime(pd.Series(x), utc=True).dt.normalize(),
        "transfer_volume_usd": pd.to_numeric(pd.Series(y), errors="coerce"),
    })
    df = (df.dropna(subset=["date", "transfer_volume_usd"])
            .drop_duplicates("date", keep="last")
            .sort_values("date"))
    today = pd.Timestamp.now(tz="UTC").normalize()
    df = df[(df["date"] < today) & (df["transfer_volume_usd"] > 0)]  # complete days only
    if len(df) < 1000:
        logger.warning("checkonchain transfer volume: only %d usable days", len(df))
        return None
    # Guard the recent window, not 2009 (when settled value was legitimately
    # far below $1e7): a mis-decoded trace or a changed chart shows up here.
    recent = df[df["date"] >= df["date"].max() - pd.Timedelta(days=800)]
    valid = recent["transfer_volume_usd"].between(CC_VALUE_MIN, CC_VALUE_MAX)
    if valid.mean() < 0.95:
        logger.warning("checkonchain transfer volume: %.1f%% of the last 800 days "
                       "outside guard", 100 * (1 - valid.mean()))
        return None
    df = df[df["transfer_volume_usd"] <= CC_VALUE_MAX].reset_index(drop=True)
    df["source"] = "checkonchain"
    df["fetched_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logger.info("checkonchain transfer volume: %d days, %s -> %s",
                len(df), df["date"].min().date(), df["date"].max().date())
    return df


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

    # Fallback store: checkonchain settled value, kept beside (not inside) the
    # primary so build_final can fill only the days Blockchain.com missed.
    outputs = [out_path]
    try:
        fb = fetch_transfer_volume_fallback(client, force_refetch)
    except Exception as exc:  # noqa: BLE001 — a fallback must not kill the phase
        fb = None
        notes.append(f"checkonchain transfer volume fallback: {type(exc).__name__}: {exc}")
        logger.warning("checkonchain transfer volume failed: %s", exc)
    if fb is not None and len(fb):
        fb_path = out_path.parent / "transfer_volume_usd_checkonchain.parquet"
        atomic_write_parquet(fb, fb_path, {
            "data_source": DS["checkonchain_charts"],
            "confidence_tier": "High",
            "dataset_id": "checkonchain_transfer_volume_usd_btc_daily",
        })
        atomic_write_json({
            "dataset_id": "checkonchain_transfer_volume_usd_btc_daily",
            "description": "Bitcoin transfer volume [USD] from the checkonchain "
                           "transfer_volumes_usd chart — fallback for Blockchain.com's "
                           "estimated-transaction-volume-usd when that chart is frozen",
            "source_url": CC_TRANSFER_CHART,
            "trace": CC_TRANSFER_TRACE,
            "coverage": [str(fb["date"].min().date()), str(fb["date"].max().date())],
            "row_count": int(len(fb)),
            "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_source": DS["checkonchain_charts"],
            "confidence_tier": "High",
        }, fb_path.with_name("transfer_volume_usd_checkonchain_meta.json"))
        outputs.append(fb_path)
    else:
        notes.append("checkonchain transfer volume fallback: unavailable")

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
        outputs=outputs)


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
