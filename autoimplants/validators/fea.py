"""Finite element stress check, and the field the UI draws as a heatmap.

Owner: this validator replaces SKIP with a number on any topology, which is what
the beam surrogate in :mod:`autoimplants.validators.stress` cannot do off a shaft
axis. See :mod:`autoimplants.fea` for the formulation and the list of
idealisations -- they matter, and the check message repeats the important one.

It reports:

``fea_max_von_mises``
    peak nodal von Mises against the case's allowable.
``fea_peak_displacement``
    peak deflection, against ``max_deflection_mm`` when the case sets one. A
    device can be under yield and still be uselessly floppy over a defect.

and writes ``stress_field.json`` next to the implant: the surface triangles plus a
per-vertex stress value, which is what the viewer colours. The field is the
deliverable as much as the scalar is -- a single peak says pass or fail, the field
says *where*, which is what a surgeon or a design iteration can act on.

SKIP, not PASS, when the case declares no ``load_cases``: with no load the field
is zero everywhere and a heatmap of it would be theatre.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree

from .. import case_io, fea
from ..contracts import ERROR, FAIL, PASS, SKIP, Check, Report
from .stress import _loads

CHECK_IDS = ("fea_max_von_mises", "fea_peak_displacement")
FIELD_NAME = "stress_field.json"
# Ti-6Al-4V, used only if the case omits its own material block.
DEFAULT_YOUNGS_GPA = 114.0
DEFAULT_POISSON = 0.34
# How far from a bore, in element sizes, the reported peak must be read.
BOUNDARY_MARGIN_ELEMENTS = 2.0


def _skipped(reason: str) -> Report:
    return Report.from_checks(
        [
            Check(
                id=cid,
                status=SKIP,
                unit="MPa" if cid == "fea_max_von_mises" else "mm",
                message=reason,
            )
            for cid in CHECK_IDS
        ],
        meta={"validator": "fea", "skipped": reason},
    )


def _write_field(
    path: Path, surface: trimesh.Trimesh, values: np.ndarray, allowable: float
) -> None:
    """Per-vertex stress on the surface mesh, in the shape the viewer reads."""
    path.write_text(
        json.dumps(
            {
                "unit": "MPa",
                "allowable_MPa": allowable,
                "max_MPa": float(values.max()),
                "vertices": np.round(surface.vertices, 4).ravel().tolist(),
                "faces": surface.faces.ravel().tolist(),
                "von_mises_MPa": np.round(values, 3).tolist(),
                "note": (
                    "indicative linear-static FEA, no contact, no bone, no fatigue"
                ),
            }
        ),
        encoding="utf-8",
    )


def _errored(reason: str) -> Report:
    """A solver that cannot run is an ERROR, never a silent pass or skip.

    Meshing and factorisation can fail on a geometry the design step produced --
    a sliver, a self-intersection, a shell that is not closed. That is a real
    finding about the candidate and it has to reach the loop as one, so the
    exception is turned into checks rather than killing the run.
    """
    return Report.from_checks(
        [
            Check(
                id=cid,
                status=ERROR,
                unit="MPa" if cid == "fea_max_von_mises" else "mm",
                message=reason,
            )
            for cid in CHECK_IDS
        ],
        meta={"validator": "fea", "failed": reason},
    )


def validate(implant_path: str, case: dict) -> Report:
    moment_nmm, axial_n = _loads(case)
    if moment_nmm <= 0.0 and axial_n <= 0.0:
        return _skipped(
            "the case declares no load_cases, so there is no load to apply -- "
            "stress is unevaluated, not satisfied"
        )

    screws = case_io.load_screws(case)
    if len(screws) < 2:
        return _skipped(
            "fewer than two planned screws, so no load path between a fixed and a "
            "loaded group can be resolved"
        )

    material = case.get("material", {})
    youngs_mpa = float(material.get("youngs_modulus_GPa", DEFAULT_YOUNGS_GPA)) * 1000.0
    poisson = float(material.get("poisson_ratio", DEFAULT_POISSON))
    allowable = case.get("thresholds", {}).get("max_stress_MPa")

    surface = trimesh.load(implant_path, force="mesh")
    target = fea.element_size(case, float(np.ptp(surface.bounds, axis=0).max()))
    try:
        nodes, tets = fea.tetrahedralize(implant_path, target)
        fixed, loaded, axis, lever = fea.load_path(nodes, screws)
    except (RuntimeError, ValueError) as exc:
        return _errored(f"the solid could not be solved: {exc}")

    # The nodes where displacement is imposed and where point loads land carry a
    # boundary-condition singularity: their stress rises with every refinement and
    # never converges, because the idealisation is a rigid screw, not a screw. That
    # error does not stop at the bore wall, so the peak is read outside a bore
    # dilated by a couple of elements; read any closer and the reported number
    # tracks the restraints instead of the section, which makes it useless to
    # iterate against. The full field, artefacts included, is still written out, so
    # the heatmap does not quietly hide where the restraints are.
    away = np.setdiff1d(
        np.arange(len(nodes)),
        fea.boundary_zone(nodes, screws, BOUNDARY_MARGIN_ELEMENTS * target),
    )
    if len(away) == 0:
        return _skipped(
            "every node lies in a screw bore, so there is no unrestrained section "
            "whose stress would mean anything"
        )

    # Both bending planes, worse reported: the case names its moment axis
    # anatomically and the mesh frame cannot confirm which direction that is. Both
    # go into one solve call, which shares the factorisation between them.
    plane_loads = [
        fea.nodal_forces(
            len(nodes), loaded, axis, transverse, moment_nmm, axial_n, lever
        )
        for transverse in fea.transverse_axes(axis)
    ]
    try:
        solved = fea.solve(
            nodes, tets, youngs_mpa, poisson, fixed, np.stack(plane_loads)
        )
    except (RuntimeError, ValueError) as exc:
        return _errored(f"the stiffness system could not be solved: {exc}")

    worst_stress = -np.inf
    worst_field = np.zeros(len(nodes))
    worst_node = int(away[0])
    worst_displacement = 0.0
    for displacement, field in solved:
        peak = float(field[away].max())
        if peak > worst_stress:
            worst_stress = peak
            worst_field = field
            worst_node = int(away[int(np.argmax(field[away]))])
            worst_displacement = float(np.linalg.norm(displacement, axis=1).max())

    # Report the field on the exported surface the viewer already loads, so the
    # heatmap and the downloadable STL are the same geometry.
    surface_field = worst_field[cKDTree(nodes).query(surface.vertices)[1]]
    field_path = Path(implant_path).with_name(FIELD_NAME)
    _write_field(field_path, surface, surface_field, float(allowable or 0.0))

    checks = []
    if allowable is None:
        checks.append(
            Check(
                id="fea_max_von_mises",
                status=SKIP,
                value=worst_stress,
                unit="MPa",
                message=(
                    f"peak von Mises is {worst_stress:.1f} MPa but the case sets no "
                    f"max_stress_MPa to judge it against"
                ),
            )
        )
    else:
        limit = float(allowable)
        checks.append(
            Check(
                id="fea_max_von_mises",
                status=PASS if worst_stress <= limit else FAIL,
                value=worst_stress,
                limit=limit,
                unit="MPa",
                location=[float(v) for v in nodes[worst_node]],
                message=(
                    f"peak von Mises {worst_stress:.1f} MPa against {limit:.0f} MPa "
                    f"allowable, from an indicative linear-static solve: rigid "
                    f"screws, no bone load sharing, no fatigue"
                ),
            )
        )

    deflection_limit = case.get("thresholds", {}).get("max_deflection_mm")
    checks.append(
        Check(
            id="fea_peak_displacement",
            status=(
                SKIP
                if deflection_limit is None
                else (
                    PASS
                    if worst_displacement <= float(deflection_limit)
                    else FAIL
                )
            ),
            value=worst_displacement,
            limit=None if deflection_limit is None else float(deflection_limit),
            unit="mm",
            message=(
                f"peak deflection {worst_displacement:.3f} mm"
                + (
                    " -- the case sets no max_deflection_mm"
                    if deflection_limit is None
                    else ""
                )
            ),
        )
    )

    return Report.from_checks(
        checks,
        meta={
            "validator": "fea",
            "nodes": int(len(nodes)),
            "elements": int(len(tets)),
            "element_size_mm": round(target, 3),
            "lever_arm_mm": round(lever, 2),
            "fixed_nodes": int(len(fixed)),
            "loaded_nodes": int(len(loaded)),
            "stress_field": FIELD_NAME,
            "peak_read": (
                f"outside every bore by {BOUNDARY_MARGIN_ELEMENTS:g} elements "
                f"(boundary-condition singularity)"
            ),
            "model": "Tet4 linear elastic, static, indicative -- not certified",
        },
    )
