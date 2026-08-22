# Real CT Support Implementation Plan

## Status: implemented

All ten subtasks shipped. See `docs/real-ct-cases.md` for the resulting usage
documentation. Four things were done differently from the plan below, each for a
reason found during implementation:

1. **Output goes to `real_cases/<case_id>/generated/`, not `inputs/`.**
   `harness/guard.py` locks `inputs/*` as system-controlled, so a case imported
   there would read as a guard violation on the agent's own diff.
2. **The coordinate frame is transformed, not merely validated.** Subtask 4 said
   reject a case whose frame is wrong. Every real segmentation arrives in scanner
   coordinates, so rejection would have rejected everything; the importer now
   computes the rigid transform from the plan's landmarks and applies it to mesh,
   screws and keepouts together, recording the matrix in `frame_transform.json`.
3. **DICOM dependencies were split out, then folded back in on request.** They
   add ~255 MB to every container rebuild and the design loop never imports them,
   so they started in a separate `requirements-dicom.txt`; the team chose one
   install step over a smaller image, so they now live in `requirements.txt` and
   `dicom_to_mesh` imports them lazily instead. `networkx` was added there too —
   without it trimesh's component split and hole filling are silent no-ops, which
   are exactly the two repairs the mesh gate depends on.
4. **A face budget and batched ray casting were added.** Not in the plan. A
   marching-cubes femur carries 10^5-10^6 triangles, and the per-sample ray loop
   the gap check used would have dominated every iteration.

Two further notes: the mesh gate ranks components by surface area rather than
face count (a 1.5 mm speck can carry more triangles than a coarsely tessellated
shaft), and the plan's `.venv/bin/python` invocation is `.venv/Scripts/python.exe`
on Windows.

Generalising the gap check to sample across the plate width moved the synthetic
baseline from 6.566 mm at the y=0 centreline to 8.596 mm at the y=6 edge lane. The
same check still fails, so the demo thesis is unchanged; the documented figures
were re-baselined.

## Summary

Implement real-case support in three phases: first accept a real CT-derived segmented
bone mesh plus surgical planning JSON, then add raw DICOM-to-mesh tooling, then validate
against a public dataset.

Chosen defaults:

- V1 input is **segmented mesh + external surgical JSON**.
- Raw DICOM support is **Phase 2**, because DICOM is scanner-slice data that must be
  segmented before this repo can design against it.
- Public dataset validation should target a dataset that includes both CT imaging and
  segmentations/landmarks where possible, such as HFValid or Visible Human lower
  extremity data.

## Key Changes

- Add a real-case importer CLI:

  ```bash
  python -m autoimplants.import_case \
    --case real_cases/<case_id>/surgical_plan.json \
    --bone real_cases/<case_id>/bone.stl \
    --out inputs
  ```

- The importer validates the real mesh and surgical plan, then writes the current repo
  format:
  - `inputs/bone.stl`
  - `inputs/case.json`
  - `inputs/screw_positions.json`
  - `inputs/keepout_zones.json`
- Add a surgical-plan schema for externally supplied planning data:
  - case id
  - target bone
  - side
  - approach
  - coordinate frame landmarks
  - plate footprint
  - screws
  - keepouts
  - material
  - thresholds
  - optional load notes
- Do not auto-invent screw positions or clinical decisions in V1.
- Add mesh quality checks before generation:
  - readable mesh
  - non-empty mesh
  - watertight or repairable mesh
  - single major component
  - plausible units in mm
  - valid shaft axis
  - valid lateral direction
  - footprint within mesh bounds
- Generalize current geometry assumptions:
  - screw trajectory checks must support arbitrary 3D screw directions, not only `-X`.
  - bone gap checks must sample across the plate width, not only `y=0`.
  - bone loading/sampling utilities must accept a configured bone path instead of relying
    only on `inputs/bone.stl`.
- Add Phase 2 raw DICOM tooling:

  ```bash
  python -m autoimplants.dicom_to_mesh \
    --dicom-dir <dir> \
    --bone femur \
    --out real_cases/<case_id>/bone.stl
  ```

- Use `pydicom`, `SimpleITK`, and `scikit-image` for DICOM loading, HU volume handling,
  threshold segmentation, connected-component cleanup, marching cubes, and STL export.
