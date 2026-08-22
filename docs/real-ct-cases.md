# Running a real CT-derived case

The synthetic femur in `inputs/` is the default quick-start path and is unchanged.
This document covers the other path: designing against a bone segmented from real
CT imaging.

The split is deliberate. This repo designs an implant *around a surgical plan*; it
does not decide where screws go in a patient. So a real case arrives as two
things — a segmented bone mesh and a surgical plan — and the importer's job is to
refuse a plan it cannot design against rather than invent the missing half.

## The two phases

| Phase | Input | Command |
|---|---|---|
| 1 | Segmented mesh + surgical plan | `autoimplants.import_case` |
| 2 | Raw DICOM series | `autoimplants.dicom_to_mesh`, then phase 1 |

Phase 2 is optional research tooling. Threshold segmentation produces a plausible
bone, not a correct one — a human has to look at it before it becomes a case.

## Phase 1: mesh plus plan

```bash
python -m autoimplants.import_case \
    --case real_cases/example/surgical_plan.json \
    --bone real_cases/example/bone.stl
```

That writes a self-contained case:

```
real_cases/<case_id>/generated/
    bone.stl                 the mesh, rigidly transformed into the repo frame
    case.json                thresholds, envelope, material, input paths
    screw_positions.json     locked planning input, in the repo frame
    keepout_zones.json       locked planning input, in the repo frame
    frame_transform.json     the 4x4 that was applied, for traceability
    import_report.json       every check the import ran
```

Run it exactly like the synthetic case:

```bash
python -m autoimplants.run \
    --case real_cases/<case_id>/generated/case.json \
    --validators geometry,stress
```

### Why not `inputs/`

`harness/guard.py` locks `inputs/*` as system-controlled — case constraints and
anatomy are not the agent's to edit. A case imported into that directory would
show up as a guard violation on the agent's own diff, so imports land in
`real_cases/` instead and the case file is passed with `--case`.

### The coordinate frame

Everything downstream of the importer assumes:

- **+Z** along the shaft, proximal to distal, origin at the proximal landmark
- **+X** the aspect the plate mounts on
- **+Y** the plate width direction

Real segmentations arrive in scanner coordinates: obliquely angled, origin at the
isocentre. Rejecting those would make real-CT support useless, since no scanner
produces an axis-aligned bone. So the importer *computes* the transform from the
landmarks in the plan and applies it to the mesh, the screws and the keepouts
together. Relative geometry is untouched — it is a change of description, not of
anatomy — and the matrix is written to `frame_transform.json` so the mapping back
to the source scan is never lost.

## The surgical plan

`real_cases/example/surgical_plan.json` is the worked example. Required fields:

| Field | Meaning |
|---|---|
| `case_id` | Identifier; names the output directory |
| `bone` | `femur`, `tibia`, … — sets the plausible-size bounds for the mesh gate |
| `side` | `left` / `right` |
| `approach` | Which aspect the plate mounts on, e.g. `lateral` |
| `coordinate_frame` | `landmarks` (three points) or explicit `axes` |
| `footprint_z_mm` | `[z_start, z_end]` of the plate, in the repo frame |
| `screws` | `id`, `entry_mm`, `direction`, `diameter_mm`, `length_mm` |
| `keepouts` | Spheres: `id`, `center_mm`, `radius_mm`, `rationale` |
| `material` | At minimum `name` and `density_g_cm3` |
| `thresholds` | The limits the validators enforce |

Landmarks, when used, are three points in the same frame as the mesh:

- `proximal_shaft_mm` — becomes the origin, and the `-Z` end of the shaft axis
- `distal_shaft_mm` — the `+Z` end
- `mount_side_mm` — any point on the aspect the plate mounts on, well clear of
  the axis

Screw `direction` need not be normalised; only the zero vector is an error.
Directions are not required to be axis-aligned — the geometry validator casts
each bore check along the screw's own trajectory.

A missing field is an error naming the field, never a default. That is the whole
point: an incomplete plan that imports anyway produces an implant fitted to
numbers nobody supplied.

## What the import checks

**The mesh** (`autoimplants/mesh_quality.py`) — none of these can be produced by
`inputs/make_bone.py`, which is exactly why the gate exists:

- non-empty and readable
- plausible size for the named bone, measured across the convex hull so an
  obliquely posed mesh is not mistaken for a short one
- single component; speckle islands are dropped, two comparably sized components
  fail for human review
- watertight, repairing holes where it can — containment queries are undefined on
  an open surface
- within the ray-casting face budget, decimating if `fast-simplification` is
  installed

**The plan against the bone** (`autoimplants/surgical_plan.py`):

- every screw entry lies on the bone surface (2.5 mm tolerance)
- every trajectory passes through bone, with the thinnest purchase reported
- the plate footprint lies on the shaft that exists in this mesh
- every screw falls inside that footprint

Those four catch a structurally perfect plan that refers to a *different scan* —
before the design loop spends its whole iteration budget on geometry that was
never seated on the bone.

## Phase 2: raw DICOM

```bash
python -m autoimplants.dicom_to_mesh \
    --dicom-dir <one-series-directory> \
    --bone femur \
    --out real_cases/<case_id>/bone.stl
```

