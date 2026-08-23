"""THE FILE DEVIN EDITS.

Everything else in this repo is scaffolding around this one function::

    build_implant(params: dict) -> cadquery.Workplane

What it builds is a CONTOURED, VARIABLE-SECTION plate: the bone-facing surface is
a cylinder fitted to the measured cortex at each station along the shaft, so the
part follows both the longitudinal bow and the transverse curvature, and the wall
thickness varies along the length so section sits where the bending moment is.

The generic off-the-shelf part it replaced -- a flat, straight,
constant-thickness slab -- could not pass on this patient, and no scalar version
of it can: a flat face against a ~10 mm radius cortex gapes ~3.9 mm at the plate
edge before the 22 mm bow is even counted, and the exhaustive sweep in
inputs/case.json finds no passing constant-thickness design inside the legal
envelope.

Coordinate frame (matches inputs/bone.stl):
    +Z  along the femoral shaft, proximal to distal
    +X  lateral -- the aspect the plate mounts on, and the plate thickness direction
    +Y  the plate width direction

Two of the four topology handles are now implemented: ``contour_spline`` (extra
radial standoff along the length, on top of the fitted seating radius) and
``thickness_profile``. ``ribs`` and ``hole_slots`` are still declared and still
raise, because setting a parameter must not be enough -- to use them you have to
write the geometry.
"""

from __future__ import annotations

import math
from pathlib import Path

import cadquery as cq
import numpy as np
from scipy.ndimage import gaussian_filter, maximum_filter

from . import case_io, patch
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
# 2 rather than 4: 4 costs ~26k rays against a marching-cubes mesh for a further
# 0.4 mm of conformance margin the limit does not need.
SEAT_REFINE = 2

# Flip these to True as you implement the corresponding geometry below.
THICKNESS_PROFILE_IMPLEMENTED = True
RIBS_IMPLEMENTED = True
HOLE_SLOTS_IMPLEMENTED = False
CONTOUR_SPLINE_IMPLEMENTED = True
WIDTH_PROFILE_IMPLEMENTED = True


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
        ("width_profile", WIDTH_PROFILE_IMPLEMENTED,
         "Vary the plate width along its length, so a keepout that blocks width "
         "over a few centimetres does not have to narrow the whole plate."),
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


def _moment_thickness(
    spec: dict, zs: np.ndarray, screw_zs: np.ndarray
) -> np.ndarray:
    """Wall thickness following the bending moment of *this* plan's screw span.

    ``thickness_profile`` places section at fixed fractions of the plate length,
    which is only right while the screws stay where they were when the fractions
    were picked. They do not: an imported case puts its own six entries wherever
    the surgeon planned them, and the two stations that decide the hole checks
    move with them. On a femur segmented from a real CT the inner holes landed at
    s=0.22 and s=0.78, either side of a profile whose 4.5 mm peak sat between
    them, and both reported ~410 MPa against a 350 limit on a 3.1 mm wall while
    the thickest part of the plate carried no hole at all.

    So thickness is derived rather than pinned. The load model is a
    simply-supported span loaded at mid-footprint -- full moment over the
    fracture, tapering to zero at the outermost screws -- and the wall rises with
    that moment: ``t = min_mm + (max_mm - min_mm) * taper**exponent``, with
    ``min_mm`` what the ends keep once the moment has run out. ``exponent`` = 0.5
    is the constant-fibre-stress rule (modulus goes as t^2); the default is 1.0
    because 0.5 overruns the mass budget -- see params.moment_thickness.

    The result is combined with ``thickness_profile`` by taking the larger of the
    two, so an explicit profile can still add section this rule does not ask for,
    and cannot silently remove any.
    """
    if not spec:
        return np.zeros(zs.shape)

    t_min = float(spec.get("min_mm", 0.0))
    t_max = float(spec.get("max_mm", t_min))
    exponent = float(spec.get("exponent", 0.5))
    if t_max < t_min:
        raise ValueError(
            f"moment_thickness max_mm ({t_max:.2f}) is below min_mm ({t_min:.2f})"
        )

    z_mid = 0.5 * (float(screw_zs.min()) + float(screw_zs.max()))
    half_span = 0.5 * (float(screw_zs.max()) - float(screw_zs.min()))
    if half_span <= 0.0:
        return np.full(zs.shape, t_max)

    taper = np.clip(1.0 - np.abs(zs - z_mid) / half_span, 0.0, 1.0)
    return t_min + (t_max - t_min) * taper**exponent


