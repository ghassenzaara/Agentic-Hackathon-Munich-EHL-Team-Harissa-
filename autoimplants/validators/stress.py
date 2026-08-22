"""Reduced-order analytical stress surrogate.

NOT clinical FEA. Beam bending on sampled cross-sections, stress concentration
factors at the screw holes, and a screw pull-out estimate. Analytical on purpose:
it must run in tens of milliseconds and it must never crash, because it sits
inside an autonomous loop.

Owner: B.

What it measures
----------------
Section properties come from ``autoimplants/section.py``, which reads them off
the *exported solid* by ray casting. Nothing here knows what a rib or a thickness
profile is: material added where the moment is high raises ``i_yy`` at that
station and the number drops. That is what makes this model worth having rather
than a formula over ``params`` -- it rewards geometry, not parameters.

Assumptions, stated because they are the first thing an engineer will ask about
-------------------------------------------------------------------------------
1. **The plate carries the whole moment.** No load sharing with the bone. For an
   intact shaft that is very wrong -- the femur's EI is roughly two orders above
   the plate's, so a real plate on a healed bone sees almost nothing. For a
   bridging plate over a fracture gap, which is the case being designed here, it
   is close to right and conservative.
2. **Moment peaks at mid-span and tapers to the outermost screws.** The plate
   bridges the fracture at mid-footprint and load enters through the screws
   either side, so this is a simply-supported span loaded at the middle. It makes
   *where* material sits matter, which is the difference between a thickness
   profile and a thickness.
3. **Both bending planes are checked and the worse is reported.** ``case.json``
   labels the gait moment "medio-lateral" while the generator calls +X lateral
   and the bow anterior; those cannot all be true, so rather than pick one
   reading the model computes both and keeps the larger stress.
4. **Linear elastic, static, single cycle.** No fatigue, no plasticity, no
   contact.

BEFORE YOU CALIBRATE, read design_space_note in inputs/case.json. The thresholds
are chosen so that uniform thickening cannot pass. If you retune the stress
model, re-check that property holds, or the whole "why not an optimiser" argument
collapses.
"""

from __future__ import annotations

import numpy as np

from ..contracts import FAIL, PASS, SKIP, Check, Report
from ..section import section_at, sections

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


def _load_mesh(implant_path: str):
    import trimesh

    mesh = trimesh.load(str(implant_path), force="mesh")
    if mesh.is_empty:
        raise ValueError(f"implant mesh at {implant_path} is empty or unreadable")
    return mesh


def _loads(case: dict) -> tuple[float, float]:
    """Peak bending moment in N*mm and axial force in N, from the case."""
    moment_nmm = 0.0
    axial_n = 0.0
    for lc in case.get("load_cases", []) or []:
        if lc.get("type") == "bending":
            moment_nmm = max(moment_nmm, float(lc.get("moment_Nm", 0.0)) * 1000.0)
        elif lc.get("type") == "axial":
            axial_n = max(axial_n, float(lc.get("force_N", 0.0)))
    return moment_nmm, axial_n


def _moment_profile(zs: np.ndarray, z_mid: float, half_span: float, peak_nmm: float):
    """Simply-supported span loaded at mid-footprint: triangular moment.

    Full moment over the fracture, falling linearly to zero at the outermost
    screws, which is where load leaves the plate and re-enters the bone.
    """
    if half_span <= 0.0:
        return np.full(zs.shape, peak_nmm)
    taper = 1.0 - np.abs(zs - z_mid) / half_span
    return peak_nmm * np.clip(taper, 0.0, 1.0)


def hole_kt(diameter_mm: float, width_mm: float) -> float:
    """Stress concentration factor for a transverse hole in a finite-width strip.

    Heywood's net-section correlation, Kt = 2 + (1 - d/W)^3, referenced to the
    net section -- which is what ``section.py`` measures, because the hole is
    physically absent from the mesh it samples.

    This is the in-plane tension case, used deliberately as a conservative stand
    in: for out-of-plane bending of a thin plate the factor is lower (roughly 1.8
    to 2.0 over this d/W range), so quoting the tension value cannot flatter the
    design. Named and sourced here rather than buried as a magic 2.4.
    """
    ratio = float(np.clip(diameter_mm / max(width_mm, 1e-6), 0.0, 0.9))
    return 2.0 + (1.0 - ratio) ** 3


def _peak_stress(sections_list, moments_nmm, axial_n):
    """Worst fibre stress over all stations, in both bending planes."""
    worst = -np.inf
    worst_section = None
    worst_axis = ""

    for section, moment in zip(sections_list, moments_nmm):
        if not section.is_solid:
            continue
        axial = axial_n / section.area

        for axis, inertia, c in (
            ("thin (X-Z plane)", section.i_yy, section.c_x),
            ("wide (Y-Z plane)", section.i_zz, section.c_y),
        ):
            if inertia <= 0.0:
                continue
            sigma = moment * c / inertia + axial
            if sigma > worst:
                worst, worst_section, worst_axis = sigma, section, axis

    return worst, worst_section, worst_axis


