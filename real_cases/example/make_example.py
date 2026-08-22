"""Generate the worked example of a real-style case, in an arbitrary scanner frame.

Real CT data cannot be committed to this repo -- it is large, and it is patient
data. So the example that exercises the import path is built from the synthetic
femur instead, deliberately rotated and translated out of the repo frame into a
plausible scanner frame.

That is the point: the mesh and the plan arrive nowhere near axis-aligned, the
way a real segmentation does, and ``autoimplants.import_case`` has to recover the
frame from the landmarks. Because the ground truth is known exactly -- it is
``inputs/`` before the transform -- the round trip is testable, which is what
``tests/test_import_case.py`` checks.

Run:  python real_cases/example/make_example.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import trimesh

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
INPUTS = REPO_ROOT / "inputs"
sys.path.insert(0, str(REPO_ROOT))

CASE_ID = "EXAMPLE-FEMUR-CT-001"
OBLIQUE_CASE_ID = "EXAMPLE-FEMUR-CT-001-OBLIQUE"

# The oblique variant: how far off the y=0 centreline each screw sits, in mm.
# A real surgeon does not place six screws in a perfect line down the middle of
# the lateral aspect, and a plan that does is the one plan the old generator
# happened to handle.
OBLIQUE_Y_OFFSETS_MM = [0.0, 4.0, -3.5, 3.0, -4.0, 1.5]

# An arbitrary rigid pose standing in for scanner coordinates: obliquely angled,
# origin far from the bone. Fixed numbers so the example is regenerable and
# diffable, like inputs/make_bone.py.
ROT_DEG = (19.0, -34.0, 71.0)
TRANSLATION_MM = (-118.5, 342.0, -87.25)

# Shaft centreline of the synthetic femur is (bow(s), 0, z); see inputs/make_bone.py.
# These are the landmarks a surgeon or planning tool would place, expressed in the
# repo frame before the pose is applied.
LANDMARK_PROXIMAL_REPO = [0.0, 0.0, 0.0]
LANDMARK_DISTAL_REPO = [0.0, 0.0, 400.0]
LANDMARK_MOUNT_REPO = [35.0, 0.0, 200.0]  # on the +X aspect at mid-shaft

PROVENANCE = (
    "SYNTHETIC STAND-IN FOR REAL DATA -- the synthetic femur from inputs/, rigidly "
    "transformed into an arbitrary scanner-like frame so the import path can be "
    "exercised and tested without committing patient imaging. Not patient data. "
    "No clinical or regulatory claim."
)


def scanner_pose() -> np.ndarray:
    """The fixed 4x4 pose this example is written in."""
    rx, ry, rz = np.radians(ROT_DEG)
    pose = trimesh.transformations.euler_matrix(rx, ry, rz, "sxyz")
    pose[:3, 3] = TRANSLATION_MM
    return pose


def _apply_points(points, pose):
    pts = np.atleast_2d(np.asarray(points, dtype=float))
    return pts @ pose[:3, :3].T + pose[:3, 3]


def _apply_dirs(dirs, pose):
    return np.atleast_2d(np.asarray(dirs, dtype=float)) @ pose[:3, :3].T


def oblique_screws(screws, pose):
    """The same screws, placed the way a real plan places them.

    Each entry is moved off the centreline and re-seated on the actual bone
    surface at that y, then aimed at the shaft axis instead of straight down -X.
    Both properties are what a surgeon's plan has and the synthetic one does not:
    screws converge on the medullary canal, they do not run parallel.

    Entries stay exactly on the surface, so the import checks still pass -- the
    only thing that changes is geometry the generator has to respect.
    """
    from autoimplants.bone import surface_x_at  # noqa: PLC0415

    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("make_bone", INPUTS / "make_bone.py")
    make_bone = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_bone)

    out = []
    for s, dy in zip(screws, OBLIQUE_Y_OFFSETS_MM):
        z = float(s["entry_mm"][2])
        y = float(dy)

        # Re-seat on the bone at this y. NaN would mean the ray missed the shaft.
        x = surface_x_at(z, y=y, path=INPUTS / "bone.stl")
        if not np.isfinite(x):
            raise RuntimeError(f"no bone surface at y={y}, z={z}")
        entry = np.array([x, y, z])

        # Aim at the shaft centreline, which on this bone is (bow(s), 0, z).
        axis_point = np.array([make_bone.bow(z / make_bone.LENGTH_MM), 0.0, z])
        direction = axis_point - entry
        direction /= np.linalg.norm(direction)

        out.append(
            {
                "id": s["id"],
                "entry_mm": _apply_points(entry, pose)[0].tolist(),
                "direction": _apply_dirs(direction, pose)[0].tolist(),
                "diameter_mm": s["diameter_mm"],
                "length_mm": s["length_mm"],
            }
        )
    return out


def main() -> int:
    pose = scanner_pose()

    mesh = trimesh.load(str(INPUTS / "bone.stl"), force="mesh")
    mesh.apply_transform(pose)
    mesh.export(str(HERE / "bone.stl"))

    case = json.loads((INPUTS / "case.json").read_text(encoding="utf-8"))
    screws = json.loads((INPUTS / "screw_positions.json").read_text(encoding="utf-8"))["screws"]
    zones = json.loads((INPUTS / "keepout_zones.json").read_text(encoding="utf-8"))["zones"]

    plan = {
        "case_id": CASE_ID,
        "provenance": PROVENANCE,
        "bone": "femur",
        "side": "right",
        "approach": "lateral",
        "coordinate_frame": {
            "note": "Landmarks are in the same frame as bone.stl. The importer "
                    "derives the repo frame from them: +Z proximal to distal, "
                    "origin at the proximal landmark, +X the mount aspect.",
            "landmarks": {
                "proximal_shaft_mm": _apply_points(LANDMARK_PROXIMAL_REPO, pose)[0].tolist(),
                "distal_shaft_mm": _apply_points(LANDMARK_DISTAL_REPO, pose)[0].tolist(),
                "mount_side_mm": _apply_points(LANDMARK_MOUNT_REPO, pose)[0].tolist(),
            },
        },
        "footprint_z_mm": case["envelope"]["footprint_z_mm"],
        "screws": [
            {
                "id": s["id"],
                "entry_mm": _apply_points(s["entry_mm"], pose)[0].tolist(),
                "direction": _apply_dirs(s["direction"], pose)[0].tolist(),
                "diameter_mm": s["diameter_mm"],
                "length_mm": s["length_mm"],
            }
            for s in screws
        ],
        "keepouts": [
            {
                "id": z["id"],
                "type": "sphere",
                "center_mm": _apply_points(z["center_mm"], pose)[0].tolist(),
                "radius_mm": z["radius_mm"],
                "rationale": z.get("rationale", ""),
            }
            for z in zones
        ],
        "material": case["material"],
        "envelope": {
            "max_width_mm": case["envelope"]["max_width_mm"],
            "max_standoff_mm": case["envelope"]["max_standoff_mm"],
            "thickness_bounds_mm": case["envelope"]["thickness_bounds_mm"],
        },
        "thresholds": case["thresholds"],
        "load_cases": case["load_cases"],
        "load_notes": "Copied from the synthetic case. A real case would carry the "
                      "patient's own load estimate.",
        "iteration_budget": case["iteration_budget"],
    }

    (HERE / "surgical_plan.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8"
    )

    # The oblique variant shares this bone; only the plan differs. It is the
    # regression case for the generator consuming a plan rather than assuming
    # one -- against the old generator half its bores came out blocked.
    oblique = dict(plan)
    oblique["case_id"] = OBLIQUE_CASE_ID
    oblique["provenance"] = PROVENANCE + (
        " Screws are angled toward the shaft axis and offset off the centreline, "
        "which is how a real plan places them."
    )
    oblique["screws"] = oblique_screws(screws, pose)
    (HERE / "surgical_plan_oblique.json").write_text(
        json.dumps(oblique, indent=2) + "\n", encoding="utf-8"
    )

    print(f"wrote {HERE / 'bone.stl'} ({len(mesh.faces)} faces)")
    print(f"wrote {HERE / 'surgical_plan.json'} ({len(plan['screws'])} screws)")
    print(f"wrote {HERE / 'surgical_plan_oblique.json'} ({len(oblique['screws'])} screws)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