def _hole_bosses(spec: dict, zs: np.ndarray, screw_zs: np.ndarray) -> np.ndarray:
    """Extra wall local to each bore, blended in over ``span_mm``.

    The moment rule above puts the thickest wall at mid-footprint, which is where
    the moment is -- and on any plan whose screws straddle the fracture, that is
    the one station with no hole in it. Every reported hole stress is a *net*
    section amplified by Kt, so the material that decides those checks is the
    material beside the bore, not 20 mm away from it. This is the correction:
    a raised pad at each planned entry, tapered out with a cosine so the loft
    stays smooth and the wall never steps.

    Local because mass is the binding constraint (54.5 of 55 g). Thickening the
    whole plate to reach the same wall at the bores costs ~4 g the budget does not
    have; six pads spanning ``span_mm`` each cost ~0.5 g.
    """
    if not spec:
        return np.zeros(zs.shape)

    height = float(spec.get("height_mm", 0.0))
    span = float(spec.get("span_mm", 0.0))
    if height <= 0.0 or span <= 0.0:
        return np.zeros(zs.shape)

    boost = np.zeros(zs.shape)
    for z0 in screw_zs:
        d = np.abs(zs - float(z0)) / span
        local = np.where(d < 1.0, height * np.cos(0.5 * np.pi * d) ** 2, 0.0)
        boost = np.maximum(boost, local)
    return boost


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
    primitive fitted through it. It used to be one circle per station: two degrees
    of freedom against a whole cross-section, and that approximation cost twice
    over. The residual it could not follow showed up directly in
    ``bone_conformance_gap``, and then, to stay out of the bone, the circle had to
    enclose that residual, spending standoff on fitting error rather than on wall.
    On a mesh reconstructed from CT the two together left 1.25 mm of gap against a
    1.5 mm limit -- passing, but with nothing left for a rougher scan. Offsetting
    the measurement removes both terms: the gap is the clearance, by construction,
    whatever shape the bone turns out to be.

    The samples are dilated by one step in each direction first. Rays only see the
    surface where they are cast, so a bump *between* samples could otherwise poke
    through; taking the local maximum makes the seat clear the neighbourhood a
    sample stands for rather than the single point it measured.

    The dilation runs on a grid refined ``SEAT_REFINE`` times in both directions,
    not on the stations and lanes the section is built from. Those are ~4.5 mm
    apart along the length, so dilating at that spacing has each section clear the
    highest cortex within +-4.5 mm of itself -- on a tapering shaft that is mostly
    the taper, not a bump. Across the width the same argument is sharper still:
    the outermost lanes sit on the flank of the shaft, where x falls away steeply,
    so half a lane of dilation there reads as a millimetre of standoff. Both
    showed up as ``bone_conformance_gap`` on the CT mesh -- 1.80 mm at station
    spacing, 1.53 mm with the length refined alone, against a 1.5 mm limit.
    Refining both keeps the window near 1 mm along and 0.1 mm across, which is the
    scale a reconstruction ripple lives at rather than the scale the anatomy does.
    """
    fine_lanes = np.interp(
        np.linspace(0, len(lanes) - 1, (len(lanes) - 1) * SEAT_REFINE + 1),
        np.arange(len(lanes)),
        lanes,
    )
    fine_n = (len(zs) - 1) * SEAT_REFINE + 1
    _, _, surface = surface_grid(
        float(zs[0]), float(zs[-1]), ys=fine_lanes, n=fine_n
    )
    filled = _fill_missing(surface)

    padded = np.pad(filled, 1, mode="edge")
    dilated = np.maximum.reduce(
        [
            padded[i : i + filled.shape[0], j : j + filled.shape[1]]
            for i in range(3)
            for j in range(3)
        ]
    )
    return _smooth_envelope(dilated[::SEAT_REFINE, ::SEAT_REFINE])


def _smooth_envelope(seat: np.ndarray) -> np.ndarray:
    """Smooth the seat, then lift it until it encloses every sample again.

    Seating straight onto reconstructed samples makes the loft sections jagged at
    the ~0.2 mm scale of the segmentation ripple, and neighbouring wires that
    wobble against each other loft into a solid whose faces graze and cross. That
    is not a visible failure: the STL still closes, so ``manifold_watertight``
    passes, but a +X ray through the part comes back with one hit instead of two
    and the gap check reads a 4.6 mm standoff off the far wall of a fold.

    Smoothing alone would sink the seat into the bumps it averaged over, i.e. into
    the bone. So the smoothed surface is raised until it clears the samples again
    -- but by the largest residual *nearby*, not the largest anywhere. A single
    global lift is set by the worst place on the plate, and the worst place is the
    proximal end, where the flank falls away steeply and smoothing has most to
    average over; on the CT mesh that one number spent 1.3 mm of gap at the far
    end of the plate, where the fit was fine (1.75 mm against a 1.5 mm limit).
    Taking the local maximum of the residual keeps the envelope property -- the
    window over any sample includes that sample -- and prices it where it is
    earned.
    """
    smooth = gaussian_filter(seat, sigma=(1.5, 0.5), mode="nearest")
    lift = maximum_filter(seat - smooth, size=(5, 3), mode="nearest")
    return smooth + lift


def _check_thickness_bounds(thicknesses: np.ndarray, zs: np.ndarray) -> None:
    """Hold the wall inside the case envelope's declared thickness bounds.

    ``envelope.thickness_bounds_mm`` is a locked input, but no validator reads its
    ``max``: ``check_min_wall`` only enforces the minimum, so a design can sail
    past 21/21 on a wall the case says is out of bounds -- which is exactly what
    the previous 5.5 mm profile did against a stated 4.5 mm maximum. Refusing it
    here keeps the loop honest about a constraint the examiner happens not to
    measure, since a plate is manufactured to the case, not to the report.
    """
    if np.any(thicknesses <= 0.0):
        raise ValueError(
            f"thickness_profile produces a non-positive wall "
            f"({thicknesses.min():.2f} mm); a plate cannot be thinner than nothing"
        )

    bounds = (case_io.active_case() or {}).get("envelope", {}).get(
        "thickness_bounds_mm", {}
    )
    lo, hi = bounds.get("min"), bounds.get("max")
    for limit, worst, what, ok in (
        (lo, thicknesses.min(), "below", lo is None or thicknesses.min() >= lo - 1e-6),
        (hi, thicknesses.max(), "above", hi is None or thicknesses.max() <= hi + 1e-6),
    ):
        if not ok:
            at = zs[int(np.argmin(thicknesses) if what == "below" else np.argmax(thicknesses))]
            raise ValueError(
                f"thickness_profile reaches {worst:.2f} mm at z={at:.0f} mm, "
                f"{what} the {limit} mm bound in the case envelope's "
                f"thickness_bounds_mm. No geometry check enforces this bound, so "
                f"it is enforced here instead of quietly exceeded."
            )


def _rib_height(ribs: list, z: float, zs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Extra outer-face height from ``ribs`` at station ``z``, lane by lane.

    Ribs are built into the section profile rather than unioned on as separate
    solids: the loft then stays a single closed shell, so ``manifold_watertight``
    cannot be broken by a boolean that nearly misses. Each entry is
    ``{s, length_mm, height_mm, width_mm}`` with an optional ``y_offset_mm``,
    so a pair of ribs can straddle the screw bores instead of running down the
    centreline where the bores would cut them in half.

    Both the y and z edges are ramped rather than stepped. A step would put a
    sharp re-entrant corner on the outer face -- a stress riser in the one place
    the design is adding material to *reduce* stress -- and would give the loft
    coincident faces to reconcile.
    """
    total = np.zeros_like(ys)
    z0, z1 = float(zs[0]), float(zs[-1])
    for rib in ribs:
        height = float(rib["height_mm"])
        half_len = float(rib["length_mm"]) / 2.0
        half_w = float(rib["width_mm"]) / 2.0
        y0 = float(rib.get("y_offset_mm", 0.0))
        z_center = z0 + float(rib["s"]) * (z1 - z0)

        ramp_z = max(half_len * 0.25, 1.0)
        along = np.clip((half_len + ramp_z - abs(z - z_center)) / ramp_z, 0.0, 1.0)
        if along <= 0.0:
            continue

        ramp_y = max(half_w * 0.5, 0.5)
        across = np.clip((half_w + ramp_y - np.abs(ys - y0)) / ramp_y, 0.0, 1.0)
        total = np.maximum(total, height * along * across)
    return total


