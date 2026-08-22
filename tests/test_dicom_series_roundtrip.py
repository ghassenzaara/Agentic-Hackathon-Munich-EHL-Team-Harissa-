"""A real DICOM series, written and read back, end to end.

test_dicom_to_mesh.py exercises segmentation and surfacing on an array. This
covers the part that array cannot: SimpleITK reading an actual series off disk
and reconstructing Hounsfield units, spacing and patient position from the tags.
That is where a wrong answer is least visible -- the mesh still looks like a
bone, it is just the wrong size or in the wrong place.

The series is synthesised here. Public CT datasets are far too large to commit,
and patient imaging must never enter this repository at all; validating against a
public dataset is a documented manual step (see docs/real-ct-cases.md).
"""

from __future__ import annotations

import numpy as np
import pytest

from autoimplants import dicom_to_mesh

pytest.importorskip("pydicom", reason="DICOM stack not installed; see requirements.txt")
pytest.importorskip("SimpleITK", reason="DICOM stack not installed; see requirements.txt")
pytest.importorskip("scipy", reason="DICOM stack not installed; see requirements.txt")
pytest.importorskip("skimage", reason="DICOM stack not installed; see requirements.txt")

PIXEL_SPACING_MM = (0.8, 0.8)     # (row, column)
SLICE_THICKNESS_MM = 1.25
N_SLICES = 96
GRID = 100                        # rows == columns
SHAFT_RADIUS_MM = 12.0
BONE_HU = 900
SOFT_TISSUE_HU = 40
ORIGIN_MM = (-100.0, 250.0, -60.0)

# CT stores unsigned counts; HU comes back via slope/intercept. Getting this
# wrong is the classic "everything is 1024 HU too high" bug.
RESCALE_INTERCEPT = -1024


def _write_series(directory):
    import pydicom
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

    series_uid = pydicom.uid.generate_uid()
    study_uid = pydicom.uid.generate_uid()
    frame_uid = pydicom.uid.generate_uid()

    rows = cols = GRID
    ii, jj = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    r_mm = np.sqrt(
        ((ii - rows / 2) * PIXEL_SPACING_MM[0]) ** 2
        + ((jj - cols / 2) * PIXEL_SPACING_MM[1]) ** 2
    )
    hu = np.where(r_mm <= SHAFT_RADIUS_MM, BONE_HU, SOFT_TISSUE_HU)
    stored = (hu - RESCALE_INTERCEPT).astype(np.uint16)

    for k in range(N_SLICES):
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian

        path = directory / f"slice_{k:04d}.dcm"
        ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)

        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        ds.SeriesInstanceUID = series_uid
        ds.StudyInstanceUID = study_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "CT"

        ds.Rows, ds.Columns = rows, cols
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.PixelSpacing = [PIXEL_SPACING_MM[0], PIXEL_SPACING_MM[1]]
        ds.SliceThickness = SLICE_THICKNESS_MM
        ds.SpacingBetweenSlices = SLICE_THICKNESS_MM
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.ImagePositionPatient = [
            ORIGIN_MM[0],
            ORIGIN_MM[1],
            ORIGIN_MM[2] + k * SLICE_THICKNESS_MM,
        ]
        ds.InstanceNumber = k + 1
        ds.RescaleIntercept = RESCALE_INTERCEPT
        ds.RescaleSlope = 1
        ds.PixelData = stored.tobytes()

        ds.save_as(str(path), enforce_file_format=True)

    return directory


@pytest.fixture(scope="module")
def series(tmp_path_factory):
    return _write_series(tmp_path_factory.mktemp("series"))


def test_hounsfield_units_are_reconstructed(series):
    volume, _ = dicom_to_mesh.load_series(series)
    assert volume.shape == (N_SLICES, GRID, GRID)
    assert np.isclose(volume.max(), BONE_HU, atol=1.0)
    assert np.isclose(volume.min(), SOFT_TISSUE_HU, atol=1.0)


def test_affine_carries_spacing_and_origin(series):
    _, affine = dicom_to_mesh.load_series(series)
    assert np.allclose(np.diag(affine[:3, :3]), [*PIXEL_SPACING_MM, SLICE_THICKNESS_MM])
    assert np.allclose(affine[:3, 3], ORIGIN_MM)


def test_series_converts_to_a_mesh_at_the_right_size(series, tmp_path):
    out = tmp_path / "bone.stl"
    result = dicom_to_mesh.dicom_to_mesh(series, out, bone="femur")

    assert out.exists()

    import trimesh

    mesh = trimesh.load(str(out), force="mesh")
    extents = mesh.bounds[1] - mesh.bounds[0]

    assert 2 * SHAFT_RADIUS_MM - 3 <= extents[0] <= 2 * SHAFT_RADIUS_MM + 4
    assert extents[2] >= (N_SLICES - 4) * SLICE_THICKNESS_MM
    assert mesh.bounds[0][1] > 200.0  # positioned by ImagePositionPatient

    # A 120 mm shaft is nowhere near a femur, so the gate is expected to say so:
    # the point is that it ran and reported, not that this fixture passes.
    assert result.report.by_id("mesh_units_plausible") is not None


def test_multiple_series_in_one_directory_is_refused(series, tmp_path_factory):
    """Silently picking one is how you segment the scout scan by accident."""
    mixed = tmp_path_factory.mktemp("mixed")
    for f in list(series.glob("*.dcm"))[:4]:
        (mixed / f.name).write_bytes(f.read_bytes())
    second = _write_series(tmp_path_factory.mktemp("second"))
    for f in list(second.glob("*.dcm"))[:4]:
        (mixed / f"other_{f.name}").write_bytes(f.read_bytes())

    with pytest.raises(dicom_to_mesh.DicomError, match="series"):
        dicom_to_mesh.load_series(mixed)
