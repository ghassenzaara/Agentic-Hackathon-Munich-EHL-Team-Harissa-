"""STEP solid -> second-order tetrahedral mesh.

Second-order (C3D10) is not configurable. Linear tets lock in bending: they are
artificially stiff, they underpredict stress, and they do it worst at curved
features -- which in a fixation plate means the screw-hole fillets, i.e. exactly
where the design actually fails. A linear-tet run returns a plausible number
that is wrong in the unsafe direction, which is the single worst failure mode
this harness can have. So there is no `order` argument.

Node ordering: gmsh's tet10 differs from Abaqus C3D10 in the last two mid-side
nodes. meshio's gmsh reader applies that permutation and its canonical ordering
matches C3D10, so the mesh goes gmsh -> .msh -> meshio and never through the
gmsh Python API's raw connectivity. The detour is deliberate.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

import gmsh
import meshio
import numpy as np


@dataclass(frozen=True)
class FeMesh:
    """Nodes and C3D10 connectivity, in millimetres."""

    points: np.ndarray  # (n_nodes, 3)
    tets: np.ndarray  # (n_elems, 10), 0-based, Abaqus C3D10 ordering
    target_size_mm: float

    @property
    def n_nodes(self) -> int:
        return len(self.points)

    @property
    def n_elems(self) -> int:
        return len(self.tets)

    @property
    def element_length_mm(self) -> float:
        """Measured mean corner-to-corner edge length.

        Measured rather than assumed: gmsh treats `target_size_mm` as a request
        and curvature refinement makes the real mesh finer. The exclusion radius
        in stress.py is a multiple of this, so it has to be the truth.
        """
        c = self.points[self.tets[:, :4]]  # (n, 4, 3) corners only
        pairs = [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
        edges = np.concatenate([c[:, i] - c[:, j] for i, j in pairs])
        return float(np.linalg.norm(edges, axis=1).mean())

    def select(self, axis: int, side: str, depth_mm: float) -> np.ndarray:
        """Node indices in a slab at one end of `axis`.

        Never returns empty: if the requested depth catches nothing (a
        zero-depth selector on a mesh whose nodes miss the exact plane by a
        float epsilon) the slab widens to one element. An empty fixed set is an
        unconstrained model, which CalculiX reports as a warning buried in a log
        rather than as an error.
        """
        col = self.points[:, axis]
        extreme = col.min() if side == "min" else col.max()
        depth = max(depth_mm, 1e-9)
        for _ in range(8):
            sel = (
                np.flatnonzero(col <= extreme + depth)
                if side == "min"
                else np.flatnonzero(col >= extreme - depth)
            )
            if sel.size:
                return sel
            depth = max(depth * 4, self.element_length_mm)
        raise RuntimeError(f"no nodes within {depth} mm of {side} of axis {axis}")


@contextlib.contextmanager
def _gmsh_session(verbose: bool = False):
    """gmsh is a global singleton; leaking it poisons the next solve."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 1 if verbose else 0)
        yield
    finally:
        gmsh.finalize()


def mesh_step(
    step_path: Path | str,
    size_mm: float = 1.0,
    msh_out: Path | str | None = None,
    curvature_elements: int = 12,
    verbose: bool = False,
) -> FeMesh:
    """Mesh a STEP solid to C3D10 tets at roughly `size_mm`.

    `curvature_elements` is how the "refined near holes" requirement is met
    without hand-tagging anything: gmsh puts that many elements around a full
    turn of curvature, so a 3.5 mm screw hole and a 1 mm fillet get a fine mesh
    automatically and the flat shaft does not. Set to 0 for a uniform mesh --
    the convergence test uses that, since a curvature-driven mesh does not
    refine uniformly and would confound the study.
    """
    step_path = Path(step_path)
    if not step_path.is_file():
        raise FileNotFoundError(step_path)
    msh_out = Path(msh_out) if msh_out else step_path.with_suffix(".msh")

    with _gmsh_session(verbose):
        gmsh.model.add(step_path.stem)
        gmsh.model.occ.importShapes(str(step_path))
        gmsh.model.occ.synchronize()

        if not gmsh.model.getEntities(3):
            raise ValueError(f"{step_path} contains no 3D solid to mesh")

        gmsh.option.setNumber("Mesh.MeshSizeMax", size_mm)
        gmsh.option.setNumber("Mesh.MeshSizeMin", size_mm / 4.0)
        if curvature_elements:
            gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", curvature_elements)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.ElementOrder", 2)
        # Incomplete second order drops the mid-face nodes C3D10 requires.
        gmsh.option.setNumber("Mesh.SecondOrderIncomplete", 0)
        gmsh.option.setNumber("Mesh.Optimize", 1)
        gmsh.option.setNumber("Mesh.OptimizeNetgen", 0)

        gmsh.model.mesh.generate(3)
        gmsh.model.mesh.setOrder(2)
        gmsh.write(str(msh_out))

    m = meshio.read(msh_out)
    tets = [b.data for b in m.cells if b.type == "tetra10"]
    if not tets:
        found = sorted({b.type for b in m.cells})
        raise RuntimeError(f"no tetra10 cells in {msh_out}; got {found}")

    return FeMesh(
        points=np.asarray(m.points, dtype=float),
        tets=np.concatenate(tets).astype(np.int64),
        target_size_mm=size_mm,
    )
