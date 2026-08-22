"""Geometry validation: fast, deterministic, mesh-based, runs first.

Nothing here is a surrogate or an approximation of physics -- these are hard
facts about the exported solid. They run first so an implant that is not even a
valid part never reaches the stress model.

Owner: B. This is a working first pass, not a finished suite. Each check is
independent, so extending it is additive.

Everything is measured off the exported STL rather than the CadQuery model on
purpose: a mesh is what the ray casts and volume queries here need, and it is the
one artefact that stays valid if the generator is rewritten to use a different
kernel. STEP is the manufacturing deliverable (see autoimplants/export.py); the
STL is the measurement surface. Its tessellation tolerance is set tight enough
that faceting error alone cannot fail a threshold.

Two assumptions were removed when real CT cases arrived, because neither held
outside the synthetic femur: that screws always run along -X (they now cast along
their own direction), and that the plate-bone gap can be read off the y=0
centreline (it is now sampled across the plate width). Both changes are strictly
more conservative -- they can only find violations the old checks missed.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .. import case_io
from ..bone import load_bone, surface_grid
from ..contracts import FAIL, PASS, Check, Report

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Sampling is inset from the part's bounding box so that the thin slivers at
# filleted edges do not register as min-wall violations.
EDGE_INSET_MM = 2.0
N_THICKNESS_Y = 9
N_THICKNESS_Z = 41
N_PROFILE_SAMPLES = 31
# Lanes across the plate width for the bone-gap checks. Odd so one lane is the
# centreline, keeping the historical measurement inside the new envelope.
N_PROFILE_LANES = 5


def _load(path: str):
    import trimesh

    mesh = trimesh.load(str(path), force="mesh")
    if mesh.is_empty:
        raise ValueError(f"implant mesh at {path} is empty or unreadable")
    return mesh


def _screws(case: dict) -> list[dict]:
    return case_io.load_screws(case)


def _keepouts(case: dict) -> list[dict]:
    return case_io.load_keepouts(case)


def _bone(case: dict):
    return load_bone(case_io.bone_path(case))


def _x_chords(mesh, y: float, z: float) -> list[float]:
    """Solid chord lengths where a +X ray at (y, z) passes through the part."""
    origin = np.array([[mesh.bounds[0][0] - 10.0, y, z]])
    hits, _, _ = mesh.ray.intersects_location(
        ray_origins=origin, ray_directions=np.array([[1.0, 0.0, 0.0]])
    )
    if len(hits) < 2:
        return []
    xs = np.sort(hits[:, 0])
    # Entry/exit pairs. Odd counts mean a degenerate or non-manifold hit; drop the tail.
    return [float(xs[i + 1] - xs[i]) for i in range(0, len(xs) - 1, 2)]


def _lanes(mesh) -> np.ndarray:
    """y positions across the plate width to measure the bone gap along."""
    lo, hi = mesh.bounds
    span = float(hi[1] - lo[1])
    inset = min(EDGE_INSET_MM, 0.25 * span)
    return np.linspace(float(lo[1]) + inset, float(hi[1]) - inset, N_PROFILE_LANES)


def _first_hit_x(mesh, ys: np.ndarray, zs: np.ndarray) -> np.ndarray:
    """x where a +X ray at each (y, z) first meets the part. NaN if it misses.

    One batched cast: the per-sample Python loop this replaced was the dominant
    cost of the gap check, and multiplying the sample count by the number of
    lanes would have made it the dominant cost of the whole validator.
    """
    m = ys.size
    if m == 0:
        return np.zeros(0)
    origins = np.column_stack([np.full(m, float(mesh.bounds[0][0]) - 10.0), ys, zs])
    directions = np.tile([1.0, 0.0, 0.0], (m, 1))
    hits, ray_idx, _ = mesh.ray.intersects_location(
        ray_origins=origins, ray_directions=directions
    )
    best = np.full(m, np.inf)
    if len(hits):
        np.minimum.at(best, ray_idx, hits[:, 0])
    return np.where(np.isfinite(best), best, np.nan)


def _perpendicular_basis(d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two unit vectors spanning the plane perpendicular to unit vector ``d``.

    The old code built the ring in the Y-Z plane, which is perpendicular to the
    trajectory only while every screw runs along X. Real planning data has
    obliquely angled screws, and a ring in the wrong plane samples an ellipse
    wider than the bore -- reporting a blocked screw where the bore is open.
    """
    seed = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(d, seed)
    u /= np.linalg.norm(u)
    v = np.cross(d, u)
    return u, v / np.linalg.norm(v)


