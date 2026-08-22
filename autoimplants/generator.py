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

import math
from pathlib import Path

import cadquery as cq
import numpy as np

from . import case_io
from .bone import max_surface_x

REPO_ROOT = Path(__file__).resolve().parent.parent

# Lanes across the plate width used to find the seating height. Matches the
# lane count the geometry validator measures the bone gap along, so the
# generator and its examiner are looking at the same geometry.
N_SEAT_LANES = 5

# Flip these to True as you implement the corresponding geometry below.
THICKNESS_PROFILE_IMPLEMENTED = False
RIBS_IMPLEMENTED = False
HOLE_SLOTS_IMPLEMENTED = False
CONTOUR_SPLINE_IMPLEMENTED = False


def _screws() -> list[dict]:
    """The LOCKED planning input, in full. Never hard-code these.

    Which planning file that is comes from the active case, not a constant, so a
    real imported case builds against its own screws (see autoimplants.case_io),
    and ``direction`` arrives unit-normalised.

    This used to return heights only. Everything else about a screw -- where it
    enters and which way it points -- was discarded, which meant the plate was
    only ever correct for planning data whose screws happened to sit on the y=0
    centreline and run along -X. The synthetic femur does exactly that, so the
    assumption was invisible until a real plan arrived with angled screws.
    """
    return case_io.load_screws()


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
    standing clear of the most protruding point of the bone so that it does not
    intersect the shaft.
    """
    _guard_unimplemented(params)

    length = float(params["length_mm"])
    width = float(params["width_mm"])
    thickness = float(params["thickness_mm"])
    hole_d = float(params["hole_diameter_mm"])
    fillet = float(params["fillet_mm"])
    clearance = float(params["mount_clearance_mm"])

    screws = _screws()
    entries = np.array([s["entry_mm"] for s in screws], dtype=float)

    z_center = 0.5 * (float(entries[:, 2].min()) + float(entries[:, 2].max()))
    z0, z1 = z_center - length / 2.0, z_center + length / 2.0

    # Follow the screws across the width, rather than assuming they sit on y=0.
    # A real shaft's mounting aspect wanders in y, and the plate has to be where
    # the screws are or none of the bores can clear.
    y_center = float(entries[:, 1].mean())
    y_span = float(entries[:, 1].max() - entries[:, 1].min())
    if y_span + hole_d > width + 1e-9:
        raise ValueError(
            f"screws span {y_span:.1f} mm across the plate width and each needs "
            f"{hole_d:.1f} mm of bore, so this plan needs at least "
            f"{y_span + hole_d:.1f} mm of width; params['width_mm'] is {width:.1f} mm. "
            f"Widen the plate or reject the plan -- do not truncate it, because a "
            f"screw the plate does not reach is a screw that fixes nothing."
        )

    # A flat plate has to clear the most protruding point of the bow, otherwise it
    # cuts into the shaft. This is exactly why it then gapes at both ends. The
    # clearance is held at the apex, so the gap only grows from there -- which is
    # the whole problem a contoured plate solves.
    #
    # Seating is measured along the lanes the plate actually covers, not the y=0
    # centreline alone: on irregular cortex the most protruding point under the
    # plate is often not on its midline, and seating to the midline would bury
    # the plate edge in bone.
    seat_lanes = np.linspace(y_center - width / 2.0, y_center + width / 2.0, N_SEAT_LANES)
    mount_x = max_surface_x(z0, z1, ys=seat_lanes) + clearance

    plate = (
        cq.Workplane("YZ")
        .workplane(offset=mount_x)
        .moveTo(y_center, z_center)
        .rect(width, length)
        .extrude(thickness)
    )

    # Round the four long corners. Edges parallel to X are the plan-view corners.
    if fillet > 0:
        plate = plate.edges("|X").fillet(fillet)

    # Screw bores, each along its own planned trajectory. The cutter starts well
    # outside the plate and is long enough to leave it again whatever the angle,
    # so an obliquely angled screw gets a bore that runs all the way through
    # instead of a hole drilled straight down the X axis it does not follow.
    plate_center = np.array([mount_x + thickness / 2.0, y_center, z_center])
    diagonal = math.sqrt(length**2 + width**2 + thickness**2)

    for screw, entry in zip(screws, entries):
        direction = np.array(screw["direction"], dtype=float)  # unit, via case_io
        reach = diagonal + float(np.linalg.norm(entry - plate_center))
        start = entry - direction * reach

        cutter = cq.Solid.makeCylinder(
            hole_d / 2.0,
            2.0 * reach,
            cq.Vector(*start),
            cq.Vector(*direction),
        )
        plate = plate.cut(cq.Workplane(obj=cutter))

    return plate


if __name__ == "__main__":
    from .params import default_params

    solid = build_implant(default_params())
    print("volume mm^3", round(solid.val().Volume(), 1))
