"""Propose coordinate-frame landmarks from a bone mesh, and scaffold a plan file.

    python -m autoimplants.landmarks --bone real_cases/<id>/bone.stl \\
        --mount-side +y --out real_cases/<id>/surgical_plan.json

``import_case`` needs three landmarks in the mesh's own coordinates. After a CT
conversion those coordinates are patient coordinates -- origin at the scanner
isocentre, axes nothing to do with the bone -- so the numbers cannot be guessed
and typing them by hand means reading them off a viewer one at a time. This
measures the two shaft landmarks instead, which are geometry and not judgement.

What it will not do
-------------------
It does not write a usable surgical plan. The template it emits has no screws and
no keepouts, so ``surgical_plan.validate_structure`` rejects it until a human
fills them in. That is deliberate: this repo designs an implant around a plan and
does not invent one, and a tool that scaffolded a plausible-looking plan would be
the easiest possible way to smuggle fabricated clinical data into a case.

Two judgements it cannot make, and says so
------------------------------------------
* **Which end is proximal.** Guessed from the flare -- a femur's condyles are
  bulkier than its trochanteric end -- and that heuristic is wrong for other
  bones and for a partial scan. ``--flip`` swaps it.
* **Which way the plate mounts.** Pure anatomy, not geometry: nothing in a mesh
  says which aspect the surgeon approaches. It has to be given with
  ``--mount-side``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# Fraction of the bone's length treated as diaphysis. The flared ends pull a
# centroid off the shaft axis, so the axis is fitted from the middle only.
DIAPHYSIS_SPAN = (0.25, 0.75)
N_AXIS_STATIONS = 25

# Where along the diaphysis the two shaft landmarks are placed. Far enough apart
# to define the axis stably (surgical_plan requires 50 mm), inside the straight
# part at both ends.
LANDMARK_SPAN = (0.15, 0.85)

AXIS_HINTS = {
    "+x": [1.0, 0.0, 0.0], "-x": [-1.0, 0.0, 0.0],
    "+y": [0.0, 1.0, 0.0], "-y": [0.0, -1.0, 0.0],
    "+z": [0.0, 0.0, 1.0], "-z": [0.0, 0.0, -1.0],
}


class LandmarkError(ValueError):
    """A mesh this tool cannot read a shaft axis from."""


def _principal_axis(vertices: np.ndarray) -> np.ndarray:
    """Long axis of the bone, from the principal component of its vertices."""
    centered = vertices - vertices.mean(axis=0)
    _, _, right = np.linalg.svd(centered, full_matrices=False)
    return right[0] / np.linalg.norm(right[0])


def shaft_axis(mesh, n_stations: int = N_AXIS_STATIONS):
    """Fit the diaphyseal axis. Returns ``(origin, direction, station_centroids)``.

    Fitted from cross-section centroids rather than the raw vertex cloud: a
    vertex-cloud PCA is pulled off-axis by whichever end carries more surface,
    which on a femur is the condyles.
    """
    vertices = np.asarray(mesh.vertices, dtype=float)
    if len(vertices) < 4:
        raise LandmarkError("mesh has too few vertices to fit an axis")

    coarse = _principal_axis(vertices)
    projection = vertices @ coarse
    lo, hi = float(projection.min()), float(projection.max())
    span = hi - lo
    if span <= 0:
        raise LandmarkError("mesh is degenerate along its principal axis")

    starts = lo + span * np.linspace(*DIAPHYSIS_SPAN, n_stations)
    slab = span * (DIAPHYSIS_SPAN[1] - DIAPHYSIS_SPAN[0]) / (2.0 * n_stations)

    centroids = []
    for s in starts:
        band = vertices[np.abs(projection - s) <= slab]
        if len(band) >= 3:
            centroids.append(band.mean(axis=0))
    if len(centroids) < 3:
        raise LandmarkError(
            "could not sample the diaphysis -- the mesh may be a fragment rather "
            "than a whole shaft"
        )

    centroids = np.array(centroids)
    origin = centroids.mean(axis=0)
    direction = _principal_axis(centroids)

    # Keep the fitted axis pointing the same way as the coarse one, so the
    # sign of the SVD result does not silently flip the bone end for end.
    if float(direction @ coarse) < 0:
        direction = -direction
    return origin, direction, centroids


def _distal_is_positive(mesh, origin, direction) -> bool:
    """Guess which end is distal from which end is bulkier.

    A femur's condyles carry more cross-section than its trochanteric end. This
    is a heuristic and it is stated as one; --flip exists because it is wrong for
    other bones and for partial scans.
    """
    vertices = np.asarray(mesh.vertices, dtype=float)
    s = (vertices - origin) @ direction
    cut = np.percentile(np.abs(s), 80)
    positive = vertices[s > cut]
    negative = vertices[s < -cut]
    if not len(positive) or not len(negative):
        return True

    def spread(points):
        return float(np.linalg.norm(points - points.mean(axis=0), axis=1).mean())

    return spread(positive) >= spread(negative)


def _surface_point(mesh, origin, direction, outward):
    """Where the surface sits, looking inward along ``outward`` at mid-shaft."""
    reach = float(np.linalg.norm(mesh.bounds[1] - mesh.bounds[0]))
    start = origin + outward * reach
    hits, _, _ = mesh.ray.intersects_location(
        ray_origins=np.array([start]), ray_directions=np.array([-outward])
    )
    if not len(hits):
        raise LandmarkError(
            "the mount-side direction does not meet the bone. Give --mount-side a "
            "direction that points from the shaft axis out through the aspect the "
            "plate mounts on"
        )
    # The first surface met travelling inward.
    along = (hits - start) @ (-outward)
    return hits[int(np.argmin(along))]


def propose(mesh, mount_side, flip: bool = False) -> dict:
    """Landmarks for ``coordinate_frame.landmarks``, plus what was assumed."""
    origin, direction, centroids = shaft_axis(mesh)

    if not _distal_is_positive(mesh, origin, direction):
        direction = -direction
    if flip:
        direction = -direction

    s = (np.asarray(mesh.vertices, dtype=float) - origin) @ direction
    lo, hi = float(s.min()), float(s.max())
    length = hi - lo

    proximal = origin + direction * (lo + length * LANDMARK_SPAN[0])
    distal = origin + direction * (lo + length * LANDMARK_SPAN[1])

    outward = np.asarray(mount_side, dtype=float)
    outward = outward - (outward @ direction) * direction
    norm = float(np.linalg.norm(outward))
    if norm < 1e-6:
        raise LandmarkError(
            "--mount-side is parallel to the shaft axis, so it says nothing about "
            "which way the plate faces"
        )
    outward /= norm
    mount = _surface_point(mesh, origin + direction * (lo + length * 0.5), direction, outward)

    return {
        "landmarks": {
            "proximal_shaft_mm": [round(float(c), 3) for c in proximal],
            "distal_shaft_mm": [round(float(c), 3) for c in distal],
            "mount_side_mm": [round(float(c), 3) for c in mount],
        },
        "measured": {
            "shaft_axis": [round(float(c), 6) for c in direction],
            "bone_length_mm": round(length, 2),
            "landmark_separation_mm": round(float(np.linalg.norm(distal - proximal)), 2),
            "axis_stations_used": int(len(centroids)),
        },
    }


def plan_template(proposal: dict, bone: str, case_id: str) -> dict:
    """A plan file with the frame filled in and every clinical field left empty.

    It is intentionally invalid: surgical_plan.validate_structure rejects an empty
    screw list, so this cannot be imported until a human supplies the planning
    data.
    """
    return {
        "case_id": case_id,
        "provenance": "TEMPLATE -- coordinate frame measured from the mesh by "
                      "autoimplants.landmarks. Every clinical field below is a "
                      "placeholder and must be filled in from real surgical "
                      "planning before this case can be imported.",
        "bone": bone,
        "side": "REQUIRED: left or right",
        "approach": "REQUIRED: the aspect the plate mounts on, e.g. lateral",
        "coordinate_frame": {
            "note": "Landmarks proposed from the mesh. Check them in a viewer: the "
                    "proximal/distal assignment is a flare heuristic and the mount "
                    "side was supplied on the command line, not measured.",
            **proposal,
        },
        "footprint_z_mm": ["REQUIRED: plate start", "REQUIRED: plate end"],
        "screws": [],
        "keepouts": [],
        "material": {"name": "REQUIRED", "density_g_cm3": "REQUIRED"},
        "thresholds": {"REQUIRED": "copy the limits this case is to be judged against"},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autoimplants.landmarks", description=__doc__)
    ap.add_argument("--bone", required=True, help="segmented bone mesh")
    ap.add_argument(
        "--mount-side",
        required=True,
        help="direction pointing out through the aspect the plate mounts on: "
             "one of +x/-x/+y/-y/+z/-z, or three comma-separated numbers",
    )
    ap.add_argument("--bone-name", default="femur")
    ap.add_argument("--case-id", default="UNNAMED-CASE")
    ap.add_argument("--flip", action="store_true", help="swap the proximal/distal guess")
    ap.add_argument("--out", help="write a plan template here (default: print only)")
    args = ap.parse_args(argv)

    import trimesh

    key = args.mount_side.strip().lower()
    if key in AXIS_HINTS:
        mount_side = AXIS_HINTS[key]
    else:
        try:
            mount_side = [float(v) for v in args.mount_side.split(",")]
            if len(mount_side) != 3:
                raise ValueError
        except ValueError:
            print(
                f"could not read --mount-side {args.mount_side!r}: expected one of "
                f"{sorted(AXIS_HINTS)} or three comma-separated numbers",
                file=sys.stderr,
            )
            return 1

    mesh = trimesh.load(str(args.bone), force="mesh")
    try:
        proposal = propose(mesh, mount_side, flip=args.flip)
    except LandmarkError as exc:
        print(f"could not propose landmarks: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(proposal, indent=2))
    print(
        "\nCheck these in a viewer before using them. The proximal/distal "
        "assignment is a flare heuristic -- pass --flip if it is the wrong way "
        "round -- and the mount side is whatever you passed on the command line.",
        file=sys.stderr,
    )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(plan_template(proposal, args.bone_name, args.case_id), indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote plan template to {out}", file=sys.stderr)
        print(
            "It is deliberately incomplete: screws, keepouts, material and "
            "thresholds are placeholders, and import_case will reject it until "
            "they carry real planning data.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
