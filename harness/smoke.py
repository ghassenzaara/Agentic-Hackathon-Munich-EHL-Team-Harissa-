"""Prove the whole Devin path works, end to end, in one command.

This is the H+1 no-go check. It does the smallest thing that exercises every
unknown at once: Devin clones the repo, installs the CadQuery toolchain, runs the
validator, commits a file, and pushes. If this passes, the remaining work is
prompt quality. If it fails, nothing else in the project matters yet.

    python -m harness.smoke --dry-run     # print the prompt, no API call, no key needed
    python -m harness.smoke               # for real
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from .devin_client import DEFAULT_ACU_LIMIT, DevinClient, DevinError

REPO_ROOT = Path(__file__).resolve().parent.parent

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "setup_ok": {"type": "boolean", "description": "did setup.sh complete successfully"},
        "python_version": {"type": "string"},
        "cadquery_ok": {"type": "boolean", "description": "did 'import cadquery' succeed"},
        "validator_exit_code": {"type": "integer"},
        "failing_checks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "ids of checks reported as FAIL",
        },
        "branch": {"type": "string"},
        "commit_sha": {"type": "string", "description": "full SHA of the commit you pushed"},
        "pushed": {"type": "boolean"},
        "notes": {"type": "string", "description": "anything that went wrong or surprised you"},
    },
    "required": [
        "setup_ok",
        "cadquery_ok",
        "validator_exit_code",
        "branch",
        "commit_sha",
        "pushed",
    ],
}


def repo_url() -> str:
    out = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def build_prompt(branch: str) -> str:
    return f"""You are doing an infrastructure smoke test on the AutoImplants repository.
Do exactly these steps and nothing more. Do not redesign anything.

Repository: {repo_url()}
Base branch: main

1. Clone the repo and check out a new branch named `{branch}`.

2. Set up the environment by running `bash setup.sh`. Read it first. It pins
   Python 3.12 on purpose: CadQuery has no wheels for Python 3.13 or 3.14, so if
   you use a newer interpreter the install will fail. If `uv` is unavailable, fall
   back to any Python 3.12 with `python3.12 -m venv .venv` and
   `.venv/bin/pip install -r requirements.txt`.

3. Confirm the toolchain works:
   `.venv/bin/python -c "import cadquery, trimesh; print(cadquery.__version__)"`

4. Run the validator on the baseline design:
   `.venv/bin/python -m autoimplants.run --validators geometry,stress`
   Record the exit code. It is EXPECTED to be 1: the baseline is a generic flat
   plate and it legitimately fails the bone-conformance check on this patient's
   femur. A non-zero exit here is success for this smoke test, not a problem to
   fix. Do NOT attempt to fix the design.

5. Write a file `runs/smoke.md` containing: the Python version you used, the
   CadQuery version, the validator exit code, and the full validator output.

6. Commit ONLY `runs/smoke.md` with the message:
   "Smoke test: toolchain verified, baseline validator runs"
   Then push the branch to origin.

7. Report the structured output, including the full commit SHA you pushed.

Constraints:
- Do not modify any file other than `runs/smoke.md`.
- Do not touch anything in `inputs/`, `autoimplants/validators/`, or `harness/`.
- Do not open a pull request.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="harness.smoke", description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the prompt and exit")
    ap.add_argument(
        "--acu-limit",
        type=int,
        default=DEFAULT_ACU_LIMIT,
        help=f"cost guardrail (default {DEFAULT_ACU_LIMIT})",
    )
    ap.add_argument("--timeout", type=int, default=2400, help="seconds to wait (default 40 min)")
    ap.add_argument("--no-wait", action="store_true", help="create the session and return")
    args = ap.parse_args(argv)

    branch = f"devin/smoke-{time.strftime('%H%M%S')}"
    prompt = build_prompt(branch)

    if args.dry_run:
        print(prompt)
        print("--- structured_output_schema ---")
        print(json.dumps(STRUCTURED_OUTPUT_SCHEMA, indent=2))
        return 0

    try:
        client = DevinClient()
    except DevinError as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        return 2

    created = client.create_session(
        prompt=prompt,
        title="AutoImplants smoke test",
        tags=["autoimplants", "smoke"],
        structured_output_schema=STRUCTURED_OUTPUT_SCHEMA,
        max_acu_limit=args.acu_limit,
    )
    session_id = created["session_id"]
    print(f"session   {session_id}")
    print(f"watch     {created.get('url')}")
    print(f"branch    {branch}")

    if args.no_wait:
        return 0

    seen: list[str] = []

    def on_poll(payload: dict) -> None:
        status = client.status_label(payload)
        if status not in seen:
            seen.append(status)
            print(f"[{time.strftime('%H:%M:%S')}] status -> {status}")

    final = client.wait(session_id, timeout_s=args.timeout, on_poll=on_poll)

    if final.get("_timed_out"):
        print(f"\n[warn] still {client.status_label(final)} after {args.timeout}s -- not a failure, "
              f"check the session URL")
        return 1

    out = final.get("structured_output") or {}
    print("\n--- structured output ---")
    print(json.dumps(out, indent=2))

    ok = bool(out.get("pushed")) and bool(out.get("cadquery_ok"))
    print(f"\nSMOKE TEST {'PASSED' if ok else 'INCOMPLETE'}")
    if ok:
        print(f"commit {out.get('commit_sha')} on {out.get('branch')}")
        print("Devin can clone, install CadQuery, run our validator, commit and push.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
