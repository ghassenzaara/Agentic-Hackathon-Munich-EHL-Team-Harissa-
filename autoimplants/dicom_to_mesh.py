"""Phase 2: raw DICOM series to a candidate bone mesh.

    python -m autoimplants.dicom_to_mesh \\
        --dicom-dir <dir> --bone femur --out real_cases/<case_id>/bone.stl

This is deliberately a *separate* command from the implant loop. DICOM is
scanner-slice data; it has to be segmented before anything in this repo can
design against it, and segmentation is the step where clinical judgement lives.
Threshold plus connected components is research/demo tooling -- it produces a
plausible femur, not a correct one. Nobody should run the output straight into a
design without looking at it, which is why the loop still consumes a mesh plus a
surgical plan and never a DICOM directory.

What this does do honestly: read the series with its real spacing and
orientation, convert to Hounsfield units, threshold at a conservative cortical
value, keep the largest connected component, and surface it with marching cubes
in *patient* coordinates -- not index coordinates. Getting that transform wrong
would silently scale and shear the anatomy, and every millimetre threshold
downstream would be measuring a distorted bone.

The DICOM stack (pydicom, SimpleITK, scikit-image, scipy) is in requirements.txt
with everything else, so `bash setup.sh` is the only install step. It is the
heaviest thing in there by a wide margin and nothing in the design loop imports
it, so the imports below stay lazy: a missing DICOM dependency must never break
`autoimplants.run`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

import numpy as np

from . import mesh_quality

# Cortical bone runs roughly 300-1900 HU; trabecular bone overlaps soft tissue
# below that. Starting conservatively (high) keeps soft tissue out at the cost of
# thinning the cortex, which is the safer error for a surface nobody should trust
# without review.
DEFAULT_THRESHOLD_HU = 300.0

# DICOM tags that carry direct identifiers. Not an exhaustive de-identification
# audit -- enough to notice that a series was never de-identified at all.
PHI_TAGS = {
    "PatientName": (0x0010, 0x0010),
    "PatientID": (0x0010, 0x0020),
    "PatientBirthDate": (0x0010, 0x0030),
    "PatientAddress": (0x0010, 0x1040),
    "PatientTelephoneNumbers": (0x0010, 0x2154),
    "OtherPatientIDs": (0x0010, 0x1000),
    "ReferringPhysicianName": (0x0008, 0x0090),
    "InstitutionName": (0x0008, 0x0080),
    "AccessionNumber": (0x0008, 0x0050),
    "StudyDate": (0x0008, 0x0020),
}


class DicomError(RuntimeError):
    """A DICOM directory this tool cannot turn into a volume."""


def _require(module: str):
    """Import a DICOM-only dependency lazily, or explain how to get it.

    Lazy on purpose: these are the heaviest packages in requirements.txt and
    nothing in the design loop imports them, so an environment missing one must
    fail here with an instruction rather than at ``import autoimplants.run``.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise DicomError(
            f"{module} is not installed. Re-run the environment bootstrap:\n"
            f"    bash setup.sh\n"
            f"or install directly:\n"
            f"    uv pip install -r requirements.txt"
        ) from exc


# -- reading ------------------------------------------------------------------


def scan_for_phi(dicom_dir: str | Path) -> dict[str, str]:
    """Identifying tags that are populated in the first slice of the series.

    Reported, never stripped: de-identification is the data owner's decision and
    their audit trail, not something a mesh tool should do quietly on the way past.
    """
    pydicom = _require("pydicom")

    files = sorted(Path(dicom_dir).rglob("*"))
    for f in files:
        if not f.is_file():
            continue
        try:
            # Not force=True. Forcing makes dcmread accept any file at all, so a
            # directory containing no DICOM would return "no identifiers found"
            # -- a privacy check that silently passes because it read nothing is
            # worse than no check.
            ds = pydicom.dcmread(str(f), stop_before_pixels=True)
        except Exception:
            continue
        found = {}
        for name, tag in PHI_TAGS.items():
            value = ds.get(tag)
            if value is not None and str(value.value).strip():
                found[name] = str(value.value)
        return found
    raise DicomError(f"no readable DICOM files under {dicom_dir}")


