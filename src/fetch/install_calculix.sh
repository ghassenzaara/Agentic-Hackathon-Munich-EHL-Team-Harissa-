#!/usr/bin/env bash
# CalculiX (ccx) is the FEA solver and the only dependency that is not a
# pip install. Two routes; conda is the more portable one for a Devin snapshot.
set -euo pipefail

case "${1:-conda}" in
  conda)
    conda install -y -c conda-forge calculix ;;
  brew)
    brew tap costerwi/homebrew-calculix
    brew install calculix-ccx ;;   # plain `brew install calculix-ccx` fails without the tap
  *)
    echo "usage: $0 [conda|brew]" >&2; exit 2 ;;
esac

command -v ccx >/dev/null || { echo "ccx not on PATH after install" >&2; exit 1; }
ccx -v || true