The DICOM stack (`pydicom`, `SimpleITK`, `scikit-image`, `scipy`) ships in
`requirements.txt`, so `bash setup.sh` is the only install step. It is by far the
heaviest thing in there — about 255 MB — and nothing in the design loop imports
it, so `dicom_to_mesh` imports it lazily: a broken DICOM install can never stop
`autoimplants.run` from working.

What the tool does: reads one series with its real spacing and orientation,
converts to Hounsfield units, thresholds at a conservative 300 HU cortical value,
keeps the largest connected component, closes pinholes, and surfaces it with
marching cubes **in patient coordinates**. It then runs the same mesh gate a
hand-segmented mesh must pass.

It refuses a directory holding more than one series rather than picking one —
that is how you segment the scout scan by accident.

### Patient data

- **De-identify before the data reaches this machine.** The tool reports which
  direct identifiers are still populated (`PatientName`, `PatientID`,
  `AccessionNumber`, …) and does not strip them: de-identification is the data
  owner's decision and their audit trail.
- **Never commit DICOM to this repository** — for privacy first, size second.
- Threshold segmentation is research/demo tooling. It is not clinical
  segmentation and carries no clinical, FDA, ISO or ASTM claim.

## Placing the landmarks

After a CT conversion the mesh is in patient coordinates -- origin at the scanner
isocentre, axes unrelated to the bone -- so the three landmarks cannot be guessed
and reading them off a viewer one at a time is slow and error-prone.

```bash
python -m autoimplants.landmarks     --bone real_cases/<case_id>/bone.stl     --mount-side +x     --case-id <case_id> --out real_cases/<case_id>/surgical_plan.json
```

It fits the diaphyseal axis from cross-section centroids (not a vertex-cloud PCA,
which the condyles drag off-axis) and places the two shaft landmarks at 15% and
85% of the bone's length.

Two things it cannot decide, and reports rather than hides:

- **Which end is proximal.** Guessed from which end is bulkier. Right for a
  femur, wrong for other bones and for partial scans -- pass `--flip`.
- **Which way the plate mounts.** Pure anatomy. You supply it with
  `--mount-side`.

The plan it writes is **deliberately incomplete**: no screws, no keepouts, and
placeholder material and thresholds. `import_case` rejects it until a human fills
those in from real surgical planning. A tool that scaffolded a plausible-looking
plan would be the easiest way to smuggle fabricated clinical data into a case.

## Testing the pipeline without patient data

```bash
python real_cases/synthetic_ct/make_ct.py
```

Writes a femur-shaped CT phantom -- a real DICOM series, ~27 MB, not committed
and regenerated in seconds. It is rendered *from* `inputs/bone.stl` under a stated
pose, so the correct reconstruction is known exactly and can be scored instead of
eyeballed.

It is built to have the properties that break real scans, one per failure it
catches:

| property | what it catches |
|---|---|
| cortical shell around a marrow canal | thresholding must find the cortex, not the whole limb |
| surrounding soft tissue | a 300 HU cut has to actually separate them |
| acquisition noise | morphological cleanup has to survive speckle |
| oblique direction cosines | the index-to-patient affine, applied the right way round |
| anisotropic voxels (0.8 x 0.8 x 1.25 mm) | a transposed spacing vector distorts the anatomy |

Measured end to end on that phantom:

- surface deviation from ground truth: **0.128 mm mean, 0.30 mm p95** against
  0.8 mm voxels
- screw entries land **0.2 mm** from the reconstructed surface (2.5 mm tolerance)
- the resulting design's bone gap is **8.46 mm**, against 8.60 mm on the exact
  mesh -- the difference is the reconstruction error, as it should be

One thing the phantom exposed that the unit tests could not: a thresholded cortex
is a *tube*, and marching cubes surfaces a tube as two shells -- the outer bone
and the wall of the medullary canal. Two components of comparable size reads as
"two bones in the field of view" to a naive count. The gate now detects nested
shells and keeps the outer surface.

## Validating against a public dataset

Reproducible, no patient data, not committed here:

1. Prefer a dataset carrying **both imaging and segmentations/landmarks**, so the
   plan can be built from published landmarks rather than guessed — whole-femur CT
   collections with segmentation labels are the best fit.
2. The Visible Human lower-extremity CT plus its derived STL assets is the easier
   fallback when a segmented dataset is hard to obtain.
3. Download outside the repo, convert with `dicom_to_mesh`, scaffold the frame
   with `autoimplants.landmarks`, fill in the planning data from the published
   landmarks, then import.

This step is manual and local by design: the datasets are far too large to commit
and their licences differ.

## Tests

```bash
python -m pytest tests -q
```

The import path is round-tripped against a known answer. `real_cases/example/` is
the synthetic femur rigidly transformed into an arbitrary scanner-like pose, so
the importer has to recover a frame whose correct result is exactly `inputs/` —
any error in the frame recovery shows up as a millimetre-scale disagreement with
the ground truth. Regenerate it with:

```bash
python real_cases/example/make_example.py
```

DICOM tests skip cleanly when the DICOM stack is absent, so the suite still runs
on a machine that only installed the design-loop dependencies.
