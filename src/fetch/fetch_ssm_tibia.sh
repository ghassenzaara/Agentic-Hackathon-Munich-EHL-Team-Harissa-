#!/usr/bin/env bash
# Primary bone-geometry source: 30 tibia-fibula surface meshes + statistical
# shape models. CC-BY-4.0, anonymous download, no account.
#
#   Keast M, Bonacci J, Fox A. 2023. PeerJ 11:e14708
#   https://doi.org/10.6084/m9.figshare.20454462
set -euo pipefail

DEST="$(cd "$(dirname "$0")/.." && pwd)/data"
ZIP="$DEST/_downloads/SSM-tibia-V3.zip"
URL="https://ndownloader.figshare.com/files/40194004"   # figshare article 20454462, v4
MD5="c92d24cf00609b44f5fddd5727a0120e"                  # 550,603,776 bytes

mkdir -p "$DEST/_downloads" "$DEST/ssm_tibia"

if [ ! -f "$ZIP" ]; then
  echo "downloading 550 MB from figshare..."
  curl -L --fail -o "$ZIP" "$URL"
fi

got=$(md5 -q "$ZIP" 2>/dev/null || md5sum "$ZIP" | cut -d' ' -f1)
[ "$got" = "$MD5" ] || { echo "checksum mismatch: $got != $MD5" >&2; exit 1; }

unzip -q -o "$ZIP" -d "$DEST/ssm_tibia"
echo "ok: $(ls -d "$DEST"/ssm_tibia/Segmentation/case-* | wc -l | tr -d ' ') cases, \
$(find "$DEST/ssm_tibia" -iname '*.stl' | wc -l | tr -d ' ') STLs"
