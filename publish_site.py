"""publish_site.py — rebuild site/ and push it to GitHub Pages.

Run AFTER refresh_before_visualizer.py to update the shared page your sister
opens on her phone: the site is re-exported from the refreshed data and pushed
to https://github.com/noareyenoar/BTC_True_Volume (Pages auto-redeploys from
branch main in ~1-2 min). She just re-opens her bookmark.

    .venv\Scripts\python.exe BTC_True_Volume\publish_site.py        # export + push
    .venv\Scripts\python.exe BTC_True_Volume\publish_site.py --no-push   # export + commit only

One-time setup (browser, ~2 min): create the public repo `BTC_True_Volume`
(renamed from `tv-vsa` 2026-08-23), then Repo -> Settings -> Pages -> Source:
"Deploy from a branch" -> main. First push pops a Git Credential Manager
login once.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("tvp.publish")

BASE = Path(__file__).resolve().parent                       # BTC_True_Volume
SITE = BASE / "site"
REPO_URL = "https://github.com/noareyenoar/BTC_True_Volume.git"
BRANCH = "main"
GIT_USER = "noareyenoar"
GIT_EMAIL = "noareyenoar@users.noreply.github.com"

sys.path.insert(0, str(BASE))
import export_static_site  # noqa: E402


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    logger.info("git %s", " ".join(args))
    return subprocess.run(["git", *args], cwd=SITE, check=check,
                          capture_output=True, text=True, encoding="utf-8")


def publish(no_push: bool = False) -> None:
    manifest = export_static_site.build_site()
    data_version = manifest["data_version"]

    # --- repo bootstrap (idempotent) ---
    if not (SITE / ".git").exists():
        _git("init", "-b", BRANCH)
    # local identity fallback if global git config is incomplete
    if not _git("config", "user.name", check=False).stdout.strip():
        _git("config", "user.name", GIT_USER)
    if not _git("config", "user.email", check=False).stdout.strip():
        _git("config", "user.email", GIT_EMAIL)
    remotes = _git("remote", "-v", check=False).stdout
    if "origin" not in remotes:
        _git("remote", "add", "origin", REPO_URL)

    _git("add", "-A")
    changed = _git("status", "--porcelain", check=False).stdout.strip()
    if not changed:
        logger.info("nothing changed since last publish — site already up to date")
        return
    _git("commit", "-m", f"data through {data_version}",
         "-m", f"generated_utc: {manifest['generated_utc']}",
         "-m", "TA+VSA visualizer static export (see BTC_True_Volume/export_static_site.py)")

    if no_push:
        logger.info("--no-push: committed locally, not pushed")
        return
    logger.info("pushing to %s (branch %s) ...", REPO_URL, BRANCH)
    res = _git("push", "-u", "origin", BRANCH, check=False)
    if res.returncode != 0:
        logger.error("push failed:\n%s\n%s\n\nIf the repo does not exist yet: "
                     "create the PUBLIC repo %s on github.com and enable Pages "
                     "(Settings -> Pages -> Deploy from a branch -> %s), then "
                     "re-run this script. First push may also open a one-time "
                     "GitHub login window (Git Credential Manager).",
                     res.stdout, res.stderr, REPO_URL, BRANCH)
        raise SystemExit(1)
    logger.info("pushed. Pages redeploys in ~1-2 min — "
                "https://noareyenoar.github.io/BTC_True_Volume/")


if __name__ == "__main__":
    publish(no_push="--no-push" in sys.argv)
