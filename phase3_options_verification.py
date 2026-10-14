"""
phase3_options_verification.py — Phase 3: options-market verification.

Audits the local Deribit options snapshots (deribit_options/live/{SYM}/) —
full instrument chain per snapshot with open_interest and volume_24h.

This is a VERIFICATION phase, not a backfill: it confirms whether the
derivatives-implied picture (put/call skew, OI) corroborates the volume
story for the days the snapshots exist, and produces pos_options_* columns
that are NaN outside those days.

Output: processed/options/verification/{SYM}/merged.parquet (tier Low:
single-snapshot verification, not a continuous time series).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

try:
    from .common import (PROCESSED, DERIBIT_LIVE, DS, PhaseResult,
                         append_log, logger, write_parquet_with_meta)
except ImportError:  # direct script execution
    from common import (PROCESSED, DERIBIT_LIVE, DS, PhaseResult,
                        append_log, logger, write_parquet_with_meta)

logger = logging.getLogger("tvp.phase3")

SYMBOL_CURRENCY = {"BTC": "BTC", "ETH": "ETH"}


def audit_snapshots(currency: str) -> pd.DataFrame:
    """Audit every snapshot file for a currency -> one row per snapshot."""
    snap_dir = DERIBIT_LIVE / currency
    files = sorted(snap_dir.glob("snapshot_*.parquet"))
    if not files:
        raise RuntimeError(f"phase3: no Deribit snapshots for {currency}")
    rows = []
    for p in files:
        df = pd.read_parquet(p)
        if "currency" in df.columns:
            df = df[df["currency"] == currency]
        if df.empty:
            continue
        n_calls = int((df["option_type"] == "call").sum())
        n_puts = int((df["option_type"] == "put").sum())
        call_oi = float(df.loc[df["option_type"] == "call", "open_interest"].fillna(0).sum())
        put_oi = float(df.loc[df["option_type"] == "put", "open_interest"].fillna(0).sum())
        call_vol = float(df.loc[df["option_type"] == "call", "volume_24h"].fillna(0).sum())
        put_vol = float(df.loc[df["option_type"] == "put", "volume_24h"].fillna(0).sum())
        total_oi = call_oi + put_oi
        # top strike cluster by OI (puts + calls at the same strike)
        top = (df.assign(oi=df["open_interest"].fillna(0))
                 .groupby("strike")["oi"].sum().sort_values(ascending=False).head(1))
        rows.append({
            "collected_utc": df["collected_utc"].max(),
            "instruments": len(df),
            "n_calls": n_calls,
            "n_puts": n_puts,
            "call_oi": call_oi,
            "put_oi": put_oi,
            "total_oi": total_oi,
            "put_call_oi_ratio": put_oi / call_oi if call_oi > 0 else np.nan,
            "call_vol_24h": call_vol,
            "put_vol_24h": put_vol,
            "total_vol_24h": call_vol + put_vol,
            "put_call_vol_ratio": put_vol / call_vol if call_vol > 0 else np.nan,
            "top_oi_strike": float(top.index[0]) if len(top) else np.nan,
            "top_oi_value": float(top.iloc[0]) if len(top) else np.nan,
        })
    return pd.DataFrame(rows)


def run(force_refetch: bool = False) -> PhaseResult:
    results: List[PhaseResult] = []
    for asset, currency in SYMBOL_CURRENCY.items():
        try:
            audit = audit_snapshots(currency)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"phase3 {asset}: {exc}") from exc
        audit["date"] = pd.to_datetime(audit["collected_utc"], utc=True).dt.normalize()
        audit = audit.set_index("date").sort_index()
        out = pd.DataFrame(index=audit.index)
        out["pos_options_instruments"] = audit["instruments"]
        out["pos_options_n_calls"] = audit["n_calls"]
        out["pos_options_n_puts"] = audit["n_puts"]
        out["pos_options_call_oi"] = audit["call_oi"]
        out["pos_options_put_oi"] = audit["put_oi"]
        out["pos_options_total_oi"] = audit["total_oi"]
        out["pos_options_put_call_oi_ratio"] = audit["put_call_oi_ratio"]
        out["pos_options_call_vol_24h"] = audit["call_vol_24h"]
        out["pos_options_put_vol_24h"] = audit["put_vol_24h"]
        out["pos_options_total_vol_24h"] = audit["total_vol_24h"]
        out["pos_options_put_call_vol_ratio"] = audit["put_call_vol_ratio"]
        out["pos_options_top_oi_strike"] = audit["top_oi_strike"]
        out["pos_options_top_oi_value"] = audit["top_oi_value"]
        out.index.name = "date"
        out_path = PROCESSED / "options" / "verification" / asset / "merged.parquet"
        days = ", ".join(str(d.date()) for d in out.index)
        meta = {
            "dataset_id": f"options_verification_{asset.lower()}_daily",
            "description": f"Deribit options verification snapshot for {asset} (put/call OI + volume ratios)",
            "data_source": DS["deribit"],
            "confidence_tier": "Low",
            "coverage": [str(out.index.min().date()), str(out.index.max().date())],
            "snapshot_count": len(audit),
            "known_limitations": [
                "single-snapshot verification only — NaN outside covered days",
                "volume_24h is Deribit-reported rolling 24h per instrument, not an audited tape",
            ],
        }
        write_parquet_with_meta(out, out_path, meta, DS["deribit"], "Low")
        results.append(PhaseResult(
            phase="Phase 3", name="Options verification (Deribit)", asset=asset,
            status="COMPLETE",
            output=f"{out_path} ({len(out):,} rows)",
            coverage=f"snapshots on {days}",
            confidence="Low (single-snapshot verification, not a continuous series)",
            limitations=[
                "single-snapshot verification only — NaN elsewhere",
                "volume_24h is Deribit-reported rolling 24h, not an audited tape",
            ],
            outputs=[out_path]))
    return PhaseResult(
        phase="Phase 3", name="Options verification (Deribit)", asset="BOTH",
        status="COMPLETE",
        output="; ".join(r.output for r in results),
        coverage="; ".join(r.coverage for r in results),
        confidence="Low (single-snapshot verification, not a continuous series)",
        limitations=[l for r in results for l in r.limitations],
        outputs=[p for r in results for p in r.outputs])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    res = run()
    append_log(res.phase, res.name, res.asset, res.status, res.output,
               res.coverage, res.confidence, res.limitations)
    print("\n=== PHASE 3 DONE ===")


if __name__ == "__main__":
    main()
