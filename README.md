
# <img src="docs/devin-logo.png" width="78" align="right" alt="Cognition Devin">

# AutoImplants

Designs a custom metal plate that screws onto a broken bone, then checks it
against engineering limits and reports what passed and what failed.

You supply two files:

- a **3D surface mesh of the bone** (`.stl`), and
- a **plan** (`.json`) — where the screws go, which regions must not be touched,
  the maximum size, and the forces it must survive.

It builds a parametric CAD solid, exports it, and scores it. Every check is a
number with a limit and an `(x, y, z)` location, so a coding agent can read the
failures, edit the design code, and re-run unattended.

> Research prototype. Not a medical device.

## Requirements

Python 3.12. CadQuery/OCP publish no wheels for 3.13+.

## Install

```bash
bash setup.sh              # creates .venv/, installs requirements.txt
```

Windows: `.\setup.ps1`

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

`smoke` and `loop` take `--dry-run` to skip the API entirely. Credentials and
RBAC: `devin-api-setup.md`.

## Commands

| Command | Purpose |
| --- | --- |
| `-m autoimplants.run` | Build one design and validate it |
| `-m autoimplants.server` | Local web app on `127.0.0.1:8765` — upload, run, review in 3D |
| `-m autoimplants.import_case --case P --bone B` | Turn your own mesh + plan into a runnable case |
| `-m autoimplants.dicom_to_mesh --dicom-dir D --out M` | CT scan folder to bone mesh |
| `-m autoimplants.viewer` | Render a report as one standalone HTML file |

The server needs API credentials to drive the agent loop:

```bash
cp .env.example .env       # fill in DEVIN_API_KEY, DEVIN_ORG_ID
set -a; . ./.env; set +a
.venv/bin/python -m autoimplants.server
```

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
docs/, agent/            Notes and agent skills
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
