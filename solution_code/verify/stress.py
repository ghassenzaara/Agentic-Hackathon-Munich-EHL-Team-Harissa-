"""von Mises, and the difference between a hotspot and an artefact.

A rigid boundary condition creates a stress singularity. The true elastic
solution at a perfectly clamped corner is unbounded, so the finite-element value
there does not converge -- it climbs with every refinement, forever. Our own
cantilever run showed the wall value going 275.6 -> 294.9 -> 329.0 MPa while the
peak away from the wall settled at 244.4 against a 240 MPa closed-form answer.

Both numbers come out of the same solve. Reporting the first one to an agent
tells it to reinforce a place that cannot be reinforced, because the stress
there is a property of the boundary condition rather than of the design. It
would iterate forever and the failure would look like a design problem.

So the peak that counts is measured over a region of interest with the
constrained and loaded neighbourhoods removed. The global peak is still reported
alongside, labelled, because the gap between the two is the evidence that the
exclusion is doing something.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from .solve import SolveResult


def von_mises(stress: np.ndarray) -> np.ndarray:
    """(n, 6) -> (n,). Component order SXX SYY SZZ SXY SYZ SZX, as CalculiX writes it."""
    s = np.asarray(stress, dtype=float)
    if s.ndim != 2 or s.shape[1] != 6:
        raise ValueError(f"expected (n, 6) stress, got {s.shape}")
    sxx, syy, szz, sxy, syz, szx = s.T
    return np.sqrt(
        0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
        + 3.0 * (sxy**2 + syz**2 + szx**2)
    )


@dataclass(frozen=True)
class Hotspot:
    """Where the design is most loaded, and how far that is from a boundary."""

    von_mises_mpa: float
    node: int
    xyz: tuple[float, float, float]
    distance_to_bc_mm: float


@dataclass(frozen=True)
class StressField:
    """The interpreted result: what the agent is allowed to act on."""

    vm: np.ndarray  # (n_nodes,) MPa
    roi: np.ndarray  # boolean mask, True = trustworthy
    hotspot: Hotspot  # peak within the ROI
    global_peak_mpa: float  # peak anywhere, including the singularity
    exclusion_radius_mm: float

    @property
    def artefact_ratio(self) -> float:
        """global / ROI peak. Near 1.0 means the exclusion changed nothing.

        Well above 1.0 means a boundary singularity was suppressed, which is the
        expected and healthy case for any clamped model.
        """
        return self.global_peak_mpa / max(self.hotspot.von_mises_mpa, 1e-12)


def analyse(
    result: SolveResult,
    exclusion_element_lengths: float = 2.0,
) -> StressField:
    """Compute von Mises and locate the peak that is worth reporting.

    Both the constrained set and the loaded set are excluded. The constrained
    one for the singularity; the loaded one because a total force split equally
    over face nodes is not a real traction distribution, so stress within about
    one part-thickness of it is a property of how the load was applied.
    """
    vm = von_mises(result.stress)
    pts = result.mesh.points
    h = result.mesh.element_length_mm
    radius = exclusion_element_lengths * h

    boundary = np.unique(
        np.concatenate([result.node_sets["fixed"], result.node_sets["loaded"]])
    )
    dist_to_bc, _ = cKDTree(pts[boundary]).query(pts, k=1)

    roi = dist_to_bc > radius
    if not roi.any():
        # A part smaller than the exclusion radius. Fail loudly: silently
        # falling back to the global peak would report the singularity as the
        # verdict, which is the exact outcome this module exists to prevent.
        raise ValueError(
            f"exclusion radius {radius:.3g} mm covers the whole part "
            f"({result.mesh.n_nodes} nodes, element length {h:.3g} mm). "
            "Mesh finer or shorten the constrained span."
        )

    idx = np.flatnonzero(roi)[int(np.argmax(vm[roi]))]
    return StressField(
        vm=vm,
        roi=roi,
        hotspot=Hotspot(
            von_mises_mpa=float(vm[idx]),
            node=int(idx),
            xyz=tuple(float(c) for c in pts[idx]),
            distance_to_bc_mm=float(dist_to_bc[idx]),
        ),
        global_peak_mpa=float(vm.max()),
        exclusion_radius_mm=float(radius),
    )
