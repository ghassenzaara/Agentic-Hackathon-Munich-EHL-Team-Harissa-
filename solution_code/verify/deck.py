"""FeMesh + Material + LoadCase -> a CalculiX .inp deck.

Written by hand rather than by meshio's Abaqus writer because the deck is more
than a mesh: it carries the node sets, the material, the boundary conditions and
the output requests, and those are the parts the verdict depends on. meshio
writes nodes and elements; the other four are where a silent mistake changes the
answer without changing the shape.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .loadcases import LoadCase
from .materials import Material
from .mesh import FeMesh

# CalculiX indexes nodes and elements from 1.
_OFFSET = 1


def _nset(name: str, ids: np.ndarray) -> list[str]:
    """*NSET block, 8 ids per line as CalculiX's reader expects."""
    out = [f"*NSET, NSET={name}"]
    ids = np.asarray(ids) + _OFFSET
    for start in range(0, len(ids), 8):
        out.append(", ".join(str(int(v)) for v in ids[start : start + 8]))
    return out


def write_deck(
    path: Path | str,
    mesh: FeMesh,
    material: Material,
    case: LoadCase,
) -> dict[str, np.ndarray]:
    """Write the deck; return the node sets so the reader can reuse them.

    Returning the sets matters: stress.py needs to know exactly which nodes were
    constrained in order to exclude the singularity around them, and rederiving
    that from the same selectors twice is how the two copies drift apart.
    """
    path = Path(path)
    fixed = mesh.select(case.fixed.axis, case.fixed.side, case.fixed.depth_mm)
    loaded = mesh.select(case.loaded.axis, case.loaded.side, case.loaded.depth_mm)

    # Total force split equally over the loaded nodes. See the Saint-Venant note
    # in loadcases.LoadCase: this is why the loaded end is also excluded from
    # the region of interest.
    per_node = [c / len(loaded) for c in case.force_n]

    L: list[str] = [
        f"** {case.name}: {case.description}",
        f"** units N-mm-MPa-t, material {material.name}",
        "*NODE, NSET=NALL",
    ]
    for i, (x, y, z) in enumerate(mesh.points):
        L.append(f"{i + _OFFSET}, {x:.9g}, {y:.9g}, {z:.9g}")

    L.append("*ELEMENT, TYPE=C3D10, ELSET=EALL")
    for e, conn in enumerate(mesh.tets):
        ids = ", ".join(str(int(c) + _OFFSET) for c in conn)
        L.append(f"{e + _OFFSET}, {ids}")

    L += _nset("NFIX", fixed)
    L += _nset("NLOAD", loaded)

    L += [
        f"*MATERIAL, NAME={material.name.replace('-', '')}",
        "*ELASTIC, TYPE=ISO",
        f"{material.youngs_mpa:.6g}, {material.poisson:.6g}",
        "*DENSITY",
        f"{material.density_t_per_mm3:.6g}",
        f"*SOLID SECTION, ELSET=EALL, MATERIAL={material.name.replace('-', '')}",
        "*STEP",
        "*STATIC",
        "*BOUNDARY",
    ]
    for dof in case.dofs:
        L.append(f"NFIX, {dof}, {dof}, 0.0")

    L.append("*CLOAD")
    for dof, f in enumerate(per_node, start=1):
        if f:
            L.append(f"NLOAD, {dof}, {f:.9g}")

    L += [
        "*NODE FILE",
        "U",
        # Nodal stress, extrapolated from the integration points. The
        # extrapolation is what makes the peak slightly overshoot on a coarse
        # mesh, and what the convergence study measures.
        "*EL FILE",
        "S",
        "*END STEP",
        "",
    ]

    path.write_text("\n".join(L))
    return {"fixed": fixed, "loaded": loaded}
