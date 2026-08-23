"""Autonomous loop orchestration for the AutoImplants design surface.

Each iteration validates locally, asks Devin to address the structured failures,
requires a committed structured result, applies the locked-file guard to the
pushed branch, and independently re-validates that exact commit. Iteration and
ACU caps bound both runtime and cost.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from autoimplants.contracts import Report
from .devin_client import DEFAULT_ACU_LIMIT, DevinClient
from .guard import check_range, is_ancestor

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_TEMPLATE = REPO_ROOT / "prompts" / "fix_iteration.md"
PATCH_PROMPT_TEMPLATE = REPO_ROOT / "prompts" / "patch_iteration.md"

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

# Posted-file mode: nothing is committed, so the result is the submission itself.
# `files` is a fallback for a sandbox that could not reach the job endpoint at all;
# normally the design has already arrived over HTTP by the time this is read.
PATCH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "submitted": {
            "type": "boolean",
            "description": "true if you POSTed at least one design to the job endpoint",
        },
        "files": {
            "type": "object",
            "description": "path -> complete new file contents; send this only if the "
                           "job endpoint was unreachable, otherwise leave it out",
        },
        "rationale": {"type": "string", "description": "why this change addresses the failure"},
        "topology_changed": {
            "type": "boolean",
            "description": "true if you added or removed geometry (rib, slot, contour, "
                           "variable thickness); false if you only changed scalar values",
        },
        "checks_fixed": {"type": "array", "items": {"type": "string"}},
        "infeasible": {
            "type": "boolean",
            "description": "true only if you believe no legal design can pass",
        },
    },
    "required": ["rationale", "topology_changed"],
}


def render_prompt(
    iteration: int,
    report: Report,
    branch: str,
    history: list[str],
    repo_root: str | Path = REPO_ROOT,
    feedback: str = "",
) -> str:
    """Token substitution, not str.format -- the report is full of JSON braces."""
    text = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    # Drop the template's own explanatory header.
    text = text.split("---", 2)[-1].lstrip()
    repo_url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=Path(repo_root), capture_output=True, text=True, check=True,
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
    if feedback:
        text += (
            "\n\n## Surgeon revision requirement\n\n"
            "Treat this as an immutable additional requirement; do not change locked inputs.\n\n"
            f"{feedback.strip()}\n"
        )
    return text


def render_patch_prompt(
    iteration: int,
    report: Report,
    history: list[str],
    job_url: str,
    sources: list[str],
    case_id: str,
    feedback: str = "",
) -> str:
    """The repo-resident prompt for the integrated (no-Git) design agent."""
    text = PATCH_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    text = text.split("---", 2)[-1].lstrip()
    for token, value in {
        "{{ITERATION}}": str(iteration),
        "{{REPORT}}": report.summary(),
        "{{PARAMS}}": json.dumps(report.params, indent=2),
        "{{HISTORY}}": "\n".join(history) if history else "(nothing yet -- first iteration)",
        "{{JOB_URL}}": job_url,
        "{{SOURCES}}": "\n".join(f"- `{path}`" for path in sources),
        "{{CASE}}": case_id,
    }.items():
        text = text.replace(token, value)
    if feedback:
        text += (
            "\n\n## Surgeon revision requirement\n\n"
            "Treat this as an immutable additional requirement; do not change locked inputs.\n\n"
            f"{feedback.strip()}\n"
        )
    return text


def python_executable(repo_root: str | Path = REPO_ROOT) -> Path:
    root = Path(repo_root)
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        REPO_ROOT / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        REPO_ROOT / ".venv" / "bin" / "python",
    )
    return next((path for path in candidates if path.exists()), Path(sys.executable))


def validate_locally(
    validators: str = "geometry,fea",
    repo_root: str | Path = REPO_ROOT,
    out_dir: str | Path = "out",
    iteration: int = 0,
    case_path: str | Path | None = None,
) -> Report:
    """Independent re-check of whatever is currently committed."""
    root = Path(repo_root)
    py = python_executable(root)
    out = Path(out_dir)
    if not out.is_absolute():
        out = root / out
    command = [
        str(py), "-m", "autoimplants.run", "--validators", validators,
        "--iteration", str(iteration), "--out", str(out),
    ]
    if case_path is not None:
        command.extend(["--case", str(case_path)])
    subprocess.run(
        command, cwd=root, check=False,
    )
    return Report.load(out / "report.json")


def _iteration_budget(default: int = 8) -> int:
    """The cap lives in inputs/case.json so there is one number, not two."""
    case_path = REPO_ROOT / "inputs" / "case.json"
    if not case_path.exists():
        return default
    try:
        return int(json.loads(case_path.read_text(encoding="utf-8"))["iteration_budget"])
    except (ValueError, KeyError, TypeError):
        return default


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="harness.loop", description=__doc__)
    ap.add_argument("--max-iterations", type=int, default=_iteration_budget())
    ap.add_argument("--branch", default="devin/design")
    ap.add_argument("--acu-limit", type=int, default=DEFAULT_ACU_LIMIT)
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
        if final.get("_timed_out"):
            print(f"Devin session timed out while {client.status_label(final)}; stopping safely.")
            return 4
        if not (final.get("structured_output") or {}):
            print(
                f"Devin stopped at {client.status_label(final)} without structured output; "
                "open the session URL before resuming."
            )
            return 4
        out = final.get("structured_output") or {}

        if out.get("infeasible"):
            print("Devin reports the case as infeasible. Stopping -- check the thresholds.")
            print(out.get("rationale", ""))
            return 2

        fetched = subprocess.run(
            ["git", "fetch", "origin", args.branch], cwd=REPO_ROOT, check=False
        )
        if fetched.returncode != 0:
            print(f"Could not fetch origin/{args.branch}; stopping before validation.")
            return 4
        remote_ref = f"origin/{args.branch}"
        if not is_ancestor(base_sha, remote_ref):
            print(
                f"ITERATION INVALID -- {remote_ref} does not descend from the "
                f"iteration base {base_sha}."
            )
            return 3
        clean, bad = check_range(base_sha, remote_ref)
        if not clean:
            print("ITERATION INVALID -- locked files modified:")
            for f, reason in bad:
                print(f"  {f}: {reason}")
            return 3

        # Sync to the exact commit Devin pushed; the ancestry and allowlist
        # checks above must both pass before the working branch can move.
        switched = subprocess.run(
            ["git", "checkout", "-B", args.branch, remote_ref], cwd=REPO_ROOT, check=False
        )
        if switched.returncode != 0:
            print(
                "Could not switch to Devin's branch. Commit or stash local changes, "
                "then rerun the iteration."
            )
            return 4

        history.append(
            f"iter {iteration}: {out.get('rationale', '(no rationale)')} "
            f"[topology_changed={out.get('topology_changed')}]"
        )
        report = validate_locally()

    print(f"\nHit the {args.max_iterations}-iteration cap without converging.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
