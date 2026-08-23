"""Linear-elastic finite element analysis of the exported implant.

This is a real solve, not a formula: the exported solid is tetrahedralised and
the elasticity problem is assembled and solved on that mesh, so the stress field
reflects the actual geometry -- a fillet, a boss, a bore through a thin wall.
:mod:`autoimplants.validators.stress` cannot do that, and structurally never
could: it is beam theory on cross-sections stacked along one axis, which has no
meaning on a conformal patch over a scapular blade.

Why it exists
-------------
Every non-plate case reported stress as SKIP, which is honest but useless: a
device is accepted on geometry alone and the surgeon is shown no load answer at
all. A field solve gives a number for any topology and, as a by-product, a
per-vertex field the UI can draw as a heatmap -- which is the thing that makes a
stress result readable rather than a single scalar.

Formulation
-----------
Four-node constant-strain tetrahedra (Tet4), isotropic linear elasticity, static.
Tet4 is the simplest useful element and it is deliberately chosen: it is exact for
uniform stress states (see the patch test in the tests), it needs no quadrature
scheme to get right, and its stiffness has a closed form, so nothing here can
silently mis-integrate. It is also known to be *stiff* -- it under-predicts
displacement, and therefore peak stress, on coarse meshes in bending. That bias is
the reason the element size is tied to the wall thickness rather than to the part
size, and the reason a mesh-refinement figure is reported in the meta.

What is idealised, stated up front
----------------------------------
1. **The screws are rigid.** Nodes inside the bores of one screw group are held
   fixed. Real fixation is compliant and the bone yields; this concentrates
   stress at the fixed bores, which is conservative there and non-conservative
   nowhere.
2. **No contact, no bone.** The implant carries the whole load. Same assumption
   the beam surrogate makes, for the same reason: a bridging device over a defect
   is close to that, and load sharing would only reduce stress.
3. **Static, single cycle, no plasticity, no fatigue.** A peak von Mises under
   the allowable says nothing about a million gait cycles.
4. **The load path is inferred from the plan, not authored.** The screws are
   split into two groups along their own principal axis; one group is fixed and
   the case's moment and axial force are applied at the other. This is the
   cantilever reading of the same span the beam model treats as simply supported.

None of this is a certified analysis. It is an indicative solve, and the report
labels it as one.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import factorized

# Element size as a fraction of the thinnest wall the case allows. Two elements
# through the wall is the minimum that can represent bending through it at all;
# below that a Tet4 mesh reports a wall as a membrane and loses the peak.
ELEMENTS_THROUGH_WALL = 2.0
# Nodes within this multiple of a screw radius count as belonging to that bore.
BORE_CAPTURE = 1.35
# Each uniform refinement multiplies the element count by eight, so refinement is
# capped two ways: a step count, and an element budget it must not overshoot. The
# budget is what keeps the direct factorisation quick and inside memory -- ~25k
# tets is ~15k unknowns and a few seconds; a refinement past it is minutes and
# gigabytes, and this runs once per design iteration. A coarse answer that returns
# beats an exact one that gets OOM-killed mid-loop, and the size actually achieved
# is reported in the meta either way.
MAX_REFINEMENTS = 3
MAX_ELEMENTS = 25_000


def _read_mesh(gmsh_module) -> tuple[np.ndarray, np.ndarray]:
    """Current gmsh mesh as (nodes (N,3), tets (M,4)) with dense indices."""
    node_tags, coords, _ = gmsh_module.model.mesh.getNodes()
    nodes = np.asarray(coords, dtype=float).reshape(-1, 3)
    # gmsh tags are 1-based and may have gaps; map them onto row indices.
    index = np.full(int(node_tags.max()) + 1, -1, dtype=np.int64)
    index[np.asarray(node_tags, dtype=np.int64)] = np.arange(len(node_tags))

    types, _, connectivity = gmsh_module.model.mesh.getElements(3)
    tets = [
        index[np.asarray(conn, dtype=np.int64).reshape(-1, 4)]
        for etype, conn in zip(types, connectivity)
        if etype == 4  # 4-node tetrahedron
    ]
    return nodes, np.vstack(tets) if tets else np.zeros((0, 4), dtype=np.int64)


def _mean_edge_of(nodes: np.ndarray, tets: np.ndarray) -> float:
    """Mean tetrahedron edge length, the mesh's characteristic size."""
    if len(tets) == 0:
        return np.inf
    corners = nodes[tets]
    return float(
        np.linalg.norm(
            corners[:, [0, 0, 0, 1, 1, 2]] - corners[:, [1, 2, 3, 2, 3, 3]], axis=2
        ).mean()
    )


