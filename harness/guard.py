"""Enforce what Devin is allowed to change.

The obvious way an autonomous design loop "converges" is by cheating: relax a
threshold, weaken a validator, move a locked screw position. This module makes
that mechanically detectable instead of a thing we hope did not happen.

It is an ALLOWLIST, not a denylist. Anything not explicitly editable counts as a
violation, so a file nobody thought about defaults to protected.

The jury question this exists to answer is "how do you know it didn't game the
metric?" -- and the answer is a command they can run, not a promise.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The design surface. Devin edits the generator and its parameters -- that is the
# entire point -- plus whatever artefacts it writes for the record.
EDITABLE_GLOBS = (
    "autoimplants/generator.py",
    "autoimplants/params.py",
    "autoimplants/export.py",
    "runs/*",
    "runs/**",
    "out/*",
    "out/**",
)

# Called out separately only so the violation message can explain *why* a file is
# locked. Anything absent from EDITABLE_GLOBS is locked regardless.
LOCK_REASONS = {
    "inputs/*": "case constraints, anatomy and surgical plan are system-controlled inputs",
    "autoimplants/validators/*": "a design cannot be allowed to rewrite its own examiner",
    "autoimplants/contracts.py": "the report schema is a shared contract with three other lanes",
    "autoimplants/bone.py": "the validator measures the gap through this module; editing it "
                            "would change the measurement rather than the design",
    "harness/*": "the loop must not be able to edit the loop",
}


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def changed_files(base: str, head: str = "HEAD") -> list[str]:
    raw = _git("diff", "--name-only", f"{base}..{head}")
    return [line.strip().replace("\\", "/") for line in raw.splitlines() if line.strip()]


def is_editable(path: str) -> bool:
    return any(fnmatch(path, pattern) for pattern in EDITABLE_GLOBS)


def lock_reason(path: str) -> str:
    for pattern, reason in LOCK_REASONS.items():
        if fnmatch(path, pattern):
            return reason
    return "not on the editable allowlist"


def violations(files: list[str]) -> list[tuple[str, str]]:
    return [(f, lock_reason(f)) for f in files if not is_editable(f)]


def check_range(base: str, head: str = "HEAD") -> tuple[bool, list[tuple[str, str]]]:
    """(clean, violations) for the commits in base..head."""
    bad = violations(changed_files(base, head))
    return (not bad, bad)


def is_ancestor(base: str, head: str) -> bool:
    """Whether ``head`` contains ``base`` in its history."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"git merge-base --is-ancestor {base} {head} failed: {result.stderr.strip()}"
        )
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="harness.guard", description="Verify Devin only touched the design surface."
    )
    ap.add_argument("base", help="commit/ref before Devin's work")
    ap.add_argument("head", nargs="?", default="HEAD")
    args = ap.parse_args(argv)

    files = changed_files(args.base, args.head)
    bad = violations(files)

    print(f"{len(files)} file(s) changed in {args.base}..{args.head}")
    for f in files:
        print(f"  {'LOCKED  ' if not is_editable(f) else 'editable'}  {f}")

    if bad:
        print("\nITERATION INVALID -- locked files were modified:")
        for f, reason in bad:
            print(f"  {f}\n      {reason}")
        return 1

    print("\nOK -- only the design surface was touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
