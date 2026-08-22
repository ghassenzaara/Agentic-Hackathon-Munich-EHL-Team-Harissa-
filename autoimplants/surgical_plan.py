"""The schema for externally supplied surgical planning data, and its checks.

Surgical planning -- segmentation, screw placement, keepout definition, load
estimation -- is a declared PRE-SOLVED input for this project. That decision is
what makes the scope honest: this repo designs an implant around a plan, it does
not decide where to put screws in a patient. So a real case arrives as a mesh
plus a plan, and this module's job is to refuse a plan it cannot design against
rather than to invent the missing half.

Nothing here guesses. A missing landmark, an unnormalisable screw direction or a
footprint that runs off the end of the shaft is an error with a message naming
the field, never a default that quietly produces a plausible-looking implant.

Coordinate frames
-----------------
A plan is written in whatever frame the planning software used -- usually CT
scanner coordinates, with the origin at the scanner isocentre and no relation to
the bone. Everything downstream of here assumes the repo frame:

    +Z  along the shaft, proximal to distal, origin at the proximal landmark
    +X  the aspect the plate mounts on
    +Y  the plate width direction

:func:`frame_transform` builds the rigid transform between the two from the
landmarks in the plan. The mesh, the screws and the keepouts all go through the
same transform, so their relative geometry is untouched -- this is a change of
description, not of anatomy.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .contracts import FAIL, PASS, Check, Report

REQUIRED_FIELDS = (
    "case_id",
    "bone",
    "side",
    "approach",
    "coordinate_frame",
    "footprint_z_mm",
    "screws",
    "keepouts",
    "material",
    "thresholds",
)

REQUIRED_SCREW_FIELDS = ("id", "entry_mm", "direction", "diameter_mm", "length_mm")

REQUIRED_MATERIAL_FIELDS = ("name", "density_g_cm3")

# Landmark names for the fallback (landmark-based) frame definition.
LANDMARK_PROXIMAL = "proximal_shaft_mm"
LANDMARK_DISTAL = "distal_shaft_mm"
LANDMARK_LATERAL = "mount_side_mm"

# A screw entry is planning data, so it should sit on the bone surface. This is
# the slack allowed for segmentation and rounding, not for a misplaced screw.
ENTRY_TOLERANCE_MM = 2.5
# Below this the two shaft landmarks are too close to define an axis stably.
MIN_SHAFT_SPAN_MM = 50.0
# Below this the mount-side landmark is too near the shaft axis to say which way
# is out.
MIN_LATERAL_OFFSET_MM = 5.0


class PlanError(ValueError):
    """A surgical plan that cannot be designed against. Always names the field."""


def _as_float(value, field: str) -> float:
    """A number, or a PlanError naming the field.

    Plans are hand-written and half-written -- a placeholder string left in a
    numeric field is the normal state of a plan in progress, including the
    templates autoimplants.landmarks emits. Letting float() raise turns that into
    an unhelpful traceback instead of the message that says which field to fix.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        raise PlanError(f"{field} must be a number in mm, got {value!r}") from None


def _as_vector(value, field: str) -> np.ndarray:
    """Three finite numbers, or a PlanError naming the field."""
    try:
        vector = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        raise PlanError(f"{field} must be three numbers in mm, got {value!r}") from None
    if vector.shape != (3,) or not np.isfinite(vector).all():
        raise PlanError(f"{field} must be three finite numbers in mm, got {value!r}")
    return vector


# -- loading and structural validation ----------------------------------------


def load_plan(path: str | Path) -> dict:
    """Read and structurally validate a surgical plan. Raises PlanError."""
    p = Path(path)
    if not p.exists():
        raise PlanError(f"surgical plan not found: {p}")
    try:
        plan = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlanError(f"surgical plan {p} is not valid JSON: {exc}") from exc

    validate_structure(plan)
    plan["_plan_path"] = str(p.resolve())
    return plan


