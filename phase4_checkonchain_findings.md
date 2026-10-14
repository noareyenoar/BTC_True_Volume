# Phase 4 — checkonchain.com investigation findings

Generated 2026-09-20T06:20:58.414495+00:00 (probe took 3s)

- robots.txt: allow_all=True (User-Agent: * with Allow: / and no Disallow directives)
- data model: self-contained static HTML (GitHub Pages); URPD embedded as Plotly base64 float64 arrays; no API endpoint exists
- URPD scope: BTC-UTXO-only price-bucket supply snapshot — NOT a volume series and no ETH equivalent; licensed content of a commercial brand (robots-permitted, use as investigation artifact only)
- fetcher: COMPLETE — 630 price buckets -> D:\kp_ai_agent\Dataset_Fetcher\BTC_True_Volume\processed\onchain\checkonchain\BTC\urpd_snapshot.parquet

## Decision
Investigation COMPLETE. Small keyless fetcher built for the latest URPD snapshot as a context artifact (tier Medium). URPD adds no volume signal to the true-volume stack: it is a BTC-only price-bucket supply snapshot. No ETH equivalent exists.
