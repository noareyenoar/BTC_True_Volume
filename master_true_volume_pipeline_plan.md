# Master Plan — True Volume Proxy Data Pipeline (Bitcoin → Ethereum)

## GOAL

Build the most reliable available "true volume" signal for BTC, then replicate for ETH. Exchange-reported spot volume is known to be unreliable (wash trading). This plan sequences every alternative data source from **cheapest + most reliable first** to **most expensive + most speculative last**, with explicit go/no-go checkpoints between phases. Do not skip ahead — each phase either completes or gets an explicit no-go decision before the next begins.

**Constraint carried through the entire plan: FREE SOURCES ONLY.** Where a step requires registration, it must be a free tier — verify this empirically at signup, don't assume from marketing copy.

---

## SEQUENCING LOGIC (why this order)

| Phase | Cost | Reliability as volume proxy | Backfillable? |
|---|---|---|---|
| 1. Network-level on-chain volume | Free, ~5 min setup | High (real settled value, but includes non-trading transfers) | Yes, full history |
| 2. Derivatives positioning (OI + top-trader skew) | Already mostly built | High for futures market context | Yes |
| 3. Options positioning | Already built | Medium (sentiment, not volume) | Partial |
| 4. checkonchain API investigation | Free, cheap to check | High if accessible | Depends on findings |
| 5. CoinMetrics free tier realized cap | Free, cheap | Medium (aggregate only) | Yes |
| 6. MM/whale proxy criteria | Free, research only | N/A — feeds feature engineering | N/A |
| 7. DIY exchange reserve tracking | Free, ongoing effort | High once running | No — forward-only |
| 8. Full node UTXO reproduction (URPD) | Free but heavy (700GB+, days) | Highest fidelity | Yes, but expensive |

Rule: **complete a phase, evaluate whether it's sufficient, only proceed to the next if there's a clear gap.** Phase 8 in particular should only be attempted if Phases 1–7 leave a gap the project actually needs filled — it is the most expensive step by a wide margin.

---

## PHASE 1 — Network-Level On-Chain Volume (DO THIS FIRST)

**Objective**: fetch `estimated-transaction-volume-usd` and related network aggregates from Blockchain.com Charts API — zero-cost, zero-signup, full history back to 2009.

```python
BLOCKCHAIN_CHARTS = {
    "estimated-transaction-volume-usd": "primary true-volume proxy candidate",
    "n-transactions": "raw tx count",
    "n-unique-addresses": "network usage",
    "hash-rate": "miner commitment context",
    "miners-revenue": "selling pressure context",
    "mempool-size": "congestion context",
}

def fetch_blockchain_chart(chart_name: str, timespan: str = "all"):
    import requests, pandas as pd
    url = f"https://api.blockchain.info/charts/{chart_name}"
    r = requests.get(url, params={"timespan": timespan, "format": "json", "sampled": "false"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    df = pd.DataFrame(data["values"]).rename(columns={"x": "ts", "y": chart_name})
    df["datetime_utc"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df[["datetime_utc", chart_name]]
```

**Output**: `processed/onchain/network_metrics/BTC/merged.parquet`

**Checkpoint before Phase 2**: verify data loaded correctly, spans 2009–present, no gaps. If this alone materially improves feature quality over exchange volume, note it — this is likely the single highest ROI step in the entire plan.

---

## PHASE 2 — Derivatives Positioning Completion

**Objective**: ensure `futures_um/metrics/{symbol}` is fully backfilled for BTC, and add the derived top-trader long/short skew columns.

```python
def derive_directional_oi(df):
    ratio = df["sum_toptrader_long_short_ratio"]
    df["top_trader_long_pct"] = ratio / (ratio + 1)
    df["top_trader_short_pct"] = 1 / (ratio + 1)
    df["top_trader_long_short_skew"] = df["top_trader_long_pct"] - 0.5
    return df
```

Reuse existing pipeline infrastructure (disk guard, chunked processing, checksum verification) already built for this project. This is a completion task, not new build — check existing `processed/futures_um/metrics/BTCUSDT/merged.parquet` coverage first; only backfill actual gaps.

**Checkpoint**: confirm `top_trader_long_short_skew` is a *sentiment proxy*, not literal exchange-wide long/short OI — document this clearly in output metadata (exchange-wide OI nets to zero by construction; only the top-trader cohort ratio is meaningfully asymmetric).

