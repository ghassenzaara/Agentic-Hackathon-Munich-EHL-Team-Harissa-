from __future__ import annotations

import io
import json
import subprocess
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from autoimplants.contracts import PASS, SKIP, Check, Report
from autoimplants.server import ReviewRequest, RunManager, RunStore, create_app, extract_series
from autoimplants.validators import pending_stress
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
    app = create_app(root, tmp_path / "runtime", tmp_path / "worktrees", manager=manager)

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
    app = create_app(root, tmp_path / "runtime", tmp_path / "worktrees", manager=manager)
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
    app = create_app(root, tmp_path / "runtime", tmp_path / "worktrees", manager=manager)
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


def test_rejection_is_append_only_and_gets_fresh_cycle(tmp_path, monkeypatch):
    manager = RunManager(tmp_path / "repo", tmp_path / "runtime", tmp_path / "worktrees")
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


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _guarded_repo(tmp_path: Path, changed_path: str):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    agent = tmp_path / "agent"
    server = tmp_path / "server-worktree"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "autoimplants").mkdir()
    (seed / "autoimplants" / "generator.py").write_text("VALUE = 1\n")
    (seed / "inputs").mkdir()
    (seed / "inputs" / "case.json").write_text("{}\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", str(remote), str(agent))
    _git(agent, "config", "user.email", "devin@example.com")
    _git(agent, "config", "user.name", "Devin")
    _git(agent, "checkout", "-b", "devin/autoimplants-test", "origin/main")
    target = agent / changed_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("CHANGED = True\n")
    _git(agent, "add", changed_path)
    rationale = "Contour the plate along the shaft\n\nFailure addressed: bone_conformance_gap."
    _git(agent, "commit", "-m", rationale)
    commit = _git(agent, "rev-parse", "HEAD")
    _git(agent, "push", "origin", "HEAD:refs/heads/devin/autoimplants-test")
    _git(tmp_path, "clone", str(remote), str(server))
    _git(server, "checkout", "-b", "devin/autoimplants-test", "origin/main")
    return seed, server, commit, rationale


def test_remote_commit_is_guarded_then_independently_validated(tmp_path, monkeypatch):
    seed, worktree, commit, rationale = _guarded_repo(tmp_path, "autoimplants/generator.py")
    manager = RunManager(seed, tmp_path / "runtime", tmp_path / "worktrees")
    base = _git(worktree, "rev-parse", "HEAD")
    report = Report.from_checks(
        [Check(id=f"geometry_{i}", status=PASS) for i in range(13)]
        + [Check(id=check_id, status=SKIP) for check_id in CHECK_IDS],
        iteration=1,
    )
    out = tmp_path / "validated"
    out.mkdir()
    report.write(out / "report.json")
    (out / "implant.stl").write_bytes(b"validated-stl")
    (out / "implant.step").write_bytes(b"validated-step")
    monkeypatch.setattr(manager, "_validate", lambda *_: (report, out))
    manager.store.put(
        {
            "run_id": "guarded",
            "status": "devin_running",
            "phase": "Devin",
            "created_at": "now",
            "branch": "devin/autoimplants-test",
            "worktree": str(worktree),
            "revision": 0,
            "cycle_iteration": 0,
            "total_iterations": 0,
            "max_iterations": 3,
            "iterations": [],
            "reviews": [],
            "active_session": {
                "session_id": "session-1",
                "url": "https://app.devin.ai/sessions/session-1",
                "base_sha": base,
                "iteration": 1,
            },
        }
    )
    manager._integrate_result(
        manager.store.get("guarded"),
        worktree,
        {"commit_sha": commit, "topology_changed": True},
    )
    result = manager.store.get("guarded")
    assert result["status"] == "awaiting_review"
    assert result["iterations"][0]["commit_sha"] == commit
    assert result["iterations"][0]["rationale"] == rationale
    assert result["iterations"][0]["coverage"]["geometry"]["PASS"] == 13
    assert result["iterations"][0]["coverage"]["stress"]["SKIP"] == 8


def test_locked_file_commit_is_rejected_before_validation(tmp_path, monkeypatch):
    seed, worktree, commit, _ = _guarded_repo(tmp_path, "inputs/case.json")
    manager = RunManager(seed, tmp_path / "runtime", tmp_path / "worktrees")
    base = _git(worktree, "rev-parse", "HEAD")
    called = []
    monkeypatch.setattr(manager, "_validate", lambda *_: called.append(True))
    manager.store.put(
        {
            "run_id": "locked",
            "status": "devin_running",
            "phase": "Devin",
            "created_at": "now",
            "branch": "devin/autoimplants-test",
            "worktree": str(worktree),
            "revision": 0,
            "cycle_iteration": 0,
            "total_iterations": 0,
            "max_iterations": 3,
            "iterations": [],
            "reviews": [],
            "active_session": {
                "session_id": "session-2",
                "url": "https://app.devin.ai/sessions/session-2",
                "base_sha": base,
                "iteration": 1,
            },
        }
    )
    manager._integrate_result(manager.store.get("locked"), worktree, {"commit_sha": commit})
    result = manager.store.get("locked")
    assert result["status"] == "invalid"
    assert "inputs/case.json" in result["error"]
    assert called == []
