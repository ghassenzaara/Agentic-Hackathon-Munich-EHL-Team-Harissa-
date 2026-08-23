from __future__ import annotations

from autoimplants.contracts import FAIL, PASS, SKIP, Check, Report
from autoimplants import viewer
from autoimplants.validators.stress import CHECK_IDS


def _patch_mesh_inputs(monkeypatch):
    monkeypatch.setattr(
        viewer,
        "_mesh_payload",
        lambda path, name, color, budget: {
            "name": name,
            "color": color,
            "v": [0, 0, 0, 1, 0, 0, 0, 1, 0],
            "f": [0, 1, 2],
            "faces": 1,
        },
    )
    monkeypatch.setattr(viewer.case_io, "bone_path", lambda case: "bone.stl")
    monkeypatch.setattr(viewer.case_io, "load_screws", lambda case: [])
    monkeypatch.setattr(viewer.case_io, "load_keepouts", lambda case: [])


def _report(geometry_fail: bool) -> Report:
    geometry = [
        Check(
            id=f"geometry_{index}",
            status=FAIL if geometry_fail and index == 0 else PASS,
            value=6.57 if index == 0 else 1.0,
            limit=1.5,
            unit="mm",
            location=[1.0, 2.0, 3.0],
        )
        for index in range(13)
    ]
    stress = [Check(id=check_id, status=SKIP) for check_id in CHECK_IDS]
    return Report.from_checks(geometry + stress)


def test_viewer_keeps_geometry_and_skipped_stress_separate(monkeypatch):
    _patch_mesh_inputs(monkeypatch)

    html = viewer.build_page({"case_id": "CASE-1"}, None, _report(geometry_fail=True))

    assert '"geometry":{"PASS":12,"FAIL":1,"SKIP":0,"ERROR":0,"TOTAL":13}' in html
    assert '"stress":{"PASS":0,"FAIL":0,"SKIP":8,"ERROR":0,"TOTAL":8}' in html
    assert "21/21" not in html
    assert '"geometry_converged":false' in html


def test_skipped_stress_does_not_block_geometry_convergence(monkeypatch):
    _patch_mesh_inputs(monkeypatch)

    html = viewer.build_page({"case_id": "CASE-2"}, None, _report(geometry_fail=False))

    assert '"geometry":{"PASS":13,"FAIL":0,"SKIP":0,"ERROR":0,"TOTAL":13}' in html
    assert '"stress":{"PASS":0,"FAIL":0,"SKIP":8,"ERROR":0,"TOTAL":8}' in html
    assert '"geometry_converged":true' in html
    assert "GEOMETRY CONVERGED" in html
    assert "Skipped checks are never included in a passing total." in html


def test_report_summary_never_counts_skip_as_pass():
    summary = _report(geometry_fail=False).summary()

    assert "13 PASS, 8 SKIP" in summary
    assert "21/21" not in summary
    assert "checks passing" not in summary


def test_live_viewer_can_start_a_fresh_case_without_deleting_history(monkeypatch):
    _patch_mesh_inputs(monkeypatch)

    html = viewer.build_page(
        {"case_id": "CASE-3"}, None, _report(geometry_fail=False), server_mode=True
    )

    assert 'id="new-case-button"' in html
    assert 'location.assign("/?new=1")' in html
    assert "if(!SERVER||START_FRESH)return" in html


def test_landing_story_precedes_intake_and_preserves_workflow_hooks(monkeypatch):
    _patch_mesh_inputs(monkeypatch)

    html = viewer.build_page(
        {"case_id": "CASE-4"}, None, _report(geometry_fail=True), server_mode=True
    )

    assert 'id="landing-story" data-active-beat="0"' in html
    assert 'class="story-stage" aria-hidden="true"' in html
    assert '<canvas id="landing-view"></canvas>' in html
    assert html.count('class="story-beat" data-beat=') == 3
    assert html.index('id="landing-story"') < html.index('id="case-intake"')
    assert 'href="#case-intake">Start case</a>' in html
    assert 'id="bone-file"' in html
    assert 'id="dropzone"' in html
    assert 'id="case-review"' in html
    assert 'id="start-run"' in html
    assert "IntersectionObserver" in html
    assert 'addEventListener("scroll"' not in html
    assert "function createMeshRenderer" in html
    assert "prefers-reduced-motion:reduce" in html
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in html
    assert "3D preview unavailable" in html
    assert "21/21" not in html
