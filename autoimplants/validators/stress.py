"""Reduced-order analytical stress surrogate.

NOT clinical FEA. Beam bending on sampled cross-sections, stress concentration
factors at the screw holes, and a screw pull-out estimate. Analytical on purpose:
it must run in tens of milliseconds and it must never crash, because it sits
inside an autonomous loop.

Owner: B.

Model, stated once so the numbers can be argued with:

* **Section properties are measured, not assumed.** Every cross-section is
  sampled off the exported STL by ray casting (the same technique the geometry
  validator uses), so a rib, a variable thickness profile or a slot changes the
  section modulus because the *part* changed -- there is no parameter in this
  file that has to be told about them.
* **Bending** is Euler-Bernoulli about the plate's width axis (+Y), i.e. the
  through-thickness (+X) direction is the beam depth. The plate bridges the
  defect, so the moment peaks at mid-footprint and falls linearly to zero at the
  outermost screws, which is the simply-supported analogue of a bridging
  construct. The plate carries ``PLATE_BENDING_SHARE`` of the gait moment; the
  rest goes through the bone.
* **Axial** load is a uniform membrane stress over the same section, carrying
  ``PLATE_AXIAL_SHARE`` of the stance load.
* **Screw holes** are stress risers on top of the *net* section: Kt values are
  the plate-bending values (roughly 0.6x the in-plane tension chart, Peterson,
  nu = 0.34) for a through hole at d/w ~ 0.28. An axial slot relieves most of
  that, which is the entire reason ``hole_slots`` exists as a topology handle.
* **Pull-out** is thread shear over the engaged cortex, and the engagement is
  what the plate takes away: every millimetre the plate stands off the bone is a
  millimetre of screw that is not in bone. That is why conformance and fixation
  are not independent checks.

BEFORE YOU CALIBRATE, read design_space_note in inputs/case.json. The thresholds
are chosen so that uniform thickening cannot pass -- the mass cap only allows
~+5% volume, worth a 1.11x stress reduction. If you retune the stress model,
re-check that property holds, or the whole "why not an optimiser" argument
collapses. ``harness/design_space.py`` checks it for you.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..bone import DEFAULT_BONE, surface_x_at
from ..contracts import FAIL, PASS, Check, Report

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# --- model constants ---------------------------------------------------------

# Load sharing between plate and bone in a bridging construct. The plate takes
# most of the bending across the defect and a minority of the axial column load.
# The bending share is also the calibration anchor: it puts the baseline flat
# plate at 414 MPa, the 412 MPa quoted in design_space_note. Move it and the
# "no scalar tweak can pass" property has to be re-proved -- run
# `python -m harness.design_space`.
PLATE_BENDING_SHARE = 0.57
PLATE_AXIAL_SHARE = 0.35

# Stress concentration at a screw hole, applied to the net section. Plate-bending
# values, not the in-plane tension chart: the stress gradient runs through the
# thickness, which relaxes the riser by roughly 40%.
KT_ROUND_HOLE = 1.40
KT_SLOT = 1.10
# A void longer than this many hole diameters along Z is a slot, not a hole.
SLOT_ASPECT_THRESHOLD = 1.35

# Thread shear over engaged cortex. 10 mm is near-cortex plus far-cortex purchase
# for a 4.5 mm cortical screw in a diaphysis; 12 MPa is a conservative shear
# strength for the thread cylinder.
CORTEX_ENGAGEMENT_MM = 10.0
BONE_THREAD_SHEAR_MPA = 12.0

# Sampling. Sections every SECTION_STEP_MM plus one exactly at every screw.
SECTION_STEP_MM = 2.5
N_SECTION_Y = 81
END_INSET_MM = 1.0

# Check IDs the rest of the system already refers to. Keep them stable.
CHECK_IDS = (
    "stress_max_bending",
    "stress_hole_0",
    "stress_hole_1",
    "stress_hole_2",
    "stress_hole_3",
    "stress_hole_4",
    "stress_hole_5",
    "screw_pullout_min",
)


def _load(path: str):
    import trimesh

    mesh = trimesh.load(str(path), force="mesh")
    if mesh.is_empty:
        raise ValueError(f"implant mesh at {path} is empty or unreadable")
    return mesh


def _screws() -> list[dict]:
    data = json.loads((REPO_ROOT / "inputs" / "screw_positions.json").read_text("utf-8"))
    return data["screws"]


def _x_intervals(mesh, y: float, z: float) -> list[tuple[float, float]]:
    """Solid intervals in X where a +X ray at (y, z) passes through the part."""
    origin = np.array([[mesh.bounds[0][0] - 10.0, y, z]])
    hits, _, _ = mesh.ray.intersects_location(
        ray_origins=origin, ray_directions=np.array([[1.0, 0.0, 0.0]])
    )
    if len(hits) < 2:
        return []
    xs = np.sort(hits[:, 0])
    return [(float(xs[i]), float(xs[i + 1])) for i in range(0, len(xs) - 1, 2)]


class Section:
    """Measured properties of one cross-section, bending about the +Y axis."""

    __slots__ = ("area", "c", "inertia", "modulus", "z")

    def __init__(self, z: float, area: float, inertia: float, c: float) -> None:
        self.z = z
        self.area = area
        self.inertia = inertia
        self.c = c
        self.modulus = inertia / c if c > 0 else 0.0


def section_at(mesh, z: float, n_y: int = N_SECTION_Y) -> Section | None:
    """Integrate area, second moment and extreme fibre distance by ray casting.

    Strip integration over Y: each ray gives the solid intervals in X, and a
    rectangle's own moments are exact, so the only discretisation error is in Y.
    Ribs, slots and a varying thickness all fall out of the measurement.
    """
    lo, hi = mesh.bounds
    ys = np.linspace(lo[1], hi[1], n_y)
    dy = (hi[1] - lo[1]) / (n_y - 1)

    area = first = second = 0.0
    x_lo, x_hi = np.inf, -np.inf
    for y in ys:
        for x0, x1 in _x_intervals(mesh, float(y), z):
            area += (x1 - x0) * dy
            first += dy * (x1**2 - x0**2) / 2.0
            second += dy * (x1**3 - x0**3) / 3.0
            x_lo, x_hi = min(x_lo, x0), max(x_hi, x1)

    if area <= 0.0:
        return None
    x_bar = first / area
    inertia = second - area * x_bar**2  # parallel axis, back to the centroid
    c = max(x_hi - x_bar, x_bar - x_lo)
    return Section(z, area, inertia, c)


def moment_at(z: float, z_first: float, z_last: float, peak_nmm: float) -> float:
    """Bridging-construct bending moment: peak at mid-span, zero at the end screws."""
    z_mid = 0.5 * (z_first + z_last)
    half_span = 0.5 * (z_last - z_first)
    if half_span <= 0.0:
        return peak_nmm
    return peak_nmm * max(0.0, 1.0 - abs(z - z_mid) / half_span)


def section_stress(sec: Section, moment_nmm: float, axial_n: float) -> float:
    """Combined bending + membrane stress on the extreme fibre, MPa."""
    bending = moment_nmm / sec.modulus if sec.modulus > 0 else float("inf")
    membrane = axial_n / sec.area if sec.area > 0 else float("inf")
    return bending + membrane


def hole_is_slot(mesh, z_hole: float, hole_d: float) -> bool:
    """Is the void at this screw position elongated along the shaft?

    Measured, not declared: the generator can only make this true by actually
    cutting a slot. Walks +/-Z along the hole axis line and finds how far the
    material stays absent.
    """
    step = 0.25
    reach = 3.0 * hole_d
    extent = 0.0
    for direction in (-1.0, 1.0):
        d = step
        while d <= reach:
            if _x_intervals(mesh, 0.0, z_hole + direction * d):
                break
            d += step
        extent += d - step
    return extent > SLOT_ASPECT_THRESHOLD * hole_d


def _bone_gap_at(mesh, z: float, hole_d: float) -> float:
    """Standoff between the bone surface and the implant's bone-facing face.

    Sampled just outside the bore: a ray down the hole axis passes through the
    hole and would report the plate as absent.
    """
    bone_x = surface_x_at(z, 0.0, DEFAULT_BONE)
    if np.isnan(bone_x):
        return float("nan")
    for y in (0.0, hole_d, -hole_d):
        intervals = _x_intervals(mesh, y, z)
        if intervals:
            return min(x0 for x0, _ in intervals) - float(bone_x)
    return float("nan")


# --- checks ------------------------------------------------------------------


def check_bending(sections: list[Section], moments: dict[float, float], axial_n: float,
                  limit: float, x_ref: float) -> tuple[Check, dict[float, Section]]:
    by_z = {s.z: s for s in sections}
    worst, worst_sec = -float("inf"), None
    for sec in sections:
        s = section_stress(sec, moments[sec.z], axial_n)
        if s > worst:
            worst, worst_sec = s, sec

    if worst_sec is None:
        return (
            Check(
                id="stress_max_bending",
                status=FAIL,
                limit=limit,
                unit="MPa",
                message="no cross-section could be sampled -- the part may be empty",
            ),
            by_z,
        )

    ok = worst <= limit + 1e-6
    return (
        Check(
            id="stress_max_bending",
            status=PASS if ok else FAIL,
            value=round(worst, 1),
            limit=limit,
            unit="MPa",
            location=[round(x_ref, 3), 0.0, round(worst_sec.z, 1)],
            message=(
                "peak combined bending + axial stress within the allowable"
                if ok
                else f"peak stress {worst:.0f} MPa at z={worst_sec.z:.0f} mm, section "
                     f"modulus {worst_sec.modulus:.1f} mm^3. Stress scales with 1/S, so "
                     f"move material to where the moment is, rather than everywhere"
            ),
        ),
        by_z,
    )


def check_holes(mesh, sections_by_z: dict[float, Section], moments: dict[float, float],
                axial_n: float, limit: float, x_ref: float) -> list[Check]:
    checks = []
    for screw in _screws():
        idx = int(screw["index"])
        z = float(screw["entry_mm"][2])
        sec = sections_by_z.get(z)
        cid = f"stress_hole_{idx}"
        if sec is None:
            checks.append(
                Check(
                    id=cid,
                    status=FAIL,
                    limit=limit,
                    unit="MPa",
                    location=[round(x_ref, 3), 0.0, z],
                    message=f"no material at screw {idx} (z={z:.0f} mm) to carry load",
                )
            )
            continue

        slot = hole_is_slot(mesh, z, float(screw["diameter_mm"]))
        kt = KT_SLOT if slot else KT_ROUND_HOLE
        nominal = section_stress(sec, moments[z], axial_n)
        peak = kt * nominal
        ok = peak <= limit + 1e-6
        feature = "axial slot" if slot else "round hole"
        checks.append(
            Check(
                id=cid,
                status=PASS if ok else FAIL,
                value=round(peak, 1),
                limit=limit,
                unit="MPa",
                location=[round(x_ref, 3), 0.0, z],
                message=(
                    f"{feature}, Kt {kt:.2f} on a {nominal:.0f} MPa net section"
                    if ok
                    else f"{peak:.0f} MPa at screw {idx}: {nominal:.0f} MPa net-section "
                         f"stress raised by Kt {kt:.2f} ({feature}). Either carry less "
                         f"stress through this section or stop concentrating it -- an "
                         f"axial slot drops Kt to {KT_SLOT:.2f}"
                ),
            )
        )
    return checks


def check_pullout(mesh, limit: float) -> Check:
    """Thread shear on the engaged cortex, shortened by whatever standoff remains."""
    worst, worst_id, worst_gap = float("inf"), None, 0.0
    for screw in _screws():
        z = float(screw["entry_mm"][2])
        gap = _bone_gap_at(mesh, z, float(screw["diameter_mm"]))
        if np.isnan(gap):
            continue
        engaged = max(0.0, CORTEX_ENGAGEMENT_MM - max(gap, 0.0))
        force = BONE_THREAD_SHEAR_MPA * np.pi * float(screw["diameter_mm"]) * engaged
        if force < worst:
            worst, worst_id, worst_gap = force, screw["id"], gap

    if worst_id is None:
        return Check(
            id="screw_pullout_min",
            status=FAIL,
            limit=limit,
            unit="N",
            message="could not measure screw purchase at any planned position",
        )

    ok = worst >= limit - 1e-6
    return Check(
        id="screw_pullout_min",
        status=PASS if ok else FAIL,
        value=round(float(worst), 1),
        limit=limit,
        unit="N",
        message=(
            f"weakest purchase is {worst_id} at {worst:.0f} N"
            if ok
            else f"{worst_id} holds {worst:.0f} N against a {limit:.0f} N requirement: "
                 f"the plate stands {worst_gap:.2f} mm off the bone there, and every "
                 f"millimetre of standoff is a millimetre of screw not in cortex"
        ),
    )


# --- frozen entry point ------------------------------------------------------


def validate(implant_path: str, case: dict) -> Report:
    thresholds = case.get("thresholds", {})
    stress_limit = float(thresholds.get("max_stress_MPa", 350.0))
    pullout_limit = float(thresholds.get("min_screw_pullout_N", 1200.0))

    mesh = _load(implant_path)
    lo, hi = mesh.bounds

    peak_moment_nmm = 0.0
    axial_n = 0.0
    for load in case.get("load_cases", []):
        if load.get("type") == "bending":
            peak_moment_nmm += PLATE_BENDING_SHARE * float(load["moment_Nm"]) * 1000.0
        elif load.get("type") == "axial":
            axial_n += PLATE_AXIAL_SHARE * float(load["force_N"])

    screw_z = [float(s["entry_mm"][2]) for s in _screws()]
    z_first, z_last = min(screw_z), max(screw_z)

    z_start, z_end = float(lo[2]) + END_INSET_MM, float(hi[2]) - END_INSET_MM
    n_steps = max(2, round((z_end - z_start) / SECTION_STEP_MM) + 1)
    zs = sorted(set(np.linspace(z_start, z_end, n_steps).tolist()) | set(screw_z))

    sections = [s for s in (section_at(mesh, z) for z in zs) if s is not None]
    moments = {z: moment_at(z, z_first, z_last, peak_moment_nmm) for z in zs}

    x_ref = float(lo[0])
    bending, by_z = check_bending(sections, moments, axial_n, stress_limit, x_ref)
    checks = [bending]
    checks += check_holes(mesh, by_z, moments, axial_n, stress_limit, x_ref)
    checks.append(check_pullout(mesh, pullout_limit))

    return Report.from_checks(
        checks,
        meta={
            "validator": "stress",
            "model": "reduced-order analytical surrogate (beam + Kt + thread shear)",
            "plate_peak_moment_Nmm": round(peak_moment_nmm, 1),
            "plate_axial_N": round(axial_n, 1),
            "sections_sampled": len(sections),
        },
    )
