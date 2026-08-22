#!/usr/bin/env bash
# Target-population bone geometry: paediatric lower-limb SSM, ages 4-18.
#
#   Carman L, Besier T, Choisne J. 2022. Morphological variation in paediatric
#   lower limb bones. Scientific Reports 12:3251. CC-BY-4.0
#   https://doi.org/10.1038/s41598-022-07267-4
#   https://simtk.org/projects/paed_ssm
#
# UNLIKE fetch_ssm_tibia.sh THIS IS NOT ANONYMOUS. ssm_tibia is served by
# figshare; paed_ssm is served only by SimTK, which redirects
# frs/download_confirm.php to /account/login.php. There is no figshare or
# Zenodo mirror (both searched). A free SimTK account is required:
#
#   1. register (free, instant): https://simtk.org/account/register.php
#   2. export SIMTK_USER=... SIMTK_PASS=...   (or put them in .env)
#   3. pixi run fetch-paed
#
# No checksum is pinned: the files are behind a login, so the bytes could not
# be fetched and hashed here. The script prints the md5 it received -- paste it
# into MD5_EXPECTED below on first successful run to make later runs verifying.
set -euo pipefail

DEST="$(cd "$(dirname "$0")/.." && pwd)/data"
DL="$DEST/_downloads"
OUT="$DEST/paed_ssm"
GROUP=2210
MD5_EXPECTED=""   # fill in after the first successful download

# file id : filename  (from https://simtk.org/frs/?group_id=2210)
FILES=(
  "6551:Lower_limb_bone_pred.zip"        # 137 MB - the shape model itself
  "6769:Clinical_bone_measurements.zip"  # 220 KB - linear bone measurements
)

# Pull credentials from .env if not already exported.
ENVFILE="$(cd "$(dirname "$0")/../.." && pwd)/.env"
if [ -z "${SIMTK_USER:-}" ] && [ -f "$ENVFILE" ]; then
  # shellcheck disable=SC1090
  SIMTK_USER=$(grep -E '^SIMTK_USER=' "$ENVFILE" | cut -d= -f2- || true)
  SIMTK_PASS=$(grep -E '^SIMTK_PASS=' "$ENVFILE" | cut -d= -f2- || true)
fi

if [ -z "${SIMTK_USER:-}" ] || [ -z "${SIMTK_PASS:-}" ]; then
  cat >&2 <<'MSG'
error: SIMTK_USER / SIMTK_PASS not set.

paed_ssm is account-gated -- unlike ssm_tibia there is no anonymous route.
Register free at https://simtk.org/account/register.php, then either

    export SIMTK_USER=you@example.com SIMTK_PASS=...

or add those two lines to .env (already gitignored).
MSG
  exit 2
fi

mkdir -p "$DL" "$OUT"
JAR="$(mktemp -t simtk-cookies)"
trap 'rm -f "$JAR"' EXIT

echo "logging in to simtk.org as $SIMTK_USER ..."
# The login form carries a per-session CSRF token (form_key) that must be
# scraped from the page it is posted from; a hardcoded value is rejected.
FORM_KEY=$(curl -sL --fail -c "$JAR" "https://simtk.org/account/login.php" \
  | grep -oE "name=[\"']form_key[\"'] value=[\"'][a-f0-9]+" \
  | head -1 | grep -oE '[a-f0-9]{16,}')

[ -n "$FORM_KEY" ] || { echo "could not scrape form_key -- login page layout changed" >&2; exit 1; }

curl -sL --fail -b "$JAR" -c "$JAR" -o /dev/null \
  --data-urlencode "form_key=$FORM_KEY" \
  --data-urlencode "form_loginname=$SIMTK_USER" \
  --data-urlencode "form_pw=$SIMTK_PASS" \
  --data-urlencode "return_to=/" \
  --data-urlencode "login=Login" \
  "https://simtk.org/plugins/authbuiltin/post-login.php"

grep -q "session_ser\|session_hash" "$JAR" \
  || { echo "login failed -- no session cookie. Check SIMTK_USER / SIMTK_PASS." >&2; exit 1; }
echo "  session established"

for entry in "${FILES[@]}"; do
  fid="${entry%%:*}"; fname="${entry#*:}"
  zip="$DL/$fname"

  if [ ! -f "$zip" ]; then
    echo "downloading $fname ..."
    curl -L --fail -b "$JAR" -c "$JAR" -o "$zip" \
      "https://simtk.org/frs/download_confirm.php/file/$fid/$fname?group_id=$GROUP"
  else
    echo "$fname already present, skipping download"
  fi

  # A login redirect returns 200 with an HTML body, so status is not enough --
  # check the actual bytes are a zip before trusting the file.
  if ! unzip -tq "$zip" >/dev/null 2>&1; then
    echo "error: $fname is not a valid zip (probably an HTML login page)." >&2
    echo "       first bytes: $(head -c 80 "$zip" | tr -d '\0' | tr '\n' ' ')" >&2
    rm -f "$zip"
    exit 1
  fi

  echo "  md5 $(md5 -q "$zip" 2>/dev/null || md5sum "$zip" | cut -d' ' -f1)  $fname"
  unzip -q -o "$zip" -d "$OUT"
done

echo "ok: extracted to $OUT"
find "$OUT" -maxdepth 2 -type d | head -20
echo "mat files: $(find "$OUT" -iname '*.mat' | wc -l | tr -d ' ')  \
stl: $(find "$OUT" -iname '*.stl' | wc -l | tr -d ' ')"
