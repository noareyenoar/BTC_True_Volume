"""
write_report.py — deliverables (plan step 14).

Writes:
  processed/progress_report.md        per-phase table + final stats + verification
  processed/dataset_usage_guide.md    how to load/use each dataset
  processed/schema.md                 per-symbol column dictionary

Runs in two modes:
  --incremental  after every phase (uses whatever exists on disk)
  final          at terminal state (full detail)

--no-shutdown is accepted for parity with supervisor (report writing itself
never shuts anything down).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

try:
    from .common import (PROCESSED, REGISTRY_YAML, SYMBOLS, ASSETS,
                         logger)
except ImportError:  # direct script execution
    from common import (PROCESSED, REGISTRY_YAML, SYMBOLS, ASSETS,
                        logger)

logger = logging.getLogger("tvp.report")

BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "true_volume_pipeline_log.md"
STATE_PATH = BASE_DIR / "pipeline_state.json"
VERIFY_JSON = PROCESSED / "verification_report.json"

PHASE_LABELS = {
    "step0": "Step 0 — exchange true volume (spot+futures tape rollup)",
    "phase1_btc": "Phase 1-BTC — network metrics (Blockchain.com)",
    "phase1_eth": "Phase 1-ETH — network metrics (Etherscan/CoinMetrics)",
    "phase2": "Phase 2 — futures positioning (Vision metrics backfill)",
    "phase3": "Phase 3 — options verification (Deribit)",
    "phase4": "Phase 4 — checkonchain investigation",
    "phase5": "Phase 5 — realized cap (CoinMetrics derived)",
    "phase6": "Phase 6 — MM research (link)",
    "phase7": "Phase 7 — forward-only reserves (DEFERRED)",
    "phase8": "Phase 8 — full node cost/benefit (approval gate)",
    "final": "Final — merged deliverable build",
    "register": "Register — data_registry.yaml entries",
    "verify": "Verify — end-to-end checks",
}


def _state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"phases": {}}


def _log_text() -> str:
    if LOG_PATH.exists():
        return LOG_PATH.read_text(encoding="utf-8")
    return "_no log yet_"


def _parquet_summary(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    return {"rows": int(len(df)),
            "cols": [str(c) for c in df.columns],
            "start": str(pd.to_datetime(df["date"], utc=True).min().date()),
            "end": str(pd.to_datetime(df["date"], utc=True).max().date())}


def _meta_of(path: Path) -> Dict[str, Any]:
    mj = path.with_name("_meta.json")
    if mj.exists():
        try:
            return json.loads(mj.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _registry_entries() -> str:
    if not REGISTRY_YAML.exists():
        return "_registry missing_"
    return REGISTRY_YAML.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# progress report
# --------------------------------------------------------------------------

def _phase_rows(st: Dict[str, Any]) -> str:
    rows = []
    phases = st.get("phases", {})
    for p, label in PHASE_LABELS.items():
        ph = phases.get(p, {})
        status = ph.get("status", "pending")
        att = ph.get("attempts", 0)
        err = ph.get("last_error") or ""
        rows.append(f"| {label} | {status} | {att} | "
                    f"{ph.get('finished_at') or '-'} | {err[:80]} |")
    return "\n".join(rows)


def _final_stats() -> str:
    lines = []
    for asset in ("BTC", "ETH"):
        p = PROCESSED / "true_volume" / asset / "merged.parquet"
        s = _parquet_summary(p)
        if s is None:
            lines.append(f"- **{asset}**: _not built yet_")
            continue
        meta = _meta_of(p)
        lines.append(f"- **{asset}**: {s['rows']:,} rows, {s['start']} → {s['end']}, "
                     f"{len(s['cols'])} columns")
        if meta.get("blocks_meta"):
            for blk, (ds, tier) in meta["blocks_meta"].items():
                lines.append(f"  - `{blk}` block — {ds} / {tier}")
    return "\n".join(lines)


def write_progress_report() -> Path:
    st = _state()
    out = PROCESSED / "progress_report.md"
    PROCESSED.mkdir(parents=True, exist_ok=True)
    ver = ""
    if VERIFY_JSON.exists():
        try:
            v = json.loads(VERIFY_JSON.read_text(encoding="utf-8"))
            ver = "\n".join(f"- {k}: {'PASS' if not v else 'FAIL'}"
                            for k, v in v.get("results", {}).items())
        except json.JSONDecodeError:
            ver = "_verify report unreadable_"
    text = f"""# True Volume Pipeline — Progress Report

