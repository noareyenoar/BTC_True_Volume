"""export_static_site.py — snapshot the visualizer into site/ for GitHub Pages.

Reuses the app's OWN endpoint functions (visualizer.app) so the exported JSON is
the same data the local API serves — gap flags, the resampled 1d tail, daily
alignment, source metadata — with zero logic duplication. Only 1h + 1d are
exported (1m stays local: ~150 MB per store / 5M rows is not a phone or a Pages
budget — see visualizer_usage.md).

Layout produced under BTC_True_Volume/site/:
    manifest.json              boot file — static-mode detection + data_version
    symbols.json               /api/symbols response, intervals restricted to 1h/1d
    bars/{SYM}_{TF}.json[.gz]  /api/ohlc payloads, FULL history (no pagination)
    volume/{SYM}_{TF}.json[.gz]/api/volume_breakdown payloads, FULL history
    onchain/{SYM}.json         /api/onchain_snapshot payloads
    profile/{SYM}_{window}.json.gz  /api/volume_profile payloads (30d/90d/1y/all)
    cost_basis/{SYM}_{window}.json.gz  /api/cost_basis payloads (30d…365d, 4y)
    cost_basis/BTC_dated.json.gz       the publisher's own dated curves, the
                                       preferred source for window=custom
    cost_basis/BTC_archive.json.gz     weekly dated curves: window=custom's
                                       fallback — both subtracted in the browser
                                       (BTC only)
    index.html, app.js, style.css, vendor/…   copied verbatim from static/

Serialization matches starlette's JSONResponse.render byte-for-byte
(json.dumps(ensure_ascii=False, allow_nan=False, separators=(",", ":"))),
plus a NaN -> null sanitize: starlette would 500 on a NaN payload, the browser
shows the honest "n/a" — the static site is the robust superset.

Run from the project root:  .venv\Scripts\python.exe BTC_True_Volume\export_static_site.py
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("tvp.export_site")

BASE = Path(__file__).resolve().parent                       # BTC_True_Volume
sys.path.insert(0, str(BASE))                                # -> import visualizer.app
from visualizer import app                                   # noqa: E402

SITE = BASE / "site"
SYMS = tuple(app.SYMBOLS)
INTERVALS = ("1h", "1d")          # phone/Pages scope; 1m stays local
GZIP_INTERVALS = ("1h",)          # the big per-bar files travel compressed
# volume-profile files, one per (symbol, window); read from the route's own table
# so the two can never drift apart
PROFILE_WINDOW_KEYS = tuple(app.PROFILE_WINDOWS)
# cost-basis windows, likewise read from the route's own table. Exported for
# every symbol so the client lookup never 404s: ETH's files carry the honest
# "no ETH equivalent exists" answer rather than a failed fetch.
# every window the API answers, publisher-built and locally computed alike --
# otherwise "4y" would 404 in static mode while working in live mode
COST_BASIS_WINDOW_KEYS = tuple(app.COST_BASIS_ALL)
MAX_JSON_DEPTH = 32


# --------------------------------------------------------------------------- #
#  serialization                                                               #
# --------------------------------------------------------------------------- #

def _clean(o):
    """numpy scalars -> native; float nan/inf -> None (browser-safe null)."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if o is None or isinstance(o, (bool, str, int)):
        return o
    if isinstance(o, np.floating):
        return None if not math.isfinite(float(o)) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    raise TypeError(f"unserializable {type(o).__name__}: {o!r}")


def _dump(payload, path: Path, gz: bool) -> None:
    """Write payload as starlette-identical JSON; NaN -> null; atomic tmp+replace."""
    text = json.dumps(_clean(payload), ensure_ascii=False, allow_nan=False,
                      indent=None, separators=(",", ":"))
    tmp = path.with_name(path.name + ".tmp")
    if gz:
        tmp.write_bytes(gzip.compress(text.encode("utf-8"), 9))
    else:
        tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    logger.info("wrote %s (%.1f KB)", path.name, path.stat().st_size / 1024)


# --------------------------------------------------------------------------- #
#  export                                                                      #
# --------------------------------------------------------------------------- #

