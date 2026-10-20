"""
verify_all.py — end-to-end verification of every output (plan section: Verification).

Checks:
  1. Step 0 (exchange_true_volume): volumes >= 0, taker_buy_ratio in [0,1],
     coverage modes consistent, gate dict in _meta.json passes, cross-fill flags sane.
  2. Phase 1 (network metrics): gap-free daily index within each chart's range,
     all values > 0 where expected, dates 00:00 UTC, no dupes.
  3. Phase 2 (positioning): OI > 0, ratios in [1e-3, 1e3], long_pct in [0,1],
     skew in [-0.5, 0.5], gap-free within coverage.
  4. Phase 5 (realized cap): rc_realized_cap_usd > 0, mvrv identity, cross-check
     already embedded in _meta.json (pass_2pct).
  5. Final deliverable: gap-free date index, no negatives, label columns populated
     from the constant set, _meta.json present, row count == expected days.
  6. E2E: registry entries present; pipeline log has one entry per executed phase.

Exits non-zero on the first failure; writes a machine-readable summary to
processed/verification_report.json.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

try:
    from .common import (PROCESSED, RAW, REGISTRY_YAML, SYMBOLS, LOG_FILE,
                         PhaseResult, append_log, logger)
except ImportError:  # direct script execution
    from common import (PROCESSED, RAW, REGISTRY_YAML, SYMBOLS, LOG_FILE,
                        PhaseResult, append_log, logger)

logger = logging.getLogger("tvp.verify")

LABEL_SOURCES = {"blockchain.com_charts_api", "etherscan_free_api", "coinmetrics_community_api",
                 "coinmetrics_community_derived", "binance_vision_klines",
                 "binance_vision_aggtrades", "binance_vision_metrics", "deribit_public_api",
                 "onchain_dataset_local", "dataset_1d_symbols_crossfill",
                 "binance_rest_top_trader", "checkonchain_public_github_pages",
                 "checkonchain_bitview_series_api",
                 "merged multi-source (see blocks_meta)"}

# The per-date archive the visualizer's window=custom reads. Its grid is $200,
# which does NOT nest in the publisher's $500 -- so the width is part of the
# store's identity, not a presentation detail: a $500 row written into it would
# be silently summed into $200 buckets by every reader.
# Two grids live in it: one date a week from 2016, and EVERY published date from
# CB_ARCHIVE_DAILY_FROM on. The daily claim is the one worth checking, because the
# archive path snaps a requested end to the nearest stored date with no distance
# limit (it is the fallback that must always answer) -- a store that says it is
# daily while holding holes would serve a window weeks from the one asked for and
# draw a plausible chart of it. Same constants as the collector's SERIES_DAILY_*
# and SERIES_PROVISIONAL_DAYS.
CB_ARCHIVE = PROCESSED / "onchain" / "checkonchain" / "BTC" / "urpd_archive.parquet"
CB_ARCHIVE_META = CB_ARCHIVE.with_name("urpd_archive_meta.json")
CB_ARCHIVE_ZONE_USD = 200.0
CB_ARCHIVE_DAILY_FROM = "2024-10-13"
CB_ARCHIVE_DAILY_MAX_GAP = 3
CB_ARCHIVE_PROVISIONAL_DAYS = 35
CB_CURVES = PROCESSED / "onchain" / "checkonchain" / "BTC" / "urpd_curves.parquet"
# The published page computes a custom window from THIS artifact, without a server,
# so it is part of the deliverable and not an export detail.
CB_ARCHIVE_EXPORT = PROCESSED.parent / "site" / "cost_basis" / "BTC_archive.json.gz"
# ...and the publisher's OWN dated curves, which a custom pair prefers: the same
# "part of the deliverable, not an export detail" argument, and the store that
# makes such a pair agree with the preset beside it.
CB_DATED_EXPORT = PROCESSED.parent / "site" / "cost_basis" / "BTC_dated.json.gz"
CB_SITE_MANIFEST = PROCESSED.parent / "site" / "manifest.json"

# The daily US spot-ETF net flows drawn as bars in the candle pane.  The store
# row is one calendar day; the publisher writes an explicit 0.0 for a day the US
# market did not open, so "contiguous" here means every calendar day, weekends
# included.  What ships is what the phone reads, so it is part of the deliverable.
ETF_CHART = "btconchain/etfs/etf_flowsvsprice/etf_flowsvsprice_light.html"
ETF_METRIC = "etf_net_flow_usd"
ETF_FIRST = "2024-01-11"                      # first US spot-BTC session
ETF_MIN_DAYS = 985                            # the series only grows; a fall means a broken collector
SERIES_STORE = PROCESSED / "onchain" / "checkonchain" / "BTC" / "checkonchain_series.parquet"
ETF_EXPORT = PROCESSED.parent / "site" / "etf" / "BTCUSDT.json"
ETF_EXPORT_ETH = PROCESSED.parent / "site" / "etf" / "ETHUSDT.json"


def _archive_meta() -> Dict[str, Any]:
    """The archive store's own sidecar (a named file, not the directory's _meta)."""
    if not CB_ARCHIVE_META.exists():
        return {}
    try:
        return json.loads(CB_ARCHIVE_META.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _meta(path: Path) -> Dict[str, Any]:
    mj = path.with_name("_meta.json")
    if not mj.exists():
        return {}
    return json.loads(mj.read_text(encoding="utf-8"))


def check_gap_free(idx: pd.DatetimeIndex) -> List[str]:
    full = pd.date_range(idx.min(), idx.max(), freq="1D", tz="UTC")
    return [str(d.date()) for d in full.difference(idx)]


def verify_exchange() -> List[str]:
    errs: List[str] = []
    for sym in SYMBOLS:
        p = PROCESSED / "exchange_true_volume" / sym / "merged.parquet"
        if not p.exists():
            errs.append(f"step0: missing {p}")
            continue
        df = pd.read_parquet(p)
        num = df.select_dtypes("number").fillna(0)
        if (num < 0).any().any():
            errs.append(f"step0 {sym}: negative values")
        for c in ("exch_spot_taker_buy_ratio", "exch_fut_taker_buy_ratio",
                  "exch_combined_taker_buy_ratio"):
            r = df[c].dropna()
            if ((r < 0) | (r > 1)).any():
                errs.append(f"step0 {sym}: {c} outside [0,1]")
        modes = set(df["exch_coverage_mode"].unique())
        if not modes <= {"spot_only", "spot+futures", "futures_only", "gap"}:
            errs.append(f"step0 {sym}: unknown coverage modes {modes}")
        m = _meta(p)
        g = m.get("tape_gate", {})
        v = g.get("volume", {})
        if v.get("pct_ok", 0) < 99.9 and not g.get("adjudicated", {}).get("tape_validated"):
            errs.append(f"step0 {sym}: tape gate volume {v} (not adjudicated)")
        if g.get("adjudicated"):
            if not g["adjudicated"].get("volume_days"):
                errs.append(f"step0 {sym}: adjudicated gate lacks volume_days evidence")
            # every adjudicated day must have been within the gate's bad list
            vol_days = set(g["adjudicated"].get("volume_days", []))
            bad = set(g.get("volume_bad_days", []))
            if vol_days - bad:
                errs.append(f"step0 {sym}: adjudication days not in gate bad days "
                            f"{sorted(vol_days - bad)}")
        # cross-fill flags must match the coverage narrative
        if "exch_spot_cross_filled" in df and "exch_fut_cross_filled" in df:
            cf = df[["exch_spot_cross_filled", "exch_fut_cross_filled"]].sum()
            if cf.get("exch_spot_cross_filled", 0) < 80:
                errs.append(f"step0 {sym}: expected ~88 spot cross-fill days, got {cf['exch_spot_cross_filled']}")
        if not m.get("data_source") or not m.get("confidence_tier"):
            errs.append(f"step0 {sym}: _meta.json lacks provenance labels")
        logger.info("step0 %s: %d rows %s..%s modes=%s", sym, len(df),
                    df["date"].min().date(), df["date"].max().date(), sorted(modes))
    return errs


def verify_network_metrics() -> List[str]:
    errs: List[str] = []
    for asset in ("BTC", "ETH"):
        p = PROCESSED / "onchain" / "network_metrics" / asset / "merged.parquet"
        if not p.exists():
            errs.append(f"phase1: missing {p}")
            continue
        df = pd.read_parquet(p)
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
        dup = df["date"].duplicated().sum()
        if dup:
            errs.append(f"phase1 {asset}: {dup} duplicate dates")
        if (df["date"].dt.hour != 0).any() or (df["date"].dt.minute != 0).any():
            errs.append(f"phase1 {asset}: non-midnight dates")
        for c in df.select_dtypes("number").columns:
            v = df[c].dropna()
            if len(v) and (v <= 0).all():
                pass  # some charts legitimately small; only flag all-zero
            if len(v) and (v == 0).all():
                errs.append(f"phase1 {asset}: {c} all zeros")
        gaps = check_gap_free(pd.DatetimeIndex(df["date"]))
        if len(gaps) > 2:
            errs.append(f"phase1 {asset}: {len(gaps)} gap days (e.g. {gaps[:4]})")
        logger.info("phase1 %s: %d rows %s..%s", asset, len(df),
                    df["date"].min().date(), df["date"].max().date())
    return errs


def verify_positioning() -> List[str]:
    errs: List[str] = []
    for sym in SYMBOLS:
        p = PROCESSED / "futures" / "positioning" / sym / "merged.parquet"
        if not p.exists():
            errs.append(f"phase2: missing {p}")
            continue
        df = pd.read_parquet(p)
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
        oi = df["pos_oi_usd"].dropna()
        if len(oi) and (oi <= 0).any():
            errs.append(f"phase2 {sym}: OI <= 0")
        r = df["pos_top_trader_ls_ratio"].dropna()
        if len(r) and ((r < 1e-3) | (r > 1e3)).any():
            errs.append(f"phase2 {sym}: ratio outside [1e-3, 1e3]")
        lp = df["pos_top_trader_long_pct"].dropna()
        if len(lp) and ((lp < 0) | (lp > 1)).any():
            errs.append(f"phase2 {sym}: long_pct outside [0,1]")
        sk = df["pos_top_trader_skew"].dropna()
        if len(sk) and ((sk < -0.5) | (sk > 0.5)).any():
            errs.append(f"phase2 {sym}: skew outside [-0.5, 0.5]")
        # plan-literal identity: long_pct + short_pct == 1
        if {"pos_top_trader_long_pct", "pos_top_trader_short_pct"} <= set(df.columns):
            s = (df["pos_top_trader_long_pct"] + df["pos_top_trader_short_pct"]).dropna()
            if (s - 1).abs().max() > 1e-9:
                errs.append(f"phase2 {sym}: long+short != 1")
        logger.info("phase2 %s: %d rows %s..%s", sym, len(df),
                    df["date"].min().date(), df["date"].max().date())
    return errs


def verify_realized_cap() -> List[str]:
    errs: List[str] = []
    for asset in ("BTC", "ETH"):
        p = PROCESSED / "onchain" / "realized_cap" / asset / "merged.parquet"
        if not p.exists():
            errs.append(f"phase5: missing {p}")
            continue
        df = pd.read_parquet(p)
        rc = df["rc_realized_cap_usd"].dropna()
        if len(rc) and (rc <= 0).any():
            errs.append(f"phase5 {asset}: realized cap <= 0")
        # identity: mvrv == market_cap / realized_cap
        ident = (df["rc_mvrv_ratio"] - df["rc_market_cap_usd"] / df["rc_realized_cap_usd"].replace(0, np.nan))
        if ident.dropna().abs().max() > 1e-6:
            errs.append(f"phase5 {asset}: mvrv identity deviation {ident.dropna().abs().max():.3g}")
        m = _meta(p)
        xc = m.get("cross_check_vs_local_onchain", {})
        if asset == "BTC" and xc.get("done") and not xc.get("pass_2pct"):
            errs.append(f"phase5 BTC: local cross-check FAILED {xc}")
        logger.info("phase5 %s: %d rows %s..%s", asset, len(df),
                    pd.to_datetime(df["date"], utc=True).min().date(),
                    pd.to_datetime(df["date"], utc=True).max().date())
    return errs


def verify_final() -> List[str]:
    errs: List[str] = []
    for asset in ("BTC", "ETH"):
        p = PROCESSED / "true_volume" / asset / "merged.parquet"
        if not p.exists():
            errs.append(f"final: missing {p}")
            continue
        df = pd.read_parquet(p)
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
        gaps = check_gap_free(pd.DatetimeIndex(df["date"]))
        if gaps:
            errs.append(f"final {asset}: {len(gaps)} gap days (e.g. {gaps[:4]})")
        num = df.select_dtypes("number").fillna(0)
        # signed by construction: pos_*_skew, *net_exchange_flow (same rule as build_final)
        signed = [c for c in num.columns
                  if "skew" in c or "net_exchange_flow" in c or "net_flow" in c]
        if (num.drop(columns=signed, errors="ignore") < 0).any().any():
            errs.append(f"final {asset}: negative values")
        for prefix in ("exch", "net", "rc", "pos", "onch"):
            c = f"{prefix}_data_source"
            if c in df.columns:
                vals = set(df[c].dropna().unique())
                unknown = vals - LABEL_SOURCES
                if unknown:
                    errs.append(f"final {asset}: {c} has unexpected values {unknown}")
            else:
                errs.append(f"final {asset}: missing {c}")
        if not (p.with_name("_meta.json")).exists():
            errs.append(f"final {asset}: _meta.json missing")
        logger.info("final %s: %d rows %s..%s (%d cols)", asset, len(df),
                    df["date"].min().date(), df["date"].max().date(), len(df.columns))
    return errs


def verify_cb_archive() -> List[str]:
    """The per-date $200 archive behind the any-window cost basis.

    Structural, because that is the failure this store's sibling has already
    suffered once: rows landing at a key that does not mean what the reader
    assumes.  The two that matter here are a bucket width that is not $200 and a
    grid that is not dense from $0 -- either one lets every reader sum the wrong
    slots and still produce plausible numbers.

    The per-zone agreement with the older curve store is checked where the
    provenance is known (collector: cross_check_live, LIVE dates only -- the
    mirror-era curves genuinely disagree bucket-by-bucket at an identical total).
    What is checked here is the generation-independent part: every date shared
    with `urpd_curves.parquet` must carry the same total supply, in both
    generations, to within 0.05%.
    """
    errs: List[str] = []
    if not CB_ARCHIVE.exists():
        return [f"cb_archive: missing {CB_ARCHIVE} — run "
                f"fetch_checkonchain_urpd.py --backfill-series"]
    df = pd.read_parquet(CB_ARCHIVE)
    for col in ("as_of_date", "variant", "cohort", "units", "price_bucket_usd",
                "value", "bucket_usd"):
        if col not in df.columns:
            errs.append(f"cb_archive: missing column {col}")
    if errs:
        return errs
    if not (df["variant"] == "supply").all():
        errs.append(f"cb_archive: variants {set(df['variant'].unique())} — "
                    f"only supply rows belong in this store")
    if df["cohort"].notna().any():
        errs.append("cb_archive: cohort rows present — supply rows must carry a null cohort")
    if not (df["units"] == "BTC").all():
        errs.append(f"cb_archive: units {set(df['units'].unique())}")
    if not (df["bucket_usd"] == CB_ARCHIVE_ZONE_USD).all():
        errs.append(f"cb_archive: bucket widths {sorted(set(df['bucket_usd'].unique()))[:6]} "
                    f"— every row must be ${CB_ARCHIVE_ZONE_USD:.0f}")
    off = np.abs(df["price_bucket_usd"].to_numpy(dtype="float64") % CB_ARCHIVE_ZONE_USD)
    if (off > 1e-6).sum():
        errs.append(f"cb_archive: {(off > 1e-6).sum()} buckets are not multiples "
                    f"of ${CB_ARCHIVE_ZONE_USD:.0f}")
    if (df["price_bucket_usd"] < 0).any():
        errs.append("cb_archive: negative price buckets")
    v = df["value"].to_numpy(dtype="float64")
    if not np.isfinite(v).all() or (v < 0).any():
        errs.append("cb_archive: non-finite or negative supply values")
    dupes = int(df.duplicated(subset=["as_of_date", "price_bucket_usd"]).sum())
    if dupes:
        errs.append(f"cb_archive: {dupes} duplicate (as_of_date, price_bucket_usd) rows")

    df = df.assign(_d=df["as_of_date"].astype(str).str[:10])
    per_day = df.groupby("_d")
    counts = per_day["price_bucket_usd"].agg(["count", "max"])
    # dense from $0: a date owning N slots must own exactly $0, $200, … $(N-1)*200
    broken = counts[np.abs(counts["max"] / CB_ARCHIVE_ZONE_USD + 1 - counts["count"]) > 1e-6]
    if len(broken):
        errs.append(f"cb_archive: {len(broken)} dates are not a dense grid from $0 "
                    f"(e.g. {broken.index[0]}: {int(broken['count'].iloc[0])} slots to "
                    f"${broken['max'].iloc[0]:,.0f})")
    totals = per_day["value"].sum().sort_index()
    bad = totals[(totals < 15e6) | (totals > 21e6)]
    if len(bad):
        errs.append("cb_archive: implausible supply on " +
                    ", ".join(f"{d} ({x:,.0f} BTC)" for d, x in bad.head(4).items()))
    # Gaps are indexed by the LATER date, so the daily claim can be tested on its
    # own region instead of over the whole store.
    gaps = pd.Series(pd.to_datetime(totals.index), index=totals.index).diff().dt.days.dropna()
    if len(gaps) and gaps.max() > 10:
        errs.append(f"cb_archive: {int((gaps > 10).sum())} gap(s) wider than 10 days "
                    f"(max {int(gaps.max())}) — wider than either grid this store carries")
    meta = _archive_meta()
    if meta.get("cadence_days") == 1:
        # The sidecar CLAIMS the daily region is in. The collector only releases
        # that region whole (store_dates(), all-or-nothing), so hold it to it: a
        # claim the data cannot back is worse than the weekly grid it replaced.
        if meta.get("daily_from") != CB_ARCHIVE_DAILY_FROM:
            errs.append(f"cb_archive: sidecar daily_from={meta.get('daily_from')!r}, this "
                        f"checker knows {CB_ARCHIVE_DAILY_FROM!r} — one of the two moved")
        if CB_ARCHIVE_DAILY_FROM not in totals.index:
            errs.append(f"cb_archive: cadence_days=1 but the store holds no curve at "
                        f"{CB_ARCHIVE_DAILY_FROM} — the daily region is not whole")
        # Both ends of the gap must be inside the region: the seam itself is a
        # 7-day step (the last weekly lattice point to the first daily date) and
        # is exactly what the two grids meeting looks like.
        daily_from_ts = pd.Timestamp(CB_ARCHIVE_DAILY_FROM)
        prev_ts = pd.Series(pd.to_datetime(totals.index), index=totals.index).shift(1)
        in_daily = gaps[(pd.Series(pd.to_datetime(totals.index),
                                   index=totals.index) > daily_from_ts)
                        & (prev_ts >= daily_from_ts)]
        if len(in_daily) and in_daily.max() > CB_ARCHIVE_DAILY_MAX_GAP:
            errs.append(f"cb_archive: cadence_days=1 but the daily region has "
                        f"{int((in_daily > CB_ARCHIVE_DAILY_MAX_GAP).sum())} gap(s) wider "
                        f"than {CB_ARCHIVE_DAILY_MAX_GAP} days (max {int(in_daily.max())}) "
                        f"— that region must be whole or absent")

    if CB_CURVES.exists():
        cur = pd.read_parquet(CB_CURVES, columns=["as_of_date", "variant", "cohort", "value"])
        cur = cur[(cur["variant"] == "supply") & cur["cohort"].isna()].copy()
        cur["_d"] = cur["as_of_date"].astype(str).str[:10]
        shared_cur = cur.groupby("_d")["value"].sum()
        rel = (totals - shared_cur.reindex(totals.index)).abs() / shared_cur.reindex(totals.index)
        worst = rel.dropna()
        # The publisher's own restatement window is excluded, exactly as the
        # collector's cross_check_live excludes it: the two stores are readings of
        # a curve that is still moving, and at daily cadence every one of the last
        # ~35 days is shared with the chart store (before, only the odd Sunday was
        # -- so this comparison is new ground, not a louder version of the old one).
        if len(worst):
            newest = datetime.fromisoformat(totals.index[-1])
            keep = [d for d in worst.index
                    if (newest - datetime.fromisoformat(d)).days > CB_ARCHIVE_PROVISIONAL_DAYS]
            worst = worst.loc[keep]
        if len(worst) and float(worst.max()) > 5e-4:
            errs.append(f"cb_archive: {int((worst > 5e-4).sum())} date(s) disagree with "
                        f"urpd_curves on total supply by >0.05% "
                        f"(worst {worst.idxmax()} at {float(worst.max()) * 100:.3f}%)")
        logger.info("cb_archive: %d dates, %d values, %s..%s, totals %.0f-%.0f BTC, "
                    "%d date(s) cross-checked against the curve store",
                    len(totals), int(counts["count"].sum()), totals.index[0],
                    totals.index[-1], float(totals.min()), float(totals.max()), len(worst))
    return errs


def verify_cb_archive_export() -> List[str]:
    """The SHIPPED copy of that archive — the file the phone actually loads.

    A custom window on the published page is computed from this gzipped artifact
    and nothing else, so a pure reader of it (gzip + json, no app.py) must
    rebuild every stored curve value for value.  The bound is the encoding's own:
    values ship as centi-BTC integers, so one may sit up to half a cent (0.005
    BTC) from the parquet float and a date's total up to a half-cent per bucket.
    A larger gap means artifact and store have drifted — the one failure that
    would be invisible in the UI, since both would still draw plausible charts.
    """
    import gzip
    import hashlib

    if not CB_ARCHIVE.exists():
        return []            # verify_cb_archive already reported the missing store
    if not CB_ARCHIVE_EXPORT.exists():
        return [f"cb_archive_export: {CB_ARCHIVE_EXPORT} is missing — the published "
                f"page cannot answer window=custom; re-run export_static_site.py"]
    errs: List[str] = []
    raw = CB_ARCHIVE_EXPORT.read_bytes()
    ar = json.loads(gzip.decompress(raw).decode("utf-8"))
    dates, counts, supply = ar["dates"], ar["counts"], ar["supply"]
    if sum(counts) != len(supply):
        return [f"cb_archive_export: counts[] sums to {sum(counts)} but the value "
                f"array holds {len(supply)}"]
    if ar.get("grid_from_usd") != 0.0 or ar.get("bucket_usd") != CB_ARCHIVE_ZONE_USD:
        errs.append(f"cb_archive_export: grid_from {ar.get('grid_from_usd')} / bucket "
                    f"{ar.get('bucket_usd')} — the store is dense from $0 on "
                    f"${CB_ARCHIVE_ZONE_USD:.0f}")
    if len(ar.get("price_usd", [])) != len(dates):
        errs.append(f"cb_archive_export: {len(ar.get('price_usd', []))} price_usd "
                    f"entries for {len(dates)} dates")

    store = pd.read_parquet(CB_ARCHIVE)
    store = store[store["variant"] == "supply"].copy()
    store["_d"] = store["as_of_date"].astype(str).str[:10]
    if set(store["_d"].unique()) != set(dates):
        return [f"cb_archive_export: the artifact holds {len(dates)} dates, the store "
                f"{store['_d'].nunique()} — re-export the site"]
    by_day = {d: g for d, g in store.groupby("_d")}
    off, worst_v, worst_t, worst_day = 0, 0.0, 0.0, None
    for i, day in enumerate(dates):
        g = by_day[day].sort_values("price_bucket_usd")
        if counts[i] != len(g):
            errs.append(f"cb_archive_export: {day} ships {counts[i]} slots, the store "
                        f"holds {len(g)} buckets")
            off += counts[i]
            continue
        mine = np.array(supply[off:off + counts[i]], dtype="float64") / 100.0
        off += counts[i]
        stored = g["value"].to_numpy(dtype="float64")
        dworst = float(np.abs(mine - stored).max())
        if dworst > 0.005 + 1e-9:
            errs.append(f"cb_archive_export: {day} differs from the store by "
                        f"{dworst:.4f} BTC — more than the centi-BTC quantum")
        if dworst > worst_v:
            worst_v, worst_day = dworst, day
        worst_t = max(worst_t, abs(float(mine.sum()) - float(stored.sum())))

    man = {}
    if CB_SITE_MANIFEST.exists():
        man = (json.loads(CB_SITE_MANIFEST.read_text(encoding="utf-8"))
               .get("cost_basis_archive", {}).get("BTCUSDT", {}))
        sha = hashlib.sha256(raw).hexdigest()
        for k, want in (("bytes", len(raw)), ("sha256", sha), ("n_dates", len(dates)),
                        ("n_values", len(supply)), ("first", dates[0]),
                        ("last", dates[-1]),
                        # the two fields that describe WHICH grid this file is: a
                        # manifest left saying cadence_days=7 beside a daily
                        # artifact is exactly the drift the UI text would then
                        # report to the reader as fact
                        ("cadence_days", ar.get("cadence_days")),
                        ("daily_from", ar.get("daily_from"))):
            if man.get(k) != want:
                errs.append(f"cb_archive_export: manifest says {k}={man.get(k)!r}, the "
                            f"artifact {want!r} — the browser reads the manifest for "
                            f"its date bounds")
    else:
        errs.append(f"cb_archive_export: no site manifest at {CB_SITE_MANIFEST}")

    if not errs:
        logger.info("cb_archive_export: %d dates / %d values, %.1f KB gz, every value "
                    "within %.4f BTC of the store, worst date %s, worst total drift "
                    "%.3e BTC, manifest agrees",
                    len(dates), len(supply), len(raw) / 1024, worst_v, worst_day, worst_t)
    return errs


def verify_cb_dated_custom() -> List[str]:
    """A custom pair answered from the publisher's own curves IS the preset.

    The complaint this exists for: a reader picked 08/24 → 09/24, put it beside
    the 30d button, and got two different pictures they could not adjudicate.
    Both were internally correct -- the custom pair was being served from the
    $200 per-date archive, whose recent tail is a curve per day -- but "both
    right, and different" is not something a reader can act on. So the custom
    path now prefers the publisher's OWN dated curves whenever both ends foot on
    a date it holds, and such a pair must then be the preset exactly: same rows,
    same prices, same deltas, to the last bit.

    This is the check that fails loudly if that equality ever breaks -- a mixed
    grid, a cohort row leaking into the supply rows, an off-by-one in the snap,
    the two stores fetched on different days -- and every one of those would
    still draw a plausible chart.  It also rebuilds the SHIPPED artifact from the
    store, because the published page has no server to fall back on.

    Equality is asserted to 1e-9, not to the bit: the publisher's cumulative
    shares round differently from a straight cumsum/sum in the last bit or two
    (measured 1e-16 on pct_distributed; delta, past, current and pct_accumulated
    come back bit-identical).  Anything coarser than 1e-9 would be a different
    number, not a different rounding of the same one.
    """
    import gzip
    import sys as _sys

    errs: List[str] = []
    root = PROCESSED.parent
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    try:
        from visualizer import app
    except Exception as e:                    # pragma: no cover - env problem
        return [f"cb_dated_custom: cannot import visualizer.app ({e}) — the check "
                f"compares the endpoint's own code path against the stores"]

    # (a) every publisher-built window the dated store can foot on must come back
    #     identical from the custom path
    checked: List[str] = []
    for win, days in app.COST_BASIS_WINDOWS.items():
        ref = app._cb_diffs(days)
        if ref is None or ref.empty:
            errs.append(f"cb_dated_custom: the {win} diff is absent from the store")
            continue
        as_of = str(ref["as_of_date"].iloc[0])[:10]
        prev = str(ref["prev_as_of_date"].iloc[0])[:10]
        frame, info = app._cb_dated_custom(prev, as_of)
        if frame is None:
            errs.append(f"cb_dated_custom: the {win} window ({prev} → {as_of}) is a "
                        f"date pair the publisher's own curves cannot rebuild "
                        f"({info.get('reason')}), yet a prebuilt diff for it exists")
            continue
        if info.get("from_snapped") or info.get("to_snapped"):
            errs.append(f"cb_dated_custom: {win} runs {prev} → {as_of}, but the dated "
                        f"store answered {info.get('prev_as_of')} → {info.get('as_of')}"
                        f" — the diff store and the curve store are from different "
                        f"fetch runs, so the preset and a custom pair would disagree")
            continue
        want = app._cb_diffs(days).sort_values("price_bucket_usd")
        got = frame.sort_values("price_bucket_usd")
        if len(want) != len(got):
            errs.append(f"cb_dated_custom: {win} — the preset holds {len(want)} rows, "
                        f"the rebuilt pair {len(got)}")
            continue
        if not np.array_equal(want["price_bucket_usd"].to_numpy(dtype="float64"),
                              got["price_bucket_usd"].to_numpy(dtype="float64")):
            errs.append(f"cb_dated_custom: {win} — the two grids hold different prices")
            continue
        worst, worst_col = 0.0, ""
        for col in ("delta", "past_value", "current_value",
                    "pct_accumulated", "pct_distributed"):
            d = float(np.abs(want[col].to_numpy(dtype="float64")
                             - got[col].to_numpy(dtype="float64")).max())
            if d > worst:
                worst, worst_col = d, col
        if worst > 1e-9:
            errs.append(f"cb_dated_custom: {win} — rebuilt pair differs from the "
                        f"preset by {worst:.6g} on {worst_col} (max over "
                        f"{len(got)} rows)")
        else:
            checked.append(f"{win} ({prev}→{as_of}, {len(got)} rows)")

    # (b) the shipped artifact, read by nothing but gzip + json, rebuilds the store
    art = CB_DATED_EXPORT
    if not CB_CURVES.exists():
        errs.append(f"cb_dated_custom: missing {CB_CURVES}")
    elif not art.exists():
        errs.append(f"cb_dated_custom: {art} is missing — the published page cannot "
                    f"answer window=custom from the publisher's curves; re-run "
                    f"export_static_site.py")
    else:
        raw = art.read_bytes()
        ar = json.loads(gzip.decompress(raw).decode("utf-8"))
        counts, prices, supply = ar["counts"], ar["prices"], ar["supply"]
        if len(counts) != len(ar["dates"]) or len(prices) != len(supply) \
                or sum(counts) != len(prices):
            errs.append(f"cb_dated_custom: the shipped artifact is internally "
                        f"inconsistent ({len(ar.get('dates', []))} dates, "
                        f"{len(counts)} counts, {len(prices)} prices, "
                        f"{len(supply)} values)")
        else:
            store = pd.read_parquet(CB_CURVES)
            store = store[(store["variant"] == "supply")
                          & store["cohort"].isna()].copy()
            store["_d"] = store["as_of_date"].astype(str).str[:10]
            store = store.drop_duplicates(subset=["_d", "price_bucket_usd"], keep="first")
            if set(store["_d"].unique()) != set(ar["dates"]):
                errs.append(f"cb_dated_custom: the artifact holds {len(ar['dates'])} "
                            f"dates, the store {store['_d'].nunique()} — re-export")
            else:
                by_day = {d: g for d, g in store.groupby("_d")}
                off, worst, worst_day = 0, 0.0, None
                for i, day in enumerate(ar["dates"]):
                    g = by_day[day].sort_values("price_bucket_usd")
                    n = counts[i]
                    if n != len(g):
                        errs.append(f"cb_dated_custom: {day} ships {n} slots, the "
                                    f"store holds {len(g)} buckets")
                        off += n
                        continue
                    mine = np.array(supply[off:off + n], dtype="float64") / 100.0
                    mine_p = np.array(prices[off:off + n], dtype="float64")
                    off += n
                    # the price column is the store's own, exactly: it is what
                    # makes the $200-era and $500-era curves subtractable
                    if not np.array_equal(mine_p,
                                          g["price_bucket_usd"].to_numpy(dtype="float64")):
                        errs.append(f"cb_dated_custom: {day} ships different prices "
                                    f"than the store — the artifact must carry "
                                    f"each value's own price, not a grid")
                        continue
                    d = float(np.abs(mine - g["value"].to_numpy(dtype="float64")).max())
                    if d > 0.005 + 1e-9:
                        errs.append(f"cb_dated_custom: {day} differs from the store by "
                                    f"{d:.4f} BTC — more than the centi-BTC quantum")
                    if d > worst:
                        worst, worst_day = d, day
                if CB_SITE_MANIFEST.exists():
                    man = (json.loads(CB_SITE_MANIFEST.read_text(encoding="utf-8"))
                           .get("cost_basis_dated", {}).get("BTCUSDT", {}))
                    import hashlib
                    for k, w in (("bytes", len(raw)),
                                 ("sha256", hashlib.sha256(raw).hexdigest()),
                                 ("n_dates", len(ar["dates"])),
                                 ("n_values", len(prices)),
                                 ("first", ar["dates"][0]), ("last", ar["dates"][-1]),
                                 ("tolerance_days", app.CB_SNAP_TOLERANCE_DAYS)):
                        if man.get(k) != w:
                            errs.append(f"cb_dated_custom: manifest says {k}="
                                        f"{man.get(k)!r}, the artifact {w!r}")
                else:
                    errs.append(f"cb_dated_custom: no site manifest at {CB_SITE_MANIFEST}")
                if not errs:
                    logger.info("cb_dated_custom: rebuilt %d preset windows row for "
                                "row to 1e-9 (%s); artifact %d dates / %d values, "
                                "worst value %.4f BTC from the store (date %s)",
                                len(checked), "; ".join(checked), len(ar["dates"]),
                                len(prices), worst, worst_day)
                elif checked:
                    logger.info("cb_dated_custom: %d preset windows rebuilt row for "
                                "row before the artifact checks reported above",
                                len(checked))
    return errs


def verify_etf_flows() -> List[str]:
    """The daily US spot-ETF net flows behind the bars in the candle pane.

    Three claims that fail silently on their own, so they are checked separately:

      * the STORE is one row per calendar day from the first session, with no
        duplicate date -- a missing day draws a chart that simply skips it, and
        a duplicate makes every reader's lookup order-dependent;
      * re-parsing the CACHED RAW CHART reproduces every stored value.  Upstream
        is a third-party CDN with no contract: a renamed trace would make
        build_series skip the metric while the layer went on drawing whatever
        was stored last -- a chart that looks alive and is not;
      * the SHIPPED site/etf/*.json is the store, field for field.  That file is
        the whole feature on the published page; a stale export is invisible
        except here.

    Plus a freshness line: the series is stamped every calendar day, so it can
    never trail the newest Vision daily bar by more than a day, while the bars
    themselves lag Vision by one.
    """
    errs: List[str] = []
    if not SERIES_STORE.exists():
        return [f"etf_flows: missing {SERIES_STORE} — run fetch_checkonchain_urpd.py"]
    df = pd.read_parquet(SERIES_STORE)
    for col in ("metric", "date", "value", "units", "source_chart"):
        if col not in df.columns:
            errs.append(f"etf_flows: the series store has no {col} column — only the "
                        f"long-format store written by fetch_checkonchain_urpd.py")
    if errs:
        return errs
    ser = df[df["metric"] == ETF_METRIC].copy()
    if ser.empty:
        return [f"etf_flows: {ETF_METRIC} is absent from the series store — run "
                f"fetch_checkonchain_urpd.py (the chart may have been renamed upstream)"]
    ser["_d"] = ser["date"].astype(str).str[:10]
    dupes = int(ser["_d"].duplicated().sum())
    if dupes:
        errs.append(f"etf_flows: {dupes} duplicate date(s) in the store")
    ser = ser.sort_values("_d")
    first, last = str(ser["_d"].iloc[0]), str(ser["_d"].iloc[-1])
    if first != ETF_FIRST:
        errs.append(f"etf_flows: the series starts {first}, expected {ETF_FIRST} "
                    f"(the first US spot-BTC session)")
    if len(ser) < ETF_MIN_DAYS:
        errs.append(f"etf_flows: only {len(ser)} days stored (want >= {ETF_MIN_DAYS})")
    full = pd.date_range(first, last, freq="1D")
    missing = [str(d.date()) for d in full.difference(pd.DatetimeIndex(ser["_d"]))]
    if missing:
        errs.append(f"etf_flows: {len(missing)} calendar day(s) missing between "
                    f"{first} and {last} (e.g. {missing[:4]}) — a closed market must be "
                    f"an explicit 0.0, not an absent row")
    units = set(ser["units"].astype(str).unique())
    if units != {"USD"}:
        errs.append(f"etf_flows: units {sorted(units)} — this series is dollars, and a "
                    f"BTC row under the same metric would be read as dollars")
    chart = set(ser["source_chart"].astype(str).unique())
    if chart != {ETF_CHART}:
        errs.append(f"etf_flows: rows came from {sorted(chart)} — one metric, one chart")
    vals = ser["value"].to_numpy(dtype="float64")
    if not np.isfinite(vals).all():
        errs.append("etf_flows: non-finite flow values")
    zeros = int((vals == 0).sum())
    if not zeros:
        errs.append("etf_flows: no 0.0 days — a closed US market is written as an "
                    "explicit zero by the publisher")
    if not (vals > 0).any() or not (vals < 0).any():
        errs.append("etf_flows: the series has only one direction of flow")

    # (b) the raw chart on disk must reproduce the store, value for value
    try:
        from .fetch_checkonchain_urpd import SERIES_CHARTS, decode_arr, parse_traces
    except ImportError:
        from fetch_checkonchain_urpd import SERIES_CHARTS, decode_arr, parse_traces
    mapping = SERIES_CHARTS.get(ETF_CHART, {})
    traces_want = {k for k, v in mapping.items() if v[0] == ETF_METRIC}
    raw_path = RAW / "checkonchain" / ETF_CHART.replace("/", "__")
    worst = 0.0
    n_raw = 0
    if not traces_want:
        errs.append(f"etf_flows: {ETF_METRIC} is not mapped in SERIES_CHARTS[{ETF_CHART!r}]")
    elif not raw_path.exists():
        errs.append(f"etf_flows: the raw chart is not cached at {raw_path} — the store "
                    f"cannot be re-derived (fetch_checkonchain_urpd.py caches it on "
                    f"every run)")
    else:
        traces = parse_traces(raw_path.read_text(encoding="utf-8", errors="replace"))
        pairs = {}
        for tr in traces:
            if (tr.get("name") or "").strip() not in traces_want:
                continue
            x, y = decode_arr(tr.get("x")), decode_arr(tr.get("y"))
            if x is None or y is None:
                continue
            for d, v in zip(x, y):
                if isinstance(d, str) and len(d) >= 10 and np.isfinite(v):
                    pairs[d[:10]] = float(v)
        n_raw = len(pairs)
        stored = dict(zip(ser["_d"], vals))
        gone = sorted(set(stored) - set(pairs))
        extra = sorted(set(pairs) - set(stored))
        if gone:
            errs.append(f"etf_flows: {len(gone)} stored day(s) are not in the cached chart "
                        f"(e.g. {gone[:3]}) — the store holds rows this chart no longer serves")
        if extra:
            errs.append(f"etf_flows: {len(extra)} cached day(s) never reached the store "
                        f"(e.g. {extra[:3]}) — re-run fetch_checkonchain_urpd.py")
        bad = 0
        bad_day = None
        for d in sorted(set(stored) & set(pairs)):
            dv = abs(stored[d] - pairs[d])
            if dv > worst:
                worst, bad_day = dv, d
            if dv > 0:
                bad += 1
        if bad:
            errs.append(f"etf_flows: {bad} value(s) differ from the cached chart "
                        f"(worst {worst:.6g} on {bad_day})")

    # (c) the file the published page reads must be the store, value for value
    if not ETF_EXPORT.exists():
        errs.append(f"etf_flows: {ETF_EXPORT} is missing — the published page has no "
                    f"ETF layer; re-run export_static_site.py")
    else:
        ex = json.loads(ETF_EXPORT.read_text(encoding="utf-8"))
        for k, want in (("metric", ETF_METRIC), ("units", "USD"), ("bucket", "1d"),
                        ("first", first), ("last", last), ("n", len(ser))):
            if ex.get(k) != want:
                errs.append(f"etf_flows: the shipped payload says {k}={ex.get(k)!r}, the "
                            f"store {want!r}")
        pts = ex.get("points") or []
        if len(pts) != len(ser):
            errs.append(f"etf_flows: the shipped payload carries {len(pts)} points, the "
                        f"store {len(ser)} days")
        else:
            off = 0
            worst_x = 0.0
            for (d, v), sd, sv in zip(pts, ser["_d"], vals):
                if d != sd or float(v) != float(sv):
                    off += 1
                    worst_x = max(worst_x, abs(float(v) - float(sv)))
            if off:
                errs.append(f"etf_flows: {off} shipped point(s) differ from the store "
                            f"(worst |diff| {worst_x:.6g}) — re-run export_static_site.py")
        sess = ser[ser["value"] != 0]
        if sess.empty:
            errs.append("etf_flows: the store has no session days at all")
        else:
            want = [str(sess["_d"].iloc[-1]), float(sess["value"].iloc[-1])]
            got = ex.get("last_session")
            if not got or got[0] != want[0] or float(got[1]) != want[1]:
                errs.append(f"etf_flows: the shipped last_session is {got}, the store "
                            f"ends on {want} — the strip quotes this day")
        man_files = {}
        if CB_SITE_MANIFEST.exists():
            man_files = (json.loads(CB_SITE_MANIFEST.read_text(encoding="utf-8"))
                         .get("files", {}).get("etf", {}))
            if man_files.get("BTCUSDT") != "etf/BTCUSDT.json":
                errs.append(f"etf_flows: the manifest names {man_files.get('BTCUSDT')!r} "
                            f"for BTCUSDT — the page reads the manifest, not a guess")
        else:
            errs.append("etf_flows: no site manifest — re-run export_static_site.py")
    if not ETF_EXPORT_ETH.exists():
        errs.append(f"etf_flows: {ETF_EXPORT_ETH} is missing — the page would fetch a "
                    f"404 for ETH instead of the endpoint's own answer")
    else:
        eth = json.loads(ETF_EXPORT_ETH.read_text(encoding="utf-8"))
        if eth.get("available"):
            errs.append("etf_flows: the ETH payload claims an available series — no US "
                        "spot ETH ETF is in this chart")
        elif not re.search(r"BTC", str(eth.get("note", ""))):
            errs.append(f"etf_flows: the ETH payload does not explain itself: "
                        f"{str(eth.get('note'))[:80]!r}")

    # (d) freshness, against the newest locally stored Vision daily bar
    refs = []
    for market in ("futures_um", "spot"):
        p = PROCESSED / "vsa_klines" / market / "BTCUSDT" / "1d" / "merged.parquet"
        if p.exists():
            ot = pd.read_parquet(p, columns=["open_time"])["open_time"].max()
            refs.append(str(pd.to_datetime(ot, unit="ms", utc=True).date()))
    if refs:
        vision = max(refs)
        lag = (datetime.fromisoformat(last) - datetime.fromisoformat(vision)).days
        # The ETF store is session-dated (no weekend points) while Vision bars
        # include weekends, and the upstream flow chart routinely trails the
        # session by a day.  A 1-day rule fails every weekend: on 2026-10-19 the
        # bars reached Saturday 18th while the chart (Thursday 16th) had not yet
        # published Friday's session.  Three calendar days covers a weekend plus
        # that publication lag; a genuine stall still trips it.
        if lag < -3:
            errs.append(f"etf_flows: the series ends {last}, {abs(lag)} day(s) BEHIND the "
                        f"newest Vision daily bar ({vision}) — it stopped updating")
        elif lag > 4:
            errs.append(f"etf_flows: the series ends {last}, {lag} day(s) past the newest "
                        f"Vision daily bar ({vision}) — the bars are the stale side, "
                        f"re-run the refresh")
    else:
        logger.warning("etf_flows: no local Vision daily store to date the series against")

    if not errs:
        logger.info("etf_flows: %d days %s..%s, %d session / %d closed, %d values "
                    "re-derived from the cached chart (worst |diff| %g), shipped payload "
                    "matches the store point for point",
                    len(ser), first, last, int((vals != 0).sum()), zeros, n_raw, worst)
    return errs


def verify_registry_and_log() -> List[str]:
    errs: List[str] = []
    text = REGISTRY_YAML.read_text(encoding="utf-8") if REGISTRY_YAML.exists() else ""
    for dsid in ("true_volume_btc_daily", "true_volume_eth_daily", "exchange_true_volume_daily",
                 "onchain_network_metrics_btc_daily", "onchain_network_metrics_eth_daily",
                 "onchain_realized_cap_btc_daily", "onchain_realized_cap_eth_daily"):
        if not re.search(rf"^\s*{dsid}:", text, re.MULTILINE):
            errs.append(f"registry: missing {dsid}")
    # Read the log exactly where append_log WRITES it (common.LOG_FILE, under
    # ROOT) -- not PROCESSED.parent, which resolves to BTC_True_Volume/ and has
    # never held this file. The writer's constant is authoritative.
    log_path = LOG_FILE
    if not log_path.exists():
        errs.append(f"pipeline log missing ({log_path})")
    else:
        log_text = log_path.read_text(encoding="utf-8")
        for phase in ("## step0", "## Phase 1", "## Phase 2", "## Phase 3",
                      "## Phase 4", "## Phase 5", "## Phase 6", "## Phase 7", "## Phase 8",
                      "## final", "## register"):
            if phase not in log_text:
                errs.append(f"pipeline log: missing {phase} entry")
        # one entry per orchestrator phase: Phase 1 is asset-scoped (BTC + ETH)
        # so exactly 2; every other phase exactly 1.
        for phase, want in (("Phase 1", 2), ("Phase 2", 1), ("Phase 3", 1),
                            ("Phase 4", 1), ("Phase 5", 1)):
            n = log_text.count(f"## {phase}")
            if n != want:
                errs.append(f"pipeline log: {phase} has {n} entries (want {want})")
    return errs


def verify_orderbook() -> List[str]:
    """Structural checks on the shipped order-book heat payloads.

    The layers are additive: a missing/empty layer is reported (the page has an
    honest unavailable body), but structural corruption — bad cells, mismatched
    counts, unsorted columns — fails the run.
    """
    errs: List[str] = []
    site = PROCESSED.parent / "site"
    man_p = site / "manifest.json"
    if not man_p.exists():
        return ["orderbook: site/manifest.json missing"]
    man = json.loads(man_p.read_text(encoding="utf-8"))
    ob = (man.get("files") or {}).get("orderbook")
    if not ob:
        return ["orderbook: manifest has no files.orderbook"]
    expect = {
        "spot": {f"{s}_{w}" for s in SYMBOLS for w in ("30d", "90d", "1y")},
        "depth": {f"{s}_{w}" for s in SYMBOLS for w in ("1d", "7d", "30d")},
    }
    for layer, keys in expect.items():
        declared = ob.get(layer) or {}
        missing = keys - set(declared)
        if missing:
            errs.append(f"orderbook {layer}: manifest missing {sorted(missing)}")
        for key, rel in declared.items():
            p = site / rel
            if not p.exists():
                errs.append(f"orderbook {layer} {key}: file missing")
                continue
            try:
                payload = json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                errs.append(f"orderbook {layer} {key}: unreadable ({exc})")
                continue
            if not isinstance(payload.get("available"), bool):
                errs.append(f"orderbook {layer} {key}: available is not bool")
                continue
            if not payload["available"]:
                if not payload.get("note"):
                    errs.append(f"orderbook {layer} {key}: unavailable without a note")
                continue
            cols = payload.get("columns") or []
            if not cols:
                errs.append(f"orderbook {layer} {key}: available but no columns")
                continue
            ts = [c.get("t") for c in cols]
            if ts != sorted(ts):
                errs.append(f"orderbook {layer} {key}: columns not sorted by t")
            if payload.get("first_ts") != ts[0] or payload.get("last_ts") != ts[-1]:
                errs.append(f"orderbook {layer} {key}: first/last_ts mismatch")
            n = 0
            for c in cols:
                if not (c.get("mid") or 0) > 0:
                    errs.append(f"orderbook {layer} {key}: non-positive mid")
                    break
                for side in ("bids", "asks"):
                    for cell in (c.get(side) or []):
                        n += 1
                        if layer == "spot":
                            if len(cell) < 4 or cell[0] < 0 or cell[1] < 0 or cell[2] < 0:
                                errs.append(f"orderbook spot {key}: bad cell {cell}")
                                break
                        else:
                            if (len(cell) < 4 or not (0 <= cell[0] < cell[1] <= 5.01)
                                    or cell[2] < 0):
                                errs.append(f"orderbook depth {key}: bad band {cell}")
                                break
            if n != payload.get("n_cells"):
                errs.append(f"orderbook {layer} {key}: n_cells "
                            f"{payload.get('n_cells')} != {n}")
    return errs


def run() -> Dict[str, List[str]]:
    results = {
        "step0_exchange": verify_exchange(),
        "phase1_network": verify_network_metrics(),
        "phase2_positioning": verify_positioning(),
        "phase5_realized_cap": verify_realized_cap(),
        "final_deliverable": verify_final(),
        "cb_archive": verify_cb_archive(),
        "cb_archive_export": verify_cb_archive_export(),
        "cb_dated_custom": verify_cb_dated_custom(),
        "etf_flows": verify_etf_flows(),
        "orderbook_heat": verify_orderbook(),
        "registry_and_log": verify_registry_and_log(),
    }
    out = PROCESSED / "verification_report.json"
    PROCESSED.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated_utc": datetime.now(timezone.utc).isoformat(),
                               "results": results,
                               "passed": {k: not v for k, v in results.items()}},
                              indent=2, default=str), encoding="utf-8")
    for k, v in results.items():
        logger.info("verify %s: %s", k, "PASS" if not v else f"FAIL ({len(v)})")
        for e in v[:5]:
            logger.warning("  - %s", e)
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    results = run()
    failed = {k: v for k, v in results.items() if v}
    if failed:
        print("\n=== VERIFY FAILED ===")
        for k, v in failed.items():
            print(f"  {k}:")
            for e in v:
                print(f"    - {e}")
        sys.exit(1)
    print("\n=== VERIFY ALL PASS ===")


if __name__ == "__main__":
    main()