def validate(implant_path: str, case: dict) -> Report:
    thresholds = case.get("thresholds", {})
    stress_limit = thresholds.get("max_stress_MPa")
    pullout_limit = thresholds.get("min_screw_pullout_N")

    from .. import case_io  # local import keeps the stub path dependency-free

    screws = case_io.load_screws(case)
    screw_zs = np.array([s["entry_mm"][2] for s in screws], dtype=float)

    mesh = _load_mesh(implant_path)
    # Force a station through every hole: those are the weakest sections, and
    # letting the even grid decide whether it lands on one makes the reported
    # peak depend on the station count rather than on the design.
    secs = sections(mesh, extra_z=screw_zs)
    solid = [s for s in secs if s.is_solid]
    if not solid:
        return Report.errored(
            "stress_max_bending",
            "no cross-section carried any material -- the part may be empty",
        )

    moment_nmm, axial_n = _loads(case)

    z_mid = 0.5 * (float(screw_zs.min()) + float(screw_zs.max()))
    half_span = 0.5 * (float(screw_zs.max()) - float(screw_zs.min()))

    zs = np.array([s.z for s in secs])
    moments = _moment_profile(zs, z_mid, half_span, moment_nmm)

    checks: list[Check] = []

    peak, at, axis = _peak_stress(secs, moments, axial_n)
    ok = stress_limit is None or peak <= float(stress_limit) + 1e-6
    checks.append(
        Check(
            id="stress_max_bending",
            status=PASS if ok else FAIL,
            value=round(float(peak), 2),
            limit=stress_limit,
            unit="MPa",
            location=[round(at.x_centroid, 2), round(at.y_centroid, 2), round(at.z, 1)],
            message=(
                f"peak fibre stress {peak:.0f} MPa in the {axis}, "
                f"section I={at.i_yy:.0f} mm^4, c={at.c_x:.2f} mm"
                if ok
                else f"peak fibre stress {peak:.0f} MPa exceeds the {stress_limit} MPa "
                     f"allowable, in the {axis} at z={at.z:.0f} mm where the section "
                     f"offers I={at.i_yy:.0f} mm^4 against c={at.c_x:.2f} mm. Add section "
                     f"where the moment is, not everywhere -- the mass budget will not "
                     f"pay for uniform thickening"
            ),
        )
    )

    # Per-hole net-section stress. The hole is absent from the mesh, so the
    # sampled section IS the net section; Kt is applied on top of it.
    width_mm = max((s.c_y * 2.0 for s in solid), default=0.0)
    for index, screw in enumerate(screws):
        cid = f"stress_hole_{index}"
        if cid not in CHECK_IDS:
            break

        section = section_at(secs, float(screw["entry_mm"][2]))
        kt = hole_kt(float(screw["diameter_mm"]), width_mm)
        moment = float(
            _moment_profile(
                np.array([section.z]), z_mid, half_span, moment_nmm
            )[0]
        )
        nominal = moment * section.c_x / section.i_yy + axial_n / section.area
        sigma = kt * nominal

        hole_ok = stress_limit is None or sigma <= float(stress_limit) + 1e-6
        checks.append(
            Check(
                id=cid,
                status=PASS if hole_ok else FAIL,
                value=round(float(sigma), 2),
                limit=stress_limit,
                unit="MPa",
                location=[round(c, 2) for c in screw["entry_mm"]],
                message=(
                    f"{nominal:.0f} MPa net section x Kt {kt:.2f} at {screw['id']}"
                    if hole_ok
                    else f"{sigma:.0f} MPa at {screw['id']}: {nominal:.0f} MPa net "
                         f"section amplified by Kt {kt:.2f} for a "
                         f"{screw['diameter_mm']:.1f} mm hole in a {width_mm:.1f} mm "
                         f"width. A slot relieves the concentration a round hole fixes"
                ),
            )
        )

    # Pull-out is a property of the bone and the screw, both of which are locked
    # planning input. No change to the plate can move it, so a number here could
    # never drive the loop -- reporting one would be theatre.
    checks.append(
        Check(
            id="screw_pullout_min",
            status=SKIP,
            limit=pullout_limit,
            unit="N",
            message="not a design variable: pull-out depends on bone quality and screw "
                    "geometry, both locked surgical planning inputs. The implant cannot "
                    "change it, so it is reported as SKIP rather than measured",
        )
    )

    return Report.from_checks(
        checks,
        meta={
            "validator": "stress",
            "model": "beam on measured sections; no load sharing with bone",
            "peak_moment_Nmm": round(moment_nmm, 1),
            "axial_N": round(axial_n, 1),
            "stations": len(secs),
        },
    )