def load_series(dicom_dir: str | Path):
    """Read a DICOM series into an HU volume plus its index-to-patient affine.

    Returns ``(volume, affine)`` where ``volume`` is indexed ``[z, y, x]`` in
    Hounsfield units and ``affine`` is the 4x4 mapping a continuous ``(x, y, z)``
    index to patient coordinates in mm.
    """
    sitk = _require("SimpleITK")

    reader = sitk.ImageSeriesReader()
    series = reader.GetGDCMSeriesIDs(str(dicom_dir))
    if not series:
        raise DicomError(f"no DICOM series found under {dicom_dir}")
    if len(series) > 1:
        # Picking one silently is how you segment the scout scan by accident.
        raise DicomError(
            f"{len(series)} DICOM series found under {dicom_dir}. Point --dicom-dir "
            f"at a single series: {', '.join(series)}"
        )

    files = reader.GetGDCMSeriesFileNames(str(dicom_dir), series[0])
    reader.SetFileNames(files)
    image = reader.Execute()

    volume = sitk.GetArrayFromImage(image).astype(np.float32)  # [z, y, x], HU

    spacing = np.array(image.GetSpacing(), dtype=float)          # (x, y, z) mm
    direction = np.array(image.GetDirection(), dtype=float).reshape(3, 3)
    origin = np.array(image.GetOrigin(), dtype=float)

    affine = np.eye(4)
    affine[:3, :3] = direction @ np.diag(spacing)
    affine[:3, 3] = origin
    return volume, affine


# -- segmentation -------------------------------------------------------------


def segment_bone(volume: np.ndarray, threshold_hu: float = DEFAULT_THRESHOLD_HU) -> np.ndarray:
    """Binary mask of the largest connected structure above ``threshold_hu``.

    The largest component is the bone in a cropped limb series. In a whole-pelvis
    series it may be the pelvis, or two femurs bridged by a partial volume. The
    mesh gate catches the bridged case; the rest is why the output needs review.
    """
    ndimage = _require("scipy.ndimage")

    mask = volume >= float(threshold_hu)
    if not mask.any():
        raise DicomError(
            f"no voxel reaches {threshold_hu:.0f} HU. Either the series is not CT, "
            f"or the rescale slope/intercept were not applied -- check the source data"
        )

    labels, n = ndimage.label(mask)
    if n > 1:
        sizes = ndimage.sum(mask, labels, index=range(1, n + 1))
        mask = labels == (int(np.argmax(sizes)) + 1)

    # Close pinholes left by the threshold in thin cortex, so marching cubes does
    # not produce a surface riddled with holes the gate then has to repair.
    return ndimage.binary_closing(mask, iterations=2)


# How far outside the planned landmarks the region of interest still reaches. The
# plan's landmarks sit on the shaft, inset from both bone ends, so the margin has
# to cover the condyles and the neck as well as the surrounding cortex.
ROI_MARGIN_MM = 60.0


def crop_to_landmarks(
    volume: np.ndarray,
    affine: np.ndarray,
    landmarks_mm: list[np.ndarray] | np.ndarray,
    margin_mm: float = ROI_MARGIN_MM,
):
    """Restrict the volume to the region the surgical plan is about.

    A clinical series is rarely cropped to one bone: a lower-limb scan holds the
    femur and the tibia bridged at the joint, so the largest connected structure
    spans both and the mesh gate rejects it for being far too long -- correctly,
    because that surface is not a femur. The plan already says where the bone is,
    in the same patient coordinates as the series, so its landmarks define the
    region of interest instead of a hand-tuned slice range.

    Returns ``(volume, affine)`` for the sub-volume; the affine is shifted so the
    cropped indices still map to the same patient coordinates as before.
    """
    points = np.asarray(landmarks_mm, dtype=float).reshape(-1, 3)
    if len(points) == 0:
        raise DicomError("no landmarks to crop to")

    # Patient mm -> continuous (x, y, z) index, then to the [z, y, x] array order.
    inverse = np.linalg.inv(affine)
    index_xyz = (points - affine[:3, 3]) @ inverse[:3, :3].T

    # A margin in mm is a different number of voxels on every axis.
    spacing = np.linalg.norm(affine[:3, :3], axis=0)
    pad_xyz = margin_mm / np.maximum(spacing, 1e-6)

    low_xyz = np.floor(index_xyz.min(axis=0) - pad_xyz).astype(int)
    high_xyz = np.ceil(index_xyz.max(axis=0) + pad_xyz).astype(int)

    shape_xyz = np.array(volume.shape[::-1])
    if np.any(low_xyz >= shape_xyz) or np.any(high_xyz < 0):
        raise DicomError(
            "the plan's landmarks fall outside the CT volume. The plan and the "
            "series have to share one patient coordinate frame"
        )

    low = np.clip(low_xyz, 0, shape_xyz - 1)
    high = np.clip(high_xyz + 1, low + 1, shape_xyz)

    cropped = volume[low[2] : high[2], low[1] : high[1], low[0] : high[0]]
    if cropped.size == 0:
        raise DicomError("the landmark region of interest is empty")

    shifted = affine.copy()
    shifted[:3, 3] = affine[:3, 3] + affine[:3, :3] @ low.astype(float)
    return cropped, shifted


