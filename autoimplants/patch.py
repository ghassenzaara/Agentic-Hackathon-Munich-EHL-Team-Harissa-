"""The anatomy-agnostic implant family: a conformal shell over a bone region.

The plate in :mod:`autoimplants.generator` sweeps a section along one axis, which
is why it only makes sense on a long bone. Most patient-specific implants are not
that shape -- a cranial patch, a scapular or acetabular reconstruction, a
mandibular onlay -- and what they have in common is not an axis but a *surface*:
the device is the bone's own surface over some region, offset outward by a wall,
closed at the rim, and pierced by the planned screws.

That is what this module builds, straight off the imported bone mesh:

    region        -> the faces of the bone the plan asks the device to cover
    inner surface -> those faces offset along their vertex normals by the
                     periosteal clearance
    outer surface -> offset again by the wall thickness (which may vary)
    rim           -> quads stitching the two boundary loops into one solid
    screws        -> the planned trajectories cut through it

Nothing here names a bone or assumes a direction, so the same code produces a
femoral onlay and a cranial patch; the anatomy arrives entirely as mesh plus plan.

The result is converted to an OCC solid at the end because the rest of the
pipeline (export, STEP, validators) speaks CadQuery, and because a sewn solid is
what a manufacturer can actually receive.
"""

from __future__ import annotations

import cadquery as cq
import numpy as np
import trimesh
from scipy.spatial import Delaunay, cKDTree

from . import case_io, self_intersection

# A rim quad per boundary edge is enough: the boundary follows bone triangles, so
# its edges are already at mesh resolution.
MIN_REGION_FACES = 12
# Normal-smoothing passes tried, in order, when offsetting the sheet. The first
# one that yields a shell not passing through itself wins: smoothing is what
# unfolds a crease, but every pass also pulls the inner face away from the bone,
# so the least that works is the right amount. See build_shell.
SMOOTHING_LADDER = (3, 6, 12, 24)
# Sheet vertices closer together than this are one vertex. See _weld.
WELD_TOL_MM = 0.05
# How square-on to the fixation a bone face has to be to count as a seat: 60
# degrees. Anything steeper is a cut wall or a fossa side, and wrapping the shell
# around it creates a crease no offset survives (see _facing_screws).
SEAT_COS = 0.5


def _bone_mesh(case: dict | None = None) -> trimesh.Trimesh:
    mesh = trimesh.load(case_io.bone_path(case), process=False)
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.dump(concatenate=True)
    # STL stores every triangle independently, so a freshly loaded bone has no
    # shared vertices and therefore no connectivity: region selection would return
    # a cloud of loose triangles, and every one of them would be its own
    # "component". Merging restores the surface this module is built on.
    mesh.merge_vertices()
    return mesh


def region_faces(bone: trimesh.Trimesh, region: dict, screws: list[dict]) -> np.ndarray:
    """Indices of the bone faces the device covers.

    Two ways to say it, both plan-authored rather than hard-coded:

    ``{"type": "sphere", "center_mm": [...], "radius_mm": r}``
        the region around a point -- how a defect is described.

    ``{"type": "screw_span", "margin_mm": m}`` (the default)
        every bone point within ``m`` of the planned screw entries. This is the
        general reading of "cover the fixation": it needs no extra authoring, and
        it follows whatever pattern the surgeon planned, on any anatomy.
    """
    kind = region.get("type", "screw_span")
    if kind == "sphere":
        center = np.asarray(region["center_mm"], dtype=float)
        radius = float(region["radius_mm"])
        near = np.linalg.norm(bone.vertices - center, axis=1) <= radius
    elif kind == "screw_span":
        if not screws:
            raise ValueError(
                "region type 'screw_span' needs planned screws to span. Either "
                "the plan carries screws or it declares an explicit sphere region."
            )
        entries = np.array([s["entry_mm"] for s in screws], dtype=float)
        margin = float(region.get("margin_mm", 12.0))
        distance = np.linalg.norm(
            bone.vertices[:, None, :] - entries[None, :, :], axis=2
        ).min(axis=1)
        near = distance <= margin
    else:
        raise ValueError(
            f"unknown region type {kind!r}; the plan must declare 'sphere' or "
            f"'screw_span'"
        )

    faces = np.flatnonzero(near[bone.faces].all(axis=1))
    faces = _facing_screws(bone, faces, screws)
    if len(faces) < MIN_REGION_FACES:
        raise ValueError(
            f"the declared region covers only {len(faces)} bone faces, too little "
            f"surface to build a shell on. Widen the region, or check that the "
            f"plan and the bone mesh share one coordinate frame."
        )
    return faces


