"""
build_final.py — merge all phase outputs into the FINAL deliverable.

Per symbol (BTC, ETH): outer join on UTC date of:
  exch_*  processed/exchange_true_volume/{SYM}/merged.parquet      (Step 0)
  net_*   processed/onchain/network_metrics/{ASSET}/merged.parquet (Phase 1)
  rc_*    processed/onchain/realized_cap/{ASSET}/merged.parquet    (Phase 5)
  onch_*  BTC: local onchain_dataset downsampled daily; ETH: Phase-5 flow aliases
  pos_*   processed/futures/positioning/{SYM}/merged.parquet       (Phase 2)
  pos_options_* processed/options/verification/{ASSET}/merged.parquet (Phase 3)

Every block carries explicit *_{data_source,confidence_tier} string columns so
provenance is readable without file metadata (plan guardrail: a proxy metric
is never presented as literal true volume).

Output: processed/true_volume/{ASSET}/merged.parquet (+ _meta.json)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from .common import (ASSETS, KLINE_STORE, ONCHAIN_LOCAL, PROCESSED, SYMBOLS,
                         DS, PhaseResult, append_log, logger,
                         write_parquet_with_meta)
except ImportError:  # direct script execution
    from common import (ASSETS, KLINE_STORE, ONCHAIN_LOCAL, PROCESSED, SYMBOLS,
                        DS, PhaseResult, append_log, logger,
                        write_parquet_with_meta)

logger = logging.getLogger("tvp.final")

EXCH_SUBDIR = PROCESSED / "exchange_true_volume"
NET_SUBDIR = PROCESSED / "onchain" / "network_metrics"
RC_SUBDIR = PROCESSED / "onchain" / "realized_cap"
POS_SUBDIR = PROCESSED / "futures" / "positioning"
OPT_SUBDIR = PROCESSED / "options" / "verification"


def _meta_of(parquet_path: Path) -> Dict[str, Any]:
    mj = parquet_path.with_name("_meta.json")
    if mj.exists():
        return json.loads(mj.read_text(encoding="utf-8"))
    return {}


def _load_block(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        logger.warning("missing block: %s", path)
        return None
    df = pd.read_parquet(path)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
    else:
        df = df.reset_index()  # date was the index
        if "date" not in df.columns:
            # unnamed index lands as "index" — a named date index that the
            # writer dropped; treat any non-data column named after the index
            if "index" in df.columns:
                df = df.rename(columns={"index": "date"})
            else:
                raise RuntimeError(f"{path}: no date column and no recoverable index")
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
    df = df.drop_duplicates("date", keep="last").set_index("date")
    return df.sort_index()


def _merge_settled_fallback(df_n: pd.DataFrame, asset: str) -> pd.DataFrame:
    """Fill Blockchain.com's settled-value holes with checkonchain transfer volume.

    The primary column is kept untouched; ``net_settled_volume_usd`` is the
    per-date union and ``net_settled_volume_source`` names the provider for
    each day, so a fallback day is auditable rather than a silent splice.
    """
    if "net_estimated_tx_volume_usd" not in df_n.columns:
        return df_n
    primary = pd.to_numeric(df_n["net_estimated_tx_volume_usd"], errors="coerce")
    settled = primary.copy()
    source = pd.Series(np.where(primary.notna(), "blockchain_com", None),
                       index=df_n.index, dtype="object")
    fb_path = NET_SUBDIR / asset / "transfer_volume_usd_checkonchain.parquet"
    if fb_path.exists():
        fb = pd.read_parquet(fb_path)
        fb["date"] = pd.to_datetime(fb["date"], utc=True).dt.normalize()
        fb = (fb.drop_duplicates("date", keep="last").set_index("date")
                ["transfer_volume_usd"].astype(float).sort_index())
        fill = fb.reindex(df_n.index)
        need = settled.isna() & fill.notna()
        settled = settled.where(~need, fill)
        source = source.where(~need, "checkonchain")
        logger.info("settled fallback: %d primary days, %d filled from checkonchain "
                    "(%s -> %s)", int(primary.notna().sum()), int(need.sum()),
                    fb.index.min().date() if len(fb) else "-",
                    fb.index.max().date() if len(fb) else "-")
    else:
        logger.warning("settled fallback: %s missing", fb_path)
    df_n = df_n.copy()
    df_n["net_settled_volume_usd"] = settled
    df_n["net_settled_volume_source"] = source
    return df_n


def _onch_block_btc() -> pd.DataFrame:
    """BTC on-chain block from the local onchain_dataset (daily .last())."""
    onch = pd.read_parquet(ONCHAIN_LOCAL)
    onch["date"] = pd.to_datetime(onch["timestamp"], utc=True).dt.normalize()
    onch = onch.set_index("date").sort_index()
    cols = {"exchange_reserve_btc": "onch_exchange_reserve",
            "exchange_inflow_btc": "onch_exchange_inflow",
            "exchange_outflow_btc": "onch_exchange_outflow",
            "realized_price": "onch_realized_price",
            "market_price": "onch_market_price",
            "sopr": "onch_sopr",
            "estimated_leverage_ratio": "onch_estimated_leverage_ratio"}
    out = onch[list(cols)].rename(columns=cols).astype(float)
    out = out.groupby(level="date").last()
    logger.info("onch BTC: %d days (%s -> %s)", len(out),
                out.index.min().date(), out.index.max().date())
    return out


def _onch_block_eth(rc: pd.DataFrame) -> pd.DataFrame:
    """ETH on-chain block aliased from Phase-5 CoinMetrics flows."""
    out = pd.DataFrame(index=rc.index)
    out["onch_exchange_net_flow"] = rc["rc_net_exchange_flow"]
    out["onch_exchange_supply"] = rc["rc_exchange_supply"]
    return out


def build(asset: str) -> PhaseResult:
    sym = next(s for s in SYMBOLS if ASSETS[s] == asset)
    blocks: Dict[str, pd.DataFrame] = {}
    sources: Dict[str, Dict[str, str]] = {}

    exch_path = EXCH_SUBDIR / sym / "merged.parquet"
    df_e = _load_block(exch_path)
    if df_e is not None:
        blocks["exch"] = df_e
        m = _meta_of(exch_path)
        sources["exch"] = {"data_source": m.get("data_source", "unknown"),
                           "confidence_tier": m.get("confidence_tier", "unknown")}

    net_path = NET_SUBDIR / asset / "merged.parquet"
    df_n = _load_block(net_path)
    if df_n is not None:
        blocks["net"] = df_n
        m = _meta_of(net_path)
        sources["net"] = {"data_source": m.get("data_source", "unknown"),
                          "confidence_tier": m.get("confidence_tier", "unknown")}

    rc_path = RC_SUBDIR / asset / "merged.parquet"
    df_rc = _load_block(rc_path)
    if df_rc is not None:
        blocks["rc"] = df_rc
        m = _meta_of(rc_path)
        sources["rc"] = {"data_source": m.get("data_source", "unknown"),
                         "confidence_tier": m.get("confidence_tier", "unknown")}

    pos_path = POS_SUBDIR / sym / "merged.parquet"
    df_pos = _load_block(pos_path)
    if df_pos is not None:
        blocks["pos"] = df_pos
        m = _meta_of(pos_path)
        sources["pos"] = {"data_source": m.get("data_source", "unknown"),
                          "confidence_tier": m.get("confidence_tier", "unknown")}

    opt_path = OPT_SUBDIR / asset / "merged.parquet"
    df_opt = _load_block(opt_path)
    if df_opt is not None:
        blocks["pos_options"] = df_opt
        m = _meta_of(opt_path)
        sources["pos_options"] = {"data_source": m.get("data_source", "unknown"),
                                  "confidence_tier": m.get("confidence_tier", "unknown")}

    if asset == "BTC":
        onch = _onch_block_btc()
        onch_src = {"data_source": DS["onchain_local"], "confidence_tier": "Medium"}
    elif "rc" in blocks:
        onch = _onch_block_eth(blocks["rc"])
        onch_src = {"data_source": DS["coinmetrics_derived"], "confidence_tier": "Medium"}
    else:
        onch = pd.DataFrame()
        onch_src = {"data_source": "missing", "confidence_tier": "N/A"}
    if len(onch):
        blocks["onch"] = onch
        sources["onch"] = onch_src

    if not blocks:
        raise RuntimeError("build_final: no phase outputs available")

    df = None
    for name, blk in blocks.items():
        df = blk if df is None else df.join(blk, how="outer")
    df.index.name = "date"
    df = df.sort_index()

    # explicit per-block provenance columns (guardrail)
    for name, src in sources.items():
        prefix = name if name != "exch" else "exch"
        df[f"{prefix}_data_source"] = src["data_source"]
        df[f"{prefix}_confidence_tier"] = src["confidence_tier"]

    # reindex to the full calendar so gaps are explicit NaN, not missing rows
    full = pd.date_range(df.index.min(), df.index.max(), freq="1D", tz="UTC")
    df = df.reindex(full)
    df.index.name = "date"

    # Settled-value union AFTER the calendar reindex: the net block only reaches
    # Blockchain.com's last published day, while the checkonchain fallback can
    # be fresher than every other block.
    df = _merge_settled_fallback(df, asset)

    # --- sanity -------------------------------------------------------------
    numeric = df.select_dtypes("number")
    # signed by construction: pos_*_skew (long_pct - 0.5 in [-0.5, 0.5]) and
    # *net_exchange_flow (FlowInExNtv - FlowOutExNtv, net outflow is negative)
    signed = [c for c in numeric.columns
              if "skew" in c or "net_exchange_flow" in c or "net_flow" in c]
    neg = (numeric.drop(columns=signed, errors="ignore") < -1e-9).sum().sum()
    assert neg == 0, f"negative values in final frame ({neg})"
    for p in ("exch", "net", "rc", "pos"):
        c = f"{p}_data_source"
        if c in df.columns:
            assert df[c].notna().all(), f"{c} has NaN"
            assert df[c].iloc[0] != "unknown", f"{c} unresolved"
    # ratios in [0,1] where present — only taker-buy ratios are bounded;
    # ls_ratio and put_call_*_ratio are unbounded by construction
    ratio_cols = [c for c in df.columns if "taker_buy_ratio" in c]
    if ratio_cols:
        bad = df[ratio_cols].dropna().apply(lambda s: ((s < 0) | (s > 1)).sum()).sum()
        assert bad == 0, f"{bad} taker-buy ratio values outside [0,1]"

    out_path = PROCESSED / "true_volume" / asset / "merged.parquet"
    blocks_meta = {k: (v.get("data_source"), v.get("confidence_tier"))
                   for k, v in sources.items()}
    meta = {
        "dataset_id": f"true_volume_{asset.lower()}_daily",
        "description": f"{asset} daily true-volume proxy stack — exchange tape volume, "
                       f"network metrics, realized cap, positioning, options verification",
        "data_source": "merged multi-source (see blocks_meta)",
        "confidence_tier": "per-block (see *_data_source / *_confidence_tier columns)",
        "blocks_meta": blocks_meta,
        "coverage": [str(df.index.min().date()), str(df.index.max().date())],
        "row_count": int(len(df)),
        "known_limitations": [
            "every metric is a PROXY — never literal true volume; per-block tier labels apply",
            "spot klines stale after 2024-12-31 (exch_coverage_mode flags futures_only days)",
            "onch block ends 2026-07-14 (local onchain_dataset) — NaN after for BTC",
            "pos block: top-trader ratios are the top-20% cohort sentiment proxy, not exchange-wide OI",
            "pos_options block: single-snapshot verification (2026-08-14) — NaN elsewhere",
            "net block BTC includes non-trading transfers (settled-value proxy)",
        ],
    }
    write_parquet_with_meta(df, out_path, meta, meta["data_source"], "per-block")
    mode_counts = df["exch_coverage_mode"].value_counts().to_dict() if "exch_coverage_mode" in df else {}
    return PhaseResult(
        phase="final", name="Merged true-volume deliverable", asset=asset,
        status="COMPLETE",
        output=f"{out_path} ({len(df):,} rows, {len(df.columns)} cols)",
        coverage=f"{df.index.min().date()} -> {df.index.max().date()}, {len(df):,} days "
                 f"({mode_counts})",
        confidence="per-block tiers; see *_confidence_tier columns",
        limitations=[
            "proxy metrics only — per-block labels carry the caveat",
            "spot staleness / onch end-2026-07-14 / options single-snapshot — see _meta.json",
        ],
        outputs=[out_path])


def run(force_refetch: bool = False) -> PhaseResult:
    results = [build(asset) for asset in ("BTC", "ETH")]
    return PhaseResult(
        phase="final", name="Merged true-volume deliverable", asset="BOTH",
        status="COMPLETE",
        output="; ".join(r.output for r in results),
        coverage="; ".join(r.coverage for r in results),
        confidence="per-block tiers; see *_confidence_tier columns",
        limitations=[l for r in results for l in r.limitations],
        outputs=[p for r in results for p in r.outputs])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    res = run()
    append_log(res.phase, res.name, res.asset, res.status, res.output,
               res.coverage, res.confidence, res.limitations)
    print("\n=== FINAL BUILD DONE ===")


if __name__ == "__main__":
    main()
