"""Turn a real CT-derived bone mesh plus a surgical plan into a runnable case.

    python -m autoimplants.import_case \\
        --case real_cases/<case_id>/surgical_plan.json \\
        --bone real_cases/<case_id>/bone.stl

The importer is the whole boundary between "clinical data as it actually
arrives" and "the locked input files this repo designs against". It:

  1. runs the mesh quality gate (autoimplants.mesh_quality)
  2. computes the rigid transform into the repo frame from the plan's landmarks
     (autoimplants.surgical_plan) and applies it to mesh, screws and keepouts
  3. checks the transformed plan against the actual bone -- entries on the
     surface, trajectories through bone, footprint on the shaft
  4. writes bone.stl / case.json / screw_positions.json / keepout_zones.json in
     exactly the shape the existing loop already reads

Nothing is invented. If the plan does not say where the screws go, the import
fails; it does not pick positions. The synthetic demo in ``inputs/`` is
untouched and remains the default quick-start path.

Output goes to ``real_cases/<case_id>/generated/`` rather than ``inputs/``,
because ``harness/guard.py`` locks ``inputs/*`` as system-controlled: a case
imported into that directory would show up as a guard violation on the agent's
own diff. Run an imported case by pointing --case at the generated case.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from . import mesh_quality, surgical_plan
from .contracts import Report
from .surgical_plan import PlanError

REPO_ROOT = Path(__file__).resolve().parent.parent

# Defaults for envelope fields a plan does not state. Ranges are the same
# published bone-plate figures the synthetic case was built from; a plan that
# cares should say so explicitly.
DEFAULT_MAX_WIDTH_MM = 20.0
DEFAULT_MAX_STANDOFF_MM = 6.0
DEFAULT_THICKNESS_BOUNDS_MM = {"min": 2.5, "max": 4.5}

PROVENANCE = (
    "REAL CT-DERIVED -- bone geometry segmented from patient imaging; surgical "
    "planning supplied externally and treated as a locked, pre-solved input. "
    "Imported by autoimplants.import_case. Verify the source data was "
    "de-identified before it entered this repo. Research/demo tooling only: no "
    "clinical, FDA, ISO or ASTM claim."
)


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def build_case_json(plan: dict, out_dir: Path) -> dict:
    """The case.json the loop reads, assembled from the plan.

    Paths are written relative to the case file so the generated directory is
    self-contained and can be moved or archived whole.
    """
    z0, z1 = (float(v) for v in plan["footprint_z_mm"])
    envelope = dict(plan.get("envelope") or {})
    envelope.setdefault("footprint_z_mm", [z0, z1])
    envelope.setdefault("aspect", f"{plan['approach']} {plan['bone']} shaft")
    envelope.setdefault("max_length_mm", round(z1 - z0, 3))
    envelope.setdefault("max_width_mm", DEFAULT_MAX_WIDTH_MM)
    envelope.setdefault("max_standoff_mm", DEFAULT_MAX_STANDOFF_MM)
    envelope.setdefault("thickness_bounds_mm", dict(DEFAULT_THICKNESS_BOUNDS_MM))

    thresholds = dict(plan["thresholds"])
    thresholds.setdefault("require_all_screws", len(plan["screws"]))

    case = {
        "case_id": plan["case_id"],
        "provenance": PROVENANCE,
        "source": {
            "surgical_plan": plan.get("_plan_path"),
            "bone": plan["bone"],
            "side": plan["side"],
            "approach": plan["approach"],
        },
        "inputs": {
            "bone_mesh": "bone.stl",
            "screw_positions": "screw_positions.json",
            "keepout_zones": "keepout_zones.json",
        },
        "material": dict(plan["material"]),
        "envelope": envelope,
        "thresholds": thresholds,
    }

    if plan.get("load_cases"):
        case["load_cases"] = plan["load_cases"]
    if plan.get("load_notes"):
        case["load_notes"] = plan["load_notes"]
    if plan.get("iteration_budget"):
        case["iteration_budget"] = plan["iteration_budget"]

    case["threshold_notes"] = {
        "unenforced": "max_stress_MPa and min_screw_pullout_N are read only to "
                      "populate limits -- every stress check returns SKIP until "
                      "validators/stress.py is real.",
        "frame": "All coordinates are in the repo frame (+Z along the shaft from "
                 "the proximal landmark, +X the mount aspect). The rigid transform "
                 "from the plan's original frame is recorded in frame_transform.json.",
    }
    return case


def build_screw_positions(plan: dict) -> dict:
    return {
        "provenance": PROVENANCE,
        "note": "Surgical planning is a declared PRE-SOLVED input for this project. "
                "These positions are locked: the implant must accommodate them, not "
                "move them. Coordinates are in the repo frame.",
        "bone_mesh": "bone.stl",
        "screws": [
            {
                "id": s["id"],
                "index": i,
                "entry_mm": [round(float(c), 4) for c in s["entry_mm"]],
                "direction": [round(float(c), 6) for c in s["direction"]],
                "diameter_mm": float(s["diameter_mm"]),
                "length_mm": float(s["length_mm"]),
            }
            for i, s in enumerate(plan["screws"])
        ],
    }


def build_keepout_zones(plan: dict) -> dict:
    return {
        "provenance": PROVENANCE,
        "zones": [
            {
                "id": z["id"],
                "type": "sphere",
                "center_mm": [round(float(c), 4) for c in z["center_mm"]],
                "radius_mm": float(z["radius_mm"]),
                "rationale": z.get("rationale", ""),
            }
            for z in plan["keepouts"]
        ],
    }


def import_case(
    plan_path: str | Path,
    bone_path: str | Path,
    out_dir: str | Path | None = None,
    units: str = "mm",
    max_faces: int = mesh_quality.MAX_FACES,
) -> tuple[Report, Path | None]:
    """Run the full import. Returns the merged report and the case.json path.

    The report is a normal ``Report``, so a failed import reads exactly like a
    failed validation -- same table, same check ids, same JSON.
    """
    plan = surgical_plan.load_plan(plan_path)

    gate = mesh_quality.gate(bone_path, bone=plan["bone"], units=units, max_faces=max_faces)
    if not gate.passed:
        return gate.report, None

    transform = surgical_plan.frame_transform(plan)
    mesh = gate.mesh.copy()
    mesh.apply_transform(transform)

    placed = surgical_plan.transformed_plan(plan, transform)
    plan_report = surgical_plan.validate_against_bone(placed, mesh)

    merged = Report.merge([gate.report, plan_report])
    merged.meta["case_id"] = plan["case_id"]
    if not merged.passed:
        return merged, None

    out = Path(out_dir) if out_dir else REPO_ROOT / "real_cases" / plan["case_id"] / "generated"
    out.mkdir(parents=True, exist_ok=True)

    mesh.export(str(out / "bone.stl"))
    case_path = _write_json(out / "case.json", build_case_json(placed, out))
    _write_json(out / "screw_positions.json", build_screw_positions(placed))
    _write_json(out / "keepout_zones.json", build_keepout_zones(placed))
    _write_json(
        out / "frame_transform.json",
        {
            "note": "Rigid transform applied to the source mesh, screws and keepouts "
                    "to bring them into the repo frame. Row-major 4x4, mm.",
            "source_mesh": str(Path(bone_path).resolve()),
            "source_plan": plan.get("_plan_path"),
            "matrix": [[round(float(v), 9) for v in row] for row in transform],
        },
    )
    merged.write(out / "import_report.json")

    return merged, case_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autoimplants.import_case", description=__doc__)
    ap.add_argument("--case", required=True, help="surgical_plan.json for the real case")
    ap.add_argument("--bone", required=True, help="segmented bone mesh (STL/PLY/OBJ)")
    ap.add_argument("--out", help="output directory (default real_cases/<case_id>/generated)")
    ap.add_argument(
        "--units",
        default="mm",
        choices=sorted(mesh_quality.UNIT_SCALE_MM),
        help="units the bone mesh is written in (default mm)",
    )
    ap.add_argument("--max-faces", type=int, default=mesh_quality.MAX_FACES)
    args = ap.parse_args(argv)

    try:
        report, case_path = import_case(
            args.case, args.bone, args.out, units=args.units, max_faces=args.max_faces
        )
    except PlanError as exc:
        # A rejected plan is the expected outcome for incomplete planning data,
        # not a crash. Print it the way every other failure in this repo prints.
        print(f"surgical plan rejected: {exc}", file=sys.stderr)
        return 1

    print(report.summary())

    if case_path is None:
        print("\nimport failed -- no case was written", file=sys.stderr)
        return 1

    if any(part == "inputs" for part in case_path.resolve().parts):
        print(
            "\n[warn] this case was written inside inputs/, which harness/guard.py "
            "locks as system-controlled. Committing it will read as a guard violation.",
            file=sys.stderr,
        )

    rel = case_path.relative_to(REPO_ROOT) if case_path.is_relative_to(REPO_ROOT) else case_path
    print(f"\nimported case written to {rel.parent}")
    print("run it with:")
    print(f"  python -m autoimplants.run --case {rel.as_posix()} --validators geometry,stress")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
