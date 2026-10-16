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

Branch `main` has TWO publishers — this PC and the daily cloud-refresh Actions
workflow — so every run fetches `origin/main` first and, when this checkout is
behind or has diverged, adopts the remote tip and re-exports on top of it (see
_sync_with_remote). A push that starts from a stale tip is rejected as
non-fast-forward; that is what failed the 2026-10-16 run after the refresh and
the export had both already succeeded.
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
REMOTE_REF = f"refs/remotes/origin/{BRANCH}"                 # the fetched tip of BRANCH
GIT_USER = "noareyenoar"
GIT_EMAIL = "noareyenoar@users.noreply.github.com"

sys.path.insert(0, str(BASE))
import export_static_site  # noqa: E402


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    logger.info("git %s", " ".join(args))
    return subprocess.run(["git", *args], cwd=SITE, check=check,
                          capture_output=True, text=True, encoding="utf-8")


def _rev(ref: str) -> str:
    """SHA of `ref`, or "" when it does not resolve (no commits yet, no such ref).

    `--verify` is load-bearing: plain `git rev-parse HEAD` in a repo with no
    commits yet prints the literal "HEAD" (it is a symref) instead of failing,
    and `--quiet` keeps a missing ref from printing a fatal: line for a case we
    handle by doing nothing.
    """
    res = _git("rev-parse", "--verify", "--quiet", ref, check=False)
    return res.stdout.strip() if res.returncode == 0 else ""


def _data_version(rev: str) -> str:
    """`data_version` from the manifest.json AT `rev` — `git show` reads it out of
    the object store, so nothing is checked out. "" if that commit has no
    manifest to read."""
    res = _git("show", f"{rev}:manifest.json", check=False)
    if res.returncode != 0:
        return ""
    try:
        return str(json.loads(res.stdout).get("data_version", ""))
    except ValueError:                       # truncated / not JSON
        return ""


