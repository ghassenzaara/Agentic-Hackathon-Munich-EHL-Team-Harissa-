"""Run CalculiX, and refuse to return a number it does not believe.

CalculiX exits 0 on several conditions that make its output meaningless -- an
under-constrained model, a non-positive-definite matrix, a missing result block.
Everything that could turn one of those into a plausible verdict is checked here
rather than downstream.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .deck import write_deck
from .frd import FrdResult, read_frd
from .loadcases import LoadCase
from .materials import Material
from .mesh import FeMesh


class SolveError(RuntimeError):
    """The solve did not produce a result worth interpreting."""


@dataclass(frozen=True)
class SolveResult:
    """A completed static solve, aligned to the mesh's node order."""

    mesh: FeMesh
    case: LoadCase
    material: Material
    displacement: np.ndarray  # (n_nodes, 3) mm
    stress: np.ndarray  # (n_nodes, 6) MPa, order SXX SYY SZZ SXY SYZ SZX
    node_sets: dict[str, np.ndarray]
    elapsed_s: float
    workdir: Path

    @property
    def stiffness_n_per_mm(self) -> float:
        """Applied force over the deflection it produced, along the load axis.

        Measured on the loaded set only, projected onto the force direction, so
        a design that deflects sideways is not credited for being stiff.
        """
        f = np.asarray(self.case.force_n, dtype=float)
        mag = float(np.linalg.norm(f))
        if mag == 0.0:
            raise SolveError("load case applies no force; stiffness undefined")
        u = self.displacement[self.node_sets["loaded"]] @ (f / mag)
        travel = abs(float(u.mean()))
        if travel < 1e-12:
            raise SolveError("loaded set did not move; model is over-constrained")
        return mag / travel


def _find_ccx() -> str:
    ccx = shutil.which("ccx")
    if ccx is None:
        raise SolveError(
            "ccx not on PATH -- CalculiX is the verdict engine. `pixi run doctor`"
        )
    return ccx


def solve(
    mesh: FeMesh,
    material: Material,
    case: LoadCase,
    workdir: Path | str,
    job: str = "job",
    timeout_s: float = 900.0,
) -> SolveResult:
    """Mesh + material + load case -> displacements and stresses."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    inp = workdir / f"{job}.inp"
    node_sets = write_deck(inp, mesh, material, case)

    ccx = _find_ccx()
    t0 = time.perf_counter()
    proc = subprocess.run(
        [ccx, "-i", job],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    elapsed = time.perf_counter() - t0
    log = (proc.stdout or "") + (proc.stderr or "")
    (workdir / f"{job}.log").write_text(log)

    frd = workdir / f"{job}.frd"
    if proc.returncode != 0 or not frd.is_file():
        raise SolveError(
            f"ccx failed (exit {proc.returncode}) for {job}:\n{log[-2000:]}"
        )

    # ccx exits 0 while printing these. Treating them as success is how a
    # meaningless stress field reaches the verdict engine looking like a result.
    for marker in ("*ERROR", "nonpositive jacobian", "singular matrix"):
        if marker.lower() in log.lower():
            raise SolveError(f"ccx reported {marker!r} for {job}:\n{log[-2000:]}")

    result: FrdResult = read_frd(frd)
    try:
        disp = result.field("DISP")
        stress = result.field("STRESS")
    except KeyError as exc:
        raise SolveError(f"{frd.name} has no {exc} block; solve did not converge") from exc

    if len(disp) != mesh.n_nodes:
        raise SolveError(
            f"{len(disp)} result nodes vs {mesh.n_nodes} mesh nodes -- "
            "the .frd does not belong to this mesh"
        )
    if stress.shape[1] != 6:
        raise SolveError(f"expected 6 stress components, got {stress.shape[1]}")
    if not np.isfinite(disp).all() or not np.isfinite(stress).all():
        raise SolveError("non-finite values in the result field")

    return SolveResult(
        mesh=mesh,
        case=case,
        material=material,
        displacement=disp,
        stress=stress,
        node_sets=node_sets,
        elapsed_s=elapsed,
        workdir=workdir,
    )
