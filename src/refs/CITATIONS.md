# Attribution

Every dataset in `src/data/` is CC-BY-4.0. Attribution is a licence
condition, not a courtesy — carry these into any paper, slide, or demo.

## ssm_tibia — primary bone geometry

> Keast M, Bonacci J, Fox A. 2023. Geometric variation of the human
> tibia-fibula: a public dataset of tibia-fibula surface meshes and
> statistical shape model. *PeerJ* 11:e14708.
> https://doi.org/10.7717/peerj.14708

Dataset: https://doi.org/10.6084/m9.figshare.20454462 (v4) · CC-BY-4.0 ·
project page https://simtk.org/projects/ssm_tibia

Underlying CT scans come from the New Mexico Decedent Image Database
(https://nmdid.unm.edu/). The scans themselves are **not** redistributable and
are not in this repo; the derived surface meshes are the open part.

The dataset's `Code/Supplementary/` bundles third-party MATLAB functions
(CPD2, intriangulation). If any of that logic gets ported, cite the original
authors — see the dataset's own README.

## paed_ssm — target-population bone geometry (paediatric)

> Carman L, Besier TF, Choisne J. 2022. Morphological variation in paediatric
> lower limb bones. *Scientific Reports* 12:3251.
> https://doi.org/10.1038/s41598-022-07267-4

Dataset: https://simtk.org/projects/paed_ssm · paper CC-BY-4.0 · **account-gated,
no anonymous mirror** (see `fetch/fetch_paed_ssm.sh`)

Statistical shape model of pelvis, femur and tibia-fibula from 333 CT scans of
children aged 4–18. The number worth quoting: SSM prediction of tibia-fibula
geometry gives RMSE 1.85 ± 0.54 mm against 4.39 ± 0.86 mm for linear scaling of
an adult mesh — published evidence that resizing an adult is not the same as
shaping a child.

Underlying CT is clinical paediatric imaging and is **not** redistributable;
the shape model is the open derived artefact.

## TotalSegmentator — secondary / CT-mask path

> Wasserthal J, et al. TotalSegmentator: robust segmentation of 104 anatomic
> structures in CT images. *Radiology: Artificial Intelligence* (2023).
> https://doi.org/10.1148/ryai.230024

Dataset v2: https://doi.org/10.5281/zenodo.8367088 · CC-BY-4.0
Mirror used here: https://huggingface.co/datasets/YongchengYAO/TotalSegmentator-CT-Lite

The TotalSegmentator *tool*'s model weights carry their own licence, separate
from the dataset licence. Check it before shipping anything that runs them.

## ASTM F382

Not obtainable — paywalled, ~$70. Nothing here cites it. Output is labelled
**"F382-style"**; do not claim compliance.
