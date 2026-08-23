"""Local web application and guarded Devin run queue for AutoImplants.

Run with::

    python -m autoimplants.server

The server binds to localhost by default. Credentials never cross the HTTP
boundary: the browser sees readiness and Devin session URLs, never API keys.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from autoimplants import case_io, dicom_to_mesh, import_case, viewer
from autoimplants.contracts import Report
from harness.devin_client import DevinClient, DevinError, load_env
from harness.guard import check_range, changed_files, is_ancestor
from harness.loop import ITERATION_OUTPUT_SCHEMA, render_prompt, validate_locally

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = REPO_ROOT / ".autoimplants-runtime"
WORKTREES_ROOT = REPO_ROOT / ".autoimplants-worktrees"
DEMO_CASE = REPO_ROOT / "inputs" / "case.json"
DEMO_BONE = REPO_ROOT / "inputs" / "bone.stl"
VALIDATORS = "geometry,pending_stress"
ACU_PER_ITERATION = 5
MAX_ITERATIONS = 3
MAX_BONE_BYTES = 25 * 1024 * 1024
MAX_DICOM_BYTES = 1024 * 1024 * 1024
MAX_PLAN_BYTES = 1024 * 1024
ACTIVE_STATES = {
    "preparing",
    "ingesting",
    "validating",
    "devin_running",
    "fetching",
    "guarding",
    "validating_result",
}
RUNNABLE_STATES = {"queued", "revision_queued"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(
    args: list[str], cwd: Path, *, check: bool = True, timeout: int = 120
) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{' '.join(args)} failed: {detail[:1500]}")
    return result.stdout.strip()


async def save_upload(upload: UploadFile, destination: Path, limit: int) -> Path:
    """Stream an upload to disk, refusing it the moment it passes ``limit``.

    Streamed rather than read whole: a CT series is hundreds of megabytes and
    must never be held in memory, and the cap has to bite before the disk fills.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with destination.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                handle.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    413, f"{destination.name} exceeds the {limit // (1024 * 1024)} MB upload limit"
                )
            handle.write(chunk)
    if not size:
        raise HTTPException(422, f"{destination.name} is empty")
    return destination


