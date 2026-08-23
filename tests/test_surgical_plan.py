"""A plan this repo cannot design against must be rejected, by name.

The failure mode these tests exist to prevent is the quiet one: an incomplete
plan that imports anyway, because some default filled the hole, and produces an
implant fitted to numbers nobody supplied.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from autoimplants import surgical_plan
from autoimplants.surgical_plan import PlanError

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_example_plan_is_structurally_valid(example_plan):
    surgical_plan.validate_structure(example_plan)


def test_synthetic_ct_plan_tracks_default_case_constraints():
    default_case = json.loads((REPO_ROOT / "inputs" / "case.json").read_text(encoding="utf-8"))
    ct_plan = json.loads(
        (REPO_ROOT / "real_cases" / "synthetic_ct" / "surgical_plan.json").read_text(
            encoding="utf-8"
        )
    )

    assert ct_plan["thresholds"] == default_case["thresholds"]
    assert ct_plan["load_cases"] == default_case["load_cases"]


@pytest.mark.parametrize("field", surgical_plan.REQUIRED_FIELDS)
def test_missing_required_field_is_rejected_by_name(example_plan, field):
    plan = dict(example_plan)
    plan.pop(field)
    with pytest.raises(PlanError) as exc:
        surgical_plan.validate_structure(plan)
    assert field in str(exc.value)


def test_missing_landmarks_are_rejected(example_plan):
    plan = dict(example_plan)
    plan["coordinate_frame"] = {"note": "no landmarks, no axes"}
    with pytest.raises(PlanError) as exc:
        surgical_plan.validate_structure(plan)
    assert "landmarks" in str(exc.value)


def test_partial_landmarks_are_rejected(example_plan):
    plan = json.loads(json.dumps(example_plan))
    plan["coordinate_frame"]["landmarks"].pop("mount_side_mm")
    with pytest.raises(PlanError) as exc:
        surgical_plan.validate_structure(plan)
    assert "mount_side_mm" in str(exc.value)


def test_zero_length_screw_direction_is_rejected(example_plan):
    plan = json.loads(json.dumps(example_plan))
    plan["screws"][2]["direction"] = [0.0, 0.0, 0.0]
    with pytest.raises(PlanError) as exc:
        surgical_plan.validate_structure(plan)
    assert "screws[2]" in str(exc.value)


def test_unnormalised_screw_direction_is_accepted_and_normalised(example_plan):
    """Magnitude carries no meaning; only the zero vector is an error."""
    plan = json.loads(json.dumps(example_plan))
    plan["screws"][0]["direction"] = [
        7.0 * c for c in plan["screws"][0]["direction"]
    ]
    surgical_plan.validate_structure(plan)

    transform = surgical_plan.frame_transform(plan)
    placed = surgical_plan.transformed_plan(plan, transform)
    assert np.isclose(np.linalg.norm(placed["screws"][0]["direction"]), 1.0)


def test_no_screws_is_rejected(example_plan):
    plan = dict(example_plan)
    plan["screws"] = []
    with pytest.raises(PlanError):
        surgical_plan.validate_structure(plan)


def test_malformed_keepout_is_rejected(example_plan):
    plan = json.loads(json.dumps(example_plan))
    plan["keepouts"][0]["radius_mm"] = -3.0
    with pytest.raises(PlanError) as exc:
        surgical_plan.validate_structure(plan)
    assert "radius_mm" in str(exc.value)


def test_non_sphere_keepout_is_rejected(example_plan):
    plan = json.loads(json.dumps(example_plan))
    plan["keepouts"][0]["type"] = "cylinder"
    with pytest.raises(PlanError) as exc:
        surgical_plan.validate_structure(plan)
    assert "sphere" in str(exc.value)


def test_backwards_footprint_is_rejected(example_plan):
    plan = dict(example_plan)
    plan["footprint_z_mm"] = [280.0, 100.0]
    with pytest.raises(PlanError):
        surgical_plan.validate_structure(plan)


# -- the frame transform -------------------------------------------------------


def test_frame_transform_inverts_the_known_pose(example_plan):
    """The example is the synthetic femur under a known pose, so the recovered
    transform must be that pose's inverse -- to floating point."""
    import sys
    sys.path.insert(0, str(surgical_plan.Path(__file__).resolve().parent.parent
                           / "real_cases" / "example"))
    from make_example import scanner_pose  # noqa: PLC0415

    recovered = surgical_plan.frame_transform(example_plan)
    assert np.allclose(recovered @ scanner_pose(), np.eye(4), atol=1e-6)


def test_frame_transform_is_rigid(example_plan):
    t = surgical_plan.frame_transform(example_plan)
    rotation = t[:3, :3]
    assert np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-9)
    assert np.isclose(np.linalg.det(rotation), 1.0, atol=1e-9)