- Keep DICOM conversion separate from the implant loop; the implant loop still consumes
  a mesh plus surgical JSON.

## Subtasks

### 1. Plan Artifact

- Create `docs/planning/real-ct-support-plan.md` with this plan.
- Commit it with message: `docs: plan real CT case ingestion`.

### 2. Case Schema

- Add a JSON schema or Python validator for
  `real_cases/<case_id>/surgical_plan.json`.
- Required fields:
  - `case_id`
  - `bone`
  - `side`
  - `approach`
  - `coordinate_frame`
  - `footprint_z_mm`
  - `screws`
  - `keepouts`
  - `material`
  - `thresholds`
- Fail loudly if required clinical planning data is missing.

### 3. Mesh Importer

- Add `autoimplants/import_case.py`.
- Load STL/PLY/OBJ via `trimesh`.
- Normalize or copy the mesh to `inputs/bone.stl`.
- Generate the existing locked input files from the surgical plan.
- Preserve the current synthetic demo path; importing a real case is opt-in.

### 4. Coordinate Frame Validation

- Require landmarks or explicit axes in the surgical JSON.
- Validate that `+Z` is shaft direction and `+X` is the selected plate approach side.
- Reject cases where the coordinate frame is missing or ambiguous.

### 5. Planning Data Validation

- Confirm every screw entry lies near the bone surface.
- Confirm every screw direction is normalized and intersects the bone volume.
- Confirm keepout zones are valid shapes and use mm units.
- Confirm the plate footprint is inside the available shaft region.

### 6. Validator Generalization

- Update screw checks to cast rays along each screw's own direction.
- Update bone conformance checks to sample multiple `y` positions across the plate width.
- Keep current synthetic case passing/failing behavior unchanged unless the existing
  geometry truly violates the generalized checks.

### 7. Configurable Bone Path

- Keep default behavior pointing at `inputs/bone.stl`.
- Allow validators and bone utilities to receive the bone path from `case.json`.
- Avoid global hard-coding where real case import needs per-case files.

### 8. DICOM Phase

- Add DICOM dependencies to `requirements.txt`.
- Implement DICOM series loading and HU volume reconstruction.
- Segment cortical bone with a conservative threshold default and connected-component
  selection.
- Export a mesh, then run the same mesh quality gate as Phase 1.
- Document that DICOM segmentation is research/demo tooling, not clinical segmentation.

### 9. Dataset Validation

- Add documentation for one reproducible public-data path.
- Prefer HFValid for whole-femur CTs with segmentations/landmarks, or Visible Human lower
  extremity CT plus STL assets if easier to obtain.
- Do not commit large DICOM files to the repo.

### 10. Docs

- Update README scope from "synthetic anatomy only" to "synthetic demo by default; real
  CT-derived mesh supported via importer."
- Add example `real_cases/example/surgical_plan.json`.
- Document the exact command sequence from real mesh to validated implant.

## Test Plan

- Unit tests:
  - valid surgical JSON imports successfully.
  - missing landmarks, missing screws, malformed keepouts, and non-normalized screw
    vectors fail with clear messages.
  - screw checks work for non-`-X` directions.
- Integration tests:
  - current synthetic case still runs with:

    ```bash
    .venv/bin/python -m autoimplants.run --validators geometry,stress
    ```

  - importer can regenerate `inputs/` from an example real-style mesh and plan.
  - mesh quality gate rejects empty, disconnected, wrong-unit, and non-watertight meshes.
- DICOM tests:
  - use a tiny fixture or mocked DICOM volume for CI.
  - full public DICOM dataset test is manual/local because datasets are too large for the
    repo.
- Acceptance criteria:
  - Given a real segmented femur mesh and complete surgical JSON, the repo generates the
    same input-file shape it uses today and can run the existing implant validation loop.
  - Given raw DICOM, Phase 2 can produce a candidate mesh, but the user must still review
    segmentation quality before implant generation.

## Assumptions

- Surgical planning remains external in V1; the repo validates and consumes the plan but
  does not decide screw placement clinically.
- Real CT data is not committed to git.
- All geometry units are millimeters.
- The current synthetic demo remains the default quick-start path.
- Stress/FEA remains skipped unless a separate feature implements a real stress validator.
