"""One iteration of the loop: generate -> export -> validate -> report.

This is the command Devin runs to check its own work::

    python -m autoimplants.run                      # full: build + validate
    python -m autoimplants.run --validators stub    # no CAD toolchain needed
    python -m autoimplants.run --stl out/implant.stl --no-build

Exit code is 0 when the design passes and 1 when it fails, so the loop (and
Devin) can branch on ``$?`` without parsing anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import case_io
from .contracts import Report
from .params import check_params, default_params
from .validators import REGISTRY, run_all

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASE = REPO_ROOT / "inputs" / "case.json"

# Used only when inputs/case.json does not exist yet, so the stub path works
# from the very first minute of the hackathon.
_FALLBACK_CASE = {
    "case_id": "FALLBACK",
    "thresholds": {
        "max_stress_MPa": 350.0,
        "min_wall_mm": 2.5,
        "max_bone_gap_mm": 1.5,
        "min_bone_gap_mm": 0.1,
    },
}


def load_case(path: str | Path) -> dict:
    """Load the case and make it the active one.

    Registering it with case_io is what lets the generator -- whose signature is
    frozen and never sees the case -- resolve the right screw file for an
    imported real case instead of always reading inputs/.
    """
    p = Path(path)
    if not p.exists():
        print(f"[warn] {p} not found -- using built-in fallback case", file=sys.stderr)
        return case_io.set_active_case(dict(_FALLBACK_CASE))
    return case_io.set_active_case(case_io.load_case(p), p)


def load_params(path: str | None) -> dict:
    if not path:
        return default_params()
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build(params: dict, out_dir: Path) -> Path:
    """Generate and export the implant. Imported lazily: CadQuery is only needed here."""
    from .export import export_implant  # noqa: PLC0415
    from .generator import build_implant  # noqa: PLC0415

    solid = build_implant(params)
    return export_implant(solid, out_dir / "implant")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autoimplants.run", description=__doc__)
    ap.add_argument("--params", help="JSON file of params (default: built-in DEFAULT_PARAMS)")
    ap.add_argument("--case", default=str(DEFAULT_CASE), help="case JSON with thresholds")
    ap.add_argument(
        "--validators",
        default="geometry,stress",
        help=f"comma list from {sorted(REGISTRY)}, or 'all'",
    )
    ap.add_argument("--iteration", type=int, default=0)
    ap.add_argument("--out", default="out", help="working directory for artifacts")
    ap.add_argument("--stl", help="validate this existing STL instead of building one")
    ap.add_argument("--no-build", action="store_true", help="skip CAD generation entirely")
    ap.add_argument("--json", action="store_true", help="print report JSON, not the table")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    case = load_case(args.case)
    params = load_params(args.params)

    names = tuple(sorted(REGISTRY)) if args.validators == "all" else tuple(
        n.strip() for n in args.validators.split(",") if n.strip()
    )

    # Bad params are a failing report, not a crash -- Devin gets told what it broke.
    problems = check_params(params)
    if problems:
        report = Report.errored(
            "params_invalid", "; ".join(problems), iteration=args.iteration, params=params
        )
        print(report.summary())
        report.write(out_dir / "report.json")
        return 1

    implant_path = args.stl or str(out_dir / "implant.stl")
    if not (args.no_build or args.stl):
        try:
            implant_path = str(build(params, out_dir))
        except Exception as exc:
            # A generator that does not even run is the most common Devin failure.
            # Report it in the same shape as everything else so the loop is uniform.
            report = Report.errored(
                "generator_failed",
                f"build_implant() raised: {exc!r}",
                iteration=args.iteration,
                params=params,
            )
            print(report.summary())
            report.write(out_dir / "report.json")
            return 1

    report = run_all(implant_path, case, names=names, iteration=args.iteration, params=params)
    report.write(out_dir / "report.json")

    print(report.to_json() if args.json else report.summary())
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
