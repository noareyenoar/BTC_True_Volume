# Phase 8 — Full-node cost/benefit (NOT-STARTED — approval gate)

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

- Disk free on D:\: **546 GB**
- Disk used: 414 GB of 960 GB

## Verdict

- BTC-only full node: NOT feasible on current free disk
- ETH full node: NOT feasible on current free disk
- The proxy stack (phases 1-7) delivers the same *signals* at ~1% of the disk
  cost; the node buys *verifiability*, not new signals.

## Decision gate

To authorize: run the phase with the explicit `--approved` flag and a signed
reason. Without it the phase remains NOT-STARTED forever.

*Generated 2026-08-20T16:47:44.849215+00:00*
