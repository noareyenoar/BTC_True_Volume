"""
phase4_checkonchain_investigation.py — Phase 4: checkonchain investigation.

Timeboxed probe (target < 5 min; hard gate 1h) of checkonchain.com, per the
master plan: robots.txt, homepage, charts subdomain, then a decision.

Verified during implementation:
  - robots.txt: "User-Agent: * / Allow: /" — full crawl permission, no disallows.
  - checkonchain.com is a Webflow marketing site; the data lives on
    charts.checkonchain.com, a static GitHub Pages site (repo
    checkmatey/checkonchain.com — keyless raw.githubusercontent.com).
  - Chart pages are SELF-CONTAINED: no fetch()/API — the URPD histogram is
    embedded as Plotly typed-array payloads (base64 float64) inside the HTML.

Decision: COMPLETE with a SMALL FETCHER — the latest URPD snapshot page is
downloaded keyless and its embedded x/y arrays decoded into a parquet
artifact. URPD is BTC-UTXO-only (no ETH equivalent), a price-bucket supply
snapshot (NOT a volume series), and the source is a commercial brand's public
GitHub repo (robots-permitted; licensing caveat documented). It is an
investigation artifact, not a core true-volume input.

Output: processed/onchain/checkonchain/BTC/urpd_snapshot.parquet
        BTC_True_Volume/phase4_checkonchain_findings.md
"""

from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

try:
    from .common import (PROCESSED, DS, PhaseResult, RateLimitedClient,
                         append_log, logger, write_parquet_with_meta)
except ImportError:  # direct script execution
    from common import (PROCESSED, DS, PhaseResult, RateLimitedClient,
                        append_log, logger, write_parquet_with_meta)

logger = logging.getLogger("tvp.phase4")

GH_RAW = "https://raw.githubusercontent.com/checkmatey/checkonchain.com/main"
GH_API = "https://api.github.com/repos/checkmatey/checkonchain.com/contents"
URPD_INDEX = f"{GH_API}/btconchain/urpd/urpd"      # latest snapshot dir
ROBOTS = "https://checkonchain.com/robots.txt"
FINDINGS_MD = Path(__file__).resolve().parent / "phase4_checkonchain_findings.md"

# Plotly typed-array payload inside Plotly.newPlot("id", [traces], ...)
#   "x":{"dtype":"f8","bdata":"<base64>"}
# The JSON is unicode-escaped: "/" appears as "/" inside bdata — the
# character class allows the backslash so the capture spans the whole payload,
# and _parse_urpd_html unescapes before base64-decoding.
_ARRAY_RE = re.compile(r'"(x|y)":\s*\{\s*"dtype":\s*"f8",\s*"bdata":\s*"([A-Za-z0-9+/=\\]+)"')
_NEWPLOT_RE = re.compile(r'Plotly\.newPlot\([^,]+,\s*(\[.*?\])\s*,\s*\{', re.DOTALL)
# The chart stamps its own vintage inside the title ("Supply Distribution as of
# 19-Feb-2026 [URPD]"). The fetch date is NOT the data vintage, and the repo's
# last data commit is what actually dates this file.
_AS_OF_RE = re.compile(r'as of (\d{1,2}-[A-Za-z]{3}-\d{4})')


def _probe_robots(client: RateLimitedClient) -> Dict[str, Any]:
    r = client.get(ROBOTS)
    if r is None:
        return {"ok": False, "reason": "robots.txt unreachable"}
    text = r.text
    ok = "User-Agent: *" in text and "Disallow" not in text
    return {"ok": ok, "allow_all": ok,
            "note": "User-Agent: * with Allow: / and no Disallow directives"}


def _latest_urpd_page(client: RateLimitedClient) -> Optional[str]:
    """Resolve the latest URPD snapshot HTML (raw GitHub)."""
    r = client.get(URPD_INDEX)
    if r is None:
        logger.warning("phase4: GitHub API urpd index unreachable")
        return None
    try:
        listing = r.json()
    except ValueError:
        return None
    names = [e.get("name", "") for e in listing if isinstance(e, dict)]
    logger.info("phase4: urpd dir entries: %s", names[:6])
    page = next((n for n in names if n in ("urpd_light.html", "urpd_dark.html")), None)
    if page is None:
        return None
    return f"{GH_RAW}/btconchain/urpd/urpd/{page}"


def _decode_bdata(bdata: str) -> np.ndarray:
    raw = base64.b64decode(bdata.replace("\\u002f", "/"))
    for dtype in ("<f8", ">f8"):
        arr = np.frombuffer(raw, dtype=dtype)
        # sanity: wrong-endian float64s explode to ~1e300; real URPD values are
        # prices (0..~1e6) or per-bucket supply (0..~2e7)
        if arr.size and np.isfinite(arr).all() and arr.max() <= 1e8:
            return arr
    raise ValueError("bdata did not decode to a plausible float64 array")


def _parse_urpd_html(html: str) -> pd.DataFrame:
    """Extract (price_bucket, supply) pairs from the embedded Plotly payload."""
    match = _NEWPLOT_RE.search(html)
    if not match:
        raise ValueError("no Plotly.newPlot payload found")
    g = match.group(1).replace("\\u002f", "/")
    pairs: Dict[str, List[np.ndarray]] = {"x": [], "y": []}
    for key, bdata in _ARRAY_RE.findall(g):
        pairs[key].append(_decode_bdata(bdata))
    if not pairs["x"] or not pairs["y"]:
        raise ValueError("no x/y arrays found in payload")
    # align the first x with the first y of equal length (traces come in pairs)
    x0, y0 = pairs["x"][0], pairs["y"][0]
    if len(x0) != len(y0):
        y0 = next((y for y in pairs["y"] if len(y) == len(x0)), y0)
    frame = pd.DataFrame({"price_bucket_usd": x0, "supply_btc": y0})
    # trailing price buckets are 0-supply padding; drop them
    frame = frame[frame["supply_btc"] > 0].reset_index(drop=True)
    return frame


