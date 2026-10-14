# Independent Pipeline Audit — Verify & Remediate

## CONTEXT

A prior overnight supervisor run reported: 13 phases `completed`, "ALL PASS" on all gates (step0 tape gates including "Vision adjudication", network, positioning, realized-cap cross-check, final, registry/log), registry integrity "clean at 28 entries," clean shutdown with 120s grace.

**Do not trust this summary at face value.** It is a self-report from the same automation that ran the pipeline — no independent party has verified it. Your job is to re-derive every claim from the actual files on disk and either confirm or correct it. Treat this as an audit, not a status check.

---

## ULTIMATE GOAL

Determine, with hard evidence from the files themselves (not from any prior agent's summary text), whether the dataset in `processed/` is genuinely ready for **Technical Analysis (TA) and Volume Spread Analysis (VSA)** work. VSA has stricter requirements than generic "data exists" — it depends on volume being trustworthy, OHLC being internally consistent, and bars being gapless and evenly spaced. If any of these are not actually true, fix them before declaring readiness.

---

## PHASE 1 — RE-DERIVE THE REGISTRY FROM SCRATCH

Do not read `registry.json` and trust it. Re-run the registry scanner fresh, or if that's not possible, independently walk the filesystem and recompute the key facts yourself:

```python
from pathlib import Path
import pyarrow.parquet as pq

processed_root = Path("./processed")
all_parquets = sorted(processed_root.rglob("*.parquet"))

print(f"Actual .parquet files found on disk: {len(all_parquets)}")
for p in all_parquets:
    try:
        md = pq.read_metadata(p)
        print(f"  {p.relative_to(processed_root)}: {md.num_rows:,} rows")
    except Exception as e:
        print(f"  {p.relative_to(processed_root)}: ERROR — {e}")
```

Compare this actual count against the claimed "28 entries."

- If registry.json says 28 but you find a different number of actual parquet files → the registry is stale or wrong. Document the discrepancy explicitly.
- List what the **planned scope** was (9 symbols × klines intervals × spot/UM/CM markets + metrics + options + on-chain network metrics, per this project's prior planning documents) vs. what actually exists. Produce an explicit gap table:

```
| Expected dataset_id | Found on disk? | Row count | Status |
|---|---|---|---|
```

If 28 is far below the planned scope, this is not "clean" — it's incomplete, and the prior "ALL PASS" claim needs correction.

---

## PHASE 2 — VERIFY EACH GATE INDEPENDENTLY

For every gate the prior run claimed passed, recompute it yourself rather than reading the claim:

### 2.1 "Network" gate
- Confirm what this actually checked. Find the code/log that defines it. If it just means "download succeeded / no connection errors," state that plainly — it says nothing about data quality.

### 2.2 "Positioning" gate
- Load `futures_um/metrics/{symbol}/merged.parquet` for every symbol claimed complete.
- Verify `sum_open_interest`, `sum_toptrader_long_short_ratio`, etc. are present, non-null, and cover the expected date range.
- Report actual null percentages — do not accept "passed" without a number.

### 2.3 "Realized-cap cross-check" gate
- Find what this compared against what. If it cross-checked against CoinMetrics or another source, verify the comparison logic itself makes sense (e.g., correct units, correct date alignment) — a gate can "pass" while comparing the wrong things.

### 2.4 "Vision adjudication" gate — SPECIAL SCRUTINY
This term does not appear in any prior planning document for this project. Before accepting this gate at all:
1. Find the actual code that implements it. What model is used? What is it looking at — chart images? What is the pass/fail criterion?
2. If it is an LLM/vision-model subjective judgment (e.g., "does this chart look reasonable"), flag this explicitly as a **non-deterministic, non-reproducible check** — re-running it might give a different answer. State this limitation in your findings even if it "passed."
3. Determine whether this gate could have silently passed due to a lenient or vague prompt to the vision model, rather than a genuine quality bar. Read the exact prompt/instruction that was given to the vision model, if recoverable from logs.
4. If you cannot find what this gate actually did, report it as **UNVERIFIED**, not as passed — absence of evidence is not evidence of correctness.

### 2.5 "Final" and "registry/log" gates
- Same principle: find the actual check, re-run it or re-derive its result, don't accept the label.

---

## PHASE 3 — VSA-SPECIFIC READINESS CHECKS (the real bar for this project's use case)

Regardless of what the generic gates say, VSA/TA readiness requires these specific properties. Check each one directly against the data:

### 3.1 Volume trustworthiness
- For every symbol/interval, confirm whether the `volume` column used is (a) raw exchange volume, or (b) the on-chain/OI-derived proxy from the master true-volume plan, or (c) both, joined together.
- If the on-chain volume proxy (`estimated-transaction-volume-usd` from Blockchain.com, per the earlier plan) was supposed to be joined to the kline bars and is NOT actually joined — this is a gap. Check for its presence as a column in the merged kline files, not just as a separate file sitting in `onchain/`.
- Report explicitly: is VSA about to be run on raw exchange volume (the thing this whole project set out to replace) or on the improved proxy? This is the single most important check in this phase.

### 3.2 Gap analysis
```python
import pandas as pd

def check_gaps(parquet_path, interval_ms):
    df = pd.read_parquet(parquet_path, columns=["open_time"])
    df = df.sort_values("open_time")
    diffs = df["open_time"].diff().dropna()
    gap_count = (diffs > interval_ms * 1.5).sum()
    gap_pct = gap_count / len(df) * 100
    return gap_count, gap_pct
```
Run this for every klines file. VSA is highly sensitive to missing bars — a gap misreads as a "no supply/no demand" signal. Report actual `gap_pct` per dataset; do not accept a summary claim of "gates pass" without this number.

### 3.3 OHLC integrity
```python
def check_ohlc_integrity(df):
    violations = (
        (df["low"] > df["open"]) | (df["low"] > df["close"]) |
        (df["high"] < df["open"]) | (df["high"] < df["close"])
    )
    return violations.sum(), violations.sum() / len(df) * 100
```
Report actual violation counts per dataset. Any non-zero count needs investigation before VSA work — a broken spread (High-Low) invalidates the core VSA read on that bar.

### 3.4 Interval consistency
- Confirm every bar in a given file is actually spaced at its claimed interval (not a mix of 1m and 5m bars merged incorrectly, for example).

---

## PHASE 4 — REPORT REGENERATION AUDIT

The prior run admitted `progress_report.md` did not auto-refresh from the supervisor's terminal path and had to be regenerated manually before shutdown.

1. Check the actual file timestamps of `progress_report.md`, `dataset_usage_guide.md`, `schema.md`, `registry_report.md` against `pipeline_state.json`'s last-updated time. Confirm they're actually in sync now.
2. Because one silent gap was already found in the automation (the report not auto-refreshing), **specifically look for other silent gaps** in the supervisor's final block — e.g., did the registry scanner actually get re-run after the last phase, or is `registry.json` also stale? Check its timestamp too.
3. Do not assume this was an isolated, one-off issue — treat it as a signal that other steps in the "final block" may have similarly failed silently. Audit each claimed final step (registry write, log write, report write) by timestamp and content, not by the supervisor's own claim that it happened.

---

## PHASE 5 — REMEDIATION (only after Phases 1–4 are complete)

Based on actual findings (not assumptions), fix what's broken:

- If datasets are missing vs. planned scope → resume the fetch/process pipeline for the missing ones, reusing existing disk-guard and chunked-processing infrastructure from prior work
- If gaps/OHLC violations exceed acceptable thresholds (`gap_pct > 1%`, any `ohlc_integrity_violations > 0` unexplained) → do not silently patch by dropping rows; document root cause first, then decide fill-vs-flag per the existing project convention (flag, don't silently fill)
- If the on-chain volume proxy isn't actually joined to kline data → build that join now, this was the core deliverable of the master plan
- If `registry.json` is stale → re-run the scanner
- Do NOT mark anything "fixed" without re-running the Phase 1–3 checks afterward to confirm

---

## OUTPUT

Produce `independent_audit_report.md`:

```markdown
# Independent Pipeline Audit

## Summary Verdict
- Prior claim: "ALL PASS, 28 entries clean"
- Actual finding: [CONFIRMED / PARTIALLY CONFIRMED / CONTRADICTED — with evidence]

## Phase 1 — Registry Re-derivation
[actual file count vs claimed, gap table]

## Phase 2 — Gate-by-Gate Re-verification
[each gate: what it actually checks, re-verified result, confidence]
### Vision Adjudication — special note
[what it does, reproducibility concern, verdict]

## Phase 3 — VSA Readiness
[volume trustworthiness verdict — raw exchange vs proxy vs joined]
[gap_pct table per dataset]
[ohlc violation table per dataset]
[interval consistency verdict]

## Phase 4 — Silent Gap Audit
[timestamp cross-check results, any other silent failures found]

## Phase 5 — Remediation Actions Taken
[what was fixed, what was re-verified after fixing]

## Final Verdict
Is this dataset actually ready for TA+VSA work? State plainly, with the specific
evidence that supports the answer — not a restated confidence claim.
```

---

## GUARDRAILS

- Every claim in your final report must trace to a number or file you actually read in this session — no restating the prior agent's summary as if independently confirmed
- If you cannot verify a claim (e.g., can't find the Vision adjudication code), say "UNVERIFIED," not "assumed passing"
- Do not treat "the previous agent said X" as evidence for X
- If remediation is needed and is large in scope (e.g., most of the planned datasets are actually missing), stop and report before doing a large amount of new fetching — don't silently expand scope back to a multi-day job without flagging it first
