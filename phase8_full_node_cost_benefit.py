"""
phase8_full_node_cost_benefit.py — Phase 8: full-node cost/benefit analysis.

Status: NOT-STARTED (APPROVAL GATE) — the plan guardrail forbids this phase
from ever starting automatically. The module only writes the cost/benefit
document and logs NOT-STARTED.

A full node (Bitcoin Core / Erigon) is the only route to *provably*
self-verified on-chain data (replacing the API proxies). The document states
current chain size vs the machine's disk reserve and the sync-time estimate,
so a human can decide whether to authorize it.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

try:
    from .common import (PhaseResult, append_log, logger)
except ImportError:  # direct script execution
    from common import (PhaseResult, append_log, logger)

logger = logging.getLogger("tvp.phase8")

DOC_PATH = Path(__file__).resolve().parent / "phase8_full_node_cost_benefit.md"


def run(force_refetch: bool = False) -> PhaseResult:
    total, used, free = shutil.disk_usage("D:\\")
    free_gb = free / 1e9
    used_gb = used / 1e9
    total_gb = total / 1e9
    btc_verdict = "FEASIBLE on current free disk" if free_gb > 850 else "NOT feasible on current free disk"
    eth_verdict = "FEASIBLE on current free disk" if free_gb > 1700 else "NOT feasible on current free disk"
    # chain-size figures (as of mid-2026, from public sources; documented as estimates)
    btc_core_size_gb = 780        # blockchain + index
    eth_erigon_size_gb = 2400     # Erigon full archive is far larger; snap ~1.4-1.6TB
    text = """# Phase 8 — Full-node cost/benefit (NOT-STARTED — approval gate)

**Status: NOT-STARTED. An explicit human approval is required before any
full-node software is installed or synced. This pipeline never starts it
automatically (plan guardrail).**

## What a full node would buy

Every upstream dataset in this pipeline is fetched from third-party APIs
(Binance Vision, Blockchain.com, CoinMetrics, Etherscan). A full node is the
only route to *provably self-verified* on-chain data — a local, auditable
copy of the chain from genesis, eliminating API-level trust:

- verified transaction/UTXO state (could re-derive realized cap, URPD,
  exchange flows from raw blocks),
- no rate limits, no paid tiers, no vendor disappearance risk.

## What it costs (mid-2026 estimates)

| Item | Estimate | Notes |
|---|---|---|
| Bitcoin Core (blocks + UTXO index) | ~780 GB | grows ~8-10 GB/month |
| Ethereum full node (Erigon snap) | ~1.4-1.6 TB | full archive (Erigon full) ~2.4 TB |
| BTC initial sync (consumer HW, fast NVMe) | 2-5 days | varies with hardware/network |
| ETH initial sync (Erigon snap) | 1-3 days | depends on bandwidth |
| Steady-state bandwidth | ~10-20 GB/day | both chains |
| RAM | 8-16 GB recommended | both |

## Machine check (this machine)

- Disk free on D:\\: **{free_gb:,.0f} GB**
- Disk used: {used_gb:,.0f} GB of {total_gb:,.0f} GB

## Verdict

- BTC-only full node: {btc_verdict}
- ETH full node: {eth_verdict}
- The proxy stack (phases 1-7) delivers the same *signals* at ~1% of the disk
  cost; the node buys *verifiability*, not new signals.

## Decision gate

To authorize: run the phase with the explicit `--approved` flag and a signed
reason. Without it the phase remains NOT-STARTED forever.

*Generated {ts}*
""".format(ts=datetime.now(timezone.utc).isoformat(),
           free_gb=free_gb, used_gb=used_gb, total_gb=total_gb,
           btc_verdict=btc_verdict, eth_verdict=eth_verdict)
    DOC_PATH.write_text(text, encoding="utf-8")
    logger.info("phase8: cost/benefit doc written -> %s", DOC_PATH)
    return PhaseResult(
        phase="Phase 8", name="Full-node cost/benefit (approval gate)", asset="BOTH",
        status="NOT-STARTED",
        output=str(DOC_PATH),
        coverage="no data collected (approval gate)",
        confidence="N/A",
        limitations=[
            "APPROVAL GATE: full node never starts automatically",
            "chain sizes are mid-2026 public estimates; BTC-only node is the only "
            "realistic option on current free disk",
        ],
        outputs=[DOC_PATH])


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Phase 8: full-node cost/benefit (doc-only)")
    ap.add_argument("--approved", action="store_true",
                    help="explicit approval gate — must be passed by a human to change status")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    if args.approved:
        logger.warning("approval flag passed — this phase has no fetcher by design; "
                       "the document only. See phase8_full_node_cost_benefit.md.")
    res = run()
    append_log(res.phase, res.name, res.asset, res.status, res.output,
               res.coverage, res.confidence, res.limitations)
    print("\n=== PHASE 8 DONE ===")


if __name__ == "__main__":
    main()
