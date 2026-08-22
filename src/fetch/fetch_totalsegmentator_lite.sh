#!/usr/bin/env bash
# Secondary / CT-pipeline source: TotalSegmentator v2 via the Hugging Face
# "Lite" mirror, which pre-merges the 117 per-structure masks into one NIfTI
# per case. CC-BY-4.0, anonymous.
#
# READ THIS FIRST: the 117-label set has NO tibia — the most distal bone is
# femur_left/right (75/76). Use this for exercising the mask -> mesh -> FEA
# path, not as a tibia source. See ../refs/DATA_NOTES.md.
#
#   masks  ~0.8 GB   (default)
#   images ~22.6 GB  (pass --with-images)
set -euo pipefail

DEST="$(cd "$(dirname "$0")/.." && pwd)/data"
BASE="https://huggingface.co/datasets/YongchengYAO/TotalSegmentator-CT-Lite/resolve/main"
OUT="$DEST/totalsegmentator_lite"
mkdir -p "$DEST/_downloads" "$OUT"

curl -sL --fail -o "$OUT/meta.csv"           "$BASE/meta.csv"
curl -sL --fail -o "$OUT/DATASET_README.md"  "$BASE/README.md"

[ -f "$DEST/_downloads/TS-CT-Lite-Masks.zip" ] || \
  curl -L --fail -o "$DEST/_downloads/TS-CT-Lite-Masks.zip" "$BASE/Masks.zip"
unzip -q -o "$DEST/_downloads/TS-CT-Lite-Masks.zip" -d "$OUT"

if [ "${1:-}" = "--with-images" ]; then
  echo "downloading 22.6 GB of CT volumes..."
  [ -f "$DEST/_downloads/TS-CT-Lite-Images.zip" ] || \
    curl -L --fail -o "$DEST/_downloads/TS-CT-Lite-Images.zip" "$BASE/Images.zip"
  unzip -q -o "$DEST/_downloads/TS-CT-Lite-Images.zip" -d "$OUT"
fi

echo "ok: $(ls "$OUT/Masks" | wc -l | tr -d ' ') mask volumes"
