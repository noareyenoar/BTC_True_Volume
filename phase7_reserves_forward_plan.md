# Phase 7 — Forward-only exchange-reserves collector (DEFERRED)

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
| `rc_exchange_supply` (SplyExNtv) | `processed/onchain/realized_cap/{BTC,ETH}/merged.parquet` | daily, free CoinMetrics |
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

*Generated 2026-08-20T16:47:39.791575+00:00*
