"""
common.py — shared infrastructure for the True Volume proxy pipeline.

Everything lives in one place: canonical paths, label constants (the plan's
data_source / confidence_tier guardrail), rate-limited HTTP with retry, raw
API response caching, the plan-literal pipeline-log appender, and atomic
parquet + _meta.json writing.

All paths are derived from this file's location (project root = parents[2]),
never from cwd. Use `python .venv\\Scripts\\python.exe BTC_True_Volume\\...` from
the project root.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

try:  # dotenv is optional; env vars may already be set
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

logger = logging.getLogger("tvp.common")

# ---------------------------------------------------------------------------
# Canonical paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]                      # Dataset_Fetcher
# PROCESSED is the tree every phase writes and the visualizer reads:
# BTC_True_Volume/processed (NOT ROOT/"processed" — the phase outputs,
# rollup cache and final deliverables all live here; the app's DATA_ROOT
# is the same path, so keep this in sync if the layout ever changes).
PROCESSED = Path(__file__).resolve().parent / "processed"
RAW = Path(__file__).resolve().parent / "raw"                   # API response cache
LOG_FILE = ROOT / "true_volume_pipeline_log.md"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

KLINE_STORE = ROOT / "binance_full_dataset" / "processed"
AGGTRADES_DIR = KLINE_STORE / "futures_um" / "aggTrades"
ONCHAIN_LOCAL = ROOT / "onchain_dataset" / "processed" / "onchain_dataset.parquet"
SYMBOLS_STORE = ROOT / "dataset_1d" / "symbols"                 # spot cross-fill source
LAKE_DIR = ROOT / "dataset_1d" / "lake"
DERIBIT_LIVE = ROOT / "deribit_options" / "live"
MM_REPORT = ROOT / "Market_Maker_datasets" / "mm_proxy_criteria_report.md"
REGISTRY_YAML = ROOT.parent / "data_registry.yaml"              # D:\kp_ai_agent\data_registry.yaml

SYMBOLS = ("BTCUSDT", "ETHUSDT")
ASSETS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
COINMETRICS_ASSETS = {"BTC": "btc", "ETH": "eth"}

# ---------------------------------------------------------------------------
# Label constants (plan guardrail: every output carries data_source +
# confidence_tier; a proxy metric is never presented as literal true volume)
# ---------------------------------------------------------------------------
DS = {
    "blockchain_com": "blockchain.com_charts_api",
    "etherscan": "etherscan_free_api",
    "coinmetrics_api": "coinmetrics_community_api",
    "coinmetrics_derived": "coinmetrics_community_derived",
    "binance_vision_klines": "binance_vision_klines",
    "binance_vision_aggtrades": "binance_vision_aggtrades",
    "binance_vision_metrics": "binance_vision_metrics",
    "binance_spot_depth": "binance_spot_rest_depth",
    "binance_vision_bookdepth": "binance_vision_bookdepth_10m",
    "deribit": "deribit_public_api",
    "onchain_local": "onchain_dataset_local",
    "checkonchain_github": "checkonchain_public_github_pages",
    "checkonchain_charts": "checkonchain_charts_cdn",
    "checkonchain_bitview": "checkonchain_bitview_series_api",
    "crossfill": "dataset_1d_symbols_crossfill",
    "binance_rest_top_trader": "binance_rest_top_trader",
}
TIER = {"High": "High", "Medium": "Medium", "Low": "Low", "NA": "N/A"}

# ---------------------------------------------------------------------------
# Env (values are never printed or logged)
# ---------------------------------------------------------------------------
_ETHERSCAN_KEYS = ("ETHERSCAN_API_KEY", "ETHERSCAN_KEY")


def load_env() -> None:
    """Load .env from the project root (or nearest ancestor)."""
    if load_dotenv is None:
        return
    for candidate in (ROOT / ".env", ROOT.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            return


def has_etherscan_key() -> bool:
    """True if an Etherscan key env var is present (presence only, never value)."""
    load_env()
    return any(bool(os.getenv(k, "").strip()) for k in _ETHERSCAN_KEYS)


# ---------------------------------------------------------------------------
# Rate-limited HTTP
# ---------------------------------------------------------------------------
class RateLimitedClient:
    """Session with a global minimum interval between requests and retries.

    Pattern mirrors onchain_dataset/coinmetrics_fetcher.py: 429 -> backoff,
    403 -> logged as requiring a paid tier (never silently retried forever).
    """

    def __init__(self, min_interval: float = 1.0, max_retries: int = 4,
                 user_agent: Optional[str] = None):
        self.session = requests.Session()
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_request_time = 0.0

    def get(self, url: str, params: Optional[Dict[str, Any]] = None,
            timeout: int = 60,
            headers: Optional[Dict[str, str]] = None) -> Optional[requests.Response]:
        """GET with retries.  ``headers`` is per-request (e.g. If-Modified-Since).

        A 304 counts as success and is returned as-is: it is the *expected*
        answer to a conditional request whose resource has not moved, and
        treating it as a failure would burn four retries and thirty seconds on
        exactly the case conditional requests exist to make cheap.
        """
        for attempt in range(self.max_retries):
            elapsed = time.time() - self._last_request_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            try:
                resp = self.session.get(url, params=params, timeout=timeout,
                                        headers=headers)
            except requests.RequestException as exc:
                logger.warning("request error %s (attempt %d): %s", url, attempt + 1, exc)
            else:
                self._last_request_time = time.time()
                if resp.status_code == 429:
                    logger.warning("429 rate limited on %s — backing off 10s", url)
                    time.sleep(10)
                    continue
                if resp.status_code in (403, 401):
                    logger.error("HTTP %s on %s — likely requires a paid tier/key", resp.status_code, url)
                    return None
                if resp.status_code == 404:
                    resp.raise_for_status()  # let caller decide via except
                if resp.status_code in (200, 304):
                    return resp
                logger.warning("HTTP %s on %s (attempt %d)", resp.status_code, url, attempt + 1)
            time.sleep(5 * (attempt + 1))
        logger.error("gave up after %d attempts: %s", self.max_retries, url)
        return None

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        resp = self.get(url, params=params)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            logger.error("non-JSON response from %s", url)
            return None


# ---------------------------------------------------------------------------
# Raw response cache (under BTC_True_Volume/raw/)
# ---------------------------------------------------------------------------
def cache_json(rel_path: Path, obj: Any) -> None:
    p = RAW / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def load_cached_json(rel_path: Path) -> Optional[Any]:
    p = RAW / rel_path
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Atomic file writing (temp + rename — a crash never leaves a corrupt output)
# ---------------------------------------------------------------------------
def atomic_write_parquet(df: pd.DataFrame, path: Path, metadata: Optional[Dict[str, str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # preserve a NAMED index (e.g. the date index some phases write); drop the
    # unnamed RangeIndex that from_pandas adds for column-based frames
    table = pa.Table.from_pandas(df, preserve_index=True)
    if "__index_level_0__" in table.column_names:
        table = table.drop("__index_level_0__")
    if metadata:
        table = table.replace_schema_metadata(metadata)
    pq.write_table(table, tmp, compression="zstd", version="2.6")
    os.replace(tmp, path)


def atomic_write_json(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
    os.replace(tmp, path)


def save_json_cache(path: Path, obj: Dict[str, Any]) -> None:
    """Atomic tmp+rename JSON write with Windows PermissionError retry
    (transient AV lock), mirroring compute_large_trades._save_ckpt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, default=str)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(1.0 + attempt)
    path.write_text(text, encoding="utf-8")  # last resort: direct overwrite