def _facing_screws(
    bone: trimesh.Trimesh, faces: np.ndarray, screws: list[dict]
) -> np.ndarray:
    """Drop faces the device cannot lie on, given where the screws come from.

    A distance criterion is blind to which *side* of the bone it selects: on a
    cranial vault, a margin around the screws reaches the inner table 6 mm below
    them, and on a rib it reaches the far cortex. Building over both sides yields a
    shell that intersects itself and reports a zero wall.

    A screw is driven from outside inwards, so the surface the device seats on is
    the one whose outward normal opposes the screw direction. That is the only
    orientation information in the plan, and it needs no anatomy-specific frame.

    Merely opposing is not enough: the wall left by a resection opposes the screws
    too, at 60-80 degrees to the surface they enter, and taking it into the region
    wraps the device's inner face around the rim. The fold that produces is what
    reports a 0.5 mm wall on a 1.8 mm design, so the criterion is squareness rather
    than sign.
    """
    if not screws:
        return faces
    directions = np.array([s["direction"] for s in screws], dtype=float)
    centers = bone.triangles_center[faces]
    entries = np.array([s["entry_mm"] for s in screws], dtype=float)
    nearest = np.argmin(
        np.linalg.norm(centers[:, None, :] - entries[None, :, :], axis=2), axis=1
    )
    outward = np.einsum("ij,ij->i", bone.face_normals[faces], -directions[nearest])
    return faces[outward > SEAT_COS]


def _largest_component(sub: trimesh.Trimesh) -> trimesh.Trimesh:
    """One contiguous patch.

    A radius around scattered screw entries can select surface on two sides of a
    bone -- or on the bone behind it. Building a shell over both halves would
    produce a device that cannot be placed, so only the largest connected sheet
    is kept.
    """
    pieces = sub.split(only_watertight=False)
    if len(pieces) <= 1:
        return sub
    return max(pieces, key=lambda piece: piece.area)


def _boundary_edges(mesh: trimesh.Trimesh) -> np.ndarray:
    """Edges used by exactly one face -- the rim of an open sheet."""
    unique, counts = np.unique(mesh.edges_sorted, axis=0, return_counts=True)
    return unique[counts == 1]