# --- individual checks -------------------------------------------------------


def check_watertight(mesh, case: dict) -> Check:
    required = bool(case.get("thresholds", {}).get("require_watertight", True))
    ok = bool(mesh.is_watertight and mesh.is_winding_consistent)
    return Check(
        id="manifold_watertight",
        status=PASS if (ok or not required) else FAIL,
        value=1.0 if ok else 0.0,
        limit=1.0 if required else 0.0,
        unit="bool",
        message=(
            "exported solid is a closed, consistently wound manifold"
            if ok
            else f"solid is not manifold (watertight={mesh.is_watertight}, "
                 f"winding={mesh.is_winding_consistent}); it is not manufacturable "
                 f"and downstream checks cannot be trusted"
                 + ("" if required else " -- not enforced: require_watertight is off")
        ),
    )


def check_envelope(mesh, case: dict) -> list[Check]:
    env = case.get("envelope", {})
    lo, hi = mesh.bounds
    span_x, span_y, span_z = (hi - lo)

    checks = [
        Check(
            id="envelope_length",
            status=PASS if span_z <= env.get("max_length_mm", 1e9) + 1e-6 else FAIL,
            value=round(float(span_z), 3),
            limit=env.get("max_length_mm"),
            unit="mm",
            message="plate length along the shaft",
        ),
        Check(
            id="envelope_width",
            status=PASS if span_y <= env.get("max_width_mm", 1e9) + 1e-6 else FAIL,
            value=round(float(span_y), 3),
            limit=env.get("max_width_mm"),
            unit="mm",
            message="plate width",
        ),
    ]

    # Standoff: how far the part protrudes beyond the bone surface it mounts on.
    z0, z1 = float(lo[2]), float(hi[2])
    _, _, bone_xs = surface_grid(
        z0, z1, ys=_lanes(mesh), n=N_PROFILE_SAMPLES, path=case_io.bone_path(case)
    )
    bone_max_x = float(np.nanmax(bone_xs))
    standoff = float(hi[0]) - bone_max_x
    checks.append(
        Check(
            id="envelope_standoff",
            status=PASS if standoff <= env.get("max_standoff_mm", 1e9) + 1e-6 else FAIL,
            value=round(standoff, 3),
            limit=env.get("max_standoff_mm"),
            unit="mm",
            message="outer surface protrusion beyond the bone -- soft tissue clearance",
        )
    )
    return checks


def check_min_wall(mesh, case: dict) -> Check:
    limit = case.get("thresholds", {}).get("min_wall_mm", 0.0)
    lo, hi = mesh.bounds

    ys = np.linspace(lo[1] + EDGE_INSET_MM, hi[1] - EDGE_INSET_MM, N_THICKNESS_Y)
    zs = np.linspace(lo[2] + EDGE_INSET_MM, hi[2] - EDGE_INSET_MM, N_THICKNESS_Z)

    worst = float("inf")
    worst_at = None
    for y in ys:
        for z in zs:
            for chord in _x_chords(mesh, float(y), float(z)):
                if chord < worst:
                    worst, worst_at = chord, [None, float(y), float(z)]

    if worst == float("inf"):
        return Check(
            id="min_wall_thickness",
            status=FAIL,
            unit="mm",
            message="could not sample any wall thickness -- the part may be empty or hollow",
        )

    if worst_at:
        worst_at[0] = round(float(lo[0]), 3)
    return Check(
        id="min_wall_thickness",
        status=PASS if worst >= limit - 1e-6 else FAIL,
        value=round(worst, 3),
        limit=limit,
        unit="mm",
        location=[round(c, 3) for c in worst_at] if worst_at else None,
        message=(
            "thinnest sampled wall clears the manufacturing minimum"
            if worst >= limit - 1e-6
            else f"thinnest sampled wall is {worst:.2f} mm, below the {limit} mm minimum"
        ),
    )


def check_mass(mesh, case: dict) -> Check:
    density = case.get("material", {}).get("density_g_cm3", 4.43)
    limit = case.get("thresholds", {}).get("max_implant_mass_g")
    mass_g = float(mesh.volume) / 1000.0 * density
    ok = limit is None or mass_g <= limit + 1e-6
    return Check(
        id="implant_mass",
        status=PASS if ok else FAIL,
        value=round(mass_g, 3),
        limit=limit,
        unit="g",
        message=(
            "implant mass within budget"
            if ok
            else f"implant mass {mass_g:.1f} g exceeds the {limit} g budget -- "
                 f"reinforce locally instead of adding material everywhere"
        ),
    )


