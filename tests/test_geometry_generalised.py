"""The two geometry assumptions that only held for the synthetic case.

Both were correct for ``inputs/`` and wrong for anything else: screws were assumed
to run along -X, and the bone gap was read off the y=0 centreline. A real plan has
obliquely angled screws, and a real shaft curves in both planes.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
import trimesh

from autoimplants import case_io
from autoimplants.validators import geometry

OBLIQUE = np.array([-0.80, 0.36, 0.48])
OBLIQUE /= np.linalg.norm(OBLIQUE)

HOLE_D_MM = 4.5


def _plate_with_bore(direction: np.ndarray, tmp_path):
    """A plate with one clean bore along ``direction`` through its centre."""
    plate = trimesh.creation.box(extents=(6.0, 20.0, 60.0))
    align = trimesh.geometry.align_vectors([0.0, 0.0, 1.0], direction)
    bore = trimesh.creation.cylinder(radius=HOLE_D_MM / 2.0, height=200.0, transform=align)

    drilled = trimesh.boolean.difference([plate, bore])
    path = tmp_path / "plate.stl"
    drilled.export(str(path))
    return drilled


def _case_with_screw(direction, tmp_path, entry=(0.0, 0.0, 0.0)) -> dict:
    """A minimal case whose single screw runs along ``direction``."""
    screws = {
        "screws": [
            {
                "id": "screw_0",
                "index": 0,
                "entry_mm": list(entry),
                "direction": list(direction),
                "diameter_mm": HOLE_D_MM,
                "length_mm": 30.0,
            }
        ]
    }
    screws_path = tmp_path / "screw_positions.json"
    screws_path.write_text(json.dumps(screws), encoding="utf-8")

    return {
        "case_id": "TEST",
        "inputs": {"screw_positions": str(screws_path), "keepout_zones": str(tmp_path / "none.json")},
        "thresholds": {"require_all_screws": 1},
    }


def test_oblique_bore_reads_as_clear(tmp_path):
    """The bore is drilled along the screw's own axis, so the screw is unobstructed."""
    mesh = _plate_with_bore(OBLIQUE, tmp_path)
    case = _case_with_screw(OBLIQUE, tmp_path)

    check = geometry.check_screws(mesh, case)[0]
    assert check.status == "PASS", check.message


def test_oblique_screw_through_solid_reads_as_blocked(tmp_path):
    """Same plate, a screw on a different oblique axis: it hits material."""
    mesh = _plate_with_bore(OBLIQUE, tmp_path)
    other = np.array([-0.55, -0.70, 0.45])
    other /= np.linalg.norm(other)
    case = _case_with_screw(other, tmp_path)

    check = geometry.check_screws(mesh, case)[0]
    assert check.status == "FAIL"
    assert "screw_0" in check.message


def test_axis_aligned_case_still_behaves(tmp_path):
    """Generalising the ring must not change the verdict it already gave."""
    axis = np.array([-1.0, 0.0, 0.0])
    mesh = _plate_with_bore(axis, tmp_path)
    case = _case_with_screw(axis, tmp_path)

    assert geometry.check_screws(mesh, case)[0].status == "PASS"


@pytest.mark.parametrize(
    "d",
    [
        [0.0, 0.0, 1.0],       # the seed vector's degenerate case
        [0.0, 0.0, -1.0],
        [1.0, 0.0, 0.0],
        [0.3, -0.9, 0.31],
    ],
)
def test_perpendicular_basis_is_orthonormal(d):
    d = np.asarray(d, dtype=float)
    d /= np.linalg.norm(d)
    u, v = geometry._perpendicular_basis(d)

    for vec in (u, v):
        assert np.isclose(np.linalg.norm(vec), 1.0)
        assert abs(float(np.dot(vec, d))) < 1e-9
    assert abs(float(np.dot(u, v))) < 1e-9


# -- the gap, measured across the plate width ---------------------------------


def test_gap_is_sampled_off_the_centreline(synthetic_case):
    """A curved shaft stands further off the plate edges than its centre.

    The old check measured y=0 only. On the synthetic femur the edge lanes are
    the worst case, so the generalised check must report a strictly larger gap --
    if it does not, it is still only reading the centreline.
    """
    from autoimplants.bone import surface_grid

    case_io.set_active_case(synthetic_case, case_io.DEFAULT_CASE_PATH)

    _, _, centre = surface_grid(100.0, 280.0, ys=(0.0,), n=31)
    _, _, edge = surface_grid(100.0, 280.0, ys=(6.0,), n=31)

    assert np.nanmax(centre) > np.nanmax(edge), (
        "the bone should protrude furthest on its centreline; if not, the lanes "
        "are not sampling different geometry"
    )


def test_lanes_span_the_plate_width():
    mesh = trimesh.creation.box(extents=(3.0, 16.0, 180.0))
    lanes = geometry._lanes(mesh)

    assert len(lanes) == geometry.N_PROFILE_LANES
    assert lanes.min() > mesh.bounds[0][1]
    assert lanes.max() < mesh.bounds[1][1]
    # An odd lane count keeps the historical centreline measurement inside the set.
    assert np.isclose(lanes[len(lanes) // 2], 0.0, atol=1e-9)
