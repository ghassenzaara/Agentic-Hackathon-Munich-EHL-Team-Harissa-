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

from . import case_io
from .bone import surface_grid

REPO_ROOT = Path(__file__).resolve().parent.parent

# Cross-sections lofted along the shaft, and points sampled across the width
# within each one. 41 stations put a section every ~4.5 mm on a 180 mm plate;
# the bow's sagitta over that span is under 0.01 mm, so a ruled loft between
# neighbouring sections is smooth to far better than any threshold here.
N_STATIONS = 41
N_SECTION_POINTS = 41

# Bone samples across the width per station, used to fit the seating cylinder.
# 17 lanes on a 16 mm plate is a sample every millimetre; the seating surface is
# held clear of every one of them, so lanes the fit never saw are the only way a
# reconstruction bump can still cross it.
N_FIT_LANES = 17
# Candidate seating radii for that fit. The femoral shaft cortex here sits near
# 10 mm; the range is wide enough to cover a flatter or rounder real bone.
R_FIT_CANDIDATES = np.linspace(4.0, 80.0, 761)

# Flip these to True as you implement the corresponding geometry below.
THICKNESS_PROFILE_IMPLEMENTED = True
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


def _fit_seating_cylinder(
    ys: np.ndarray, xs: np.ndarray
) -> tuple[float, float] | None:
    """Best-fit circle through one station's cortex samples, centred on the plate axis.

    Returns ``(x_center, radius)`` of a circle ``x = x_center + sqrt(R^2 - y^2)``
    enclosing the measured surface, or None if the station has too few hits to fit.

    The fit encloses the samples rather than splitting its residual either side of
    them: the circle is placed so that no measured cortex point lies inside it. A
    least-squares seat leaves half the bone above the arc, so the only thing
    keeping the plate out of it is ``mount_clearance_mm``. That held on the
    analytic mesh and failed on a mesh reconstructed from CT, where marching-cubes
    ripple is of the same order as the clearance: -0.007 mm at one lane, i.e.
    inside the bone. Enclosing makes the clearance a clearance rather than an
    error budget.

    The radius is then the one whose *enclosing* seat sits closest to the bone --
    a minimax fit, not least squares. Enclosing a least-squares radius would work
    too, but it pays for the ripple twice: once in the residual it fits and again
    in the push-out, and every micron of push-out is standoff the outer surface
    also has to spend against a 6 mm envelope.

    Only two degrees of freedom are fitted -- radius and radial position -- and
    the centre is pinned to the plate's own y axis. A free-centre fit would chase
    the asymmetry of a single station's cortex and make the seating surface
    wander sideways between stations, which buys nothing: what the gap check
    measures is the residual between this surface and the bone, and a two
    parameter fit already drives that to a few tenths of a millimetre.
    """
    ok = np.isfinite(xs)
    y, x = ys[ok], xs[ok]
    if y.size < 3:
        return None

    candidates = R_FIT_CANDIDATES[R_FIT_CANDIDATES > np.abs(y).max() + 1e-6]
    if candidates.size == 0:
        return None

    # x = x_center + sqrt(R^2 - y^2). For each candidate R the enclosing centre is
    # the largest residual, and the gap it leaves is what the fit minimises, so
    # the whole sweep is two vectorised reductions.
    arc = np.sqrt(candidates[:, None] ** 2 - y[None, :] ** 2)
    centers = (x[None, :] - arc).max(axis=1)
    worst_gap = (centers[:, None] + arc - x[None, :]).max(axis=1)
    best = int(np.argmin(worst_gap))
    return float(centers[best]), float(candidates[best])