---

## PHASE 3 — Options Positioning Verification

**Objective**: confirm `options/EOHSummary` data (already built) is complete for BTC and usable as a sentiment cross-check against volume proxies.

No new fetch needed if already complete — this phase is a verification/documentation step only. Confirm date coverage and row counts via the registry scanner.

---

## PHASE 4 — checkonchain.com API Investigation

**Objective**: determine whether URPD / LTH-STH cohort data is fetchable via an underlying JSON API, before committing to any heavier build.

Steps:
1. Inspect network requests when loading a checkonchain URPD chart page — look for XHR/fetch calls returning JSON
2. Check the site's stated terms on data reuse/redistribution
3. Decision point:
   - **If accessible + terms permit** → build a lightweight fetcher, done, skip to Phase 6
   - **If not accessible or terms prohibit reuse** → document this, proceed to Phase 5 (do NOT scrape against stated terms)

**This phase should take under an hour.** It's a investigation gate, not a build phase — don't over-invest here.

---

## PHASE 5 — CoinMetrics Free Tier Realized Cap (Coarse Fallback)

**Objective**: if Phase 4 is a no-go, get the closest free approximation — CoinMetrics Community API's `CapRealUSD` (aggregate realized cap, the single number URPD's histogram sums to).

```python
COINMETRICS_COMMUNITY_BASE = "https://community-api.coinmetrics.io/v4/"
# Verify current endpoint structure and available free metrics before use —
# research current catalog at https://docs.coinmetrics.io/asset-metrics/market/realised-cap
```

**Output**: `processed/onchain/realized_cap/BTC/merged.parquet`

**Checkpoint**: this gives aggregate realized cap trend only — no price-bucket granularity, no LTH/STH split. Document this limitation. Evaluate whether this is "good enough" given the project's actual modeling needs before considering Phase 8.

---

## PHASE 6 — MM / Whale Proxy Criteria Research

**Objective**: produce the Tier 1/2/3 criteria framework (exchange-published segmentation, on-chain whale flow, order-book footprint inference) that lets later feature engineering flag likely large-player activity using data already being collected in Phases 1–5.

This is a research-only phase (no data fetching) — output is `mm_proxy_criteria_report.md` per the framework already scoped:
- Track 1: exchange-published top-trader segmentation (already have via Phase 2)
- Track 2: on-chain whale flow (Whale Alert free feed, Etherscan/Blockchair free tier)
- Track 3: order-book footprint proxies (large trades, iceberg detection, absorption — computable from existing `aggTrades`/`bookDepth` data already in the project)
- Track 4: academic/regulatory context (CFTC COT precedent, why crypto lacks an equivalent)

**Checkpoint**: this report should explicitly state the confidence ceiling — no criterion here proves "market maker," only "likely large-trader activity."

---

## PHASE 7 — DIY Exchange Reserve Tracking (Forward-Only)

**Objective**: start a forward-collecting job tracking known exchange wallet balances, since this data category **cannot be backfilled for free** regardless of method.

```python
# Start TODAY. This builds a series going forward only —
# document explicitly that no historical backfill is achievable.
EXCHANGE_ADDRESSES = {}  # populate from a currently-maintained, verified source
                          # do not hardcode from memory — addresses migrate
```

**Decision point**: is this worth starting now given it won't have useful history for months? Recommend starting the collector regardless (cost is near-zero, ongoing value compounds over time), but do not block on it — this runs in the background while later phases proceed.

---

## PHASE 8 — Full Bitcoin Node UTXO Reproduction (LAST RESORT — EXPLICIT APPROVAL GATE)

**Objective**: only if Phases 1–7 leave a gap that specifically requires full URPD histogram + exact LTH/STH cohort supply, and that gap is judged worth ~700GB disk + multi-day sync + ongoing node maintenance.

**Do not start this phase automatically.** Produce a cost/benefit note first:
- Confirmed disk requirement (verify current Bitcoin chain size — grows over time, check current figure rather than assuming)
- Estimated sync time on available hardware
- Whether this exceeds the project's existing 100GB disk reserve — if yes, this needs a **separate explicit budget decision**, not silent consumption of the existing reserve

Only proceed past this checkpoint if there's affirmative sign-off that the disk/time cost is acceptable.

---

## BITCOIN → ETHEREUM TRANSITION