def plan_landmarks(plan_path: str | Path) -> list[list[float]]:
    """The landmark points a surgical plan carries, in patient mm."""
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    landmarks = (plan.get("coordinate_frame") or {}).get("landmarks") or {}
    points = [value for value in landmarks.values() if isinstance(value, (list, tuple))]
    return [[float(c) for c in point] for point in points if len(point) == 3]


def mask_to_mesh(mask: np.ndarray, affine: np.ndarray):
    """Surface the mask with marching cubes, in patient coordinates."""
    measure = _require("skimage.measure")
    trimesh = _require("trimesh")

    verts_zyx, faces, _, _ = measure.marching_cubes(mask.astype(np.uint8), level=0.5)

    # marching_cubes indexes the array as [z, y, x]; the affine is built for
    # (x, y, z) index order, so the columns have to be swapped before transforming.
    verts_xyz = verts_zyx[:, ::-1]
    verts = verts_xyz @ affine[:3, :3].T + affine[:3, 3]

    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
    mesh.fix_normals()
    return mesh


def dicom_to_mesh(
    dicom_dir: str | Path,
    out_path: str | Path,
    bone: str = "femur",
    threshold_hu: float = DEFAULT_THRESHOLD_HU,
    max_faces: int = mesh_quality.MAX_FACES,
    landmarks_mm: list[list[float]] | None = None,
):
    """Full conversion, then the same mesh gate a hand-segmented mesh must pass."""
    volume, affine = load_series(dicom_dir)
    if landmarks_mm:
        volume, affine = crop_to_landmarks(volume, affine, landmarks_mm)
    mask = segment_bone(volume, threshold_hu)
    mesh = mask_to_mesh(mask, affine)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(str(out_path))

    # The gate runs on what was written, so its verdict describes the artefact
    # the next step will actually read.
    result = mesh_quality.gate(out_path, bone=bone, units="mm", max_faces=max_faces)

    # Write the repaired mesh back. The gate drops the medullary canal shell and
    # decimates to the ray-casting budget; leaving the raw surface on disk would
    # mean the file the user is handed is not the one that passed.
    if result.mesh is not None and result.repairs:
        result.mesh.export(str(out_path))

    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autoimplants.dicom_to_mesh", description=__doc__)
    ap.add_argument("--dicom-dir", required=True, help="directory holding one DICOM series")
    ap.add_argument("--bone", default="femur", choices=sorted(mesh_quality.BONE_EXTENT_MM))
    ap.add_argument("--out", required=True, help="STL to write")
    ap.add_argument(
        "--plan",
        default=None,
        help="surgical-plan JSON whose landmarks bound the region of interest. Without "
             "it the whole series is segmented, which on a lower-limb scan gives the "
             "femur and tibia as one structure",
    )
    ap.add_argument("--threshold-hu", type=float, default=DEFAULT_THRESHOLD_HU)
    ap.add_argument("--max-faces", type=int, default=mesh_quality.MAX_FACES)
    args = ap.parse_args(argv)

    try:
        phi = scan_for_phi(args.dicom_dir)
        if phi:
            print(
                "WARNING: this DICOM series still carries direct identifiers: "
                + ", ".join(sorted(phi))
                + "\nDe-identify the source data before it goes any further. Do not "
                  "commit DICOM to this repository.",
                file=sys.stderr,
            )

        result = dicom_to_mesh(
            args.dicom_dir,
            args.out,
            bone=args.bone,
            threshold_hu=args.threshold_hu,
            max_faces=args.max_faces,
            landmarks_mm=plan_landmarks(args.plan) if args.plan else None,
        )
    except DicomError as exc:
        print(f"DICOM conversion failed: {exc}", file=sys.stderr)
        return 1

    print(result.report.summary())
    print(f"\nwrote {args.out}")
    print(
        "\nThis is threshold segmentation, not clinical segmentation. Open the mesh "
        "and check it is the bone you meant before importing a case against it."
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
