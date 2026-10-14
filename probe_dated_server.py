"""probe_dated_server.py — dump the live API's custom-pair bodies for the probe.

Writes probe_dated_server.json beside this file: one scenario per date pair, each
with the server's own payload (JSON-safe, via the exporter's _clean) and what the
BROWSER is expected to do with it. probe_dated_pair.js then runs app.js's
cbDatedAttempt / cbPairFromArchive over the same pair and diffs the two.

Run from the project root:
    .venv\\Scripts\\python.exe BTC_True_Volume\\probe_dated_server.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from export_static_site import _clean                        # noqa: E402
from visualizer import app                                   # noqa: E402

# The pairs are DERIVED, not hard-coded: both stores rotate with every refresh
# (the dated store replaces its recent anchors with the curves the publisher now
# serves -- the 30d/90d/180d/365d windows; the weekly archive appends a Sunday),
# so a literal pair written today is a decline-only pair in a month and the probe
# would silently stop exercising the dated path. Anchoring on the 30d preset's
# own endpoints keeps every scenario pointed at a live branch of the rule:
#   1. preset-exact            both ends stored -> dated, reproduces the 30d window
#   2. start 2 days off        -> dated with from_snapped (inside the 4-day tolerance)
#   3. start 5 days off        -> outside the dated tolerance, archive must answer
#   4. history (2020 pair)     -> archive answers (dated store has no 2020 anchor there)
#   5. 2022-12-31 -> now       -> dated across grid generations, regrouped at $1,000
#   6. now-2 -> now-1          -> both ends snap to the SAME dated curve -> refused
#   7. end +5 past dated max   -> dated answers from its newest curve, `missing` flags
#   8. end +1 past dated max   -> same, single-day flag (the drag-to-the-edge pick)
#   9. end +5 past archive max -> archive answers, `missing` flags (the user's case)
#  10. end +9 past archive max -> beyond every store's grace -> refused as before
#  11. end +9 past dated max   -> dated declines on `to` first, refused as before
# Expects are asserted: a rotation that turns a scenario into a different branch
# is a loud failure, not a quiet pass.
_D = timedelta(days=1)


def _preset_pair() -> tuple[date, date]:
    t30 = app._cb_diffs(app.COST_BASIS_WINDOWS["30d"])
    if t30 is None:
        raise SystemExit("preset diff store not built — run fetch_checkonchain_urpd.py")
    p_from = date.fromisoformat(str(t30["prev_as_of_date"].iloc[0])[:10])
    p_to = date.fromisoformat(str(t30["as_of_date"].iloc[0])[:10])
    return p_from, p_to


def main() -> int:
    p_from, p_to = _preset_pair()
    d_last = date.fromisoformat(app._archive_dates(app._cb_dated_curves())[-1])
    a_last = date.fromisoformat(app._archive_dates(app._cb_archive())[-1])
    # (from, to, expected kind, print the status strip, expected `missing`,
    #  expected as_of or None to skip that check)
    pairs = [
        (p_from.isoformat(), p_to.isoformat(), "dated-pair-custom", True,
         None, p_to.isoformat()),
        ((p_from + 2 * _D).isoformat(), p_to.isoformat(), "dated-pair-custom", False,
         None, p_to.isoformat()),
        ((p_from + 5 * _D).isoformat(), (p_to - 7 * _D).isoformat(), "archive-pair", True,
         None, None),
        ("2020-03-01", "2020-04-01", "archive-pair", False, None, None),
        ("2022-12-31", p_to.isoformat(), "dated-pair-custom", False,
         None, p_to.isoformat()),
        ((p_to - 2 * _D).isoformat(), (p_to - _D).isoformat(), "not-dated", True,
         None, None),
        (p_from.isoformat(), (d_last + 5 * _D).isoformat(), "dated-pair-custom", True,
         {"from": (d_last + _D).isoformat(), "to": (d_last + 5 * _D).isoformat()},
         d_last.isoformat()),
        (p_from.isoformat(), (d_last + _D).isoformat(), "dated-pair-custom", True,
         {"from": (d_last + _D).isoformat(), "to": (d_last + _D).isoformat()},
         d_last.isoformat()),
        ((a_last - 14 * _D).isoformat(), (a_last + 5 * _D).isoformat(), "archive-pair", True,
         {"from": (a_last + _D).isoformat(), "to": (a_last + 5 * _D).isoformat()},
         a_last.isoformat()),
        ((a_last - 14 * _D).isoformat(), (a_last + 9 * _D).isoformat(), "unavailable", True,
         None, None),
        (p_from.isoformat(), (d_last + 9 * _D).isoformat(), "unavailable", True,
         None, None),
    ]
    out, bad = [], []
    for fr, to, exp, strip, exp_missing, exp_as_of in pairs:
        body = app.cost_basis("BTCUSDT", app.CB_CUSTOM, fr, to)
        body = _clean(body)
        kind = body.get("estimate_kind") or "unavailable"
        ok = (kind != "dated-pair-custom") if exp == "not-dated" else (kind == exp)
        if not ok:
            bad.append(f"{fr}->{to}: expected {exp}, got {kind}")
        # `missing` rides on every custom-success body and is absent (None) on
        # refusals; exact comparison -- the page prints this pair in red
        if body.get("missing") != exp_missing:
            bad.append(f"{fr}->{to}: expected missing {exp_missing}, "
                       f"got {body.get('missing')}")
        if exp_as_of is not None and body.get("as_of") != exp_as_of:
            bad.append(f"{fr}->{to}: expected as_of {exp_as_of}, got {body.get('as_of')}")
        if exp == "unavailable" and not body.get("dated_reason"):
            bad.append(f"{fr}->{to}: refusal carries no dated_reason")
        # A pair the dated store CAN take must be reproduced exactly; where it
        # cannot, the archive's body is cbPairFromArchive's to reproduce --
        # either way the browser must land on this body, and probe_dated_pair.js
        # diffs whichever store answered against the payload shipped here.
        if exp == "dated-pair-custom":
            assert body["available"], f"{fr}->{to}: dated pair but unavailable"
        declined_ok = kind != "dated-pair-custom"
        out.append({"from": fr, "to": to, "kind": kind,
                    "declined_ok": declined_ok,
                    "strip": strip,
                    "payload": body if kind == "dated-pair-custom" else None,
                    "archive_payload": body if kind == "archive-pair" else None})
    path = BASE / "probe_dated_server.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {path.name}: "
          + ", ".join(f"{s['from']}->{s['to']}={s['kind']}" for s in out))
    for b in bad:
        print(f"[FAIL] {b}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
