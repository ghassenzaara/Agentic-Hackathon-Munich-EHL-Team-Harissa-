# Data notes — what is actually in `src/data`, verified by inspection

Everything below was checked against the downloaded files, not read off a
portal page. Where it contradicts `../../resources.md`, this file is right.

## Corrections to resources.md

1. **TotalSegmentator has no tibia.** `resources.md` says "117 structures
   incl. tibia". It does not. The most distal bone in the v2 label set is
   `femur_left` / `femur_right` (75/76); the list ends at ribs, sternum and
   costal cartilages. Tibia lives in TotalSegmentator's separate
   `appendicular_bones` subtask, which is **not part of this 1228-CT
   dataset**. On top of that, the scans are mostly thorax/abdomen/pelvis, so
   the lower leg is usually outside the field of view. The "we also handle
   raw CT" story does not work off this dataset as written — it needs
   lower-limb CT plus the `appendicular_bones` task, and that task's model
   weights carry their own licence.

2. **MedShapeNet is not a usable programmatic source right now.** Its Python
   API serves only "showcase" samples from a MinIO bucket; the bulk archive
   is waiting on storage (the maintainers' own README says so). The portal
   responds, but plan on manual download at most. `resources.md` already
   flags the empty Hugging Face mirror; the API is the same story.

3. **The shape models are MATLAB `.mat`, and all the dataset's own code is
   MATLAB** (GIBBON, geom3d, export_fig). That is not a blocker — see below —
   but nothing in the dataset runs as shipped without MATLAB.

4. **`paed_ssm` is account-gated, not merely "not yet pulled".**
   `resources.md` called it "likely a near drop-in for `fetch_ssm_tibia.sh`".
   It is not. `ssm_tibia` downloads anonymously from **figshare**; `paed_ssm`
   exists only on **SimTK**, whose `frs/download_confirm.php` redirects to
   `/account/login.php`. No figshare or Zenodo mirror exists (both searched via
   their APIs). It needs a free SimTK account and a scraped per-session
   `form_key` CSRF token — `fetch/fetch_paed_ssm.sh` does this. The login and
   failure paths are tested; the authenticated download is not, because no
   credentials exist yet.

Net effect: **`ssm_tibia` is not just the primary source, it is effectively
the only tibia source.** The SSM sampling path is what backs "five unseen
anatomies", so it carries more weight than the brief assumes.

## ssm_tibia — verified contents

550,603,776 bytes, md5 `c92d24cf00609b44f5fddd5727a0120e`, figshare v4,
CC-BY-4.0. Unpacks to ~1.1 GB, 445 STLs.

```
Segmentation/case-<id>/     30 cases, ages 19–40
  <id>-tibia-cortical.stl          ~39.6k tris, ~353 mm long   <- the build target
  <id>-tibia-cortical-remesh.stl   ~20.7k tris, evenly remeshed <- cheaper for FEA
  <id>-tibia-trabecular.stl        ~10.5k tris, inner boundary  <- screw purchase
  <id>-fibula.stl                  ~16.8k tris
  <id>-TP.stl                      tibial plateau patch, ~65x44x13 mm
  <id>-AJ.stl                      ankle joint patch, ~31x32x5 mm
  LC/LM/MC/MM.txt                  single landmark points, UTF-16 XML
                                   (Materialise/Mimics format, not plain text)
  tibial-plateau.xml               plateau definition
ShapeModels/{tibia,tibia-fibula,tibia-plus-trabecular,trabecular-tibia}/
  *ShapeModel.mat                  the model
  *ShapeModel_mean.stl             mean surface (point order differs from .mat)
Data/participant-characteristics.csv   age, weight, height
Data/registeredSurfaceDataPoints.mat   pre-registered points (skips a 1–2 h step)
Examples/case{1,2,3}/            MATLAB: sample a population; predict trabecular;
                                 reconstruct from palpable landmarks
Code/                            MATLAB SSM construction + supplementary deps
```

Two small snags: the participant CSV has **32 rows for 30 cases** — ids
`134065` and `172501` have no segmentation folder, so join on the case dirs,
not the CSV. And the CSV has **no sex column**, so the 20M/10F split quoted
in `resources.md` cannot be recovered from the data itself (it is in the
paper).

## The shape model, read from Python — verified working

`src/ssm/load_ssm.py`. `scipy.io.loadmat` reads the `.mat` directly
(pre-v7.3, no h5py). No MATLAB anywhere in the path.

Tibia model: 3500 points, 6996 triangles, 29 PCs, **5 retained = 90.3 % of
variance**. Fields: `mean` (1, 10500), `loadings` (10500, 29) with unit-norm
columns, `latent` (29,), `score` (30, 29), `F` (6996, 3) **1-based, MATLAB
convention — convert before use**. Point layout is x,y,z interleaved per
point, so a plain row-major `reshape(3500, 3)` is correct.

Validation actually run:

- `mean + loadings @ score[i]` reproduces training surface `nodes[i]` to
  **1e-13** — the reconstruction math is confirmed, not assumed.
- Truncating to the 5 retained PCs costs **≤3.7 mm** max point error on
  case 0, consistent with the paper's reported error.
- `sqrt(latent)` equals the column std of `score`, so sampling weights in
  units of standard deviation is the right scaling.
- Sampled surfaces measure ~72 x 397 x 72 mm — a plausible tibia.

Two traps. The stored `reconstructed` array is in a **different, recentred
frame** (values near the origin) — do not diff it against your own
reconstruction, it will look 400 mm wrong when nothing is. And on macOS the
Accelerate BLAS raises spurious divide-by-zero/overflow flags on this matmul
even though every input is finite; the loader suppresses them deliberately.

## totalsegmentator_lite — verified contents

1228 merged mask volumes (`Masks/s*.nii.gz`), 902 MB extracted, plus
`meta.csv` (age, sex, institute, study type, scanner, pathology) and the
mirror's own README carrying the full 117-label map. Images (22.6 GB) not
downloaded — `fetch/fetch_totalsegmentator_lite.sh --with-images`.

Given correction 1, treat this as a **test fixture for the mask -> mesh ->
mesh-check path** (femur is at least a long bone), not as tibia geometry.

## Not obtained

- **PeerJ 14708 PDF** — peerj.com returns 403 to scripted fetches. Open
  access in a browser; cited in `CITATIONS.md`.
- **ASTM F382** — paywalled, ~$70. Output stays labelled "F382-style".
- **NMDID source CTs** — registration required, not redistributable, and not
  needed: the derived meshes are the open part.
- **`paed_ssm` files** — `Lower_limb_bone_pred.zip` (137 MB) and
  `Clinical_bone_measurements.zip` (220 KB). Behind a SimTK login, so no bytes
  and therefore **no pinned checksum**; `fetch_paed_ssm.sh` prints the md5 it
  receives so the first successful run can pin it.
