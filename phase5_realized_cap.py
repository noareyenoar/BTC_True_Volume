"""
phase5_realized_cap.py — Phase 5: realized cap (CoinMetrics, free tier).

CapRealUSD is NOT on the CoinMetrics free tier (verified 403). Per the plan the
realized cap is DERIVED from free metrics, exactly as the existing
onchain_dataset does:

    rc_realized_cap_usd   = CapMrktCurUSD / CapMVRVCur
    rc_realized_price_usd = rc_realized_cap_usd / SplyCur
    rc_mvrv_ratio         = CapMrktCurUSD / rc_realized_cap_usd   (== CapMVRVCur)

Also collected (forward-signal value, feeds Phase 7's doc-only argument):
SplyExNtv, FlowInExNtv, FlowOutExNtv, PriceUSD (+ TxCnt for eth, reused as the
ETH network-metric upgrade path).

BTC cross-check vs the local onchain_dataset (2020 -> 2026-07-14, same
derivation): |rc_realized_cap - onch_realized_price * supply| / value < 2%.

Output: processed/onchain/realized_cap/{BTC,ETH}/merged.parquet (Medium)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

try:
    from .common import (COINMETRICS_ASSETS, ONCHAIN_LOCAL, PROCESSED, DS,
                         PhaseResult, RateLimitedClient, append_log, cache_json,
                         load_cached_json, logger, write_parquet_with_meta)
except ImportError:  # direct script execution
    from common import (COINMETRICS_ASSETS, ONCHAIN_LOCAL, PROCESSED, DS,
                        PhaseResult, RateLimitedClient, append_log, cache_json,
                        load_cached_json, logger, write_parquet_with_meta)

logger = logging.getLogger("tvp.phase5")

CM_BASE = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
METRICS = ["CapMrktCurUSD", "CapMVRVCur", "SplyCur", "SplyExNtv",
           "FlowInExNtv", "FlowOutExNtv", "PriceUSD"]
ETH_EXTRA = ["TxCnt"]


def _fetch_asset_metrics(client: RateLimitedClient, asset: str, metrics: List[str],
                         force_refetch: bool = False) -> pd.DataFrame:
    cache_rel = Path("coinmetrics") / f"{asset}_phase5.json"
    cached = None if force_refetch else load_cached_json(cache_rel)
    if cached is not None:
        df = pd.DataFrame(cached["values"])
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
        return df.set_index("date").sort_index()
    params = {"assets": asset, "metrics": ",".join(metrics),
              "frequency": "1d", "page_size": 10000}
    rows: List[Dict[str, Any]] = []
    token = None
    while True:
        if token:
            params["next_page_token"] = token
        data = client.get_json(CM_BASE, params=params)
        if data is None:
            raise RuntimeError(f"phase5: CoinMetrics {asset} request failed")
        items = data.get("data", [])
        if not items:
            break
        rows.extend(items)
        token = data.get("next_page_token")
        if not token:
            break
    if not rows:
        raise RuntimeError(f"phase5: CoinMetrics {asset} returned no rows")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["time"], utc=True).dt.normalize()
    df = df.drop_duplicates("date", keep="last").set_index("date").sort_index()
    # strip CoinMetrics "-status" flash fields; keep only requested metrics
    keep = [c for c in df.columns if c in metrics]
    df = df[keep].apply(pd.to_numeric, errors="coerce")
    cache_json(cache_rel, {"values": [{"date": str(d.date()), **{c: (None if pd.isna(r[c]) else float(r[c]))
                                                                for c in keep}}
                                      for d, r in df.iterrows()]})
    return df


def cross_check_local(df: pd.DataFrame) -> Dict[str, Any]:
    """Compare rc_realized_price_usd vs the local onchain_dataset's realized_price.

    The local store has realized_price but no supply column, so the plan's
    check (|rc_realized_cap - onch_realized_price * supply| / value < 2%) is
    applied in its divided-by-supply form: realized_price directly. Both were
    derived from the same CoinMetrics CapMrktCurUSD/CapMVRVCur pair — expect
    ~0% deviation; >2% means a bug in one derivation.
    """
    out = {"done": False}
    if not ONCHAIN_LOCAL.exists():
        logger.warning("phase5: local onchain store missing — cross-check skipped")
        return out
    onch = pd.read_parquet(ONCHAIN_LOCAL)
    if "realized_price" not in onch.columns:
        logger.warning("phase5: local onchain lacks realized_price — cross-check skipped")
        return out
    onch = onch.copy()
    onch["date"] = pd.to_datetime(onch["timestamp"], utc=True).dt.normalize()
    onch = onch.set_index("date").sort_index()
    local_price = onch.groupby(level="date")["realized_price"].last().astype(float)
    both = pd.concat([df["rc_realized_price_usd"], local_price], axis=1, join="inner").dropna()
    if len(both) < 10:
        logger.warning("phase5: cross-check overlap too small (%d days)", len(both))
        return out
    rel = (both.iloc[:, 0] - both.iloc[:, 1]).abs() / both.iloc[:, 0]
    out.update({"done": True, "days": len(both), "max_rel_diff": float(rel.max()),
                "mean_rel_diff": float(rel.mean()),
                "pass_2pct": bool(rel.max() < 0.02),
                "overlap": [str(both.index.min().date()), str(both.index.max().date())]})
    logger.info("phase5 cross-check: %s", out)
    return out


def run(force_refetch: bool = False) -> PhaseResult:
    client = RateLimitedClient(min_interval=0.62)   # 10 req / 6s
    results = []
    for asset in ("BTC", "ETH"):
        cm = COINMETRICS_ASSETS[asset]
        metrics = list(METRICS) + (ETH_EXTRA if asset == "ETH" else [])
        df = _fetch_asset_metrics(client, cm, metrics, force_refetch)
        df["rc_realized_cap_usd"] = df["CapMrktCurUSD"] / df["CapMVRVCur"].replace(0, np.nan)
        df["rc_realized_price_usd"] = df["rc_realized_cap_usd"] / df["SplyCur"].replace(0, np.nan)
        df["rc_mvrv_ratio"] = df["CapMrktCurUSD"] / df["rc_realized_cap_usd"].replace(0, np.nan)
        df["rc_net_exchange_flow"] = df["FlowInExNtv"] - df["FlowOutExNtv"]
        cols = ["rc_realized_cap_usd", "rc_realized_price_usd", "rc_mvrv_ratio",
                "rc_net_exchange_flow", "rc_exchange_supply", "rc_market_cap_usd",
                "rc_price_usd"]
        out = pd.DataFrame(index=df.index)
        out["rc_realized_cap_usd"] = df["rc_realized_cap_usd"]
        out["rc_realized_price_usd"] = df["rc_realized_price_usd"]
        out["rc_mvrv_ratio"] = df["rc_mvrv_ratio"]
        out["rc_net_exchange_flow"] = df["rc_net_exchange_flow"]
        out["rc_exchange_supply"] = df["SplyExNtv"]
        out["rc_market_cap_usd"] = df["CapMrktCurUSD"]
        out["rc_price_usd"] = df["PriceUSD"]
        if asset == "ETH":
            # rc_ prefix keeps it inside the rc block namespace — the net block
            # (phase1_eth) already carries net_tx_count; duplicating the name
            # would collide in build_final's outer join
            out["rc_tx_count"] = df["TxCnt"]
        out.index.name = "date"
        # identity: rc_mvrv_ratio ≈ market_cap / realized_cap
        ident = (out["rc_mvrv_ratio"] - out["rc_market_cap_usd"] / out["rc_realized_cap_usd"].replace(0, np.nan)).abs().max()
        if not np.isfinite(ident) or ident > 1e-6:
            logger.warning("phase5 %s: mvrv identity deviation %.3g", asset, ident)
        xcheck = cross_check_local(out) if asset == "BTC" else {"done": False}
        out_path = PROCESSED / "onchain" / "realized_cap" / asset / "merged.parquet"
        meta = {
            "dataset_id": f"onchain_realized_cap_{asset.lower()}_daily",
            "description": f"{asset} realized cap derived from CoinMetrics free metrics "
                           "(CapMrktCurUSD/CapMVRVCur — same derivation as onchain_dataset)",
            "data_source": DS["coinmetrics_derived"],
            "coverage": [str(out.index.min().date()), str(out.index.max().date())],
            "derivation": "rc_realized_cap_usd = CapMrktCurUSD / CapMVRVCur; "
                          "rc_realized_price_usd = rc_realized_cap_usd / SplyCur; "
                          "rc_mvrv_ratio = CapMrktCurUSD / rc_realized_cap_usd",
            "cross_check_vs_local_onchain": xcheck,
            "known_limitations": [
                "aggregate cap only — no price-bucket / LTH-STH split (CapRealUSD requires paid tier)",
                "CapMrktCurUSD is circulating-market-cap, includes staked/locked coins",
                "FlowInExNtv/FlowOutExNtv are exchange-wallet flow estimates, not audited tapes",
            ],
        }
        write_parquet_with_meta(out, out_path, meta, DS["coinmetrics_derived"], "Medium")
        x = "" if not xcheck.get("done") else (f"; cross-check {xcheck['days']}d max {xcheck['max_rel_diff']:.2%}"
                                               + (" PASS" if xcheck["pass_2pct"] else " FAIL"))
        results.append(PhaseResult(
            phase="Phase 5", name="Realized cap (CoinMetrics derived)", asset=asset,
            status="COMPLETE",
            output=f"{out_path} ({len(out):,} rows)",
            coverage=f"{out.index.min().date()} -> {out.index.max().date()}, {len(out):,} days{x}",
            confidence="Medium as a coin-level realized-value proxy (derived, free tier)",
            limitations=[
                "aggregate cap only — no LTH/STH split",
                "derived from CapMrktCurUSD/CapMVRVCur (CapRealUSD needs paid tier)",
            ],
            outputs=[out_path]))
    return PhaseResult(
        phase="Phase 5", name="Realized cap (CoinMetrics derived)", asset="BOTH",
        status="COMPLETE",
        output="; ".join(r.output for r in results),
        coverage="; ".join(r.coverage for r in results),
        confidence="Medium as a coin-level realized-value proxy (derived, free tier)",
        limitations=[l for r in results for l in r.limitations],
        outputs=[p for r in results for p in r.outputs])


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Phase 5: realized cap via CoinMetrics")
    ap.add_argument("--force-refetch", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    res = run(force_refetch=args.force_refetch)
    append_log(res.phase, res.name, res.asset, res.status, res.output,
               res.coverage, res.confidence, res.limitations)
    print("\n=== PHASE 5 DONE ===")


if __name__ == "__main__":
    main()
