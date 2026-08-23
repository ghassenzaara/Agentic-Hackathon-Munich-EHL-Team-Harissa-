"""Per-case file resolution, so the pipeline is not hard-wired to ``inputs/``.

The repo shipped with one case, and every module reached for
``REPO_ROOT/inputs/<file>`` directly. That is fine for the synthetic demo and
wrong the moment a second case exists: a real CT-derived case lives in
``real_cases/<case_id>/`` with its own bone mesh, screws and keepouts.

``inputs/case.json`` already declared where its files live::

    "inputs": {
      "bone_mesh": "inputs/bone.stl",
      "screw_positions": "inputs/screw_positions.json",
      "keepout_zones": "inputs/keepout_zones.json"
    }

Nothing read those keys. This module makes them load-bearing, with the old
hard-coded paths as the fallback, so the synthetic path behaves exactly as
before.

``build_implant(params)`` has a frozen signature and never receives the case, so
the active case is also module state: ``run.py`` calls :func:`set_active_case`
once at startup and the generator picks it up. ``AUTOIMPLANTS_CASE`` does the
same job for a subprocess that never goes through ``run.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASE_PATH = REPO_ROOT / "inputs" / "case.json"

# Where each input lands when a case does not declare it. Exactly the paths the
# modules used to hard-code.
_FALLBACK_INPUTS = {
    "bone_mesh": "inputs/bone.stl",
    "screw_positions": "inputs/screw_positions.json",
    "keepout_zones": "inputs/keepout_zones.json",
}

_ACTIVE: dict | None = None


# -- active case --------------------------------------------------------------


def set_active_case(case: dict, path: str | Path | None = None) -> dict:
    """Make ``case`` the one the generator resolves files against.

    ``path`` is remembered as ``case["_case_path"]`` so relative input paths can
    be resolved against the case file's own directory.
    """
    global _ACTIVE
    case = dict(case)
    if path is not None:
        case["_case_path"] = str(Path(path).resolve())
    _ACTIVE = case
    return case


def active_case() -> dict:
    """The case set by ``run.py``, else ``$AUTOIMPLANTS_CASE``, else the default.

    Falls back to an empty case rather than raising: a caller with no case still
    gets the historical ``inputs/`` paths, which is what the synthetic demo wants.
    """
    if _ACTIVE is not None:
        return _ACTIVE

    env = os.environ.get("AUTOIMPLANTS_CASE")
    candidate = Path(env) if env else DEFAULT_CASE_PATH
    if candidate.exists():
        return set_active_case(load_case(candidate), candidate)
    return {}


def load_case(path: str | Path) -> dict:
    p = Path(path)
    case = json.loads(p.read_text(encoding="utf-8"))
    case["_case_path"] = str(p.resolve())
    return case


# -- path resolution ----------------------------------------------------------


def case_dir(case: dict | None = None) -> Path:
    """Directory holding the case file, for resolving its relative paths."""
    case = active_case() if case is None else case
    p = case.get("_case_path")
    return Path(p).parent if p else REPO_ROOT


def resolve(case: dict | None, key: str) -> Path:
    """Absolute path to input ``key`` for ``case``.

    A relative path is tried against the case file's directory first (how a
    self-contained ``real_cases/<id>/`` case refers to its own bone), then
    against the repo root (how ``inputs/case.json`` has always referred to
    ``inputs/bone.stl``). The repo-root reading is the default when neither
    exists, so the error message names the path a reader expects.
    """
    case = active_case() if case is None else case
    rel = (case.get("inputs") or {}).get(key) or _FALLBACK_INPUTS[key]

    candidate = Path(rel)
    if candidate.is_absolute():
        return candidate

    from_case = (case_dir(case) / candidate).resolve()
    if from_case.exists():
        return from_case
    return (REPO_ROOT / candidate).resolve()


def bone_path(case: dict | None = None) -> Path:
    return resolve(case, "bone_mesh")


def screws_path(case: dict | None = None) -> Path:
    return resolve(case, "screw_positions")


def keepouts_path(case: dict | None = None) -> Path:
    return resolve(case, "keepout_zones")


# -- input loading ------------------------------------------------------------


def load_screws(case: dict | None = None) -> list[dict]:
    """Planned screws, with every direction unit-normalised.

    Normalising here rather than at each use site means a validator can trust
    ``direction`` is a unit vector -- ray offsets and trajectory lengths are both
    wrong if it is not, and silently so.
    """
    data = json.loads(screws_path(case).read_text(encoding="utf-8"))
    screws = []
    for s in data["screws"]:
        s = dict(s)
        d = np.asarray(s["direction"], dtype=float)
        n = float(np.linalg.norm(d))
        if n < 1e-9:
            raise ValueError(f"screw {s.get('id')!r} has a zero-length direction")
        s["direction"] = (d / n).tolist()
        screws.append(s)
    return screws


def load_keepouts(case: dict | None = None) -> list[dict]:
    """Keepout zones. A case with no keepout file simply has none.

    A case that lists its inputs and does not list keepouts has none either: the
    repo-root fallback would otherwise hand the demo femur's zones to an unrelated
    case, so a cranial device would be checked against a femoral neurovascular
    corridor. Silent cross-case leakage is worse than a missing check.
    """
    case = active_case() if case is None else case
    declared = case.get("inputs") or {}
    if declared and "keepout_zones" not in declared:
        return []
    p = keepouts_path(case)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))["zones"]


def thresholds(case: dict | None = None) -> dict[str, Any]:
    case = active_case() if case is None else case
    return case.get("thresholds", {}) or {}
