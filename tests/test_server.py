from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoimplants.contracts import FAIL, PASS, SKIP, Check, Report
from autoimplants.server import (
    EDITABLE_SOURCES,
    VALIDATORS,
    ReviewRequest,
    RunManager,
    RunStore,
    create_app,
    extract_series,
)
from autoimplants.validators import pending_stress
from autoimplants.validators.fea import FIELD_NAME as STRESS_FIELD_NAME
from autoimplants.validators.stress import CHECK_IDS


class FakeManager:
    def __init__(self, repo_root: Path, runtime_root: Path):
        self.repo_root = repo_root
        self.runtime_root = runtime_root
        self.demo_bone_path = repo_root / "inputs" / "bone.stl"
        self.demo_case_path = repo_root / "inputs" / "case.json"
        self.store = RunStore(runtime_root)
        self.created = []
        self.intakes: list[Path | None] = []

    def start(self):
        pass

    def stop(self):
        pass

    def preflight(self):
        return {"ready": True, "errors": []}

    def create_run(self, max_iterations: int, intake: Path | None = None):
        self.created.append(max_iterations)
        self.intakes.append(intake)
        return self.store.put(
            {
                "run_id": f"run-test-{len(self.created)}",
                "case_id": "SYNTH-FEMUR-001",
                "status": "queued",
                "phase": "Waiting",
                "created_at": "2026-08-23T00:00:00+00:00",
                "max_iterations": max_iterations,
                "acu_per_iteration": 5,
                "max_acu": max_iterations * 5,
                "iterations": [],
                "reviews": [],
            }
        )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "bone.stl").write_bytes(b"demo-stl")
    (inputs / "case.json").write_text(json.dumps({"case_id": "SYNTH-FEMUR-001"}))
    return root


def test_upload_requires_demo_fingerprint_and_cost_ack(tmp_path):
    root = _repo(tmp_path)
    manager = FakeManager(root, tmp_path / "runtime")
    app = create_app(root, tmp_path / "runtime", tmp_path / "workspaces", manager=manager)

    with TestClient(app) as client:
        missing_ack = client.post(
            "/api/runs",
            files={"bone": ("bone.stl", b"demo-stl", "model/stl")},
            data={"max_iterations": "3", "acu_per_iteration": "5", "cost_ack": "false"},
        )
        assert missing_ack.status_code == 422

        wrong_bone = client.post(
            "/api/runs",
            files={"bone": ("bone.stl", b"another-bone", "model/stl")},
            data={"max_iterations": "3", "acu_per_iteration": "5", "cost_ack": "true"},
        )
        assert wrong_bone.status_code == 422
        assert "surgical-plan JSON" in wrong_bone.json()["detail"]

        accepted = client.post(
            "/api/runs",
            files={"bone": ("bone.stl", b"demo-stl", "model/stl")},
            data={"max_iterations": "3", "acu_per_iteration": "5", "cost_ack": "true"},
        )
        assert accepted.status_code == 202
        assert accepted.json()["max_acu"] == 15
        assert manager.created == [3]


def _series_zip(slices: int = 3) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("CASE/DICOM/", "")
        for index in range(slices):
            bundle.writestr(f"CASE/DICOM/slice_{index:04d}.dcm", b"DICM-fake-slice")
    return buffer.getvalue()


def test_dicom_series_and_plan_are_staged_for_the_worker(tmp_path):
    root = _repo(tmp_path)
    manager = FakeManager(root, tmp_path / "runtime")
    app = create_app(root, tmp_path / "runtime", tmp_path / "workspaces", manager=manager)
    plan = json.dumps({"case_id": "REAL-CT-001", "screws": []}).encode()

    with TestClient(app) as client:
        accepted = client.post(
            "/api/runs",
            files={
                "dicom": ("series.zip", _series_zip(), "application/zip"),
                "plan": ("surgical_plan.json", plan, "application/json"),
            },
            data={"max_iterations": "2", "acu_per_iteration": "5", "cost_ack": "true"},
        )
        assert accepted.status_code == 202, accepted.text

    staged = manager.intakes[0]
    assert staged is not None
    assert json.loads((staged / "surgical_plan.json").read_text())["case_id"] == "REAL-CT-001"
    assert len(list((staged / "series").iterdir())) == 3
    assert not (staged / "series.zip").exists()


