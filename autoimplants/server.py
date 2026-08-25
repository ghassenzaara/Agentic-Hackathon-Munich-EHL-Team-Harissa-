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
import threading
import time
import uuid
import zipfile
from urllib.parse import urlparse
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from autoimplants import case_io, dicom_to_mesh, import_case, surgical_plan, viewer
from autoimplants.contracts import Report
from harness.devin_client import DevinClient, DevinError, load_env
from harness.guard import EDITABLE_GLOBS, violations
from harness.loop import PATCH_OUTPUT_SCHEMA, render_patch_prompt, validate_locally

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = REPO_ROOT / ".autoimplants-runtime"
WORKSPACES_ROOT = REPO_ROOT / ".autoimplants-workspaces"
DEMO_CASE = REPO_ROOT / "inputs" / "case.json"
DEMO_BONE = REPO_ROOT / "inputs" / "bone.stl"
VALIDATORS = "geometry,fea"
# Everything an iteration publishes: the solid, the report, the audit trail and the
# solver's stress field, which the viewer colours the solid with.
ITERATION_ARTIFACTS = (
    "report.json",
    "implant.stl",
    "implant.step",
    viewer.STRESS_FIELD_NAME,
)
# What one design turn is allowed to spend. Devin suspends a session that reaches
# its limit with `usage_limit_exceeded`, mid-edit and with nothing posted back, so
# this is a real failure mode rather than an accounting detail: a turn has to read
# the generator, reason about geometry and rewrite the file, which the original 5
# did not always cover. Override it downwards for a cheap demo.
DEFAULT_ACU_PER_ITERATION = 20
MAX_ITERATIONS = 3
MAX_BONE_BYTES = 25 * 1024 * 1024
MAX_DICOM_BYTES = 1024 * 1024 * 1024
MAX_PLAN_BYTES = 1024 * 1024
# The design surface the agent is handed and allowed to post back. Anything else
# is refused at the endpoint, before it can reach a workspace.
EDITABLE_SOURCES = (
    "autoimplants/generator.py",
    "autoimplants/params.py",
    "autoimplants/export.py",
)
MAX_PATCH_BYTES = 2 * 1024 * 1024
PATCH_POLL_SECONDS = 4
PATCH_TIMEOUT_SECONDS = 45 * 60
# Copied into each run's workspace; everything else in the checkout is either
# regenerated, irrelevant to a design run, or too large to duplicate per run.
WORKSPACE_TREE = ("autoimplants", "harness", "inputs", "prompts", "real_cases")
ACTIVE_STATES = {
    "preparing",
    "ingesting",
    "validating",
    "devin_running",
    "awaiting_patch",
    "validating_result",
}
RUNNABLE_STATES = {"queued", "revision_queued"}