def load_json_cache(path: Path) -> Optional[Dict[str, Any]]:
    """Read a JSON cache; missing or corrupt -> None (caller warns), never crash."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def write_parquet_with_meta(df: pd.DataFrame, path: Path, meta: Dict[str, Any],
                            data_source: str, confidence_tier: str) -> None:
    """Write parquet + sibling _meta.json. Parquet metadata carries the
    guardrail labels so provenance survives even if _meta.json is lost."""
    meta = dict(meta)
    meta.setdefault("data_source", data_source)
    meta.setdefault("confidence_tier", confidence_tier)
    meta.setdefault("generated_utc", datetime.now(timezone.utc).isoformat())
    meta.setdefault("row_count", int(len(df)))
    if "date" in df.columns:
        meta.setdefault("coverage", [str(df["date"].min()), str(df["date"].max())])
    atomic_write_parquet(df, path, {"data_source": data_source,
                                    "confidence_tier": confidence_tier,
                                    "dataset_id": meta.get("dataset_id", "")})
    atomic_write_json(meta, path.with_name("_meta.json"))


# ---------------------------------------------------------------------------
# Daily index / gap helpers
# ---------------------------------------------------------------------------
def daily_index(start: str, end: str) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="1D", tz="UTC")


def missing_days(dates: pd.Series, start: str, end: str) -> List[pd.Timestamp]:
    """Dates in [start, end] absent from `dates` (normalized UTC-day series)."""
    have = set(pd.DatetimeIndex(pd.to_datetime(dates, utc=True)).normalize())
    want = daily_index(start, end)
    return [d for d in want if d not in have]


# ---------------------------------------------------------------------------
# Plan-literal pipeline log appender
# ---------------------------------------------------------------------------
def append_log(phase: str, name: str, asset: str, status: str, output: str,
               coverage: str, confidence: str, limitations: List[str]) -> None:
    """Write a phase entry to true_volume_pipeline_log.md using the master
    plan's exact template. Status: COMPLETE / NO-GO / DEFERRED / INCOMPLETE /
    NOT-STARTED.

    Idempotent: an existing section with the same "## {phase} — ... — [{asset}]"
    header is REPLACED (so a re-run of a completed phase updates in place and
    the log always has exactly one entry per phase/asset — the supervisor
    re-runs phases on cache hits)."""
    header = (f"\n## {phase} — {name} — [{asset}]\n"
              f"- Status: {status}\n"
              f"- Output location: {output}\n"
              f"- Coverage: {coverage}\n"
              f"- Confidence tier: {confidence}\n")
    body = "- Known limitations:\n" + "\n".join(f"  - {l}" for l in limitations) + "\n"
    entry = header + body
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.write_text("# True Volume Pipeline Log\n"
                            "Running log for the master true-volume proxy plan "
                            "(BTC_True_Volume/master_true_volume_pipeline_plan.md).\n"
                            "One entry per phase; re-runs replace in place.\n",
                            encoding="utf-8")
    text = LOG_FILE.read_text(encoding="utf-8")
    # remove existing sections with the same phase + asset (asset-scoped so a
    # "Phase 1 — ... [BTC]" entry never deletes the "[ETH]" one)
    import re
    marker_re = re.compile(rf"\n## {re.escape(phase)} — .*? — \[{re.escape(asset)}\]\n",
                           re.DOTALL)
    text = marker_re.sub("\n", text)
    # collapse leftover blank lines where a section was removed
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.rstrip("\n") + "\n" + entry
    LOG_FILE.write_text(text, encoding="utf-8")
    logger.info("log entry written (replace-in-place): %s %s %s", phase, asset, status)


# ---------------------------------------------------------------------------
# Phase result contract
# ---------------------------------------------------------------------------
@dataclass
class PhaseResult:
    phase: str                                  # e.g. "phase1_btc"
    name: str                                   # human name e.g. "Network-level on-chain volume"
    asset: str                                  # "BTC", "ETH", or "BOTH"
    status: str                                 # COMPLETE / NO-GO / DEFERRED / INCOMPLETE / NOT-STARTED
    output: str                                 # "path/to/output (n rows)"
    coverage: str                               # "2017-08-17 -> 2026-08-13, 3,289 rows"
    confidence: str                             # "High/Medium/Low as a volume proxy" (+ why)
    limitations: List[str] = field(default_factory=list)
    outputs: List[Path] = field(default_factory=list)