def test_intake_needs_exactly_one_anatomy_source_and_a_readable_plan(tmp_path):
    root = _repo(tmp_path)
    manager = FakeManager(root, tmp_path / "runtime")
    app = create_app(root, tmp_path / "runtime", tmp_path / "workspaces", manager=manager)
    ack = {"max_iterations": "1", "acu_per_iteration": "5", "cost_ack": "true"}
    plan = ("surgical_plan.json", json.dumps({"case_id": "X"}).encode(), "application/json")

    with TestClient(app) as client:
        both = client.post(
            "/api/runs",
            files={
                "bone": ("bone.stl", b"demo-stl", "model/stl"),
                "dicom": ("series.zip", _series_zip(), "application/zip"),
                "plan": plan,
            },
            data=ack,
        )
        assert both.status_code == 422
        assert "exactly one anatomy source" in both.json()["detail"]

        neither = client.post("/api/runs", files={"plan": plan}, data=ack)
        assert neither.status_code == 422

        planless = client.post(
            "/api/runs",
            files={"dicom": ("series.zip", _series_zip(), "application/zip")},
            data=ack,
        )
        assert planless.status_code == 422
        assert "surgical plan" in planless.json()["detail"]

        broken = client.post(
            "/api/runs",
            files={
                "dicom": ("series.zip", _series_zip(), "application/zip"),
                "plan": ("surgical_plan.json", b"{not json", "application/json"),
            },
            data=ack,
        )
        assert broken.status_code == 422

        empty = client.post(
            "/api/runs",
            files={
                "dicom": ("series.zip", _empty_zip(), "application/zip"),
                "plan": plan,
            },
            data=ack,
        )
        assert empty.status_code == 422

    assert manager.created == []
    # Every rejection cleans up after itself, so no half-staged upload survives.
    assert not list((tmp_path / "runtime" / "uploads").glob("*"))


def _empty_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("CASE/", "")
    return buffer.getvalue()


