"""The generator must build against the plan it is given, not the one it assumes.

`build_implant()` used to read one number per screw -- its z -- and drill every
hole at y=0 along +X. That was correct for exactly one plan: the synthetic one,
whose screws are generated on the centreline running along -X. Every real
surgical plan angles screws toward the medullary canal and spreads them across
the mounting aspect, and against such a plan half the bores came out obstructed
with no parameter able to fix it.

The proof case here is `real_cases/example/surgical_plan_oblique.json`: the same
bone and the same six screws, re-seated off the centreline and aimed at the shaft
axis. Against the old generator it scored 3/6 on `screw_trajectories_clear`.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from autoimplants import case_io, import_case
from autoimplants.params import default_params
from autoimplants.validators import geometry

pytest.importorskip("cadquery", reason="CAD toolchain not installed")

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "real_cases" / "example"


def _build_and_validate(case_path, tmp_path, params=None):
    """Build the implant for a case and run the geometry validator on it."""
    from autoimplants.export import export_implant
    from autoimplants.generator import build_implant

    case = case_io.set_active_case(case_io.load_case(case_path), case_path)
    solid = build_implant(params or default_params())
    stl = export_implant(solid, tmp_path / "implant")
    return case, geometry.validate(str(stl), case)


@pytest.fixture(scope="module")
def oblique_case(tmp_path_factory):
    plan = EXAMPLE / "surgical_plan_oblique.json"
    bone = EXAMPLE / "bone.stl"
    if not plan.exists() or not bone.exists():
        pytest.skip("run real_cases/example/make_example.py to generate the fixtures")

    out = tmp_path_factory.mktemp("oblique")
    report, case_path = import_case.import_case(plan, bone, out_dir=out / "generated")
    assert report.passed, report.summary()
    return case_path


def test_oblique_screws_all_get_a_clear_bore(oblique_case, tmp_path):
    """The whole point of Phase 1. This scored 3/6 before the generator changed."""
    _, report = _build_and_validate(oblique_case, tmp_path)

    check = report.by_id("screw_trajectories_clear")
    assert check.status == "PASS", check.message
    assert check.value == 6.0


def test_plate_follows_the_screws_across_the_width(oblique_case, tmp_path):
    """A plate centred on y=0 would not sit over off-centreline screws."""
    case, _ = _build_and_validate(oblique_case, tmp_path)

    entries = np.array([s["entry_mm"] for s in case_io.load_screws(case)])
    expected_center = float(entries[:, 1].mean())
    assert abs(expected_center) > 0.1, "fixture is not off-centre; the test proves nothing"

    import trimesh

    mesh = trimesh.load(str(tmp_path / "implant.stl"), force="mesh")
    plate_center_y = float(mesh.bounds[:, 1].mean())
    assert np.isclose(plate_center_y, expected_center, atol=0.2)


def test_screws_wider_than_the_plate_fail_loudly(oblique_case, tmp_path):
    """Truncating the plan silently would leave a screw the plate cannot reach."""
    from autoimplants.generator import build_implant

    case_io.set_active_case(case_io.load_case(oblique_case), oblique_case)
    params = default_params()
    params["width_mm"] = 6.0  # narrower than the screw spread plus a bore

    with pytest.raises(ValueError) as exc:
        build_implant(params)

    message = str(exc.value)
    assert "width" in message
    assert "6.0 mm" in message


def test_demo_case_meets_every_geometry_check(tmp_path):
    """The demo case must pass, on limits rather than on remembered numbers.

    This used to pin the flat plate's 36.996 g and 8.596 mm gap, i.e. it asserted
    that the design task had *not* been done: the conformance figure it protected
    is 5.7x the case's own limit, so it failed by construction the moment the
    plate started following the bone. Checking each measurement against the limit
    the case declares keeps the regression value -- a plate that stops seating,
    gets heavy or loses a bore still fails here -- without freezing one
    particular solution.
    """
    case_path = REPO_ROOT / "inputs" / "case.json"
    _, report = _build_and_validate(case_path, tmp_path)

    assert report.by_id("screw_trajectories_clear").value == 6.0
    failures = [c.id for c in report.checks if c.status == "FAIL"]
    assert not failures, report.summary()


def test_seating_uses_the_lanes_the_plate_covers(oblique_case, tmp_path):
    """Seating to the y=0 centreline alone can bury a plate edge in cortex."""
    case, report = _build_and_validate(oblique_case, tmp_path)

    # The generator holds mount_clearance_mm at the most protruding point under
    # the plate, so nothing may end up inside the bone.
    assert report.by_id("no_bone_collision").status == "PASS"
    assert report.by_id("bone_clearance_min").status == "PASS"


def test_bores_are_cut_along_each_screws_own_axis(oblique_case, tmp_path):
    """A bore drilled down +X would not line up with an angled screw at all."""
    case, _ = _build_and_validate(oblique_case, tmp_path)

    import trimesh

    mesh = trimesh.load(str(tmp_path / "implant.stl"), force="mesh")

    for screw in case_io.load_screws(case):
        entry = np.array(screw["entry_mm"], dtype=float)
        direction = np.array(screw["direction"], dtype=float)

        # A ray down the screw's own axis passes through the bore without
        # meeting material.
        origin = entry - direction * 200.0
        hits, _, _ = mesh.ray.intersects_location(
            ray_origins=np.array([origin]), ray_directions=np.array([direction])
        )
        assert len(hits) == 0, f"{screw['id']} bore is obstructed"


def test_fixture_actually_is_oblique():
    """Guards the fixture itself: if it degenerates to axis-aligned centreline
    screws, every test above passes for the wrong reason."""
    plan_path = EXAMPLE / "surgical_plan_oblique.json"
    if not plan_path.exists():
        pytest.skip("run real_cases/example/make_example.py to generate the fixtures")

    plan = json.loads(plan_path.read_text("utf-8"))
    directions = np.array([s["direction"] for s in plan["screws"]], dtype=float)
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)

    # No two screws share a direction, i.e. they converge rather than run parallel.
    spread = float(np.max(directions.std(axis=0)))
    assert spread > 1e-3, "fixture screws are parallel; they should converge"
