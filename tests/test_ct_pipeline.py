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

import json
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
    pytest.importorskip("cadquery", reason="CAD toolchain not installed")

    from autoimplants import case_io
    from autoimplants.export import export_implant
    from autoimplants.generator import build_implant
    from autoimplants.params import default_params
    from autoimplants.validators import geometry

    out, _ = converted
    _, case_path = import_case.import_case(PLAN, out, out_dir=tmp_path / "generated")

    case = case_io.set_active_case(case_io.load_case(case_path), case_path)
    stl = export_implant(build_implant(default_params()), tmp_path / "implant")
    report = geometry.validate(str(stl), case)

    # The plate is built and seated: everything except conformance passes, which
    # is the same failure the synthetic case has and the one Devin is asked to fix.
    assert report.by_id("screw_trajectories_clear").status == "PASS"
    assert report.by_id("no_bone_collision").status == "PASS"
    assert report.by_id("bone_conformance_gap").status == "FAIL"

    # And it agrees with the exact-mesh case to within the reconstruction error.
    assert abs(report.by_id("bone_conformance_gap").value - 8.596) < 0.5


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