def test_archive_members_cannot_escape_the_intake_directory(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../../escaped.dcm", b"DICM")
        bundle.writestr("nested/deep/slice.dcm", b"DICM")
    destination = tmp_path / "series"

    assert extract_series(archive, destination) == 2
    assert sorted(path.name for path in destination.iterdir()) == [
        "00000_escaped.dcm",
        "00001_slice.dcm",
    ]
    assert not (tmp_path.parent / "escaped.dcm").exists()


def test_run_store_round_trips_atomically(tmp_path):
    store = RunStore(tmp_path)
    saved = store.put({"run_id": "abc", "status": "queued", "created_at": "now"})
    assert saved["version"] == 1
    assert not store.path("abc").with_suffix(".json.tmp").exists()
    updated = store.update("abc", status="validating")
    assert updated["version"] == 2
    assert RunStore(tmp_path).get("abc")["status"] == "validating"


def test_pending_stress_is_eight_visible_skips():
    report = pending_stress.validate(
        "implant.stl",
        {"thresholds": {"max_stress_MPa": 350, "min_screw_pullout_N": 1200}},
    )
    assert len(report.checks) == 8
    assert {check.status for check in report.checks} == {SKIP}
    assert report.passed is True


def test_iteration_publishes_and_serves_the_solved_stress_field(tmp_path):
    """The field is an audited artifact, not a side file: hashed, listed, servable."""
    manager = RunManager(tmp_path / "repo", tmp_path / "runtime", tmp_path / "workspaces")
    record = manager.store.put(
        {"run_id": "field-run", "status": "validating", "created_at": "now", "iterations": []}
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "implant.stl").write_bytes(b"solid\n")
    (out_dir / STRESS_FIELD_NAME).write_text(
        json.dumps({"vertices": [[0.0, 0.0, 0.0]], "von_mises_MPa": [12.5], "max_MPa": 12.5})
    )
    report = Report.from_checks([Check(id="fea_max_von_mises", status=PASS, value=12.5)], iteration=0)

    iteration = manager._snapshot(record, report, out_dir)

    assert STRESS_FIELD_NAME in iteration["artifact_hashes"]
    assert iteration["artifacts"]["stress_field"].endswith(f"artifacts/{STRESS_FIELD_NAME}")

    client = TestClient(
        create_app(
            manager.repo_root,
            tmp_path / "runtime",
            tmp_path / "workspaces",
            manager=manager,
        )
    )
    served = client.get(f"/api/runs/field-run/iterations/0/artifacts/{STRESS_FIELD_NAME}")
    assert served.status_code == 200
    assert served.json()["max_MPa"] == 12.5


def test_the_live_workflow_runs_the_solver_not_a_placeholder():
    assert "fea" in VALIDATORS.split(",")
    assert "pending_stress" not in VALIDATORS


def test_rejection_is_append_only_and_gets_fresh_cycle(tmp_path, monkeypatch):
    manager = RunManager(tmp_path / "repo", tmp_path / "runtime", tmp_path / "workspaces")
    report = Report.from_checks([Check(id="geometry", status=PASS)], iteration=2)
    iteration = {
        "number": 2,
        "geometry_converged": True,
        "commit_sha": "a" * 40,
        "artifact_hashes": {"implant.stl": {"sha256": "b" * 64}},
        "report": report.to_dict(),
    }
    manager.store.put(
        {
            "run_id": "revision-run",
            "status": "awaiting_review",
            "phase": "Review",
            "created_at": "now",
            "revision": 0,
            "cycle_iteration": 2,
            "iterations": [iteration],
            "reviews": [],
        }
    )
    queued = []
    monkeypatch.setattr(manager, "enqueue", lambda run_id, preserve_status=False: queued.append(run_id))
    result = manager.review(
        "revision-run",
        ReviewRequest(
            decision="revision_requested",
            reviewer="Dr Test",
            iteration=2,
            feedback="Reduce the proximal prominence.",
            pin=[1.0, 2.0, 3.0],
            cost_ack=True,
        ),
    )
    assert result["status"] == "revision_queued"
    assert result["revision"] == 1
    assert result["cycle_iteration"] == 0
    assert result["reviews"][0]["commit_sha"] == "a" * 40
    assert result["reviews"][0]["artifact_hashes"] == iteration["artifact_hashes"]
    assert queued == ["revision-run"]


def _patch_run(tmp_path: Path, monkeypatch, report: Report, *, max_iterations: int = 3):
    """A run parked on a workspace, waiting for a design to be posted."""
    repo = tmp_path / "repo"
    (repo / "autoimplants").mkdir(parents=True)
    (repo / "autoimplants" / "generator.py").write_text("PEAK_WALL_MM = 3.0\n")
    (repo / "inputs").mkdir()
    (repo / "inputs" / "case.json").write_text(json.dumps({"case_id": "SYNTH-FEMUR-001"}))
    manager = RunManager(repo, tmp_path / "runtime", tmp_path / "workspaces")
    workspace = tmp_path / "workspaces" / "posted"
    (workspace / "autoimplants").mkdir(parents=True)
    (workspace / "autoimplants" / "generator.py").write_text("PEAK_WALL_MM = 3.0\n")
    (workspace / "inputs").mkdir()
    (workspace / "inputs" / "case.json").write_text(json.dumps({"case_id": "SYNTH-FEMUR-001"}))

    out = tmp_path / "validated"
    out.mkdir()
    report.write(out / "report.json")
    (out / "implant.stl").write_bytes(b"validated-stl")
    (out / "implant.step").write_bytes(b"validated-step")
    monkeypatch.setattr(manager, "_validate", lambda *_: (report, out))
    manager.store.put(
        {
            "run_id": "posted",
            "case_id": "SYNTH-FEMUR-001",
            "status": "awaiting_patch",
            "phase": "Devin is engineering the next geometry",
            "created_at": "now",
            "workspace": str(workspace),
            "revision": 0,
            "cycle_iteration": 0,
            "total_iterations": 0,
            "max_iterations": max_iterations,
            "iterations": [],
            "reviews": [],
            "pending_patch": None,
            "patch_results": {},
            "active_session": {
                "session_id": "session-1",
                "url": "https://app.devin.ai/sessions/session-1",
                "token": "job-token",
                "iteration": 1,
                "patches": 0,
            },
        }
    )
    return manager, workspace


def _converged_report() -> Report:
    return Report.from_checks(
        [Check(id=f"geometry_{i}", status=PASS) for i in range(13)]
        + [Check(id=check_id, status=SKIP) for check_id in CHECK_IDS],
        iteration=1,
    )


def test_posted_design_is_executed_in_the_workspace_and_reported_back(tmp_path, monkeypatch):
    report = _converged_report()
    manager, workspace = _patch_run(tmp_path, monkeypatch, report)

    accepted = manager.submit_patch(
        "job-token",
        {"autoimplants/generator.py": "PEAK_WALL_MM = 4.2\n"},
        rationale="Raise the pad over the distal bores.",
        topology_changed=True,
    )
    assert accepted["status"] == "accepted"
    assert accepted["iteration"] == 1
    # Nothing reaches the workspace until the worker executes the submission.
    assert (workspace / "autoimplants" / "generator.py").read_text() == "PEAK_WALL_MM = 3.0\n"
    assert manager.patch_job("job-token")["status"] == "validating"

    record = manager._apply_patch(
        manager.store.get("posted"), workspace, manager.store.get("posted")["pending_patch"]
    )
    assert (workspace / "autoimplants" / "generator.py").read_text() == "PEAK_WALL_MM = 4.2\n"
    assert record["status"] == "awaiting_review"
    assert record["iterations"][0]["rationale"] == "Raise the pad over the distal bores."
    assert record["iterations"][0]["commit_sha"] == accepted["design_sha"]
    assert record["iterations"][0]["coverage"]["geometry"]["PASS"] == 13
    assert record["iterations"][0]["coverage"]["stress"]["SKIP"] == 8

    # The job is closed once the geometry converged, and the verdict is readable.
    job = manager.patch_job("job-token")
    assert job["status"] == "closed"
    assert job["last_result"]["verdict"] == "converged"


def test_a_failing_design_gets_the_report_back_and_stays_open(tmp_path, monkeypatch):
    failing = Report.from_checks(
        [Check(id="implant_mass", status=FAIL, value=61.0, limit=55.0, unit="g")],
        iteration=1,
    )
    manager, workspace = _patch_run(tmp_path, monkeypatch, failing)
    messages = []
    monkeypatch.setattr(
        manager,
        "client_factory",
        lambda: type(
            "Client",
            (),
            {"send_message": lambda _self, session, text: messages.append((session, text))},
        )(),
    )
    manager.submit_patch("job-token", {"autoimplants/params.py": "WALL = 5\n"})
    record = manager._apply_patch(
        manager.store.get("posted"), workspace, manager.store.get("posted")["pending_patch"]
    )
    assert record["status"] == "awaiting_patch"
    assert record["cycle_iteration"] == 1
    job = manager.patch_job("job-token")
    assert job["status"] == "report_ready"
    assert job["last_result"]["failing"] == ["implant_mass"]
    assert job["iteration"] == 2
    assert job["sources"]["autoimplants/generator.py"] == "PEAK_WALL_MM = 3.0\n"
    assert messages and "implant_mass" in messages[0][1]


def test_locked_files_are_refused_before_they_reach_a_workspace(tmp_path, monkeypatch):
    manager, workspace = _patch_run(tmp_path, monkeypatch, _converged_report())
    with pytest.raises(PermissionError) as locked:
        manager.submit_patch("job-token", {"inputs/case.json": "{}"})
    assert "inputs/case.json" in str(locked.value)
    with pytest.raises(PermissionError):
        manager.submit_patch("job-token", {"autoimplants/validators/geometry.py": "pass"})
    assert manager.store.get("posted")["pending_patch"] is None
    assert (workspace / "inputs" / "case.json").read_text() == json.dumps(
        {"case_id": "SYNTH-FEMUR-001"}
    )


def test_an_unknown_or_closed_job_cannot_post(tmp_path, monkeypatch):
    manager, _ = _patch_run(tmp_path, monkeypatch, _converged_report())
    with pytest.raises(LookupError):
        manager.submit_patch("not-a-token", {"autoimplants/params.py": "WALL = 5\n"})
    manager.submit_patch("job-token", {"autoimplants/params.py": "WALL = 5\n"})
    with pytest.raises(ValueError):
        manager.submit_patch("job-token", {"autoimplants/params.py": "WALL = 6\n"})


def test_patch_endpoints_serve_the_job_and_reject_locked_paths(tmp_path, monkeypatch):
    manager, _ = _patch_run(tmp_path, monkeypatch, _converged_report())
    app = create_app(manager.repo_root, tmp_path / "runtime", tmp_path / "workspaces", manager=manager)
    with TestClient(app) as client:
        job = client.get("/api/patch/job-token")
        assert job.status_code == 200
        assert job.json()["editable_files"] == list(EDITABLE_SOURCES)
        assert job.json()["sources"]["autoimplants/generator.py"] == "PEAK_WALL_MM = 3.0\n"
        assert client.get("/api/patch/nope").status_code == 404

        locked = client.post(
            "/api/patch/job-token",
            json={"files": {"harness/guard.py": "pass"}, "rationale": "no"},
        )
        assert locked.status_code == 403

        posted = client.post(
            "/api/patch/job-token",
            json={
                "files": {"autoimplants/generator.py": "PEAK_WALL_MM = 4.0\n"},
                "rationale": "Thicker wall at the bores.",
                "topology_changed": False,
            },
        )
        assert posted.status_code == 200, posted.text
        assert posted.json()["paths"] == ["autoimplants/generator.py"]

        busy = client.post(
            "/api/patch/job-token",
            json={"files": {"autoimplants/params.py": "WALL = 5\n"}},
        )
        assert busy.status_code == 409


def test_infeasible_submission_stops_the_run(tmp_path, monkeypatch):
    manager, _ = _patch_run(tmp_path, monkeypatch, _converged_report())
    result = manager.submit_patch(
        "job-token", {}, rationale="Mass budget and keepouts cannot both hold.", infeasible=True
    )
    assert result["status"] == "closed"
    record = manager.store.get("posted")
    assert record["status"] == "failed"
    assert "keepouts" in record["error"]
