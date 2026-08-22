# Industry Workflow → Our System: 1:1 Mapping and Build Plan

The documented patient-specific implant workflow, step by step, with what replaces each step and how to implement it.

## The mapping at a glance

| # | Industry step | Tool used today | Our replacement | Who does it |
|---|---|---|---|---|
| 1 | Image acquisition | CT scanner, DICOM | Dataset input | given |
| 2 | Segmentation | Mimics | Threshold + marching cubes, or TotalSegmentator | script |
| 3 | Mesh refinement for FEA | 3-matic | trimesh smooth / decimate / repair | script |
| 4 | Anatomical alignment | manual landmarking | PCA + landmark detection | script |
| 5 | Implant design | 3-matic, Rhino, SolidWorks by hand | CadQuery script written by Devin | **Devin** |
| 6 | FEA | Ansys, Abaqus | gmsh + CalculiX harness | frozen service |
| 7 | Design iteration | engineer returns to CAD | Devin edits the script and re-submits | **Devin** |
| 8 | Manufacture | SLM / laser powder bed fusion | STL + sliced G-code | script |
| 9 | Documentation | manual report | auto-generated verification report | script |

Steps 5 and 7 are the human bottleneck. They are the only steps Devin owns. Everything else is deterministic plumbing we write ourselves.

---

## Step 1 — Image acquisition

**Industry:** CT or CBCT of the affected region, exported as DICOM.

**Ours:** Two input paths into the same interface.

**Implementation**
- Primary batch source: VSDFullBodyBoneModels, 30 subjects, lower extremity bone meshes as MATLAB MAT files. Clone recursively from GitHub, or pull the Zenodo release.
- Live demo source: TotalSegmentator dataset, the 102-subject quick-download subset from Zenodo, not the full 23.6 GB set.
- Load MAT with `scipy.io.loadmat`. If the file is v7.3 it is HDF5, so fall back to `h5py`. **Check this in the first ten minutes.**

**Done when:** `load_case(subject_id) -> raw mesh or DICOM directory` works for both sources.

**Owner:** data person. **Budget:** 30 min.

---

## Step 2 — Segmentation

**Industry:** Mimics. An operator thresholds, refines by hand, separates bones at joint spaces, closes holes. Hours per case.

**Ours:** Scripted, no operator.

**Implementation**
- Read DICOM series with `pydicom` or `SimpleITK`.
- Threshold at 200 HU lower bound, volume max as upper bound (the value the dataset authors themselves used).
- `skimage.measure.marching_cubes` to get a surface.
- Crop the region of interest **below the joint line** before anything else. Thresholding fuses tibia to femur across the thin joint gap; cropping sidesteps the manual joint separation entirely.
- Optional: TotalSegmentator instead, if we want the "a neural net finds the bone" line in the pitch. Physics does not care which we use.

**Done when:** `dicom_dir -> raw surface mesh`, running headless.

**Owner:** data person. **Budget:** 1 h.

---

## Step 3 — Mesh refinement for FEA

**Industry:** 3-matic. Inspect for geometric errors, smooth, close holes, optimize the triangle mesh so it can be used in an FE package.

**Ours:** trimesh, plus a hard quality gate.

**Implementation**
- Taubin smoothing (preserves volume, unlike Laplacian).
- Decimate to roughly 20k faces.
- `trimesh.repair.fill_holes`, then assert `mesh.is_watertight`.
- **Quality gate, all must pass:** watertight, single connected component, volume within a plausible physiological range, principal-axis length plausible. Failing any of these rejects the case before Devin ever sees it.

That gate is what closes the hole in the autonomy claim: a bad mask otherwise produces a well-validated plate for the wrong surface, and every downstream check passes.

**Validation trick:** run our own pipeline on a subject whose expert mesh we already have, then compare surfaces with `trimesh.proximity`. Gives us a measured accuracy number against a published 0.4 mm benchmark. This is our single strongest credibility slide.

**Done when:** `raw mesh -> clean mesh + quality report`.

**Owner:** data person. **Budget:** 1 h.

---

## Step 4 — Anatomical alignment

**Industry:** Engineer identifies landmarks and axes manually, or semi-automatically.

**Ours:** PCA plus landmarks.

**Implementation**
- PCA over shaft vertices; first principal component is the long axis.
- Two landmark points to fix rotation about that axis.
- Build a transform into a canonical frame. Every downstream position ("40 mm distal, lateral surface") is expressed in this frame.
- Ground truth available: the dataset ships manually selected landmarks from five experienced raters. Check our automatic detection against them.

**Why it matters:** without a shared frame, 30 patients produce 30 plates in 30 arbitrary orientations and we cannot tell whether anything generalized.

**Done when:** `clean mesh -> (mesh in canonical frame, transform)`.

**Owner:** data person. **Budget:** 1 h.

---

## Step 5 — Implant design ← DEVIN OWNS THIS