def _section_wire(
    z: float, ys: np.ndarray, inner: np.ndarray, outer: np.ndarray, bevel: float
) -> cq.Wire:
    """One cross-section: the offset cortex profile, walled outward along +X.

    The wall is built along X because that is how both validators measure it --
    ``check_min_wall`` and ``section.py`` read X chords -- so ``outer - inner`` is
    exactly the thickness they will report. Walling radially instead would make
    the reported thickness a cosine of the local surface angle, thinnest at the
    plate edges, which is where the screw heads bear.

    ``bevel`` draws the outer profile in from the plate edge, breaking the sharp
    lateral corner. It replaces a CadQuery ``.fillet()`` on the lofted edges: on a
    free-form section the ``|X`` selector no longer picks out the four plan-view
    corners, and filleting whatever it did pick returned an invalid solid that
    had lost a third of its volume. Building the relief into the profile keeps it
    inside the loft, where it cannot fail silently.
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

    ``params["family"]`` picks the topology, because a plate is only one kind of
    implant: ``"plate"`` sweeps a fitted section along a shaft (long bones), and
    ``"conformal_patch"`` offsets a region of the bone's own surface into a shell
    (any anatomy -- see :mod:`autoimplants.patch`). The plate is the default so
    every existing case behaves exactly as before.
    """
    family = params.get("family", "plate")
    if family == "conformal_patch":
        return patch.build_patch(params)
    if family != "plate":
        raise ValueError(
            f"unknown implant family {family!r}; this generator builds 'plate' or "
            f"'conformal_patch'"
        )
    return _build_plate(params)


