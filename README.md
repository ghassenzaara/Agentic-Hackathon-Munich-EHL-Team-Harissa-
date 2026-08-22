# ehl-munich-august-2026

Cognition "Devin for X" track — an autonomous layer for patient-specific
orthopedic implants (tibial fixation plates).

![X-rays of a fractured tibia repaired with metal fixation plates and screws, front and side views](artifacts/tibial_fixation_plate.png)

*A broken shin bone, front (a) and side (b). The metal screwed along the bone to hold the fragments in place while
they heal is the **fixation plate**. 

Today an engineer shapes each one by hand from a CT
scan, over weeks. That is the bottleneck.*

## Repository structure

```
.
├── README.md                          this file
├── LICENSE
│
├── devin-challenge-brief.md           the pitch: domain, verdict engine, autonomy story, risks
├── devin-challenge-alignment-note.md  gap analysis of the brief vs. the track requirements
├── resources.md                       datasets + software the build needs, and their status
├── devin-api-setup.md                 Devin v3 API contract, credential handling (key pending)
│
├── .env.example                       config template — copy to .env, never commit .env
├── .gitignore
│
├── pixi.toml                          the environment: toolchain + ccx solver + tasks
├── pixi.lock                          cross-platform lock (osx-arm64 + linux-64) — commit this
├── src/                               resources, fetch scripts, SSM loader (see src/README.md)
│
├── artifacts/
│   ├── image.png                      the track brief slide
│   └── tibial_fixation_plate.png      post-op X-rays, used in this README
│
├── skills-lock.json                   pinned Entire skill versions
├── .agents/skills/                    Entire agent skills (canonical copies, 12 skills)
├── .claude/
│   ├── settings.json
│   └── skills/                        symlinks into ../../.agents/skills/
├── .codex/
│   └── hooks.json                     Codex session hooks
├── agent/skills/                      Entire agent skills (generic-agent copy, 12 skills)
└── .entire/                           Entire session tracking (logs + metadata, gitignored)
    ├── .gitignore
    └── settings.json
```

## Getting set up

```bash
pixi install          # toolchain + CalculiX solver, from the lockfile
pixi run setup        # verify it, fetch the anatomy data, sample 5 unseen tibias
```

`pixi.lock` pins both `osx-arm64` (laptops) and `linux-64` (the Devin machine
snapshot, `DEVIN_SNAPSHOT_ID` in [.env.example](.env.example)), so the agent
solves against the same toolchain we develop against. Details and the one
deliberate version bump are in [pixi.toml](pixi.toml); resource status is in
[src/README.md](src/README.md).

The anatomy data is not committed. `pixi run setup` downloads it, or
`pixi run fetch-ssm` on its own (~550 MB, md5-checked, safe to re-run) into
gitignored `src/data/`; `pixi run fetch-ts` adds the optional CT mask set. Which
sources we picked, which ones failed to deliver, and why, is in
[resources.md](resources.md).

The design loop itself is not built yet, and the Devin API key is still to be
provided; see [devin-api-setup.md](devin-api-setup.md).
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
```

## The loop

```
generate (CadQuery) → export STEP (deliverable) + STL (measured) → geometry validator → [pass?]
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
| `harness/` | Devin API client, smoke test, locked-file guard, loop. |
| `runs/` | Committed per-iteration artefacts — the demo record. |

## Why this is not just an optimiser

This is the question that decides the prize, so it is built into the case file
rather than argued on stage. The constraints in `inputs/case.json` are chosen so
that **no combination of scalar parameter tweaks can pass**:

- The baseline plate is 37.0 g against a 39 g budget, so uniform thickening buys
  at most ~5% more volume — a 1.11× bending-stress reduction, not enough.
- Widening past 17.6 mm hits the `perforating_vessel_bundle` keepout (centre y=14.8 mm less a 6.0 mm radius); the baseline sits at 16 mm.
- Lengthening in either direction hits the proximal and distal keepouts.

What remains is contouring the plate to the bone and adding local reinforcement:
ribs, a variable thickness profile, hole-to-slot conversion. Those require
**writing geometry code**, not setting a float.

The generator enforces this directly: `build_implant()` raises
`NotImplementedError` if a topology parameter is set without the geometry behind
it. Setting `ribs=[...]` and hoping is not a valid move.

See `design_space_note` in `inputs/case.json` before recalibrating anything.

## Guardrail

`harness/guard.py` is an allowlist, not a denylist. Devin may edit
`generator.py`, `params.py` and `export.py`, and write into `runs/` and `out/`.
Nothing else. A commit touching the validators, the thresholds, the anatomy or the
bone sampler makes the iteration invalid. Anything nobody thought about defaults to
locked — the list in `EDITABLE_GLOBS` is the whole of it.

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

That anterior bow is what makes the generic flat plate fail on this patient. The
plate is mounted with a 0.4 mm clearance at the apex of the bow, and because it is
straight the gap can only grow from there — 6.57 mm at the proximal end, against
the 1.5 mm `max_bone_gap_mm` limit.

The gap is bounded from both sides. `min_bone_gap_mm` is 0.1 mm, because a plate
pressed onto bone crushes the periosteum and interrupts the blood supply the
fracture heals through — zero clearance is a failure, not an optimum. That pair of
bounds is what forces contouring: the plate cannot be pushed inward to close the
gap without violating the floor.

(Not to be confused with `envelope_standoff`, a different check with a 6.0 mm
limit, which measures how far the plate's *outer* surface protrudes into soft
tissue.)
