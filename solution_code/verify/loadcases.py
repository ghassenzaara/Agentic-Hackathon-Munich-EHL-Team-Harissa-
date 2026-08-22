"""What gets held and what gets pushed. Server-side, like materials.

A load case is expressed as *geometric selectors* rather than node numbers,
because the node numbering changes every time gmsh remeshes a revised design.
"the distal 2 mm of the part, held" survives a remesh; "nodes 1..847" does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["min", "max"]


@dataclass(frozen=True)
class Selector:
    """A slab of nodes at one end of one axis.

    `depth_mm` is the slab thickness. It should be at least one element so the
    set is never empty on a coarse mesh, which is a silent way to end up with an
    unconstrained model that CalculiX reports only as a singular-matrix warning.
    """

    axis: int  # 0=x, 1=y, 2=z
    side: Side
    depth_mm: float = 1e-6


@dataclass(frozen=True)
class LoadCase:
    """One boundary-value problem.

    `force_n` is the TOTAL force on the loaded set, split equally over its
    nodes. Equal nodal split is not a strictly uniform traction for quadratic
    tetrahedra -- the mid-side nodes should carry a different share -- so the
    stress within roughly one part-thickness of the loaded face is wrong. That
    is Saint-Venant's principle doing what it always does, and it is the reason
    `roi_exclusion` covers the loaded end as well as the held end.

    `dofs` says which degrees of freedom the fixed set loses. Full encastre
    (1,2,3) is the right model for the benchmark, where the analytical solution
    assumes exactly that. It is the WRONG model for screw fixation, and that is
    the known gap recorded below.
    """

    name: str
    fixed: Selector
    loaded: Selector
    force_n: tuple[float, float, float]
    dofs: tuple[int, ...] = (1, 2, 3)
    description: str = ""

    @property
    def force_magnitude(self) -> float:
        return float(sum(c * c for c in self.force_n)) ** 0.5


# ---------------------------------------------------------------------------
# the cases
# ---------------------------------------------------------------------------

# The regression benchmark. 100 x 10 x 5 mm bar along +x, held at x=0, 100 N
# transverse at x=100. Bending in the x-z plane, so I = b*h^3/12 with b=10 (y)
# and h=5 (z). Closed form: sigma = 240.0 MPa at the wall, delta = 2.8119 mm at
# the tip. See tests/test_cantilever.py -- the numbers are derived there, not
# copied, so a change to the material propagates instead of going stale.
CANTILEVER = LoadCase(
    name="cantilever",
    fixed=Selector(axis=0, side="min", depth_mm=1e-6),
    loaded=Selector(axis=0, side="max", depth_mm=1e-6),
    force_n=(0.0, 0.0, -100.0),
    description="closed-form regression benchmark, not a physiological case",
)

# Physiological: body weight through the tibial axis during stance. 400 N is
# the published static figure; the patient-specific case scales it by body
# weight and a gait factor, which is a multiplier on this vector and nothing
# else. Long axis is z once the anatomy pipeline has moved the bone into the
# canonical frame (step 4).
GAIT_AXIAL = LoadCase(
    name="gait_axial",
    fixed=Selector(axis=2, side="min", depth_mm=2.0),
    loaded=Selector(axis=2, side="max", depth_mm=2.0),
    force_n=(0.0, 0.0, -400.0),
    description="400 N axial through the proximal end, distal end held",
)

CASES = {c.name: c for c in (CANTILEVER, GAIT_AXIAL)}


# ---------------------------------------------------------------------------
# known gap -- stated here rather than in a document nobody opens
# ---------------------------------------------------------------------------
#
# BC SOFTENING IS NOT IMPLEMENTED. suggested_plan.md lists three mitigations for
# the clamp singularity; two of them are live (near-BC exclusion and ROI peak
# reporting, both in stress.py) and they are the two that actually stop the
# agent chasing a phantom hotspot. The third -- replacing the rigid encastre
# with grounded springs or a kinematic coupling -- needs SPRING1 elements
# generated per constrained node, which is real work and buys accuracy near the
# screws rather than correctness of the verdict.
#
# Consequence, stated so it is not discovered later: stress within the exclusion
# radius of a screw is not reported at all, rather than reported softly. If a
# design fails for a reason that lives entirely inside that radius, this
# verifier will pass it. Screw-region strength is the gap.
#
# An F382-style four-point bend case belongs here too. It is a different
# Selector pair and the same solve, so it is cheap; it is absent because the
# standard is paywalled and the span dimensions should come from the document
# rather than from memory.
