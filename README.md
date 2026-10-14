# BTC True Volume — TA + VSA visualizer pipeline

Data pipeline and local visualizer for the BTC/ETH "true volume" work: Binance
Vision klines + aggTrades, on-chain (checkonchain URPD / cohorts), cost basis,
ETF flows, volume profile — served to a lightweight-charts page (BTC + ETH,
1h + 1d) and published as a static site.

## Repo layout — github.com/noareyenoar/BTC_True_Volume

This GitHub repo carries **two branches with unrelated trees**:

| Branch | What it is | Working tree on disk |
|---|---|---|
| `src`  | This source tree (everything below, incl. raw caches + small processed outputs) | this folder |
| `main` | The published GitHub Pages site (static export) | `site/` — a separate clone of `main` |

`publish_site.py` re-exports `site/` and pushes it to `main` from its own
nested working tree. Don't edit `main` by hand; don't commit from `site/`
into `src`.

## Entry points

Run everything from the project root (`Dataset_Fetcher`, one level up) with
the venv python — see `visualizer_usage.md` for the full guide:

```
.venv\Scripts\python.exe BTC_True_Volume\refresh_before_visualizer.py   # all feeds -> latest Vision date, then server
.venv\Scripts\python.exe BTC_True_Volume\refresh_before_visualizer.py --no-launch
.venv\Scripts\python.exe BTC_True_Volume\publish_site.py               # re-export site/ + push to Pages
.venv\Scripts\python.exe -m uvicorn BTC_True_Volume.visualizer.app:app --host 127.0.0.1 --port 8000
```

`tools/Z_BTCTrueVolumerefresh-and-publish.bat` is the one-click
refresh-then-publish .bat (a tracked copy; the live one runs from the drive
root and has the project path hardcoded).

## What is NOT in this repo

- `site/` — separate clone of `main` (see above).
- `processed/vsa_klines/**/1m/merged.parquet` — 4 files, 160–200 MB each,
  over GitHub's 100 MB per-file limit. Rebuild from `../binance_full_dataset`
  aggTrades or carry on a drive.
- `.env` (API keys), `.venv/`, and the sibling stores under
  `Dataset_Fetcher/` the pipeline reads (`binance_full_dataset`,
  `onchain_dataset`, `dataset_1d`, `deribit_options`, `Market_Maker_datasets`)
  — the big ones never fit in git.

Moving to a new machine: **SETUP_NEW_PC.md**.
