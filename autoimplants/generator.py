"""THE FILE DEVIN EDITS.

Everything else in this repo is scaffolding around this one function::

    build_implant(params: dict) -> cadquery.Workplane

What it builds is a CONTOURED plate: the bone-facing surface is the measured
cortex itself, offset outward by the mounting clearance at every station along
the shaft and at every lane across the width, so the part follows both the
longitudinal bow and the transverse curvature of the shaft.

The generic off-the-shelf part it replaced -- a flat, straight slab -- cannot
pass on this patient, and no scalar version of it can. A flat face has to clear
the most protruding point of the cortex under its whole footprint, so the gap it
leaves everywhere else is the sum of the 22 mm anterior bow's sagitta and the
transverse fall-off of a ~10 mm radius shaft: 8.60 mm measured at (y=6, z=100)
against a 1.5 mm limit. Thickening, widening and lengthening all leave that
term untouched -- they change the slab, not the fact that it is a slab.

Coordinate frame (matches inputs/bone.stl):
    +Z  along the femoral shaft, proximal to distal
    +X  lateral -- the aspect the plate mounts on, and the plate thickness direction
    +Y  the plate width direction

One of the four topology handles is now implemented: ``contour_spline``, which
lifts the seat radially along the length on top of the fitted cortex offset.
``thickness_profile``, ``ribs`` and ``hole_slots`` are still declared and still
raise, because setting a parameter must not be enough -- to use them you have to
write the geometry.
"""

from __future__ import annotations

import math
from pathlib import Path

import cadquery as cq
import numpy as np

from . import case_io
from .bone import surface_grid

REPO_ROOT = Path(__file__).resolve().parent.parent

# Cross-sections lofted along the shaft, and points sampled across the width
# within each one. 41 stations put a section every ~4.5 mm on a 180 mm plate;
# the bow's sagitta over that span is under 0.01 mm, so a ruled loft between
# neighbouring sections is smooth to far better than any threshold here.
N_STATIONS = 41
N_SECTION_POINTS = 41

# The bone is sampled on the same lanes the section is built from, so the seating
# surface is the measured cortex itself rather than a curve fitted through it.
N_SEAT_LANES = N_SECTION_POINTS

# Cortex samples taken per station and per lane the plate is *built* on, so the
# between-sample dilation in _seating_surface works at ~1 mm rather than ~4.5 mm.
SEAT_REFINE = 2

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


def _interp_profile(
    control: list, s: np.ndarray, default: float, what: str
) -> np.ndarray:
    """Piecewise-linear value along the plate, from [[s, value], ...] control points.

    ``s`` runs 0 at the proximal end of the footprint to 1 at the distal end --
    the same normalised coordinate the params docstring uses, so a profile stays
    meaningful if the plate length changes. An empty list means "constant
    ``default`` everywhere", which is what keeps a plain plate expressible.
    """
    if not control:
        return np.full(s.shape, float(default))

    pts = sorted((float(a), float(b)) for a, b in control)
    xs = np.array([p[0] for p in pts])
    vs = np.array([p[1] for p in pts])
    if xs.min() < -1e-9 or xs.max() > 1.0 + 1e-9:
        raise ValueError(
            f"{what} control positions must lie in 0..1 along the plate; got "
            f"{xs.min():.3f}..{xs.max():.3f}"
        )
    # Ends are held flat rather than extrapolated: extrapolating a spline past
    # its last control point is how a plate grows a negative thickness.
    return np.interp(s, xs, vs)


def _fill_missing(surface: np.ndarray) -> np.ndarray:
    """Replace rays that missed the bone with the nearest lane, then station, that hit.

    A footprint can overhang the end of a segmented mesh, and clinical imaging is
    routinely cropped, so a plate that raised an exception here would be unusable
    on exactly the cases this pipeline exists to handle.
    """
    filled = surface.copy()
    lanes = np.arange(filled.shape[1], dtype=float)
    for i, row in enumerate(filled):
        ok = np.isfinite(row)
        if ok.any() and not ok.all():
            filled[i] = np.interp(lanes, lanes[ok], row[ok])

    hit = np.flatnonzero(np.isfinite(filled).all(axis=1))
    if hit.size == 0:
        raise ValueError(
            "could not find the bone surface under any station of the plate "
            "footprint -- the plan places the plate off the segmented bone"
        )
    if hit.size < filled.shape[0]:
        stations = np.arange(filled.shape[0])
        nearest = hit[np.abs(stations[:, None] - hit[None, :]).argmin(axis=1)]
        filled = filled[nearest]
    return filled