**Industry:** An engineer draws the plate in 3-matic, Rhino or SolidWorks, offsetting from the bone surface and setting thickness, accounting for fracture location and shape, bone structure, and muscle and nerve tissue. Days. The literature calls this step tedious and complicated.

**Ours:** Devin writes and edits a CadQuery script.

**Implementation — the archetype generator (we write v1, Devin edits it)**

```python
def generate_plate(bone_mesh, params):
    pts     = sample_surface(bone_mesh, along=axis, region="lateral", n=20)
    normals = surface_normals(bone_mesh, pts)
    path    = spline(pts + normals * (params.thickness/2 + params.clearance))
    section = cross_section(params)          # Devin may change its shape
    solid   = sweep(section, path)
    solid   = drill(solid, n=params.n_holes, spacing=params.hole_spacing,
                    dia=params.hole_dia, direction=normals)
    return fillet(solid, params.fillet_radius)
```

**Seed parameters from published FEA dimensions**

| Parameter | Starting value | Range |
|---|---|---|
| length | 140 mm | 80–190 |
| width | 12 mm | 10–14 |
| thickness | 3.5 mm | 3.0–5.0 |
| n_holes | 8 | 6–12 |
| hole_spacing | 13 mm | 10–16 |
| hole_dia | 3.5 mm locking / 4.5 mm compression | fixed |
| clearance | 0.2 mm | 0.1–1.0 |

**Hard rule: never boolean the plate against the bone mesh.** OCCT fails on a 20k-triangle solid with `BRep_API: command not done` and no diagnostic. Clearance is checked numerically in trimesh, never enforced geometrically in CAD.

**Tagging:** when the generator creates a face, record which parameter produced it. A stress hotspot then resolves by nearest-face lookup to `fillet_radius_hole_3` instead of a bare coordinate. This is what turns an FEA result into an actionable edit.

**Critical design requirement:** the script must be written so that *structural* changes are possible — add a rib, split the head into two screw columns, taper the section, reroute the path. If the generator is a fixed function of twelve floats, Devin has nothing to do that `scipy.optimize` could not do better, and we have no answer to the hardest question we will be asked.

**Done when:** `generate(bone_mesh, params) -> STEP file` succeeds on every patient, and gmsh can mesh every result.

**Owner:** geometry person. **Budget:** 4 h.

---

## Step 6 — FEA ← FROZEN SERVICE

**Industry:** Ansys or Abaqus. Engineer sets up loads, boundary conditions, materials, meshes, solves, reads the stress plot.

**Ours:** gmsh + CalculiX behind an HTTP endpoint Devin cannot write to.

**Implementation**

```
run_fea(step_path, load_case) -> {
    peak_von_mises, safety_factor, stiffness, mass,
    hotspot_xyz, hotspot_tag, mesh_convergence
}
```

- gmsh meshes to second-order tets (C3D10), ~1 mm, refined near holes. Linear tets are too stiff in bending and underpredict stress exactly at curved features — unusable here.
- meshio converts to Abaqus `.inp`.
- Material Ti-6Al-4V: E = 113800 MPa, nu = 0.342, yield 880 MPa. Units N / mm / MPa throughout, no exceptions.
- Loads: 400 N axial through the proximal end, distal end fixed, matching the standard published setup. Scale by body weight × gait factor for the patient-specific case.
- Solve with ccx, parse the `.frd` stress block, compute von Mises in numpy from the six components.

**Boundary-condition handling — from our own test result.** Our cantilever benchmark showed wall stress diverging (275.6 → 294.9 → 329.0 MPa) while the global peak converged cleanly to 244.4 against a 240 MPa analytical value. That divergence is a stress singularity at the rigid clamp: it never converges, no matter how fine the mesh. In the plate, the equivalent artefact will appear at the fixed screw nodes and will send Devin chasing a hotspot that does not exist.

Mitigation, all three:
1. Exclude elements within 1–2 element lengths of any constrained node when finding the hotspot.
2. Soften the BC — elastic foundation springs or a kinematic coupling, not a rigid encastre.
3. Report the region-of-interest peak, not the global peak.

**Validation, already done and kept as a regression test:** cantilever beam, 100 × 10 × 5 mm, 100 N tip load. Analytical σ = 240 MPa, δ = 2.812 mm. Our harness: 244.4 MPa (1.8%) and 2.7935 mm (0.7%). Units, BCs, parser and von Mises formula all confirmed correct.

**Speed target:** under 30 s per solve. If it is minutes, crop harder and decimate more. Iteration count is the whole demo.

**Owner:** solver person. **Budget:** done for the harness, 1 h to wrap as a service.

---

## Step 7 — Design iteration ← DEVIN OWNS THIS

**Industry:** The engineer reads the stress plot, goes back to CAD, thickens or reshapes, re-runs. This round trip is the actual bottleneck. Published automated workflows exist but are explicitly *semi*-automated — an engineer is present at every turn. That word is our gap.

