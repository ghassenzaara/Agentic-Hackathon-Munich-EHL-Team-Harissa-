"""Every shape that crosses a module boundary. Frozen first, edited last.

Four teams build against this file instead of against each other: the anatomy
pipeline produces `BoneCase`, the generator produces `PlateCandidate`, the
verifier consumes both and returns `Verdict`, the orchestrator reads `Verdict`
and ranks. Nobody needs anybody else's implementation to start.

`Verdict` is also the Devin session contract. `verdict_json_schema()` emits the
Draft-7 document that goes into the v3 create-session `structured_output_schema`
field, so the verifier's output and the agent's expected output are generated
from one definition and cannot drift apart.

UNITS. Newton, millimetre, megapascal, tonne -- everywhere, no exceptions. This
is the standard consistent set for CalculiX and it is the single most common way
an FEA harness returns a confidently wrong number. Derived: density in t/mm^3
(Ti-6Al-4V = 4.43e-9), stress in MPa = N/mm^2, stiffness in N/mm. Mass is
reported in grams because that is how the budget is written, and the conversion
happens in exactly one place (`verify.geometry.mass_g`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np

SCHEMA_VERSION = "1.0.0"
UNIT_SYSTEM = "N-mm-MPa-t"

Bound = Literal["min", "max", "window"]


# --------------------------------------------------------------------------
# the verdict
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One constraint, evaluated. Never a bare boolean.

    `measured` and `limit` are what make a failure actionable: the agent learns
    how far off it is and in which direction, not merely that it failed.
    `where` is the face tag the generator recorded, so a stress hotspot resolves
    to `fillet_radius_hole_3` rather than to a coordinate nobody can act on.
    """

    name: str
    passed: bool
    measured: float
    limit: float | tuple[float, float]
    bound: Bound
    units: str
    where: str | None = None
    xyz: tuple[float, float, float] | None = None

    @classmethod
    def against(
        cls,
        name: str,
        measured: float,
        limit: float | tuple[float, float],
        bound: Bound,
        units: str,
        where: str | None = None,
        xyz: tuple[float, float, float] | None = None,
    ) -> Check:
        """Build a Check with `passed` derived, never passed in.

        A hand-set `passed` that disagrees with `measured` vs `limit` is a bug
        class this constructor removes outright.
        """
        if bound == "window":
            lo, hi = limit  # type: ignore[misc]
            ok = lo <= measured <= hi
            limit = (float(lo), float(hi))
        elif bound == "min":
            ok = measured >= limit  # type: ignore[operator]
        elif bound == "max":
            ok = measured <= limit  # type: ignore[operator]
        else:  # pragma: no cover - guarded by the Literal
            raise ValueError(f"unknown bound {bound!r}")
        return cls(name, bool(ok), float(measured), limit, bound, units, where, xyz)

    @property
    def margin(self) -> float:
        """Signed slack in `units`. Positive is inside the bound.

        This is the number a search steers on: it turns a pass/fail into a
        gradient direction without exposing the threshold itself as a knob.
        """
        if self.bound == "window":
            lo, hi = self.limit  # type: ignore[misc]
            return min(self.measured - lo, hi - self.measured)
        if self.bound == "min":
            return self.measured - self.limit  # type: ignore[operator]
        return self.limit - self.measured  # type: ignore[operator]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "measured": self.measured,
            "limit": list(self.limit) if self.bound == "window" else self.limit,
            "bound": self.bound,
            "units": self.units,
            "margin": self.margin,
            "where": self.where,
            "xyz": list(self.xyz) if self.xyz else None,
        }


