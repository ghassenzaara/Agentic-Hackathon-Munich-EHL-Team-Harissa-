# Resources

What this project needs, where it comes from, and whether we have it.

Everything in the data section below has now been **downloaded and opened**, not
just checked for a working link. Where that changed the story, the row says so.
The toolchain is pinned and installed via [pixi.toml](pixi.toml). Full verified
inventory, with the numbers behind each claim, is in
[src/refs/DATA_NOTES.md](src/refs/DATA_NOTES.md).

## Known gaps — read before quoting this doc

So the rest of this page can't be skimmed into a promise:

- **One anatomy source in hand.** `ssm_tibia` works; every alternative we tried
  either has no tibia label or can't be pulled programmatically. `paed_ssm` is the
  confirmed second source — the fetch script is written and its login flow tested,
  but it needs a free SimTK account, so **no paediatric bytes are on disk yet**.
- **Adult data, paediatric/veterinary market.** We demo on adults because that is
  what is downloadable anonymously. Paediatric is one free registration away;
  veterinary is not reachable in this timeframe at all.
- **No raw-CT path.** It needs lower-limb CT plus TotalSegmentator's
  `appendicular_bones` task, whose weights licence is unchecked. Unbuilt scope.
- **Nothing has been solved yet.** CalculiX is installed and answers `ccx -v`,
  but no plate geometry exists, no FEA has been run, and there are no tests.
  The only thing proven end to end is data → sampled tibia surfaces.
- **No slicer.** The STL → G-code leg is entirely unproven.
- **ASTM F382 not obtained** (paywalled). Output stays "F382-style".
- **Devin API key still pending**, so the autonomy loop is unexercised.

## Data — bone geometry

**We are targeting children and animals; we build on adult data.** The market is the
cases where no off-the-shelf plate fits — paediatric, deformity, veterinary — because
that is where custom design competes with nothing rather than with a commodity part.
Standard adult tibia is the weakest market precisely because stock plates already work
there. Adult data is what we can actually download today, so that is what the pipeline
runs on. A dataset constraint, not the ambition.

