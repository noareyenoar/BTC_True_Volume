"""Collect the free checkonchain URPD family + age-cohort series.

The project previously recorded this data as unavailable: one 630-bucket URPD
snapshot, and ``onch_sopr`` / ``lth_supply_pct`` / ``sth_supply_pct`` empty
behind an unapproved, disk-infeasible Phase 8 full node.  checkonchain's public
GitHub Pages repo carries it for free -- every ``*_light.html`` chart is a
self-contained page whose data is embedded as Plotly typed arrays, no API and
no key.

This generalizes the decode machinery in ``phase4_checkonchain_investigation``
(whose narrow snapshot path keeps working), fixing its three real limits:

* ``_NEWPLOT_RE`` is a non-greedy regex that truncates on the larger payloads
  -> bracket-matched extraction of the ``Plotly.newPlot(id, [traces], ...)``
  argument, quote- and escape-aware.
* ``_ARRAY_RE`` is not trace-aware -- it takes the first x and first y, which
  is the wrong pair on a six-trace diff chart -> decode per trace, keyed by
  ``name`` and ``type``.
* ``_decode_bdata`` rejects any array whose ``max > 1e8``; the ``_volume_usd``
  and ``_realised`` variants legitimately exceed that -> trust the declared
  dtype and tolerate NaN.

Two things are treated as first-class here:

**Tracing.**  A diff chart carries the past URPD curve, the current URPD curve
and the per-bucket delta.  ``trace0 == trace2 - trace1`` holds exactly, which is
asserted rather than assumed -- drift there means a mis-selected trace pair.

**Dating.**  The date cannot come from the filename.  Each curve is stamped from
its own ``as of <DD-MMM-YYYY>`` annotation, falling back to the diff traces'
``URPD on <date>`` labels.  The families genuinely disagree (the plain diff
curves are stamped 18-Feb-2026 while ``_realised``/``_volume_usd`` are stamped
10-Feb-2026), so every row carries its own date rather than inheriting one.

**Two sources, and the split matters.**  checkonchain deleted its GitHub Pages
CNAME on 2026-02-19 and moved the charts to ``charts.checkonchain.com`` (payloads
served from ``charts-cdn.checkonchain.com``).  The GitHub mirror we used to read
froze that day, which is why every store here carried a 2026-02 vintage while the
price chart on the same page was live.  The live host is now read first; it is
current to the day (measured 2026-09-20: the 30d diff spans 21-Aug-2026 ->
20-Sep-2026, while the mirror still ended at 2026-02-18).

The live host serves only the CURRENT generation of charts.  The historical
ones -- the 2016-2023 date-pair diffs, the 2019/2021/2022 snapshots, every
``_volume_usd`` variant, ``urpd_diff_60_realized`` and the whole 14-day window --
404 there.  Those are read from the frozen mirror and then from our own raw
cache; their bytes cannot change because the data behind them no longer updates.
The retired 14-day window is skipped outright rather than silently mixed in: one
window that reads February while its neighbours read today is exactly the
confusion this file exists to end.

The live host also coarsened the price grid for the snapshot/cohort charts, from
$200 to $500 buckets (630 -> 252 rows on the plain curve).  Diffs still arrive at
$500.  Nothing downstream assumes a bucket width; the overlay's own $1,000 zones
aggregate either.

**A third source, for arbitrary dates.**  Every chart above carries a fixed
window, so any comparison the reader picks has to be assembled from dated curves
-- and the chart family only stamps a handful of those.  checkonchain's own URPD
Analysis Tool gets its arbitrary dates from ``bitview.space``, and that endpoint
serves a full curve for every published date back to 2009-01-03.  This file
backfills one date a week from 2016 into ``urpd_archive.parquet`` (a separate
store, on its own $200 grid) and keeps its newest anchors current, which is what
makes a cost-basis window of the reader's choosing possible at all.  The archive
is verified against the publisher's own precomputed diffs -- see the section
comment above ``BITVIEW_BASE``.
"""
from __future__ import annotations

import argparse
import base64
import bisect
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from .common import (RAW, DS, RateLimitedClient, atomic_write_json,
                         atomic_write_parquet, load_json_cache, logger,
                         save_json_cache)
except ImportError:  # direct script execution
    from common import (RAW, DS, RateLimitedClient, atomic_write_json,
                        atomic_write_parquet, load_json_cache, logger,
                        save_json_cache)

logger = logging.getLogger("tvp.checkonchain_urpd")

GH_REPO = "checkmatey/checkonchain.com"
GH_TREE = f"https://api.github.com/repos/{GH_REPO}/git/trees/main?recursive=1"
# Upstream's abandoned mirror.  Still the only source for the historical charts
# (the live host dropped them) and the last-resort fallback for everything else.
GH_RAW = f"https://raw.githubusercontent.com/{GH_REPO}/main"
# The live host.  Payloads are fetched from the CDN; the www page is only an
# index of what currently exists.
CDN_BASE = "https://charts-cdn.checkonchain.com"
CDN_INDEX = "https://charts.checkonchain.com/"
# Windows the live host no longer publishes.  Dropped, not merged: a window that
# stamps February beside windows that stamp today is worse than no window.
RETIRED_DIFF_WINDOWS = frozenset({14})
# Sidecar prefix recording "this chart 404s on the live host; the bytes came from
# the frozen mirror and cannot change again" -- which is what lets a routine
# refresh skip the network for the ~30 historical charts entirely.
LEGACY_STAMP = "legacy\t"
# Stamps written by the previous tree-sha gate hold a 40-hex blob sha.  That is
# not a date, and sending it as If-Modified-Since risks a 400 on servers that
# reject it instead of ignoring it -- which would quietly downgrade the refresh
# to "use the stale cache".  Only a real HTTP date is ever sent as a validator.
_HTTP_DATE_RE = re.compile(r"^[A-Z][a-z]{2}, \d{1,2} [A-Z][a-z]{2} \d{4} ")

OUT_DIR = Path(__file__).resolve().parent / "processed" / "onchain" / "checkonchain" / "BTC"
CACHE_DIR = RAW / "checkonchain"
TREE_CACHE = CACHE_DIR / "tree.json"
# The older curve store, read only to cross-check the archive against it.
CB_CURVES = OUT_DIR / "urpd_curves.parquet"
# Width both curve generations divide exactly, so a cross-check zone is a sum
# and never an interpolation.  Same constant, same reason, as CB_ZONE_USD in
# visualizer/app.py.
CB_ZONE_USD = 1000.0

# ---------------------------------------------------------------- decoding --

# Plotly typed arrays: {"dtype": "f8", "bdata": "<base64>"}.  Byte order is
# little-endian in every file observed, but the declared dtype is honoured and
# a big-endian retry covers the case where it is not.
_DTYPES = {"f8": "f8", "f4": "f4", "i8": "i8", "i4": "i4", "i2": "i2",
           "i1": "i1", "u1": "u1", "u8": "u8"}

_AS_OF_RE = re.compile(r"as of (\d{1,2}-[A-Za-z]{3}-\d{4})")
_URPD_ON_RE = re.compile(r"URPD on (\d{1,2}-[A-Za-z]{3}-\d{4})")
# A declared window is `urpd_diff_<N>` with a 1-3 digit N.  The family also
# carries ARBITRARY date-pair diffs (`urpd_diff_2016-01-01_2017-12-17`,
# `urpd_diff_2025-08-30`) whose folder would otherwise parse as an absurd
# window, so the match must be anchored and bounded.
_WINDOW_RE = re.compile(r"^urpd_diff_(\d{1,3})$")
_VARIANT_SUFFIXES = ("_volume_usd", "_realized", "_realised")