def check_bone_collision(mesh, case: dict) -> Check:
    bone = _bone(case)
    inside = bone.contains(mesh.vertices)
    n_inside = int(inside.sum())
    loc = None
    if n_inside:
        loc = [round(float(c), 3) for c in mesh.vertices[inside].mean(axis=0)]
    return Check(
        id="no_bone_collision",
        status=PASS if n_inside == 0 else FAIL,
        value=float(n_inside),
        limit=0.0,
        unit="vertices",
        location=loc,
        message=(
            "implant does not intersect the bone"
            if n_inside == 0
            else f"{n_inside} implant vertices lie inside the bone -- the part cannot be seated"
        ),
    )


def check_bone_conformance(mesh, case: dict) -> list[Check]:
    """The bone-implant gap, bounded from both sides.

    Too large and the plate does not follow the shaft -- this is the check the
    generic flat plate fails on this patient. Too small is also a failure, and a
    less obvious one: pressing a plate onto bone crushes the periosteum and
    interrupts the blood supply the fracture heals through. Zero clearance is not
    the optimum, which is why limited-contact plate designs exist.

    Both bounds come out of one ray-cast pass. The profile is sampled along
    several lanes across the plate width, not just the y=0 centreline: a shaft is
    curved in both planes, so a plate can seat on its centreline and still stand
    off -- or dig in -- at its edges.
    """
    thresholds = case.get("thresholds", {})
    max_limit = thresholds.get("max_bone_gap_mm", 1e9)
    min_limit = thresholds.get("min_bone_gap_mm")
    lo, hi = mesh.bounds

    lanes = _lanes(mesh)
    zs, ys, bone_xs = surface_grid(
        float(lo[2]), float(hi[2]), ys=lanes, n=N_PROFILE_SAMPLES,
        path=case_io.bone_path(case),
    )

    zz, yy = np.meshgrid(zs, ys, indexing="ij")
    flat_bone = bone_xs.reshape(-1)
    flat_y = yy.reshape(-1)
    flat_z = zz.reshape(-1)

    seen = np.isfinite(flat_bone)
    implant_x = np.full(flat_bone.shape, np.nan)
    implant_x[seen] = _first_hit_x(mesh, flat_y[seen], flat_z[seen])

    gaps = implant_x - flat_bone
    measured = np.isfinite(gaps)

    if not measured.any():
        return [
            Check(
                id="bone_conformance_gap",
                status=FAIL,
                unit="mm",
                message="could not measure the bone-implant gap anywhere along the footprint",
            ),
            Check(
                id="bone_clearance_min",
                status=FAIL,
                unit="mm",
                message="could not measure the bone-implant gap anywhere along the footprint",
            ),
        ]

    idx_worst = int(np.nanargmax(np.where(measured, gaps, -np.inf)))
    idx_tight = int(np.nanargmin(np.where(measured, gaps, np.inf)))
    worst_gap = float(gaps[idx_worst])
    tightest_gap = float(gaps[idx_tight])

    ok_max = worst_gap <= max_limit + 1e-6
    checks = [
        Check(
            id="bone_conformance_gap",
            status=PASS if ok_max else FAIL,
            value=round(worst_gap, 3),
            limit=max_limit,
            unit="mm",
            location=[
                round(float(implant_x[idx_worst]), 3),
                round(float(flat_y[idx_worst]), 2),
                round(float(flat_z[idx_worst]), 2),
            ],
            message=(
                "implant follows the bone surface within tolerance"
                if ok_max
                else f"implant stands {worst_gap:.2f} mm off the bone at "
                     f"y={flat_y[idx_worst]:.1f}, z={flat_z[idx_worst]:.0f} mm; "
                     f"the plate must follow the contour of the shaft"
            ),
        )
    ]

    # A negative gap means the bone-facing surface has crossed inside the bone
    # surface. check_bone_collision only samples implant vertices, so a face
    # passing through the bone between vertices reaches this check and nothing
    # else.
    ok_min = min_limit is None or tightest_gap >= min_limit - 1e-6
    checks.append(
        Check(
            id="bone_clearance_min",
            status=PASS if ok_min else FAIL,
            value=round(tightest_gap, 3),
            limit=min_limit,
            unit="mm",
            location=[
                round(float(implant_x[idx_tight]), 3),
                round(float(flat_y[idx_tight]), 2),
                round(float(flat_z[idx_tight]), 2),
            ],
            message=(
                "implant keeps a periosteal clearance off the bone"
                if ok_min
                else f"implant sits {tightest_gap:.2f} mm from the bone at "
                     f"y={flat_y[idx_tight]:.1f}, z={flat_z[idx_tight]:.0f} mm, "
                     f"inside the {min_limit} mm minimum"
                     + (
                         " -- the surface has crossed into the bone"
                         if tightest_gap < 0
                         else " -- contact this tight strips the periosteum"
                     )
            ),
        )
    )
    return checks