def _seating_geometry(
    zs: np.ndarray, y_center: float, half_width: float
) -> tuple[np.ndarray, np.ndarray]:
    """Fitted cortex centre and radius at every station, in one ray batch.

    Stations whose rays miss the bone inherit the nearest fitted station: the
    footprint can overhang the end of a segmented mesh, and a plate that raises
    an exception there would be unusable on real imaging that is simply cropped.
    """
    lanes = np.linspace(y_center - half_width, y_center + half_width, N_FIT_LANES)
    _, _, surface = surface_grid(float(zs[0]), float(zs[-1]), ys=lanes, n=len(zs))

    centers = np.full(len(zs), np.nan)
    radii = np.full(len(zs), np.nan)
    for i in range(len(zs)):
        fit = _fit_seating_cylinder(lanes - y_center, surface[i])
        if fit is not None:
            centers[i], radii[i] = fit

    fitted = np.flatnonzero(np.isfinite(radii))
    if fitted.size == 0:
        raise ValueError(
            "could not find the bone surface under any station of the plate "
            "footprint -- the plan places the plate off the segmented bone"
        )
    if fitted.size < len(zs):
        nearest = fitted[np.abs(np.arange(len(zs))[:, None] - fitted[None, :]).argmin(axis=1)]
        centers = centers[nearest]
        radii = radii[nearest]

    return centers, radii


def _section_wire(
    z: float,
    y_center: float,
    half_width: float,
    x_center: float,
    r_inner: float,
    r_outer: float,
) -> cq.Wire:
    """One cross-section: a cylindrical shell of constant radial thickness, cut square.

    Both faces are arcs about the fitted cortex centre, so the wall is ``r_outer
    - r_inner`` thick along every ray the validators cast. The sides are cut by
    the planes ``y = y_center +/- half_width`` rather than by a constant sector
    angle: a sector of fixed angle tapers to a sliver at the plate edge as the
    wall thickens, which is both a min-wall failure and a stress riser exactly
    where the screw heads bear.
    """
    ys = np.linspace(y_center - half_width, y_center + half_width, N_SECTION_POINTS)
    local = ys - y_center
    inner = x_center + np.sqrt(r_inner**2 - local**2)
    outer = x_center + np.sqrt(r_outer**2 - local**2)

    points = [cq.Vector(float(x), float(y), z) for x, y in zip(inner, ys)]
    points += [cq.Vector(float(x), float(y), z) for x, y in zip(outer[::-1], ys[::-1])]
    points.append(points[0])
    return cq.Wire.makePolygon(points)


def build_implant(params: dict) -> cq.Workplane:
    """Build the implant solid from params. FROZEN SIGNATURE.

    A plate whose bone-facing surface is fitted to the cortex station by station,
    with a wall thickness that follows ``thickness_profile`` along the length and
    six round screw bores along their planned trajectories.
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
    x_centers, radii = _seating_geometry(zs, y_center, half_width)

    standoff = clearance + _interp_profile(
        params["contour_spline"], s, 0.0, "contour_spline"
    )
    thicknesses = _interp_profile(
        params["thickness_profile"], s, thickness, "thickness_profile"
    )
    if np.any(thicknesses <= 0.0):
        raise ValueError(
            f"thickness_profile produces a non-positive wall "
            f"({thicknesses.min():.2f} mm); a plate cannot be thinner than nothing"
        )

    r_inner = radii + standoff
    r_outer = r_inner + thicknesses
    if np.any(r_inner <= half_width + 1e-6):
        worst = int(np.argmin(r_inner))
        raise ValueError(
            f"the cortex fitted at z={zs[worst]:.0f} mm has a seating radius of "
            f"{r_inner[worst]:.1f} mm, which is narrower than the {half_width:.1f} mm "
            f"half-width of the plate. The plate would have to wrap past the "
            f"widest point of the shaft; narrow params['width_mm'] instead."
        )

    wires = [
        _section_wire(float(z), y_center, half_width, float(xc), float(ri), float(ro))
        for z, xc, ri, ro in zip(zs, x_centers, r_inner, r_outer)
    ]
    plate = cq.Workplane(obj=cq.Solid.makeLoft(wires, ruled=True))

    # Round the four long corners. Edges parallel to X are the plan-view corners.
    if fillet > 0:
        plate = plate.edges("|X").fillet(fillet)

    # Screw bores, each along its own planned trajectory. The cutter starts well
    # outside the plate and is long enough to leave it again whatever the angle,
    # so an obliquely angled screw gets a bore that runs all the way through
    # instead of a hole drilled straight down the X axis it does not follow.
    plate_center = np.array(
        [float(x_centers.mean() + r_inner.mean()), y_center, z_center]
    )
    diagonal = math.sqrt(length**2 + width**2 + float(r_outer.max()) ** 2)

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
