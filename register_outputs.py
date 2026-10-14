"""
register_outputs.py — idempotent registry registration of the 7 new datasets.

Appends to D:\\kp_ai_agent\\data_registry.yaml (the project's single source of
truth) matching the existing entry format. Idempotent: entries are keyed by
dataset_id; re-running updates nothing if present, or appends if absent.
Then runs data_registry.check_registry_integrity() from the project root.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

try:
    from .common import (PROCESSED, REGISTRY_YAML, SYMBOLS, PhaseResult,
                         append_log, logger)
except ImportError:  # direct script execution
    from common import (PROCESSED, REGISTRY_YAML, SYMBOLS, PhaseResult,
                        append_log, logger)

logger = logging.getLogger("tvp.register")

REGISTRY_LOADER = Path(__file__).resolve().parents[1] / "data_registry.py"


def _stats(path: Path) -> Dict[str, Any]:
    df = pd.read_parquet(path)
    return {"columns": [str(c) for c in df.columns],
            "row_count": int(len(df)),
            "date_range": {"start": str(pd.to_datetime(df["date"], utc=True).min().date()),
                           "end": str(pd.to_datetime(df["date"], utc=True).max().date())}}


def _file_dir(path: Path) -> Path:
    """Registry entries point at files when a single file exists (the integrity
    check verifies existence), but the directory itself is also a valid target
    for per-symbol stores. Prefer the merged.parquet file."""
    cand = path / "merged.parquet" if path.is_dir() else path
    return cand if cand.exists() else path


def _entry(dataset_id: str, description: str, path: Path, source: str,
           symbols: List[str], timeframe: str, caveats: List[str],
           collector: str) -> Dict[str, Any]:
    path = _file_dir(path)
    st = _stats(path)
    return {
        "description": description,
        "path": str(path),
        "source": source,
        "file_format": "parquet",
        "symbols": symbols,
        "timeframe": timeframe,
        "date_range": st["date_range"],
        "columns": st["columns"],
        "row_count": st["row_count"],
        "known_caveats": caveats,
        "collector_script": collector,
        "cost_tier": "free",
        "contains_synthetic": False,
        "last_verified": "2026-08-20",
    }


def build_entries() -> Dict[str, Dict[str, Any]]:
    caveat = "PROXY METRICS — never literal true volume; per-block data_source/confidence_tier labels in the parquet + true_volume_pipeline_log.md"
    now = datetime.now(timezone.utc).date().isoformat()
    e: Dict[str, Dict[str, Any]] = {}
    for asset, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
        fid = f"true_volume_{asset.lower()}_daily"
        e[fid] = _entry(
            fid,
            f"{asset} daily true-volume PROXY stack (exchange tape volume + network metrics + realized cap + positioning + options verification)",
            PROCESSED / "true_volume" / asset / "merged.parquet",
            "merged: binance_vision_aggtrades/klines, blockchain.com/etherscan/coinmetrics (free tiers)",
            [sym], "1d",
            [caveat,
             "exch block: spot stale after 2024-12-31 (coverage_mode column flags futures_only days)",
             "onch block (BTC): local onchain_dataset ends 2026-07-14",
             "pos block: top-trader ratios = top-20% cohort sentiment proxy, NOT exchange-wide OI",
             "pos_options block: single-snapshot verification only (NaN elsewhere)"],
            f"BTC_True_Volume/run_pipeline.py --phases step0,phase1_*,phase2,phase3,phase4,phase5,phase6,phase7,phase8,final (built {now})")
        e[f"exchange_true_volume_daily"] = _entry(
            "exchange_true_volume_daily",
            "BTC+ETH daily exchange true volume (spot+futures combined, taker flows) from the aggTrades tape + kline cross-fills",
            PROCESSED / "exchange_true_volume",
            "binance_vision_aggtrades (futures tape), binance_vision_klines (spot + num_trades)",
            list(SYMBOLS), "1d",
            [caveat,
             "spot store stale after 2024-12-31",
             "kline-artifact days quarantined + documented in _meta.json (tape value stands)"],
            "BTC_True_Volume/exchange_true_volume.py")
        e[f"onchain_network_metrics_{asset.lower()}_daily"] = _entry(
            f"onchain_network_metrics_{asset.lower()}_daily",
            f"{asset} daily network-level metrics (BTC: Blockchain.com charts; ETH: Etherscan dailytx or CoinMetrics TxCnt)",
            PROCESSED / "onchain" / "network_metrics" / asset,
            "blockchain.com_charts_api (BTC) / etherscan_free_api (ETH, or coinmetrics fallback)",
            [sym], "1d",
            [caveat,
             "BTC estimated tx volume includes non-trading transfers (settled-value proxy)",
             "ETH falls back to TxCnt without an ETHERSCAN_API_KEY in .env"],
            f"BTC_True_Volume/phase1_{asset.lower()}_network_metrics.py")
        e[f"onchain_realized_cap_{asset.lower()}_daily"] = _entry(
            f"onchain_realized_cap_{asset.lower()}_daily",
            f"{asset} daily realized cap derived from CoinMetrics free metrics (CapMrktCurUSD/CapMVRVCur)",
            PROCESSED / "onchain" / "realized_cap" / asset,
            "coinmetrics_community_api (derived)",
            [sym], "1d",
            [caveat,
             "aggregate only — no price-bucket / LTH-STH split (CapRealUSD needs paid tier)",
             "cross-checked vs local onchain_dataset < 2% (2,387 days)"],
            "BTC_True_Volume/phase5_realized_cap.py")
    # de-duplicate the shared exchange entry
    e["exchange_true_volume_daily"] = e.pop("exchange_true_volume_daily")
    return e


def register() -> None:
    entries = build_entries()
    text = REGISTRY_YAML.read_text(encoding="utf-8")
    added = []
    for dataset_id, entry in entries.items():
        # idempotent: skip if the id block already exists
        if re.search(rf"^\s{dataset_id}:", text, re.MULTILINE):
            logger.info("registry: %s already present — skipped", dataset_id)
            continue
        lines = [f"  {dataset_id}:"]
        for k, v in entry.items():
            if isinstance(v, list):
                inner = "\n".join(f"      - {item}" for item in v)
                lines.append(f"    {k}:\n{inner}")
            elif isinstance(v, dict):
                inner = ", ".join(f'"{kk}": "{vv}"' for kk, vv in v.items())
                lines.append(f"    {k}: {{{inner}}}")
            elif isinstance(v, str):
                # double-quoted YAML scalars: backslashes must be escaped
                # (Windows paths would otherwise fail to parse)
                lines.append(f"    {k}: \"{v.replace(chr(92), chr(92)*2)}\"")
            else:
                lines.append(f"    {k}: \"{v}\"")
        text += "\n" + "\n".join(lines) + "\n"
        added.append(dataset_id)
    if added:
        REGISTRY_YAML.write_text(text, encoding="utf-8")
        logger.info("registry: appended %d entries: %s", len(added), added)
    else:
        logger.info("registry: nothing to append (all present)")
    # integrity check via the project loader (in-process)
    sys.path.insert(0, str(REGISTRY_LOADER.parent))
    try:
        import data_registry
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError(f"registry loader import failed: {exc}") from exc
    report = data_registry.check_registry_integrity()
    broken = [b["dataset_id"] for b in report.get("broken", [])
              if b["dataset_id"] in entries]
    if broken:
        raise RuntimeError(f"registry integrity check FAILED for: {broken}")
    logger.info("registry integrity check: PASS (%d entries, %d total)",
                len(report.get("ok", [])), report.get("total", 0))


def run(force_refetch: bool = False) -> PhaseResult:
    register()
    n = len(build_entries())
    return PhaseResult(
        phase="register", name="Registry registration", asset="BOTH",
        status="COMPLETE",
        output=str(REGISTRY_YAML),
        coverage=f"{n} dataset entries idempotent (path, source, caveats, contains_synthetic: false)",
        confidence="N/A (bookkeeping)",
        limitations=["registry YAML is the single source of truth — edits must go through the loader"],
        outputs=[REGISTRY_YAML])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    res = run()
    append_log(res.phase, res.name, res.asset, res.status, res.output,
               res.coverage, res.confidence, res.limitations)
    print("\n=== REGISTER DONE ===")


if __name__ == "__main__":
    main()