Once Phases 1–7 are complete for BTC (Phase 8 optional/deferred), repeat the equivalent structure for ETH with these adaptations:

### What transfers directly:
- **Phase 1 equivalent**: Etherscan's free API provides `dailytx` (transaction count) and gas-based network activity — no direct "estimated transaction volume USD" equivalent as clean as Blockchain.com's, so check Etherscan's free-tier stats endpoints (`https://api.etherscan.io/api?module=stats`) for the closest analog. Verify current free-tier rate limits (historically ~5 calls/sec, confirm current 2026 limits at signup).
- **Phase 2 equivalent**: ETH perpetual futures `metrics` (OI, top-trader ratio) — same Binance bulk data pattern, same schema, just symbol=ETHUSDT. Already partially built per this project's existing pipeline.
- **Phase 3 equivalent**: ETH options EOHSummary — already in existing pipeline scope.
- **Phase 6 equivalent**: MM/whale proxy criteria — largely reusable framework, same tiers apply.

### What does NOT transfer directly — requires new methodology:
- **URPD / UTXO concepts (Phases 4/5/8) do not apply to Ethereum.** Ethereum is account-based, not UTXO-based — there is no "UTXO age" to bucket. The closest equivalent concept is **address-level cost-basis tracking** (tracking each address's ETH balance changes and the price at each change), which is a different and generally harder computation than Bitcoin's UTXO model. Before building anything here:
  1. Research whether checkonchain or an equivalent free source publishes an ETH realized-cap/cohort equivalent
  2. Research CoinMetrics free tier ETH coverage for `CapRealUSD` (they do cover ETH, verify current metric availability)
  3. Do NOT attempt a DIY full-Ethereum-node cost-basis reproduction as a default plan — this is significantly more complex than the Bitcoin UTXO case (Ethereum state is far larger and continuously mutating) and should only be considered after exhausting free aggregate sources, with its own explicit cost/benefit gate like Phase 8 above

### Execution order for Ethereum phase:
1. Etherscan free-tier network stats (equivalent to Phase 1)
2. ETH futures metrics completion (equivalent to Phase 2)
3. ETH options EOHSummary verification (equivalent to Phase 3)
4. Research free ETH realized-cap/cohort sources (equivalent to Phases 4–5, new research since ETH sources differ from BTC)
5. Extend MM/whale proxy criteria to ETH-specific patterns (equivalent to Phase 6) — note ETH has additional whale-tracking surface area via DeFi/staking flows that BTC doesn't (e.g., large staking deposits/withdrawals, DEX large-swap detection) — research these as ETH-specific additions to the Tier 1/2/3 framework
6. DIY exchange reserve tracking for ETH addresses (equivalent to Phase 7) — Etherscan's labeled address exports make this easier to bootstrap than Bitcoin's address attribution
7. Full-node cost-basis reproduction — only with explicit approval gate, same as Phase 8, expect this to be judged not worth it in most cases given the added complexity over Bitcoin's UTXO model

---

## REPORTING REQUIREMENT

At the end of each phase (both BTC and ETH), append a section to a running `true_volume_pipeline_log.md`:

```markdown
## Phase N — [name] — [BTC/ETH]
- Status: COMPLETE / NO-GO / DEFERRED
- Output location: [path]
- Coverage: [date range, row count]
- Confidence tier: [High/Medium/Low as a volume proxy]
- Known limitations: [explicit list]
```

This log is the single source of truth for what's actually usable vs. aspirational by the end of the plan — consult it before any downstream feature engineering assumes a data source exists.

---

## GUARDRAILS THROUGHOUT

- Never skip a checkpoint to save time — each phase's go/no-go decision protects the next phase from being built on an unverified assumption
- Never present a proxy metric as literal "true volume" — every output must carry a `data_source` and `confidence_tier` label
- Never let Phase 8 (or its Ethereum equivalent) start without an explicit approval step — it is disproportionately expensive compared to every other phase in this plan
- Reuse existing project infrastructure (disk guard at 100GB reserve, chunked processing, checksum verification, registry scanner integration) at every phase rather than building parallel one-off scripts
- If a "free" source turns out to require payment upon actual signup (this has happened before in this project's research — CoinGlass, Glassnode exchange-flow metrics), document it plainly and move to the next candidate rather than substituting a paid source silently
