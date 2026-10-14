"""
supervisor.py — overnight loop engine (plan step 13).

Drives run_pipeline phases to a terminal state unattended:
  * checkpoint state in BTC_True_Volume/pipeline_state.json (per-phase
    pending|running|completed|failed + attempts + timestamps), written
    atomically (tmp+rename) after every transition — a crash mid-run resumes
    exactly where it left off.
  * 3 attempts per phase with 30s -> 2min -> 5min backoff.
  * 60s heartbeat lines to run_pipeline.log so progress is visible the
    morning after.
  * --max-hours cap (default 8h) prevents infinite loops.
  * Terminal states: all COMPLETE -> success; a phase FAILED after its
    attempts -> continue the rest, final state PARTIAL (documented, never
    silently skipped). After terminal state: incremental final report, then
    optional `shutdown /s /t 120` (--no-shutdown disables; the real shutdown
    only fires when every phase reached a terminal state AND the final report
    exists).

Usage: python BTC_True_Volume\\supervisor.py [--auto] [--max-hours 8] [--no-shutdown]
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .run_pipeline import PHASE_ORDER, run_one
    from .common import (PROCESSED, PhaseResult, append_log, logger)
except ImportError:  # direct script execution
    from run_pipeline import PHASE_ORDER, run_one
    from common import (PROCESSED, PhaseResult, append_log, logger)

logger = logging.getLogger("tvp.supervisor")

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "pipeline_state.json"
LOG_PATH = BASE_DIR / "run_pipeline.log"
REPORT_WRITER = BASE_DIR / "write_report.py"

RETRY_BACKOFF_S = (30, 120, 300)          # 3 attempts
HEARTBEAT_S = 60
DEFAULT_MAX_HOURS = 8

TERMINAL = {"completed", "failed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _blank_state() -> Dict[str, Any]:
    return {p: {"status": "pending", "attempts": 0, "last_error": None,
                "started_at": None, "finished_at": None}
            for p in PHASE_ORDER}


def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            st.setdefault("created_at", _now())
            st.setdefault("updated_at", _now())
            st.setdefault("phases", _blank_state())
            return st
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("state file corrupt (%s) — starting fresh", exc)
    return {"created_at": _now(), "updated_at": _now(), "phases": _blank_state()}


def save_state(st: Dict[str, Any]) -> None:
    st["updated_at"] = _now()
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_PATH)   # atomic


def run_phase(phase: str, force_refetch: bool = False,
              resume_aggtrades: bool = False) -> PhaseResult:
    """Run one phase in-process; raise on failure."""
    return run_one(phase, force_refetch=force_refetch,
                   resume_aggtrades=resume_aggtrades)


def heartbeat(msg: str) -> None:
    line = f"[{_now()}] {msg}\n"
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line)
    logger.info("heartbeat: %s", msg)


def update_report() -> None:
    """Regenerate the incremental progress report after every phase."""
    try:
        subprocess.run([sys.executable, "-m", "BTC_True_Volume.write_report",
                        "--no-shutdown", "--incremental"],
                       cwd=str(BASE_DIR.parents[1]), check=False,
                       capture_output=True, timeout=300)
    except Exception as exc:  # noqa: BLE001
        heartbeat(f"report refresh failed (non-fatal): {exc}")


def issue_shutdown() -> bool:
    """Issue `shutdown /s /t 120` with a comment. Returns success."""
    if os.environ.get("TVP_NO_SHUTDOWN"):
        return False
    cmd = ["shutdown", "/s", "/t", "120",
           "/c", "True volume pipeline complete - report at processed/progress_report.md"]
    try:
        subprocess.run(cmd, check=False, timeout=30)
        heartbeat("shutdown issued (/s /t 120) — final flush window")
        return True
    except Exception as exc:  # noqa: BLE001
        heartbeat(f"shutdown FAILED: {exc}")
        return False


def auto_run(max_hours: float = DEFAULT_MAX_HOURS,
             no_shutdown: bool = False) -> int:
    if no_shutdown:
        os.environ["TVP_NO_SHUTDOWN"] = "1"
    st = load_state()
    phases = st["phases"]
    deadline = time.time() + max_hours * 3600
    heartbeat(f"supervisor start: created {st['created_at']}, "
              f"max_hours={max_hours}, no_shutdown={no_shutdown}")
    overall = "RUNNING"
    failed_any = False
    last_beat = 0.0

    while True:
        now = time.time()
        if now - last_beat >= HEARTBEAT_S:
            active = sum(1 for p in phases.values() if p["status"] == "running")
            done = sum(1 for p in phases.values() if p["status"] == "completed")
            heartbeat(f"{done}/{len(phases)} phases completed, {active} running, "
                      f"{sum(1 for p in phases.values() if p['status'] == 'failed')} failed")
            last_beat = now
        if now > deadline:
            overall = "TIMEOUT"
            heartbeat(f"supervisor exceeded max_hours={max_hours} — stopping, state saved")
            break

        # next phase in order that is not terminal
        nxt = None
        for p in PHASE_ORDER:
            if phases[p]["status"] not in TERMINAL:
                nxt = p
                break
        if nxt is None:
            overall = "PARTIAL" if failed_any else "COMPLETE"
            heartbeat(f"terminal state reached: {overall}")
            break

        ph = phases[nxt]
        ph["status"] = "running"
        ph["attempts"] += 1
        ph["started_at"] = ph.get("started_at") or _now()
        save_state(st)
        heartbeat(f"phase {nxt} attempt {ph['attempts']}/3 starting")
        try:
            res = run_phase(nxt)
            ph["status"] = "completed"
            ph["last_error"] = None
            ph["finished_at"] = _now()
            save_state(st)
            heartbeat(f"phase {nxt} COMPLETE ({res.status})")
        except Exception as exc:  # noqa: BLE001
            ph["last_error"] = str(exc)[:500]
            if ph["attempts"] >= len(RETRY_BACKOFF_S):
                ph["status"] = "failed"
                ph["finished_at"] = _now()
                failed_any = True
                save_state(st)
                heartbeat(f"phase {nxt} FAILED after {ph['attempts']} attempts: {exc}")
            else:
                sleep_s = RETRY_BACKOFF_S[ph["attempts"] - 1]
                save_state(st)
                heartbeat(f"phase {nxt} attempt {ph['attempts']} failed ({exc}); "
                          f"retry in {sleep_s}s")
                time.sleep(sleep_s)
                continue
        update_report()
        # brief pause between phases so the disk settles
        time.sleep(5)

    update_report()
    heartbeat(f"supervisor finished: {overall}")
    print(f"SUPERVISOR: {overall}")
    if overall in ("COMPLETE", "PARTIAL"):
        # plan step 15: shutdown only after report exists at terminal state
        if no_shutdown:
            heartbeat("--no-shutdown set — machine stays on")
        else:
            report = PROCESSED / "progress_report.md"
            if report.exists():
                issue_shutdown()
            else:
                heartbeat("final report missing — refusing to shutdown")
    return 0 if overall == "COMPLETE" else 1


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Overnight supervisor loop")
    ap.add_argument("--auto", action="store_true", help="run the loop (default)")
    ap.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS)
    ap.add_argument("--no-shutdown", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    sys.exit(auto_run(max_hours=args.max_hours, no_shutdown=args.no_shutdown))


if __name__ == "__main__":
    main()
