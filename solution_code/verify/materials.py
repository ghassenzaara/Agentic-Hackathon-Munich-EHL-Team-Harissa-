"""Materials, and the unit convention everything else obeys.

Server-side by design. The verifier is the test suite; a candidate that could
choose its own material or yield strength could pass by declaring itself
stronger. Nothing in a request body reaches this module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Material:
    """Linear-elastic isotropic, in N-mm-MPa-t.

    `density_t_per_mm3` is the consistent-unit density (tonne/mm^3), which is
    what CalculiX needs and what looks wrong to everyone reading it for the
    first time: 4.43 g/cm^3 is 4.43e-9 t/mm^3. Getting this wrong by 1e9 is the
    canonical FEA unit bug, so the g/cm^3 value is carried alongside and
    `verify.geometry` derives mass from that one instead of rescaling here.
    """

    name: str
    youngs_mpa: float
    poisson: float
    yield_mpa: float
    density_g_per_cm3: float

    @property
    def density_t_per_mm3(self) -> float:
        return self.density_g_per_cm3 * 1e-9


# Grade 5 titanium, the standard for load-bearing orthopedic hardware and for
# the DMLS route in step 8. Values as used in the published fixation-plate FEA
# the seed parameters come from.
TI6AL4V = Material(
    name="Ti-6Al-4V",
    youngs_mpa=113_800.0,
    poisson=0.342,
    yield_mpa=880.0,
    density_g_per_cm3=4.43,
)

DEFAULT_MATERIAL = TI6AL4V