def acu_per_iteration() -> int:
    """The per-turn spend limit, overridable through the environment."""
    raw = os.environ.get("AUTOIMPLANTS_ACU_PER_ITERATION", "").strip()
    if not raw:
        return DEFAULT_ACU_PER_ITERATION
    try:
        budget = int(raw)
    except ValueError:
        return DEFAULT_ACU_PER_ITERATION
    return budget if budget > 0 else DEFAULT_ACU_PER_ITERATION


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        workspaces_root: Path = WORKSPACES_ROOT,
        client_factory: Callable[[], DevinClient] = DevinClient,
        public_base_url: str | None = None,
    ):
        # The URL the design agent posts to. A cloud sandbox cannot reach loopback,
        # so a tunnel URL belongs here; preflight says so when it is still local.
        self.public_base_url = (
            public_base_url or os.environ.get("AUTOIMPLANTS_PUBLIC_URL") or "http://127.0.0.1:8000"
        )
        self.repo_root = Path(repo_root).resolve()
        self.runtime_root = Path(runtime_root).resolve()
        self.workspaces_root = Path(workspaces_root).resolve()
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
                    error="Resume only after checking the recorded workspace and session.",
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
            "workspace": str(self.workspaces_root / run_id),
            "queue_position": None,
            "max_iterations": max_iterations,
            "acu_per_iteration": acu_per_iteration(),
            "max_acu": max_iterations * acu_per_iteration(),
            "revision": 0,
            "cycle_iteration": 0,
            "total_iterations": 0,
            "feedback": "",
            "feedback_pin": None,
            "iterations": [],
            "reviews": [],
            "active_session": None,
            "pending_patch": None,
            "patch_results": {},
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
        errors: list[str] = []
        warnings: list[str] = []
        sources_present = all(
            (self.repo_root / path).exists() for path in EDITABLE_SOURCES
        ) and self.demo_case_path.exists()
        if not sources_present:
            errors.append("The design sources or the demo case are missing from this checkout.")
        job_base = self.public_base_url
        if urlparse(job_base).hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            warnings.append(
                "The job URL is loopback-only, so a cloud sandbox cannot POST to it. "
                "Designs will arrive through the session's structured output instead. "
                "Set AUTOIMPLANTS_PUBLIC_URL to a reachable URL for the live exchange."
            )
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
            "ready": bool(key and org and devin_ready and sources_present),
            "credentials": {
                "api_key": f"{key[:4]}…{key[-4:]}" if len(key) >= 10 else ("set" if key else "missing"),
                "org_id": f"{org[:4]}…{org[-4:]}" if len(org) >= 10 else ("set" if org else "missing"),
            },
            "devin_permission": devin_ready,
            "job_base_url": job_base,
            "design_sources": list(EDITABLE_SOURCES),
            "acu_per_iteration": acu_per_iteration(),
            "warnings": warnings,
            "errors": errors,
        }

    def _prepare_workspace(self, record: dict) -> Path:
        """A private copy of the design sources for this run.

        No Git, no branch and no remote: the run owns a directory, the agent is
        posted the files it may change, and the validators run against whatever
        lands here. Two concurrent runs therefore cannot see each other's
        geometry.
        """
        workspace = Path(record["workspace"])
        if (workspace / "autoimplants" / "generator.py").exists():
            return workspace
        workspace.parent.mkdir(parents=True, exist_ok=True)
        for name in WORKSPACE_TREE:
            source = self.repo_root / name
            if not source.exists():
                continue
            shutil.copytree(
                source,
                workspace / name,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "series"),
            )
        return workspace

    def _ingest(self, record: dict, workspace: Path) -> dict:
        """Uploaded scan plus plan to a validated case bundle inside the workspace.

        The bundle lands in the run's own workspace, so the design agent works
        against the *patient's* case rather than the repo's demo one. Only
        derived artefacts are ever handed out: the mesh, the placed plan and the
        transform. The DICOM never leaves this machine and nothing is committed.
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
            # The plan's landmarks bound the region of interest: a clinical scan
            # usually holds more than the planned bone, and segmenting all of it
            # yields femur-plus-tibia, which the mesh gate rightly rejects.
            dicom_to_mesh.dicom_to_mesh(
                series,
                bone_path,
                bone="femur",
                landmarks_mm=dicom_to_mesh.plan_landmarks(plan_path),
            )

        self.store.update(run_id, status="ingesting", phase="Importing the surgical plan")
        case_id = str(record["case_id"])
        out_dir = workspace / "real_cases" / case_id / "generated"
        report, case_path = import_case.import_case(plan_path, bone_path, out_dir=out_dir)
        durable = self.store.run_dir(run_id) / "case"
        atomic_json(durable / "import_report.json", report.to_dict())
        if case_path is None:
            failures = [check.id for check in report.checks if check.status != "PASS"]
            raise RuntimeError(
                "The uploaded scan and plan did not pass intake: " + ", ".join(failures)
            )

        relative = case_path.relative_to(workspace).as_posix()
        return self.store.update(
            run_id,
            case_path=relative,
            intake_report=report.to_dict(),
            phi_tags=phi,
            phase="Case bundle imported",
        )

    def _case_path(self, record: dict, workspace: Path) -> Path:
        relative = record.get("case_path") or "inputs/case.json"
        return workspace / relative

    def _validate(self, workspace: Path, record: dict, iteration: int) -> tuple[Report, Path]:
        out_dir = self.store.run_dir(record["run_id"]) / "working"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        report = validate_locally(
            VALIDATORS,
            repo_root=workspace,
            out_dir=out_dir,
            iteration=iteration,
            case_path=self._case_path(record, workspace),
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
        for name in ITERATION_ARTIFACTS:
            source = out_dir / name
            if source.exists():
                shutil.copy2(source, target / name)
        artifacts = {}
        for name in ITERATION_ARTIFACTS:
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
                **(
                    {"stress_field": f"{prefix}/artifacts/{viewer.STRESS_FIELD_NAME}"}
                    if (target / viewer.STRESS_FIELD_NAME).exists()
                    else {}
                ),
            },
        )
        iteration["artifact_hashes"] = artifacts
        return iteration

    def _process_run(self, run_id: str) -> None:
        record = self.store.get(run_id)
        workspace = Path(record["workspace"])
        active = record.get("active_session")
        if active:
            if not workspace.exists():
                self.store.update(
                    run_id,
                    status="needs_attention",
                    phase="Recorded workspace is missing",
                    error="The existing Devin session was not replaced. Restore the workspace first.",
                )
                return
            self._resume_active(record, workspace)
            return

        self.store.update(run_id, status="preparing", phase="Preparing an isolated design workspace", queue_position=None)
        workspace = self._prepare_workspace(record)
        record = self.store.get(run_id)

        if record.get("intake") and not record.get("case_path"):
            record = self._ingest(record, workspace)

        if not record["iterations"]:
            self.store.update(run_id, status="validating", phase="Validating the baseline geometry")
            report, out_dir = self._validate(workspace, record, 0)
            baseline = self._snapshot(record, report, out_dir)
            record = self.store.mutate(run_id, lambda item: item["iterations"].append(baseline))
            if baseline["geometry_converged"]:
                self.store.update(run_id, status="awaiting_review", phase="Baseline geometry converged")
                return

        while int(record["cycle_iteration"]) < int(record["max_iterations"]):
            self._run_design_session(record, workspace)
            record = self.store.get(run_id)
            if record["status"] in {
                "awaiting_review", "needs_attention", "invalid", "failed", "capped"
            }:
                return

        self.store.update(
            run_id,
            status="capped",
            phase=f"Iteration cap reached ({record['max_iterations']})",
            queue_position=None,
        )

    def _job_url(self, token: str) -> str:
        return f"{self.public_base_url.rstrip('/')}/api/patch/{token}"

    def record_for_token(self, token: str) -> dict:
        for record in self.store.list():
            active = record.get("active_session") or {}
            if active.get("token") == token or token in (record.get("patch_results") or {}):
                return record
        raise LookupError("Unknown design job.")

    def _run_design_session(self, record: dict, workspace: Path) -> None:
        """Hand the design surface to an agent and validate whatever it posts back.

        The agent never sees this repository: it is given the failing checks, the
        current source of the files it may change, and one job URL. Each design it
        posts is executed here, against the patient's own case, and the resulting
        report is handed straight back to it.
        """
        run_id = record["run_id"]
        number = int(record["total_iterations"]) + 1
        previous = Report.from_dict(record["iterations"][-1]["report"])
        history = [
            f"iter {item['number']}: {item.get('rationale', '')}"
            for item in record["iterations"]
            if item["number"]
        ]
        feedback = record.get("feedback", "")
        if record.get("feedback_pin"):
            feedback += "\nPinned bone coordinate (mm): " + json.dumps(record["feedback_pin"])
        token = uuid.uuid4().hex
        prompt = render_patch_prompt(
            number,
            previous,
            history,
            self._job_url(token),
            list(EDITABLE_SOURCES),
            str(record["case_id"]),
            feedback=feedback,
        )
        client = self.client_factory()
        created = client.create_session(
            prompt=prompt,
            title=f"AutoImplants {run_id} · iteration {number}",
            tags=["autoimplants", run_id, f"revision-{record['revision']}", f"iter-{number}"],
            structured_output_schema=PATCH_OUTPUT_SCHEMA,
            max_acu_limit=acu_per_iteration(),
        )
        session = {
            "session_id": created["session_id"],
            "url": created.get("url", ""),
            "token": token,
            "job_url": self._job_url(token),
            "iteration": number,
            "revision": record["revision"],
            "status": created.get("status", "new"),
            "status_detail": created.get("status_detail"),
            "patches": 0,
        }
        self.store.update(
            run_id,
            status="awaiting_patch",
            phase="Devin is engineering the next geometry",
            active_session=session,
            error=None,
        )
        self._await_patches(self.store.get(run_id), workspace, client)

    def _resume_active(self, record: dict, workspace: Path) -> None:
        self.store.update(record["run_id"], status="awaiting_patch", phase="Reconnected to Devin session")
        self._await_patches(self.store.get(record["run_id"]), workspace, self.client_factory())

    def _await_patches(self, record: dict, workspace: Path, client: DevinClient) -> None:
        run_id = record["run_id"]
        session_id = record["active_session"]["session_id"]
        deadline = time.monotonic() + PATCH_TIMEOUT_SECONDS
        while not self._stop and time.monotonic() < deadline:
            record = self.store.get(run_id)
            if record["status"] not in {"awaiting_patch", "validating_result"}:
                return
            pending = record.get("pending_patch")
            if pending:
                record = self._apply_patch(record, workspace, pending)
                if record["status"] != "awaiting_patch":
                    return
                deadline = time.monotonic() + PATCH_TIMEOUT_SECONDS
                continue
            try:
                payload = client.get_session(session_id)
            except DevinError:
                time.sleep(PATCH_POLL_SECONDS)
                continue
            self._note_session(run_id, payload, client)
            if client.is_terminal(payload):
                output = payload.get("structured_output") or {}
                if output.get("infeasible"):
                    self.store.update(
                        run_id,
                        status="failed",
                        phase="Devin reported the case infeasible",
                        error=output.get("rationale") or "No rationale returned.",
                        active_session=None,
                    )
                    return
                # A sandbox that could not reach the job URL may hand the design
                # back through its structured output instead; same validation path.
                files = output.get("files") or {}
                if files:
                    try:
                        self.submit_patch(
                            record["active_session"]["token"],
                            files,
                            rationale=str(output.get("rationale", "")),
                            topology_changed=bool(output.get("topology_changed")),
                        )
                        continue
                    except (PermissionError, ValueError) as exc:
                        self.store.update(
                            run_id,
                            status="invalid",
                            phase="Submitted design rejected",
                            error=str(exc),
                            active_session=None,
                        )
                        return
                self.store.update(
                    run_id,
                    status="needs_attention",
                    phase=f"Devin stopped at {client.status_label(payload)}",
                    error="The session ended without posting a design. Open it, resolve it, then press Resume.",
                )
                return
            time.sleep(PATCH_POLL_SECONDS)
        self.store.update(
            run_id,
            status="needs_attention",
            phase="No design arrived within the iteration timeout",
            error="Open the recorded Devin session, then press Resume.",
        )

    def _note_session(self, run_id: str, payload: dict, client: DevinClient) -> None:
        def update(item: dict) -> None:
            active = item.get("active_session") or {}
            active["status"] = payload.get("status")
            active["status_detail"] = payload.get("status_detail")
            item["active_session"] = active
            if not item.get("pending_patch"):
                item["phase"] = f"Devin · {client.status_label(payload)}"
        self.store.mutate(run_id, update)

    def submit_patch(
        self,
        token: str,
        files: dict[str, str],
        rationale: str = "",
        topology_changed: bool = False,
        infeasible: bool = False,
    ) -> dict:
        """Accept a posted design. Runs on the HTTP thread, validates on the worker."""
        record = self.record_for_token(token)
        run_id = record["run_id"]
        active = record.get("active_session") or {}
        if active.get("token") != token:
            raise LookupError("This design job is closed.")
        if record.get("pending_patch"):
            raise ValueError("The previous submission is still being validated.")
        if infeasible:
            self.store.update(
                run_id,
                status="failed",
                phase="Devin reported the case infeasible",
                error=rationale or "No rationale returned.",
                active_session=None,
            )
            return {"status": "closed", "reason": "infeasible"}
        if not files:
            raise ValueError("Submit the complete contents of at least one editable file.")
        locked = violations(sorted(files))
        if locked:
            raise PermissionError(
                "; ".join(f"{path}: {reason}" for path, reason in locked)
            )
        unknown = [path for path in sorted(files) if path not in EDITABLE_SOURCES]
        if unknown:
            # Membership, not pattern matching: absolute paths, "..", symlinks and
            # stray artefacts can never name a file inside the workspace.
            raise ValueError(
                "Post the complete contents of design sources only: "
                + ", ".join(EDITABLE_SOURCES)
                + f". Refused: {', '.join(unknown)}"
            )
        payload = {path: str(text) for path, text in files.items()}
        size = sum(len(text.encode("utf-8")) for text in payload.values())
        if size > MAX_PATCH_BYTES:
            raise ValueError(f"The submission exceeds the {MAX_PATCH_BYTES // 1024} KB limit")

        number = int(record["total_iterations"]) + 1
        staging = self.store.run_dir(run_id) / "patches" / f"{number:03d}"
        if staging.exists():
            shutil.rmtree(staging)
        digest = hashlib.sha256()
        for path in sorted(payload):
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload[path], encoding="utf-8")
            digest.update(path.encode("utf-8"))
            digest.update(payload[path].encode("utf-8"))
        design_sha = digest.hexdigest()
        pending = {
            "token": token,
            "iteration": number,
            "dir": str(staging),
            "paths": sorted(payload),
            "rationale": rationale,
            "topology_changed": bool(topology_changed),
            "design_sha": design_sha,
            "submitted_at": utc_now(),
        }
        self.store.mutate(
            run_id,
            lambda item: item.update(
                {
                    "pending_patch": pending,
                    "status": "validating_result",
                    "phase": f"Executing the design Devin posted (iteration {number})",
                }
            ),
        )
        return {
            "status": "accepted",
            "iteration": number,
            "paths": pending["paths"],
            "design_sha": design_sha,
        }

    def patch_job(self, token: str) -> dict:
        """What the agent polls: the files to work on, and the last verdict."""
        record = self.record_for_token(token)
        active = record.get("active_session") or {}
        results = record.get("patch_results") or {}
        result = results.get(token)
        if record.get("pending_patch", {}) and (record.get("pending_patch") or {}).get("token") == token:
            status = "validating"
        elif active.get("token") != token:
            status = "closed"
        elif result:
            status = "report_ready"
        else:
            status = "awaiting_patch"
        workspace = Path(record["workspace"])
        sources = {}
        for path in EDITABLE_SOURCES:
            source = workspace / path
            if source.exists():
                sources[path] = source.read_text(encoding="utf-8")
        case_path = self._case_path(record, workspace)
        case = case_io.load_case(case_path) if case_path.exists() else None
        return {
            "status": status,
            "run_id": record["run_id"],
            "case_id": record["case_id"],
            "iteration": int(record["total_iterations"]) + 1,
            "iterations_used": int(record["cycle_iteration"]),
            "iteration_budget": int(record["max_iterations"]),
            "validators": VALIDATORS.split(","),
            "editable_files": list(EDITABLE_SOURCES),
            "locked_globs_hint": list(EDITABLE_GLOBS),
            "sources": sources,
            "case": case,
            "last_result": result,
        }

    def _apply_patch(self, record: dict, workspace: Path, pending: dict) -> dict:
        """Execute a posted design in this run's workspace and measure it."""
        run_id = record["run_id"]
        number = int(pending["iteration"])
        self.store.update(
            run_id,
            status="validating_result",
            phase=f"Executing the design Devin posted (iteration {number})",
        )
        staging = Path(pending["dir"])
        for path in pending["paths"]:
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / path, target)
        report, out_dir = self._validate(workspace, record, number)
        active = record.get("active_session") or {}
        iteration = self._snapshot(
            record,
            report,
            out_dir,
            commit_sha=pending["design_sha"],
            rationale=pending.get("rationale", ""),
            session_url=active.get("url", ""),
            topology_changed=bool(pending.get("topology_changed")),
            changed=pending["paths"],
        )
        result = {
            "iteration": number,
            "verdict": "converged" if iteration["geometry_converged"] else "still_failing",
            "failing": [
                check["id"] for check in iteration["report"]["checks"] if check["status"] == "FAIL"
            ],
            "report": iteration["report"],
            "validated_at": utc_now(),
        }

        def finish(item: dict) -> None:
            item["iterations"].append(iteration)
            item["total_iterations"] = number
            item["cycle_iteration"] = int(item["cycle_iteration"]) + 1
            item["pending_patch"] = None
            item["error"] = None
            item.setdefault("patch_results", {})[pending["token"]] = result
            session = item.get("active_session") or {}
            session["patches"] = int(session.get("patches", 0)) + 1
            session["iteration"] = number + 1
            item["active_session"] = session
            if iteration["geometry_converged"]:
                item["status"] = "awaiting_review"
                item["phase"] = "Geometry converged · awaiting surgeon review"
                item["active_session"] = None
            elif int(item["cycle_iteration"]) >= int(item["max_iterations"]):
                item["status"] = "capped"
                item["phase"] = f"Iteration cap reached ({item['max_iterations']})"
                item["active_session"] = None
            else:
                item["status"] = "awaiting_patch"
                item["phase"] = f"Iteration {number} still fails · waiting for the next design"
        record = self.store.mutate(run_id, finish)
        if record["status"] == "awaiting_patch" and active.get("session_id"):
            failing = ", ".join(result["failing"]) or "none"
            try:
                self.client_factory().send_message(
                    active["session_id"],
                    "Your design was executed against the patient case. Failing checks: "
                    f"{failing}. GET your job URL for the full report, then post the next "
                    "design to the same URL.",
                )
            except DevinError:
                pass
        return record

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
                raise ValueError(
                    "A fresh revision cycle of up to "
                    f"{MAX_ITERATIONS * acu_per_iteration()} ACU must be confirmed."
                )

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