def validate_structure(plan: dict) -> None:
    """Every required field present and the right shape. Raises PlanError."""
    missing = [f for f in REQUIRED_FIELDS if f not in plan]
    if missing:
        raise PlanError(
            "surgical plan is missing required field(s): "
            + ", ".join(missing)
            + ". Surgical planning is a pre-solved input -- this repo will not "
              "invent clinical decisions to fill them in."
        )

    footprint = plan["footprint_z_mm"]
    if not (isinstance(footprint, (list, tuple)) and len(footprint) == 2):
        raise PlanError("footprint_z_mm must be [z_start, z_end] in mm")
    z_start = _as_float(footprint[0], "footprint_z_mm[0]")
    z_end = _as_float(footprint[1], "footprint_z_mm[1]")
    if z_end <= z_start:
        raise PlanError(f"footprint_z_mm must increase: got {z_start} to {z_end}")

    if not plan["screws"]:
        raise PlanError("surgical plan lists no screws; the implant has nothing to fix to")

    seen_ids = set()
    for i, s in enumerate(plan["screws"]):
        missing = [f for f in REQUIRED_SCREW_FIELDS if f not in s]
        if missing:
            raise PlanError(f"screws[{i}] is missing: {', '.join(missing)}")
        if s["id"] in seen_ids:
            raise PlanError(f"duplicate screw id {s['id']!r}")
        seen_ids.add(s["id"])

        _as_vector(s["entry_mm"], f"screws[{i}].entry_mm")

        d = _as_vector(s["direction"], f"screws[{i}].direction")
        if float(np.linalg.norm(d)) < 1e-6:
            raise PlanError(f"screws[{i}].direction is zero-length and has no trajectory")

        diameter = _as_float(s["diameter_mm"], f"screws[{i}].diameter_mm")
        length = _as_float(s["length_mm"], f"screws[{i}].length_mm")
        if diameter <= 0 or length <= 0:
            raise PlanError(f"screws[{i}] diameter_mm and length_mm must be positive mm")

    for i, z in enumerate(plan["keepouts"]):
        if z.get("type") != "sphere":
            raise PlanError(
                f"keepouts[{i}].type={z.get('type')!r}; only 'sphere' zones are "
                f"understood by the geometry validator today"
            )
        for f in ("id", "center_mm", "radius_mm"):
            if f not in z:
                raise PlanError(f"keepouts[{i}] is missing {f}")
        _as_vector(z["center_mm"], f"keepouts[{i}].center_mm")
        if _as_float(z["radius_mm"], f"keepouts[{i}].radius_mm") <= 0:
            raise PlanError(f"keepouts[{i}].radius_mm must be a positive length in mm")

    missing = [f for f in REQUIRED_MATERIAL_FIELDS if f not in plan["material"]]
    if missing:
        raise PlanError(f"material is missing: {', '.join(missing)}")

    validate_frame_fields(plan["coordinate_frame"])


def validate_frame_fields(frame: dict) -> None:
    """The frame must be stated, one way or the other. Raises PlanError."""
    if "axes" in frame:
        axes = frame["axes"]
        for f in ("shaft", "mount_side", "origin_mm"):
            if f not in axes:
                raise PlanError(f"coordinate_frame.axes is missing {f}")
        return

    if "landmarks" not in frame:
        raise PlanError(
            "coordinate_frame needs either 'landmarks' "
            f"({LANDMARK_PROXIMAL}, {LANDMARK_DISTAL}, {LANDMARK_LATERAL}) "
            "or explicit 'axes' (shaft, mount_side, origin_mm). Without one of "
            "them the mesh orientation is unknown and every measurement in this "
            "repo -- gap, standoff, wall thickness -- is measured along the wrong "
            "direction."
        )

    landmarks = frame["landmarks"]
    missing = [
        n for n in (LANDMARK_PROXIMAL, LANDMARK_DISTAL, LANDMARK_LATERAL)
        if n not in landmarks
    ]
    if missing:
        raise PlanError(f"coordinate_frame.landmarks is missing: {', '.join(missing)}")


# -- the frame transform ------------------------------------------------------


