"""Fake FAILING validator -- the H+0:45 unblocking artifact.

Exists so the harness (C) and demo (D) lanes can build against a real Report
before any real geometry or stress model exists. Deliberately dependency-free
and deterministic.

It reproduces the failure shape from the project brief: a stress concentration
at screw hole 3, plus a bone-conformance failure the flat baseline plate really
will hit on a curved femur.

DELETE ME once geometry.py and stress.py both return real numbers.
"""

from __future__ import annotations

from ..contracts import FAIL, PASS, Check, Report


def validate(implant_path: str, case: dict) -> Report:
    thresholds = case.get("thresholds", {})
    stress_limit = thresholds.get("max_stress_MPa", 350.0)
    gap_limit = thresholds.get("max_bone_gap_mm", 1.5)
    min_wall = thresholds.get("min_wall_mm", 2.5)

    checks = [
        Check(
            id="manifold_watertight",
            status=PASS,
            unit="bool",
            message="exported solid is a closed manifold",
        ),
        Check(
            id="within_envelope",
            status=PASS,
            value=176.0,
            limit=180.0,
            unit="mm",
            message="plate length inside the allowed envelope",
        ),
        Check(
            id="min_wall_thickness",
            status=PASS,
            value=3.0,
            limit=min_wall,
            unit="mm",
            message="uniform 3.0 mm wall clears the minimum",
        ),
        Check(
            id="screw_positions_preserved",
            status=PASS,
            value=6,
            limit=6,
            unit="count",
            message="all 6 planned screw positions are accommodated",
        ),
        Check(
            id="bone_conformance_gap",
            status=FAIL,
            value=4.8,
            limit=gap_limit,
            unit="mm",
            location=[11.2, -3.4, 96.0],
            message=(
                "Flat plate stands off the curved femoral shaft by 4.8 mm at mid-diaphysis. "
                "The plate must follow the bone contour along its length."
            ),
        ),
        Check(
            id="stress_hole_3",
            status=FAIL,
            value=412.0,
            limit=stress_limit,
            unit="MPa",
            location=[12.0, 4.0, 88.0],
            message=(
                "Stress concentration at screw hole 3 exceeds limit under gait bending. "
                "Reduce the stress riser or add material local to the hole."
            ),
        ),
    ]

    return Report.from_checks(
        checks,
        meta={
            "validator": "stub",
            "STUB": True,
            "note": "hardcoded values -- not derived from the supplied geometry",
        },
    )