| Source | What | Format | License | Status |
|---|---|---|---|---|
| **SimTK `ssm_tibia`** ([project](https://simtk.org/projects/ssm_tibia), [figshare](https://doi.org/10.6084/m9.figshare.20454462)) | 30 tibia-fibula surface meshes (ages 19–40) **plus 4 statistical shape models** | STL + MATLAB `.mat` | CC-BY-4.0 | ✅ **Downloaded and working.** md5-verified, 445 STLs, 1.1 GB; SSM reconstructs to 1e-13 from Python |
| **TotalSegmentator dataset** ([Zenodo](https://zenodo.org/records/8367088)) | 1228 clinical CTs, 117 structures — **tibia is not one of them** | NIfTI masks | CC-BY-4.0 | ⚠️ **Downloaded, but no tibia label.** Usable only as a mask→mesh test fixture |
| **MedShapeNet 2.0** ([portal](https://medshapenet.ikim.nrw/), [GitHub](https://github.com/GLARKI/MedShapeNet2.0)) | 100k+ anatomical shapes incl. bones | mesh | free, cite | ❌ **Not usable programmatically.** The Python API serves showcase samples only |
| **SimTK `paed_ssm`** ([project](https://simtk.org/projects/paed_ssm), [paper](https://doi.org/10.1038/s41598-022-07267-4)) — *target population* | SSM of pelvis / femur / **tibia-fibula**, ages 4–18, from 333 CT; predicts shape from age, height, weight | SSM (`.mat`) + measurements CSV, 137 MB + 220 KB | CC-BY-4.0 (paper) | 🔑 **Account-gated, script written and waiting.** `fetch/fetch_paed_ssm.sh` runs as soon as `SIMTK_USER`/`SIMTK_PASS` exist. **Not** a drop-in for `fetch_ssm_tibia.sh` — see below |
| **Canine femur/tibia/patella SSM** (97 CT, U. Zürich) — *target population* | veterinary lower-limb shape model | — | on request | ❌ **Gated.** Access by email to the authors; not viable in this timeframe |

**`ssm_tibia` is not just primary — it is effectively our only tibia source.** It is already tibia, already STL, and skips the segmentation stage entirely. Its shape model lets us *sample* unseen anatomies on demand, which is how we back the "five unseen anatomies, live" claim without hand-picking examples. That path is verified working from plain Python (numpy + scipy, no MATLAB): 3500 points, 6996 triangles, 5 retained PCs = 90.3 % of variance, ≤3.7 mm truncation error, and shared mesh topology across samples, so plate-fitting logic written against the mean surface transfers without re-registration.

Because it is the only source, it is also a single point of failure — worth saying out loud rather than implying a redundancy we do not have. Two caveats on the data itself: the participant CSV has **32 rows for 30 cases** (join on the case folders) and carries **no sex column**, so the 20M/10F split is a claim from the paper, not something recoverable from the files.

### `paed_ssm` — the target-population source, and how to get it

Same shape of artefact as `ssm_tibia` (a PCA shape model over registered
surfaces) but built from **333 CT scans of children aged 4–18**, covering
pelvis, femur and **tibia-fibula**. Two files, 137 MB and 220 KB.

**The access route is different and that matters.** `ssm_tibia` is hosted on
figshare and downloads anonymously; `paed_ssm` exists only on SimTK, whose
`frs/download_confirm.php` redirects to `/account/login.php`. figshare and
Zenodo were both searched for a mirror — there is none. So this needs a **free
SimTK account** (instant registration), and `fetch_paed_ssm.sh` scrapes the
login form's per-session CSRF token, posts credentials, then downloads with the
session cookie. Everything up to the authenticated download is tested; the
download itself is unexercised until credentials exist. Earlier drafts of this
doc called it "likely a near drop-in for `fetch_ssm_tibia.sh`" — that was wrong,
and it was wrong in the direction that costs time on the day.

**Why it is worth the registration — the paper hands us our own argument.**
Carman et al. measured exactly the thing we claim. Predicting paediatric bone
shape with the SSM gives RMSE **1.85 ± 0.54 mm** on the tibia-fibula, while
**linearly scaling an adult mesh** to the same child gives **4.39 ± 0.86 mm** —
more than twice the error. That is published, third-party evidence that a child
is not a small adult, and that resizing an adult template is the wrong move.
It is the same distinction the whole build rests on: changing a *size* is not
the same as changing a *shape*. Cite it rather than asserting it.

**There is no working "we also handle raw CT" fallback yet.** TotalSegmentator was cast in that role, and it cannot play it: its 117-label set stops at `femur_left`/`femur_right` and never reaches the tibia, and its scans are mostly thorax/abdomen/pelvis, so the lower leg is usually outside the field of view. Tibia lives in TotalSegmentator's separate `appendicular_bones` task, which is not part of this dataset and whose model weights carry their own unchecked licence. Standing up the CT path therefore means sourcing lower-limb CT *and* clearing that licence — treat it as unbuilt scope, not a fallback in hand.

The NMDID scans behind `ssm_tibia` need registration and can't be redistributed — irrelevant to us, the derived meshes are the open part.

### Hugging Face / OpenML

- **Hugging Face:** useful only for CT mirrors, and none of them carry a tibia.
  - [`YongchengYAO/TotalSegmentator-CT-Lite`](https://huggingface.co/datasets/YongchengYAO/TotalSegmentator-CT-Lite) — live, and what we actually used: it pre-merges the 117 per-structure masks into one NIfTI per case. Masks 822 MB, images 22.6 GB. Same missing-tibia limitation as the source dataset.
  - `mrmrx/CADS-dataset` — real and large (75,789 files, 22,022 CT volumes, 167 structures), but it spans **head to knee**, so it stops short of the tibia too.
  - `YuheLiuu/MedShapeNet` — **effectively empty** (2 files: `.gitattributes` and a README). Do not plan around it.
- **OpenML:** not relevant. It's a tabular-ML benchmark registry; no 3D meshes, no medical volumes.

## Software

[pixi.toml](pixi.toml) is the source of truth for versions; this table is the
reasoning behind it. Everything resolves from **conda-forge**, so there is no
pip layer at all.

| Tool | Role | Status |
|---|---|---|
| `cadquery` 2.8.0 | parametric plate geometry — **the artifact Devin writes** | ✅ installed. **Bumped from 2.5.2** — see below |
| `trimesh` 4.12.2 | clearance checks, decimation, cropping, watertightness | ✅ installed (5.0.0 exists; held deliberately) |
| `gmsh` 4.15.2 | tet meshing for FEA | ✅ installed (`gmsh` binary + `python-gmsh` API) |
| `meshio` 5.3.5 | mesh format bridging | ✅ installed |
| **CalculiX (`ccx`) 2.23** | the FEA solver — **the verdict** | ✅ installed and answering `ccx -v`. Not on PyPI; this is why the env is conda-based |
| `ccx2paraview` 3.2.0 | read solver results back | ✅ installed |
| Python | — | ✅ 3.12, **not** the 3.10 first planned: conda-forge's `ocp 7.7.2` has no cp310 build |
| CuraEngine / PrusaSlicer | STL → G-code | ❌ **not installed, not tested.** No slicer on PATH; the G-code leg of the story is unproven |
| TotalSegmentator (tool) | CT → masks | ❌ **not installed.** Moot until the CT path exists at all, and its **weights licence is still unchecked** |

`pixi run doctor` is what backs the ticks above — it imports every library,
runs the solver, and counts the STLs.

**The cadquery bump is the one deliberate deviation.** On conda-forge, cadquery
and gmsh *share* OpenCASCADE: cadquery 2.5.2 pulls `occt 7.7.2` while gmsh
4.15.2 needs `occt 7.9.3`, which is unsolvable. cadquery 2.8.0 sits on the same
`occt 7.9.3` as gmsh, so the whole geometry stack shares one OCC. Since the
brief names the cadquery→gmsh handoff as the likely point of failure, that is
worth more than holding the pin. (pip hides this clash rather than fixing it —
its gmsh wheel bundles a second OCC copy into the same process.)

## Standards

**ASTM F382** (four-point bend for bone plates) is a paywalled document, ~$70 from ASTM. We can implement the test geometry from published descriptions, but without the document we cannot cite exact span and loading-rate values. Label our output **"F382-style"** and do not claim compliance.

## Accounts and credentials

| Need | Status |
|---|---|
| **Devin API key + org ID** | ⚠️ **To be provided.** Not yet available to us — see [devin-api-setup.md](devin-api-setup.md). Config scaffolding is written and waiting for the key. |
| ACU budget | **We run inside whatever limits the event sets.** See below |
| Hugging Face / Zenodo / SimTK | No account needed, all anonymous download |

### Compute budget

For the hackathon, **we take the ACU allowance and concurrency limits the event gives us and stay inside them.** The caps in `.env.example` (`DEVIN_MAX_ACU_PER_SESSION`, `DEVIN_MAX_CONCURRENT_SESSIONS`, `DEVIN_MAX_ITERATIONS`) are placeholders to be set to the event's numbers once known — they are not a claim about what the system needs.

Outside the event those numbers are the wrong ones. **In real use the budget should be sized against the biomedical engineer's actual caseload** — cases per week, how many candidate designs per case are worth evaluating, and how much of a three-day manual design an hour of fan-out is expected to replace. Fan-out width is a business decision about throughput, not a technical constant.

A cap is recommended, not required — `max_acu_limit` is optional and usage policies are opt-in, so uncapped is the default. Cap it anyway: fan-out multiplies spend.

Devin bills in **ACUs** (Agent Compute Units), not tokens. Consumption is tracked in the dashboard and readable over the API — see [Usage & billing](https://docs.devin.ai/admin/billing/usage), [per-user daily consumption (v3)](https://docs.devin.ai/api-reference/v3/consumption/consumption-daily-users), and [daily consumption (v2)](https://docs.devin.ai/api-reference/v2/consumption/daily-consumption). Org admins can also set hard per-organization ACU limits, which stop activity when hit rather than overspending.
