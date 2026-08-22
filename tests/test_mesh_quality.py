"""The mesh gate has to reject exactly the meshes a real segmentation produces.

Each test builds the defect deliberately, because none of them can be produced by
inputs/make_bone.py -- that is the point of the gate.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh

from autoimplants import mesh_quality

REPO_ROOT = Path(__file__).resolve().parent.parent
BONE = REPO_ROOT / "inputs" / "bone.stl"


@pytest.fixture
def bone() -> trimesh.Trimesh:
    return trimesh.load(str(BONE), force="mesh")


def _write(mesh, tmp_path, name="mesh.stl") -> str:
    path = tmp_path / name
    mesh.export(str(path))
    return str(path)


def test_clean_bone_passes(tmp_path, bone):
    result = mesh_quality.gate(_write(bone, tmp_path), bone="femur")
    assert result.passed
    assert result.mesh is not None


def test_missing_file_errors_without_raising(tmp_path):
    result = mesh_quality.gate(tmp_path / "nope.stl", bone="femur")
    assert not result.passed
    assert result.mesh is None


def test_metre_scale_mesh_is_rejected_with_a_hint(tmp_path, bone):
    """A femur written in metres passes every mm threshold meaninglessly."""
    tiny = bone.copy()
    tiny.apply_scale(0.001)
    result = mesh_quality.gate(_write(tiny, tmp_path), bone="femur")

    assert not result.passed
    check = result.report.by_id("mesh_units_plausible")
    assert check.status == "FAIL"
    assert "--units m" in check.message


def test_declared_units_are_applied(tmp_path, bone):
    """The same file passes once the caller says what units it is in."""
    in_cm = bone.copy()
    in_cm.apply_scale(0.1)
    result = mesh_quality.gate(_write(in_cm, tmp_path), bone="femur", units="cm")

    assert result.passed
    assert any("scaled from cm" in r for r in result.repairs)


def test_longest_extent_ignores_orientation(bone):
    """A rotated femur is still 400 mm long, whatever its bounding box says."""
    straight = mesh_quality.longest_extent_mm(bone)

    tilted = bone.copy()
    tilted.apply_transform(trimesh.transformations.euler_matrix(0.4, -0.7, 1.1))
    assert np.isclose(straight, mesh_quality.longest_extent_mm(tilted), rtol=1e-6)

    # The bounding box, which this replaced, does not survive the rotation.
    assert float(np.max(tilted.bounds[1] - tilted.bounds[0])) < straight - 10.0


def test_speckle_islands_are_dropped(tmp_path, bone):
    """Segmentation noise is removed, not treated as anatomy."""
    speck = trimesh.creation.icosphere(radius=1.5)
    speck.apply_translation([90.0, 90.0, 90.0])
    dirty = trimesh.util.concatenate([bone, speck])

    result = mesh_quality.gate(_write(dirty, tmp_path), bone="femur")

    assert result.passed
    assert any("speckle island" in r for r in result.repairs)
    # The island is gone, so the mesh bounds no longer reach out to it.
    assert result.mesh.bounds[1][0] < 80.0


def test_second_comparable_component_fails(tmp_path, bone):
    """Two bones in the field of view is a human decision, not an auto-repair."""
    second = bone.copy()
    second.apply_translation([200.0, 0.0, 0.0])
    both = trimesh.util.concatenate([bone, second])

    result = mesh_quality.gate(_write(both, tmp_path), bone="femur")

    assert not result.passed
    assert result.report.by_id("mesh_single_component").status == "FAIL"


def test_open_mesh_is_repaired_or_reported(tmp_path, bone):
    """Containment queries are undefined on an open surface, so it cannot pass silently."""
    holed = bone.copy()
    keep = np.ones(len(holed.faces), dtype=bool)
    keep[:40] = False
    holed.update_faces(keep)
    assert not holed.is_watertight

    result = mesh_quality.gate(_write(holed, tmp_path), bone="femur")
    check = result.report.by_id("mesh_watertight")

    if check.status == "PASS":
        assert result.mesh.is_watertight
        assert any("filled holes" in r for r in result.repairs)
    else:
        assert result.mesh is None


def test_face_budget_reported(tmp_path, bone):
    """A mesh over budget is either decimated or explicitly flagged as slow."""
    result = mesh_quality.gate(_write(bone, tmp_path), bone="femur", max_faces=500)
    check = result.report.by_id("mesh_face_budget")
    assert check.status == "PASS"
    assert check.value <= 500 or "decimation is unavailable" in check.message