def check_screws(mesh, case: dict) -> list[Check]:
    """Every planned screw must still have an unobstructed bore through the plate."""
    checks = []
    blocked = []
    screws = _screws(case)
    diag = float(np.linalg.norm(mesh.bounds[1] - mesh.bounds[0]))

    for s in screws:
        entry = np.array(s["entry_mm"], dtype=float)
        d = np.array(s["direction"], dtype=float)  # unit, normalised by case_io
        r = 0.45 * float(s["diameter_mm"])  # just inside the nominal bore

        # Centre ray plus a ring, all launched from outside the part, in the plane
        # perpendicular to this screw's own trajectory.
        u, v = _perpendicular_basis(d)
        offsets = [np.zeros(3)]
        for ang in (0.0, np.pi / 2, np.pi, 3 * np.pi / 2):
            offsets.append(r * (np.cos(ang) * u + np.sin(ang) * v))

        # Back off far enough to start outside the part whatever the entry point
        # is, rather than the fixed 60 mm that assumed the synthetic geometry.
        backoff = diag + float(np.linalg.norm(entry - mesh.bounds.mean(axis=0)))
        origin = entry - d * backoff
        origins = np.array([origin + o for o in offsets])
        dirs = np.tile(d, (len(offsets), 1))
        hits, _, _ = mesh.ray.intersects_location(ray_origins=origins, ray_directions=dirs)
        # No intersection means the bore is open: the rays travel through the
        # hole without meeting solid. Any hit means material is in the way.
        if len(hits):
            blocked.append(s["id"])

    n_ok = len(screws) - len(blocked)
    required = int(case.get("thresholds", {}).get("require_all_screws", len(screws)))
    ok = n_ok >= required
    checks.append(
        Check(
            id="screw_trajectories_clear",
            status=PASS if ok else FAIL,
            value=float(n_ok),
            limit=float(required),
            unit="count",
            message=(
                f"{n_ok} of {len(screws)} planned screws have an open bore through the plate"
                if ok
                else f"obstructed screw bores: {', '.join(blocked)}. Screw positions are "
                     f"locked planning input -- the implant must accommodate them."
            ),
        )
    )
    return checks


def check_keepouts(mesh, case: dict) -> list[Check]:
    limit = float(case.get("thresholds", {}).get("max_keepout_encroach_mm", 0.0))
    checks = []
    for zone in _keepouts(case):
        if zone.get("type") != "sphere":
            continue
        center = np.array([zone["center_mm"]], dtype=float)
        radius = float(zone["radius_mm"])
        _, dist, _ = mesh.nearest.on_surface(center)
        d = float(dist[0])
        inside = bool(mesh.contains(center)[0])
        encroach = radius - d
        violated = inside or encroach > limit + 1e-6
        checks.append(
            Check(
                id=f"keepout_{zone['id']}",
                status=FAIL if violated else PASS,
                value=round(max(encroach, 0.0), 3),
                limit=limit,
                unit="mm",
                location=[round(float(c), 3) for c in center[0]],
                message=(
                    f"clears the {zone['id']} zone by {d - radius:.2f} mm"
                    if not violated
                    else f"encroaches {encroach:.2f} mm into {zone['id']}. "
                         f"{zone.get('rationale', '')}"
                ),
            )
        )
    return checks


# --- frozen entry point -----------------------------------------------------


def validate(implant_path: str, case: dict) -> Report:
    mesh = _load(implant_path)

    checks: list[Check] = [check_watertight(mesh, case)]
    checks += check_envelope(mesh, case)
    checks.append(check_min_wall(mesh, case))
    checks.append(check_mass(mesh, case))
    checks.append(check_bone_collision(mesh, case))
    checks += check_bone_conformance(mesh, case)
    checks += check_screws(mesh, case)
    checks += check_keepouts(mesh, case)

    return Report.from_checks(
        checks,
        meta={
            "validator": "geometry",
            "triangles": int(len(mesh.faces)),
            "volume_mm3": round(float(mesh.volume), 2),
            "bone_mesh": str(case_io.bone_path(case)),
        },
    )