def _build_plate(params: dict) -> cq.Workplane:
    """A plate whose bone-facing surface is fitted to the cortex station by station,
    with a wall thickness that follows ``thickness_profile`` along the length and
    round screw bores along their planned trajectories.
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

    # Seat on the cortex itself rather than on the single most protruding point
    # of it. A flat plate has to clear the apex of the bow and therefore gapes
    # everywhere else; contouring removes that constraint, which is what turns an
    # 8.6 mm gap into a fraction of a millimetre.
    # Sample the cortex once, on lanes spanning the widest station, then read each
    # station's own lanes out of that grid: the width varies along the length.
    grid_lanes = np.linspace(y_center - half_width, y_center + half_width, N_SEAT_LANES)
    seat_grid = _seating_surface(zs, grid_lanes)

    half_widths = 0.5 * _interp_profile(
        params["width_profile"], s, width, "width_profile"
    )
    if np.any(half_widths * 2.0 > width + 1e-9):
        raise ValueError(
            f"width_profile reaches {2.0 * half_widths.max():.1f} mm, wider than "
            f"params['width_mm'] ({width:.1f} mm), which is what the seating grid "
            f"was sampled across. Raise width_mm if the plate really is that wide."
        )
    if np.any(half_widths * 2.0 < y_span + hole_d - 1e-9):
        worst = int(np.argmin(half_widths))
        raise ValueError(
            f"width_profile narrows to {2.0 * half_widths[worst]:.1f} mm at "
            f"z={zs[worst]:.0f} mm, but the screws span {y_span:.1f} mm and each "
            f"bore needs {hole_d:.1f} mm. A waist that cuts into a bore leaves the "
            f"screw head bearing on nothing."
        )

    standoff = clearance + _interp_profile(
        params["contour_spline"], s, 0.0, "contour_spline"
    )
    moment_spec = params["moment_thickness"]
    thicknesses = np.maximum(
        _interp_profile(
            params["thickness_profile"], s, thickness, "thickness_profile"
        ),
        _moment_thickness(moment_spec, zs, entries[:, 2]),
    )
    # Pads clipped to ceiling_mm -- the legal wall maximum, above the moment
    # rule's own peak, so a pad can reach it where the plain wall must not.
    ceiling = math.inf
    if moment_spec:
        ceiling = float(
            moment_spec.get("ceiling_mm", moment_spec.get("max_mm", math.inf))
        )
    thicknesses = np.minimum(
        thicknesses + _hole_bosses(params["hole_bosses"], zs, entries[:, 2]),
        ceiling,
    )
    _check_thickness_bounds(thicknesses, zs)

    wires = []
    for z, hw, row, off, t in zip(zs, half_widths, seat_grid, standoff, thicknesses):
        lanes = np.linspace(y_center - hw, y_center + hw, N_SECTION_POINTS)
        inner = np.interp(lanes, grid_lanes, row) + off
        outer = inner + t + _rib_height(params["ribs"], float(z), zs, lanes)
        wires.append(_section_wire(float(z), lanes, inner, outer, fillet))
    plate = cq.Workplane(obj=cq.Solid.makeLoft(wires, ruled=True))

    # Screw bores, each along its own planned trajectory. The cutter starts well
    # outside the plate and is long enough to leave it again whatever the angle,
    # so an obliquely angled screw gets a bore that runs all the way through
    # instead of a hole drilled straight down the X axis it does not follow.
    plate_center = np.array([float(seat_grid.mean()), y_center, z_center])
    depth = float(seat_grid.max() - seat_grid.min()) + float(thicknesses.max())
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