def tetrahedralize(
    surface_path: str | Path, target_mm: float
) -> tuple[np.ndarray, np.ndarray]:
    """Tet-mesh a closed triangle surface. Returns (nodes (N,3), tets (M,4)).

    gmsh keeps a discrete surface exactly as given and only honours the size field
    in the interior, so a CAD tessellation with one big triangle across a flat wall
    would pin that wall to a single element -- and one element carries no bending
    gradient. Uniform splitting is applied afterwards until the mesh is at the
    requested size, which is conforming by construction; subdividing the STL
    beforehand is not, and leaves gmsh with T-junctions it rejects.

    gmsh is used through its Python API rather than by shelling out, and its
    terminal output is captured: this runs inside an autonomous loop where stray
    stdout corrupts the report stream.
    """
    import gmsh

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.Verbosity", 0)
        with contextlib.redirect_stdout(io.StringIO()):
            gmsh.merge(str(surface_path))
            # The STL arrives as triangles with no topology. Deriving the boundary
            # topology is enough to bound a volume with it; reparametrising it onto
            # spline patches (classifySurfaces/createGeometry) is not, and fails
            # outright on a shell whose surface is one closed blob of curvature.
            gmsh.model.mesh.createTopology()
            surfaces = [tag for dim, tag in gmsh.model.getEntities(2)]
            loop = gmsh.model.geo.addSurfaceLoop(surfaces)
            gmsh.model.geo.addVolume([loop])
            gmsh.model.geo.synchronize()
            gmsh.option.setNumber("Mesh.MeshSizeMin", 0.5 * target_mm)
            gmsh.option.setNumber("Mesh.MeshSizeMax", target_mm)
            gmsh.option.setNumber("Mesh.Algorithm3D", 1)
            gmsh.model.mesh.generate(3)
            for _ in range(MAX_REFINEMENTS):
                nodes, tets = _read_mesh(gmsh)
                if _mean_edge_of(nodes, tets) <= target_mm:
                    break
                if len(tets) * 8 > MAX_ELEMENTS:
                    break
                gmsh.model.mesh.refine()

        nodes, tets = _read_mesh(gmsh)
    finally:
        gmsh.finalize()

    if len(tets) == 0:
        raise ValueError(
            "gmsh produced no tetrahedra: the surface is probably not a closed "
            "volume. Geometry validation runs first for exactly this reason."
        )
    return nodes, tets


def elastic_matrix(youngs_mpa: float, poisson: float) -> np.ndarray:
    """Isotropic stiffness in Voigt order (xx, yy, zz, yz, xz, xy)."""
    lam = youngs_mpa * poisson / ((1.0 + poisson) * (1.0 - 2.0 * poisson))
    mu = youngs_mpa / (2.0 * (1.0 + poisson))
    d = np.zeros((6, 6))
    d[:3, :3] = lam
    d[0, 0] = d[1, 1] = d[2, 2] = lam + 2.0 * mu
    d[3, 3] = d[4, 4] = d[5, 5] = mu
    return d