def extract_series(archive: Path, destination: Path) -> int:
    """Unpack an uploaded DICOM archive flat, ignoring the sender's directory tree.

    Flat on purpose: a series exported from a PACS can arrive nested arbitrarily
    deep, and the reader globs recursively anyway. Member names are reduced to
    their basename, which also removes any ``..`` or absolute path escape.
    """
    destination.mkdir(parents=True, exist_ok=True)
    written = 0
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = Path(info.filename).name
            if info.is_dir() or not name or name.startswith("."):
                continue
            with bundle.open(info) as source, (destination / f"{written:05d}_{name}").open("wb") as target:
                shutil.copyfileobj(source, target)
            written += 1
    if not written:
        raise RuntimeError("The uploaded archive contains no files.")
    return written


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class RunStore:
    """Small durable JSON store; one file is the recovery boundary for one run."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, run_id: str) -> Path:
        return self.root / "runs" / run_id / "run.json"

    def run_dir(self, run_id: str) -> Path:
        return self.path(run_id).parent

    def get(self, run_id: str) -> dict:
        with self._lock:
            path = self.path(run_id)
            if not path.exists():
                raise KeyError(run_id)
            return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        with self._lock:
            records = []
            for path in (self.root / "runs").glob("*/run.json"):
                try:
                    records.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
            return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)

    def put(self, record: dict) -> dict:
        with self._lock:
            record = dict(record)
            record["updated_at"] = utc_now()
            record["version"] = int(record.get("version", 0)) + 1
            atomic_json(self.path(record["run_id"]), record)
            return json.loads(json.dumps(record))

    def update(self, run_id: str, **changes: Any) -> dict:
        with self._lock:
            record = self.get(run_id)
            record.update(changes)
            return self.put(record)

    def mutate(self, run_id: str, fn: Callable[[dict], None]) -> dict:
        with self._lock:
            record = self.get(run_id)
            fn(record)
            return self.put(record)


class RunManager:
    """Serial background worker that owns all paid-session creation."""

    def __init__(
        self,
        repo_root: Path = REPO_ROOT,
        runtime_root: Path = RUNTIME_ROOT,
        worktrees_root: Path = WORKTREES_ROOT,
        client_factory: Callable[[], DevinClient] = DevinClient,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.runtime_root = Path(runtime_root).resolve()
        self.worktrees_root = Path(worktrees_root).resolve()
        self.store = RunStore(self.runtime_root)
        self.client_factory = client_factory
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._condition = threading.Condition()
        self._stop = False
        self._thread: threading.Thread | None = None

    @property
    def demo_case_path(self) -> Path:
        return self.repo_root / "inputs" / "case.json"

    @property
    def demo_bone_path(self) -> Path:
        return self.repo_root / "inputs" / "bone.stl"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        for record in reversed(self.store.list()):
            status = record.get("status")
            if status in RUNNABLE_STATES:
                self.enqueue(record["run_id"], preserve_status=True)
            elif status == "devin_running" and record.get("active_session"):
                self.enqueue(record["run_id"], preserve_status=True)
            elif status in ACTIVE_STATES:
                self.store.update(
                    record["run_id"],
                    status="needs_attention",
                    phase="Server restarted during a local Git or validation step",
                    error="Resume only after checking the recorded worktree and session.",
                )
        self._thread = threading.Thread(target=self._worker, daemon=True, name="autoimplants-worker")
        self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=5)

    def enqueue(self, run_id: str, *, preserve_status: bool = False) -> None:
        with self._condition:
            if run_id in self._queued:
                return
            if not preserve_status:
                record = self.store.get(run_id)
                if record.get("status") == "needs_attention":
                    if record.get("active_session"):
                        self.store.update(
                            run_id,
                            status="devin_running",
                            phase="Rechecking Devin session",
                            error=None,
                        )
                    else:
                        self.store.update(
                            run_id,
                            status="queued",
                            phase="Retrying the interrupted local step",
                            error=None,
                        )
            self._queue.append(run_id)
            self._queued.add(run_id)
            self._refresh_positions()
            self._condition.notify()

    def _refresh_positions(self) -> None:
        for index, run_id in enumerate(self._queue, start=1):
            try:
                self.store.update(run_id, queue_position=index)
            except KeyError:
                pass

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stop:
                    self._condition.wait(timeout=1)
                if self._stop:
                    return
                run_id = self._queue.popleft()
                self._queued.discard(run_id)
                self._refresh_positions()
            try:
                self._process_run(run_id)
            except Exception as exc:
                self.store.update(
                    run_id,
                    status="failed",
                    phase="Run stopped",
                    queue_position=None,
                    error=str(exc),
                )

    def create_run(self, max_iterations: int, intake: Path | None = None) -> dict:
        run_id = uuid.uuid4().hex[:12]
        branch = f"devin/autoimplants-{run_id}"
        base_sha = run_command(["git", "rev-parse", "HEAD"], self.repo_root)
        if intake is None:
            case_id = case_io.load_case(self.demo_case_path).get("case_id", "SYNTH-FEMUR-001")
            phase = "Waiting for the Devin worker"
        else:
            # Read the id straight from the plan: the case bundle does not exist
            # yet, and the UI needs something to label the run with immediately.
            plan = json.loads((intake / "surgical_plan.json").read_text(encoding="utf-8"))
            case_id = str(plan.get("case_id", "IMPORTED-CASE"))
            phase = "Queued for CT intake"
            staged = self.store.run_dir(run_id) / "intake"
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(intake), str(staged))
            intake = staged
        record = {
            "schema_version": 1,
            "run_id": run_id,
            "case_id": case_id,
            "status": "queued",
            "phase": phase,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "version": 0,
            "branch": branch,
            "base_sha": base_sha,
            "worktree": str(self.worktrees_root / run_id),
            "queue_position": None,
            "max_iterations": max_iterations,
            "acu_per_iteration": ACU_PER_ITERATION,
            "max_acu": max_iterations * ACU_PER_ITERATION,
            "revision": 0,
            "cycle_iteration": 0,
            "total_iterations": 0,
            "feedback": "",
            "feedback_pin": None,
            "iterations": [],
            "reviews": [],
            "active_session": None,
            "error": None,
            "intake": str(intake) if intake else None,
            "case_path": None,
            "case_source": "bundled demo case" if intake is None else "uploaded scan and surgical plan",
            "phi_tags": [],
            "intake_report": None,
        }
        self.store.put(record)
        self.enqueue(run_id, preserve_status=True)
        return self.store.get(run_id)

    def preflight(self) -> dict:
        """Read-only proof that a paid run can be created safely."""
        load_env(self.repo_root / ".env")
        key = os.environ.get("DEVIN_API_KEY", "")
        org = os.environ.get("DEVIN_ORG_ID", "")
        remote = ""
        branch = ""
        synced = False
        errors: list[str] = []
        try:
            remote = run_command(["git", "remote", "get-url", "origin"], self.repo_root)
            branch = run_command(["git", "branch", "--show-current"], self.repo_root)
            local = run_command(["git", "rev-parse", "HEAD"], self.repo_root)
            upstream = run_command(["git", "rev-parse", "origin/main"], self.repo_root)
            synced = local == upstream
            if branch != "main":
                errors.append("The application checkout must be on main.")
            if not synced:
                errors.append("Local main must be committed, pushed, and equal to origin/main.")
        except Exception:
            errors.append("Git origin/main readiness check failed.")
        devin_ready = False
        if key and org:
            try:
                self.client_factory().list_sessions(limit=1)
                devin_ready = True
            except Exception:
                errors.append(
                    "Devin permission check failed. Verify the service-user key, "
                    "organization ID, network access, and ManageOrgSessions permission."
                )
        else:
            if not key:
                errors.append("DEVIN_API_KEY is missing")
            if not org:
                errors.append("DEVIN_ORG_ID is missing")
        return {
            "ready": bool(key and org and devin_ready and remote and branch == "main" and synced),
            "credentials": {
                "api_key": f"{key[:4]}…{key[-4:]}" if len(key) >= 10 else ("set" if key else "missing"),
                "org_id": f"{org[:4]}…{org[-4:]}" if len(org) >= 10 else ("set" if org else "missing"),
            },
            "devin_permission": devin_ready,
            "git": {"remote": remote, "branch": branch, "main_synced": synced},
            "errors": errors,
        }

    def _prepare_worktree(self, record: dict) -> Path:
        worktree = Path(record["worktree"])
        if worktree.exists() and (worktree / ".git").exists():
            return worktree
        worktree.parent.mkdir(parents=True, exist_ok=True)
        branch = record["branch"]
        exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.repo_root,
            check=False,
        ).returncode == 0
        command = ["git", "worktree", "add"]
        if not exists:
            command.extend(["-b", branch])
        command.extend([str(worktree), branch if exists else record["base_sha"]])
        run_command(command, self.repo_root, timeout=180)
        run_command(
            ["git", "push", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}"],
            worktree,
            timeout=180,
        )
        return worktree

    def _ingest(self, record: dict, worktree: Path) -> dict:
        """Uploaded scan plus plan to a validated case bundle inside the worktree.

        The bundle is committed on the run's own branch so the Devin sandbox
        validates the *patient's* case rather than the repo's demo one. Only
        derived artefacts travel: the mesh, the placed plan and the transform.
        The DICOM never leaves this machine.
        """
        run_id = record["run_id"]
        intake = Path(record["intake"])
        plan_path = intake / "surgical_plan.json"
        bone_path = intake / "bone.stl"
        series = intake / "series"

        phi: list[str] = []
        if series.is_dir():
            self.store.update(run_id, status="ingesting", phase="Reading the uploaded DICOM series")
            phi = sorted(dicom_to_mesh.scan_for_phi(series))
            self.store.update(
                run_id,
                phi_tags=phi,
                phase="Segmenting bone from the CT volume",
            )
            dicom_to_mesh.dicom_to_mesh(series, bone_path, bone="femur")

        self.store.update(run_id, status="ingesting", phase="Importing the surgical plan")
        case_id = str(record["case_id"])
        out_dir = worktree / "real_cases" / case_id / "generated"
        report, case_path = import_case.import_case(plan_path, bone_path, out_dir=out_dir)
        durable = self.store.run_dir(run_id) / "case"
        atomic_json(durable / "import_report.json", report.to_dict())
        if case_path is None:
            failures = [check.id for check in report.checks if check.status != "PASS"]
            raise RuntimeError(
                "The uploaded scan and plan did not pass intake: " + ", ".join(failures)
            )

        relative = case_path.relative_to(worktree).as_posix()
        run_command(["git", "add", "--force", str(out_dir.relative_to(worktree))], worktree)
        run_command(
            [
                "git",
                "commit",
                "-m",
                f"Add imported case bundle for {case_id}\n\n"
                "Derived mesh plus placed surgical plan for this run only. "
                "No DICOM and no identifying tags are included.",
            ],
            worktree,
        )
        run_command(
            ["git", "push", "origin", f"HEAD:refs/heads/{record['branch']}"], worktree, timeout=180
        )
        return self.store.update(
            run_id,
            case_path=relative,
            intake_report=report.to_dict(),
            phi_tags=phi,
            phase="Case bundle imported",
        )

    def _case_path(self, record: dict, worktree: Path) -> Path:
        relative = record.get("case_path") or "inputs/case.json"
        return worktree / relative

    def _validate(self, worktree: Path, record: dict, iteration: int) -> tuple[Report, Path]:
        out_dir = self.store.run_dir(record["run_id"]) / "working"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        report = validate_locally(
            VALIDATORS,
            repo_root=worktree,
            out_dir=out_dir,
            iteration=iteration,
            case_path=self._case_path(record, worktree),
        )
        return report, out_dir

    def _snapshot(
        self,
        record: dict,
        report: Report,
        out_dir: Path,
        *,
        commit_sha: str = "",
        rationale: str = "",
        session_url: str = "",
        topology_changed: bool = False,
        changed: list[str] | None = None,
    ) -> dict:
        number = int(report.iteration)
        label = "Baseline" if number == 0 else f"Iteration {number}"
        target = self.store.run_dir(record["run_id"]) / "iterations" / f"{number:03d}"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("report.json", "implant.stl", "implant.step"):
            source = out_dir / name
            if source.exists():
                shutil.copy2(source, target / name)
        artifacts = {}
        for name in ("report.json", "implant.stl", "implant.step"):
            path = target / name
            if path.exists():
                artifacts[name] = {
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
        audit = {
            "run_id": record["run_id"],
            "revision": record.get("revision", 0),
            "iteration": number,
            "commit_sha": commit_sha,
            "rationale": rationale,
            "session_url": session_url,
            "topology_changed": bool(topology_changed),
            "changed_files": changed or [],
            "artifacts": artifacts,
            "validated_at": utc_now(),
            "validators": VALIDATORS.split(","),
        }
        atomic_json(target / "audit.json", audit)
        prefix = f"/api/runs/{record['run_id']}/iterations/{number}"
        iteration = viewer.iteration_payload(
            report,
            {
                "label": label,
                "rationale": rationale or (
                    "Baseline design — no autonomous geometry edit has been committed yet."
                ),
                "commit_sha": commit_sha,
                "session_url": session_url,
                "topology_changed": bool(topology_changed),
                "revision": record.get("revision", 0),
            },
            {
                "report": f"{prefix}/artifacts/report.json",
                "stl": f"{prefix}/artifacts/implant.stl",
                "step": f"{prefix}/artifacts/implant.step",
                "audit": f"{prefix}/artifacts/audit.json",
                "mesh": f"{prefix}/mesh",
            },
        )
        iteration["artifact_hashes"] = artifacts
        return iteration

    def _process_run(self, run_id: str) -> None:
        record = self.store.get(run_id)
        worktree = Path(record["worktree"])
        active = record.get("active_session")
        if active:
            if not worktree.exists():
                self.store.update(
                    run_id,
                    status="needs_attention",
                    phase="Recorded worktree is missing",
                    error="The existing Devin session was not replaced. Restore the worktree first.",
                )
                return
            self._resume_active(record, worktree)
            return

        self.store.update(run_id, status="preparing", phase="Creating isolated design branch", queue_position=None)
        worktree = self._prepare_worktree(record)
        record = self.store.get(run_id)

        if record.get("intake") and not record.get("case_path"):
            record = self._ingest(record, worktree)

        if not record["iterations"]:
            self.store.update(run_id, status="validating", phase="Validating the baseline geometry")
            report, out_dir = self._validate(worktree, record, 0)
            baseline = self._snapshot(record, report, out_dir)
            record = self.store.mutate(run_id, lambda item: item["iterations"].append(baseline))
            if baseline["geometry_converged"]:
                self.store.update(run_id, status="awaiting_review", phase="Baseline geometry converged")
                return

        while int(record["cycle_iteration"]) < int(record["max_iterations"]):
            self._start_iteration(record, worktree)
            record = self.store.get(run_id)
            if record["status"] in {
                "awaiting_review", "needs_attention", "invalid", "failed"
            }:
                return

        self.store.update(
            run_id,
            status="capped",
            phase=f"Iteration cap reached ({record['max_iterations']})",
            queue_position=None,
        )

    def _start_iteration(self, record: dict, worktree: Path) -> None:
        run_id = record["run_id"]
        number = int(record["total_iterations"]) + 1
        base_sha = run_command(["git", "rev-parse", "HEAD"], worktree)
        previous = Report.from_dict(record["iterations"][-1]["report"])
        history = [
            f"iter {item['number']}: {item.get('rationale', '')}"
            for item in record["iterations"]
            if item["number"]
        ]
        feedback = record.get("feedback", "")
        if record.get("feedback_pin"):
            feedback += "\nPinned bone coordinate (mm): " + json.dumps(record["feedback_pin"])
        prompt = render_prompt(
            number,
            previous,
            record["branch"],
            history,
            repo_root=worktree,
            feedback=feedback,
        )
        case_flag = ""
        if record.get("case_path"):
            case_flag = f" --case {record['case_path']}"
        prompt += (
            "\n\nFor this live workflow the authoritative validation command is:\n"
            f"`.venv/bin/python -m autoimplants.run --validators geometry,pending_stress{case_flag}`\n"
            "The eight pending stress checks must remain SKIP; do not edit them."
        )
        if case_flag:
            prompt += (
                "\nThis run designs against an imported patient case committed on the "
                "branch, not the demo case. Its bundle is a locked input: derive the "
                "geometry from it, never edit it."
            )
        repo_url = run_command(["git", "remote", "get-url", "origin"], worktree)
        client = self.client_factory()
        created = client.create_session(
            prompt=prompt,
            title=f"AutoImplants {run_id} · iteration {number}",
            tags=["autoimplants", run_id, f"revision-{record['revision']}", f"iter-{number}"],
            structured_output_schema=ITERATION_OUTPUT_SCHEMA,
            max_acu_limit=ACU_PER_ITERATION,
            repos=[repo_url],
        )
        session = {
            "session_id": created["session_id"],
            "url": created.get("url", ""),
            "base_sha": base_sha,
            "iteration": number,
            "revision": record["revision"],
            "status": created.get("status", "new"),
            "status_detail": created.get("status_detail"),
            "structured_output": None,
        }
        self.store.update(
            run_id,
            status="devin_running",
            phase="Devin is engineering the next geometry",
            active_session=session,
            error=None,
        )
        self._wait_and_integrate(self.store.get(run_id), worktree, client)

    def _resume_active(self, record: dict, worktree: Path) -> None:
        client = self.client_factory()
        active = record["active_session"]
        if active.get("structured_output"):
            self._integrate_result(record, worktree, active["structured_output"])
            return
        self.store.update(record["run_id"], status="devin_running", phase="Reconnected to Devin session")
        self._wait_and_integrate(self.store.get(record["run_id"]), worktree, client)

    def _wait_and_integrate(self, record: dict, worktree: Path, client: DevinClient) -> None:
        run_id = record["run_id"]
        session_id = record["active_session"]["session_id"]

        def on_poll(payload: dict) -> None:
            def update(item: dict) -> None:
                active = item.get("active_session") or {}
                active["status"] = payload.get("status")
                active["status_detail"] = payload.get("status_detail")
                item["active_session"] = active
                item["phase"] = f"Devin · {client.status_label(payload)}"
            self.store.mutate(run_id, update)

        final = client.wait(session_id, on_poll=on_poll)
        output = final.get("structured_output") or {}
        if final.get("_timed_out") or not output:
            self.store.update(
                run_id,
                status="needs_attention",
                phase=f"Devin stopped at {client.status_label(final)}",
                error="Open the recorded Devin session, resolve it, then press Resume.",
            )
            return

        def record_output(item: dict) -> None:
            item["active_session"]["structured_output"] = output
        record = self.store.mutate(run_id, record_output)
        self._integrate_result(record, worktree, output)

    def _integrate_result(self, record: dict, worktree: Path, output: dict) -> None:
        run_id = record["run_id"]
        active = record["active_session"]
        base_sha = active["base_sha"]
        branch = record["branch"]
        number = int(active["iteration"])
        if output.get("infeasible"):
            self.store.update(
                run_id,
                status="failed",
                phase="Devin reported the case infeasible",
                error=output.get("rationale") or "No rationale returned.",
            )
            return

        self.store.update(run_id, status="fetching", phase="Fetching Devin's committed geometry")
        run_command(["git", "fetch", "origin", branch], worktree, timeout=180)
        remote = f"origin/{branch}"
        remote_sha = run_command(["git", "rev-parse", remote], worktree)
        reported_sha = str(output.get("commit_sha", "")).strip()
        if not reported_sha or run_command(["git", "rev-parse", reported_sha], worktree) != remote_sha:
            self.store.update(
                run_id,
                status="invalid",
                phase="Iteration rejected",
                error="Devin's reported commit does not match the pushed branch head.",
            )
            return

        self.store.update(run_id, status="guarding", phase="Checking locked engineering inputs")
        if not is_ancestor(base_sha, remote, repo_root=worktree):
            self.store.update(
                run_id,
                status="invalid",
                phase="Iteration rejected",
                error="The pushed branch does not descend from the validated iteration base.",
            )
            return
        clean, violations = check_range(base_sha, remote, repo_root=worktree)
        if not clean:
            message = "; ".join(f"{path}: {reason}" for path, reason in violations)
            self.store.update(run_id, status="invalid", phase="Locked files were modified", error=message)
            return

        run_command(["git", "merge", "--ff-only", remote], worktree)
        rationale = run_command(["git", "show", "-s", "--format=%B", remote_sha], worktree).strip()
        files = changed_files(base_sha, remote_sha, repo_root=worktree)
        self.store.update(run_id, status="validating_result", phase="Independently validating the commit")
        report, out_dir = self._validate(worktree, record, number)
        iteration = self._snapshot(
            record,
            report,
            out_dir,
            commit_sha=remote_sha,
            rationale=rationale,
            session_url=active.get("url", ""),
            topology_changed=bool(output.get("topology_changed")),
            changed=files,
        )

        def finish(item: dict) -> None:
            item["iterations"].append(iteration)
            item["total_iterations"] = number
            item["cycle_iteration"] = int(item["cycle_iteration"]) + 1
            item["active_session"] = None
            item["error"] = None
            if iteration["geometry_converged"]:
                item["status"] = "awaiting_review"
                item["phase"] = "Geometry converged · awaiting surgeon review"
            else:
                item["status"] = "validating"
                item["phase"] = "Validated iteration still has geometry failures"
        self.store.mutate(run_id, finish)

    def review(self, run_id: str, request: "ReviewRequest") -> dict:
        record = self.store.get(run_id)
        if record.get("status") != "awaiting_review":
            raise ValueError("Only a converged geometry awaiting review can be decided.")
        selected = record["iterations"][-1]
        if request.iteration != selected["number"]:
            raise ValueError("The decision must target the latest validated iteration.")
        if request.decision == "approved_prototype" and not request.stress_ack:
            raise ValueError("Approval requires acknowledging the skipped stress checks.")
        if request.decision == "revision_requested":
            if not request.feedback.strip():
                raise ValueError("Revision feedback is required.")
            if not request.cost_ack:
                raise ValueError("A fresh maximum 15-ACU revision must be confirmed.")

        review = {
            "decision": request.decision,
            "reviewer": request.reviewer.strip(),
            "iteration": request.iteration,
            "commit_sha": selected.get("commit_sha", ""),
            "artifact_hashes": selected.get("artifact_hashes", {}),
            "stress_ack": bool(request.stress_ack),
            "feedback": request.feedback.strip(),
            "pin": request.pin,
            "timestamp": utc_now(),
        }

        def apply(item: dict) -> None:
            item["reviews"].append(review)
            if request.decision == "approved_prototype":
                item["status"] = "approved"
                item["phase"] = "Prototype approved"
            else:
                item["revision"] = int(item["revision"]) + 1
                item["cycle_iteration"] = 0
                item["feedback"] = request.feedback.strip()
                item["feedback_pin"] = request.pin
                item["status"] = "revision_queued"
                item["phase"] = "Surgeon revision queued"
                item["queue_position"] = None
        result = self.store.mutate(run_id, apply)
        if request.decision == "revision_requested":
            self.enqueue(run_id, preserve_status=True)
            result = self.store.get(run_id)
        return result


class ReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approved_prototype|revision_requested)$")
    reviewer: str = Field(min_length=1, max_length=160)
    iteration: int = Field(ge=0)
    stress_ack: bool = False
    feedback: str = Field(default="", max_length=4000)
    pin: list[float] | None = None
    cost_ack: bool = False


def public_record(record: dict) -> dict:
    """Run JSON is already non-secret; normalize missing queue fields for clients."""
    result = json.loads(json.dumps(record))
    result.setdefault("queue_position", None)
    return result


def create_app(
    repo_root: Path = REPO_ROOT,
    runtime_root: Path = RUNTIME_ROOT,
    worktrees_root: Path = WORKTREES_ROOT,
    manager: RunManager | None = None,
) -> FastAPI:
    repo_root = Path(repo_root).resolve()
    manager = manager or RunManager(repo_root, runtime_root, worktrees_root)
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        manager.start()
        try:
            yield
        finally:
            manager.stop()

    app = FastAPI(title="AutoImplants", version="1.0", lifespan=lifespan)
    app.state.manager = manager

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        case_path = repo_root / "inputs" / "case.json"
        case = case_io.set_active_case(case_io.load_case(case_path), case_path)
        report_path = repo_root / "out" / "report.json"
        implant_path = repo_root / "out" / "implant.stl"
        report = Report.load(report_path) if report_path.exists() else None
        html = viewer.build_page(
            case,
            implant_path if implant_path.exists() else None,
            report,
            title="AutoImplants · Live Devin workflow",
            server_mode=True,
        )
        return HTMLResponse(html)

    @app.get("/api/preflight")
    def preflight() -> dict:
        return manager.preflight()

    @app.get("/api/demo-bone")
    def demo_bone() -> FileResponse:
        return FileResponse(manager.demo_bone_path, filename="bone.stl", media_type="model/stl")

    @app.post("/api/runs", status_code=202)
    async def create_run_endpoint(
        bone: UploadFile | None = File(default=None),
        dicom: UploadFile | None = File(default=None),
        plan: UploadFile | None = File(default=None),
        max_iterations: int = Form(default=MAX_ITERATIONS),
        acu_per_iteration: int = Form(default=ACU_PER_ITERATION),
        cost_ack: bool = Form(default=False),
    ) -> dict:
        if not 1 <= max_iterations <= MAX_ITERATIONS:
            raise HTTPException(422, "max_iterations must be between 1 and 3")
        if acu_per_iteration != ACU_PER_ITERATION:
            raise HTTPException(422, "acu_per_iteration is locked to 5")
        if not cost_ack:
            raise HTTPException(422, "Confirm the maximum ACU cost before starting")
        readiness = manager.preflight()
        if not readiness["ready"]:
            raise HTTPException(503, " · ".join(readiness["errors"]) or "Live workflow is not ready")
        if bool(bone and bone.filename) == bool(dicom and dicom.filename):
            raise HTTPException(422, "Upload exactly one anatomy source: a bone STL or a zipped DICOM series")

        # A surgical plan is what makes a case a case: screw entries, directions,
        # keepouts and the landmarks the frame is recovered from. Imaging alone
        # cannot supply them, so anything other than the bundled demo femur has
        # to arrive with one.
        staged: Path | None = None
        if plan and plan.filename:
            staged = manager.runtime_root / "uploads" / uuid.uuid4().hex
            staged.mkdir(parents=True, exist_ok=True)
            try:
                await save_upload(plan, staged / "surgical_plan.json", MAX_PLAN_BYTES)
                json.loads((staged / "surgical_plan.json").read_text(encoding="utf-8"))
                if dicom and dicom.filename:
                    archive = staged / "series.zip"
                    await save_upload(dicom, archive, MAX_DICOM_BYTES)
                    extract_series(archive, staged / "series")
                    archive.unlink()
                elif bone is not None:
                    await save_upload(bone, staged / "bone.stl", MAX_BONE_BYTES)
            except HTTPException:
                shutil.rmtree(staged, ignore_errors=True)
                raise
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                shutil.rmtree(staged, ignore_errors=True)
                raise HTTPException(422, f"Intake rejected: {exc}") from exc
        elif dicom and dicom.filename:
            raise HTTPException(422, "A CT series needs a surgical plan JSON alongside it")
        elif bone is not None:
            digest = hashlib.sha256()
            size = 0
            while chunk := await bone.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BONE_BYTES:
                    raise HTTPException(413, "The STL exceeds the 25 MB upload limit")
                digest.update(chunk)
            if digest.hexdigest() != sha256_file(manager.demo_bone_path):
                raise HTTPException(
                    422,
                    "Without a surgical plan only the bundled demo femur can be designed. "
                    "Other anatomy requires a bone mesh or CT series plus a surgical-plan JSON.",
                )
        try:
            return public_record(manager.create_run(max_iterations, intake=staged))
        except Exception as exc:
            if staged is not None:
                shutil.rmtree(staged, ignore_errors=True)
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/runs")
    def list_runs() -> list[dict]:
        return [public_record(record) for record in manager.store.list()]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            return public_record(manager.store.get(run_id))
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str) -> StreamingResponse:
        try:
            manager.store.get(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc

        async def stream():
            seen = -1
            while True:
                try:
                    record = manager.store.get(run_id)
                except KeyError:
                    return
                version = int(record.get("version", 0))
                if version != seen:
                    seen = version
                    yield "event: snapshot\ndata: " + json.dumps(public_record(record)) + "\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/runs/{run_id}/iterations/{iteration}/mesh")
    def iteration_mesh(run_id: str, iteration: int) -> JSONResponse:
        try:
            record = manager.store.get(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc
        selected = next((item for item in record["iterations"] if item["number"] == iteration), None)
        if selected is None:
            raise HTTPException(404, "Iteration not found")
        target = manager.store.run_dir(run_id) / "iterations" / f"{iteration:03d}"
        implant = target / "implant.stl"
        worktree = Path(record["worktree"])
        case_path = worktree / "inputs" / "case.json"
        if not case_path.exists():
            case_path = manager.demo_case_path
        case = case_io.set_active_case(case_io.load_case(case_path), case_path)
        meshes = [
            viewer._mesh_payload(case_io.bone_path(case), "bone", viewer.BONE_COLOR, viewer.BONE_FACE_BUDGET)
        ]
        if implant.exists():
            meshes.append(
                viewer._mesh_payload(implant, "implant", viewer.IMPLANT_COLOR, viewer.IMPLANT_FACE_BUDGET)
            )
        return JSONResponse({"meshes": meshes})

    @app.get("/api/runs/{run_id}/iterations/{iteration}/artifacts/{name}")
    def artifact(run_id: str, iteration: int, name: str) -> FileResponse:
        if name not in {"report.json", "implant.stl", "implant.step", "audit.json"}:
            raise HTTPException(404, "Artifact not found")
        try:
            manager.store.get(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc
        path = manager.store.run_dir(run_id) / "iterations" / f"{iteration:03d}" / name
        if not path.exists():
            raise HTTPException(404, "Artifact not found")
        return FileResponse(path, filename=name)

    @app.post("/api/runs/{run_id}/reviews")
    def review(run_id: str, request: ReviewRequest) -> dict:
        if request.pin is not None and len(request.pin) != 3:
            raise HTTPException(422, "pin must be an [x,y,z] coordinate")
        try:
            return public_record(manager.review(run_id, request))
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str) -> dict:
        try:
            record = manager.store.get(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc
        if record.get("status") != "needs_attention":
            raise HTTPException(409, "Only a run needing attention can be resumed")
        manager.enqueue(run_id)
        return public_record(manager.store.get(run_id))

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autoimplants.server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    import uvicorn

    uvicorn.run("autoimplants.server:app", host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