def _boundary_loops(edges: np.ndarray) -> list[list[int]]:
    """Boundary edges walked into closed vertex loops."""
    adjacency: dict[int, list[int]] = {}
    for a, b in edges:
        adjacency.setdefault(int(a), []).append(int(b))
        adjacency.setdefault(int(b), []).append(int(a))

    loops, seen = [], set()
    for start in adjacency:
        if start in seen:
            continue
        loop, current, previous = [start], start, None
        seen.add(start)
        while True:
            nexts = [v for v in adjacency[current] if v != previous and v not in seen]
            if not nexts:
                break
            previous, current = current, nexts[0]
            seen.add(current)
            loop.append(current)
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def _local_frame(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Origin and 3x3 basis (two in-plane axes, then the normal) fitted to points."""
    origin = points.mean(axis=0)
    _, _, basis = np.linalg.svd(points - origin, full_matrices=False)
    return origin, basis  # rows: largest spread, second, normal


def _fit_quadric(uv: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Least-squares coefficients of w = f(u, v), quadratic in u and v.

    Quadratic, not spherical: anatomy is as often saddle-shaped as domed (a
    scapular blade, an acetabular wall), and a sphere fitted to a saddle bulges
    the wrong way across the middle of the hole -- precisely where a
    reconstruction has to be right.
    """
    u, v = uv[:, 0], uv[:, 1]
    basis = np.column_stack([np.ones_like(u), u, v, u * u, u * v, v * v])
    coefficients, *_ = np.linalg.lstsq(basis, w, rcond=None)
    return coefficients


def _quadric_at(coefficients: np.ndarray, uv: np.ndarray) -> np.ndarray:
    u, v = uv[:, 0], uv[:, 1]
    return np.column_stack(
        [np.ones_like(u), u, v, u * u, u * v, v * v]
    ) @ coefficients


def _inside(polygon: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Crossing-number test: which points lie inside the closed 2D polygon."""
    a = polygon
    b = np.roll(polygon, -1, axis=0)
    px, py = points[:, 0][:, None], points[:, 1][:, None]
    straddles = (a[:, 1] > py) != (b[:, 1] > py)
    with np.errstate(divide="ignore", invalid="ignore"):
        x_at_y = a[:, 0] + (py - a[:, 1]) * (b[:, 0] - a[:, 0]) / (b[:, 1] - a[:, 1])
    return (straddles & (px < x_at_y)).sum(axis=1) % 2 == 1


def _cap(sheet: trimesh.Trimesh, loop: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """New vertices and faces spanning one interior loop, following the bone.

    The loop is flattened onto its own best-fit plane, filled with a Delaunay mesh
    at the bone's own resolution, and every new vertex is lifted back onto a
    quadric fitted to the bone *around* the hole. A fan from a single apex -- the
    cheap alternative -- degenerates on a ragged resection boundary: the triangles
    become slivers, and offsetting slivers along their vertex normals pinches the
    wall to nothing.
    """
    ring = sheet.vertices[loop]
    origin, basis = _local_frame(ring)

    span = float(np.linalg.norm(ring - ring.mean(axis=0), axis=1).max())
    near = sheet.vertices[
        np.linalg.norm(sheet.vertices - ring.mean(axis=0), axis=1) <= 2.0 * span
    ]
    local = (np.vstack([ring, near]) - origin) @ basis.T
    coefficients = _fit_quadric(local[:, :2], local[:, 2])

    ring_uv = (ring - origin) @ basis.T
    step = max(
        float(np.linalg.norm(np.diff(ring_uv[:, :2], axis=0, append=ring_uv[:1, :2]),
                             axis=1).mean()),
        1e-3,
    )
    lo, hi = ring_uv[:, :2].min(axis=0), ring_uv[:, :2].max(axis=0)
    # A coarse bone mesh gives a coarse loop, and a span triangulated from the loop
    # alone is a flat lid across the defect however good the fitted surface is. Take
    # a few rows across the opening regardless of how far apart the rim points are.
    step = min(step, float((hi - lo).max()) / 5.0)
    grid = np.stack(
        np.meshgrid(
            np.arange(lo[0], hi[0] + step, step),
            np.arange(lo[1], hi[1] + step, step),
            indexing="ij",
        ),
        axis=-1,
    ).reshape(-1, 2)
    # Keep interior samples clear of the boundary, so the triangulation near the
    # rim is driven by the loop itself rather than by near-coincident points.
    keep = _inside(ring_uv[:, :2], grid)
    if keep.any():
        keep &= cKDTree(ring_uv[:, :2]).query(grid)[0] > 0.6 * step
    interior_uv = grid[keep]

    uv = np.vstack([ring_uv[:, :2], interior_uv])
    triangles = Delaunay(uv).simplices
    centroids = uv[triangles].mean(axis=1)
    triangles = triangles[_inside(ring_uv[:, :2], centroids)]

    lifted = origin + np.column_stack(
        [interior_uv, _quadric_at(coefficients, interior_uv)]
    ) @ basis
    # Loop vertices keep their existing indices; interior points are appended.
    index = np.concatenate(
        [np.array(loop, dtype=int), len(sheet.vertices) + np.arange(len(interior_uv))]
    )
    return lifted, index[triangles]


def close_interior_holes(sheet: trimesh.Trimesh) -> trimesh.Trimesh:
    """Span holes inside the region, leaving the outer rim open.

    A defect is a hole in the bone, so the region selected around it is an annulus.
    Offsetting an annulus gives a device with a hole exactly where the bone is
    missing -- the opposite of a reconstruction. The interior loops are therefore
    triangulated across, following the curvature fitted to the bone around each
    hole, while the outer rim stays open for the wall to close against.
    """
    loops = _boundary_loops(_boundary_edges(sheet))
    if len(loops) < 2:
        return sheet

    def perimeter(loop: list[int]) -> float:
        ring = sheet.vertices[loop + [loop[0]]]
        return float(np.linalg.norm(np.diff(ring, axis=0), axis=1).sum())

    loops.sort(key=perimeter, reverse=True)
    filled = sheet
    for loop in loops[1:]:  # loops[0] is the outer rim
        vertices, faces = _cap(filled, loop)
        filled = trimesh.Trimesh(
            vertices=np.vstack([filled.vertices, vertices]),
            faces=np.vstack([filled.faces, faces]),
            process=False,
        )
    filled.fix_normals()
    return filled


def _wall(spec, points: np.ndarray, screw_entries: np.ndarray) -> np.ndarray:
    """Per-vertex wall thickness.

    ``spec`` is either a number (uniform wall) or
    ``{"base_mm", "boss_mm", "boss_radius_mm"}``: thicker around each planned
    screw, because that is where the section is lost to the bore and where the
    head bears. The same reason the plate has hole bosses, expressed on a surface
    instead of along an axis.
    """
    if not isinstance(spec, dict):
        return np.full(len(points), float(spec))

    base = float(spec["base_mm"])
    boss = float(spec.get("boss_mm", 0.0))
    radius = float(spec.get("boss_radius_mm", 0.0))
    thickness = np.full(len(points), base)
    if boss > 0.0 and radius > 0.0 and len(screw_entries):
        distance = np.linalg.norm(
            points[:, None, :] - screw_entries[None, :, :], axis=2
        ).min(axis=1)
        # Linear falloff: full boss at the bore, gone at boss_radius_mm. A step
        # would put a crack-starting notch in the outer surface.
        thickness += boss * np.clip(1.0 - distance / radius, 0.0, 1.0)
    return thickness


def _smoothed_normals(sheet: trimesh.Trimesh, passes: int = 3) -> np.ndarray:
    """Vertex normals averaged with their neighbours' before offsetting.

    Two surfaces meeting at a crease -- the spanned defect against the bone around
    it, or a resection edge -- give neighbouring vertices normals that diverge by
    tens of degrees. Offsetting along those folds the outer surface back on itself,
    and a folded surface reports a wall of nearly zero however thick it was asked to
    be. Averaging costs a fraction of a millimetre of conformance at the crease and
    buys a wall that is actually there.
    """
    normals = sheet.vertex_normals.copy()
    edges = sheet.edges_unique
    for _ in range(passes):
        summed = normals.copy()
        np.add.at(summed, edges[:, 0], normals[edges[:, 1]])
        np.add.at(summed, edges[:, 1], normals[edges[:, 0]])
        lengths = np.linalg.norm(summed, axis=1, keepdims=True)
        normals = np.where(lengths > 1e-9, summed / lengths, normals)
    return normals


def _weld(sheet: trimesh.Trimesh, tol_mm: float = WELD_TOL_MM) -> trimesh.Trimesh:
    """Fuse sheet vertices that sit on top of each other, within ``tol_mm``.

    Exact-duplicate merging is not enough. Where a defect was cut out of the bone
    mesh the cut leaves rim vertices a hundredth of a millimetre apart -- distinct,
    so they survive ``merge_vertices``, yet close enough that the rim edge between
    them is a sliver. Extruded, that sliver becomes a wall quad with no width that
    crosses the outer surface, and one such facet pair is enough for tetgen to
    refuse the whole solid. The tolerance is far below any feature the validators
    measure, so welding costs the geometry nothing.
    """
    pairs = cKDTree(sheet.vertices).query_pairs(tol_mm, output_type="ndarray")
    if not len(pairs):
        return sheet
    remap = np.arange(len(sheet.vertices))
    for a, b in pairs:
        lo, hi = sorted((remap[a], remap[b]))
        remap[remap == hi] = lo
    faces = remap[sheet.faces]
    kept = (
        (faces[:, 0] != faces[:, 1])
        & (faces[:, 1] != faces[:, 2])
        & (faces[:, 0] != faces[:, 2])
    )
    welded = trimesh.Trimesh(vertices=sheet.vertices, faces=faces[kept], process=False)
    welded.remove_unreferenced_vertices()
    return welded


def _offset_shell(
    sheet: trimesh.Trimesh,
    clearance_mm: float,
    wall_spec,
    screw_entries: np.ndarray,
    passes: int,
) -> trimesh.Trimesh:
    """Offset the sheet twice along its normals and stitch the copies at the rim."""
    normals = _smoothed_normals(sheet, passes=passes)
    inner = sheet.vertices + normals * clearance_mm
    outer = inner + normals * _wall(wall_spec, sheet.vertices, screw_entries)[:, None]

    n = len(sheet.vertices)
    rim = _boundary_edges(sheet)
    if not len(rim):
        raise ValueError(
            "the selected region is a closed surface, so it has no rim to close "
            "against; a shell needs an open patch of bone"
        )
    walls = np.vstack(
        [
            np.column_stack([rim[:, 0], rim[:, 1], rim[:, 1] + n]),
            np.column_stack([rim[:, 0], rim[:, 1] + n, rim[:, 0] + n]),
        ]
    )

    shell = trimesh.Trimesh(
        vertices=np.vstack([inner, outer]),
        # Inner faces wound inward: it is the back of the solid.
        faces=np.vstack([sheet.faces[:, ::-1], sheet.faces + n, walls]),
    )
    shell.fix_normals()
    if not shell.is_watertight:
        shell.fill_holes()
        shell.fix_normals()
    return shell


def build_shell(
    bone: trimesh.Trimesh,
    faces: np.ndarray,
    clearance_mm: float,
    wall_spec,
    screw_entries: np.ndarray,
) -> trimesh.Trimesh:
    """Close the selected bone region into a solid shell standing off the bone.

    The offset is retried with progressively smoother normals until the shell stops
    passing through itself. Where the spanned defect meets the bone around it the
    two surfaces form a crease, and offsetting across a crease crosses the offset
    directions: the solid comes out closed and looks right, but a few facets sit on
    the wrong side of each other. Nothing downstream tolerates that -- tetgen
    aborts with ``PLC Error: A segment and a facet intersect``, so the FEA
    validator can only report ERROR and the case gets no stress number at all, and
    the fold is not a manufacturable body either. Smoothing unfolds it for a
    fraction of a millimetre of conformance, which is why the ladder stops at the
    first amount that works instead of always smoothing hard.
    """
    sheet = _weld(
        close_interior_holes(
            _largest_component(bone.submesh([faces], append=True, repair=False))
        )
    )
    attempts = []
    for passes in SMOOTHING_LADDER:
        shell = _offset_shell(sheet, clearance_mm, wall_spec, screw_entries, passes)
        if not self_intersection.is_self_intersecting(shell):
            return shell
        attempts.append(shell)
    # Nothing on the ladder unfolded it, so smoothing further only cost conformance
    # and wall for nothing: hand back the least-smoothed attempt. Returning it beats
    # raising, because the geometry validators then measure it honestly and a
    # reported wall or conformance number tells the next design iteration more than
    # an exception carrying none. Downstream tetgen will still refuse it.
    return attempts[0]


def _drill(shell: trimesh.Trimesh, screws: list[dict], diameter: float) -> trimesh.Trimesh:
    """Cut each planned trajectory through the shell, along its own axis."""
    reach = float(np.ptp(shell.bounds, axis=0).max()) * 4.0 + 20.0
    for screw in screws:
        entry = np.asarray(screw["entry_mm"], dtype=float)
        direction = np.asarray(screw["direction"], dtype=float)
        cutter = trimesh.creation.cylinder(
            radius=diameter / 2.0,
            segment=np.vstack([entry - direction * reach, entry + direction * reach]),
        )
        shell = trimesh.boolean.difference([shell, cutter])
    return shell


def to_solid(mesh: trimesh.Trimesh) -> cq.Workplane:
    """Sew the triangles into an OCC solid so STEP export and the rest work."""
    verts = mesh.vertices
    faces = [
        cq.Face.makeFromWires(
            cq.Wire.makePolygon([cq.Vector(*verts[i]) for i in tri], close=True)
        )
        for tri in mesh.faces
    ]
    solid = cq.Solid.makeSolid(cq.Shell.makeShell(faces))
    return cq.Workplane(obj=solid)


def build_patch(params: dict) -> cq.Workplane:
    """A conformal shell over the region the plan declares. Any anatomy."""
    spec = params.get("patch") or {}
    case = case_io.active_case()
    bone = _bone_mesh(case)
    screws = case_io.load_screws(case)
    entries = np.array([s["entry_mm"] for s in screws], dtype=float) if screws else np.empty((0, 3))

    region = spec.get("region") or (case.get("implant") or {}).get("region") or {}
    faces = region_faces(bone, region, screws)

    shell = build_shell(
        bone,
        faces,
        float(params["mount_clearance_mm"]),
        spec.get("wall", params["thickness_mm"]),
        entries,
    )
    if screws:
        shell = _drill(shell, screws, float(params["hole_diameter_mm"]))
    if not shell.is_watertight:
        raise ValueError(
            "the shell did not come out watertight after drilling; the bores may "
            "graze the rim, which leaves a screw head bearing on a broken edge"
        )
    return to_solid(shell)
