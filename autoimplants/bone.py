"""Bone surface sampling, shared by the generator and the geometry validator.

The generator needs the bone surface to contour the plate to it; the validator
needs it to measure the residual gap. Both go through this module so they can
never disagree about where the bone is.

Everything here reads the mesh -- no analytic shortcuts -- so swapping bone.stl
for a real segmented femur changes nothing else in the codebase.

Frame assumption (enforced at import time by ``autoimplants.import_case``, which
rigidly transforms a real CT mesh into it before anything here sees it):

    +Z  along the shaft, proximal to distal
    +X  the aspect the plate mounts on
    +Y  the plate width direction

Rays are cast inward along -X from outside the mesh. The launch point is derived
from the mesh bounds rather than a fixed constant, because a real segmented bone
does not arrive centred near the origin the way the synthetic one does.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

from . import case_io

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BONE = REPO_ROOT / "inputs" / "bone.stl"

# How far outside the mesh bounding box to launch rays from.
_RAY_STANDOFF_MM = 10.0


def _resolve(path: str | Path | None):
    """``None`` means 'whatever bone the active case names'."""
    return Path(path) if path is not None else case_io.bone_path()


@lru_cache(maxsize=4)
def _load_cached(path_str: str):
    import trimesh

    mesh = trimesh.load(path_str, force="mesh")
    if mesh.is_empty:
        raise ValueError(f"bone mesh at {path_str} is empty")
    return mesh


def load_bone(path: str | Path | None = None):
    """Load and cache the bone mesh. Cached because validators call this per check."""
    return _load_cached(str(_resolve(path)))


def ray_start_x(path: str | Path | None = None) -> float:
    """An x outside the bone, to launch inward-travelling (-X) rays from."""
    mesh = load_bone(path)
    return float(mesh.bounds[1][0]) + _RAY_STANDOFF_MM


def surface_x_at(z: float, y: float = 0.0, path: str | Path | None = None) -> float:
    """x of the lateral (+X) bone surface at height z.

    Returns NaN when the ray misses the bone entirely, which is a real answer:
    it means the plate footprint has run off the end of the shaft.
    """
    mesh = load_bone(path)
    hits, _, _ = mesh.ray.intersects_location(
        ray_origins=np.array([[ray_start_x(path), y, z]]),
        ray_directions=np.array([[-1.0, 0.0, 0.0]]),
    )
    if len(hits) == 0:
        return float("nan")
    return float(np.max(hits[:, 0]))


def surface_profile(
    z0: float, z1: float, n: int = 25, y: float = 0.0, path: str | Path | None = None
) -> np.ndarray:
    """Sample the lateral surface across a footprint. Returns an (n, 2) array of (z, x).

    Vectorised into one ray batch -- calling surface_x_at in a loop is measurably
    slower and this runs inside the validator on every iteration.
    """
    grid = surface_grid(z0, z1, ys=(y,), n=n, path=path)
    return np.column_stack([grid[0], grid[2][:, 0]])


def surface_grid(
    z0: float,
    z1: float,
    ys: tuple[float, ...] | list[float] | np.ndarray = (0.0,),
    n: int = 25,
    path: str | Path | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Lateral surface x over a z x y grid, in one ray batch.

    Returns ``(zs, ys, xs)`` where ``xs`` has shape ``(len(zs), len(ys))`` and is
    NaN where the ray missed. The multi-y form exists because a bone is not a
    prism: sampling only the y=0 centreline measures the gap under the middle of
    the plate and says nothing about how its edges seat on a curved shaft.
    """
    mesh = load_bone(path)
    zs = np.linspace(z0, z1, n)
    ys_arr = np.asarray(ys, dtype=float).reshape(-1)
    start_x = ray_start_x(path)

    zz, yy = np.meshgrid(zs, ys_arr, indexing="ij")
    flat_z = zz.reshape(-1)
    flat_y = yy.reshape(-1)
    m = flat_z.size

    origins = np.column_stack([np.full(m, start_x), flat_y, flat_z])
    directions = np.tile([-1.0, 0.0, 0.0], (m, 1))

    hits, ray_idx, _ = mesh.ray.intersects_location(
        ray_origins=origins, ray_directions=directions
    )

    xs = np.full(m, np.nan)
    if len(hits):
        # np.maximum.at keeps the outermost hit per ray without a Python loop
        # over rays; the grid is len(zs) * len(ys) rays and the loop showed up
        # in profiles once ys stopped being a single centreline.
        order = np.full(m, -np.inf)
        np.maximum.at(order, ray_idx, hits[:, 0])
        xs = np.where(np.isfinite(order), order, np.nan)

    return zs, ys_arr, xs.reshape(len(zs), len(ys_arr))


def max_surface_x(
    z0: float,
    z1: float,
    n: int = 25,
    ys: tuple[float, ...] | list[float] | np.ndarray = (0.0,),
    path: str | Path | None = None,
) -> float:
    """Most protruding point of the lateral surface across a footprint.

    A flat plate must be mounted at least this far out or it cuts into the bone.
    """
    _, _, xs = surface_grid(z0, z1, ys=ys, n=n, path=path)
    return float(np.nanmax(xs))
