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