@dataclass(frozen=True)
class Verdict:
    """The complete answer to "is this plate any good?".

    `stage` says how far the candidate got. A candidate rejected by the geometry
    gate never reached the solver, so `metrics` carries no stress figures -- the
    consumer must branch on `stage`, not assume every key is present.
    """

    case_id: str
    candidate_id: str
    passed: bool
    stage: Literal["geometry", "fea"]
    checks: tuple[Check, ...]
    metrics: dict[str, float]
    mesh_convergence: dict[str, Any] = field(default_factory=dict)
    advice: tuple[str, ...] = ()
    elapsed_s: float = 0.0
    schema_version: str = SCHEMA_VERSION

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed)

    @property
    def binding(self) -> Check | None:
        """The check with the least slack -- the one to fix first.

        Among failures if any exist, otherwise among passes, where it names the
        constraint that would break next.
        """
        pool = self.failures or self.checks
        return min(pool, key=lambda c: c.margin) if pool else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "candidate_id": self.candidate_id,
            "passed": self.passed,
            "stage": self.stage,
            "checks": [c.to_dict() for c in self.checks],
            "metrics": self.metrics,
            "mesh_convergence": self.mesh_convergence,
            "advice": list(self.advice),
            "elapsed_s": round(self.elapsed_s, 3),
        }


# --------------------------------------------------------------------------
# acceptance thresholds -- the verdict, not a budget knob
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    """What "passing" means. Devin reads these; Devin cannot set them.

    Every entry that could be gamed from one side is two-sided. The stiffness
    window is the important one: with a floor alone, the cheapest route to a
    safety factor of 2.5 is a solid brick, and a search will find that route
    within a handful of iterations. The ceiling exists because an over-stiff
    plate shields the bone from load, and the bone resorbs and refractures --
    a real failure mode, not a scoring trick.
    """

    min_safety_factor: float = 2.5
    stiffness_window_n_per_mm: tuple[float, float] = (150.0, 900.0)
    max_mass_g: float = 40.0
    max_thickness_mm: float = 6.0
    clearance_window_mm: tuple[float, float] = (0.1, 1.0)
    min_screw_purchase_mm: float = 4.0
    max_overhang_deg: float = 45.0
    min_feature_mm: float = 1.0

    # Solver-side, not design-side: how far from a constrained node an element
    # must be before its stress is allowed to count. See verify/stress.py.
    bc_exclusion_element_lengths: float = 2.0


DEFAULT_THRESHOLDS = Thresholds()


# --------------------------------------------------------------------------
# anatomy -> verifier  (steps 1-4)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BoneCase:
    """A patient, cleaned and aligned, ready to design against.

    `transform` maps the source mesh into the canonical frame; every downstream
    position ("40 mm distal, lateral surface") is expressed in that frame, which
    is what makes a plate written for one anatomy meaningful on the next.
    """

    case_id: str
    mesh_path: Path
    transform: np.ndarray  # (4, 4) source -> canonical
    landmarks: dict[str, tuple[float, float, float]]
    quality: dict[str, Any]  # watertight, n_components, volume_mm3, ...

    def __post_init__(self) -> None:
        if self.transform.shape != (4, 4):
            raise ValueError(f"transform must be 4x4, got {self.transform.shape}")


# --------------------------------------------------------------------------
# generator -> verifier  (step 5)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PlateParams:
    """Seed values from published fixation-plate FEA; ranges in `PARAM_RANGES`.

    Deliberately NOT the whole design space. If the generator is a pure function
    of these eight floats then the agent has nothing to do that scipy could not
    do better, and the interesting half of the claim collapses. These are the
    coefficients; topology -- ribs, split screw columns, a rerouted path -- lives
    in the generator source that the agent edits.
    """

    length_mm: float = 140.0
    width_mm: float = 12.0
    thickness_mm: float = 3.5
    n_holes: int = 8
    hole_spacing_mm: float = 13.0
    hole_dia_mm: float = 3.5
    clearance_mm: float = 0.2
    fillet_radius_mm: float = 1.0


