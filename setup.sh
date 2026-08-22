#!/usr/bin/env bash
# Environment bootstrap. Devin runs this verbatim in its container, and it is the
# same path we use locally, so "works on my machine" and "works for Devin" cannot
# drift apart.
#
# Python 3.12 is not a preference. CadQuery/OCP publish no wheels for 3.13+, so a
# newer interpreter fails at install time with an unhelpful resolver error.
set -euo pipefail

cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12
  uv pip install -r requirements.txt
else
  echo "uv not found, falling back to python3.12 -m venv"
  python3.12 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

PY=".venv/bin/python"
[ -x "$PY" ] || PY=".venv/Scripts/python.exe"   # Windows layout

"$PY" -c "import sys, cadquery, trimesh; print('python', sys.version.split()[0]); print('cadquery', cadquery.__version__); print('trimesh', trimesh.__version__)"

echo
echo "Environment ready. Check the baseline design with:"
echo "  $PY -m autoimplants.run --validators geometry,stress"
