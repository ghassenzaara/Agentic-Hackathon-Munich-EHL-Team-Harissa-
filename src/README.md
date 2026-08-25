# src — the resources this build runs on

Downloaded, checksum-verified and inspected. Read
[refs/DATA_NOTES.md](refs/DATA_NOTES.md) for the verified inventory.

## Layout

```
src/
├── README.md                   this file
├── env/doctor.py               verify imports + ccx + data (`pixi run doctor`)
├── ssm/load_ssm.py             read the shape model from Python, sample anatomies
│
├── fetch/
│   ├── fetch_ssm_tibia.sh              primary data, md5-verified, idempotent
│   ├── fetch_paed_ssm.sh               target population (ages 4–18); needs SimTK login
│   └── fetch_totalsegmentator_lite.sh  secondary; --with-images for the 22.6 GB
│
├── refs/
│   ├── DATA_NOTES.md           verified inventory
│   └── CITATIONS.md            CC-BY attribution, a licence condition
│
└── data/                       gitignored (3.2 GB: 2.0 GB extracted + 1.3 GB archives)
    ├── ssm_tibia/              30 tibia-fibula cases + 4 shape models
    ├── paed_ssm/               paediatric SSM, ages 4–18 — empty until credentials
    ├── totalsegmentator_lite/  1228 CT mask volumes + metadata
    └── _downloads/             the source archives, kept for re-extraction
```

`data/` is deliberately gitignored. The fetch scripts are the reproducible part; the 2 GB is
not. Both scripts skip a download that is already present, so re-running is
cheap.

## Have / haven't

| Resource | Status |
|---|---|
| `ssm_tibia` meshes + SSM | **downloaded**, md5 verified, 445 STLs, 1.1 GB |
| SSM readable without MATLAB | **verified** — reconstructs training surfaces to 1e-13 |
| `paed_ssm` (ages 4–18) — *target population* | 🔑 **script ready, account-gated.** Needs `SIMTK_USER`/`SIMTK_PASS`; login flow tested, download unexercised |
| TotalSegmentator masks | **downloaded**, 1228 volumes — but **no tibia label** |
| TotalSegmentator CT images | not downloaded, 22.6 GB, script ready |
| MedShapeNet | **unusable programmatically** — API serves samples only |
| Python toolchain | **installed + locked** via `../pixi.toml` (cadquery 2.8.0, see note) |
| CalculiX `ccx` | **installed** — conda-forge 2.23, part of the locked env |
| ASTM F382 | paywalled, not obtained — output stays "F382-style" |
| PeerJ paper PDF | 403 to scripted fetch; open access in a browser |

## Quick start

```bash
pixi install          # whole toolchain incl. the ccx solver, from ../pixi.lock
pixi run setup        # doctor + fetch data + sample 5 unseen anatomies to STL
```

`pixi run setup` is the one command: it verifies imports and that `ccx` is on
PATH, fetches the adult SSM data (~550 MB, resumable, md5-checked), then writes
the mean plus 5 unseen anatomies as STL — the cheapest end-to-end check that the
primary data is intact and usable. The steps are separately runnable as
`pixi run doctor`, `pixi run fetch-ssm`, `pixi run ssm-sample`.

**The target population needs one extra step.** `paed_ssm` (children, ages 4–18)
is the second anatomy source and the one that matches the market. It is not in
`setup` because SimTK gates it behind a login and there is no anonymous mirror:

```bash
# register free at https://simtk.org/account/register.php, then
export SIMTK_USER=... SIMTK_PASS=...      # or put them in .env
pixi run fetch-paed
```

CalculiX comes from conda-forge as part of the locked environment. See
[../pixi.toml](../pixi.toml) for the complete toolchain and the CadQuery 2.8.0
compatibility rationale.

## Why the SSM matters more than the brief assumes

The "five unseen anatomies, live" claim rests entirely on sampling the shape
model, because `ssm_tibia` is effectively the only tibia source we have:
TotalSegmentator does not label the tibia, and MedShapeNet cannot be pulled
programmatically. The good news is that the sampling path is verified working
from plain Python — 5 principal components, 90.3 % of variance, weights in
standard deviations, mesh topology shared across every sample (so any
plate-fitting logic written against the mean surface transfers to samples
without re-registration).
