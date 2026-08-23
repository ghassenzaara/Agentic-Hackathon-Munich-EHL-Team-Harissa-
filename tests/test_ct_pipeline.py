"""Raw DICOM to a validated design, in one pass.

Every other test covers one seam. This one covers the joins, on a phantom built
to have the properties that break real scans: a cortical shell around a marrow
canal, surrounding soft tissue, acquisition noise, anisotropic voxels and oblique
direction cosines.

The phantom is rendered *from* ``inputs/bone.stl`` under a stated pose, so the
right answer is known exactly and the reconstruction can be scored rather than
eyeballed. Generate it with::

    python real_cases/synthetic_ct/make_ct.py

It is ~27 MB and therefore not committed; these tests skip without it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("SimpleITK", reason="DICOM stack not installed; see requirements.txt")
pytest.importorskip("skimage", reason="DICOM stack not installed; see requirements.txt")

from autoimplants import dicom_to_mesh, import_case, landmarks  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PHANTOM = REPO_ROOT / "real_cases" / "synthetic_ct"
SERIES = PHANTOM / "series"
GROUND_TRUTH = PHANTOM / "ground_truth.stl"
PLAN = PHANTOM / "surgical_plan.json"

# The phantom's voxels are 0.8 x 0.8 x 1.25 mm. A reconstruction good to well
# under a voxel means the index-to-patient affine is right; getting it wrong
# shows up as centimetres, not tenths of a millimetre.
MAX_MEAN_DEVIATION_MM = 0.35
MAX_P95_DEVIATION_MM = 0.80


def _require_phantom():
    if not (SERIES.exists() and GROUND_TRUTH.exists() and PLAN.exists()):
        pytest.skip("run real_cases/synthetic_ct/make_ct.py to build the CT phantom")


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    _require_phantom()
    out = tmp_path_factory.mktemp("ct") / "bone.stl"
    result = dicom_to_mesh.dicom_to_mesh(SERIES, out, bone="femur")
    assert result.passed, result.report.summary()
    return out, result


def test_conversion_passes_the_mesh_gate(converted):
    _, result = converted
    assert result.report.by_id("mesh_single_component").status == "PASS"
    assert result.report.by_id("mesh_watertight").status == "PASS"


def test_cortical_shell_does_not_read_as_two_bones(converted):
    """Thresholding a cortex gives a tube, and a tube surfaces as two shells --
    the outer bone and the wall of the medullary canal. Counting components
    naively calls that a second bone in the field of view."""
    _, result = converted
    assert any("internal cavity" in r for r in result.repairs), result.repairs


def test_reconstruction_matches_ground_truth(converted):
    """The whole point of carrying direction cosines and spacing through."""
    import trimesh

    out, _ = converted
    reconstructed = trimesh.load(str(out), force="mesh")
    truth = trimesh.load(str(GROUND_TRUTH), force="mesh")

    samples = truth.sample(8000)
    _, deviation, _ = reconstructed.nearest.on_surface(samples)

    assert deviation.mean() < MAX_MEAN_DEVIATION_MM, deviation.mean()
    assert np.percentile(deviation, 95) < MAX_P95_DEVIATION_MM


def test_reconstruction_is_positioned_not_just_shaped(converted):
    """A mesh built in index space would be the right shape at the wrong place."""
    import trimesh

    out, _ = converted
    reconstructed = trimesh.load(str(out), force="mesh")
    truth = trimesh.load(str(GROUND_TRUTH), force="mesh")

    offset = np.linalg.norm(reconstructed.bounds.mean(axis=0) - truth.bounds.mean(axis=0))
    assert offset < 1.0, f"reconstruction sits {offset:.1f} mm from the truth"


def test_case_imports_from_the_reconstructed_mesh(converted, tmp_path):
    """The plan is in scanner coordinates; the importer has to recover the frame."""
    out, _ = converted
    report, case_path = import_case.import_case(PLAN, out, out_dir=tmp_path / "generated")

    assert report.passed, report.summary()
    assert case_path is not None
    assert report.by_id("plan_screw_entries_on_bone").value < 2.5


def test_imported_ct_case_runs_the_design_loop(converted, tmp_path):
    """End to end: DICOM in, validated design out."""
    if importlib.util.find_spec("cadquery") is None:
        pytest.skip("CAD toolchain not installed")

    from autoimplants.contracts import Report

    out, _ = converted
    _, case_path = import_case.import_case(PLAN, out, out_dir=tmp_path / "generated")
    design_out = tmp_path / "design"

    # SimpleITK and OpenCASCADE can access-violate during Windows interpreter
    # teardown when both native runtimes have been loaded into one process.
    # Production already runs DICOM ingestion and CAD validation as separate CLI
    # stages, so exercise that real boundary here too.
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoimplants.run",
            "--case",
            str(case_path),
            "--validators",
            "geometry",
            "--out",
            str(design_out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = Report.load(design_out / "report.json")

    # What this test owns is the pipeline, so it asserts the DICOM-derived case is
    # designed against like any other -- not one particular failure. It used to
    # require conformance to FAIL at 8.596 mm, the flat plate's number, which made
    # a plate that follows the reconstructed cortex a test failure.
    assert report.by_id("screw_trajectories_clear").status == "PASS"
    assert report.by_id("no_bone_collision").status == "PASS"
    assert [c.id for c in report.checks if c.status == "FAIL"] == []


# -- the landmark helper ------------------------------------------------------


def test_proposed_axis_matches_the_true_shaft_axis(converted):
    """The phantom's pose is known, so the fitted axis has a right answer."""
    import trimesh

    _require_phantom()
    import sys

    sys.path.insert(0, str(PHANTOM))
    from make_ct import bone_pose  # noqa: PLC0415

    out, _ = converted
    mesh = trimesh.load(str(out), force="mesh")
    proposal = landmarks.propose(mesh, mount_side=[1.0, 0.0, 0.0])

    fitted = np.array(proposal["measured"]["shaft_axis"])
    true_axis = bone_pose()[:3, :3] @ np.array([0.0, 0.0, 1.0])

    angle = np.degrees(np.arccos(abs(float(fitted @ true_axis))))
    assert angle < 2.0, f"fitted shaft axis is {angle:.1f} degrees off"


