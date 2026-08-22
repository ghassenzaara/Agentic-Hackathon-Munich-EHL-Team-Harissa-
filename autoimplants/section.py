"""Cross-section properties of the exported implant, measured off the mesh.

The stress validator needs area and second moment of area at stations along the
plate. Both are properties of the *actual exported solid*, not of the parameters
that produced it -- which is the point: a rib, a thickness profile or a hole
turned into a slot all show up here without the validator knowing they exist.

Measured the same way the geometry validator measures everything else: cast rays
through the solid and read the chords. At a station z, a ray travelling +X at
height y enters and leaves the solid at pairs of x values, so the solid part of
that lane is the sum of those chords. Integrating the chords across y gives area
and moments directly, with no meshing, no polygon library, and no new dependency.

Sign conventions, in the repo frame (+Z along the shaft, +X the mount direction,
+Y the plate width):

    i_yy   second moment about the centroidal Y axis. Governs bending in the X-Z
           plane -- the plate's thin direction, and its weak one.
    i_zz   second moment about the centroidal Z axis. Governs bending in the Y-Z
           plane -- across the width, the stiff direction.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Lanes across the width. 121 puts them ~0.13 mm apart on a 16 mm plate, which
# resolves a screw hole edge to well under the tolerance any threshold cares
# about, and the whole sweep is still one batched ray cast.
N_LANES = 121
N_STATIONS = 41

# Rays start this far outside the solid's bounding box.
_RAY_STANDOFF_MM = 10.0


@dataclass(frozen=True)
class Section:
    """One cross-section, already reduced to the numbers beam theory needs."""

    z: float
    area: float          # mm^2
    i_yy: float          # mm^4, about the centroidal Y axis (thin direction)
    i_zz: float          # mm^4, about the centroidal Z axis (width direction)
    x_centroid: float    # mm
    y_centroid: float    # mm
    c_x: float           # mm, extreme fibre distance from the centroid in X
    c_y: float           # mm, extreme fibre distance from the centroid in Y

    @property
    def is_solid(self) -> bool:
        """False where the rays found no material -- past the end of the part."""
        return self.area > 0.0


def _chord_bounds(hits_x: np.ndarray) -> list[tuple[float, float]]:
    """Entry/exit pairs along one ray. Odd counts mean a degenerate hit; drop the tail."""
    xs = np.sort(hits_x)
    return [(float(xs[i]), float(xs[i + 1])) for i in range(0, len(xs) - 1, 2)]


def sections(
    mesh,
    z0: float | None = None,
    z1: float | None = None,
    n_stations: int = N_STATIONS,
    n_lanes: int = N_LANES,
    extra_z: list[float] | tuple[float, ...] | np.ndarray | None = None,
) -> list[Section]:
    """Section properties at stations along the part.

    One batched ray cast for the whole sweep: ``n_stations * n_lanes`` rays. Doing
    it per station was measurably slower and this runs on every loop iteration.

    ``extra_z`` forces stations at exact positions on top of the even grid. Pass
    the screw heights: the weakest section of a plate is the one through a hole,
    and whether the even grid happens to land on a hole centre is an accident of
    the station count. Left to luck, the reported peak moved between 669 and 765
    MPa on the same solid purely with ``n_stations`` -- and a number that jumps
    when the geometry has not changed is poison inside an iterative loop.
    """
    lo, hi = mesh.bounds
    z0 = float(lo[2]) if z0 is None else float(z0)
    z1 = float(hi[2]) if z1 is None else float(z1)

    zs = np.linspace(z0, z1, n_stations)
    if extra_z is not None and len(extra_z):
        forced = np.asarray(extra_z, dtype=float)
        forced = forced[(forced >= z0) & (forced <= z1)]
        zs = np.unique(np.concatenate([zs, forced]))

    # Lanes sit at the CENTRE of each strip, not on its edges. Sampling the
    # endpoints instead makes n lanes each carry (hi-lo)/(n-1) of width, so the
    # strips sum to more than the section is wide -- a systematic overshoot that
    # showed up as ~1% on area and ~2.5% on i_zz, where the y^2 weighting
    # amplifies the spurious outermost lanes. Midpoints also avoid launching a
    # ray exactly along the boundary face, where the hit count is degenerate.
    dy = (float(hi[1]) - float(lo[1])) / n_lanes
    ys = float(lo[1]) + (np.arange(n_lanes) + 0.5) * dy

    zz, yy = np.meshgrid(zs, ys, indexing="ij")
    flat_z = zz.reshape(-1)
    flat_y = yy.reshape(-1)
    n_rays = flat_z.size

    origins = np.column_stack(
        [np.full(n_rays, float(lo[0]) - _RAY_STANDOFF_MM), flat_y, flat_z]
    )
    directions = np.tile([1.0, 0.0, 0.0], (n_rays, 1))

    hits, ray_idx, _ = mesh.ray.intersects_location(
        ray_origins=origins, ray_directions=directions
    )

    # Bucket the hits by ray so each lane can be reduced independently.
    # A total miss comes back as a 1-D empty array rather than an empty (n, 3),
    # so the column index has to be guarded -- otherwise sampling a station past
    # the end of the part crashes the validator instead of reporting no material.
    per_ray: list[list[float]] = [[] for _ in range(n_rays)]
    if len(hits):
        for x, idx in zip(hits[:, 0], ray_idx):
            per_ray[int(idx)].append(float(x))

    out: list[Section] = []
    for si in range(len(zs)):
        area = 0.0
        moment_x = 0.0     # about x = 0
        moment_x2 = 0.0    # about x = 0
        moment_y = 0.0     # about y = 0
        moment_y2 = 0.0    # about y = 0
        x_min, x_max = np.inf, -np.inf
        y_min, y_max = np.inf, -np.inf

        for li in range(len(ys)):
            xs_hit = per_ray[si * len(ys) + li]
            if len(xs_hit) < 2:
                continue
            y = float(ys[li])
            lane_area = 0.0
            for a, b in _chord_bounds(np.asarray(xs_hit)):
                lane_area += (b - a) * dy
                # Integrating x and x^2 over the chord, times the lane width.
                moment_x += 0.5 * (b**2 - a**2) * dy
                moment_x2 += (b**3 - a**3) * dy / 3.0
                x_min, x_max = min(x_min, a), max(x_max, b)
            if lane_area <= 0.0:
                continue
            area += lane_area
            moment_y += y * lane_area
            moment_y2 += y * y * lane_area
            # The extreme fibre is at the strip's outer edge, not at the lane
            # centre the ray was cast along -- half a lane short would understate
            # c, and understating c understates stress.
            y_min, y_max = min(y_min, y - dy / 2.0), max(y_max, y + dy / 2.0)

        if area <= 0.0:
            out.append(Section(float(zs[si]), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
            continue

        x_bar = moment_x / area
        y_bar = moment_y / area
        # Parallel axis, moving each second moment from the origin to the centroid.
        i_yy = moment_x2 - area * x_bar**2
        i_zz = moment_y2 - area * y_bar**2

        out.append(
            Section(
                z=float(zs[si]),
                area=float(area),
                i_yy=float(max(i_yy, 0.0)),
                i_zz=float(max(i_zz, 0.0)),
                x_centroid=float(x_bar),
                y_centroid=float(y_bar),
                c_x=float(max(x_max - x_bar, x_bar - x_min)),
                c_y=float(max(y_max - y_bar, y_bar - y_min)),
            )
        )

    return out


def section_at(sections_list: list[Section], z: float) -> Section | None:
    """The measured station nearest ``z``, skipping stations that found no solid."""
    solid = [s for s in sections_list if s.is_solid]
    if not solid:
        return None
    return min(solid, key=lambda s: abs(s.z - z))
