"""Geometry validation: fast, deterministic, mesh-based, runs first.

Nothing here is a surrogate or an approximation of physics -- these are hard
facts about the exported solid. They run first so an implant that is not even a
valid part never reaches the stress model.

Owner: B. This is a working first pass, not a finished suite. Each check is
independent, so extending it is additive.

Everything is measured off the exported STL rather than the CadQuery model on
purpose: the STL is what a manufacturer receives, and it is the one artefact that
stays valid if the generator is rewritten to use a different kernel.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..bone import DEFAULT_BONE, load_bone, surface_profile
from ..contracts import FAIL, PASS, Check, Report

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Sampling is inset from the part's bounding box so that the thin slivers at
# filleted edges do not register as min-wall violations.
EDGE_INSET_MM = 2.0
N_THICKNESS_Y = 9
N_THICKNESS_Z = 41
N_PROFILE_SAMPLES = 31


def _load(path: str):
    import trimesh

    mesh = trimesh.load(str(path), force="mesh")
    if mesh.is_empty:
        raise ValueError(f"implant mesh at {path} is empty or unreadable")
    return mesh


def _screws() -> list[dict]:
    data = json.loads((REPO_ROOT / "inputs" / "screw_positions.json").read_text("utf-8"))
    return data["screws"]


def _keepouts() -> list[dict]:
    p = REPO_ROOT / "inputs" / "keepout_zones.json"
    if not p.exists():
        return []
    return json.loads(p.read_text("utf-8"))["zones"]


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


# --- individual checks -------------------------------------------------------


def check_watertight(mesh) -> Check:
    ok = bool(mesh.is_watertight and mesh.is_winding_consistent)
    return Check(
        id="manifold_watertight",
        status=PASS if ok else FAIL,
        value=1.0 if ok else 0.0,
        limit=1.0,
        unit="bool",
        message=(
            "exported solid is a closed, consistently wound manifold"
            if ok
            else f"solid is not manifold (watertight={mesh.is_watertight}, "
                 f"winding={mesh.is_winding_consistent}); it is not manufacturable "
                 f"and downstream checks cannot be trusted"
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
    prof = surface_profile(z0, z1, n=N_PROFILE_SAMPLES)
    bone_max_x = float(np.nanmax(prof[:, 1]))
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
    bone = load_bone(DEFAULT_BONE)
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


def check_bone_conformance(mesh, case: dict) -> Check:
    """Largest residual gap between the implant's bone-facing surface and the bone.

    This is the check the generic flat plate fails on this patient.
    """
    limit = case.get("thresholds", {}).get("max_bone_gap_mm", 1e9)
    lo, hi = mesh.bounds
    prof = surface_profile(float(lo[2]), float(hi[2]), n=N_PROFILE_SAMPLES)

    worst_gap = -float("inf")
    worst_z = None
    for z, bone_x in prof:
        if np.isnan(bone_x):
            continue
        # First hit travelling +X is the implant's bone-facing surface.
        hits, _, _ = mesh.ray.intersects_location(
            ray_origins=np.array([[lo[0] - 10.0, 0.0, z]]),
            ray_directions=np.array([[1.0, 0.0, 0.0]]),
        )
        if len(hits) == 0:
            continue
        gap = float(hits[:, 0].min()) - float(bone_x)
        if gap > worst_gap:
            worst_gap, worst_z = gap, float(z)

    if worst_z is None:
        return Check(
            id="bone_conformance_gap",
            status=FAIL,
            unit="mm",
            message="could not measure the bone-implant gap anywhere along the footprint",
        )

    ok = worst_gap <= limit + 1e-6
    return Check(
        id="bone_conformance_gap",
        status=PASS if ok else FAIL,
        value=round(worst_gap, 3),
        limit=limit,
        unit="mm",
        location=[round(float(mesh.bounds[0][0]), 3), 0.0, round(worst_z, 2)],
        message=(
            "implant follows the bone surface within tolerance"
            if ok
            else f"implant stands {worst_gap:.2f} mm off the bone at z={worst_z:.0f} mm; "
                 f"the plate must follow the contour of the shaft"
        ),
    )


def check_screws(mesh, case: dict) -> list[Check]:
    """Every planned screw must still have an unobstructed bore through the plate."""
    checks = []
    blocked = []
    for s in _screws():
        entry = np.array(s["entry_mm"], dtype=float)
        d = np.array(s["direction"], dtype=float)
        r = 0.45 * float(s["diameter_mm"])  # just inside the nominal bore

        # Centre ray plus a ring, all launched from outside the part.
        offsets = [np.zeros(3)]
        for ang in (0.0, np.pi / 2, np.pi, 3 * np.pi / 2):
            offsets.append(np.array([0.0, r * np.cos(ang), r * np.sin(ang)]))

        origin = entry - d * 60.0
        origins = np.array([origin + o for o in offsets])
        dirs = np.tile(d, (len(offsets), 1))
        hits, _, _ = mesh.ray.intersects_location(ray_origins=origins, ray_directions=dirs)
        if len(hits):
            blocked.append(s["id"])

    n_ok = len(_screws()) - len(blocked)
    checks.append(
        Check(
            id="screw_trajectories_clear",
            status=PASS if not blocked else FAIL,
            value=float(n_ok),
            limit=float(case.get("thresholds", {}).get("require_all_screws", len(_screws()))),
            unit="count",
            message=(
                "all planned screw trajectories pass through the implant unobstructed"
                if not blocked
                else f"obstructed screw bores: {', '.join(blocked)}. Screw positions are "
                     f"locked planning input -- the implant must accommodate them."
            ),
        )
    )
    return checks


def check_keepouts(mesh, case: dict) -> list[Check]:
    checks = []
    for zone in _keepouts():
        if zone.get("type") != "sphere":
            continue
        center = np.array([zone["center_mm"]], dtype=float)
        radius = float(zone["radius_mm"])
        _, dist, _ = mesh.nearest.on_surface(center)
        d = float(dist[0])
        inside = bool(mesh.contains(center)[0])
        encroach = radius - d
        violated = inside or encroach > 0
        checks.append(
            Check(
                id=f"keepout_{zone['id']}",
                status=FAIL if violated else PASS,
                value=round(max(encroach, 0.0), 3),
                limit=0.0,
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

    checks: list[Check] = [check_watertight(mesh)]
    checks += check_envelope(mesh, case)
    checks.append(check_min_wall(mesh, case))
    checks.append(check_mass(mesh, case))
    checks.append(check_bone_collision(mesh, case))
    checks.append(check_bone_conformance(mesh, case))
    checks += check_screws(mesh, case)
    checks += check_keepouts(mesh, case)

    return Report.from_checks(
        checks,
        meta={
            "validator": "geometry",
            "triangles": int(len(mesh.faces)),
            "volume_mm3": round(float(mesh.volume), 2),
        },
    )
