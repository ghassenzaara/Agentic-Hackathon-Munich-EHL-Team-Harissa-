"""Does a closed triangle mesh pass through itself?

Watertightness is not enough. A surface can be closed, single-component and
correctly wound and still fold through itself -- which is exactly what offsetting
a sheet along its vertex normals does wherever two parts of the sheet meet at a
crease, because the two offset directions cross. Such a solid has no well-defined
interior along the fold, and two things downstream refuse it outright:

* ``gmsh``/tetgen abort with ``PLC Error: A segment and a facet intersect``, so
  the FEA validator can only report ERROR -- a case gets no stress number at all;
* it is not a manufacturable body either: the fold is material that is inside and
  outside the part at once.

``trimesh`` has no self-intersection test (its boolean backend assumes valid
input and happily carries the fold through), so the test lives here: an AABB tree
gives candidate pairs and each pair is settled with the standard
Moller triangle-triangle interval test. Pairs that merely share a vertex or an
edge are neighbours, not intersections, and are skipped.

Coplanar overlaps are deliberately not reported. They need a different test, they
do not come out of offsetting, and reporting them would make the caller's
"is this fixable by smoothing?" question unanswerable.
"""

from __future__ import annotations

import numpy as np

EPS = 1e-9


def _plane_distances(
    triangle: np.ndarray, other: np.ndarray
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Signed distances of ``other``'s corners from ``triangle``'s plane."""
    p0, p1, p2 = triangle
    normal = np.cross(p1 - p0, p2 - p0)
    length = float(np.linalg.norm(normal))
    if length < EPS:  # a degenerate sliver has no plane to test against
        return None, None
    normal = normal / length
    return (other - p0) @ normal, normal


def _interval(triangle: np.ndarray, distances: np.ndarray, axis: np.ndarray):
    """Where the triangle crosses the other plane, projected onto ``axis``."""
    points = []
    for i in range(3):
        j = (i + 1) % 3
        if abs(distances[i]) <= EPS:
            points.append(triangle[i])
        if distances[i] * distances[j] < 0.0:
            weight = distances[i] / (distances[i] - distances[j])
            points.append(triangle[i] + weight * (triangle[j] - triangle[i]))
    if len(points) < 2:
        return None
    projected = [float(np.dot(p, axis)) for p in points]
    return min(projected), max(projected)


def triangles_intersect(first: np.ndarray, second: np.ndarray) -> bool:
    """True when two triangles cross, sharing more than a touching plane."""
    d_second, n_first = _plane_distances(first, second)
    if d_second is None or (d_second > EPS).all() or (d_second < -EPS).all():
        return False
    d_first, n_second = _plane_distances(second, first)
    if d_first is None or (d_first > EPS).all() or (d_first < -EPS).all():
        return False

    axis = np.cross(n_first, n_second)
    if np.linalg.norm(axis) < EPS:  # coplanar: see the module docstring
        return False

    span_first = _interval(first, d_first, axis)
    span_second = _interval(second, d_second, axis)
    if span_first is None or span_second is None:
        return False
    return not (
        span_first[1] < span_second[0] + EPS or span_second[1] < span_first[0] + EPS
    )


def intersecting_pairs(mesh, stop_after: int | None = None) -> list[tuple[int, int]]:
    """Indices of face pairs that pass through each other.

    ``stop_after`` returns early once that many pairs are found, which is what a
    caller asking "is this mesh usable?" wants: the count only matters as
    diagnostics, and a folded offset is rejected on the first pair.
    """
    triangles = mesh.triangles
    tree = mesh.triangles_tree
    faces = mesh.faces
    found: list[tuple[int, int]] = []
    for i, triangle in enumerate(triangles):
        lo = triangle.min(axis=0)
        hi = triangle.max(axis=0)
        corners_i = set(faces[i].tolist())
        for j in tree.intersection((*lo, *hi)):
            if j <= i or corners_i & set(faces[j].tolist()):
                continue
            if triangles_intersect(triangle, triangles[j]):
                found.append((i, int(j)))
                if stop_after is not None and len(found) >= stop_after:
                    return found
    return found


def is_self_intersecting(mesh) -> bool:
    """True when any two non-adjacent faces of the mesh cross."""
    return bool(intersecting_pairs(mesh, stop_after=1))
