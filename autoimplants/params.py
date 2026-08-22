"""The parameter surface Devin is allowed to change.

Note the four topology handles (``thickness_profile``, ``ribs``, ``hole_slots``,
``contour_spline``). They are unused by the baseline plate on purpose. They exist
from the first commit because they are *how Devin changes topology* rather than
nudging floats -- and adding them later would mean renegotiating the contract
with three other people mid-hackathon.
"""

from __future__ import annotations

from copy import deepcopy

DEFAULT_PARAMS: dict = {
    # --- overall plate envelope ------------------------------------------------
    "length_mm": 180.0,
    "width_mm": 16.0,
    "thickness_mm": 3.0,

    # Gap held between the bone-facing surface and the bone. Deliberate, not slop:
    # a plate pressed onto bone crushes the periosteum and interrupts the blood
    # supply the fracture heals through, so thresholds.min_bone_gap_mm makes zero
    # clearance a failure. Mounting exactly tangent also puts the measured gap on a
    # knife edge, where its sign depends on ray-sample alignment.
    "mount_clearance_mm": 0.4,

    # --- topology handles (baseline ignores these; Devin reaches for them) -----
    # [[s, t], ...] with s in 0..1 along the plate length, t in mm
    "thickness_profile": [],
    # [{"s": 0.5, "length_mm": 30, "height_mm": 2.0, "width_mm": 4.0}, ...]
    "ribs": [],
    # indices of screw holes to convert from round hole to sliding slot
    "hole_slots": [],
    # [[s, offset_mm], ...] contour bend fitted to the bone surface
    "contour_spline": [],

    # --- screw interface ------------------------------------------------------
    "hole_diameter_mm": 4.5,
    "hole_countersink_mm": 0.0,

    # --- stress relief / cosmetics -------------------------------------------
    "fillet_mm": 1.0,
    "edge_chamfer_mm": 0.0,
}

# Keys the generator and validators rely on existing. If Devin deletes one we want
# a loud, specific error rather than a KeyError six frames deep.
REQUIRED_KEYS = tuple(DEFAULT_PARAMS.keys())


def default_params() -> dict:
    """A fresh deep copy -- never hand out the module-level dict."""
    return deepcopy(DEFAULT_PARAMS)


def check_params(params: dict) -> list[str]:
    """Return a list of human-readable problems. Empty list means usable."""
    problems = [f"missing required param: {k}" for k in REQUIRED_KEYS if k not in params]

    for key in ("length_mm", "width_mm", "thickness_mm", "hole_diameter_mm"):
        v = params.get(key)
        if v is not None and (not isinstance(v, (int, float)) or v <= 0):
            problems.append(f"{key} must be a positive number, got {v!r}")

    for key in ("thickness_profile", "ribs", "hole_slots", "contour_spline"):
        v = params.get(key)
        if v is not None and not isinstance(v, list):
            problems.append(f"{key} must be a list, got {type(v).__name__}")

    return problems
