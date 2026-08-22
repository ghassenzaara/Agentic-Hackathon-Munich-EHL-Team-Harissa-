"""Run the full suite across native-library-safe process boundaries.

On Windows, importing SimpleITK and OpenCASCADE into one interpreter can cause
an access violation during interpreter shutdown even after every assertion has
passed. The application already treats imaging and CAD as separate CLI stages;
this runner mirrors that boundary and combines their pytest exit codes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS = REPO_ROOT / "tests"
IMAGING_MODULES = {
    "test_ct_pipeline.py",
    "test_dicom_series_roundtrip.py",
    "test_dicom_to_mesh.py",
}


def _run(label: str, paths: list[Path]) -> int:
    print(f"\n=== {label} ({len(paths)} modules) ===", flush=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(REPO_ROOT / ".test-tmp" / label),
        *(str(path) for path in paths),
    ]
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def main() -> int:
    modules = sorted(TESTS.glob("test_*.py"))
    imaging = [path for path in modules if path.name in IMAGING_MODULES]
    core = [path for path in modules if path.name not in IMAGING_MODULES]

    core_exit = _run("core", core)
    imaging_exit = _run("imaging", imaging)

    if core_exit == 0 and imaging_exit == 0:
        print("\nAll test groups passed.")
        return 0

    print(f"\nTest failure: core={core_exit}, imaging={imaging_exit}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
