"""End to end: an obliquely posed real-style case must import to the repo frame.

The example case is the synthetic femur under a known rigid pose, so the import
has an exact expected answer -- ``inputs/`` itself. Anything the frame recovery
gets wrong shows up here as a millimetre-scale disagreement with the ground truth.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import trimesh

from autoimplants import import_case
from autoimplants.surgical_plan import PlanError

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUTS = REPO_ROOT / "inputs"


@pytest.fixture
def imported(tmp_path, example_plan_path, example_bone_path):
    report, case_path = import_case.import_case(
        example_plan_path, example_bone_path, out_dir=tmp_path / "generated"
    )
    assert report.passed, report.summary()
    assert case_path is not None
    return case_path


def test_import_writes_the_locked_input_shape(imported):
    out = imported.parent
    for name in ("bone.stl", "case.json", "screw_positions.json", "keepout_zones.json"):
        assert (out / name).exists(), f"{name} was not written"


def test_screws_round_trip_to_the_synthetic_ground_truth(imported, synthetic_screws):
    got = json.loads((imported.parent / "screw_positions.json").read_text("utf-8"))["screws"]
    assert len(got) == len(synthetic_screws)

    for a, b in zip(got, synthetic_screws):
        assert a["id"] == b["id"]
        assert np.allclose(a["entry_mm"], b["entry_mm"], atol=1e-3)
        assert np.allclose(a["direction"], b["direction"], atol=1e-6)


def test_keepouts_round_trip_to_the_synthetic_ground_truth(imported):
    got = json.loads((imported.parent / "keepout_zones.json").read_text("utf-8"))["zones"]
    want = json.loads((INPUTS / "keepout_zones.json").read_text("utf-8"))["zones"]

    for a, b in zip(got, want):
        assert a["id"] == b["id"]
        assert np.allclose(a["center_mm"], b["center_mm"], atol=1e-3)
        assert a["radius_mm"] == b["radius_mm"]


def test_mesh_lands_back_in_the_repo_frame(imported):
    got = trimesh.load(str(imported.parent / "bone.stl"), force="mesh")
    want = trimesh.load(str(INPUTS / "bone.stl"), force="mesh")
    assert np.allclose(got.bounds, want.bounds, atol=1e-2)


def test_case_json_points_at_its_own_files(imported):
    case = json.loads(imported.read_text("utf-8"))
    assert case["inputs"]["bone_mesh"] == "bone.stl"

    from autoimplants import case_io

    loaded = case_io.load_case(imported)
    assert case_io.bone_path(loaded) == (imported.parent / "bone.stl").resolve()
    assert len(case_io.load_screws(loaded)) == 6


def test_transform_is_recorded_for_traceability(imported):
    recorded = json.loads((imported.parent / "frame_transform.json").read_text("utf-8"))
    matrix = np.array(recorded["matrix"])
    assert matrix.shape == (4, 4)
    assert np.allclose(matrix[:3, :3] @ matrix[:3, :3].T, np.eye(3), atol=1e-9)


def test_plan_mesh_mismatch_is_caught(tmp_path, example_plan, example_bone_path):
    """A plan describing a different scan must not import silently."""
    plan = json.loads(json.dumps(example_plan))
    for s in plan["screws"]:
        s["entry_mm"] = [c + 25.0 for c in s["entry_mm"]]

    plan_path = tmp_path / "surgical_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    report, case_path = import_case.import_case(
        plan_path, example_bone_path, out_dir=tmp_path / "generated"
    )
    assert not report.passed
    assert case_path is None
    assert report.by_id("plan_screw_entries_on_bone").status == "FAIL"


def test_footprint_off_the_bone_is_caught(tmp_path, example_plan, example_bone_path):
    plan = json.loads(json.dumps(example_plan))
    plan["footprint_z_mm"] = [500.0, 700.0]

    plan_path = tmp_path / "surgical_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    report, case_path = import_case.import_case(
        plan_path, example_bone_path, out_dir=tmp_path / "generated"
    )
    assert not report.passed
    assert case_path is None
    assert report.by_id("plan_footprint_within_bone").status == "FAIL"


def test_incomplete_plan_raises_rather_than_guessing(tmp_path, example_plan, example_bone_path):
    plan = dict(example_plan)
    plan.pop("screws")
    plan_path = tmp_path / "surgical_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(PlanError):
        import_case.import_case(plan_path, example_bone_path, out_dir=tmp_path / "generated")


def test_imported_case_runs_the_existing_geometry_validator(imported):
    """The whole point: an imported case is just a case."""
    from autoimplants import case_io
    from autoimplants.validators import run_one

    case = case_io.set_active_case(case_io.load_case(imported), imported)
    report = run_one("geometry", str(REPO_ROOT / "out" / "implant.stl"), case)

    if report.by_id("geometry_crashed") or report.by_id("geometry_import"):
        pytest.skip("no exported implant to validate; run autoimplants.run first")

    assert report.by_id("bone_conformance_gap") is not None
    assert report.meta["bone_mesh"].endswith("bone.stl")


# -- non-plate anatomy --------------------------------------------------------

CRANIAL_DIR = REPO_ROOT / "real_cases" / "synthetic_patch"


@pytest.fixture
def imported_patch(tmp_path):
    report, case_path = import_case.import_case(
        CRANIAL_DIR / "surgical_plan.json",
        CRANIAL_DIR / "bone.stl",
        out_dir=tmp_path / "generated",
    )
    assert report.passed, report.summary()
    assert case_path is not None
    return case_path


def test_patch_plan_carries_its_family_into_the_case(imported_patch):
    """Without this the generator would build a plate on a cranial vault."""
    case = json.loads(imported_patch.read_text("utf-8"))
    assert case["implant"]["family"] == "conformal_patch"
    assert case["implant"]["region"]["type"] == "screw_span"


def test_patch_case_gets_no_invented_shaft_envelope(imported_patch):
    envelope = json.loads(imported_patch.read_text("utf-8"))["envelope"]
    assert "footprint_z_mm" not in envelope
    assert "max_length_mm" not in envelope
    assert envelope["max_footprint_mm"] > 0


def test_patch_case_builds_the_patch_family(imported_patch):
    from autoimplants import case_io, params

    case = case_io.load_case(imported_patch)
    resolved = params.for_case(params.default_params(), case)
    assert resolved["family"] == "conformal_patch"


def test_patch_case_carries_the_declared_loads_and_stress_limits(imported_patch):
    """The solver reads the case, not the plan, so the loads have to survive import."""
    case = json.loads(imported_patch.read_text("utf-8"))
    assert {lc["type"] for lc in case["load_cases"]} == {"axial", "bending"}
    assert case["thresholds"]["max_stress_MPa"] > 0.0
    assert case["thresholds"]["max_deflection_mm"] > 0.0
