# AutoImplants

An autonomous engineering loop for patient-specific orthopedic fixation plates,
built for the Cognition/Devin track at the TUM.ai Munich Agentic Hackathon.

The working demo takes a bone mesh and surgical constraints, generates a
CadQuery implant, exports STEP and STL, runs geometry and reduced-order stress
checks, and produces a structured report. Devin can read that report, edit the
small allowed design surface, commit its engineering rationale, and repeat.

> Research prototype only. It is not a clinical device or a substitute for
> surgical planning, verification, validation, or regulatory review.

## What is working

- Parametric implant generation and STEP/STL export.
- Geometry, fit, keepout, screw-path, mass, and analytical stress checks.
- Synthetic femur demo plus mesh/surgical-plan import for external cases.
- DICOM-to-mesh ingestion for the synthetic CT fixture.
- A self-contained offline HTML review page.
- Guarded Devin smoke test and iterative design harness.
- An automated regression suite covering the runnable path and API client.

The repository also retains the team's higher-fidelity CalculiX/gmsh work in
`solution_code/`, with its data/toolchain setup in `src/` and `pixi.toml`.
That is an experimental verifier scaffold, not the acceptance path used by the
demo. It is deliberately not presented as clinical FEA or silently mixed into
the validated analytical report.

## Quick start

Python 3.12 is required because the CadQuery/OCP stack is pinned for it.

Windows PowerShell:

```powershell
.\setup.ps1
$py = ".\.venv\Scripts\python.exe"
```

Linux, macOS, WSL, or Git Bash:

```bash
bash setup.sh
PY=.venv/bin/python
[ -x "$PY" ] || PY=.venv/Scripts/python.exe
```

Run one complete baseline iteration:

```powershell
& $py -m autoimplants.run --validators geometry,stress
```

```bash
$PY -m autoimplants.run --validators geometry,stress
```

The baseline intentionally exits with code `1`. It is a generic flat plate
that should fail bone conformance and stress checks; that structured failure is
the starting signal for the autonomous design loop. A crash or missing artifact
is a real failure. A report that says `FAIL` is the expected baseline result.

The command writes:

- `out/implant.step` — CAD deliverable;
- `out/implant.stl` — measured validation mesh;
- `out/report.json` — machine-readable verdict.

Build the offline review page:

```powershell
& $py -m autoimplants.viewer
Start-Process .\out\viewer.html
```

```bash
$PY -m autoimplants.viewer
# open out/viewer.html in a browser
```

## Verify the project

```powershell
& $py -m harness.test_all
```

On Linux/macOS, direct pytest is also available:

```bash
$PY -m pytest -q
```

The grouped runner is the portable default. It uses a workspace-local temp
directory and keeps the SimpleITK imaging runtime out of the OpenCASCADE CAD
process; some Windows builds otherwise access-violate during interpreter
shutdown after printing a fully passing pytest result.

Useful fast checks:

```powershell
& $py -m autoimplants.run --validators stub --no-build
& $py -m harness.smoke --dry-run
```

## Run the Devin path

The client uses the current organization-scoped Devin v3 API.

1. Copy `.env.example` to `.env`.
2. Add the `cog_...` service-user key and `org_...` organization ID.
3. Give the service user session permissions and keep the ACU cap small first.
4. Prove cloning, setup, validation, commit, and push with the smoke test.

```powershell
Copy-Item .env.example .env
# edit .env, then:
& $py -m harness.smoke --acu-limit 5
```

Once smoke passes, start the guarded iterative loop:

```powershell
& $py -m harness.loop --max-iterations 8 --acu-limit 5 --branch devin/design
```

The loop validates locally, starts one Devin session per failed iteration,
requires structured output, fetches the pushed branch, rejects changes outside
the allowlist, and independently validates the resulting commit. A timeout,
approval pause, or missing structured result stops safely instead of scoring a
stale checkout. See `devin-api-setup.md` for credential and RBAC details.

## External mesh and surgical plan

The importer accepts a mesh plus an explicit surgical plan. It recovers the
repository frame from landmarks, gates mesh scale/components/watertightness,
checks the plan against the bone, and writes a runnable case without inventing
missing screws or landmarks.