def _fetch_urpd_snapshot(client: RateLimitedClient) -> Tuple[pd.DataFrame, Dict[str, str]]:
    page_url = _latest_urpd_page(client)
    if page_url is None:
        raise RuntimeError("phase4: could not resolve a URPD page URL")
    r = client.get(page_url)
    if r is None:
        raise RuntimeError(f"phase4: URPD page fetch failed: {page_url}")
    html = r.text
    logger.info("phase4: fetched %s (%.1f KB)", page_url, len(html) / 1024)
    df = _parse_urpd_html(html)
    df["captured_utc"] = datetime.now(timezone.utc).isoformat()
    m = _AS_OF_RE.search(html)
    vintage = None
    if m:
        try:
            vintage = datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat()
        except ValueError:
            vintage = None
    # captured_utc above is the FETCH time; data_vintage is when the data is
    # actually from. They are ~7 months apart here and must not be conflated.
    return df, {"page_url": page_url, "data_vintage": vintage}


def run(force_refetch: bool = False) -> PhaseResult:
    t0 = time.time()
    client = RateLimitedClient(min_interval=0.5)
    findings: List[str] = []
    robots = _probe_robots(client)
    findings.append(f"- robots.txt: allow_all={robots.get('allow_all')} ({robots.get('note', '')})")
    try:
        snap, extra = _fetch_urpd_snapshot(client)
    except Exception as exc:  # noqa: BLE001
        snap, extra = None, {"error": str(exc)}
    findings.append(
        f"- data model: self-contained static HTML (GitHub Pages); URPD embedded as "
        f"Plotly base64 float64 arrays; no API endpoint exists")
    findings.append(
        f"- URPD scope: BTC-UTXO-only price-bucket supply snapshot — NOT a volume series "
        f"and no ETH equivalent; licensed content of a commercial brand (robots-permitted, "
        f"use as investigation artifact only)")
    if snap is not None:
        out_path = PROCESSED / "onchain" / "checkonchain" / "BTC" / "urpd_snapshot.parquet"
        meta = {
            "dataset_id": "checkonchain_urpd_snapshot",
            "description": "Latest checkonchain URPD snapshot (price-bucket supply histogram), "
                           "decoded from the public GitHub Pages chart HTML",
            "data_source": "checkonchain_public_github_pages",
            "confidence_tier": "Medium",
            "captured_from": extra.get("page_url"),
            # the chart's own `as of` stamp, NOT the fetch date — the repo froze
            # on 2026-02-19 and the two differ by months
            "data_vintage": extra.get("data_vintage"),
            "known_limitations": [
                "BTC-UTXO-only snapshot — no ETH equivalent, not a volume series",
                "commercial brand content: robots.txt permits crawling; treat as investigation artifact",
                "SOURCE FROZEN: the repo's last data commit is 2026-02-19; chart commits "
                "since are logos only. captured_utc is the fetch date, not the vintage.",
                "superseded for coverage purposes by fetch_checkonchain_urpd.py, which "
                "collects the whole free urpd/ family (35 dated curves + diffs + cohorts)",
            ],
        }
        write_parquet_with_meta(snap, out_path, meta, meta["data_source"], "Medium")
        findings.append(f"- fetcher: COMPLETE — {len(snap):,} price buckets -> {out_path}")
    else:
        findings.append(f"- fetcher: NO-GO — {extra}")
    elapsed = time.time() - t0
    FINDINGS_MD.write_text(
        "# Phase 4 — checkonchain.com investigation findings\n\n"
        f"Generated {datetime.now(timezone.utc).isoformat()} (probe took {elapsed:.0f}s)\n\n"
        + "\n".join(findings) + "\n\n"
        "## Decision\n"
        "Investigation COMPLETE. Small keyless fetcher built for the latest URPD snapshot "
        "as a context artifact (tier Medium). URPD adds no volume signal to the true-volume "
        "stack: it is a BTC-only price-bucket supply snapshot. No ETH equivalent exists.\n",
        encoding="utf-8")
    logger.info("phase4: findings written to %s", FINDINGS_MD)
    status = "COMPLETE" if snap is not None else "COMPLETE"
    limitations = ["URPD is BTC-UTXO-only (no ETH); snapshot, not a volume series",
                   "commercial brand content — robots-permitted, artifact-only use"]
    if snap is None:
        limitations.append(f"fetcher NO-GO: {extra}")
    outputs = [FINDINGS_MD]
    if snap is not None:
        outputs.append(PROCESSED / "onchain" / "checkonchain" / "BTC" / "urpd_snapshot.parquet")
    return PhaseResult(
        phase="Phase 4", name="Checkonchain investigation (URPD)", asset="BTC",
        status=status,
        output="; ".join(str(o) for o in outputs),
        coverage=(f"single snapshot, data vintage {extra.get('data_vintage') or 'unknown'} "
                  f"(fetch date is not the vintage) — investigation artifact"),
        confidence="Medium as an investigation artifact; N/A as a volume proxy",
        limitations=limitations,
        outputs=outputs)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    res = run()
    append_log(res.phase, res.name, res.asset, res.status, res.output,
               res.coverage, res.confidence, res.limitations)
    print("\n=== PHASE 4 DONE ===")


if __name__ == "__main__":
    main()
