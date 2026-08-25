
# <img src="docs/devin-logo.png" width="78" align="right" alt="Cognition Devin">

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
- A localhost review application with a live 3D iteration timeline and durable queue.
- Guarded Devin smoke test, iterative design harness, and server-side surgeon review.
- An automated regression suite covering the runnable path and API client.

The repository also retains the team's higher-fidelity CalculiX/gmsh work in
`solution_code/`, with its data/toolchain setup in `src/` and `pixi.toml`.
That is an experimental verifier scaffold, not the acceptance path used by the
demo. It is deliberately not presented as clinical FEA or silently mixed into
the validated analytical report.

## Quick start

```bash
.venv/bin/python -m autoimplants.run --validators geometry
```

Writes to `out/`:

| File | Contents |
| --- | --- |
| `implant.step` | CAD solid |
| `implant.stl` | mesh the checks are measured on |
| `report.json` | every check: value, limit, location |

Exit code is `0` when all checks pass, `1` when any fails.

Three validators are available; run any subset.

```bash
.venv/bin/python -m autoimplants.run --validators geometry,stress,fea
```

| Validator | Speed | What it does |
| --- | --- | --- |
| `geometry` | ~8 s | Size, wall thickness, mass, screw paths, no-go zones |
| `stress` | fast | Analytical beam-bending estimate, not FEA |
| `fea` | slow | Linear-elastic finite element solve |

## The loop

`run` scores one design. The loop is what makes an agent fix the ones that fail.

```
run  ->  report.json  ->  Devin edits generator.py + params.py  ->  run  ->  ...
```

Every failing check carries its value, its limit and an `(x, y, z)`, which is
the entire input the agent needs. It edits, pushes a branch, and the harness
re-validates that branch itself rather than trusting what the session claims.

**Why an agent and not a parameter sweep.** The thresholds are set so that no
legal constant-thickness parameter combination passes. Reaching green requires a
geometry change — contouring, redistributing section, adding ribs, turning a
hole into a slot. `build_implant()` raises if a topology flag is set without the
geometry behind it, so the agent cannot pass by flipping a label.

**What it may touch.** `harness/guard.py` is an allowlist: generator, parameters,
export, and iteration records. Never anatomy, thresholds, validators, or the
guard itself.

```bash
.venv/bin/python -m harness.smoke --acu-limit 5              # prove the path end to end
.venv/bin/python -m harness.loop --max-iterations 3 --acu-limit 5 --branch devin/design
.venv/bin/python -m harness.guard <base-sha>                 # audit a candidate range
```

`smoke` and `loop` take `--dry-run` to skip the API entirely. Credentials go in
`.env` (see `.env.example`): a Devin service-user API key and the organization ID.

## Commands

| Command | Purpose |
| --- | --- |
| `-m autoimplants.run` | Build one design and validate it |
| `-m autoimplants.server` | Local web app on `127.0.0.1:8765` — upload, run, review in 3D |
| `-m autoimplants.import_case --case P --bone B` | Turn your own mesh + plan into a runnable case |
| `-m autoimplants.dicom_to_mesh --dicom-dir D --out M` | CT scan folder to bone mesh |
| `-m autoimplants.viewer` | Render a report as one standalone HTML file |

## Web app

```bash
.venv/bin/python -m autoimplants.server
```

Open <http://127.0.0.1:8765>. Drop in a bone `.stl` or a zipped DICOM series plus
a plan `.json`, watch iterations arrive in a 3D timeline, and download the STEP,
STL and report.

It starts and serves fine with no credentials — you can upload, preview meshes
and read past runs. Credentials are only needed to *start* an agent run:

```bash
cp .env.example .env       # add DEVIN_API_KEY and DEVIN_ORG_ID, then restart
```

`.env` is read automatically at startup; you do not need to export it yourself.
`/api/preflight` reports exactly what is missing.

### Letting other people reach it

```bash
.venv/bin/python -m autoimplants.server --host 0.0.0.0 --port 8765
```

Others on the same network then use `http://<your-ip>:8765`.

> **There is no login.** Every route is open, so anyone who can reach the port
> can start runs, spend ACUs and read every case on the box. Use it on a trusted
> network only, and do not put it on the public internet.

A cloud agent cannot POST back to a loopback address. Give it a reachable base
URL when you tunnel:

```bash
.venv/bin/python -m autoimplants.server --public-url https://<tunnel-host>
```

### Sending someone a result without a server

```bash
.venv/bin/python -m autoimplants.viewer \
  --case inputs/case.json --implant out/implant.stl \
  --report out/report.json --out out/viewer.html
```

One self-contained HTML file — geometry, report and 3D viewer inlined, no
network. Email it, attach it to a PR, or open it offline.

## Ready-made inputs

```bash
ls real_cases/example        # synthetic femur + four plans
ls real_cases/synthetic_ct   # DICOM fixture for the CT path
```

## Tests

```bash
.venv/bin/python -m pytest   # 224 tests
```

## Repo tree

```
autoimplants/            The library
  run.py                 One iteration: generate -> export -> validate -> report
  generator.py           The parametric CAD model (the file the agent edits)
  params.py              Tunable design parameters
  patch.py               Anatomy-agnostic implant family: a shell over a bone region
  export.py              STEP/STL writers
  contracts.py           Frozen Report/Check types shared by every component
  validators/            geometry.py, stress.py, fea.py + registry
  bone.py                Bone surface sampling
  landmarks.py           Propose a coordinate frame from a mesh
  section.py             Cross-section properties of the exported solid
  mesh_quality.py        Gate for meshes arriving from outside the repo
  self_intersection.py   Does the mesh pass through itself?
  surgical_plan.py       Plan schema and its checks
  case_io.py             Per-case file resolution
  fea.py                 Linear-elastic FE solver
  dicom_to_mesh.py       CT series -> mesh
  import_case.py         External mesh + plan -> runnable case
  viewer.py              Report -> standalone HTML
  viewer_template.html
  assets/                Fonts and the demo femur

harness/                 Agent driver
  loop.py                Iterate: run -> report -> agent -> repeat
  devin_client.py        API client
  guard.py               Allowlist of what the agent may change
  smoke.py, test_all.py

real_cases/              Ready-made inputs
  example/               Synthetic femur + plans
  synthetic_ct/          DICOM fixture
  synthetic_patch/, synthetic_scapula/

inputs/                  Default case used by `run`
tests/                   224 tests
prompts/                 Agent instructions
docs/                    Notes
solution_code/, src/     Experimental gmsh/CalculiX lane — optional, see pixi.toml
```

## Optional solver lane

`solution_code/` and `src/` hold a separate gmsh/CalculiX environment, pinned in
`pixi.toml` (Linux and macOS). Not needed for anything above.

```bash
pixi install && pixi run doctor
```

## License

MIT. See `LICENSE`.