PARAM_RANGES: dict[str, tuple[float, float]] = {
    "length_mm": (80.0, 190.0),
    "width_mm": (10.0, 14.0),
    "thickness_mm": (3.0, 5.0),
    "n_holes": (6, 12),
    "hole_spacing_mm": (10.0, 16.0),
    "clearance_mm": (0.1, 1.0),
    "fillet_radius_mm": (0.5, 3.0),
    # hole_dia_mm is fixed by the screw: 3.5 locking / 4.5 compression.
}


@dataclass(frozen=True)
class FaceTags:
    """Which parameter produced which piece of surface.

    Stored as centroids rather than face indices on purpose: the solid gets
    remeshed by gmsh before the solver ever sees it, so indices do not survive
    the trip. Centroids do, and a nearest-point lookup from a stress hotspot
    back to a tag is what converts an FEA result into an edit.
    """

    centroids: np.ndarray  # (n, 3)
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.labels) != len(self.centroids):
            raise ValueError(
                f"{len(self.centroids)} centroids vs {len(self.labels)} labels"
            )

    def nearest(self, xyz) -> str | None:
        """Tag of the tagged face closest to `xyz`, or None if untagged."""
        if not self.labels:
            return None
        d = np.linalg.norm(self.centroids - np.asarray(xyz, dtype=float), axis=1)
        return self.labels[int(np.argmin(d))]

    @classmethod
    def empty(cls) -> FaceTags:
        return cls(np.zeros((0, 3)), ())


@dataclass(frozen=True)
class PlateCandidate:
    """One point in the design space, as a solid on disk plus its provenance."""

    candidate_id: str
    step_path: Path
    params: PlateParams
    tags: FaceTags = field(default_factory=FaceTags.empty)


# --------------------------------------------------------------------------
# orchestrator and artifacts  (steps 7-9) -- consumed, not yet produced
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionHandle:
    """One Devin session working one candidate. See devin-api-setup.md."""

    session_id: str
    url: str
    case_id: str
    candidate_id: str
    tags: tuple[str, ...] = ()
    max_acu: float | None = None


@dataclass(frozen=True)
class ArtifactSet:
    """What a converged candidate leaves behind."""

    case_id: str
    candidate_id: str
    stl: Path | None = None
    gcode: Path | None = None
    report: Path | None = None
    history: Path | None = None


# --------------------------------------------------------------------------
# JSON Schema for the Devin API
# --------------------------------------------------------------------------

_CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name", "passed", "measured", "limit", "bound", "units", "margin"],
    "properties": {
        "name": {"type": "string"},
        "passed": {"type": "boolean"},
        "measured": {"type": "number"},
        "limit": {
            "oneOf": [
                {"type": "number"},
                {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
            ]
        },
        "bound": {"enum": ["min", "max", "window"]},
        "units": {"type": "string"},
        "margin": {"type": "number", "description": "signed slack; positive is inside"},
        "where": {"type": ["string", "null"], "description": "generator face tag"},
        "xyz": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 3,
            "maxItems": 3,
        },
    },
}


def verdict_json_schema() -> dict[str, Any]:
    """Draft-7 for the Devin v3 `structured_output_schema` field.

    Generated from the same definition the verifier serialises, so the agent's
    expected output and the verifier's actual output stay in step.
    """
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "PlateVerdict",
        "type": "object",
        "required": ["schema_version", "case_id", "candidate_id", "passed", "stage", "checks", "metrics"],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "case_id": {"type": "string"},
            "candidate_id": {"type": "string"},
            "passed": {"type": "boolean"},
            "stage": {
                "enum": ["geometry", "fea"],
                "description": "'geometry' means the cheap gate rejected it before any solve",
            },
            "checks": {"type": "array", "items": _CHECK_SCHEMA},
            "metrics": {
                "type": "object",
                "description": f"units: {UNIT_SYSTEM}, mass in g",
                "additionalProperties": {"type": "number"},
            },
            "mesh_convergence": {"type": "object"},
            "advice": {"type": "array", "items": {"type": "string"}},
            "elapsed_s": {"type": "number"},
        },
    }