def _shape_gradients(
    nodes: np.ndarray, tets: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Per-element shape-function gradients (M,4,3) and volumes (M,)."""
    p = nodes[tets]
    edges = np.stack([p[:, 1] - p[:, 0], p[:, 2] - p[:, 0], p[:, 3] - p[:, 0]], axis=1)
    determinant = np.linalg.det(edges)
    # A negatively oriented tet is the same element with two nodes swapped: fix the
    # winding rather than carrying a signed volume through the assembly.
    flipped = determinant < 0.0
    if flipped.any():
        tets[flipped] = tets[flipped][:, [0, 2, 1, 3]]
        p = nodes[tets]
        edges = np.stack(
            [p[:, 1] - p[:, 0], p[:, 2] - p[:, 0], p[:, 3] - p[:, 0]], axis=1
        )
        determinant = np.linalg.det(edges)

    volumes = determinant / 6.0
    inverse = np.linalg.inv(edges)
    gradients = np.zeros((len(tets), 4, 3))
    # Natural coordinates are xi = A^-1 (x - p0), so grad(N_i) for i=1..3 are the
    # rows of A^-1 and grad(N_0) closes the partition of unity.
    gradients[:, 1:, :] = np.transpose(inverse, (0, 2, 1))
    gradients[:, 0, :] = -gradients[:, 1:, :].sum(axis=1)
    return gradients, volumes


def _strain_displacement(gradients: np.ndarray) -> np.ndarray:
    """B matrices (M,6,12) mapping nodal displacement to Voigt strain."""
    m = len(gradients)
    b = np.zeros((m, 6, 12))
    for node in range(4):
        gx, gy, gz = (gradients[:, node, k] for k in range(3))
        col = 3 * node
        b[:, 0, col + 0] = gx
        b[:, 1, col + 1] = gy
        b[:, 2, col + 2] = gz
        b[:, 3, col + 1] = gz
        b[:, 3, col + 2] = gy
        b[:, 4, col + 0] = gz
        b[:, 4, col + 2] = gx
        b[:, 5, col + 0] = gy
        b[:, 5, col + 1] = gx
    return b


def solve(
    nodes: np.ndarray,
    tets: np.ndarray,
    youngs_mpa: float,
    poisson: float,
    fixed: np.ndarray,
    load_cases: np.ndarray,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Static solve of every load case on one mesh.

    ``fixed`` are node indices held at zero displacement; ``load_cases`` is a
    (K,N,3) stack of nodal load vectors in N, one per case, and the result is one
    ``(displacement (N,3) in mm, von Mises per node in MPa)`` pair per case.

    Load cases are taken as a stack rather than one at a time because they share a
    stiffness matrix: factorising it is the whole cost of the solve, and each extra
    right-hand side is a back-substitution -- microseconds against seconds. Two
    bending planes therefore cost what one costs.
    """
    if not len(fixed):
        raise ValueError(
            "no node is restrained, so the part is free to fly: the screw groups "
            "did not resolve to any bore. Check that the plan's screws lie on the "
            "implant."
        )

    d = elastic_matrix(youngs_mpa, poisson)
    gradients, volumes = _shape_gradients(nodes, tets)
    b = _strain_displacement(gradients)
    # K_e = V * B^T D B, exactly: strain is constant over a Tet4.
    ke = volumes[:, None, None] * np.einsum("mki,kl,mlj->mij", b, d, b)

    dofs = (3 * tets[:, :, None] + np.arange(3)[None, None, :]).reshape(len(tets), 12)
    rows = np.repeat(dofs, 12, axis=1).ravel()
    cols = np.tile(dofs, (1, 12)).ravel()
    n_dof = 3 * len(nodes)
    stiffness = coo_matrix(
        (ke.ravel(), (rows, cols)), shape=(n_dof, n_dof)
    ).tocsr()

    free = np.ones(n_dof, dtype=bool)
    free[(3 * fixed[:, None] + np.arange(3)[None, :]).ravel()] = False
    solver = factorized(stiffness[free][:, free].tocsc())

    results = []
    # Accepts a single (N,3) case as well as a (K,N,3) stack.
    for forces in np.asarray(load_cases, dtype=float).reshape(-1, len(nodes), 3):
        displacement = np.zeros(n_dof)
        displacement[free] = solver(forces.ravel()[free])
        displacement = displacement.reshape(-1, 3)

        strain = np.einsum("mij,mj->mi", b, displacement[tets].reshape(len(tets), 12))
        stress = strain @ d.T
        results.append(
            (displacement, _nodal_von_mises(stress, tets, volumes, len(nodes)))
        )
    return results


def _nodal_von_mises(
    stress: np.ndarray, tets: np.ndarray, volumes: np.ndarray, n_nodes: int
) -> np.ndarray:
    """Element von Mises averaged onto nodes, weighted by element volume.

    Tet4 stress is constant per element, so a node-wise field needs averaging. The
    volume weight keeps a cloud of slivers from outvoting the elements that carry
    the load.
    """
    sxx, syy, szz, syz, sxz, sxy = stress.T
    per_element = np.sqrt(
        0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
        + 3.0 * (syz**2 + sxz**2 + sxy**2)
    )
    total = np.zeros(n_nodes)
    weight = np.zeros(n_nodes)
    np.add.at(total, tets.ravel(), np.repeat(per_element * volumes, 4))
    np.add.at(weight, tets.ravel(), np.repeat(volumes, 4))
    return np.where(weight > 0.0, total / np.maximum(weight, 1e-12), 0.0)


def bore_nodes(nodes: np.ndarray, screw: dict, margin_mm: float = 0.0) -> np.ndarray:
    """Node indices lying on the wall of one planned bore.

    ``margin_mm`` widens the cylinder, which is how the boundary-condition
    neighbourhood is taken: a rigid restraint and a nodal load are both singular at
    the nodes they act on and still wrong an element or two away, so the reported
    peak is read outside a dilated bore rather than outside the bore itself.

    The bore is a cylinder about the screw's own trajectory, so the test is
    distance from that axis within the drilled length -- the same description the
    generator drilled from, which keeps the load path tied to the plan rather than
    to whatever the mesh happens to look like.
    """
    entry = np.asarray(screw["entry_mm"], dtype=float)
    direction = np.asarray(screw["direction"], dtype=float)
    direction = direction / np.linalg.norm(direction)
    radius = 0.5 * float(screw["diameter_mm"])

    offset = nodes - entry
    along = offset @ direction
    radial = np.linalg.norm(offset - along[:, None] * direction, axis=1)
    length = float(screw.get("length_mm", 0.0)) or np.inf
    reach = BORE_CAPTURE * radius + margin_mm
    return np.flatnonzero(
        (radial <= reach) & (along >= -reach) & (along <= length + margin_mm)
    )


def load_path(
    nodes: np.ndarray, screws: list[dict]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Split the fixation into a restrained group and a loaded group.

    Returns ``(fixed nodes, loaded nodes, span axis, lever arm in mm)``.

    The axis is the direction the screws spread out along -- their principal
    component -- which is the span the device bridges whether or not the anatomy
    has a shaft. Screws below the median projection hold the device; the load is
    applied at the ones above it, a lever arm away. Nothing anatomical is assumed.
    """
    entries = np.array([s["entry_mm"] for s in screws], dtype=float)
    centred = entries - entries.mean(axis=0)
    axis = np.linalg.svd(centred, full_matrices=False)[2][0]
    projection = centred @ axis

    near = [s for s, t in zip(screws, projection) if t <= np.median(projection)]
    far = [s for s, t in zip(screws, projection) if t > np.median(projection)]
    if not near or not far:
        raise ValueError(
            "the planned screws do not separate into two groups along their own "
            "span, so there is no load path to solve: a single cluster of screws "
            "restrains the device everywhere it is loaded."
        )

    fixed = np.unique(np.concatenate([bore_nodes(nodes, s) for s in near]))
    loaded = np.unique(np.concatenate([bore_nodes(nodes, s) for s in far]))
    lever = abs(
        float(
            np.mean([s["entry_mm"] for s in far], axis=0) @ axis
            - np.mean([s["entry_mm"] for s in near], axis=0) @ axis
        )
    )
    return fixed, loaded, axis, lever


def boundary_zone(
    nodes: np.ndarray, screws: list[dict], margin_mm: float
) -> np.ndarray:
    """Nodes within ``margin_mm`` of any planned bore.

    The stress reported to the validator is the peak *outside* this zone. Inside it
    the field is dominated by the idealisations -- screws modelled as rigid, heads
    as nodal point loads -- so its magnitude tracks the mesh rather than the design,
    and iterating a design against it would chase the boundary conditions.
    """
    return np.unique(
        np.concatenate(
            [bore_nodes(nodes, s, margin_mm) for s in screws] + [np.zeros(0, int)]
        )
    )


def nodal_forces(
    n_nodes: int,
    loaded: np.ndarray,
    axis: np.ndarray,
    transverse: np.ndarray,
    moment_nmm: float,
    axial_n: float,
    lever_mm: float,
) -> np.ndarray:
    """Spread the case's loads over the loaded bores.

    A transverse force of ``moment / lever`` reproduces the case's peak moment at
    the restrained bores; the axial force is applied along the span. Both are
    divided equally over the loaded nodes, which smears the screw-head bearing
    pressure instead of pretending to model contact.
    """
    forces = np.zeros((n_nodes, 3))
    if not len(loaded):
        return forces
    resultant = axial_n * axis
    if lever_mm > 0.0 and moment_nmm > 0.0:
        resultant = resultant + (moment_nmm / lever_mm) * transverse
    forces[loaded] = resultant / len(loaded)
    return forces


def transverse_axes(axis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two directions perpendicular to the span, and to each other.

    Both are solved and the worse reported, for the reason the beam model gives:
    the case names a bending plane in anatomical terms that the mesh frame cannot
    confirm, so choosing one would be guessing.
    """
    seed = np.array([0.0, 0.0, 1.0])
    if abs(axis @ seed) > 0.9:
        seed = np.array([1.0, 0.0, 0.0])
    first = np.cross(axis, seed)
    first /= np.linalg.norm(first)
    return first, np.cross(axis, first)


def element_size(case: dict, mesh_extent_mm: float) -> float:
    """Target element size: wall-driven, with a floor so a solve always finishes."""
    wall = float(case.get("thresholds", {}).get("min_wall_mm", 2.0))
    target = wall / ELEMENTS_THROUGH_WALL
    return float(np.clip(target, 0.35, max(mesh_extent_mm / 25.0, 0.35)))
