"""
phase7_reserves_forward_plan.py — Phase 7: forward-only reserves collector.

Status: DEFERRED (documentation only — NO fetcher starts, plan guardrail).

Rationale (from the master plan): the forward signal is already covered at
near-zero cost by Phase 5's CoinMetrics collection — SplyExNtv (exchange
supply) and FlowInExNtv/FlowOutExNtv (exchange flows) are collected daily in
processed/onchain/realized_cap/{BTC,ETH}/merged.parquet. A wallet-level
exchange-reserve collector (per-address snapshots) would add marginal value
for large cost (address labeling drift, exchange wallet taxonomy changes,
daily full-chain scans).

A forward-only collector becomes worth building IF a downstream model needs
address-granular reserves rather than aggregate flows. Revisit then.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

try:
    from .common import (PROCESSED, PhaseResult, append_log, logger)
except ImportError:  # direct script execution
    from common import (PROCESSED, PhaseResult, append_log, logger)

logger = logging.getLogger("tvp.phase7")

DOC_PATH = Path(__file__).resolve().parent / "phase7_reserves_forward_plan.md"
DOC_TEXT = """# Phase 7 — Forward-only exchange-reserves collector (DEFERRED)

**Status: DEFERRED — documentation only. No collector is started by this
pipeline (plan guardrail).**

## What it would have been

A forward-only collector snapshotting exchange wallet balances daily
(bitinfocharts / CoinMetrics wallet-level feeds) to track BTC/ETH held on
exchanges as a liquidity-availability context signal.

## Why it is deferred

The forward signal is already covered at near-zero cost by Phase 5:

| Signal | Where it lives now | Cadence |
|---|---|---|
| `rc_exchange_supply` (SplyExNtv) | `processed/onchain/realized_cap/{{BTC,ETH}}/merged.parquet` | daily, free CoinMetrics |
| `rc_net_exchange_flow` (FlowInExNtv − FlowOutExNtv) | same | daily, free CoinMetrics |

A wallet-level collector would add address-granular reserves (e.g., "which
specific exchange holds X"), which no current downstream consumer needs, at
the cost of:
- address-labeling drift (exchange wallet taxonomy changes over time),
- per-exchange snapshot reconciliation,
- daily full-chain balance queries.

## Revisit trigger

Build it if a downstream model needs address-granular reserves or per-exchange
breakdowns rather than aggregate flows. Until then, Phase 5's aggregates
carry the signal.

*Generated {ts}*
""".format(ts=datetime.now(timezone.utc).isoformat())


def run(force_refetch: bool = False) -> PhaseResult:
    DOC_PATH.write_text(DOC_TEXT, encoding="utf-8")
    logger.info("phase7: deferred doc written -> %s", DOC_PATH)
    return PhaseResult(
        phase="Phase 7", name="Forward-only reserves collector", asset="BOTH",
        status="DEFERRED",
        output=str(DOC_PATH),
        coverage="no data collected (documentation only)",
        confidence="N/A",
        limitations=[
            "signal already covered by Phase 5 (SplyExNtv, FlowInExNtv/FlowOutExNtv)",
            "wallet-level collector deferred — revisit if address-granular reserves are needed",
        ],
        outputs=[DOC_PATH])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    res = run()
    append_log(res.phase, res.name, res.asset, res.status, res.output,
               res.coverage, res.confidence, res.limitations)
    print("\n=== PHASE 7 DONE ===")


if __name__ == "__main__":
    main()
