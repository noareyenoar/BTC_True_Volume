"""
phase6_mm_research.py — Phase 6: market-maker research linkage.

The existing Market_Maker_datasets/mm_proxy_criteria_report.md (2026-07-25)
documents proxy criteria for identifying market-maker-like accounts from the
local trade data. Phase 6 links that report and logs it — research linkage
only, no new computation.

Guardrail: no proxy criterion proves market-maker status — it only identifies
likely-large-trader behavior. Documented as the phase's known limitation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

try:
    from .common import (MM_REPORT, PhaseResult, append_log, logger)
except ImportError:  # direct script execution
    from common import (MM_REPORT, PhaseResult, append_log, logger)

logger = logging.getLogger("tvp.phase6")


def run(force_refetch: bool = False) -> PhaseResult:
    if not MM_REPORT.exists():
        raise RuntimeError(f"phase6: MM report missing at {MM_REPORT}")
    size_kb = MM_REPORT.stat().st_size / 1024
    logger.info("phase6: linking %s (%.1f KB)", MM_REPORT, size_kb)
    return PhaseResult(
        phase="Phase 6", name="Market-maker research linkage", asset="BOTH",
        status="COMPLETE",
        output=str(MM_REPORT),
        coverage="existing report (2026-07-25), no new computation",
        confidence="N/A as volume evidence; research context only",
        limitations=[
            "no criterion proves market maker — only likely-large-trader behavior",
            "research linkage only; the report is not a volume source",
        ],
        outputs=[MM_REPORT])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    res = run()
    append_log(res.phase, res.name, res.asset, res.status, res.output,
               res.coverage, res.confidence, res.limitations)
    print("\n=== PHASE 6 DONE ===")


if __name__ == "__main__":
    main()