def _seating_surface(zs: np.ndarray, lanes: np.ndarray) -> np.ndarray:
    """The measured cortex under the plate, as a ``(station, lane)`` grid of x.

    This *is* the bone-facing surface -- offset outward in the next step -- not a
    primitive fitted through it. Fitting a circle or a spline through the samples
    instead costs twice over: the residual it cannot follow shows up directly in
    ``bone_conformance_gap``, and then, to stay out of the bone, the fitted
    surface has to enclose that residual, spending standoff on fitting error
    rather than on wall. Offsetting the measurement removes both terms -- the gap
    is the clearance, by construction, whatever shape the bone turns out to be.

    The samples are dilated by one step in each direction first. Rays only see the
    surface where they are cast, so a bump *between* samples could otherwise poke
    through the seat; taking the local maximum makes the seat clear the
    neighbourhood a sample stands for rather than the single point it measured.
    The dilation runs on a grid refined ``SEAT_REFINE`` times in both directions,
    so the window it clears is ~1 mm along the shaft and ~0.1 mm across the width
    -- the scale of surface roughness, not the scale of the anatomy. Dilating at
    raw station spacing (~4.5 mm) would instead make every section clear the
    shaft's own taper, which is standoff spent on nothing.
    """
    fine_lanes = np.interp(
        np.linspace(0, len(lanes) - 1, (len(lanes) - 1) * SEAT_REFINE + 1),
        np.arange(len(lanes)),
        lanes,
    )
    fine_n = (len(zs) - 1) * SEAT_REFINE + 1
    _, _, surface = surface_grid(float(zs[0]), float(zs[-1]), ys=fine_lanes, n=fine_n)
    filled = _fill_missing(surface)

    padded = np.pad(filled, 1, mode="edge")
    dilated = np.maximum.reduce(
        [
            padded[i : i + filled.shape[0], j : j + filled.shape[1]]
            for i in range(3)
            for j in range(3)
        ]
    )
    return dilated[::SEAT_REFINE, ::SEAT_REFINE]


def _section_wire(
    z: float, ys: np.ndarray, inner: np.ndarray, outer: np.ndarray, bevel: float
) -> cq.Wire:
    """One cross-section: the offset cortex profile, walled outward along +X.

    The wall is built along X because that is how the validator measures it --
    ``check_min_wall`` reads X chords -- so ``outer - inner`` is exactly the
    thickness it will report. Walling radially instead would make the reported
    thickness a cosine of the local surface angle, thinnest at the plate edges,
    which is where the screw heads bear.

    ``bevel`` draws the outer profile in from the plate edge, breaking the sharp
    lateral corner. It replaces a CadQuery ``.fillet()`` on the lofted edges: on a
    free-form section the ``|X`` selector no longer picks out the four plan-view
    corners, so filleting whatever it does pick is not the operation that was
    asked for. Building the relief into the profile keeps it inside the loft,
    where it cannot fail silently.
    """
    ys_out = ys
    if bevel > 0.0:
        span = float(ys[-1] - ys[0])
        b = min(bevel, span / 4.0)
        ys_out = np.clip(ys, ys[0] + b, ys[-1] - b)
    points = [cq.Vector(float(x), float(y), z) for x, y in zip(inner, ys)]
    points += [
        cq.Vector(float(x), float(y), z) for x, y in zip(outer[::-1], ys_out[::-1])
    ]
    points.append(points[0])
    return cq.Wire.makePolygon(points)


def build_implant(params: dict) -> cq.Workplane:
    """Build the implant solid from params. FROZEN SIGNATURE.

    A plate whose bone-facing surface is the cortex offset outward by the mount
    clearance, station by station and lane by lane, with a constant wall walled
    out along +X and six round screw bores along their planned trajectories.
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

    half_width = width / 2.0
    zs = np.linspace(z0, z1, N_STATIONS)
    s = (zs - z0) / (z1 - z0)

    # Seat on the cortex itself rather than on the single most protruding point of
    # it. A flat plate has to clear the apex of the bow and therefore gapes
    # everywhere else; contouring removes that constraint, which is what turns an
    # 8.6 mm gap into the mount clearance.
    lanes = np.linspace(y_center - half_width, y_center + half_width, N_SEAT_LANES)
    seat_grid = _seating_surface(zs, lanes)

    # contour_spline lifts the seat further out along the length, on top of the
    # fitted offset. Empty means "sit at the clearance everywhere", which is what
    # a plate that only has to conform wants.
    standoff = clearance + _interp_profile(
        params["contour_spline"], s, 0.0, "contour_spline"
    )

    wires = []
    for z, row, off in zip(zs, seat_grid, standoff):
        inner = row + off
        wires.append(
            _section_wire(float(z), lanes, inner, inner + thickness, fillet)
        )
    plate = cq.Workplane(obj=cq.Solid.makeLoft(wires, ruled=True))

    # Screw bores, each along its own planned trajectory. The cutter starts well
    # outside the plate and is long enough to leave it again whatever the angle,
    # so an obliquely angled screw gets a bore that runs all the way through
    # instead of a hole drilled straight down the X axis it does not follow.
    plate_center = np.array([float(seat_grid.mean()), y_center, z_center])
    depth = float(seat_grid.max() - seat_grid.min()) + thickness
    diagonal = math.sqrt(length**2 + width**2 + depth**2)

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