def _plotly_arg(html: str, key: str = "Plotly.newPlot") -> str:
    """Return the balanced ``[...]`` argument of ``Plotly.newPlot(id, [...],``.

    A non-greedy regex truncates on big payloads; the payload is JSON with
    quoted strings that may contain brackets, so this scans with quote and
    escape awareness.
    """
    start = html.find(key)
    if start < 0:
        raise ValueError(f"no {key} payload found")
    open_at = html.index("[", start)
    depth = 0
    in_str = False
    escaped = False
    for i in range(open_at, len(html)):
        ch = html[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return html[open_at:i + 1]
    raise ValueError("unbalanced Plotly payload")


def parse_traces(html: str) -> List[Dict[str, Any]]:
    """Decode the trace list from a checkonchain chart page."""
    raw = _plotly_arg(html).replace("\\u002f", "/")
    traces = json.loads(raw)
    if not isinstance(traces, list) or not traces:
        raise ValueError("Plotly payload is not a non-empty trace list")
    return traces


def decode_arr(spec: Any) -> Optional[np.ndarray]:
    """Decode one x/y spec to float64, preserving NaN.  None if not decodeable.

    NaN is kept rather than rejected: the diff charts pad the past curve with
    NaN outside its own price range, and that padding is meaningful (it is what
    makes ``trace0 == trace2 - nan_to_num(trace1)`` hold).
    """
    if spec is None:
        return None
    if isinstance(spec, dict):
        if "bdata" not in spec:
            return None
        blob = base64.b64decode(spec["bdata"].replace("\\u002f", "/"))
        code = _DTYPES.get(str(spec.get("dtype", "f8")))
        if code is None:
            raise ValueError(f"unsupported Plotly dtype {spec.get('dtype')!r}")
        for endian in ("<", ">"):
            arr = np.frombuffer(blob, dtype=endian + code)
            if arr.dtype.kind == "f":
                out = arr.astype("float64")
                # A wrong-endian float64 explodes to ~1e300.  Real values here
                # are prices, per-bucket BTC, or USD aggregates -- all finite
                # and far below 1e18.
                if np.nanmax(np.abs(out)) < 1e18 if out.size else True:
                    return out
            else:
                return arr.astype("float64")  # ints decode unambiguously
        raise ValueError("bdata did not decode as plausible float64")
    if isinstance(spec, list):
        if not spec:
            return np.array([], dtype="float64")
        if isinstance(spec[0], str):
            return np.array(spec, dtype=object)  # ISO date labels
        return np.array([np.nan if v is None else float(v) for v in spec],
                        dtype="float64")
    return None


def as_of_date(html: str) -> Optional[date]:
    """The chart's own ``as of <DD-MMM-YYYY>`` stamp, if it carries one."""
    m = _AS_OF_RE.search(html)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d-%b-%Y").date()
    except ValueError:
        return None


def _title(html: str) -> str:
    """The chart's own <b> title line, for logging only (not used to classify)."""
    m = re.search(r'"title":\s*\{\s*"text":\s*"(.*?)"', html)
    if not m:
        return ""
    return (m.group(1).replace("\\u003c", "<").replace("\\u003e", ">")
            .replace("\\u002f", "/").replace("<b>", "").replace("</b>", ""))


def detail_of(path: str) -> Optional[str]:
    """A chart's trailing bare-number parameter, when it carries one.

    The cohort family includes scenario charts parameterised by a price
    (`urpd_unrealised_cohort_2019-02-15_3300` vs `..._2022-12-31_16500`).  They
    share a date and grid with the unparameterised chart but are different
    series, so the parameter has to be part of a curve's identity or the dedup
    pass silently merges them.  Only the cohort family is parameterised this
    way; requiring 4+ digits keeps `urpd_diff_90` from picking up `90`.
    """
    folder = path.split("/")[-2].lower()
    if "cohort" not in folder:
        return None
    m = re.search(r"_(\d{4,6})$", folder)
    return m.group(1) if m else None


def units_for(variant: str) -> str:
    """Supply is BTC; the other families are USD aggregates.

    ``realized`` is invested wealth (cumulative USD paid, by bucket) and
    ``unrealized`` is unrealised P/L -- both ~1e12 for BTC, which is how the
    mislabelling would show up if it were wrong.
    """
    return "BTC" if variant == "supply" else "USD"


def _named(traces: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    return next((t for t in traces if (t.get("name") or "").strip() == name), None)


def _iso_dates(arr: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """Normalise an ``x`` array to ISO date strings."""
    if arr is None:
        return None
    return np.array([str(v)[:10] for v in arr], dtype=object)


# ------------------------------------------------------------- classifying --

def classify(path: str) -> Tuple[str, str, Optional[int]]:
    """(kind, variant, window_days) from a chart path.  kind in
    {snapshot, diff, cohort}."""
    parts = path.split("/")
    folder = parts[-2] if len(parts) >= 2 else parts[-1]
    low = folder.lower()
    if "cohort" in low:
        kind = "cohort"
    elif "diff" in low:
        kind = "diff"
    else:
        kind = "snapshot"
    # "unrealised" contains "realis", so it must be tested first; both British
    # ("realised") and American ("realized") spellings appear across the repo.
    if "volume_usd" in low:
        variant = "volume_usd"
    elif "unrealis" in low or "unrealiz" in low:
        variant = "unrealized"
    elif "realis" in low or "realiz" in low:
        variant = "realized"
    else:
        variant = "supply"
    base = low
    for suf in _VARIANT_SUFFIXES:
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    m = _WINDOW_RE.match(base)
    return kind, variant, (int(m.group(1)) if m else None)


# ------------------------------------------------------------------ fetch --

SNAPSHOT_CURVE = "current"   # trace 1 of a snapshot chart == the total curve
DIFF_DELTA, DIFF_PAST, DIFF_CURRENT = 0, 1, 2
DIFF_PCT_ACC, DIFF_PCT_DIST = 4, 5


def list_charts(client: RateLimitedClient) -> Tuple[List[str], str]:
    """Enumerate the charts to consider, from three places.

    No single listing is authoritative any more.  The mirror's tree is the only
    place the *historical* charts are still listed; the live host's index is the
    only place a *new* chart would appear; and the raw cache holds charts that
    were fetched before either listing could be asked.  The union is the honest
    answer, and a chart that 404s everywhere is simply skipped.

    Returns (paths, tree_sha) -- the sha is recorded in the store metadata as the
    mirror revision the history came from, not as a refresh gate (a frozen mirror
    can never signal new data; ``_fetch``'s per-chart validators do that now).
    """
    paths: set = set()
    tree, sha = [], ""
    r = client.get(GH_TREE)
    if r is not None:
        payload = r.json()
        tree, sha = payload["tree"], payload.get("sha", "")
        save_json_cache(TREE_CACHE, {"tree": tree, "sha": sha,
                                     "fetched_utc": datetime.now(timezone.utc).isoformat()})
    else:
        cached = load_json_cache(TREE_CACHE)
        if cached:
            logger.warning("checkonchain: tree API unreachable, using cached tree")
            tree, sha = cached["tree"], cached["sha"]
        else:
            logger.warning("checkonchain: tree API unreachable and uncached "
                           "-- live index and raw cache only")
    paths |= {b["path"] for b in tree
              if b.get("type") == "blob"
              and b["path"].startswith("btconchain/urpd/")
              and b["path"].endswith("_light.html")}

    idx = client.get(CDN_INDEX)
    if idx is not None:
        paths |= set(re.findall(r'href="(btconchain/urpd/[^"]+_light\.html)"', idx.text))
    else:
        logger.warning("checkonchain: live index unreachable (new charts unseen)")

    # The cache filename IS the path with "/" -> "__" -- keep the extension, or
    # every cached chart is re-asked for under a name that exists nowhere.
    for f in CACHE_DIR.glob("btconchain__*.html") if CACHE_DIR.exists() else []:
        paths.add(f.name.replace("__", "/"))

    # The holder-panel series are listed on the index; asked for unconditionally
    # anyway, because _fetch degrades to the cached copy rather than failing.
    return sorted(paths | set(SERIES_CHARTS)), sha


# Per-chart trace-name -> (metric, units).  A single global map is wrong here:
# the breakdown family ships twice, and `_0` (absolute BTC) and `_1` (the same
# quantities as a SHARE OF TOTAL SUPPLY) share every trace name.  Measured, not
# assumed -- in `_1`, profit + loss == total and each trace equals
# abs/total_supply to 1e-8, while abs/cohort differs by 0.57-0.80.  A shared map
# silently merged a BTC series with a fraction under one metric name.
_PRICE = {"Price": ("price_usd", "USD")}

SERIES_CHARTS: Dict[str, Dict[str, Tuple[str, str]]] = {
    # absolute cohort supply, in BTC
    "btconchain/supply/breakdown_lthsth_0/breakdown_lthsth_0_light.html": {
        "LTH-in-Profit": ("lth_supply_profit_btc", "BTC"),
        "LTH-in-Loss": ("lth_supply_loss_btc", "BTC"),
        "LTH Total": ("lth_supply_btc", "BTC"),
        "STH-in-Profit": ("sth_supply_profit_btc", "BTC"),
        "STH-in-Loss": ("sth_supply_loss_btc", "BTC"),
        "STH Total": ("sth_supply_btc", "BTC"),
        **_PRICE,
    },
    # the same series as a share of TOTAL supply (0..1), not within-cohort
    "btconchain/supply/breakdown_lthsth_1/breakdown_lthsth_1_light.html": {
        "LTH-in-Profit": ("lth_profit_share", "fraction"),
        "LTH-in-Loss": ("lth_loss_share", "fraction"),
        "LTH Total": ("lth_supply_share", "fraction"),
        "STH-in-Profit": ("sth_profit_share", "fraction"),
        "STH-in-Loss": ("sth_loss_share", "fraction"),
        "STH Total": ("sth_supply_share", "fraction"),
        **_PRICE,
    },
    "btconchain/realised/sopr/sopr_light.html": {
        "Spent Output Profit Ratio (SOPR)": ("sopr", "ratio"),
        "SOPR 7D-EMA": ("sopr_ema_7d", "ratio"),
        **_PRICE,
    },
    # the chart's own y-axis label reads "Realised Profit/Loss BTC" -- these are
    # BTC, not USD, and losses come through signed negative
    "btconchain/realised/realisedpnl_bycohort_bands/realisedpnl_bycohort_bands_light.html": {
        "Realised Profit": ("realized_profit_btc", "BTC"),
        "Realised Loss": ("realized_loss_btc", "BTC"),
        **_PRICE,
    },
    "btconchain/unrealised/mvrv_sth/mvrv_sth_light.html": {
        "STH-MVRV": ("sth_mvrv", "ratio"),
        "STH Realised Price": ("sth_realized_price_usd", "USD"),
        **_PRICE,
    },
    # Daily net flows of the US spot ETFs, in USD -- the "why did price move"
    # series.  987 points, 2024-01-11 (first session) -> today, weekends and
    # holidays written as an explicit 0.0, which means *no session*, not no flow.
    # Not the only daily ETF chart on the host: etf_flows_1 is the same daily
    # series in USD broken out per issuer, etf_flows_0 the same in BTC, and each
    # carries its own separate "Weekly Total Flow" trace.  This is the chart the
    # layer was specified from; etf_flows_1 is the cross-check if per-issuer
    # splits are ever wanted (decoded 2026-09-23: 2024-01-11 total 655,300,000.0
    # USD in both).
    # "Price [USD]" is deliberately NOT mapped: `price_usd` already comes from the
    # charts above, and a second source under the same metric name would put two
    # rows on one date and make app._metric_on("price_usd") order-dependent.
    "btconchain/etfs/etf_flowsvsprice/etf_flowsvsprice_light.html": {
        "ETF Net Flow [USD]": ("etf_net_flow_usd", "USD"),
    },
}


def _get(client: RateLimitedClient, url: str,
         headers: Optional[Dict[str, str]] = None,
         params: Optional[Dict[str, Any]] = None):
    """GET that returns None instead of raising on 404/other HTTP errors.

    RateLimitedClient raises on 404 by design (the caller may want to know), and
    a 404 is the *normal* answer here: the live host has dropped half the chart
    family we keep history for, so walking on is the expected path, not an error.
    The 404 response itself is handed back rather than swallowed -- "the live
    host does not have this chart" and "the network failed" mean opposite things
    downstream, and only the first may pin a chart to its cached bytes.
    """
    try:
        return client.get(url, params=params, headers=headers)
    except Exception as exc:  # noqa: BLE001 -- requests.HTTPError, plus transport noise
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code == 404:
            return resp
        return None


def _is_legacy(path: str) -> bool:
    """True if the copy we hold of this chart came from the frozen mirror.

    Read off the sidecar stamp rather than re-deriving it: the stamp is written
    by whichever fetch won, so it is the only place the provenance survives a
    restart.  A missing sidecar counts as live (the default, current generation).
    """
    side = (CACHE_DIR / path.replace("/", "__")).with_suffix(".sha")
    try:
        return side.read_text(encoding="utf-8").lstrip().startswith(LEGACY_STAMP)
    except OSError:
        return False


def _fetch(client: RateLimitedClient, path: str, *,
           use_cache: bool = True) -> Tuple[Optional[str], bool]:
    """Fetch a chart HTML.  Returns ``(html, changed)``.

    ``changed`` is the incremental signal now that the source is live: the old
    gate was the repo tree sha, which cannot move on a mirror nobody updates.
    A chart is "changed" when its bytes came off the network and differ from what
    we already held -- so the mirror's cosmetic commits and its dead files cost
    nothing while a real update costs exactly one rewrite of the stores.

    Source order: the live CDN (conditional), then the frozen mirror (conditional),
    then our own raw cache.  A chart the live host has dropped and that we already
    hold from the mirror is served from cache without touching the network at all:
    both copies are frozen, and re-proving that daily buys nothing.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local = CACHE_DIR / path.replace("/", "__")
    side = local.with_suffix(".sha")
    cached = local.read_text(encoding="utf-8", errors="replace") if local.exists() else None
    stamp = side.read_text(encoding="utf-8").strip() if side.exists() else ""

    if use_cache and cached is not None and stamp.startswith(LEGACY_STAMP):
        return cached, False

    validator = stamp if _HTTP_DATE_RE.match(stamp) else ""
    conditional = ({"If-Modified-Since": validator}
                   if (use_cache and cached and validator) else None)
    dropped_live = False
    for base in (CDN_BASE, GH_RAW):
        r = _get(client, f"{base}/{path}", conditional)
        if r is None:
            continue
        if r.status_code == 404:
            # Nothing at this address.  Only the CDN's own 404 licenses the
            # frozen stamp below: a transient CDN failure must never pin a
            # current chart to its cached bytes for good.
            dropped_live = dropped_live or base == CDN_BASE
            continue
        if r.status_code == 304 and cached is not None:
            return cached, False
        last_modified = r.headers.get("Last-Modified") or r.headers.get("ETag") or ""
        text = r.text
        frozen = base == GH_RAW and dropped_live
        if cached is not None and text == cached:
            # Same bytes: nothing to rewrite, whatever the validator said.  This
            # also re-stamps the sidecar so the next run skips the network.
            if frozen:
                side.write_text(LEGACY_STAMP + last_modified, encoding="utf-8")
            elif last_modified:
                side.write_text(last_modified, encoding="utf-8")
            return cached, False
        tmp = local.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(local)
        if frozen:
            side.write_text(LEGACY_STAMP + last_modified, encoding="utf-8")
        elif last_modified:
            side.write_text(last_modified, encoding="utf-8")
        return text, True

    if cached is not None:
        logger.warning("checkonchain: %s unreachable, using cached copy", path)
        return cached, False
    logger.warning("checkonchain: no content for %s (and nothing cached)", path)
    return None, False


# ------------------------------------------------------------------ build --

def build_curves(rows: List[Dict[str, Any]], chart: str, kind: str, variant: str,
                 html: str, traces: List[Dict[str, Any]]) -> None:
    """Snapshot/cohort charts -> one row per (curve, price bucket)."""
    captured = datetime.now(timezone.utc).isoformat()
    stamped = as_of_date(html)
    if kind == "cohort":
        # The two bar traces are the age cohorts.  Which is which is taken from
        # the trace name (the naming differs across the realised/unrealised
        # families), falling back to the observed LTH-then-STH ordering.
        for idx, tr in enumerate(traces[:2]):
            nm = (tr.get("name") or "").lower()
            if "long" in nm or "lth" in nm:
                cohort = "lth"
            elif "short" in nm or "sth" in nm:
                cohort = "sth"
            elif idx == 0:
                cohort = "lth"
            else:
                cohort = "sth"
            x = decode_arr(tr.get("x"))
            y = decode_arr(tr.get("y"))
            if x is None or y is None or len(x) != len(y):
                continue
            for p, v in zip(x, y):
                if not np.isfinite(p) or not np.isfinite(v) or v == 0:
                    continue
                rows.append({
                    "as_of_date": stamped, "variant": variant, "cohort": cohort,
                    "detail": detail_of(chart),
                    "units": units_for(variant), "price_bucket_usd": float(p),
                    "value": float(v), "source_chart": chart,
                    "captured_utc": captured,
                })
        return
    # snapshot: trace 1 (name '') is the total curve; trace 0 is the same series
    # split by profit/loss and trace 2 is its percent-of-total normalisation.
    tr = _named(traces, "") or (traces[1] if len(traces) > 1 else None)
    if tr is None:
        return
    x = decode_arr(tr.get("x"))
    y = decode_arr(tr.get("y"))
    if x is None or y is None:
        return
    # Trim to the aligned head: trailing padding buckets carry zero supply.
    n = min(len(x), len(y))
    for p, v in zip(x[:n], y[:n]):
        if not np.isfinite(p) or not np.isfinite(v) or v == 0:
            continue
        rows.append({
            "as_of_date": stamped, "variant": variant, "cohort": None,
            "detail": detail_of(chart),
            "units": units_for(variant), "price_bucket_usd": float(p),
            "value": float(v), "source_chart": chart, "captured_utc": captured,
        })


def build_diff(rows: List[Dict[str, Any]], chart: str, variant: str,
               window: Optional[int], traces: List[Dict[str, Any]],
               curve_rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Diff charts -> the delta plus the two curves it spans.

    Asserts the source invariant ``trace0 == trace2 - nan_to_num(trace1)``; a
    violation means the trace pair was mis-selected and the chart is dropped
    rather than silently stored wrong.
    """
    out = {"chart": chart, "ok": False, "reason": "", "n": 0,
           "past": None, "current": None}
    if len(traces) <= DIFF_CURRENT:
        out["reason"] = "fewer than 3 traces"
        return out
    x = decode_arr(traces[DIFF_DELTA].get("x"))
    delta = decode_arr(traces[DIFF_DELTA].get("y"))
    if x is None or delta is None:
        out["reason"] = "delta trace undecodable"
        return out
    past = decode_arr(traces[DIFF_PAST].get("y"))
    cur = decode_arr(traces[DIFF_CURRENT].get("y"))
    n = min(len(x), len(delta), len(past) if past is not None else 0,
            len(cur) if cur is not None else 0)
    if n == 0:
        out["reason"] = "no aligned samples"
        return out
    resid = np.nanmax(np.abs(delta[:n] - (cur[:n] - np.nan_to_num(past[:n]))))
    if not (resid < 1e-6):
        out["reason"] = f"invariant violated (max resid {resid:g})"
        logger.warning("checkonchain: %s dropped -- %s", chart, out["reason"])
        return out

    def _d(t: Dict[str, Any]) -> Optional[date]:
        m = _URPD_ON_RE.search(t.get("name") or "")
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%d-%b-%Y").date()
        except ValueError:
            return None

    past_d, cur_d = _d(traces[DIFF_PAST]), _d(traces[DIFF_CURRENT])
    # Diff charts carry no `as of` annotation -- the two ends are named on the
    # curve traces themselves ("URPD on <date>"), so those are authoritative.
    # span_days is the honest separation and is populated even for the family's
    # arbitrary date-pair diffs, which declare no window at all.
    span = (cur_d - past_d).days if (cur_d and past_d) else None
    pa = decode_arr(traces[DIFF_PCT_ACC].get("y")) if len(traces) > DIFF_PCT_ACC else None
    pd_ = decode_arr(traces[DIFF_PCT_DIST].get("y")) if len(traces) > DIFF_PCT_DIST else None
    captured = datetime.now(timezone.utc).isoformat()
    units = units_for(variant)
    # A diff chart's two ends are full URPD curves in their own right.  Emitting
    # them is what makes the curve store cover every dated curve the repo
    # carries (the diff family is the richest source of them), and it is what
    # makes an arbitrary date-pair comparison possible later.
    if curve_rows is not None:
        for role, day, arr in (("past", past_d, past), ("current", cur_d, cur)):
            if day is None or arr is None:
                continue
            for i in range(n):
                p, v = float(x[i]), float(arr[i])
                if not np.isfinite(p) or not np.isfinite(v) or v == 0:
                    continue
                curve_rows.append({
                    "as_of_date": day, "variant": variant, "cohort": None,
                    "detail": detail_of(chart),
                    "units": units, "price_bucket_usd": p, "value": v,
                    "source_chart": chart, "captured_utc": captured,
                })
    for i in range(n):
        p = float(x[i])
        if not np.isfinite(p):
            continue
        d = float(delta[i])
        if d == 0 and (past is None or not np.isfinite(past[i])) \
                and (cur is None or not np.isfinite(cur[i])):
            continue
        rows.append({
            "chart": chart, "window_days": window, "variant": variant,
            "units": units, "price_bucket_usd": p, "delta": d,
            "pct_accumulated": float(pa[i]) if pa is not None and np.isfinite(pa[i]) else np.nan,
            "pct_distributed": float(pd_[i]) if pd_ is not None and np.isfinite(pd_[i]) else np.nan,
            "past_value": float(past[i]) if past is not None and np.isfinite(past[i]) else np.nan,
            "current_value": float(cur[i]) if cur is not None and np.isfinite(cur[i]) else np.nan,
            "as_of_date": cur_d, "prev_as_of_date": past_d, "span_days": span,
            "source_chart": chart, "captured_utc": captured,
        })
    out.update(ok=True, n=n, past=past_d, current=cur_d)
    return out


def build_series(rows: List[Dict[str, Any]], chart: str,
                 traces: List[Dict[str, Any]]) -> int:
    n = 0
    metrics = SERIES_CHARTS.get(chart, {})
    for tr in traces:
        key = (tr.get("name") or "").strip()
        metric = metrics.get(key)
        if metric is None:
            continue
        x = decode_arr(tr.get("x"))
        y = decode_arr(tr.get("y"))
        if x is None or y is None or x.dtype != object:
            continue
        units = metric[1]
        captured = datetime.now(timezone.utc).isoformat()
        for d, v in zip(x, y):
            if not isinstance(d, str) or len(d) < 10:
                continue
            if not np.isfinite(v):
                continue
            rows.append({"metric": metric[0], "date": d[:10], "value": float(v),
                         "units": units, "source_chart": chart,
                         "captured_utc": captured})
            n += 1
    return n


# ------------------------------------------------- bitview per-date series --
# The chart family above only ever carries FIXED windows, so an arbitrary
# date-pair comparison has to assemble its own pair -- which the dated curve
# store can only do for the handful of dates its charts happen to be stamped
# with.  checkonchain's own URPD Analysis Tool does not have that limit: the
# tool page sets `var BASE = "https://bitview.space"` and asks that host for a
# curve per date.  The endpoint is open and keyless and serves every published
# date back to 2009-01-03 (6,465 of them), which is what makes a window of the
# reader's choosing possible at all.
#
# Verified 2026-09-20, and the verification is the reason this is trusted:
# the historical curves are BIT-IDENTICAL to the publisher's own precomputed
# diffs (the earlier end of `urpd_diff_90/180/365` and of the two fixed
# date-pair charts: max abs difference 0.000 BTC over 126 $1,000 zones, every
# pair), while today's date -- still being restated -- differs from the published
# chart by 181 BTC of 20.09M.  So the settled end of the archive is not an
# approximation of the publisher's numbers; it IS the publisher's numbers, on the
# grid its own tool reads.
#
# Two properties of the payload shape the store:
#
# * Absent buckets are zero buckets, not unknown ones.  The grid is a clean $200
#   lattice and slots with no supply are simply missing from the JSON (measured:
#   2021-12-31 ships 339 of 343 slots), and the payload's total equals the stored
#   $500 curve's to the cent -- so a slot nobody occupies is written back as an
#   explicit 0.0 rather than left out.
# * The recent tail is provisional.  Today's curve moves through the day as
#   coins change hands, and the last ~month is still being restated upstream
#   (`urpd_diff_30`'s earlier end differs from the archive's curve for the same
#   date by 29,854 BTC in one zone).  The tail is therefore re-fetched every run
#   and everything older never is: those bytes cannot change.
#
# The archive is its own store, NOT more rows in `urpd_curves.parquet`.  The
# consensus/conflict key there is (as_of_date, variant, units, cohort, detail,
# price_bucket_usd) and does NOT include the bucket width -- so a $200 curve
# written onto a date that already holds a $500 curve would sit at the same key
# as a row it disagrees with, and the consensus pass would then hand back a curve
# that is neither grid.  That is the silent-corruption class this store has
# already suffered once.
BITVIEW_BASE = "https://bitview.space"
BITVIEW_UA = "kp_dataset_fetcher/1.0 (personal research; checkonchain URPD archive)"
SERIES_BUCKET_USD = 200.0
SERIES_DIR = CACHE_DIR / "series"
SERIES_INDEX = CACHE_DIR / "series_dates.json"
SERIES_START = date(2016, 1, 3)
SERIES_CADENCE_DAYS = 7
# Anchors are calendar dates on a $7-day step snapped onto whichever dates the
# host actually published, so a week the publisher skipped shifts its anchor by
# a day or two instead of leaving a hole in the archive.
SERIES_SNAP_DAYS = 3
SERIES_TAIL_REFRESH = 3
# How long the tail keeps moving.  Measured 2026-09-20: today's curve moves
# through the day and the last ~month is still restated upstream, so a curve
# inside this window is not evidence against the live charts and not evidence
# for them either -- it is a reading that has not settled.
SERIES_PROVISIONAL_DAYS = 35
ARCHIVE_NAME = "urpd_archive.parquet"
ARCHIVE_MODES = ("tail", "backfill", "off")


def series_url(day: date) -> str:
    return f"{BITVIEW_BASE}/api/series/cost-basis/all/{day.isoformat()}"


def _parse_date_list(payload: Any) -> List[date]:
    out = set()
    for item in payload if isinstance(payload, list) else []:
        try:
            out.add(date.fromisoformat(str(item)[:10]))
        except ValueError:
            continue
    return sorted(out)


def fetch_available_dates(client: RateLimitedClient) -> List[date]:
    """Every date the host can serve, oldest first (6,465 of them, to today).

    Cached, because a 560-date backfill must not depend on a listing call
    succeeding, and because the list only ever grows at the tail.
    """
    r = _get(client, f"{BITVIEW_BASE}/api/series/cost-basis/all/dates")
    if r is not None and r.status_code == 200:
        try:
            dates = _parse_date_list(r.json())
        except ValueError:
            dates = []
        if dates:
            save_json_cache(SERIES_INDEX, {
                "dates": [d.isoformat() for d in dates],
                "fetched_utc": datetime.now(timezone.utc).isoformat()})
            return dates
        logger.warning("bitview: date listing unusable -- falling back to cache")
    cached = load_json_cache(SERIES_INDEX) or {}
    dates = _parse_date_list(cached.get("dates") or [])
    if dates:
        logger.warning("bitview: using cached date listing (%d dates, to %s)",
                       len(dates), dates[-1])
    return dates


def nearest_anchor(available: List[date], target: date) -> Optional[date]:
    """The published date closest to ``target``, ties going to the EARLIER date.

    ``available`` must be sorted ascending.  This exact rule is ported to the
    browser (see the static decoder in visualizer/static/app.js): the server and
    the shipped archive must resolve "2024-03-15" to the same day, or the same
    window would read differently depending on which one answered.
    """
    if not available:
        return None
    i = bisect.bisect_left(available, target)
    cands = available[max(0, i - 1):i + 1]
    if not cands:
        return None
    return min(cands, key=lambda a: (abs((a - target).days), a))


def series_anchors(available: List[date], start: date = SERIES_START,
                   cadence: int = SERIES_CADENCE_DAYS,
                   snap: int = SERIES_SNAP_DAYS) -> Tuple[List[date], List[date]]:
    """(anchors, dropped) -- the weekly archive grid, snapped onto real dates."""
    if not available:
        return [], []
    anchors: List[date] = []
    dropped: List[date] = []
    seen: set = set()
    end = available[-1]
    day = start
    while day <= end:
        a = nearest_anchor(available, day)
        if a is None or abs((a - day).days) > snap:
            dropped.append(day)          # publisher skipped that whole week
        elif a not in seen:
            seen.add(a)
            anchors.append(a)
        day += timedelta(days=cadence)
    return anchors, dropped


def parse_series(text: str) -> Dict[float, float]:
    """A ``{price: supply}`` payload -> ``{float price: float BTC}``, validated.

    Anything off the $200 lattice, non-finite, negative, missing its $0 bucket,
    or summing to an implausible supply is the host changing shape under us --
    raise, so the caller keeps the cached copy instead of caching the new shape.
    """
    payload = json.loads(text)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("payload is not a non-empty object")
    out: Dict[float, float] = {}
    for key, val in payload.items():
        try:
            p, v = float(key), float(val)
        except (TypeError, ValueError):
            raise ValueError(f"non-numeric entry {key!r}")
        if not np.isfinite(p) or not np.isfinite(v) or p < 0 or v < 0:
            raise ValueError(f"non-finite or negative entry {key!r}={val!r}")
        if abs(p % SERIES_BUCKET_USD) > 1e-6:
            raise ValueError(f"price {p} is off the ${SERIES_BUCKET_USD:.0f} lattice")
        out[p] = out.get(p, 0.0) + v   # JSON collapses duplicate keys already;
        #                                accumulating keeps that explicit
    if 0.0 not in out:
        raise ValueError("no $0 bucket (every dated curve carries one)")
    total = sum(out.values())
    if not 1e5 < total < 21e6:
        raise ValueError(f"total supply {total:,.0f} BTC is implausible")
    return out


def _series_path(day: date) -> Path:
    return SERIES_DIR / f"{day.isoformat()}.json"


def load_series(day: date) -> Optional[Dict[float, float]]:
    """The cached curve for one date, or None if absent/unusable.

    The on-disk payload is the raw response, so a re-parse can always be redone
    from exactly the bytes the host sent.
    """
    p = _series_path(day)
    if not p.exists():
        return None
    try:
        return parse_series(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("bitview: cached %s unusable (%s)", p.name, exc)
        return None


def _write_series(day: date, text: str) -> bool:
    """Cache one raw payload.  True if it differed from what we held."""
    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    p = _series_path(day)
    old = p.read_text(encoding="utf-8") if p.exists() else None
    if old == text:
        return False
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):                      # transient AV lock, as in save_json_cache
        try:
            tmp.replace(p)
            return True
        except PermissionError:
            time.sleep(1.0 + attempt)
    p.write_text(text, encoding="utf-8")
    return True


def fetch_series(client: RateLimitedClient, day: date, *,
                 refresh: bool = False) -> Tuple[Optional[Dict[float, float]], bool]:
    """One date's curve.  Returns ``(curve, changed)``.

    The per-date raw cache IS the resume checkpoint for the backfill, and the
    store is always rebuilt from it -- never appended to.
    """
    if not refresh:
        cached = load_series(day)
        if cached is not None:
            return cached, False
    r = _get(client, series_url(day), params={"bucket": "lin200"})
    if r is None or r.status_code != 200:
        stale = load_series(day)
        if stale is not None:
            logger.warning("bitview: %s unreachable, using cached copy", day)
            return stale, False
        logger.warning("bitview: no curve for %s (and nothing cached)", day)
        return None, False
    try:
        curve = parse_series(r.text)
    except ValueError as exc:
        logger.warning("bitview: %s rejected -- %s", day, exc)
        return load_series(day), False
    return curve, _write_series(day, r.text)


def sync_series(client: RateLimitedClient, *, mode: str = "tail",
                start: date = SERIES_START) -> Dict[str, Any]:
    """Keep the per-date cache current.  Never raises; returns a summary.

    ``tail`` re-fetches only the newest anchors -- the only ones the publisher
    still revises -- so a routine refresh costs three requests.  ``backfill``
    additionally fetches every anchor with no cache file, resumably.
    """
    out: Dict[str, Any] = {"mode": mode, "skipped": mode == "off",
                           "n_available": 0, "last_available": None,
                           "anchors": [], "dropped": [], "refreshed": [],
                           "changed": [], "fetched_utc": None}
    if mode not in ARCHIVE_MODES:
        raise ValueError(f"series mode must be one of {ARCHIVE_MODES}")
    if mode == "off":
        return out
    available = fetch_available_dates(client)
    if not available:
        logger.warning("bitview: no date listing -- series sync skipped")
        return out
    anchors, dropped = series_anchors(available, start)
    out.update(n_available=len(available), last_available=available[-1].isoformat(),
               anchors=anchors, dropped=dropped,
               fetched_utc=datetime.now(timezone.utc).isoformat())
    tail = anchors[-SERIES_TAIL_REFRESH:]
    tail_set = set(tail)
    if mode == "backfill":
        missing = [a for a in anchors if not _series_path(a).exists()]
        targets = missing + [a for a in tail if a not in set(missing)]
    else:
        targets = tail
    for day in targets:
        curve, changed = fetch_series(client, day, refresh=day in tail_set)
        if curve is not None:
            out["refreshed"].append(day.isoformat())
            if changed:
                out["changed"].append(day.isoformat())
    logger.info("bitview: %d anchors (%s..%s), %d/%d dropped, %d refreshed (%d changed)",
                len(anchors), anchors[0] if anchors else "-",
                anchors[-1] if anchors else "-", len(dropped), len(anchors) + len(dropped),
                len(out["refreshed"]), len(out["changed"]))
    return out


# ------------------------------------------------------- the archive store --

def archive_rows(days: List[date]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Dense long-form rows: one per $200 slot from $0 up to that date's own top.

    Dense, zeros included, because a client cumsums one date's curve against
    another's: a slot nobody occupies is a real answer (no coin has that cost
    basis) and shipping the grid explicitly is what lets the published site
    rebuild any pair without the server.  A date's grid stops at its own highest
    occupied bucket -- in 2016 that is $1,000, which is not a truncation but the
    truth about that day.
    """
    rows: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {"n_dates": 0, "n_values": 0, "n_omitted": 0,
                             "missing": [], "totals": {}}
    for day in days:
        p = _series_path(day)
        curve = load_series(day)
        if curve is None:
            stats["missing"].append(day.isoformat())
            continue
        # captured_utc is the payload's own mtime, not "now": the archive is
        # rebuilt whole on every run, and a run that changed nothing must produce
        # a byte-identical store -- which is also what makes the idempotency
        # check meaningful rather than tautological.
        try:
            captured = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            captured = datetime.now(timezone.utc).isoformat()
        top = int(max(curve) // SERIES_BUCKET_USD)
        grid = np.zeros(top + 1, dtype="float64")
        for price, value in curve.items():
            grid[int(round(price / SERIES_BUCKET_USD))] += value
        url = f"{series_url(day)}?bucket=lin200"
        for i in range(grid.size):
            rows.append({
                "as_of_date": day, "variant": "supply", "cohort": None,
                "detail": None, "units": "BTC",
                "price_bucket_usd": float(i) * SERIES_BUCKET_USD,
                "value": float(grid[i]),
                "bucket_usd": SERIES_BUCKET_USD,
                "source_kind": "api_series", "source_chart": url,
                "captured_utc": captured,
            })
        stats["n_dates"] += 1
        stats["n_values"] += int(grid.size)
        stats["n_omitted"] += int(grid.size - len(curve))
        stats["totals"][day.isoformat()] = float(grid.sum())
    return rows, stats


def archive_guards(df: pd.DataFrame, stats: Dict[str, Any]) -> Dict[str, Any]:
    """Hard-fail or log on the ways this store could be silently wrong.

    The store has a corruption history (a URPD store once shipped duplicate
    buckets under one date), so the duplicate key and the total-supply bounds are
    failures, not warnings.  The cross-check against the older curve store is a
    warning on mirror-era dates and a failure on live ones -- see
    ``cross_check_live`` for why those are not the same evidence.
    """
    key = ["as_of_date", "price_bucket_usd"]
    dupes = int(df.duplicated(subset=key).sum())
    if dupes:
        raise ValueError(f"{dupes} duplicate (as_of_date, price_bucket_usd) rows")
    totals = pd.Series(stats["totals"]).sort_index()
    if len(totals):
        low, high = float(totals.min()), float(totals.max())
        if not (15e6 <= low and high <= 21e6):
            bad = totals[(totals < 15e6) | (totals > 21e6)]
            raise ValueError("implausible supply on " +
                             ", ".join(f"{d} ({v:,.0f} BTC)" for d, v in bad.items()))
    # Supply is monotonically non-decreasing apart from burns and restatements;
    # a step down bigger than this is a parse or dating error, not the chain.
    steps = totals.diff().dropna()
    worst = steps[steps < -1e5]
    if len(worst):
        raise ValueError("supply drops by more than 100k BTC: " +
                         ", ".join(f"{d} ({v:,.0f})" for d, v in worst.items()))
    soft = steps[(steps < -1e4) & (steps >= -1e5)]
    for d, v in soft.items():
        logger.warning("bitview: archive total falls %.0f BTC into %s", -v, d)
    return {"totals_min": float(totals.min()) if len(totals) else None,
            "totals_max": float(totals.max()) if len(totals) else None,
            "monotone_breaks": int(len(soft))}


def cross_check_live(df: pd.DataFrame) -> Dict[str, Any]:
    """Compare the archive against the older curve store, LIVE dates only.

    The two checkonchain generations genuinely disagree bucket-by-bucket at an
    identical total: for 2021-12-31 the mirror-era snapshot in
    ``urpd_curves.parquet`` correlates 0.987 with the series API at a mean
    $200-bucket difference of 15.6k BTC, while the totals match to the cent.  The
    mirror-era charts are superseded output, so their disagreement says nothing
    about the series API -- but a date the LIVE charts also carry is real
    evidence, and there the agreement was exact (every settled date measured so
    far: 0.000 BTC over 74-126 $1,000 zones).

    The exception is the tail: the newest dates are still being restated, so the
    charts and the series API legitimately disagree about them until they settle
    (measured 2026-09-20: 42,908 BTC in one $1,000 zone at $80,000, where the
    spot price sits and coins cross the boundary intraday).  Dates inside
    ``SERIES_PROVISIONAL_DAYS`` of the archive's own newest date are therefore
    *measured and reported*, never failed -- and they are the reason the newest
    anchors are re-fetched on every run.
    """
    out: Dict[str, Any] = {"n_compared": 0, "compared": [], "legacy_skipped": [],
                           "provisional": [], "max_abs_delta": 0.0, "worst": None,
                           "max_rel": 0.0}
    if not CB_CURVES.exists():
        out["note"] = "curve store absent -- cross-check skipped"
        return out
    cur = pd.read_parquet(CB_CURVES, columns=["as_of_date", "variant", "cohort",
                                              "price_bucket_usd", "value",
                                              "source_chart"])
    cur = cur[(cur["variant"] == "supply") & cur["cohort"].isna()].copy()
    cur["_d"] = cur["as_of_date"].astype(str).str[:10]
    cur = cur[cur["_d"].isin(set(df["as_of_date"].astype(str)))]
    if cur.empty:
        out["note"] = "no date shared with the curve store"
        return out
    newest = max(date.fromisoformat(d) for d in set(df["as_of_date"].astype(str)))
    for day, grp in cur.groupby("_d"):
        legacy = grp["source_chart"].map(_is_legacy)
        if bool(legacy.all()):
            out["legacy_skipped"].append(day)
            continue
        live_rows = grp[~legacy]

        def zones(rows: pd.DataFrame) -> pd.Series:
            z = np.floor(rows["price_bucket_usd"].to_numpy(dtype="float64")
                         / CB_ZONE_USD) * CB_ZONE_USD
            return pd.Series(rows["value"].to_numpy(dtype="float64"),
                             index=z).groupby(level=0).sum()

        both = pd.concat({"a": zones(df[df["as_of_date"].astype(str) == day]),
                          "c": zones(live_rows)}, axis=1).fillna(0.0)
        delta = (both["a"] - both["c"]).abs()
        abs_max = float(delta.max())
        rel = float(abs_max / both["c"].sum()) if both["c"].sum() else 0.0
        stale = (newest - date.fromisoformat(day)).days <= SERIES_PROVISIONAL_DAYS
        if stale:
            out["provisional"].append({"date": day, "max_abs_delta": abs_max,
                                       "max_rel": rel, "worst_zone": float(
                                           delta.idxmax()) if delta.size else None})
            continue
        out["compared"].append(day)
        out["n_compared"] += 1
        if abs_max > out["max_abs_delta"]:
            out.update(max_abs_delta=abs_max, worst=day, max_rel=rel)
    if out["n_compared"] and out["max_rel"] > 5e-4:
        raise ValueError(
            f"archive disagrees with the LIVE curve store on {out['worst']} by "
            f"{out['max_abs_delta']:,.0f} BTC ({out['max_rel'] * 100:.3f}%)")
    return out


def stamp_archive_check(sync: Dict[str, Any]) -> None:
    """Record a re-check that changed nothing.

    Reached only when every anchor answered from cache with byte-identical
    payloads -- i.e. the run that proves the store is idempotent.  The parquet is
    not touched, and `store_sha256` is re-hashed from the file rather than copied
    out of the previous sidecar, so a sidecar that has drifted from its store
    shows up here as a warning instead of as agreement.
    """
    path = OUT_DIR / ARCHIVE_NAME
    mpath = path.with_name("urpd_archive_meta.json")
    meta = load_json_cache(mpath)
    if not meta:
        return
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if meta.get("sha256") and meta["sha256"] != sha:
        logger.warning("checkonchain: archive sidecar names %s but the store "
                       "hashes %s -- one of the two is stale",
                       meta["sha256"][:12], sha[:12])
    tail = sync.get("refreshed") or []
    meta["last_check"] = {
        "utc": datetime.now(timezone.utc).isoformat(),
        "mode": sync.get("mode"),
        "refreshed": tail,
        "n_dates": meta.get("n_dates"),
        "store_sha256": sha,
    }
    atomic_write_json(meta, mpath)
    logger.info("checkonchain: archive re-checked (%d newest anchors) -- store "
                "unchanged, sha %s", len(tail), sha[:12])


def write_archive(days: List[date], *, sync: Dict[str, Any],
                  prev_meta: Dict[str, Any]) -> Optional[Path]:
    """Rebuild the archive store from the cached payloads."""
    rows, stats = archive_rows(days)
    if not rows:
        logger.warning("checkonchain: no cached series curves -- archive not written")
        return None
    df = pd.DataFrame(rows)
    df["as_of_date"] = df["as_of_date"].astype("string")
    guards = archive_guards(df, stats)
    cross = cross_check_live(df)
    vintage = sorted(stats["totals"])
    out = OUT_DIR / ARCHIVE_NAME
    meta = {
        "dataset_id": "checkonchain_urpd_archive",
        "description": ("URPD curves on a uniform $200 grid for one date per week, "
                        "read from checkonchain's own per-date series API -- the "
                        "archive that lets any two dates be compared"),
        "data_source": DS.get("checkonchain_bitview", "checkonchain_bitview_series_api"),
        "confidence_tier": "Medium",
        "captured_from": f"{BITVIEW_BASE} (per-date URPD series, bucket=lin200)",
        "source_hosts": {"series_api": BITVIEW_BASE},
        "bucket_usd": SERIES_BUCKET_USD,
        "grid": "uniform $200 lattice, dense from $0 to each date's own top",
        "cadence_days": SERIES_CADENCE_DAYS,
        "anchor_start": days[0].isoformat() if days else None,
        "series_dates": vintage,
        "n_dates": stats["n_dates"],
        "n_values": stats["n_values"],
        "n_slots_zero_filled": stats["n_omitted"],
        "dates_without_payload": stats["missing"],
        "last_sync": {k: sync.get(k) for k in ("mode", "n_available",
                                               "last_available", "refreshed",
                                               "changed", "fetched_utc")},
        # No `last_check` in this dict, deliberately: that key records the last
        # run that re-checked the newest anchors and found nothing to write, so
        # its *absence* is what says this sidecar was written by the run that
        # produced the store it names.
        "known_limitations": [
            "THE RECENT TAIL IS PROVISIONAL: today's curve is restated through "
            "the day and the last ~month is still revised upstream. The newest "
            "three anchors are re-fetched on every run precisely because of this, "
            "and the store is rewritten whole when any of them moves.",
            "One date per week. A pair whose end falls between anchors is served "
            "from the nearest published date, and the payload names both the "
            "requested and the resolved date rather than quietly substituting.",
            "No data before 2016-01-03 in this archive (the host publishes back "
            "to 2009, the backfill simply starts here). Early windows read oddly: "
            "on 2016-01-03 the $0 bucket (coins with no known cost basis) holds "
            "5,037,889 BTC -- a THIRD of that date's 15.04M supply -- the whole "
            "curve is six occupied buckets deep, and the top one is $1,000.",
            "The payload is sparse and the grid is not: interior slots the host "
            "omits are zero-filled, 805 of them across the archive (worst date "
            "2024-11-24, 15 empty $200 slots below its $99,000 top). An absent "
            "slot means zero supply at that price, never unknown supply.",
            "A $200 grid is a genuine second generation of the same data: it "
            "matches the live charts bit-for-bit, but the mirror-era $200/$500 "
            "snapshots in urpd_curves.parquet for the same dates differ "
            "bucket-by-bucket at an identical total (2021-12-31: corr 0.987, "
            "mean $200-bucket difference 15.6k BTC). Both are checkonchain's own "
            "numbers from different pipeline generations; this store holds the "
            "current one.",
            "Third-party computed by checkonchain's node from UTXO data this "
            "project has never had -- a dated measurement, not a live tape.",
            "Undocumented host. The endpoint is open and CORS-enabled but carries "
            "no contract or versioning; payloads are validated on arrival, cached "
            "per date, and the published site ships a frozen copy.",
            "BTC-UTXO-only (no ETH equivalent).",
            "The $0 price bucket is excluded from overlay bars but reported "
            "separately. Its share is not a constant: 13.9% of supply on "
            "2026-09-20 (2,788,439 BTC), 17.4% in 2020, 33.5% in 2016 -- a 2016 "
            "pair is dominated by coins whose cost basis is simply unknown.",
        ],
        "cross_check_vs_curves": cross,
        "guards": guards,
        "prev_sha256": prev_meta.get("sha256"),
    }
    p = _write(df, ARCHIVE_NAME, meta)
    if p is None:
        return None
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    meta["sha256"] = sha
    same = bool(meta["prev_sha256"]) and meta["prev_sha256"] == sha
    meta["byte_identical_to_previous"] = same
    atomic_write_json(meta, p.with_name("urpd_archive_meta.json"))
    logger.info("checkonchain: archive %d dates, %d rows, %.2f MB%s",
                stats["n_dates"], stats["n_values"],
                p.stat().st_size / 1e6,
                " (byte-identical to the previous build)" if same else "")
    return p


# ------------------------------------------------------------------- run ---

def _write(df: pd.DataFrame, name: str, meta: Dict[str, Any]) -> Optional[Path]:
    """Write one store plus ITS OWN human-readable meta sidecar.

    ``common.write_parquet_with_meta`` keeps a single ``_meta.json`` per
    directory, which is fine for the one-store-per-directory builders but
    silently last-writer-wins here: this directory holds four stores, and the
    pre-existing ``urpd_snapshot.parquet`` (owned by phase4) would lose its
    metadata to ours.  So the guardrail labels still go into the parquet schema
    (surviving even if the sidecar is lost, same as the shared helper) and the
    readable meta is written per store instead.
    """
    if df.empty:
        logger.warning("checkonchain: %s is empty -- not written", name)
        return None
    out = OUT_DIR / name
    stem = out.stem
    meta = dict(meta)
    meta.setdefault("dataset_id", f"checkonchain_{stem}")
    meta.setdefault("generated_utc", datetime.now(timezone.utc).isoformat())
    meta.setdefault("row_count", int(len(df)))
    atomic_write_parquet(df, out, {"data_source": meta["data_source"],
                                   "confidence_tier": meta["confidence_tier"],
                                   "dataset_id": meta["dataset_id"]})
    atomic_write_json(meta, out.with_name(f"{stem}_meta.json"))
    logger.info("checkonchain: %s -> %s (%d rows)", name, out, len(df))
    return out


def _consensus(df: pd.DataFrame, key: List[str]) -> pd.DataFrame:
    """One row per ``key``: the value the most source charts agree on.

    The source republishes the same curve and the same series in several charts,
    and the copies occasionally differ -- a re-derived price, a snapshot page
    that no longer matches the diff charts.  Which copy sorts first is not
    evidence; how many charts carry it is.  Ties go to the smaller value
    (deterministic, and the conservative choice for supply figures).
    """
    support = (df.groupby(key + ["value"], dropna=False).size()
                 .rename("_support").reset_index())
    winners = (support.sort_values(["_support", "value"], ascending=[False, True])
                      .drop_duplicates(subset=key, keep="first")[key + ["value"]])
    return (df.merge(winners, on=key + ["value"], how="inner")
              .drop_duplicates(subset=key, keep="first")).reset_index(drop=True)


def run(force: bool = False, series_mode: str = "tail",
        series_start: date = SERIES_START) -> Dict[str, Any]:
    """Collect every chart into the three stores, and the dated-curve archive.

    Returns a summary dict.  Gated per chart on the source's own validator: the
    current charts are live again, so a refresh costs one conditional GET each,
    while the historical charts -- frozen upstream -- never move at all.  The
    archive has its own, weaker gate: the newest anchors are always re-fetched
    (they are the ones still being revised), everything older is never touched.
    """
    t0 = time.time()
    result: Dict[str, Any] = {"ok": False, "outputs": [], "notes": [],
                              "tree_sha": None, "skipped": False, "elapsed": 0.0,
                              "series": None}
    client = RateLimitedClient(min_interval=0.5)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    paths, tree_sha = list_charts(client)
    result["tree_sha"] = tree_sha
    logger.info("checkonchain: %d charts to consider (mirror tree %s, live host first)",
                len(paths), tree_sha[:12])

    # Pass 1 -- refresh the raw cache and note what actually moved.  Nothing is
    # decoded here: a routine refresh whose charts are all unchanged must cost a
    # few conditional GETs, not a re-parse of every payload we hold.
    contents: Dict[str, str] = {}
    changed: List[str] = []
    retired: List[str] = []
    for path in paths:
        kind, _variant, window = classify(path)
        if kind == "diff" and window in RETIRED_DIFF_WINDOWS:
            retired.append(path)
            continue
        html, moved = _fetch(client, path, use_cache=not force)
        if html:
            contents[path] = html
            if moved:
                changed.append(path)
    if retired:
        logger.info("checkonchain: %d chart(s) skipped -- window retired upstream (%s)",
                    len(retired),
                    ", ".join(f"{w}d" for w in sorted(RETIRED_DIFF_WINDOWS)))
    if changed:
        logger.info("checkonchain: %d chart(s) rewritten", len(changed))

    # The dated-curve archive comes from a different host with a different gate,
    # so it is synced here -- before the early-exit below, which would otherwise
    # skip a run whose only news is a moved series curve.
    archive_path = OUT_DIR / ARCHIVE_NAME
    # named series_sync, not series: `series` is already the age-cohort frame below
    series_sync: Dict[str, Any] = {"mode": "off", "anchors": [], "changed": [],
                                   "refreshed": []}
    if series_mode != "off":
        sclient = RateLimitedClient(min_interval=1.0, user_agent=BITVIEW_UA)
        series_sync = sync_series(sclient, mode=series_mode, start=series_start)
    result["series"] = series_sync

    # This directory also holds phase4's urpd_snapshot.parquet and its bare
    # _meta.json, so the gate reads one of OUR per-store sidecars by name
    # rather than whatever the directory's shared slot happens to be holding.
    prev = load_json_cache(OUT_DIR / "urpd_curves_meta.json") or {}
    if (not force and not changed and not series_sync["changed"]
            and prev.get("data_vintage") and archive_path.exists()):
        logger.info("checkonchain: none of %d charts moved -- nothing to do",
                    len(contents))
        result.update(ok=True, skipped=True, elapsed=time.time() - t0,
                      notes=[f"{len(contents)} charts checked, none moved; skipped",
                             f"archive: {len(series_sync['refreshed'])} newest anchors "
                             f"re-checked, none moved"])
        if series_sync["mode"] != "off":
            # Nothing was decoded or written, so `last_sync` still describes the
            # run that last rewrote the store; this marks the run that confirmed
            # it did not need rewriting.  With the store untouched, the sha in
            # `last_check` must equal the sidecar's `sha256` and the file on disk.
            stamp_archive_check(series_sync)
        return result

    curve_rows: List[Dict[str, Any]] = []
    diff_rows: List[Dict[str, Any]] = []
    series_rows: List[Dict[str, Any]] = []
    diffs_ok: List[Dict[str, Any]] = []
    diffs_bad: List[Dict[str, Any]] = []
    n_series = 0

    # Pass 2 -- decode everything we hold, not only what moved: the stores are
    # rebuilt whole, and the historical charts exist nowhere but the raw cache.
    for path, html in contents.items():
        kind, variant, window = classify(path)
        try:
            traces = parse_traces(html)
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkonchain: %s unparseable (%s)", path, exc)
            continue
        if kind == "diff":
            info = build_diff(diff_rows, path, variant, window, traces,
                              curve_rows=curve_rows)
            (diffs_ok if info["ok"] else diffs_bad).append(info)
        elif path in SERIES_CHARTS:
            n_series += build_series(series_rows, path, traces)
        else:
            build_curves(curve_rows, path, kind, variant, html, traces)

    curves = pd.DataFrame(curve_rows)
    # One curve is reproduced by every diff chart that spans it, so dedup on the
    # curve's identity -- by consensus, not by order: on 2026-09-20 the six live
    # diff charts agree on 20,086,523 BTC while the live snapshot page offers
    # 17,794,243 for the same day, and whichever path sorted first used to win.
    if not curves.empty:
        key = ["as_of_date", "variant", "units", "cohort", "detail",
               "price_bucket_usd"]
        spread = curves.groupby(key, dropna=False)["value"].nunique()
        conflicts = int((spread > 1).sum())
        if conflicts:
            detail = spread[spread > 1].reset_index()
            for _, r in detail.head(4).iterrows():
                logger.warning("checkonchain: %s %s $%s differs across charts",
                               r["as_of_date"], r["variant"], r["price_bucket_usd"])
            logger.warning("checkonchain: %d buckets disagree across charts on the "
                           "same dated curve -- keeping the most-supported value",
                           conflicts)
        curves = _consensus(curves, key)
        curves["as_of_date"] = curves["as_of_date"].astype("string")
    diffs = pd.DataFrame(diff_rows)
    if not diffs.empty:
        diffs["as_of_date"] = diffs["as_of_date"].astype("string")
        diffs["prev_as_of_date"] = diffs["prev_as_of_date"].astype("string")
    series = pd.DataFrame(series_rows)
    # Every series chart carries its own `Price` trace, so the same price series
    # arrives five times over -- and the five copies are not identical: on
    # 2026-09-20 four charts say 80,342.21 and one says 80,472.00.  Keep one copy
    # per (metric, date), the one the most charts agree on.
    if not series.empty:
        spread = series.groupby(["metric", "date"])["value"].nunique()
        conflicts = int((spread > 1).sum())
        if conflicts:
            detail = spread[spread > 1].reset_index()
            for _, r in detail.tail(4).iterrows():
                logger.warning("checkonchain: %s %s disagrees across charts",
                               r["metric"], r["date"])
            logger.warning("checkonchain: %d series points disagree across charts "
                           "-- keeping the most-supported value", conflicts)
        series = _consensus(series, ["metric", "date"]) \
            .sort_values(["metric", "date"]).reset_index(drop=True)

    vintage = sorted({d for d in (curves["as_of_date"].dropna().unique().tolist()
                                  if not curves.empty else []) if d})
    limitations = [
        "TWO SOURCES, ONE STORE: current charts come from the live host "
        "(charts-cdn.checkonchain.com), the dated history from the frozen GitHub "
        "mirror (last data commit 2026-02-19) and our raw cache. `tree_sha` is the "
        "mirror revision the history came from -- it is not a freshness signal.",
        "The 14-day diff window is retired upstream: the live host 404s it. It is "
        "skipped outright rather than summed from mixed vintages; live windows are "
        "30/90/180/365d.",
        "Live charts bucket at $500, the mirror generation at $200. Each dated curve "
        "keeps the grid it arrived on and the two are never merged into one curve.",
        "Where the source's own charts disagree, the value carried by the most "
        "charts wins: on 2026-09-20 the live snapshot page's curve is 17.79M BTC "
        "against the six live diff charts' 20.09M for the same day, and the diff "
        "family is the one its own history corroborates.",
        "`price_usd` rides along in every series chart and those copies differ "
        "(median 0.2% in 2022-2026, up to 30% before 2016); the most-supported "
        "copy is kept rather than whichever chart listed first.",
        "BTC-UTXO-only (no ETH equivalent) -- URPD is a UTXO-chain concept.",
        "Third-party computed: derived by checkonchain's own node, not this project's.",
        "Public free chart pages (Plotly payloads in static HTML); robots-permitted, "
        "commercial brand content -- treat as investigation artifact.",
        "Historical dated curves are source-computed for fixed windows; the curve "
        "store is what makes arbitrary date pairs possible.",
        "The $0 price bucket holds ~2.8M BTC and is excluded from overlay series.",
    ]

    outputs = []
    n_live = sum(1 for p in contents if not _is_legacy(p))
    logger.info("checkonchain: %d charts live, %d frozen, %d retired",
                n_live, len(contents) - n_live, len(retired))
    for frame, name, desc, extra in (
        (curves, "urpd_curves.parquet",
         "URPD curves: coin supply bucketed by the price at which each UTXO last "
         "moved (its cost basis)", {"curve_dates": vintage, "variants": sorted(curves.variant.unique().tolist()) if not curves.empty else []}),
        (diffs, "urpd_diffs.parquet",
         "URPD diffs: per-price-bucket change in cost-basis supply between two "
         "stamped dates -- who is selling at what cost basis",
         {"windows": sorted({int(w) for w in diffs.window_days.dropna().unique()}) if not diffs.empty else [],
          "charts_ok": len(diffs_ok), "charts_dropped": len(diffs_bad)}),
        (series, "checkonchain_series.parquet",
         "Age-cohort series (LTH/STH supply in profit & loss, SOPR, realised "
         "P&L by band, STH-MVRV) in long form",
         {"metrics": sorted(series.metric.unique().tolist()) if not series.empty else []}),
    ):
        p = _write(frame, name, {
            "dataset_id": f"checkonchain_{name.split('.')[0]}",
            "description": desc,
            "data_source": DS.get("checkonchain_github", "checkonchain_public_github_pages"),
            "confidence_tier": "Medium",
            "captured_from": f"{CDN_BASE} (current charts) + "
                             f"https://github.com/{GH_REPO} (frozen history)",
            "source_hosts": {"live": CDN_BASE, "frozen_mirror": GH_RAW},
            "charts_live": n_live,
            "charts_frozen": len(contents) - n_live,
            "windows_retired": sorted(RETIRED_DIFF_WINDOWS),
            "tree_sha": tree_sha,
            "data_vintage": vintage[-1] if vintage else None,
            "known_limitations": limitations,
            **extra,
        })
        if p:
            outputs.append(str(p))

    _write_series_summary(series)

    # The dated-curve archive.  Written whenever the anchors are known, which is
    # whenever the series sync ran -- it is rebuilt whole from the per-date cache,
    # so a chart-only run (--series-mode off) leaves it exactly as it was.
    archive_out = None
    if series_sync["anchors"]:
        prev_archive = load_json_cache(OUT_DIR / "urpd_archive_meta.json") or {}
        archive_out = write_archive(series_sync["anchors"], sync=series_sync,
                                    prev_meta=prev_archive)
        if archive_out:
            outputs.append(str(archive_out))
    elif not series_sync["skipped"]:
        logger.warning("checkonchain: no series anchors -- archive untouched")

    if diffs_bad:
        for info in diffs_bad:
            logger.warning("checkonchain: dropped %s -- %s", info["chart"], info["reason"])
    result.update(
        ok=bool(outputs), outputs=outputs, elapsed=time.time() - t0,
        notes=[
            f"{len(contents)} charts held ({n_live} live, {len(contents) - n_live} "
            f"frozen, {len(retired)} retired), mirror tree {tree_sha[:12]}",
            f"{len(curves):,} curve rows across {len(vintage)} distinct dates",
            f"{len(diffs):,} diff rows ({len(diffs_ok)} ok, {len(diffs_bad)} dropped)",
            f"{len(series):,} series rows from {n_series} points",
            f"archive: {len(series_sync['anchors'])} anchors, "
            f"{len(series_sync['refreshed'])} re-checked, "
            f"{len(series_sync['changed'])} moved"
            + (f" -> {archive_out.name}" if archive_out else " (not written)"),
        ])
    return result


def _write_series_summary(series: pd.DataFrame) -> None:
    """Cheap human-readable index of what the series store actually holds."""
    if series.empty:
        return
    summary = []
    for metric, grp in series.groupby("metric"):
        latest = grp.sort_values("date").iloc[-1]
        summary.append(f"- `{metric}` ({latest['units']}): {len(grp):,} rows, "
                       f"{grp['date'].min()} -> {grp['date'].max()}, "
                       f"latest {latest['value']:,.4g}")
    (OUT_DIR / "checkonchain_series.md").write_text(
        "# checkonchain age-cohort series\n\n"
        f"Current charts live from {CDN_BASE}; the history is the frozen GitHub "
        f"mirror (last data commit 2026-02-19), which is why a metric's end date "
        f"reflects when that chart was last published upstream. Generated "
        f"{datetime.now(timezone.utc).isoformat()}.\n\n" + "\n".join(summary) + "\n",
        encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="re-download every chart and ignore the tree-sha gate")
    ap.add_argument("--backfill-series", action="store_true",
                    help="fetch every dated curve the weekly archive is missing "
                         "(~560 requests, one-time, resumable -- re-run after a "
                         "throttled run to pick up where it stopped)")
    ap.add_argument("--series-start", default=SERIES_START.isoformat(),
                    metavar="YYYY-MM-DD",
                    help=f"first date of the weekly archive grid "
                         f"(default {SERIES_START.isoformat()})")
    ap.add_argument("--series-mode", choices=ARCHIVE_MODES, default=None,
                    help="tail (default): re-check the newest anchors only; "
                         "backfill: also fetch every missing anchor; "
                         "off: leave the archive alone")
    args = ap.parse_args()
    mode = args.series_mode or ("backfill" if args.backfill_series else "tail")
    res = run(force=args.force, series_mode=mode,
              series_start=date.fromisoformat(args.series_start))
    for line in res["notes"]:
        logger.info("checkonchain: %s", line)
    for p in res["outputs"]:
        print(f"wrote {p}")
    print(f"checkonchain URPD collect: {'OK' if res['ok'] else 'FAILED'} "
          f"in {res['elapsed']:.1f}s")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
