"""Prove the toolchain is real before the design loop depends on it.

Checks the three things that silently break this build: a missing Python
import, a CalculiX binary that isn't on PATH (the verdict engine), and absent
primary anatomy data. Exits non-zero on any hard failure so `pixi run setup`
stops instead of continuing into a loop with no solver.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# (module, attribute holding the version). ccx2paraview exposes none and its
# conda metadata reports 0.0.0, so it reports as "imported" -- the version that
# counts is the one pinned in pixi.toml and frozen in pixi.lock.
MODULES = [
    ("numpy", "__version__"),
    ("scipy", "__version__"),
    ("cadquery", "__version__"),
    ("trimesh", "__version__"),
    ("gmsh", "GMSH_API_VERSION"),
    ("meshio", "__version__"),
    ("ccx2paraview", None),
]

failures: list[str] = []
warnings: list[str] = []


def check_imports() -> None:
    print("imports")
    for name, attr in MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            failures.append(f"import {name}: {exc}")
            print(f"  FAIL  {name:<14} {exc}")
            continue
        version = getattr(mod, attr, "(no version attr)") if attr else "imported"
        print(f"  ok    {name:<14} {version}")


def check_solver() -> None:
    print("solver")
    ccx = shutil.which("ccx")
    if ccx is None:
        failures.append("ccx not on PATH -- CalculiX is the verdict engine")
        print("  FAIL  ccx            not on PATH")
        return
    print(f"  ok    ccx            {ccx}")
    # `ccx -v` prints the version and exits non-zero on some builds; the binary
    # resolving and running at all is the signal we want.
    try:
        proc = subprocess.run(
            [ccx, "-v"], capture_output=True, text=True, timeout=30, check=False
        )
        banner = (proc.stdout + proc.stderr).strip().splitlines()
        print(f"  ok    version        {banner[0] if banner else '(no banner)'}")
    except (OSError, subprocess.SubprocessError) as exc:
        failures.append(f"ccx failed to execute: {exc}")
        print(f"  FAIL  ccx -v         {exc}")


def check_data() -> None:
    print("data")
    # The SSM is the only viable anatomy source, so its absence is the one that
    # actually blocks work -- but it is a `pixi run fetch-ssm` away, not fatal.
    ssm = ROOT / "src" / "data" / "ssm_tibia"
    if ssm.is_dir():
        stls = sum(1 for _ in ssm.rglob("*.stl"))
        print(f"  ok    ssm_tibia      {stls} STLs")
    else:
        warnings.append("ssm_tibia missing -- run: pixi run fetch-ssm")
        print("  WARN  ssm_tibia      missing (pixi run fetch-ssm)")


def main() -> int:
    print(f"python  {sys.version.split()[0]}  ({sys.executable})\n")
    check_imports()
    check_solver()
    check_data()

    print()
    for w in warnings:
        print(f"warning: {w}")
    if failures:
        for f in failures:
            print(f"error: {f}")
        print(f"\n{len(failures)} check(s) failed")
        return 1
    print("toolchain ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