def test_mount_landmark_on_the_axis_is_ambiguous(example_plan):
    """A mount-side landmark on the shaft axis does not say which way is out."""
    plan = json.loads(json.dumps(example_plan))
    lm = plan["coordinate_frame"]["landmarks"]
    proximal = np.array(lm["proximal_shaft_mm"])
    distal = np.array(lm["distal_shaft_mm"])
    lm["mount_side_mm"] = (proximal + 0.5 * (distal - proximal)).tolist()

    with pytest.raises(PlanError) as exc:
        surgical_plan.frame_transform(plan)
    assert "ambiguous" in str(exc.value)


def test_collapsed_shaft_landmarks_are_rejected(example_plan):
    plan = json.loads(json.dumps(example_plan))
    lm = plan["coordinate_frame"]["landmarks"]
    lm["distal_shaft_mm"] = (np.array(lm["proximal_shaft_mm"]) + 3.0).tolist()

    with pytest.raises(PlanError) as exc:
        surgical_plan.frame_transform(plan)
    assert "shaft axis" in str(exc.value)


def test_explicit_axes_are_honoured():
    """A plan may state the frame outright instead of via landmarks."""
    plan = {
        "coordinate_frame": {
            "axes": {
                "shaft": [0.0, 0.0, 2.0],
                "mount_side": [9.0, 0.0, 0.0],
                "origin_mm": [1.0, 2.0, 3.0],
            }
        }
    }
    t = surgical_plan.frame_transform(plan)
    moved = surgical_plan.transform_points([[1.0, 2.0, 3.0]], t)[0]
    assert np.allclose(moved, [0.0, 0.0, 0.0], atol=1e-9)


def test_transformed_plan_preserves_relative_geometry(example_plan):
    """A change of frame must not move anything relative to anything else."""
    t = surgical_plan.frame_transform(example_plan)
    placed = surgical_plan.transformed_plan(example_plan, t)

    before = np.array([s["entry_mm"] for s in example_plan["screws"]])
    after = np.array([s["entry_mm"] for s in placed["screws"]])
    d_before = np.linalg.norm(before[0] - before[-1])
    d_after = np.linalg.norm(after[0] - after[-1])
    assert np.isclose(d_before, d_after, atol=1e-6)


# -- the implant family the plan asks for -------------------------------------

PATCH_PLAN_PATHS = (
    REPO_ROOT / "real_cases" / "synthetic_patch" / "surgical_plan.json",
    REPO_ROOT / "real_cases" / "synthetic_scapula" / "surgical_plan.json",
)


@pytest.fixture(params=PATCH_PLAN_PATHS, ids=["cranial", "scapula"])
def patch_plan(request):
    return json.loads(request.param.read_text(encoding="utf-8"))


def test_patch_plans_are_structurally_valid_without_a_footprint(patch_plan):
    """A vault or a blade has no shaft to state a footprint_z_mm along."""
    assert "footprint_z_mm" not in patch_plan
    assert surgical_plan.implant_family(patch_plan) == "conformal_patch"
    surgical_plan.validate_structure(patch_plan)


def test_plate_family_still_requires_a_footprint(example_plan):
    plan = dict(example_plan)
    plan.pop("footprint_z_mm")
    with pytest.raises(PlanError, match="footprint_z_mm"):
        surgical_plan.validate_structure(plan)


def test_plan_defaults_to_the_plate_family(example_plan):
    assert surgical_plan.implant_family(example_plan) == "plate"


def test_unknown_family_is_rejected_by_name(patch_plan):
    patch_plan["implant"]["family"] = "hip_stem"
    with pytest.raises(PlanError, match="hip_stem"):
        surgical_plan.validate_structure(patch_plan)


def test_patch_without_a_region_is_rejected(patch_plan):
    patch_plan["implant"].pop("region")
    with pytest.raises(PlanError, match="region"):
        surgical_plan.validate_structure(patch_plan)


def test_patch_with_a_non_positive_margin_is_rejected(patch_plan):
    patch_plan["implant"]["region"]["margin_mm"] = 0.0
    with pytest.raises(PlanError, match="margin_mm"):
        surgical_plan.validate_structure(patch_plan)


def test_patch_plan_skips_the_footprint_checks_against_the_bone(patch_plan):
    """The footprint checks ask about a z range; a patch does not have one."""
    import trimesh

    bone = trimesh.load(
        REPO_ROOT / "real_cases"
        / ("synthetic_patch" if "CRANIAL" in patch_plan["case_id"] else "synthetic_scapula")
        / "bone.stl"
    )
    report = surgical_plan.validate_against_bone(patch_plan, bone)

    assert report.by_id("plan_footprint_within_bone") is None
    assert report.by_id("plan_screws_within_footprint") is None
    assert report.by_id("plan_screw_entries_on_bone").status == "PASS"
    assert report.by_id("plan_screw_trajectories_in_bone").status == "PASS"