**Ours:** Devin reads structured feedback and edits the script. Nobody present.

**Implementation — the verdict engine**

Every check returns `{passed, measured, limit, where}`. Never a bare boolean.

| Check | Rule | Purpose |
|---|---|---|
| Safety factor | 880 / peak_vm ≥ 2.5 | plate must not break |
| **Stiffness window** | inside band, not just below a ceiling | too stiff causes stress shielding: bone resorption and refracture, documented in custom canine plates at 7 months |
| Mass budget | ≤ 40 g | anti-gaming |
| Max thickness | ≤ 6.0 mm | soft-tissue impingement proxy |
| Bone intersection | no negative signed distance | must not sink into bone |
| Clearance | 0.1–1.0 mm | must not float off the bone either |
| Screw trajectory | ray cast through cortex, adequate purchase, no wrong-side exit | screws must hold |
| Printability | overhang angle ≤ 45°, min feature size | must survive DMLS |

The stiffness ceiling is the single most important entry. Without it, the fastest path to safety factor 2.5 is a solid brick, and Devin will find that path.

**Session instructions for Devin**
- Call the verifier over HTTP. It is read-only. You cannot modify loads, materials, boundary conditions, or thresholds.
- On failure: render the deformed model with the stress field, look at it, decide whether this is a shape problem or a number problem. Shape → edit the generator. Number → run the local optimizer.
- Log every iteration: what changed, why, all returned metrics.
- **Never ask a question.** If you cannot converge within the iteration cap, emit the best candidate plus a failure report.

**The local optimizer (runs on Devin's own machine).** CMA-ES or scipy over continuous parameters only — thickness taper, fillet radii, width profile. 100–200 evaluations, converging in under two minutes. This is what drops Devin turns from forty to five and answers the cost objection. Devin decides topology; the optimizer decides coefficients.

**Local vs authoritative:** Devin may render, prototype and pre-check freely on its own box. Only the remote service's verdict counts. Preserves both fast iteration and tamper-resistance.

**Done when:** trigger to converged design, hands off, on one patient.

**Owner:** orchestration person. **Budget:** 3 h.

---

## Step 8 — Manufacture

**Industry:** SLM / laser powder bed fusion in Ti-6Al-4V, then support removal, heat treatment, machining, finishing, sterilization.

**Ours:** STL and G-code. We stop at the file; we do not claim to compress the physical manufacturing chain.

**Implementation**
- STEP → STL export.
- PrusaSlicer or CuraEngine headless CLI → G-code.
- Sanity check: open the G-code in a slicer viewer and confirm it is not garbage.
- If any printer is available, print one in PLA overnight. A physical plate in hand during the pitch beats every slide.

**Owner:** artifacts person. **Budget:** 1 h.

---

## Step 9 — Documentation

**Industry:** Engineer writes the verification report by hand.

**Ours:** Generated from the run.

**Contents**
- Stress figure with the hotspot marked
- Peak von Mises, safety factor, stiffness, mass, clearance, screw checks
- Mesh convergence table proving the numbers are not a meshing artefact
- Simulated ASTM F382 four-point bend result plus the physiological gait case
- Full iteration history

**Do not** call it an ISO 13485 compliance report. An auto-generated compliance document is the part that reads as theatre. A simulated standardized test result is a real artefact an engineer can act on.

**Test:** hand it to someone outside the team. They should be able to say what the plate is and whether it passed.

**Owner:** artifacts person. **Budget:** 2 h.

---

## Honest scope limits, state these before the jury does

- **FEA is the fast loop, not the final word.** Industry validates FE predictions against physical bench testing with digital image correlation. We automate the loop that actually iterates; we do not replace physical validation.
- **Bone material properties.** Real workflows map Hounsfield units to density to elasticity per voxel. We use a single constant. We know the difference and we say so.
- **Soft tissue.** Real design accounts for muscle and nerve tissue. Our max-thickness cap is a crude proxy and we name it as such.
- **Contralateral mirroring** is the standard trick for defect cases — mirror the healthy opposite limb. Not implemented; cheap to add.
- **Regulatory split.** Veterinary use can run fully autonomous today. Human use ends with a qualified person signing the design dossier — the same way someone merges a PR. Nobody touches the design loop in either case.

## Parallel workstreams

| Person | Steps |
|---|---|
| A | 6 — solver harness and verdict engine |
| B | 5 — generator and local optimizer |
| C | 1, 2, 3, 4, 8, 9 — data pipeline and artifacts |
| D | 7 — orchestration, plus pitch |

**Freeze in the first hour:** the exact JSON the verifier returns, and the exact generator interface. A and B then build against a stub instead of against each other.

**Build order rule:** verifier before generator. If the verifier works and the CAD is crude, we still have a loop to demo. Reverse it and we have pretty geometry that nothing judges.