def _export_cb_archive() -> dict:
    """Ship the weekly dated curves with the site, as one flat centi-BTC array.

    `window=custom` is the one cost-basis window the publisher never built and
    the server computes on demand from `urpd_archive.parquet`; the published site
    has no server, so it ships the whole archive and subtracts two dates in the
    browser.  That also means the page never touches bitview.space at view time --
    the sister's phone reads bytes that are already here.

    Encoding: values are integers in HUNDREDTHS of a BTC (the largest single
    bucket is ~3M BTC, so cents fit with room to spare) because a JSON array of
    decimals does not compress.  Each date owns `counts[i]` consecutive slots
    starting at $0 -- the same dense-from-$0 layout the parquet store uses, so no
    per-value index is needed and the browser can cumsum straight down the array.
    """
    df = app._cb_archive()
    if df is None:
        logger.warning("no dated-curve archive in the store — the published site "
                       "will answer window=custom with 're-publish the site'")
        return {}
    dates = sorted(df["_d"].unique().tolist())
    counts: list = []
    supply: list = []
    price: list = []
    for day in dates:
        rows = df[df["_d"] == day].sort_values("price_bucket_usd")
        vals = rows["value"].to_numpy(dtype="float64")
        counts.append(int(vals.size))
        supply.extend(int(round(float(v) * 100)) for v in vals)
        # the same price the API would report for this date, so a custom pair
        # reads identically in the two modes (see app._metric_on)
        price.append(app._metric_on("price_usd", day))
    path = SITE / "cost_basis" / "BTC_archive.json.gz"
    _dump({"bucket_usd": app.CB_ARCHIVE_ZONE_USD, "grid_from_usd": 0.0,
           "cadence_days": 7, "dates": dates, "counts": counts,
           "supply": supply, "price_usd": price}, path, gz=True)
    return {"BTCUSDT": {
        "path": "cost_basis/BTC_archive.json.gz",
        "bucket_usd": app.CB_ARCHIVE_ZONE_USD,
        "cadence_days": 7,
        "tolerance_days": app.CB_SNAP_TOLERANCE_DAYS,
        # a requested end past the newest stored curve is still answered from
        # it within this many days (the page red-flags the days beyond it)
        "fresh_grace_days": app.CB_FRESH_GRACE_DAYS,
        "first": dates[0], "last": dates[-1],
        "n_dates": len(dates), "n_values": len(supply),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }}


def _export_cb_dated() -> dict:
    """Ship the publisher's OWN dated curves -- the store a custom pair prefers.

    `urpd_curves.parquet` is the generation the fixed windows are built from, so
    a pair whose ends both foot on stored dates is the publisher's own
    subtraction and agrees with the preset panel bar for bar; the weekly archive
    shipped beside this file is the fallback (it reaches further back, one date a
    week, and no further forward than the newest Sunday).

    Encoding: the same flat centi-BTC array as `BTC_archive.json.gz`, but with an
    explicit `prices[i]` per value rather than a dense-from-$0 grid: this store
    mixes generations, $200-bucket curves before 2024 and $500-bucket ones since,
    and the pre-2024 charts are not dense (a bucket the publisher charted as empty
    is simply absent).  A price array costs ~1 KB per date and gzips away, since
    consecutive dates share almost all of it; a dense assumption would silently
    invent the missing rows, and a zero in the past reads as coins arriving.
    """
    df = app._cb_dated_curves()
    if df is None:
        logger.warning("no dated-curve store (%s) — the published site will "
                       "answer window=custom from the weekly archive only",
                       app.CB_CURVES.name)
        return {}
    dates = sorted(df["_d"].unique().tolist())
    counts: list = []
    prices: list = []
    supply: list = []
    price: list = []
    for day in dates:
        rows = df[df["_d"] == day].sort_values("price_bucket_usd")
        vals = rows["value"].to_numpy(dtype="float64")
        counts.append(int(vals.size))
        prices.extend(float(p) for p in rows["price_bucket_usd"].to_numpy(dtype="float64"))
        supply.extend(int(round(float(v) * 100)) for v in vals)
        # the same price the API would report for this date, so a dated pair
        # reads identically in the two modes (see app._metric_on)
        price.append(app._metric_on("price_usd", day))
    path = SITE / "cost_basis" / "BTC_dated.json.gz"
    _dump({"bucket_usd": app.CB_DATED_ZONE_USD, "grid_from_usd": 0.0,
           "cadence_days": None, "dates": dates, "counts": counts,
           "prices": prices, "supply": supply, "price_usd": price}, path, gz=True)
    return {"BTCUSDT": {
        "path": "cost_basis/BTC_dated.json.gz",
        "bucket_usd": app.CB_DATED_ZONE_USD,
        "cadence_days": None,
        # not a snapshot of a rail layout: the client must not assume a grid, it
        # reads prices[i] -- kept in the manifest so the two implementations
        # cannot drift on how far a requested date may be from a stored one
        "tolerance_days": app.CB_SNAP_TOLERANCE_DAYS,
        # and how far past the newest stored date an end may still be answered
        "fresh_grace_days": app.CB_FRESH_GRACE_DAYS,
        "first": dates[0], "last": dates[-1],
        "n_dates": len(dates), "n_values": len(supply),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }}


