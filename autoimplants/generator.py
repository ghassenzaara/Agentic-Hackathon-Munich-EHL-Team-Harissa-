"""THE FILE DEVIN EDITS.

Everything else in this repo is scaffolding around this one function::

    build_implant(params: dict) -> cadquery.Workplane

What it currently builds is the GENERIC OFF-THE-SHELF PART: a flat, straight,
constant-thickness plate. That is the starting point of the demo, and it fails on
this patient -- the femur has a 22 mm anterior bow, so a flat plate stands ~6 mm
off the shaft at mid-span against a 1.5 mm limit.

Coordinate frame (matches inputs/bone.stl):
    +Z  along the femoral shaft, proximal to distal
    +X  lateral -- the aspect the plate mounts on, and the plate thickness direction
    +Y  the plate width direction

The four topology handles in params (thickness_profile, ribs, hole_slots,
contour_spline) are declared but NOT implemented. They raise NotImplementedError
if set. That is deliberate: setting a parameter must not be enough. To use them
you have to write the geometry, which is the whole point of the project -- a
parameter sweep is an optimiser, editing this file is engineering.
"""

from __future__ import annotations

import json
from pathlib import Path

import cadquery as cq

from .bone import max_surface_x

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREWS_JSON = REPO_ROOT / "inputs" / "screw_positions.json"

# Flip these to True as you implement the corresponding geometry below.
THICKNESS_PROFILE_IMPLEMENTED = False
RIBS_IMPLEMENTED = False
HOLE_SLOTS_IMPLEMENTED = False
CONTOUR_SPLINE_IMPLEMENTED = False


def _screw_z() -> list[float]:
    """Screw heights, read from the LOCKED planning input. Never hard-code these."""
    data = json.loads(SCREWS_JSON.read_text(encoding="utf-8"))
    return [s["entry_mm"][2] for s in data["screws"]]


def _guard_unimplemented(params: dict) -> None:
    """Refuse to silently ignore a topology parameter that has no geometry behind it.

    Without this, setting ``ribs=[...]`` would produce an unchanged plate and an
    unchanged validator report, and the loop would spin without learning anything.
    Failing loudly turns "I set a parameter" into "I must write the code".
    """
    unimplemented = [
        ("thickness_profile", THICKNESS_PROFILE_IMPLEMENTED,
         "Vary wall thickness along the plate. Sweep or loft a profile whose "
         "thickness follows the [[s, t], ...] control points instead of extruding "
         "a constant-thickness rectangle."),
        ("ribs", RIBS_IMPLEMENTED,
         "Add stiffening ribs. Union a raised boss onto the outer (+X) face for "
         "each {s, length_mm, height_mm, width_mm} entry. Local material adds "
         "section modulus where the bending stress peaks, at a fraction of the "
         "mass of thickening the whole plate."),
        ("hole_slots", HOLE_SLOTS_IMPLEMENTED,
         "Convert the listed screw holes from round holes to axial slots, so the "
         "hole stops acting as a fixed stress riser. Cut a slot instead of a "
         "cylinder for those indices."),
        ("contour_spline", CONTOUR_SPLINE_IMPLEMENTED,
         "Bend the plate to follow the bone. Sweep the cross-section along a "
         "spline fitted to the lateral surface (see autoimplants.bone."
         "surface_profile) rather than extruding along a straight line."),
    ]

    for name, implemented, how in unimplemented:
        if params.get(name) and not implemented:
            raise NotImplementedError(
                f"params[{name!r}] is set but build_implant() does not implement it yet. "
                f"Setting the parameter alone changes nothing about the geometry. "
                f"To use it, write the code in autoimplants/generator.py and flip "
                f"{name.upper()}_IMPLEMENTED to True. How: {how}"
            )


def build_implant(params: dict) -> cq.Workplane:
    """Build the implant solid from params. FROZEN SIGNATURE.

    Baseline: flat straight plate, constant thickness, six round screw holes,
    mounted tangent to the most protruding point of the bone so that it does not
    intersect the shaft.
    """
    _guard_unimplemented(params)

    length = float(params["length_mm"])
    width = float(params["width_mm"])
    thickness = float(params["thickness_mm"])
    hole_d = float(params["hole_diameter_mm"])
    fillet = float(params["fillet_mm"])

    screw_z = _screw_z()
    z_center = 0.5 * (min(screw_z) + max(screw_z))
    z0, z1 = z_center - length / 2.0, z_center + length / 2.0

    # A flat plate has to clear the most protruding point of the bow, otherwise it
    # cuts into the shaft. This is exactly why it then gapes at both ends.
    mount_x = max_surface_x(z0, z1)

    plate = (
        cq.Workplane("YZ")
        .workplane(offset=mount_x)
        .moveTo(0.0, z_center)
        .rect(width, length)
        .extrude(thickness)
    )

    # Round the four long corners. Edges parallel to X are the plan-view corners.
    if fillet > 0:
        plate = plate.edges("|X").fillet(fillet)

    # Screw holes: straight through the plate along -X at y = 0.
    for z in screw_z:
        cutter = cq.Solid.makeCylinder(
            hole_d / 2.0,
            thickness + 4.0,
            cq.Vector(mount_x - 2.0, 0.0, z),
            cq.Vector(1.0, 0.0, 0.0),
        )
        plate = plate.cut(cq.Workplane(obj=cutter))

    return plate


if __name__ == "__main__":
    from .params import default_params

    solid = build_implant(default_params())
    print("volume mm^3", round(solid.val().Volume(), 1))
