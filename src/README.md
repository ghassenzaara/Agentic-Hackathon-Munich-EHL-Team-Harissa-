# src — the resources this build runs on

Collected from [../resources.md](../resources.md), downloaded, checksum-verified
and inspected. Read [refs/DATA_NOTES.md](refs/DATA_NOTES.md) before trusting
anything in the parent doc — three of its claims turned out to be wrong, one
of them load-bearing.

## Layout

```
src/
├── README.md                   this file
├── requirements.txt            superseded by ../pixi.toml; kept as reference
├── environment.yml             superseded by ../pixi.toml; documents snapshot shape
│
├── env/doctor.py               verify imports + ccx + data (`pixi run doctor`)
├── ssm/load_ssm.py             read the shape model from Python, sample anatomies
│
├── fetch/
│   ├── fetch_ssm_tibia.sh              primary data, md5-verified, idempotent
│   ├── fetch_totalsegmentator_lite.sh  secondary; --with-images for the 22.6 GB
│   └── install_calculix.sh             conda|brew — the one non-pip dependency
│
├── refs/
│   ├── DATA_NOTES.md           verified inventory + corrections to resources.md
│   └── CITATIONS.md            CC-BY attribution, a licence condition
│
└── data/                       gitignored (3.2 GB: 2.0 GB extracted + 1.3 GB archives)
    ├── ssm_tibia/              30 tibia-fibula cases + 4 shape models
    ├── totalsegmentator_lite/  1228 CT mask volumes + metadata
    └── _downloads/             the source archives, kept for re-extraction
```

`data/` is deliberately gitignored (the repo's `.gitignore` already excludes
`data/` and `*.stl`). The fetch scripts are the reproducible part; the 2 GB is
not. Both scripts skip a download that is already present, so re-running is
cheap.

## Have / haven't

| Resource | Status |
|---|---|
| `ssm_tibia` meshes + SSM | **downloaded**, md5 verified, 445 STLs, 1.1 GB |
| SSM readable without MATLAB | **verified** — reconstructs training surfaces to 1e-13 |
| TotalSegmentator masks | **downloaded**, 1228 volumes — but **no tibia label** |
| TotalSegmentator CT images | not downloaded, 22.6 GB, script ready |
| MedShapeNet | **unusable programmatically** — API serves samples only |
| Python toolchain | **installed + locked** via `../pixi.toml` (cadquery 2.8.0, see note) |
| CalculiX `ccx` | **installed** — conda-forge 2.23, part of the locked env |
| ASTM F382 | paywalled, not obtained — output stays "F382-style" |
| PeerJ paper PDF | 403 to scripted fetch; open access in a browser |
| Devin API key | still pending, see [../devin-api-setup.md](../devin-api-setup.md) |

## Quick start

```bash
pixi install          # whole toolchain incl. the ccx solver, from ../pixi.lock
pixi run setup        # doctor + fetch data + sample 5 unseen anatomies to STL
```

`pixi run setup` is the one command: it verifies imports and that `ccx` is on
PATH, fetches the SSM data (~550 MB, resumable, md5-checked), then writes the
mean plus 5 unseen anatomies as STL — the cheapest end-to-end check that the
primary data is intact and usable. The steps are separately runnable as
`pixi run doctor`, `pixi run fetch-ssm`, `pixi run ssm-sample`.

CalculiX now comes from conda-forge as part of the locked environment, so
`fetch/install_calculix.sh` is no longer needed on the pixi path. See
[../pixi.toml](../pixi.toml) for why pixi replaced the pip+conda split, and for
the one deliberate version bump (cadquery 2.5.2 → 2.8.0) it forced.

## Why the SSM matters more than the brief assumes

The "five unseen anatomies, live" claim rests entirely on sampling the shape
model, because `ssm_tibia` is effectively the only tibia source we have:
TotalSegmentator does not label the tibia, and MedShapeNet cannot be pulled
programmatically. The good news is that the sampling path is verified working
from plain Python — 5 principal components, 90.3 % of variance, weights in
standard deviations, mesh topology shared across every sample (so any
plate-fitting logic written against the mean surface transfers to samples
without re-registration).