def build_site() -> dict:
    SITE.mkdir(parents=True, exist_ok=True)
    for sub in ("bars", "volume", "onchain", "profile", "cost_basis", "etf"):
        (SITE / sub).mkdir(exist_ok=True)

    # 1) static assets — the app's own frontend, verbatim
    static_dir = BASE / "visualizer" / "static"
    for p in sorted(static_dir.rglob("*")):
        if p.is_file():
            tgt = SITE / p.relative_to(static_dir)
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, tgt)
    logger.info("static assets copied (%d files)", sum(1 for _ in static_dir.rglob("*")))

    # 2) symbols — call the endpoint with INTERVALS restricted to 1h/1d so its
    #    own format is used verbatim (coverage computed only for exported ones)
    orig_intervals = app.INTERVALS
    app.INTERVALS = INTERVALS
    try:
        symbols_payload = app.symbols()
    finally:
        app.INTERVALS = orig_intervals
    _dump(symbols_payload, SITE / "symbols.json", gz=False)

    # 3) per-symbol data — full-history endpoint payloads
    files = {"symbols": "symbols.json", "bars": {}, "volume": {}, "onchain": {},
             "profile": {}, "cost_basis": {}, "etf": {}}
    last_dates: list[int] = []
    for sym in SYMS:
        for iv in INTERVALS:
            key = f"{sym}_{iv}"
            gz = iv in GZIP_INTERVALS
            ohlc_payload = app.ohlc(sym, iv, limit=10**9)
            vb_payload = app.volume_breakdown(sym, iv, limit=10**9)
            _dump(ohlc_payload, SITE / "bars" / (key + ".json" + (".gz" if gz else "")), gz=gz)
            _dump(vb_payload, SITE / "volume" / (key + ".json" + (".gz" if gz else "")), gz=gz)
            files["bars"][key] = f"bars/{key}.json" + (".gz" if gz else "")
            files["volume"][key] = f"volume/{key}.json" + (".gz" if gz else "")
            if ohlc_payload["coverage"].get("end"):
                last_dates.append(ohlc_payload["coverage"]["end"])
            logger.info("exported %s %s: %d bars",
                        sym, iv, ohlc_payload["coverage"].get("n"))
        _dump(app.onchain_snapshot(sym), SITE / "onchain" / f"{sym}.json", gz=False)
        files["onchain"][sym] = f"onchain/{sym}.json"

        # spot-ETF daily net flows — interval-independent (one point per calendar
        # day), so one file per symbol, and ETH's file is the endpoint's own
        # honest-unavailable body rather than an absent one the page must guess at
        etf_payload = app.etf_flows(sym)
        _dump(etf_payload, SITE / "etf" / f"{sym}.json", gz=False)
        files["etf"][sym] = f"etf/{sym}.json"
        logger.info("exported %s etf flows: %s", sym,
                    f"{etf_payload['n']} days to {etf_payload['last']}"
                    if etf_payload["available"] else "unavailable")

        # volume profile — one file per (symbol, window). Windows are frozen at
        # export time; the on-page profile-window buttons just switch files.
        for win in PROFILE_WINDOW_KEYS:
            key = f"{sym}_{win}"
            _dump(app.volume_profile(sym, win), SITE / "profile" / (key + ".json.gz"),
                  gz=True)
            files["profile"][key] = f"profile/{key}.json.gz"
        logger.info("exported %s volume profile: %d windows",
                    sym, len(PROFILE_WINDOW_KEYS))

        # cost basis (on-chain URPD) — one file per (symbol, window), same shape
        for win in COST_BASIS_WINDOW_KEYS:
            key = f"{sym}_{win}"
            _dump(app.cost_basis(sym, win), SITE / "cost_basis" / (key + ".json.gz"),
                  gz=True)
            files["cost_basis"][key] = f"cost_basis/{key}.json.gz"
        logger.info("exported %s cost basis: %d windows",
                    sym, len(COST_BASIS_WINDOW_KEYS))

    # 4) both dated-curve stores, shipped whole so `window=custom` is computed in
    #    the browser instead of by a server this export does not have. The
    #    publisher's own dated curves are the preferred source (the preset's own
    #    generation), the weekly archive the fallback.
    cb_archive = _export_cb_archive()
    cb_dated = _export_cb_dated()

    # 5) manifest — the frontend's static-mode detection + data_version
    data_version = datetime.fromtimestamp(max(last_dates) / 1000, tz=timezone.utc) \
        .strftime("%Y-%m-%d") if last_dates else "unknown"
    manifest = {
        "site": "ta-vsa-visualizer",
        "version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_version": data_version,
        "data_note": f"data through {data_version} (Binance Vision publish lag ~1 day)",
        "intervals": list(INTERVALS),
        "symbols": list(SYMS),
        "files": files,
        # outside `files` on purpose: those values must stay plain path strings
        "cost_basis_archive": cb_archive,
        "cost_basis_dated": cb_dated,
    }
    _dump(manifest, SITE / "manifest.json", gz=False)
    # Exclude .git: site/ is its own repo and every publish re-commits the data
    # files, so history grows without bound while the published site does not.
    # Counting it made this line read 137 MB for a 13 MB site.
    total_mb = sum(
        p.stat().st_size for p in SITE.rglob("*")
        if p.is_file() and ".git" not in p.parts
    ) / 2**20
    logger.info("=== site/ ready: data through %s, %.1f MB total ===",
                data_version, total_mb)
    return manifest


if __name__ == "__main__":
    build_site()
