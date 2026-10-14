"""check_cb_groundtruth.py — is the dated-curve archive really the publisher's numbers?

The archive behind the visualizer's any-window cost basis (`window=custom`) is
only worth having if it IS checkonchain's own URPD, so this compares it against
the publisher's own diff charts — the ones already stored in `urpd_diffs.parquet`,
which name both ends of their window and carry their own subtraction.

What it establishes, and why each part is worded the way it is:

  * A SETTLED past end is bit-identical.  For urpd_diff_90/180/365,
    urpd_diff_2024-11-01 and urpd_diff_2025-03-01 the archive-derived earlier
    curve matches the chart to 0.0000 BTC across all 126 $1,000 zones.
  * urpd_diff_30's past end is EXCLUDED, not tolerated: the last ~month is still
    revised upstream (the chart's past is ~29.9k BTC heavier in one zone), which
    is exactly why the collector re-fetches the newest three anchors every run.
  * The "now" end may differ in ONE zone (~43k BTC at $80k on 2026-09-20, with
    spot sitting on that boundary) and by a few hundred BTC net over 20M.  That
    is today's curve being computed twice, hours apart — provisional, not wrong.

The chart's `prev_as_of_date` is not always a weekly anchor (2024-11-01 is a
Friday), so the earlier curve is fetched through the collector's own
`fetch_series` — same validation, same per-date cache — rather than jumping to
the nearest archived date, which would prove nothing.

Run after a backfill, or whenever a number from this store is being doubted.
Exits non-zero if a settled comparison fails.
"""

from __future__ import annotations

import sys
from datetime import date
from typing import Dict, List, Tuple

import pandas as pd

try:
    from .common import PROCESSED, logger
    from .fetch_checkonchain_urpd import (RateLimitedClient, fetch_series,
                                          BITVIEW_UA)
except ImportError:      # direct script execution
    from common import PROCESSED, logger
    from fetch_checkonchain_urpd import (RateLimitedClient, fetch_series, BITVIEW_UA)

DIFFS = PROCESSED / "onchain" / "checkonchain" / "BTC" / "urpd_diffs.parquet"

# The charts' own $1,000 zone.  $1,000 is the finest width on which the archive's
# $200 lattice and the charts' $500/$200 lattices all nest exactly -- the same
# reason app.CB_ZONE_USD exists.
ZONE_USD = 1000.0

# chart stem -> (past, now) as the chart itself labels them.
SETTLED: Dict[str, Tuple[str, str]] = {
    "urpd_diff_90": ("2026-06-22", "2026-09-20"),
    "urpd_diff_180": ("2026-03-24", "2026-09-20"),
    "urpd_diff_365": ("2025-09-20", "2026-09-20"),
    "urpd_diff_2024-11-01": ("2024-11-01", "2026-09-20"),
    "urpd_diff_2025-03-01": ("2025-03-01", "2026-09-20"),
}
PROVISIONAL: Dict[str, Tuple[str, str]] = {"urpd_diff_30": ("2026-08-21", "2026-09-20")}

NOW_BOUND_ONE_ZONE = 50_000.0     # BTC, in a single zone
NOW_BOUND_NET = 1_000.0           # BTC, over the whole curve


def zones(curve: Dict[float, float]) -> Dict[int, float]:
    """Group a price -> supply curve onto the $1,000 lattice.

    No upper cutoff, even though the charts stop at $125,500: that last bucket is
    a catch-all for [its floor, inf), so supply our finer $200 lattice keeps above
    it still falls in the same $1,000 zone.  Clipping at the chart's top instead
    makes the top zone read 251 BTC short on 2026-06-22 and 440 BTC short on
    2026-03-24 -- and an apparent disagreement that is only an artefact of the
    comparison is worse than no comparison.
    """
    out: Dict[int, float] = {}
    for p, v in curve.items():
        key = int(p // ZONE_USD) * 1000
        out[key] = out.get(key, 0.0) + v
    return out


def compare(mine: Dict[int, float], chart: Dict[int, float]) -> Tuple[float, int, int]:
    """(worst |diff|, its zone, zones differing by more than 0.01 BTC)."""
    worst, zone, n = 0.0, 0, 0
    for z in sorted(set(mine) | set(chart)):
        d = abs(mine.get(z, 0.0) - chart.get(z, 0.0))
        if d > 0.01:
            n += 1
        if d > worst:
            worst, zone = d, z
    return worst, zone, n


def main() -> int:
    if not DIFFS.exists():
        print(f"missing {DIFFS} — run fetch_checkonchain_urpd.py first")
        return 1
    d = pd.read_parquet(DIFFS)
    d = d[d["variant"] == "supply"]
    print(f"charts on disk: {d['chart'].nunique()} supply diffs, "
          f"{d['as_of_date'].nunique()} as-of dates")

    worst_id = max((g["past_value"] + g["delta"] - g["current_value"]).abs().max()
                   for _, g in d.groupby("chart"))
    print(f"\n== the publisher's own trace identity (past + delta == current)\n"
          f"   worst |err| over {len(d):,} rows: {worst_id:.3e} BTC")

    client = RateLimitedClient(min_interval=1.0, user_agent=BITVIEW_UA)
    print(f"\n== the archive's pair vs the publisher's chart, ${ZONE_USD:,.0f} zones")
    fails: List[str] = []
    for stem, (past_day, now_day) in {**SETTLED, **PROVISIONAL}.items():
        g = d[d["chart"].str.contains(stem, regex=False)]
        if g.empty:
            print(f"   {stem}: not on disk, skipped")
            continue
        assert (g["prev_as_of_date"].iloc[0], g["as_of_date"].iloc[0]) == \
            (past_day, now_day), f"{stem}: chart disagrees about its own dates"
        cp = zones(dict(zip(g["price_bucket_usd"], g["past_value"])))
        cn = zones(dict(zip(g["price_bucket_usd"], g["current_value"])))
        past_curve, _ = fetch_series(client, date.fromisoformat(past_day))
        now_curve, _ = fetch_series(client, date.fromisoformat(now_day))
        if past_curve is None or now_curve is None:
            fails.append(f"{stem}: the series API answered nothing for "
                         f"{past_day} / {now_day}")
            continue
        wp, zp, npast = compare(zones(past_curve), cp)
        wn, zn, nnow = compare(zones(now_curve), cn)
        net = sum(zones(now_curve).values()) - sum(cn.values())

        provisional = stem in PROVISIONAL
        print(f"   {stem:24s} {past_day} -> {now_day}  {len(cp):3d} zones"
              f"{'   [PROVISIONAL past end]' if provisional else ''}")
        print(f"      past : worst |diff| {wp:,.4f} BTC at ${zp:,}, "
              f"{npast} zone(s) off by >0.01")
        print(f"      now  : worst |diff| {wn:,.2f} BTC at ${zn:,}, "
              f"{nnow} zone(s) off, net {net:+,.2f} BTC over "
              f"{sum(cn.values()) / 1e6:.4f}M")
        if not provisional:
            if wp > 0.01:
                fails.append(f"{stem}: settled past end differs by {wp:,.3f} BTC "
                             f"at ${zp:,}")
            if wn > NOW_BOUND_ONE_ZONE or abs(net) > NOW_BOUND_NET:
                fails.append(f"{stem}: now end off by {wn:,.0f} BTC in one zone / "
                             f"{net:+,.0f} BTC net (bounds "
                             f"{NOW_BOUND_ONE_ZONE:,.0f} / {NOW_BOUND_NET:,.0f})")

    print("\nRESULT:", "PASS — settled past ends exact, now ends within the "
          "provisional bounds" if not fails else "FAIL")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
