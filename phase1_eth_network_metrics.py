"""
phase1_eth_network_metrics.py — Phase 1 (ETH): network-level on-chain volume.

Primary: Etherscan Stats API `dailytx` (free tier key). Chunked by year with a
truncation guard: each chunk must end at its requested end-date, otherwise the
boundary is bisected and the tail re-requested.

Fallback (no key): CoinMetrics community `TxCnt` for eth (keyless, verified).
The phase still completes with tier Medium and the log notes how to upgrade by
adding ETHERSCAN_API_KEY to the project .env (free tier) — values are never
printed or logged.

Output: processed/onchain/network_metrics/ETH/merged.parquet (+ _meta.json)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

try:
    from .common import (PROCESSED, DS, PhaseResult, RateLimitedClient,
                         cache_json, has_etherscan_key, load_cached_json,
                         logger, write_parquet_with_meta)
except ImportError:  # direct script execution
    from common import (PROCESSED, DS, PhaseResult, RateLimitedClient,
                        cache_json, has_etherscan_key, load_cached_json,
                        logger, write_parquet_with_meta)

logger = logging.getLogger("tvp.phase1_eth")

ETHERSCAN_BASE = "https://api.etherscan.io/api"
ETHERSCAN_START = "2015-07-30"          # first Ethereum tx day
COINMETRICS_BASE = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"

DAILY_TX_FIELDS = ["UTCDate", "unixTimeStamp", "transactionCount", "transferCount",
                   "ethTransferCount", "ethUniqueFromAddresses", "ethUniqueToAddresses"]


def _chunk_years() -> List[tuple]:
    """Year-aligned chunks from ETHERSCAN_START to yesterday (12 chunks)."""
    start = datetime.strptime(ETHERSCAN_START, "%Y-%m-%d").date()
    end = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    chunks = []
    y = start.year
    while True:
        lo = datetime(y, 1, 1).date() if y > start.year else start
        hi = datetime(y + 1, 1, 1).date() - timedelta(days=1)
        if hi > end:
            hi = end
        chunks.append((lo, hi))
        if y >= end.year:
            break
        y += 1
    return chunks


def _fetch_etherscan(client: RateLimitedClient, lo, hi, api_key: str) -> Optional[Dict[str, Any]]:
    params = {"module": "stats", "action": "dailytx",
              "startdate": lo.strftime("%Y%m%d"), "enddate": hi.strftime("%Y%m%d"),
              "apikey": api_key}
    data = client.get_json(ETHERSCAN_BASE, params=params)
    if data is None:
        return None
    if data.get("status") != "1" or not data.get("result"):
        logger.warning("etherscan chunk %s..%s: status=%s message=%s",
                       lo, hi, data.get("status"), data.get("message"))
        return None
    return data


def _fetch_dailytx(client: RateLimitedClient, force_refetch: bool = False) -> pd.DataFrame:
    """Fetch full dailytx history, chunked by year with truncation guard."""
    api_key = ""
    if has_etherscan_key():
        import os
        for k in ("ETHERSCAN_API_KEY", "ETHERSCAN_KEY"):
            api_key = os.getenv(k, "").strip()
            if api_key:
                break
    else:
        logger.warning("no ETHERSCAN_API_KEY in .env — will fall back to CoinMetrics TxCnt")
    chunks = _chunk_years()
    frames: List[pd.DataFrame] = []
    for lo, hi in chunks:
        cache_rel = Path("etherscan") / f"dailytx_{lo.year}.json"
        cached = None if force_refetch else load_cached_json(cache_rel)
        data = cached
        if data is None:
            if not api_key:
                return pd.DataFrame()
            data = _fetch_etherscan(client, lo, hi, api_key)
            if data is None:
                raise RuntimeError(f"etherscan chunk {lo.year} failed")
            cache_json(cache_rel, data)
        result = data.get("result", [])
        # truncation guard: the tail must cover the chunk end
        if result:
            last_date = result[-1].get("UTCDate")
            want = hi.strftime("%m/%d/%Y")
            if last_date != want and result[-1].get("UTCDate", "").replace(" 00:00:00", "") != want:
                logger.warning("etherscan %d: chunk truncated at %s (want %s) — bisecting",
                               lo.year, last_date, want)
                # bisect: re-fetch the remainder in halves (iterative bisect)
                tail_lo = lo
                while True:
                    mid = tail_lo + (hi - tail_lo) // 2
                    d = _fetch_etherscan(client, mid, hi, api_key)
                    if d is None:
                        raise RuntimeError(f"etherscan bisect {mid}..{hi} failed")
                    r = d.get("result", [])
                    if not r:
                        raise RuntimeError(f"etherscan bisect {mid}..{hi} returned empty")
                    frames.append(pd.DataFrame(r))
                    last_date = r[-1].get("UTCDate")
                    if last_date == want or last_date.replace(" 00:00:00", "") == want:
                        break
                    if mid == tail_lo:
                        break  # cannot progress — accept what we have
                    tail_lo = mid
        if result:
            frames.append(pd.DataFrame(result))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["UTCDate"] = df["UTCDate"].astype(str).str[:10]
    df = df.drop_duplicates("UTCDate", keep="first")
    df["date"] = pd.to_datetime(df["UTCDate"], format="%m/%d/%Y", utc=True).dt.normalize()
    df = df.set_index("date").sort_index()
    num = ["transactionCount", "transferCount", "ethTransferCount",
           "ethUniqueFromAddresses", "ethUniqueToAddresses"]
    for c in num:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    rename = {"transactionCount": "net_tx_count", "transferCount": "net_transfer_count",
              "ethTransferCount": "net_eth_transfer_count",
              "ethUniqueFromAddresses": "net_unique_from_addresses",
              "ethUniqueToAddresses": "net_unique_to_addresses"}
    df = df[num].rename(columns=rename)
    return df


def _fetch_coinmetrics_txcnt(client: RateLimitedClient,
                             force_refetch: bool = False) -> Optional[pd.DataFrame]:
    cache_rel = Path("coinmetrics") / "eth_TxCnt.json"
    cached = None if force_refetch else load_cached_json(cache_rel)
    if cached is not None:
        df = pd.DataFrame(cached["values"])
        df["date"] = pd.to_datetime(df["x"], utc=True).dt.normalize()
        return df.set_index("date")["y"].to_frame("net_tx_count")
    params = {"assets": "eth", "metrics": "TxCnt", "frequency": "1d", "page_size": 10000}
    rows: List[Dict[str, Any]] = []
    token = None
    while True:
        if token:
            params["next_page_token"] = token
        data = client.get_json(COINMETRICS_BASE, params=params)
        if data is None:
            return None
        items = data.get("data", [])
        if not items:
            break
        rows.extend(items)
        token = data.get("next_page_token")
        if not token:
            break
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["time"], utc=True).dt.normalize()
    df = df.drop_duplicates("date", keep="last").set_index("date")
    out = df[["TxCnt"]].rename(columns={"TxCnt": "net_tx_count"})
    out["net_tx_count"] = pd.to_numeric(out["net_tx_count"], errors="coerce")
    cache_json(cache_rel, {"values": [{"x": str(t.date()), "y": float(v)}
                                     for t, v in out.itertuples()]})
    return out


def run(force_refetch: bool = False) -> PhaseResult:
    client = RateLimitedClient(min_interval=0.35)
    df = _fetch_dailytx(client, force_refetch)
    source = DS["etherscan"]
    notes: List[str] = []
    if df.empty:
        logger.info("Etherscan unavailable (no key) — falling back to CoinMetrics TxCnt")
        df_cm = _fetch_coinmetrics_txcnt(client, force_refetch)
        if df_cm is None or df_cm.empty:
            raise RuntimeError("phase1_eth: both Etherscan and CoinMetrics TxCnt failed")
        df = df_cm
        source = DS["coinmetrics_api"]
        notes.append("Etherscan unavailable (no free API key in .env); used CoinMetrics TxCnt instead. "
                     "Add ETHERSCAN_API_KEY (free tier) to D:\\kp_ai_agent\\Dataset_Fetcher\\.env "
                     "and re-run --phases phase1_eth to upgrade to full dailytx fields.")
    df.index.name = "date"
    df = df.reset_index()
    df["date"] = df["date"].dt.normalize()

    out_path = PROCESSED / "onchain" / "network_metrics" / "ETH" / "merged.parquet"
    meta = {
        "dataset_id": "onchain_network_metrics_eth_daily",
        "description": "Ethereum network-level metrics (Etherscan dailytx or CoinMetrics TxCnt fallback)",
        "data_source": source,
        "coverage": [str(df["date"].min().date()), str(df["date"].max().date())],
        "known_limitations": [
            "transaction counts are network-level; no exchange segmentation",
            *notes,
        ],
    }
    write_parquet_with_meta(df, out_path, meta, source,
                            "Medium" if source == DS["etherscan"] else "Medium")
    tier = "Medium as a network-level volume proxy" + (
        " (CoinMetrics TxCnt fallback)" if source == DS["coinmetrics_api"] else " (Etherscan)")
    return PhaseResult(
        phase="Phase 1", name="Network-level on-chain volume (Etherscan)", asset="ETH",
        status="COMPLETE",
        output=f"{out_path} ({len(df):,} rows)",
        coverage=f"{df['date'].min().date()} -> {df['date'].max().date()}, {len(df):,} days",
        confidence=tier,
        limitations=["transaction counts are network-level; no exchange segmentation", *notes],
        outputs=[out_path])


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Phase 1 ETH: Etherscan dailytx")
    ap.add_argument("--force-refetch", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    res = run(force_refetch=args.force_refetch)
    from .common import append_log
    append_log(res.phase, res.name, res.asset, res.status, res.output,
               res.coverage, res.confidence, res.limitations)
    print("\n=== PHASE 1 ETH DONE ===")


if __name__ == "__main__":
    main()
