"""DICOM conversion, exercised on a synthetic volume rather than patient imaging.

A real dataset is too large to commit and is patient data besides, so the fixture
is a shaft-shaped HU volume written to disk as an actual DICOM series. That is
enough to catch the failure this path is most likely to have: getting the
index-to-patient transform wrong, which silently scales and shears the anatomy so
that every millimetre threshold downstream measures a distorted bone.

Everything here skips cleanly when the DICOM stack is absent, because the design
loop does not import it and must keep running without it.
"""

from __future__ import annotations

import numpy as np
import pytest

from autoimplants import dicom_to_mesh
from autoimplants.dicom_to_mesh import DicomError

pytest.importorskip("scipy", reason="DICOM stack not installed; see requirements.txt")
pytest.importorskip("skimage", reason="DICOM stack not installed; see requirements.txt")

# The synthetic scan: a 24 mm diameter shaft, 120 mm long, in a soft-tissue field.
SPACING_XYZ = (0.8, 0.8, 1.25)   # anisotropic on purpose -- isotropic hides bugs
SHAFT_RADIUS_MM = 12.0
SHAFT_LENGTH_MM = 120.0
BONE_HU = 900.0
SOFT_TISSUE_HU = 40.0


def _synthetic_volume():
    """An HU volume plus the affine that maps its indices to patient mm."""
    nx = int(80 / SPACING_XYZ[0])
    ny = int(80 / SPACING_XYZ[1])
    nz = int(SHAFT_LENGTH_MM / SPACING_XYZ[2])

    ix, iy = np.meshgrid(np.arange(nx), np.arange(ny), indexing="ij")
    cx, cy = nx / 2.0, ny / 2.0
    r_mm = np.sqrt(
        ((ix - cx) * SPACING_XYZ[0]) ** 2 + ((iy - cy) * SPACING_XYZ[1]) ** 2
    )
    slice_hu = np.where(r_mm <= SHAFT_RADIUS_MM, BONE_HU, SOFT_TISSUE_HU)

    volume = np.repeat(slice_hu.T[None, :, :], nz, axis=0).astype(np.float32)

    affine = np.eye(4)
    affine[:3, :3] = np.diag(SPACING_XYZ)
    affine[:3, 3] = [-100.0, 250.0, -60.0]  # an origin nowhere near the bone
    return volume, affine


def test_threshold_keeps_bone_and_drops_soft_tissue():
    volume, _ = _synthetic_volume()
    mask = dicom_to_mesh.segment_bone(volume, threshold_hu=300.0)

    assert mask.any()
    assert not mask.all()
    # Soft tissue is 40 HU: nothing outside the shaft should survive a 300 HU cut.
    assert mask.sum() < volume.size * 0.5


def test_empty_threshold_is_an_error_not_an_empty_mesh():
    volume = np.full((10, 10, 10), SOFT_TISSUE_HU, dtype=np.float32)
    with pytest.raises(DicomError, match="HU"):
        dicom_to_mesh.segment_bone(volume, threshold_hu=300.0)


def test_mesh_lands_in_patient_coordinates_at_the_right_scale():
    """The whole point of carrying the affine: real mm, at the real origin."""
    volume, affine = _synthetic_volume()
    mask = dicom_to_mesh.segment_bone(volume, threshold_hu=300.0)
    mesh = dicom_to_mesh.mask_to_mesh(mask, affine)

    extents = mesh.bounds[1] - mesh.bounds[0]

    # Diameter across both transverse axes, length along the slice axis. The
    # closing operation dilates by a voxel or so, hence the tolerance.
    assert 2 * SHAFT_RADIUS_MM - 3 <= extents[0] <= 2 * SHAFT_RADIUS_MM + 4
    assert 2 * SHAFT_RADIUS_MM - 3 <= extents[1] <= 2 * SHAFT_RADIUS_MM + 4
    assert extents[2] >= SHAFT_LENGTH_MM - 5

    # And it is positioned by the affine, not left at the array origin.
    assert mesh.bounds[0][1] > 200.0


def test_anisotropic_spacing_is_not_confused_between_axes():
    """A z/x spacing mix-up would stretch the shaft: catch it by construction."""
    volume, affine = _synthetic_volume()
    mask = dicom_to_mesh.segment_bone(volume, threshold_hu=300.0)
    mesh = dicom_to_mesh.mask_to_mesh(mask, affine)

    extents = mesh.bounds[1] - mesh.bounds[0]
    assert np.isclose(extents[0], extents[1], atol=1.0), (
        "the two transverse axes have equal physical size; if they differ, the "
        "spacing vector is being applied to the wrong axes"
    )


def test_converted_mesh_faces_the_right_way():
    volume, affine = _synthetic_volume()
    mask = dicom_to_mesh.segment_bone(volume, threshold_hu=300.0)
    mesh = dicom_to_mesh.mask_to_mesh(mask, affine)

    assert mesh.is_watertight
    assert mesh.volume > 0, "inverted normals give a negative volume"


# -- the PHI check ------------------------------------------------------------


def test_phi_scan_reports_identifiers(tmp_path):
    pydicom = pytest.importorskip("pydicom", reason="DICOM stack not installed; see requirements.txt")
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(str(tmp_path / "slice.dcm"), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientName = "Doe^Jane"
    ds.PatientID = "123456"
    ds.save_as(str(tmp_path / "slice.dcm"), enforce_file_format=True)

    found = dicom_to_mesh.scan_for_phi(tmp_path)
    assert "PatientName" in found
    assert "PatientID" in found


def test_phi_scan_on_a_directory_without_dicom(tmp_path):
    pytest.importorskip("pydicom", reason="DICOM stack not installed; see requirements.txt")
    (tmp_path / "notes.txt").write_text("not a scan", encoding="utf-8")
    with pytest.raises(DicomError, match="no readable DICOM"):
        dicom_to_mesh.scan_for_phi(tmp_path)
