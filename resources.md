# Resources

What this project needs, where it comes from, and whether we have it.
Nothing here is installed yet — environment setup comes later.

## Data — bone geometry

| Source | What | Format | License | Status |
|---|---|---|---|---|
| **SimTK `ssm_tibia`** ([project](https://simtk.org/projects/ssm_tibia), [figshare](https://doi.org/10.6084/m9.figshare.20454462.v3)) | 30 tibia-fibula surface meshes (20M/10F, ages 19–40) **plus a statistical shape model** | STL | CC-BY | Open, reachable |
| **MedShapeNet 2.0** ([portal](https://medshapenet.ikim.nrw/), [GitHub](https://github.com/GLARKI/MedShapeNet2.0)) | 100k+ anatomical shapes incl. bones; web UI + Python API | mesh | free, cite | Open, reachable |
| **TotalSegmentator dataset** ([Zenodo](https://zenodo.org/records/8367088)) | 1228 clinical CTs, 117 structures incl. tibia | NIfTI masks | CC-BY-4.0 | Open — needs mask→mesh conversion |

**Primary is `ssm_tibia`.** It is already tibia, already STL, and skips the segmentation stage entirely. Its shape model also lets us *sample* unseen anatomies on demand, which is how we back the "five unseen anatomies, live" claim without hand-picking examples.

TotalSegmentator is the fallback and the "we also handle raw CT" story. The NMDID scans behind `ssm_tibia` need registration and can't be redistributed — irrelevant to us, the derived meshes are the open part.

### Hugging Face / OpenML

- **Hugging Face:** useful only for CT mirrors — [`YongchengYAO/TotalSegmentator-CT-Lite`](https://huggingface.co/datasets/YongchengYAO/TotalSegmentator-CT-Lite) is live, `mrmrx/CADS-dataset` has a subset. The `YuheLiuu/MedShapeNet` repo is **empty (2.49 kB, no files)** — do not plan around it. No tibia meshes on HF.
- **OpenML:** not relevant. It's a tabular-ML benchmark registry; no 3D meshes, no medical volumes.

## Software

| Tool | Role | Where from |
|---|---|---|
| `cadquery` 2.5.2 | parametric plate geometry — **the artifact Devin writes** | PyPI |
| `trimesh` 4.12.2 | clearance checks, decimation, cropping, watertightness | PyPI |
| `gmsh` 4.15.2 | tet meshing for FEA | PyPI |
| `meshio` 5.3.5 | mesh format bridging | PyPI |
| **CalculiX (`ccx`) 2.23** | the FEA solver — **the verdict** | **not on PyPI.** conda-forge `calculix`, or `brew tap costerwi/homebrew-calculix && brew install calculix-ccx` |
| `ccx2paraview` 3.2.0 | read solver results back | PyPI |
| CuraEngine / PrusaSlicer | STL → G-code | Docker (Cura) or brew cask |
| TotalSegmentator (tool) | CT → masks, only if we use the CT path | PyPI — check the weights licence separately from the dataset licence |

CalculiX is the one that bites: everything else is a `pip install`, and it isn't. Plain `brew install calculix-ccx` fails without the tap.

**Currently on this machine:** Python 3.10.6, numpy, scipy, `docker`, `conda`. Nothing else from the table above.

## Standards

**ASTM F382** (four-point bend for bone plates) is a paywalled document, ~$70 from ASTM. We can implement the test geometry from published descriptions, but without the document we cannot cite exact span and loading-rate values. Label our output **"F382-style"** and do not claim compliance.

## Accounts and credentials

| Need | Status |
|---|---|
| **Devin API key + org ID** | ⚠️ **To be provided.** Not yet available to us — see [devin-api-setup.md](devin-api-setup.md). Config scaffolding is written and waiting for the key. |
| ACU budget | Unknown, follows from the key. A 40-iteration loop × 5 anatomies × parallel fan-out is a real cost — cap it per session |
| Hugging Face / Zenodo / SimTK | No account needed, all anonymous download |