*Updated {datetime.now(timezone.utc).isoformat(timespec='seconds')}*
Pipeline state file: `BTC_True_Volume/pipeline_state.json`
Master plan: `BTC_True_Volume/master_true_volume_pipeline_plan.md`

## Phase status

| Phase | Status | Attempts | Finished (UTC) | Last error |
|---|---|---|---|---|
{_phase_rows(st)}

## Final deliverables

{_final_stats()}

## Verification

{ver or "_verification not run yet_"}

## Registry entries added

```
{_registry_entries()[:6000]}
```

## Pipeline log (embedded)

```
{_log_text()}
```
"""
    out.write_text(text, encoding="utf-8")
    logger.info("progress report written -> %s", out)
    return out


# --------------------------------------------------------------------------
# dataset usage guide
# --------------------------------------------------------------------------

_DATASETS: List[Dict[str, Any]] = [
    {"id": "exchange_true_volume_daily",
     "desc": "BTC+ETH daily exchange true volume from the aggTrades tape (futures) "
             "cross-validated against kline stores, with num_trades arbitration.",
     "path": "processed/exchange_true_volume/{{BTCUSDT,ETHUSDT}}/merged.parquet",
     "tier": "High (tape) / Medium (cross-fill days)",
     "load": "pd.read_parquet('processed/exchange_true_volume/BTCUSDT/merged.parquet')",
     "notes": ["exch_combined_volume = spot+futures where both exist, else single market (never NaN-sum)",
               "exch_coverage_mode: spot_only | spot+futures | futures_only | gap",
               "kline-artifact days quarantined (exch_fut_kline_artifact=True) — tape value stands",
               "spot store stale after 2024-12-31; boundary month-first days cross-filled with flag",
               "refresh: python -m BTC_True_Volume.exchange_true_volume --force"]},
    {"id": "onchain_network_metrics_btc_daily",
     "desc": "BTC network metrics from Blockchain.com free charts API: "
             "estimated-transaction-volume-usd, n-transactions, n-unique-addresses, hash-rate, miners-revenue.",
     "path": "processed/onchain/network_metrics/BTC/merged.parquet",
     "tier": "High",
     "load": "pd.read_parquet('processed/onchain/network_metrics/BTC/merged.parquet')",
     "notes": ["net_est_tx_volume_usd is the primary true-volume proxy (settled value moved on-chain, incl. non-trading transfers)",
               "gap-free daily index, 00:00 UTC",
               "refresh: python -m BTC_True_Volume.phase1_btc_network_metrics"]},
    {"id": "onchain_network_metrics_eth_daily",
     "desc": "ETH network activity: Etherscan dailytx (if ETHERSCAN_API_KEY present in .env) "
             "else CoinMetrics TxCnt fallback.",
     "path": "processed/onchain/network_metrics/ETH/merged.parquet",
     "tier": "Medium",
     "load": "pd.read_parquet('processed/onchain/network_metrics/ETH/merged.parquet')",
     "notes": ["without an Etherscan key the metric is tx count, not value",
               "add ETHERSCAN_API_KEY to .env to upgrade (presence-only check — the key is never read)",
               "refresh: python -m BTC_True_Volume.phase1_eth_network_metrics"]},
    {"id": "onchain_realized_cap_btc_daily",
     "desc": "BTC realized cap derived from CoinMetrics community API: "
             "rc_realized_cap_usd = CapMrktCurUSD / CapMVRVCur (validated vs local onchain store, <2% over 2,387 days).",
     "path": "processed/onchain/realized_cap/BTC/merged.parquet",
     "tier": "Medium",
     "load": "pd.read_parquet('processed/onchain/realized_cap/BTC/merged.parquet')",
     "notes": ["aggregate only — no LTH/STH split (CapRealUSD needs paid tier)",
               "also carries SplyExNtv / FlowInExNtv / FlowOutExNtv (exchange supply & flows)",
               "refresh: python -m BTC_True_Volume.phase5_realized_cap"]},
    {"id": "onchain_realized_cap_eth_daily",
     "desc": "ETH realized cap derived the same way from CoinMetrics (eth).",
     "path": "processed/onchain/realized_cap/ETH/merged.parquet",
     "tier": "Medium",
     "load": "pd.read_parquet('processed/onchain/realized_cap/ETH/merged.parquet')",
     "notes": ["same derivations as BTC; flow columns feed the onch block in the final",
               "refresh: python -m BTC_True_Volume.phase5_realized_cap"]},
    {"id": "true_volume_btc_daily",
     "desc": "FINAL BTC deliverable — outer-join of exch + net + rc + onch (local store) + pos + pos_options blocks.",
     "path": "processed/true_volume/BTC/merged.parquet",
     "tier": "per-block (exch High, net High, rc Medium, onch Medium, pos Medium, pos_options Low)",
     "load": "pd.read_parquet('processed/true_volume/BTC/merged.parquet')",
     "notes": ["every block carries {{block}}_data_source and {{block}}_confidence_tier columns — read them",
               "PROXY METRICS — never literal true volume",
               "BTC onch block ends 2026-07-14 (local onchain_dataset); NaN after",
               "pos block: top-20% cohort sentiment proxy, NOT exchange-wide OI",
               "pos_options block: single-snapshot verification (2026-08-14), NaN elsewhere"]},
    {"id": "true_volume_eth_daily",
     "desc": "FINAL ETH deliverable — outer-join of exch + net + rc + onch (CoinMetrics flows) + pos + pos_options.",
     "path": "processed/true_volume/ETH/merged.parquet",
     "tier": "per-block (exch High, net Medium, rc Medium, onch Medium, pos Medium, pos_options Low)",
     "load": "pd.read_parquet('processed/true_volume/ETH/merged.parquet')",
     "notes": ["ETH aggTrades tape ends 2026-06-16 unless extended with --resume-aggtrades (Vision ZIPs)",
               "PROXY METRICS — never literal true volume",
               "ETH onch block aliases CoinMetrics exchange flows",
               "pos block: top-20% cohort sentiment proxy, NOT exchange-wide OI"]},
]


def write_usage_guide() -> Path:
    out = PROCESSED / "dataset_usage_guide.md"
    PROCESSED.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Dataset Usage Guide — BTC & ETH True Volume (proxy stack)",
        "",
        "*Generated {ts}*",
        "",
        "## How to load anything",
        "",
        "```python",
        "import pandas as pd",
        "df = pd.read_parquet('processed/true_volume/BTC/merged.parquet')",
        "df['date'] = pd.to_datetime(df['date'], utc=True)   # daily index, UTC midnight",
        "df.set_index('date', inplace=True)",
        "df['exch_combined_volume'].tail()                   # daily traded volume (tape)",
        "```",
        "",
        "## Golden rules",
        "",
        "1. **These are proxy metrics, never literal true volume.** Each block column "
        "pair `{{block}}_data_source` / `{{block}}_confidence_tier` states what the block "
        "actually is and how much to trust it.",
        "2. Check `exch_coverage_mode` before comparing volumes across years — spot "
        "coverage ends 2024-12-31 (futures_only days after).",
        "3. NaN is a real state: block sources end on different dates (spot 2024-12-31, "
        "onch BTC 2026-07-14, aggTrades ETH 2026-06-16).",
        "4. `true_volume_pipeline_log.md` records every build's coverage, tier and "
        "limitations — read it before using a dataset for the first time.",
        "",
        "## Datasets",
        "",
    ]
    for ds in _DATASETS:
        lines += [
            f"### {ds['id']}",
            "",
            ds["desc"],
            "",
            f"- **Path**: `{ds['path']}`",
            f"- **Confidence**: {ds['tier']}",
            f"- **Load**: ```{ds['load']}```",
            "",
            "Notes:",
            *[f"  - {n}" for n in ds["notes"]],
            "",
        ]
    lines += [
        "## Refresh / extend",
        "",
        "| Goal | Command |",
        "|---|---|",
        "| Full rebuild (all phases) | `python -m BTC_True_Volume.supervisor --auto --no-shutdown` |",
        "| Force refetch of API caches | `python -m BTC_True_Volume.run_pipeline --force-refetch` |",
        "| Extend ETH tape to 2026-08-13 | `python -m BTC_True_Volume.exchange_true_volume --resume-aggtrades` |",
        "| Upgrade ETH network metric | add `ETHERSCAN_API_KEY` to `.env` (presence-only check), re-run phase1_eth |",
        "| Re-register registry entries | `python -m BTC_True_Volume.register_outputs` (idempotent) |",
        "",
        "## Schema dictionary",
        "",
        "See `schema.md` (per-symbol column dictionary) and `_meta.json` next to every "
        "parquet for build provenance.",
        "",
    ]
    out.write_text("\n".join(lines).format(ts=datetime.now(timezone.utc).isoformat(timespec="seconds")),
                   encoding="utf-8")
    logger.info("usage guide written -> %s", out)
    return out


# --------------------------------------------------------------------------
# schema.md — per-symbol column dictionary generated from the final parquet
# --------------------------------------------------------------------------

def write_schema() -> Path:
    out = PROCESSED / "schema.md"
    PROCESSED.mkdir(parents=True, exist_ok=True)
    lines = ["# Schema — True Volume final deliverables", "",
             "*Generated {ts}*", "",
             "Per-symbol column dictionary generated from the merged parquet files. "
             "Blocks: `exch` (exchange tape volume), `net` (network metrics), "
             "`rc` (realized cap), `onch` (on-chain exchange flows/reserve), "
             "`pos` (futures positioning), `pos_options` (options verification).", ""]
    for asset in ("BTC", "ETH"):
        p = PROCESSED / "true_volume" / asset / "merged.parquet"
        lines.append(f"## {asset}")
        lines.append("")
        if not p.exists():
            lines.append("_not built yet_")
            lines.append("")
            continue
        df = pd.read_parquet(p)
        lines += ["| Column | Type | Block | Sample |", "|---|---|---|---|"]
        for c in df.columns:
            dtype = str(df[c].dtype)
            sample = df[c].dropna().iloc[0] if len(df[c].dropna()) else None
            if isinstance(sample, float):
                sample = f"{sample:.4g}"
            sample = str(sample)[:40].replace("|", "/")
            block = "exchange" if c.startswith(("exch", "tape")) else \
                "network" if c.startswith("net_") else \
                "realized_cap" if c.startswith("rc_") else \
                "onchain" if c.startswith("onch_") else \
                "positioning" if c.startswith("pos_") else "other"
            lines.append(f"| `{c}` | {dtype} | {block} | {sample} |")
        lines.append("")
    out.write_text("\n".join(lines).format(ts=datetime.now(timezone.utc).isoformat(timespec="seconds")),
                   encoding="utf-8")
    logger.info("schema written -> %s", out)
    return out


def run(incremental: bool = False) -> Dict[str, str]:
    paths = {}
    paths["progress_report.md"] = write_progress_report()
    if not incremental or True:  # cheap enough to always write all three
        paths["dataset_usage_guide.md"] = write_usage_guide()
        paths["schema.md"] = write_schema()
    return {k: str(v) for k, v in paths.items()}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Write pipeline deliverables")
    ap.add_argument("--incremental", action="store_true",
                    help="incremental mode (after each phase)")
    ap.add_argument("--no-shutdown", action="store_true",
                    help="accepted for parity; report writing never shuts down")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    run(incremental=args.incremental)
    print("\n=== REPORT DONE ===")


if __name__ == "__main__":
    main()