def test_proposed_length_is_about_right(converted):
    import trimesh

    out, _ = converted
    mesh = trimesh.load(str(out), force="mesh")
    proposal = landmarks.propose(mesh, mount_side=[1.0, 0.0, 0.0])
    assert 380.0 < proposal["measured"]["bone_length_mm"] < 420.0


def test_landmark_template_cannot_be_imported(converted, tmp_path):
    """The scaffold must not become a way to fabricate planning data."""
    import trimesh

    from autoimplants.surgical_plan import PlanError, validate_structure

    out, _ = converted
    mesh = trimesh.load(str(out), force="mesh")
    proposal = landmarks.propose(mesh, mount_side=[1.0, 0.0, 0.0])
    template = landmarks.plan_template(proposal, "femur", "TEMPLATE")

    path = tmp_path / "template.json"
    path.write_text(json.dumps(template), encoding="utf-8")

    with pytest.raises(PlanError):
        validate_structure(template)


def test_mount_side_along_the_axis_is_rejected(converted):
    import trimesh

    out, _ = converted
    mesh = trimesh.load(str(out), force="mesh")
    axis = landmarks.propose(mesh, mount_side=[1.0, 0.0, 0.0])["measured"]["shaft_axis"]

    with pytest.raises(landmarks.LandmarkError, match="parallel"):
        landmarks.propose(mesh, mount_side=axis)


# -- region of interest -------------------------------------------------------
#
# A clinical series is not cropped to one bone: a lower-limb scan holds femur and
# tibia, the largest component bridges the joint, and the mesh gate then rejects a
# 900 mm "femur". The plan's landmarks are what say where the planned bone is.


def _two_bar_volume():
    """Volume with two bone-valued bars along z, separated by a gap."""
    volume = np.full((200, 40, 40), -200.0, dtype=np.float32)
    volume[10:70, 18:23, 18:23] = 800.0     # planned bone
    volume[130:190, 18:23, 18:23] = 800.0   # neighbouring bone
    affine = np.diag([1.0, 1.0, 2.0, 1.0])
    affine[:3, 3] = [5.0, -3.0, 100.0]
    return volume, affine


def test_landmark_crop_keeps_the_planned_bone_only():
    volume, affine = _two_bar_volume()
    # Two points on the first bar, in patient mm.
    landmarks_mm = [[25.0, -0.5, 140.0], [25.0, -0.5, 200.0]]

    cropped, shifted = dicom_to_mesh.crop_to_landmarks(volume, affine, landmarks_mm, margin_mm=20.0)

    z_low = shifted[2, 3]
    z_high = z_low + (cropped.shape[0] - 1) * shifted[2, 2]
    assert 100.0 <= z_low <= 140.0
    assert 200.0 <= z_high < 360.0  # the second bar starts at z = 360 mm

    mask = dicom_to_mesh.segment_bone(cropped, 300.0)
    kept = np.argwhere(mask)
    assert kept.size > 0
    # Every voxel kept maps back inside the first bar.
    patient_z = shifted[2, 3] + kept[:, 0] * shifted[2, 2]
    assert patient_z.max() < 360.0


def test_landmark_crop_preserves_patient_coordinates():
    volume, affine = _two_bar_volume()
    landmarks_mm = [[25.0, -0.5, 140.0], [25.0, -0.5, 200.0]]

    cropped, shifted = dicom_to_mesh.crop_to_landmarks(volume, affine, landmarks_mm, margin_mm=30.0)

    full = dicom_to_mesh.mask_to_mesh(dicom_to_mesh.segment_bone(volume[:100], 300.0), affine)
    part = dicom_to_mesh.mask_to_mesh(dicom_to_mesh.segment_bone(cropped, 300.0), shifted)
    # Cropping must move the affine, not the anatomy.
    assert np.allclose(full.bounds[:, :2], part.bounds[:, :2], atol=1e-6)
    assert np.allclose(full.centroid[:2], part.centroid[:2], atol=1e-6)


def test_landmarks_outside_the_volume_are_rejected():
    volume, affine = _two_bar_volume()
    with pytest.raises(dicom_to_mesh.DicomError, match="outside the CT volume"):
        dicom_to_mesh.crop_to_landmarks(volume, affine, [[5000.0, 5000.0, 5000.0]], margin_mm=5.0)


def test_plan_landmarks_reads_the_plan_points():
    if not PLAN.exists():
        pytest.skip("run real_cases/synthetic_ct/make_ct.py to build the CT phantom")
    points = dicom_to_mesh.plan_landmarks(PLAN)
    assert len(points) >= 2
    assert all(len(point) == 3 for point in points)