```powershell
& $py -m autoimplants.import_case `
  --case real_cases/example/surgical_plan_oblique.json `
  --bone real_cases/example/bone.stl

& $py -m autoimplants.run `
  --case real_cases/EXAMPLE-FEMUR-CT-001-OBLIQUE/generated/case.json `
  --validators geometry,stress `
  --out out_real
```

Bash uses the same arguments with `\` line continuations. Full schemas and
patient-data rules are in `docs/real-ct-cases.md`.

For the committed synthetic CT phantom:

```powershell
& $py real_cases/synthetic_ct/make_ct.py
& $py -m autoimplants.dicom_to_mesh `
  --dicom-dir real_cases/synthetic_ct/series `
  --bone femur `
  --out real_cases/synthetic_ct/bone.stl
& $py -m autoimplants.import_case `
  --case real_cases/synthetic_ct/surgical_plan.json `
  --bone real_cases/synthetic_ct/bone.stl
& $py -m autoimplants.run `
  --case real_cases/SYNTH-CT-FEMUR-001/generated/case.json `
  --validators geometry,stress `
  --out out_ct
& $py -m autoimplants.viewer `
  --case real_cases/SYNTH-CT-FEMUR-001/generated/case.json `
  --implant out_ct/implant.stl `
  --report out_ct/report.json `
  --out out_ct/viewer.html
```

All DICOM and generated case artifacts are gitignored. Never add patient
imaging to this repository. The generated flat baseline is expected to report
`FAIL`; success here means every stage completed and produced its artifacts.

## Architecture

```text
bone mesh + case + surgical plan
              |
              v
   CadQuery generator.py  <--- the guarded code Devin edits
              |
              +--> implant.step
              +--> implant.stl
                        |
                        v
          geometry + analytical stress validators
                        |
                        v
                  report.json
                   /       \
                  v         v
          offline viewer   Devin iteration
                              |
                              v
                    allowlist guard + recheck
```

Key paths:

| Path | Purpose |
|---|---|
| `autoimplants/generator.py` | Guarded parametric design surface |
| `autoimplants/run.py` | One generate/export/validate iteration |
| `autoimplants/validators/` | Geometry and analytical stress verdicts |
| `autoimplants/import_case.py` | Mesh and plan to normalized runnable case |
| `autoimplants/dicom_to_mesh.py` | Optional raw DICOM ingestion |
| `autoimplants/viewer.py` | Self-contained review HTML generator |
| `harness/` | Devin API client, smoke test, loop, and edit guard |
| `inputs/` | Locked synthetic anatomy and thresholds |
| `tests/` | Runnable path regression suite |
| `solution_code/verify/` | Experimental gmsh/CalculiX verifier scaffold |
| `src/` and `pixi.toml` | Optional dataset and FEA environment work |

## Why an agent instead of scalar optimization

The synthetic thresholds are calibrated so no legal constant-thickness scalar
combination passes. A passing design needs a geometry/code change such as
contouring, local section redistribution, ribs, or hole-to-slot conversion.
`build_implant()` raises when a topology flag is set without its geometry being
implemented, so the agent cannot pass by merely toggling a label.

The guard is an allowlist: Devin may edit the generator/parameter/export design
surface and write iteration records, but it may not alter anatomy, thresholds,
validators, or the guard itself. Run `python -m harness.guard <base-sha>` to
audit a candidate range.

## Optional FEA/data lane

`pixi.toml` captures the separate gmsh/CalculiX environment assembled by the
other prototype. Its current lock targets Linux and macOS; it is not required
for the Windows demo above.

On a supported machine with pixi:

```bash
pixi install
pixi run doctor
# large optional download (~550 MB):
pixi run setup
```

`pixi run setup` verifies the solver and downloads/samples the tibia SSM. Keep
this lane separate from the hackathon acceptance verdict until a tested adapter
turns the exported implant into a tetrahedral model with defensible boundary
conditions and maps its result back into `autoimplants.contracts.Report`.
