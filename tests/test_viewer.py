from __future__ import annotations

import hashlib
import json

import trimesh

from autoimplants.contracts import FAIL, PASS, SKIP, Check, Report
from autoimplants import viewer
from autoimplants.validators.stress import CHECK_IDS


def _patch_mesh_inputs(monkeypatch):
    monkeypatch.setattr(
        viewer,
        "_mesh_payload",
        lambda path, name, color, budget, field=None: {
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


def test_stress_field_reaches_the_browser_with_its_own_scale(tmp_path):
    """A heatmap is only honest if the legend carries the solver's own peak."""
    solid = trimesh.creation.box(extents=(10.0, 4.0, 2.0))
    implant = tmp_path / "implant.stl"
    solid.export(implant)
    field = tmp_path / viewer.STRESS_FIELD_NAME
    field.write_text(
        json.dumps(
            {
                "vertices": solid.vertices.tolist(),
                "von_mises_MPa": [float(index) for index in range(len(solid.vertices))],
                "max_MPa": float(len(solid.vertices) - 1),
                "allowable_MPa": 350.0,
                "unit": "MPa",
                "note": "indicative linear-static solve",
            }
        )
    )

    payload = viewer._mesh_payload(
        implant, "implant", viewer.IMPLANT_COLOR, 4000, field=field
    )

    assert len(payload["field"]) == len(payload["v"]) // 3
    assert payload["field_max"] == float(len(solid.vertices) - 1)
    assert payload["field_limit"] == 350.0
    assert payload["field_unit"] == "MPa"


def test_mesh_payload_without_a_solve_carries_no_field(tmp_path):
    solid = trimesh.creation.box(extents=(10.0, 4.0, 2.0))
    implant = tmp_path / "implant.stl"
    solid.export(implant)

    payload = viewer._mesh_payload(
        implant,
        "implant",
        viewer.IMPLANT_COLOR,
        4000,
        field=tmp_path / viewer.STRESS_FIELD_NAME,
    )

    assert "field" not in payload


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
    assert '<canvas id="landing-view" aria-hidden="true"></canvas>' in html
    assert '"landing_mesh":{"name":"landing-femur"' in html
    assert "DATA.landing_mesh||DATA.meshes[0]" in html
    assert "commons.wikimedia.org/wiki/File:Human_femur.stl" in html
    assert "CC BY 4.0" in html
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


def test_landing_femur_asset_is_the_reviewed_web_derivative():
    asset = viewer.LANDING_BONE_ASSET

    assert asset.exists()
    assert asset.stat().st_size == 500_084
    assert (
        hashlib.sha256(asset.read_bytes()).hexdigest()
        == "a4bd5fc78eed691c007022054b0d2c102233839faebb3c3ae52c167dce90bc88"
    )


def test_post_scroll_workspace_uses_local_dimension_design_system(monkeypatch):
    _patch_mesh_inputs(monkeypatch)

    html = viewer.build_page(
        {"case_id": "CASE-5"}, None, _report(geometry_fail=True), server_mode=True
    )

    assert 'id="dimension-workspace"' in html
    assert 'id="clinical-interface"' not in html
    assert html.count("data:font/woff2;base64,") == 3
    assert "{{DM_SANS_FONT}}" not in html
    assert "{{GEIST_FONT}}" not in html
    assert "{{GEIST_MONO_FONT}}" not in html
    assert '--dim-canvas:#e7f1f7' in html
    assert '--dim-glass:rgba(255,255,255,.68)' in html
    assert '--dim-solid:#f8fbfd' in html
    assert '--dim-ink:#122331' in html
    assert '--dim-muted:#5d7281' in html
    assert '--dim-hairline:rgba(18,35,49,.12)' in html
    assert '--dim-blue:#82a9cd' in html
    assert '--dim-action:#203746' in html
    assert '.workbench .coverage-grid{display:block' in html
    assert '.workbench .export-buttons #export-step{grid-column:1/span 2' in html
    assert "function safeRationale(text,g,s)" in html
    assert 'replace(/\\b21\\s*\\/\\s*21\\b/gi,authoritative)' in html
    assert 'id="bone-file"' in html
    assert 'id="dropzone"' in html
    assert 'id="case-review"' in html
    assert 'id="start-run"' in html
    assert 'id="approve-dialog"' in html
    assert 'id="revision-dialog"' in html
    assert "21/21" not in html


def test_post_scroll_font_assets_are_pinned_and_licensed():
    expected = {
        "DMSans-Variable.woff2": "ca72d2bcea8f4daa783dbdfa2d9b46068c3ce38168e05918fb867aa453b4f890",
        "Geist-Variable.woff2": "a369fcf5628ea2aa4e1b9e2ec6a5b3624e365bda588e1f0f2f12b564f728fbb8",
        "GeistMono-Variable.woff2": "fba8f577f38a2bbcbe818efa6348dd58f36303a10b8737c42fefad275be563ab",
    }

    for path in viewer.POST_SCROLL_FONTS.values():
        assert path.exists()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected[path.name]

    for license_name in ("OFL-DM-Sans.txt", "OFL-Geist.txt"):
        license_text = (viewer.FONT_ASSET_DIR / license_name).read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE Version 1.1" in license_text


def test_landing_visual_contract_remains_unchanged(monkeypatch):
    _patch_mesh_inputs(monkeypatch)

    html = viewer.build_page(
        {"case_id": "CASE-6"}, None, _report(geometry_fail=True), server_mode=True
    )

    assert (
        ':root{--landing-ink:#122331;--landing-white:#f8fbfd;'
        '--landing-blue:#82a9cd;--landing-blue-deep:#7199bf;'
        '--landing-blue-pale:#dceaf4' in html
    )
    assert '.landing-story{position:relative;min-height:300dvh' in html
    assert (
        'cameraFrames=[{az:-34,el:-8,zoom:1.08,tx:0,ty:0},'
        '{az:24,el:10,zoom:1.18,tx:0,ty:-8},'
        '{az:96,el:-5,zoom:1.11,tx:0,ty:4}]' in html
    )
    assert html.count('class="story-beat" data-beat=') == 3
