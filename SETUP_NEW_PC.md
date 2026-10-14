# Setting up on a new PC

Written 2026-10-14, from the machine that ran the pipeline (Windows, Python
3.11.9 — see `requirements-lock.txt`).

## What git carries, what you carry

| Piece | Where it comes from |
|---|---|
| All code, docs, prompts, logs, state, `raw/` caches, small `processed/` outputs | `git clone` (this repo, branch `src`) |
| Published site working tree | `git clone` (same repo, branch `main` → into `site/`) |
| `processed/vsa_klines/**/1m/merged.parquet` (4 files, ~665 MB) | **copy by drive** (or rebuild from `binance_full_dataset`) |
| `binance_full_dataset/` (aggTrades + klines store, tens of GB) | **copy by drive** (or `fetch_binance_vision.py`) |
| Sibling stores read by the pipeline: `onchain_dataset/`, `dataset_1d/`, `deribit_options/`, `Market_Maker_datasets/` | **copy by drive** |
| `.env` (API keys — Etherscan optional), `data_registry.yaml` | **copy by hand** — never in git |
| `.venv` | recreate (step 3) |

## Steps

All scripts derive their paths from their own location; the one external path
is the registry: `BTC_True_Volume/common.py` reads `../data_registry.yaml`
(i.e. `D:\kp_ai_agent\data_registry.yaml`). Keep the same shape on the new
box (pick your own drive letter and stay consistent):

```
D:\kp_ai_agent\
├─ data_registry.yaml
└─ Dataset_Fetcher\              <- project root ("ROOT" in the code)
   ├─ .env                       <- carried by hand
   ├─ .venv\                     <- step 3
   ├─ binance_full_dataset\      <- carried by drive
   ├─ onchain_dataset\ dataset_1d\ deribit_options\ Market_Maker_datasets\
   └─ BTC_True_Volume\           <- step 1 (this repo, branch src)
      ├─ site\                   <- step 2 (branch main clone)
      ├─ processed\  raw\
```

1. **Clone the source** (inside `Dataset_Fetcher\`):
   ```
   git clone -b src https://github.com/noareyenoar/BTC_True_Volume.git BTC_True_Volume
   ```
2. **Clone the site tree** (the Pages `main` branch, nested, gitignored here):
   ```
   git clone https://github.com/noareyenoar/BTC_True_Volume.git BTC_True_Volume/site
   ```
   Do this instead of letting `publish_site.py` bootstrap an empty repo — an
   empty local history cannot be pushed over the existing `main`.
3. **Recreate the venv** (in `Dataset_Fetcher\`):
   ```
   py -3.11 -m venv .venv
   .venv\Scripts\python.exe -m pip install -r BTC_True_Volume\requirements-lock.txt
   ```
4. **Carry the data** from the table above (drive/network copy), including
   `.env` and `data_registry.yaml`.
5. **Verify** (from `Dataset_Fetcher\`):
   ```
   .venv\Scripts\python.exe BTC_True_Volume\refresh_before_visualizer.py --no-launch
   .venv\Scripts\python.exe -m uvicorn BTC_True_Volume.visualizer.app:app --host 127.0.0.1 --port 8000
   ```
   Then the one-click path, once the .bat's `PROJ=` line is edited for the new
   path: `tools\Z_BTCTrueVolumerefresh-and-publish.bat` (the live copy on the
   old box ran from the drive root).

## Git workflow on the new box

- In `BTC_True_Volume\` you are on branch `src` — commit and push your work here.
- In `BTC_True_Volume\site\` you are on `main` — only `publish_site.py` should
  write there.
- This repo is **public**: never commit `.env` or anything with API keys.
