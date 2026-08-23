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
from .bone import surface_grid

REPO_ROOT = Path(__file__).resolve().parent.parent

# Lanes across the plate width used to find the seating height. Matches the
# lane count the geometry validator measures the bone gap along, so the
# generator and its examiner are looking at the same geometry.
N_SEAT_LANES = 5

# Cross-sections lofted along the shaft, and points sampled across the width in
# each one. These set how closely the bone-facing surface tracks the cortex.
# Chordal error between samples is what eats into the mount clearance: 180 mm
# over 40 spans on a ~910 mm bow radius is ~0.003 mm, and 16 mm over 12 spans on
# a ~13 mm shaft radius ~0.02 mm -- both well inside the 0.4 mm clearance.
N_CONTOUR_STATIONS = 41
N_SECTION_POINTS = 13

# Flip these to True as you implement the corresponding geometry below.
THICKNESS_PROFILE_IMPLEMENTED = False
RIBS_IMPLEMENTED = False
HOLE_SLOTS_IMPLEMENTED = False
CONTOUR_SPLINE_IMPLEMENTED = True


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


def _contour_offsets(contour_spline: list, s: np.ndarray) -> np.ndarray:
    """Extra stand-off in mm at each normalised station ``s``.

    ``contour_spline`` is ``[[s, offset_mm], ...]``: a correction layered on top
    of the bone-fitted contour, linearly interpolated between control points and
    held flat outside them. Empty -- the default -- means the plate follows the
    fitted cortex exactly, which is the case that matters; the handle exists so
    that asking for extra soft-tissue relief over one segment does not mean
    writing a second generator.
    """
    if not contour_spline:
        return np.zeros_like(s)
    pts = np.array(sorted([float(a), float(b)] for a, b in contour_spline), dtype=float)
    return np.interp(s, pts[:, 0], pts[:, 1])


def _fill_missing(xs: np.ndarray) -> np.ndarray:
    """Replace NaN bone samples (rays that missed) by interpolating along Z.

    A miss means that lane of the footprint has run off the cortex. Carrying the
    neighbouring readings across keeps the loft closed and leaves the verdict to
    the gap check, rather than crashing the build on a NaN vertex.
    """
    out = np.asarray(xs, dtype=float).copy()
    if not np.isfinite(out).any():
        raise ValueError(
            "bone surface could not be sampled anywhere under the plate footprint; "
            "the plan places the plate off the shaft"
        )
    idx = np.arange(out.shape[0], dtype=float)
    row_fallback = np.nanmax(np.where(np.isfinite(out), out, np.nan), axis=1)
    for j in range(out.shape[1]):
        col = out[:, j]
        ok = np.isfinite(col)
        if ok.all():
            continue
        if not ok.any():
            col = row_fallback
            ok = np.isfinite(col)
        out[:, j] = np.interp(idx, idx[ok], col[ok])
    return out


def build_implant(params: dict) -> cq.Workplane:
    """Build the implant solid from params. FROZEN SIGNATURE.

    Contoured plate: the bone-facing surface is a doubly curved sheet fitted to
    the lateral cortex -- bent along Z to follow the anterior bow and troughed
    across Y to follow the shaft's round section -- standing off it by
    ``mount_clearance_mm``. The outer face is that same sheet pushed out by the
    wall thickness, so the section is constant and the plate hugs the bone
    everywhere instead of only at the apex of the bow.
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

    # Seat the plate on the cortex itself instead of on the single most
    # protruding point of it. A flat plate has to clear the apex of the bow, and
    # that is precisely why it gapes at both ends; a surface fitted to the bone
    # holds the same small clearance the whole way along.
    #
    # The fit is two-dimensional. Following only the y=0 centreline would leave
    # the plate edges standing off a shaft that is round in section -- about
    # 1.5 mm at 6 mm off the midline on a 13 mm radius, the entire gap budget
    # spent on transverse curvature alone.
    ys = np.linspace(y_center - width / 2.0, y_center + width / 2.0, N_SECTION_POINTS)
    zs, _, bone_xs = surface_grid(z0, z1, ys=ys, n=N_CONTOUR_STATIONS)
    bone_xs = _fill_missing(bone_xs)

    s = (zs - z0) / max(length, 1e-9)
    offsets = _contour_offsets(params.get("contour_spline") or [], s)
    inner_x = bone_xs + clearance + offsets[:, None]

    # One planar section per station: the bone-facing edge traced out along +Y,
    # the outer face traced back along -Y. Ruled between stations, so the solid
    # is exactly the sampled sheet and no spline overshoot between samples can
    # push a face into the cortex.
    sections = []
    for k in range(len(zs)):
        z = float(zs[k])
        pts = [
            cq.Vector(float(inner_x[k, j]), float(ys[j]), z) for j in range(len(ys))
        ]
        pts += [
            cq.Vector(float(inner_x[k, j]) + thickness, float(ys[j]), z)
            for j in reversed(range(len(ys)))
        ]
        sections.append(cq.Wire.makePolygon(pts, close=True))

    plate = cq.Workplane(obj=cq.Solid.makeLoft(sections, ruled=True))

    # Break the two long edges. On a curved solid they run along the shaft, so
    # they are picked by direction rather than by the flat plate's "|X".
    if fillet > 0:
        try:
            plate = plate.edges("|Z").fillet(fillet)
        except Exception:  # a fillet is cosmetic; never lose the part over one
            pass

    mount_x = float(np.nanmax(inner_x))

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