def _sync_with_remote() -> None:
    """Fetch origin/<BRANCH> and ADOPT its tip when this checkout is behind it or
    has diverged from it.  Runs BEFORE build_site(), so the export is written on
    top of what the cloud published — never on top of a tip about to be thrown
    away.

    Why this exists (2026-10-16): `main` has two publishers — this PC and the
    daily cloud-refresh workflow — and the push used to run with no fetch first.
    A run that started from an older tip committed onto the stale parent, `git
    push` was rejected as non-fast-forward, and the batch printed FAILED even
    though the refresh and the export had both succeeded.  One fetch removes that
    failure mode.

    Why adopt instead of merging/rebase the artifacts: every file under site/ is
    regenerated from source + data on each run, and the .gz files carry a gzip
    timestamp, so a merge or rebase would conflict on essentially every artifact
    and could only ever resolve to "ours".  Taking the remote tip and rebuilding
    on it reaches the same tree without the conflict storm, and the cloud's
    commit stays in the history we build on — nothing it published is erased.

    Why not `push --force-with-lease`: the loser of the race would be decided by
    who pushed last, and the cloud may hold a day this PC has not refreshed yet
    (the version check below warns about exactly that case).

    Every exit here is best-effort: log and return, never raise.  Whatever is
    really wrong (network, credentials, no repo on GitHub yet) is reported by the
    push at the end, which is where the credential hint lives.
    """
    # HEAD has to be a branch: the commit and the push at the end both address
    # refs/heads/<BRANCH>, so from a detached HEAD they would commit into limbo
    # and still report success.  Re-attaching moves no files.
    if _git("symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip() != BRANCH:
        logger.warning("HEAD is not on branch %s — re-attaching it (the push at the "
                       "end publishes refs/heads/%s)", BRANCH, BRANCH)
        _git("symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")

    # Explicit refspec: the cloud checkout (actions/checkout, fetch-depth 1) pulls
    # branch main through a one-off refspec, so remote.origin.fetch cannot be
    # relied on there to create or update the remote-tracking ref.  "+" because a
    # remote-tracking ref must always follow the remote.
    res = _git("fetch", "origin", f"+refs/heads/{BRANCH}:{REMOTE_REF}", check=False)
    if res.returncode != 0:
        logger.warning("fetch failed — leaving the checkout alone and letting the push "
                       "below report the real error (a brand-new empty remote is "
                       "expected to fail here):\n%s", res.stderr.strip())
        return

    remote, head = _rev(REMOTE_REF), _rev("HEAD")
    if not remote:                           # nothing published yet
        logger.info("origin/%s does not exist yet — this run creates it", BRANCH)
        return
    if head == remote:
        # The cloud's own path: its checkout sits at origin/main with no local
        # commit yet, so this is a strict no-op there (and on any machine already
        # in sync).  Comparing SHAs also keeps a SHALLOW clone out of the ancestry
        # question entirely — merge-base/rev-list on a truncated history can
        # error out or answer from the wrong graph.
        logger.info("already at origin/%s (%s)", BRANCH, remote[:7])
        return

    # Strictly ahead: the push is a fast-forward, so leave the tree alone.
    # Adopting here would drop local work that a rebuild cannot reproduce — this
    # branch also carries files build_site() never writes, e.g.
    # .github/workflows/cloud-refresh.yml.
    if head and _git("merge-base", "--is-ancestor", REMOTE_REF, "HEAD",
                     check=False).returncode == 0:
        logger.info("origin/%s is already an ancestor of HEAD — pushing the local "
                    "commit(s) on top", BRANCH)
        return

    # Behind (plain fast-forward), diverged (both sides committed), or unborn
    # (fresh bootstrap: no commits here, the remote has some) — all three take
    # the same route: take the remote tip, then re-export on top of it.
    # `reset --hard` is right for all three: it creates the branch when HEAD is
    # unborn, it never refuses over untracked files (it deletes the ones standing
    # in the way of a tracked file — git-reset(1)), and it leaves ORIG_HEAD, so
    # the commit it replaces stays reachable through `git reflog`.
    # Deliberately not `git clean`: untracked files a rebuild does not own are
    # not ours to delete.
    dirty = _git("status", "--porcelain", check=False).stdout.splitlines()
    if dirty:
        more = f" (+{len(dirty) - 10} more)" if len(dirty) > 10 else ""
        logger.warning("uncommitted paths are about to be replaced by the export%s:\n%s",
                       more, "\n".join(dirty[:10]))
    local_ver, remote_ver = _data_version("HEAD"), _data_version(REMOTE_REF)
    if local_ver and remote_ver and remote_ver > local_ver:
        # Rebuilding from this PC's data would REGRESS the published page to an
        # older Vision day until the next refresh.  The batch flow refreshes
        # first, so this is only reachable when publish runs on its own.
        # Advisory only — the next refresh+publish (or tomorrow's cloud run)
        # heals it either way.
        logger.warning("origin/%s carries data through %s but the last local export "
                       "was %s — run refresh_before_visualizer.py first, or this run "
                       "regresses the page until the next refresh",
                       BRANCH, remote_ver, local_ver)
    logger.warning("behind/diverged from origin/%s — adopting %s and re-exporting on "
                   "top of it", BRANCH, remote[:7])
    _git("reset", "--hard", REMOTE_REF)


def publish(no_push: bool = False) -> None:
    # site/ has to exist before the git calls now: the sync below must run BEFORE
    # the export (a reset after the export would throw the fresh export away), and
    # build_site() is what used to create this directory.
    SITE.mkdir(parents=True, exist_ok=True)

    # --- repo bootstrap (idempotent) — above the export, because the sync needs
    #     the repo and the remote, and the export has to land after the sync ---
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

    _sync_with_remote()          # adopt origin/<BRANCH> BEFORE exporting

    manifest = export_static_site.build_site()
    data_version = manifest["data_version"]

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
        _git("fetch", "origin", f"+refs/heads/{BRANCH}:{REMOTE_REF}", check=False)
        moved = _rev(REMOTE_REF)
        logger.error("push failed:\n%s\n%s\n\nIf the repo does not exist yet: "
                     "create the PUBLIC repo %s on github.com and enable Pages "
                     "(Settings -> Pages -> Deploy from a branch -> %s), then "
                     "re-run this script. First push may also open a one-time "
                     "GitHub login window (Git Credential Manager).\n"
                     "If origin/%s has moved to %s while this run was exporting "
                     "(another publisher, i.e. the cloud workflow, pushed mid-run), "
                     "re-run: the fetch at the top adopts that tip and re-exports "
                     "on top of it.",
                     res.stdout, res.stderr, REPO_URL, BRANCH,
                     BRANCH, moved[:7] or "(could not re-read it)")
        raise SystemExit(1)
    logger.info("pushed. Pages redeploys in ~1-2 min — "
                "https://noareyenoar.github.io/BTC_True_Volume/")


if __name__ == "__main__":
    publish(no_push="--no-push" in sys.argv)
