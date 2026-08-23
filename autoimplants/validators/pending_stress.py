"""Explicitly unavailable stress-validation coverage for the live demo.

The repository also contains an analytical research surrogate in ``stress.py``.
That surrogate is not the locked stress-validation lane promised by the product,
so the live surgeon workflow must not present its output as enforced evidence.
Every promised stress check therefore remains visibly SKIP until that lane is
qualified and deliberately enabled.
"""

from __future__ import annotations

from ..contracts import SKIP, Check, Report
from .stress import CHECK_IDS


def validate(implant_path: str, case: dict) -> Report:
    thresholds = case.get("thresholds", {})
    checks = []
    for check_id in CHECK_IDS:
        pullout = check_id == "screw_pullout_min"
        checks.append(
            Check(
                id=check_id,
                status=SKIP,
                limit=thresholds.get(
                    "min_screw_pullout_N" if pullout else "max_stress_MPa"
                ),
                unit="N" if pullout else "MPa",
                message=(
                    "stress validation is not connected to the live workflow; "
                    "this check is intentionally visible as SKIP"
                ),
            )
        )
    return Report.from_checks(
        checks,
        meta={"validator": "pending_stress", "NOT_IMPLEMENTED": True},
    )