def frame_transform(plan: dict) -> np.ndarray:
    """4x4 rigid transform from the plan's own frame into the repo frame.

    Rejecting a mesh that is not already axis-aligned would make real CT support
    useless -- no scanner produces one. So the frame is computed and applied
    instead: rotation from the stated shaft and mount-side directions,
    translation putting the proximal landmark at the origin.
    """
    frame = plan["coordinate_frame"]

    if "axes" in frame:
        shaft = np.asarray(frame["axes"]["shaft"], dtype=float)
        lateral = np.asarray(frame["axes"]["mount_side"], dtype=float)
        origin = np.asarray(frame["axes"]["origin_mm"], dtype=float)
        span = float(np.linalg.norm(shaft))
        if span < 1e-6:
            raise PlanError("coordinate_frame.axes.shaft is zero-length")
    else:
        lm = frame["landmarks"]
        proximal = np.asarray(lm[LANDMARK_PROXIMAL], dtype=float)
        distal = np.asarray(lm[LANDMARK_DISTAL], dtype=float)
        mount = np.asarray(lm[LANDMARK_LATERAL], dtype=float)

        shaft = distal - proximal
        span = float(np.linalg.norm(shaft))
        if span < MIN_SHAFT_SPAN_MM:
            raise PlanError(
                f"{LANDMARK_PROXIMAL} and {LANDMARK_DISTAL} are {span:.1f} mm apart, "
                f"under the {MIN_SHAFT_SPAN_MM:.0f} mm needed to define the shaft axis "
                f"stably. Place them at opposite ends of the diaphysis."
            )
        lateral = mount - proximal
        origin = proximal

    z_hat = shaft / np.linalg.norm(shaft)

    # Only the component of the mount-side direction perpendicular to the shaft
    # carries information about which way is out.
    lateral_perp = lateral - np.dot(lateral, z_hat) * z_hat
    offset = float(np.linalg.norm(lateral_perp))
    if offset < MIN_LATERAL_OFFSET_MM:
        raise PlanError(
            f"the mount-side direction is only {offset:.2f} mm off the shaft axis, "
            f"under the {MIN_LATERAL_OFFSET_MM:.0f} mm minimum -- which way the plate "
            f"faces is ambiguous. Put {LANDMARK_LATERAL} on the aspect the plate "
            f"mounts on, well clear of the axis."
        )
    x_hat = lateral_perp / offset
    y_hat = np.cross(z_hat, x_hat)

    rotation = np.vstack([x_hat, y_hat, z_hat])  # rows: source -> repo components

    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = -rotation @ origin
    return transform


def transform_points(points: np.ndarray, transform: np.ndarray) -> np.ndarray:
    """Apply a 4x4 rigid transform to an (n, 3) array of positions."""
    pts = np.atleast_2d(np.asarray(points, dtype=float))
    return pts @ transform[:3, :3].T + transform[:3, 3]


def transform_directions(dirs: np.ndarray, transform: np.ndarray) -> np.ndarray:
    """Rotate directions -- no translation, and renormalised."""
    vecs = np.atleast_2d(np.asarray(dirs, dtype=float)) @ transform[:3, :3].T
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / norms


def transformed_plan(plan: dict, transform: np.ndarray) -> dict:
    """A copy of the plan with every position and direction in the repo frame."""
    out = dict(plan)

    screws = []
    for s in plan["screws"]:
        s = dict(s)
        s["entry_mm"] = transform_points(s["entry_mm"], transform)[0].tolist()
        s["direction"] = transform_directions(s["direction"], transform)[0].tolist()
        screws.append(s)
    out["screws"] = screws

    keepouts = []
    for z in plan["keepouts"]:
        z = dict(z)
        z["center_mm"] = transform_points(z["center_mm"], transform)[0].tolist()
        keepouts.append(z)
    out["keepouts"] = keepouts

    return out


# -- planning data against the actual bone ------------------------------------


