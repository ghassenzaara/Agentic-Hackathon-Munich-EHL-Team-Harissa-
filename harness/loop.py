"""Loop orchestration. SKELETON -- owner C, expected to be rewritten.

Wired end to end so the shape is agreed, but the per-iteration policy is not
finished. What it does today: build the prompt from the current report, start a
Devin session, wait, run the locked-file guard on whatever Devin committed, then
re-validate and go again.

One decision is deliberately still open (see the plan): whether Devin runs the
validators in its own container, or the harness runs them locally and feeds the
report in. This file assumes the former -- Devin checks its own work, which is
what the challenge brief asks for -- and re-validates locally afterwards as an
independent check. If CadQuery turns out not to install in Devin's container,
invert it: run locally and pass the report through the prompt only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from autoimplants.contracts import Report
from .devin_client import DevinClient
from .guard import check_range

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_TEMPLATE = REPO_ROOT / "prompts" / "fix_iteration.md"

ITERATION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "commit_sha": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string", "description": "why this change addresses the failure"},
        "topology_changed": {
            "type": "boolean",
            "description": "true if you added or removed geometry (rib, slot, contour, "
                           "variable thickness); false if you only changed scalar values",
        },
        "checks_fixed": {"type": "array", "items": {"type": "string"}},
        "validator_exit_code": {"type": "integer"},
        "infeasible": {
            "type": "boolean",
            "description": "true only if you believe no legal design can pass",
        },
    },
    "required": ["commit_sha", "files_changed", "rationale", "topology_changed"],
}


def render_prompt(iteration: int, report: Report, branch: str, history: list[str]) -> str:
    """Token substitution, not str.format -- the report is full of JSON braces."""
    text = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    # Drop the template's own explanatory header.
    text = text.split("---", 2)[-1].lstrip()
    repo_url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()

    for token, value in {
        "{{ITERATION}}": str(iteration),
        "{{REPO_URL}}": repo_url,
        "{{BRANCH}}": branch,
        "{{REPORT}}": report.summary(),
        "{{PARAMS}}": json.dumps(report.params, indent=2),
        "{{HISTORY}}": "\n".join(history) if history else "(nothing yet -- first iteration)",
    }.items():
        text = text.replace(token, value)
    return text


def validate_locally(validators: str = "geometry,stress") -> Report:
    """Independent re-check of whatever is currently committed."""
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = REPO_ROOT / ".venv" / "bin" / "python"
    subprocess.run(
        [str(py), "-m", "autoimplants.run", "--validators", validators],
        cwd=REPO_ROOT, check=False,
    )
    return Report.load(REPO_ROOT / "out" / "report.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="harness.loop", description=__doc__)
    ap.add_argument("--max-iterations", type=int, default=8)
    ap.add_argument("--branch", default="devin/design")
    ap.add_argument("--acu-limit", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="print iteration 1's prompt and exit")
    args = ap.parse_args(argv)

    report = validate_locally()
    history: list[str] = []

    if args.dry_run:
        print(render_prompt(1, report, args.branch, history))
        return 0

    client = DevinClient()

    for iteration in range(1, args.max_iterations + 1):
        if report.passed:
            print(f"\nCONVERGED after {iteration - 1} iteration(s).")
            return 0

        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        prompt = render_prompt(iteration, report, args.branch, history)
        created = client.create_session(
            prompt=prompt,
            title=f"AutoImplants iteration {iteration}",
            tags=["autoimplants", f"iter-{iteration}"],
            structured_output_schema=ITERATION_OUTPUT_SCHEMA,
            max_acu_limit=args.acu_limit,
        )
        print(f"\n=== iteration {iteration} -> {created.get('url')}")

        final = client.wait(created["session_id"])
        out = final.get("structured_output") or {}

        if out.get("infeasible"):
            print("Devin reports the case as infeasible. Stopping -- check the thresholds.")
            print(out.get("rationale", ""))
            return 2

        subprocess.run(["git", "fetch", "origin", args.branch], cwd=REPO_ROOT, check=False)
        remote_ref = f"origin/{args.branch}"
        clean, bad = check_range(base_sha, remote_ref)
        if not clean:
            print("ITERATION INVALID -- locked files modified:")
            for f, reason in bad:
                print(f"  {f}: {reason}")
            return 3

        # Sync the harness's own working tree to the commit Devin actually pushed.
        # Without this, validate_locally() below scores whatever was already
        # checked out locally rather than Devin's real commit -- and the next
        # iteration's base_sha would be wrong too. Force is safe here: this
        # working tree is orchestration state, not somewhere a human edits
        # concurrently.
        subprocess.run(
            ["git", "checkout", "-B", args.branch, remote_ref], cwd=REPO_ROOT, check=True
        )

        history.append(
            f"iter {iteration}: {out.get('rationale', '(no rationale)')} "
            f"[topology_changed={out.get('topology_changed')}]"
        )
        report = validate_locally()

    print(f"\nHit the {args.max_iterations}-iteration cap without converging.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
