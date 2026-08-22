# AutoImplants

Devin as an autonomous biomedical engineer. Given a bone mesh and engineering
constraints, Devin edits a parametric CAD generator, our validators score the
exported solid, Devin reads the structured failures, fixes the generator, and
repeats until the design passes — with a committed engineering rationale at every
step.

Built for the Cognition/Devin track, Munich Agentic Hackathon (TUM.ai), Aug 2026.

## Scope

Stated once, honestly:

- Segmentation, surgical planning and landmark identification are **precomputed
  inputs**, not part of the demo.
- Physics is a **reduced-order analytical surrogate**, not clinical FEA.
- One bone, one plate, one load case. No fatigue, no multi-material, no contact
  mechanics.
- **Not a clinical device.** Synthetic anatomy only. No FDA/ISO/ASTM claim.

## Quick start

```bash
bash setup.sh
```

Then check the baseline design:

```bash
.venv/bin/python -m autoimplants.run --validators geometry,stress
```

On Windows the interpreter is `.venv/Scripts/python.exe`.

Exit code is 0 when the design passes and 1 when it fails, so the loop and Devin
can both branch on `$?`.

Other useful entry points:

```bash
python -m autoimplants.run --validators stub --no-build   # zero dependencies, fake failing report
python inputs/make_bone.py                               # regenerate the anatomy (deterministic)
python -m harness.smoke --dry-run                        # print the Devin prompt, no API key needed
python -m harness.guard <base-sha>                       # did Devin only touch the design surface?
python -m harness.design_space                           # prove no scalar tweak can pass (~3 min)
```

## The loop

```
generate (CadQuery) → export STL/STEP → geometry validator → [pass?]
                                              ↓ fail
                                    surrogate stress validator → [pass?]
                                              ↓ fail
                                       structured Report (JSON)
                                              ↓
                              Devin session: read Report, edit generator.py
                                              ↓
                                git commit with engineering rationale
                                              ↓
                                    (repeat, capped by iteration_budget)
```

## Layout

| Path | What it is |
|---|---|
| `autoimplants/generator.py` | **The file Devin edits.** `build_implant(params) -> cq.Workplane` |
| `autoimplants/contracts.py` | Frozen `Report`/`Check` schema. Do not change unilaterally. |
| `autoimplants/validators/` | `validate(implant_path, case) -> Report`. Geometry runs first, then stress. |
| `autoimplants/bone.py` | Bone surface sampling, shared by generator and validator so they cannot disagree. |
| `inputs/` | Locked: anatomy, surgical plan, keepout zones, thresholds. |
| `harness/` | Devin API client, smoke test, locked-file guard, loop, design-space proof. |
| `runs/` | Committed per-iteration artefacts — the demo record. |

## What the validators measure

Geometry is hard fact about the exported STL: manifoldness, envelope, minimum
wall, mass, bone collision, residual bone gap, screw bores, keepout zones.

Stress is a reduced-order analytical surrogate — beam theory, not FEA — and every
section property it uses is **measured off the same STL by ray casting**, so a
rib or a thickness profile shows up in the stress result because the part
changed, not because a parameter told the model about it:

| Check | Model |
|---|---|
| `stress_max_bending` | Euler-Bernoulli about the plate width axis. The plate bridges the defect, so the moment peaks at mid-footprint and falls to zero at the end screws; the plate carries 57% of the 15 Nm gait moment and 35% of the 2100 N stance load. |
| `stress_hole_N` | Net-section stress at each screw, raised by a plate-bending Kt: 1.40 for a round hole, 1.10 for an axial slot. Whether a hole is a slot is *measured* from the void's aspect ratio, not declared. |
| `screw_pullout_min` | Thread shear over the engaged cortex, and standoff eats engagement — every millimetre of gap is a millimetre of screw not in bone, which is why conformance and fixation fail together. |

Baseline flat plate: 414 MPa peak bending against a 350 MPa allowable, 580 MPa at
the two inner holes, 910 N of pull-out against 1200 N.

## Why this is not just an optimiser

This is the question that decides the prize, so it is built into the case file
rather than argued on stage. The constraints in `inputs/case.json` are chosen so
that **no combination of scalar parameter tweaks can pass**:

- The baseline plate is 37.0 g against a 39 g budget, so uniform thickening buys
  at most ~5% more volume — a 1.11× bending-stress reduction, not enough.
- Widening past ~17 mm hits the `perforating_vessel_bundle` keepout.
- Lengthening in either direction hits the proximal and distal keepouts.

What remains is contouring the plate to the bone and adding local reinforcement:
ribs, a variable thickness profile, hole-to-slot conversion. Those require
**writing geometry code**, not setting a float.

The generator enforces this directly: `build_implant()` raises
`NotImplementedError` if a topology parameter is set without the geometry behind
it. Setting `ribs=[...]` and hoping is not a valid move.

This is checked rather than asserted. `harness/design_space.py` sweeps thickness
× width × length across and past every scalar limit, builds all 84 parts, runs
the real validators, and exits non-zero if any scalar-only design passes:

```bash
python -m harness.design_space
# Property holds: all 84 scalar-only designs fail at least one check.
```

Run it after touching `stress.py`, `case.json` or `make_bone.py`. See
`design_space_note` in `inputs/case.json` before recalibrating anything.

## Guardrail

`harness/guard.py` is an allowlist, not a denylist: Devin may edit
`generator.py`, `params.py` and `export.py`, and nothing else. A commit touching
the validators, the thresholds, the anatomy or the bone sampler makes the
iteration invalid. Anything nobody thought about defaults to locked.

That turns "how do you know it didn't game the metric?" into a command:

```bash
python -m harness.guard <sha-before-devin>
```

## Anatomy

`inputs/bone.stl` is procedurally generated by `inputs/make_bone.py` —
deterministic, regenerable, no licence question, and byte-identical across runs.
Morphometry follows published adult femoral literature (26 mm mid-diaphyseal
diameter, 22 mm anterior bow ⇒ 0.91 m radius of curvature). Screw positions are
sampled *on the generated surface* rather than hand-typed, so the validator is
never checking the implant against numbers that do not touch the bone.

That anterior bow is what makes the generic flat plate fail on this patient:
6.17 mm of measured mid-span standoff against a 1.5 mm limit.