def validate_against_bone(plan: dict, bone_mesh) -> Report:
    """Check the transformed plan describes screws and zones on *this* bone.

    A plan can be structurally perfect and still refer to a different scan. These
    checks are what catch a mismatched mesh/plan pair before the design loop
    spends eight iterations on geometry that was never seated on the bone.
    """
    checks: list[Check] = []
    entries = np.array([s["entry_mm"] for s in plan["screws"]], dtype=float)
    dirs = np.array([s["direction"] for s in plan["screws"]], dtype=float)

    # 1. Entries sit on the bone surface.
    _, dist, _ = bone_mesh.nearest.on_surface(entries)
    worst = int(np.argmax(dist))
    ok = bool(np.all(dist <= ENTRY_TOLERANCE_MM))
    checks.append(
        Check(
            id="plan_screw_entries_on_bone",
            status=PASS if ok else FAIL,
            value=round(float(dist[worst]), 3),
            limit=ENTRY_TOLERANCE_MM,
            unit="mm",
            location=[round(float(c), 3) for c in entries[worst]],
            message=(
                "every planned screw entry lies on the bone surface"
                if ok
                else f"screw {plan['screws'][worst]['id']!r} enters "
                     f"{float(dist[worst]):.1f} mm from the bone surface -- the plan and "
                     f"the mesh may come from different scans, or the landmarks are wrong"
            ),
        )
    )

    # 2. Each trajectory actually passes through bone, with enough purchase to hold.
    short = []
    worst_purchase = float("inf")
    worst_id = None
    for s, entry, d in zip(plan["screws"], entries, dirs):
        origin = entry - d * 1.0  # start just outside so the entry face is hit
        hits, _, _ = bone_mesh.ray.intersects_location(
            ray_origins=np.array([origin]), ray_directions=np.array([d])
        )
        purchase = 0.0
        if len(hits) >= 2:
            along = (hits - origin) @ d
            purchase = float(np.max(along) - np.min(along))
        elif len(hits) == 1:
            purchase = 0.0
        if purchase < worst_purchase:
            worst_purchase, worst_id = purchase, s["id"]
        if purchase <= 0.0:
            short.append(s["id"])

    checks.append(
        Check(
            id="plan_screw_trajectories_in_bone",
            status=PASS if not short else FAIL,
            value=round(worst_purchase if np.isfinite(worst_purchase) else 0.0, 3),
            limit=0.0,
            unit="mm",
            message=(
                f"every trajectory passes through bone; thinnest purchase is "
                f"{worst_purchase:.1f} mm at {worst_id!r}"
                if not short
                else f"screw(s) {', '.join(short)} do not pass through the bone along "
                     f"their stated direction -- a screw that misses the bone fixes nothing"
            ),
        )
    )

    # 3. Footprint lies on the shaft that exists in this mesh.
    z0, z1 = (float(v) for v in plan["footprint_z_mm"])
    bone_z0, bone_z1 = float(bone_mesh.bounds[0][2]), float(bone_mesh.bounds[1][2])
    inside = bone_z0 <= z0 and z1 <= bone_z1
    checks.append(
        Check(
            id="plan_footprint_within_bone",
            status=PASS if inside else FAIL,
            value=round(z1 - z0, 2),
            limit=round(bone_z1 - bone_z0, 2),
            unit="mm",
            location=[0.0, 0.0, round(z0, 2)],
            message=(
                f"plate footprint z={z0:.0f}..{z1:.0f} mm lies on the shaft "
                f"(bone spans {bone_z0:.0f}..{bone_z1:.0f} mm)"
                if inside
                else f"plate footprint z={z0:.0f}..{z1:.0f} mm runs off the bone, which "
                     f"spans {bone_z0:.0f}..{bone_z1:.0f} mm in the repo frame"
            ),
        )
    )

    # 4. Screws lie within the footprint they are supposed to fix through.
    zs = entries[:, 2]
    outside = [
        s["id"] for s, z in zip(plan["screws"], zs) if not (z0 - 1e-6 <= z <= z1 + 1e-6)
    ]
    checks.append(
        Check(
            id="plan_screws_within_footprint",
            status=PASS if not outside else FAIL,
            value=float(len(outside)),
            limit=0.0,
            unit="count",
            message=(
                "every screw falls inside the plate footprint"
                if not outside
                else f"screw(s) {', '.join(outside)} lie outside footprint_z_mm "
                     f"[{z0:.0f}, {z1:.0f}] -- the plate cannot reach them"
            ),
        )
    )

    return Report.from_checks(
        checks, meta={"validator": "surgical_plan", "case_id": plan.get("case_id")}
    )