class PatchSubmission(BaseModel):
    """One posted design: whole files, not a diff."""

    files: dict[str, str] = Field(default_factory=dict)
    rationale: str = Field(default="", max_length=8000)
    topology_changed: bool = False
    infeasible: bool = False


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
    workspaces_root: Path = WORKSPACES_ROOT,
    manager: RunManager | None = None,
) -> FastAPI:
    repo_root = Path(repo_root).resolve()
    manager = manager or RunManager(repo_root, runtime_root, workspaces_root)

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
        acu_per_iteration_ack: int = Form(default=0, alias="acu_per_iteration"),
        cost_ack: bool = Form(default=False),
    ) -> dict:
        if not 1 <= max_iterations <= MAX_ITERATIONS:
            raise HTTPException(422, "max_iterations must be between 1 and 3")
        budget = acu_per_iteration()
        # The page shows a cost before it asks for consent, so a caller that names a
        # per-iteration budget must name the one this server will actually authorize.
        if acu_per_iteration_ack not in (0, budget):
            raise HTTPException(422, f"acu_per_iteration is locked to {budget}")
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
                # Reject internal generated case manifests and incomplete plans
                # before a run is queued. These are user-correctable intake
                # failures, not background failures that belong in a workbench.
                surgical_plan.load_plan(staged / "surgical_plan.json")
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
        workspace = Path(record["workspace"])
        case_path = workspace / "inputs" / "case.json"
        if not case_path.exists():
            case_path = manager.demo_case_path
        case = case_io.set_active_case(case_io.load_case(case_path), case_path)
        meshes = [
            viewer._mesh_payload(case_io.bone_path(case), "bone", viewer.BONE_COLOR, viewer.BONE_FACE_BUDGET)
        ]
        if implant.exists():
            meshes.append(
                viewer._mesh_payload(
                    implant,
                    "implant",
                    viewer.IMPLANT_COLOR,
                    viewer.IMPLANT_FACE_BUDGET,
                    field=Path(implant).with_name(viewer.STRESS_FIELD_NAME),
                )
            )
        return JSONResponse({"meshes": meshes})

    @app.get("/api/runs/{run_id}/iterations/{iteration}/artifacts/{name}")
    def artifact(run_id: str, iteration: int, name: str) -> FileResponse:
        if name not in set(ITERATION_ARTIFACTS) | {"audit.json"}:
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

    @app.get("/api/patch/{token}")
    def patch_job(token: str) -> dict:
        """The design agent's own endpoint: what to work on, and the last verdict."""
        try:
            return manager.patch_job(token)
        except LookupError as exc:
            raise HTTPException(404, "Unknown design job") from exc

    @app.post("/api/patch/{token}")
    def submit_patch(token: str, submission: PatchSubmission) -> dict:
        try:
            return manager.submit_patch(
                token,
                submission.files,
                rationale=submission.rationale,
                topology_changed=submission.topology_changed,
                infeasible=submission.infeasible,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc) or "Unknown design job") from exc
        except PermissionError as exc:
            raise HTTPException(403, f"Locked files cannot be changed: {exc}") from exc
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
    # .env carries AUTOIMPLANTS_PUBLIC_URL. Reading it after argparse built its
    # defaults left the job URL on loopback, which a cloud sandbox cannot POST to,
    # so every run parked at awaiting_patch with no way for the design to arrive.
    load_env(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(prog="autoimplants.server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--public-url",
        default=os.environ.get("AUTOIMPLANTS_PUBLIC_URL", ""),
        help="Base URL the design agent posts to. Defaults to the served host and "
             "port, which only a local agent can reach; give a tunnel URL for a "
             "cloud sandbox.",
    )
    args = parser.parse_args(argv)
    # The job URL has to name the port we are actually serving, or the agent posts
    # its design into nothing.
    public_url = args.public_url or f"http://{args.host}:{args.port}"
    os.environ["AUTOIMPLANTS_PUBLIC_URL"] = public_url
    # The module-level app was built while importing this module, before the port
    # was known, so tell its manager where it is actually being served.
    app.state.manager.public_base_url = public_url
    import uvicorn

    uvicorn.run("autoimplants.server:app", host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
