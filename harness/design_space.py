"""Prove the claim the whole project rests on: no scalar tweak can pass.

``design_space_note`` in inputs/case.json argues this with arithmetic. This
module argues it with the actual validators: it sweeps the three scalar handles
a parameter optimiser would reach for -- thickness, width, length -- builds the
part for every combination, and runs the real geometry and stress checks on it.

Exit code 0 means the property holds: every scalar-only design fails at least one
check, so the only way out of the case is to write geometry. Exit code 1 means a
scalar design passed, the case has collapsed into a one-line optimisation, and
the thresholds or the stress calibration need looking at *before* the demo.

    python -m harness.design_space            # the standard grid
    python -m harness.design_space --quick    # thickness only, ~30 s
    python -m harness.design_space --json     # machine-readable rows

Run it after touching stress.py, case.json, or make_bone.py. It is the reason
"why is this not just an optimiser?" has an answer that is checked rather than
asserted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autoimplants.params import default_params
from autoimplants.run import build, load_case
from autoimplants.validators import run_all

REPO_ROOT = Path(__file__).resolve().parent.parent
CASE = REPO_ROOT / "inputs" / "case.json"

# The scalar moves available to an optimiser. Thickness is capped by the mass
# budget, width by the mid-span keepout, length by the envelope -- the grid runs
# past all three limits on purpose, so the failures are visible rather than
# assumed.
THICKNESS_MM = (2.5, 3.0, 3.16, 3.3, 3.6, 4.0, 4.5)
WIDTH_MM = (14.0, 16.0, 18.0, 20.0)
LENGTH_MM = (160.0, 170.0, 180.0)

QUICK_WIDTH = (16.0,)
QUICK_LENGTH = (180.0,)


def evaluate(thickness: float, width: float, length: float, case: dict, out_dir: Path) -> dict:
    params = default_params()
    params["thickness_mm"] = thickness
    params["width_mm"] = width
    params["length_mm"] = length

    try:
        stl = build(params, out_dir)
    except Exception as exc:  # a geometry that cannot even be built is not a pass
        return {
            "thickness_mm": thickness, "width_mm": width, "length_mm": length,
            "passed": False, "failing": ["build_failed"], "detail": repr(exc),
        }

    report = run_all(str(stl), case, names=("geometry", "stress"), params=params)
    worst_stress = max(
        (c.value for c in report.checks if c.unit == "MPa" and c.value is not None),
        default=None,
    )
    mass = report.by_id("implant_mass")
    return {
        "thickness_mm": thickness,
        "width_mm": width,
        "length_mm": length,
        "passed": report.passed,
        "mass_g": mass.value if mass else None,
        "worst_stress_MPa": worst_stress,
        "failing": [c.id for c in report.failures()],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="harness.design_space", description=__doc__)
    ap.add_argument("--quick", action="store_true", help="thickness sweep only")
    ap.add_argument("--json", action="store_true", help="print rows as JSON")
    ap.add_argument("--out", default="out/design_space", help="scratch directory")
    args = ap.parse_args(argv)

    case = load_case(CASE)
    out_dir = Path(args.out)
    widths = QUICK_WIDTH if args.quick else WIDTH_MM
    lengths = QUICK_LENGTH if args.quick else LENGTH_MM

    rows = [
        evaluate(t, w, length, case, out_dir)
        for length in lengths
        for w in widths
        for t in THICKNESS_MM
    ]

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{'t':>5} {'w':>5} {'L':>6} {'mass g':>7} {'worst MPa':>10}  failing")
        print("-" * 78)
        for r in rows:
            mass = "-" if r.get("mass_g") is None else f"{r['mass_g']:.1f}"
            stress = "-" if r.get("worst_stress_MPa") is None else f"{r['worst_stress_MPa']:.0f}"
            print(
                f"{r['thickness_mm']:>5.2f} {r['width_mm']:>5.1f} {r['length_mm']:>6.1f} "
                f"{mass:>7} {stress:>10}  {', '.join(r['failing']) or 'NONE -- PASSES'}"
            )

    # Under --json stdout has to stay parseable, so the verdict goes to stderr.
    summary = sys.stderr if args.json else sys.stdout
    passing = [r for r in rows if r["passed"]]
    print(file=summary)
    if passing:
        print(f"PROPERTY BROKEN: {len(passing)} scalar-only design(s) pass every check.", file=summary)
        for r in passing:
            print(f"  thickness={r['thickness_mm']} width={r['width_mm']} length={r['length_mm']}", file=summary)
        print("The case is now a scalar optimisation. Re-check the thresholds and the", file=summary)
        print("stress calibration before claiming this needs an engineer.", file=summary)
        return 1

    print(f"Property holds: all {len(rows)} scalar-only designs fail at least one check.", file=summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
