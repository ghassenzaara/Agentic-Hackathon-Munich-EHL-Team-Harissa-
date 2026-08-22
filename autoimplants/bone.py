"""Bone surface sampling, shared by the generator and the geometry validator.

The generator needs the bone surface to contour the plate to it; the validator
needs it to measure the residual gap. Both go through this module so they can
never disagree about where the bone is.

Everything here reads the mesh -- no analytic shortcuts -- so swapping bone.stl
for a real segmented femur changes nothing else in the codebase.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BONE = REPO_ROOT / "inputs" / "bone.stl"

# Rays are cast from well outside the bone, inward along -X.
_RAY_START_X = 500.0


@lru_cache(maxsize=4)
def load_bone(path: str | Path = DEFAULT_BONE):
    """Load and cache the bone mesh. Cached because validators call this per check."""
    import trimesh

    mesh = trimesh.load(str(path), force="mesh")
    if mesh.is_empty:
        raise ValueError(f"bone mesh at {path} is empty")
    return mesh


def surface_x_at(z: float, y: float = 0.0, path: str | Path = DEFAULT_BONE) -> float:
    """x of the lateral (+X) bone surface at height z.

    Returns NaN when the ray misses the bone entirely, which is a real answer:
    it means the plate footprint has run off the end of the shaft.
    """
    mesh = load_bone(path)
    hits, _, _ = mesh.ray.intersects_location(
        ray_origins=np.array([[_RAY_START_X, y, z]]),
        ray_directions=np.array([[-1.0, 0.0, 0.0]]),
    )
    if len(hits) == 0:
        return float("nan")
    return float(np.max(hits[:, 0]))


def surface_profile(
    z0: float, z1: float, n: int = 25, y: float = 0.0, path: str | Path = DEFAULT_BONE
) -> np.ndarray:
    """Sample the lateral surface across a footprint. Returns an (n, 2) array of (z, x).

    Vectorised into one ray batch -- calling surface_x_at in a loop is measurably
    slower and this runs inside the validator on every iteration.
    """
    mesh = load_bone(path)
    zs = np.linspace(z0, z1, n)
    origins = np.column_stack([np.full(n, _RAY_START_X), np.full(n, y), zs])
    directions = np.tile([-1.0, 0.0, 0.0], (n, 1))

    hits, ray_idx, _ = mesh.ray.intersects_location(
        ray_origins=origins, ray_directions=directions
    )

    xs = np.full(n, np.nan)
    for i in range(n):
        this_ray = hits[ray_idx == i]
        if len(this_ray):
            xs[i] = this_ray[:, 0].max()
    return np.column_stack([zs, xs])


def max_surface_x(z0: float, z1: float, n: int = 25, path: str | Path = DEFAULT_BONE) -> float:
    """Most protruding point of the lateral surface across a footprint.

    A flat plate must be mounted at least this far out or it cuts into the bone.
    """
    prof = surface_profile(z0, z1, n=n, path=path)
    return float(np.nanmax(prof[:, 1]))
