"""Write a femur-shaped CT series, to exercise the DICOM path on realistic data.

Patient imaging cannot be committed to this repository and public datasets are
too large, so the end-to-end DICOM test needs a scan that is synthetic but not
*easy*. A smooth cylinder in an axis-aligned scanner -- which is what the unit
tests use -- exercises almost none of what goes wrong with real data.

What this fixture adds, one property per failure it is meant to catch:

    cortical shell + marrow   thresholding must find the cortex, not the whole
                              limb; a solid bone makes any threshold look good
    surrounding soft tissue   a 300 HU cut has to actually separate them
    acquisition noise         morphological cleanup has to survive speckle
    oblique direction cosines the index-to-patient affine has to be applied, and
                              applied the right way round. This is the one that
                              fails silently: the mesh still looks like a bone,
                              it is just rotated and in the wrong place
    anisotropic spacing       0.8 x 0.8 x 1.25 mm, so a transposed spacing
                              vector distorts the anatomy instead of cancelling

Ground truth is known exactly -- the bone is inputs/bone.stl under a stated pose
-- so the reconstruction can be scored rather than eyeballed.

Run:  python real_cases/synthetic_ct/make_ct.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np
import trimesh

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
INPUTS = REPO_ROOT / "inputs"
sys.path.insert(0, str(REPO_ROOT))

SERIES_DIR = HERE / "series"

# Acquisition geometry.
PIXEL_SPACING_MM = (0.8, 0.8)   # (row, column)
SLICE_THICKNESS_MM = 1.25
MARGIN_MM = 18.0                # field of view padding around the limb

# Where the femur sits in patient coordinates, and how the scanner is oriented
# relative to it. Fixed numbers so the fixture is regenerable and diffable.
BONE_POSE_DEG = (7.0, -11.0, 23.0)
BONE_POSE_MM = (-142.0, 96.5, -310.0)
SCANNER_TILT_DEG = 9.0          # rotates the acquisition axes off the patient axes

# Hounsfield values. Cortical bone runs ~1200-1900, marrow ~150-400, muscle ~40.
HU_CORTICAL = 1350.0
HU_MARROW = 260.0
HU_SOFT_TISSUE = 45.0
HU_AIR = -1000.0
HU_NOISE_SD = 28.0

CORTEX_THICKNESS_MM = 5.0
SOFT_TISSUE_RADIUS_MM = 46.0    # thigh envelope around the shaft

RESCALE_INTERCEPT = -1024
RNG_SEED = 20260822


def bone_pose() -> np.ndarray:
    """Where the femur sits in patient coordinates."""
    rx, ry, rz = np.radians(BONE_POSE_DEG)
    pose = trimesh.transformations.euler_matrix(rx, ry, rz, "sxyz")
    pose[:3, 3] = BONE_POSE_MM
    return pose


def scanner_axes() -> np.ndarray:
    """Direction cosines of the acquisition grid: 3x3, columns are row/col/slice."""
    tilt = np.radians(SCANNER_TILT_DEG)
    return trimesh.transformations.euler_matrix(tilt, tilt * 0.6, 0.0, "sxyz")[:3, :3]


def _solid_mask(mesh, xs, ys, zs) -> np.ndarray:
    """Rasterise the mesh onto the grid, as [z, y, x].

    One batched ray cast per (y, z) lane rather than a containment test per
    voxel: the grid here is a few million points, and `mesh.contains` on that is
    minutes where this is under a second.
    """
    zz, yy = np.meshgrid(zs, ys, indexing="ij")
    flat_z, flat_y = zz.reshape(-1), yy.reshape(-1)
    n = flat_z.size

    origins = np.column_stack(
        [np.full(n, float(mesh.bounds[0][0]) - 10.0), flat_y, flat_z]
    )
    directions = np.tile([1.0, 0.0, 0.0], (n, 1))
    hits, ray_idx, _ = mesh.ray.intersects_location(
        ray_origins=origins, ray_directions=directions
    )

    mask = np.zeros((len(zs), len(ys), len(xs)), dtype=bool)
    if not len(hits):
        return mask

    per_ray: list[list[float]] = [[] for _ in range(n)]
    for x, idx in zip(hits[:, 0], ray_idx):
        per_ray[int(idx)].append(float(x))

    for ray, crossings in enumerate(per_ray):
        if len(crossings) < 2:
            continue
        si, li = divmod(ray, len(ys))
        ordered = np.sort(np.asarray(crossings))
        for a, b in zip(ordered[0::2], ordered[1::2]):
            lo = np.searchsorted(xs, a, side="left")
            hi = np.searchsorted(xs, b, side="right")
            mask[si, li, lo:hi] = True
    return mask


def build_volume():
    """The HU volume, its index-to-patient affine, and the ground-truth mesh."""
    from scipy import ndimage

    mesh = trimesh.load(str(INPUTS / "bone.stl"), force="mesh")
    truth = mesh.copy()
    truth.apply_transform(bone_pose())

    # Work in the acquisition frame: express the bone there, rasterise on an
    # axis-aligned grid, and let the direction cosines carry the rotation back.
    axes = scanner_axes()
    to_grid = np.eye(4)
    to_grid[:3, :3] = axes.T
    in_grid = truth.copy()
    in_grid.apply_transform(to_grid)

    lo = in_grid.bounds[0] - MARGIN_MM
    hi = in_grid.bounds[1] + MARGIN_MM
    xs = np.arange(lo[0], hi[0], PIXEL_SPACING_MM[1])
    ys = np.arange(lo[1], hi[1], PIXEL_SPACING_MM[0])
    zs = np.arange(lo[2], hi[2], SLICE_THICKNESS_MM)

    solid = _solid_mask(in_grid, xs, ys, zs)

    # Hollow out a medullary canal. The synthetic femur is a solid ellipse, so
    # the marrow has to be invented -- but a bone with no canal makes any
    # threshold look competent.
    erode_iters = max(int(round(CORTEX_THICKNESS_MM / PIXEL_SPACING_MM[0])), 1)
    marrow = ndimage.binary_erosion(solid, iterations=erode_iters)
    cortex = solid & ~marrow

    # Soft tissue: a thigh-shaped envelope around the shaft, so the threshold has
    # something to separate the bone from.
    dilate_iters = int(round(SOFT_TISSUE_RADIUS_MM / PIXEL_SPACING_MM[0]))
    limb = ndimage.binary_dilation(solid, iterations=dilate_iters)

    volume = np.full(solid.shape, HU_AIR, dtype=np.float32)
    volume[limb] = HU_SOFT_TISSUE
    volume[marrow] = HU_MARROW
    volume[cortex] = HU_CORTICAL

    rng = np.random.default_rng(RNG_SEED)
    volume += rng.normal(0.0, HU_NOISE_SD, size=volume.shape).astype(np.float32)

    affine = np.eye(4)
    affine[:3, :3] = axes @ np.diag(
        [PIXEL_SPACING_MM[1], PIXEL_SPACING_MM[0], SLICE_THICKNESS_MM]
    )
    affine[:3, 3] = axes @ np.array([xs[0], ys[0], zs[0]])
    return volume, affine, truth


def write_series(volume, affine, directory: Path) -> Path:
    """Write the volume as a real DICOM CT series."""
    import pydicom
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)

    series_uid = pydicom.uid.generate_uid()
    study_uid = pydicom.uid.generate_uid()
    frame_uid = pydicom.uid.generate_uid()

    axes = affine[:3, :3]
    col_dir = axes[:, 0] / np.linalg.norm(axes[:, 0])
    row_dir = axes[:, 1] / np.linalg.norm(axes[:, 1])
    slice_dir = axes[:, 2]

    n_slices, rows, cols = volume.shape
    stored = np.clip(volume - RESCALE_INTERCEPT, 0, 65535).astype(np.uint16)

    for k in range(n_slices):
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
        ds.SeriesDescription = "SYNTHETIC femur phantom -- not patient data"

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
        ds.ImageOrientationPatient = [*col_dir, *row_dir]
        ds.ImagePositionPatient = list(affine[:3, 3] + slice_dir * k)
        ds.InstanceNumber = k + 1
        ds.RescaleIntercept = RESCALE_INTERCEPT
        ds.RescaleSlope = 1
        ds.PixelData = stored[k].tobytes()

        ds.save_as(str(path), enforce_file_format=True)

    return directory


def write_plan(path: Path) -> Path:
    """The synthetic surgical plan, expressed in this phantom's patient coordinates.

    The plan is not invented here: it is ``inputs/screw_positions.json`` and
    ``inputs/keepout_zones.json``, the declared pre-solved planning input, moved
    into the frame the scan was rendered in. That keeps the end-to-end test
    honest in both directions -- the planning data is the same data the project
    has always used, and the frame is genuinely a scanner frame the importer has
    to recover.
    """
    import json

    pose = bone_pose()

    def to_patient(points):
        pts = np.atleast_2d(np.asarray(points, dtype=float))
        return (pts @ pose[:3, :3].T + pose[:3, 3])[0].tolist()

    def rotate(vectors):
        return (np.atleast_2d(np.asarray(vectors, dtype=float)) @ pose[:3, :3].T)[0].tolist()

    case = json.loads((INPUTS / "case.json").read_text(encoding="utf-8"))
    screws = json.loads((INPUTS / "screw_positions.json").read_text(encoding="utf-8"))["screws"]
    zones = json.loads((INPUTS / "keepout_zones.json").read_text(encoding="utf-8"))["zones"]

    plan = {
        "case_id": "SYNTH-CT-FEMUR-001",
        "provenance": "SYNTHETIC PHANTOM -- planning data is inputs/ expressed in the "
                      "phantom's patient coordinates. Not patient data. No clinical or "
                      "regulatory claim.",
        "bone": "femur",
        "side": "right",
        "approach": "lateral",
        "coordinate_frame": {
            "note": "Landmarks on the shaft axis of the phantom, in patient "
                    "coordinates. autoimplants.landmarks proposes these from a mesh "
                    "when the true pose is not known.",
            "landmarks": {
                "proximal_shaft_mm": to_patient([0.0, 0.0, 0.0]),
                "distal_shaft_mm": to_patient([0.0, 0.0, 400.0]),
                "mount_side_mm": to_patient([35.0, 0.0, 200.0]),
            },
        },
        "footprint_z_mm": case["envelope"]["footprint_z_mm"],
        "screws": [
            {
                "id": s["id"],
                "entry_mm": to_patient(s["entry_mm"]),
                "direction": rotate(s["direction"]),
                "diameter_mm": s["diameter_mm"],
                "length_mm": s["length_mm"],
            }
            for s in screws
        ],
        "keepouts": [
            {
                "id": z["id"],
                "type": "sphere",
                "center_mm": to_patient(z["center_mm"]),
                "radius_mm": z["radius_mm"],
                "rationale": z.get("rationale", ""),
            }
            for z in zones
        ],
        "material": case["material"],
        "envelope": {
            "max_width_mm": case["envelope"]["max_width_mm"],
            "max_standoff_mm": case["envelope"]["max_standoff_mm"],
            "thickness_bounds_mm": case["envelope"]["thickness_bounds_mm"],
        },
        "thresholds": case["thresholds"],
        "load_cases": case["load_cases"],
        "iteration_budget": case["iteration_budget"],
    }
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    volume, affine, truth = build_volume()
    write_series(volume, affine, SERIES_DIR)

    truth.export(str(HERE / "ground_truth.stl"))
    write_plan(HERE / "surgical_plan.json")

    print(f"wrote {SERIES_DIR} ({volume.shape[0]} slices of {volume.shape[1]}x{volume.shape[2]})")
    print(f"wrote {HERE / 'ground_truth.stl'} -- the bone the series was rendered from")
    print("\nconvert it with:")
    print("  python -m autoimplants.dicom_to_mesh \\")
    print(f"      --dicom-dir {SERIES_DIR.relative_to(REPO_ROOT).as_posix()} \\")
    print("      --bone femur --out real_cases/synthetic_ct/bone.stl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
