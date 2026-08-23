"""Validator registry and dispatch.

Every validator obeys the frozen signature::

    validate(implant_path: str, case: dict) -> Report

Validators are imported lazily so that ``--validators stub`` works on any
interpreter with no third-party packages installed at all. That is deliberate:
the harness and demo lanes must never be blocked on the CAD toolchain.

A validator that raises becomes an ERROR check, not a traceback. The loop must
not be killable by one bad geometry.
"""

from __future__ import annotations

import importlib
import time

from ..contracts import Report

REGISTRY = {
    "stub": "autoimplants.validators.stub",
    "geometry": "autoimplants.validators.geometry",
    "stress": "autoimplants.validators.stress",
    "fea": "autoimplants.validators.fea",
    "pending_stress": "autoimplants.validators.pending_stress",
}

# The order the real loop runs them in: cheap and deterministic first, so an
# implant that is not even a valid solid never reaches the stress model.
DEFAULT_ORDER = ("geometry", "stress")


def run_one(name: str, implant_path: str, case: dict) -> Report:
    try:
        module = importlib.import_module(REGISTRY[name])
    except KeyError:
        return Report.errored(f"{name}_unknown", f"no validator registered as {name!r}")
    except Exception as exc:  # missing dependency, syntax error in a teammate's file
        return Report.errored(f"{name}_import", f"could not import {name}: {exc!r}")

    started = time.perf_counter()
    try:
        report = module.validate(implant_path, case)
    except Exception as exc:
        return Report.errored(f"{name}_crashed", f"{name}.validate() raised: {exc!r}")

    report.meta.setdefault(f"{name}_ms", round((time.perf_counter() - started) * 1000, 1))
    return report


def run_all(
    implant_path: str,
    case: dict,
    names: tuple[str, ...] = DEFAULT_ORDER,
    iteration: int = 0,
    params: dict | None = None,
) -> Report:
    """Run validators in order and merge into a single verdict."""
    reports = [run_one(n, implant_path, case) for n in names]
    merged = Report.merge(reports, iteration=iteration, params=params or {})
    merged.meta["validators"] = list(names)
    merged.meta["implant_path"] = implant_path
    return merged
