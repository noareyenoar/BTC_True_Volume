"""
run_pipeline.py — orchestrator for the true-volume proxy pipeline.

Usage (from the project root):
    .venv\\Scripts\\python.exe -m BTC_True_Volume.run_pipeline [--phases step0,phase1_btc,...] [--skip X] [--force-refetch] [--resume-aggtrades]

Each phase's run(force_refetch) returns a PhaseResult; a log entry is appended
only on success; an exception logs INCOMPLETE and exits non-zero (no silent
skip — the supervisor decides about retries). Phases are idempotent: step0
rolls from the disk cache, phase2 resumes from cached ZIPs.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from .common import (SYMBOLS, PhaseResult, append_log, logger)
except ImportError:  # direct script execution
    from common import (SYMBOLS, PhaseResult, append_log, logger)

logger = logging.getLogger("tvp.run_pipeline")

PHASE_ORDER = ["step0", "phase1_btc", "phase1_eth", "phase2", "phase3",
               "phase4", "phase5", "phase6", "phase7", "phase8",
               "final", "register", "verify"]

# phase id -> module name
PHASE_MODULES = {
    "step0": "exchange_true_volume",
    "phase1_btc": "phase1_btc_network_metrics",
    "phase1_eth": "phase1_eth_network_metrics",
    "phase2": "phase2_futures_positioning",
    "phase3": "phase3_options_verification",
    "phase4": "phase4_checkonchain_investigation",
    "phase5": "phase5_realized_cap",
    "phase6": "phase6_mm_research",
    "phase7": "phase7_reserves_forward_plan",
    "phase8": "phase8_full_node_cost_benefit",
    "final": "build_final",
    "register": "register_outputs",
    "verify": "verify_all",
}


def run_one(phase: str, force_refetch: bool = False, resume_aggtrades: bool = False,
            log_append: bool = True) -> PhaseResult:
    """Run a single phase and return its PhaseResult. Raises on failure."""
    if phase not in PHASE_MODULES:
        raise ValueError(f"unknown phase {phase!r}")
    mod = importlib.import_module(f"BTC_True_Volume.{PHASE_MODULES[phase]}")
    if phase == "step0":
        results = []
        for sym in SYMBOLS:
            r = mod.build_exchange_true_volume(sym, resume=resume_aggtrades,
                                               force=force_refetch)
            if log_append:
                append_log(r.phase, r.name, r.asset, r.status, r.output,
                           r.coverage, r.confidence, r.limitations)
            results.append(r)
        first = results[0]
        res = PhaseResult(
            phase="step0", name="Exchange true volume rollup (BTC+ETH)", asset="BOTH",
            status=first.status,
            output="; ".join(r.output for r in results),
            coverage="; ".join(r.coverage for r in results),
            confidence="; ".join(r.confidence for r in results),
            limitations=[l for r in results for l in r.limitations],
            outputs=[p for r in results for p in r.outputs])
    elif phase == "verify":
        report = mod.run()
        failed = {k: v for k, v in report.items() if v}
        res = PhaseResult(
            phase="verify", name="End-to-end verification", asset="BOTH",
            status="COMPLETE" if not failed else "INCOMPLETE",
            output=str(Path(__file__).resolve().parents[1] / "processed" / "verification_report.json"),
            coverage="; ".join(f"{k}: {'PASS' if not v else f'FAIL ({len(v)})'}"
                               for k, v in report.items()),
            confidence="N/A (verification)",
            limitations=[f"{k}: {v[:3]}" for k, v in failed.items()],
            outputs=[])
        if log_append:
            append_log(res.phase, res.name, res.asset, res.status, res.output,
                       res.coverage, res.confidence, res.limitations)
        if failed:
            raise RuntimeError(f"verify failed: {failed}")
    else:
        res = mod.run(force_refetch=force_refetch)
        if log_append:
            append_log(res.phase, res.name, res.asset, res.status, res.output,
                       res.coverage, res.confidence, res.limitations)
    logger.info("phase %s: %s", phase, res.status)
    return res


def run(phases: Optional[List[str]] = None, skip: Optional[List[str]] = None,
        force_refetch: bool = False, resume_aggtrades: bool = False,
        log_append: bool = True) -> List[PhaseResult]:
    phases = phases or list(PHASE_ORDER)
    skip = skip or []
    results: List[PhaseResult] = []
    for phase in phases:
        if phase in skip:
            logger.info("skipping %s", phase)
            continue
        res = run_one(phase, force_refetch, resume_aggtrades, log_append)
        results.append(res)
    return results


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="True-volume proxy pipeline")
    ap.add_argument("--phases", default=",".join(PHASE_ORDER),
                    help="comma-separated phase ids (default: all)")
    ap.add_argument("--skip", default="", help="comma-separated phases to skip")
    ap.add_argument("--force-refetch", action="store_true")
    ap.add_argument("--resume-aggtrades", action="store_true",
                    help="extend ETH aggTrades tape via Vision ZIPs")
    ap.add_argument("--no-log", action="store_true", help="do not append log entries")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    skip = [p.strip() for p in args.skip.split(",") if p.strip()]
    try:
        run(phases=phases, skip=skip, force_refetch=args.force_refetch,
            resume_aggtrades=args.resume_aggtrades, log_append=not args.no_log)
    except Exception as exc:  # noqa: BLE001
        logger.error("pipeline failed: %s", exc)
        sys.exit(1)
    print("\n=== PIPELINE DONE ===")


if __name__ == "__main__":
    main()
