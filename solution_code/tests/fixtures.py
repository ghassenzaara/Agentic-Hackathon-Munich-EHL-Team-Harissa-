"""Test geometry, built in code.

`*.step`, `*.stl`, `*.inp` and `*.frd` are all gitignored, so a committed binary
fixture is not an option. That constraint is a good one: geometry that is a
function reproduces exactly on the Devin machine snapshot, and a fixture cannot
drift away from the parameters a test asserts against.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cadquery as cq


@dataclass(frozen=True)
class Beam:
    """A rectangular bar along +x, with its closed-form solution attached.

    The analytical values are derived here from the dimensions and the material
    rather than hard-coded, so changing the beam or the material moves the
    expected answer with it instead of leaving a stale constant in a test.
    """

    length_mm: float = 100.0
    width_mm: float = 10.0  # b, along y
    height_mm: float = 5.0  # h, along z -- bending happens in x-z
    load_n: float = 100.0  # transverse, -z, at the free end

    @property
    def second_moment_mm4(self) -> float:
        """I = b*h^3/12 about the y axis."""
        return self.width_mm * self.height_mm**3 / 12.0

    def bending_stress_mpa(self, x_mm: float = 0.0) -> float:
        """sigma = M(x)*c/I, with M(x) = F*(L-x) for a tip-loaded cantilever.

        Parameterised by position because the peak this harness reports is not
        at the wall: the wall is inside the boundary-condition exclusion zone.
        Comparing a value measured at x to the closed form at x is the whole
        point -- comparing it to the value at 0 would build the exclusion
        distance into the tolerance and hide it.
        """
        moment = self.load_n * (self.length_mm - x_mm)
        return moment * (self.height_mm / 2.0) / self.second_moment_mm4

    def tip_deflection_mm(self, youngs_mpa: float) -> float:
        """delta = F*L^3 / (3*E*I). No singularity, so no exclusion needed --
        which makes this the cleanest single check that units and E are right."""
        return (
            self.load_n
            * self.length_mm**3
            / (3.0 * youngs_mpa * self.second_moment_mm4)
        )

    def to_step(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        solid = cq.Workplane("XY").box(
            self.length_mm, self.width_mm, self.height_mm, centered=(False, True, True)
        )
        cq.exporters.export(solid, str(path))
        return path


@dataclass(frozen=True)
class Slab:
    """A crude plate stand-in: a bar with a row of through holes.

    Not a real implant and not pretending to be one. It exists so the verdict
    engine has something plate-shaped to judge before the generator exists, and
    so `where` has real tagged faces to resolve a hotspot against.
    """

    length_mm: float = 140.0
    width_mm: float = 12.0
    thickness_mm: float = 3.5
    n_holes: int = 8
    hole_spacing_mm: float = 13.0
    hole_dia_mm: float = 3.5
    fillet_radius_mm: float = 1.0

    def to_step(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        wp = cq.Workplane("XY").box(
            self.length_mm, self.width_mm, self.thickness_mm,
            centered=(False, True, True),
        )
        span = (self.n_holes - 1) * self.hole_spacing_mm
        x0 = (self.length_mm - span) / 2.0
        for i in range(self.n_holes):
            wp = (
                wp.faces(">Z")
                .workplane(centerOption="CenterOfBoundBox")
                .center(x0 + i * self.hole_spacing_mm - self.length_mm / 2.0, 0)
                .hole(self.hole_dia_mm)
            )
        cq.exporters.export(wp, str(path))
        return path
