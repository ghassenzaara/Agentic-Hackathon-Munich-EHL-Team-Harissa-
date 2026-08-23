#!/usr/bin/env bash
# Environment bootstrap. Devin runs this verbatim in its container, and it is the
# same path we use locally, so "works on my machine" and "works for Devin" cannot
# drift apart.
#
# Python 3.12 is not a preference. CadQuery/OCP publish no wheels for 3.13+, so a
# newer interpreter fails at install time with an unhelpful resolver error.
set -euo pipefail

cd "$(dirname "$0")"

# gmsh's wheel links against libGLU even when it only meshes headlessly, so the
# stress solver cannot import without it. Best effort: a machine that already has
# it, or has no apt, carries on.
if ! ldconfig -p 2>/dev/null | grep -q libGLU; then
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y libglu1-mesa || echo "libglu1-mesa not installed; the stress solver will not import"
  fi
fi

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

"$PY" -c "import sys, cadquery, trimesh, gmsh; print('python', sys.version.split()[0]); print('cadquery', cadquery.__version__); print('trimesh', trimesh.__version__); print('gmsh', gmsh.__version__ if hasattr(gmsh, '__version__') else 'ok')"

echo
echo "Environment ready. Check the baseline design with:"
echo "  $PY -m autoimplants.run --validators geometry,fea"
