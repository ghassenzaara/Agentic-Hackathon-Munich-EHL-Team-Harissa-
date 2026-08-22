"""Reduced-order analytical stress surrogate.

NOT clinical FEA. Beam bending on sampled cross-sections, stress concentration
factors at the screw holes, and a screw pull-out estimate. Analytical on purpose:
it must run in tens of milliseconds and it must never crash, because it sits
inside an autonomous loop.

Owner: B. This file currently emits the right check IDs with SKIP status so the
loop, the report schema, the viewer and the prompt are all wired end to end
before the physics lands. Fill in the bodies; do not change the IDs, because the
demo and the prompt refer to them.

BEFORE YOU CALIBRATE, read design_space_note in inputs/case.json. The thresholds
are chosen so that uniform thickening cannot pass -- the mass cap only allows
~+5% volume, worth a 1.11x stress reduction. If you retune the stress model,
re-check that property holds, or the whole "why not an optimiser" argument
collapses.
"""

from __future__ import annotations

from ..contracts import SKIP, Check, Report

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


def validate(implant_path: str, case: dict) -> Report:
    thresholds = case.get("thresholds", {})
    stress_limit = thresholds.get("max_stress_MPa")
    pullout_limit = thresholds.get("min_screw_pullout_N")

    checks = []
    for cid in CHECK_IDS:
        is_pullout = cid == "screw_pullout_min"
        checks.append(
            Check(
                id=cid,
                status=SKIP,
                limit=pullout_limit if is_pullout else stress_limit,
                unit="N" if is_pullout else "MPa",
                message="not implemented yet -- surrogate stress model pending (owner: B)",
            )
        )

    return Report.from_checks(
        checks, meta={"validator": "stress", "NOT_IMPLEMENTED": True}
    )
