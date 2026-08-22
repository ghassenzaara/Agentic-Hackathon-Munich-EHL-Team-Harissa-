"""Section properties, checked against closed form.

Beam theory is only as good as `A`, `I` and `c`. These are measured by ray
casting rather than derived from parameters, so they can be wrong in ways a
formula cannot -- sampling bias, endpoint double-counting, missed chords. A
rectangle has textbook answers, so it is the honest thing to check against.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from autoimplants.section import section_at, sections

W, T, L = 16.0, 3.0, 180.0


@pytest.fixture
def bar():
    return trimesh.creation.box(extents=(T, W, L))


def test_area_matches_closed_form(bar):
    s = sections(bar, n_stations=5)[2]
    assert np.isclose(s.area, W * T, rtol=1e-3)


def test_second_moments_match_closed_form(bar):
    s = sections(bar, n_stations=5)[2]
    # Thin direction: I = w*t^3/12. Wide direction: I = t*w^3/12.
    assert np.isclose(s.i_yy, W * T**3 / 12.0, rtol=2e-3)
    assert np.isclose(s.i_zz, T * W**3 / 12.0, rtol=2e-3)


def test_extreme_fibre_distances(bar):
    s = sections(bar, n_stations=5)[2]
    assert np.isclose(s.c_x, T / 2.0, rtol=1e-3)
    assert np.isclose(s.c_y, W / 2.0, rtol=1e-2)


def test_lane_count_does_not_bias_the_answer(bar):
    """Endpoint sampling made n lanes cover more than the section is wide."""
    values = [sections(bar, n_stations=3, n_lanes=n)[1].area for n in (31, 61, 121, 241)]
    for area in values:
        assert np.isclose(area, W * T, rtol=2e-3), values


def test_hollow_section_has_less_inertia_than_solid():
    """A shape check the rectangle cannot give: removing the middle must matter."""
    solid = trimesh.creation.box(extents=(T, W, L))
    hole = trimesh.creation.cylinder(radius=2.25, height=T * 4)
    hole.apply_transform(trimesh.geometry.align_vectors([0, 0, 1], [1, 0, 0]))
    drilled = trimesh.boolean.difference([solid, hole])

    at_hole = sections(drilled, n_stations=3)[1]
    at_solid = sections(solid, n_stations=3)[1]
    assert at_hole.i_yy < at_solid.i_yy
    assert at_hole.area < at_solid.area


def test_forced_stations_land_exactly(bar):
    """The weakest section is at a hole; the grid must not decide whether to see it."""
    secs = sections(bar, n_stations=5, extra_z=[12.345])
    assert any(np.isclose(s.z, 12.345) for s in secs)


def test_forced_stations_outside_the_part_are_ignored(bar):
    secs = sections(bar, n_stations=5, extra_z=[10_000.0])
    assert all(s.z <= bar.bounds[1][2] + 1e-6 for s in secs)


def test_empty_station_reports_no_material():
    """Past the end of the part is a real answer, not a crash."""
    bar = trimesh.creation.box(extents=(T, W, L))
    secs = sections(bar, z0=float(bar.bounds[1][2]) + 5.0, z1=float(bar.bounds[1][2]) + 10.0)
    assert all(not s.is_solid for s in secs)
    assert section_at(secs, 0.0) is None
