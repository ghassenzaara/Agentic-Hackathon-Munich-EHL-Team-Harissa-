"""Export the CadQuery solid to the formats the rest of the pipeline consumes.

STL is what the validators measure, so the tessellation tolerance is tight enough
that a real 2.5 mm wall does not fail a 2.5 mm minimum on faceting error alone.
STEP is the artefact a manufacturer would actually receive -- exported because
"here is the part" is a slide, and because it costs nothing.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

STL_TOLERANCE = 0.02        # mm, linear deflection
STL_ANGULAR_TOLERANCE = 0.1  # rad


def export_implant(solid: cq.Workplane, stem: str | Path) -> Path:
    """Write <stem>.stl and <stem>.step. Returns the STL path."""
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)

    stl = stem.with_suffix(".stl")
    cq.exporters.export(
        solid,
        str(stl),
        tolerance=STL_TOLERANCE,
        angularTolerance=STL_ANGULAR_TOLERANCE,
    )

    step = stem.with_suffix(".step")
    try:
        cq.exporters.export(solid, str(step))
    except Exception as exc:  # never fail the loop over a nice-to-have artefact
        print(f"[warn] STEP export failed: {exc!r}")

    return stl
