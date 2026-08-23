"""The conformal-patch family: does it build a device on anatomy with no axis?

The plate is tested elsewhere against the femur. These tests use a curved cap with
a hole in it -- a defect in a vault, in miniature -- because that is the shape the
plate family structurally cannot handle, and every assertion here is about a
property that must hold on *any* surface: the region follows the plan, the shell
closes, the bores go through, and the result is a solid a manufacturer can read.

Deliberately small meshes: these check geometry logic, and the full-size cranial
case is exercised end to end by real_cases/synthetic_patch.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import trimesh

from autoimplants import case_io, export, generator, params, patch
from autoimplants.validators import geometry as geometry_validator
from autoimplants.validators import stress as stress_validator

RADIUS = 40.0
SCREW_RING = 14.0
DEFECT_RADIUS = 7.0


def cap(radius: float = RADIUS) -> trimesh.Trimesh:
    """An open, doubly curved sheet: the top of a slightly squashed sphere."""
    sphere = trimesh.creation.icosphere(subdivisions=3, radius=radius)
    sphere.apply_scale([1.0, 0.85, 0.95])
    keep = sphere.vertices[:, 2] > 0.2 * radius
    faces = np.flatnonzero(keep[sphere.faces].all(axis=1))
    return sphere.submesh([faces], append=True, repair=False)


def vault(subdivisions: int = 4) -> trimesh.Trimesh:
    """A closed two-table bone: the cap plus an inner table, rim closed.

    Finer than :func:`cap` because a device is actually built on it: a facet the
    size of the wall makes the offset surfaces pinch, which is a property of the
    fixture rather than of the design.
    """
    sphere = trimesh.creation.icosphere(subdivisions=subdivisions, radius=RADIUS)
    sphere.apply_scale([1.0, 0.85, 0.95])
    keep = sphere.vertices[:, 2] > 0.2 * RADIUS
    faces = np.flatnonzero(keep[sphere.faces].all(axis=1))
    return patch.build_shell(sphere, faces, -4.0, 4.0, np.empty((0, 3)))


def screws(n: int = 5, ring: float = SCREW_RING, diameter: float = 2.4) -> list[dict]:
    """Screws around the pole, each driven inward along its own local normal."""
    out = []
    for i in range(n):
        angle = 2.0 * np.pi * i / n
        point = np.array(
            [ring * np.cos(angle), ring * np.sin(angle), RADIUS]
        )
        entry = point / np.linalg.norm(point) * RADIUS
        out.append(
            {
                "id": f"s{i}",
                "entry_mm": entry.tolist(),
                "direction": (-entry / np.linalg.norm(entry)).tolist(),
                "diameter_mm": diameter,
                "length_mm": 6.0,
            }
        )
    return out


# --- region selection --------------------------------------------------------


def test_sphere_region_selects_only_the_declared_neighbourhood():
    bone = cap()
    region = {"type": "sphere", "center_mm": [0.0, 0.0, RADIUS], "radius_mm": 15.0}
    faces = patch.region_faces(bone, region, [])
    centers = bone.triangles_center[faces]
    assert len(faces) < len(bone.faces)
    assert np.linalg.norm(centers - [0.0, 0.0, RADIUS], axis=1).max() <= 15.0


def test_screw_span_region_follows_the_planned_fixation():
    bone = cap()
    plan = screws()
    faces = patch.region_faces(bone, {"type": "screw_span", "margin_mm": 10.0}, plan)
    entries = np.array([s["entry_mm"] for s in plan])
    centers = bone.triangles_center[faces]
    # Every selected face is near some planned screw, and the region spans them all.
    per_face = np.linalg.norm(centers[:, None, :] - entries[None, :, :], axis=2)
    assert per_face.min(axis=1).max() <= 10.0
    assert set(np.argmin(per_face, axis=1)) == set(range(len(plan)))


def test_screw_span_without_screws_is_refused():
    with pytest.raises(ValueError, match="needs planned screws"):
        patch.region_faces(cap(), {"type": "screw_span", "margin_mm": 10.0}, [])


def test_unknown_region_type_is_refused():
    with pytest.raises(ValueError, match="unknown region type"):
        patch.region_faces(cap(), {"type": "freehand"}, [])


def test_region_too_small_to_build_on_is_refused():
    region = {"type": "sphere", "center_mm": [0.0, 0.0, RADIUS], "radius_mm": 0.5}
    with pytest.raises(ValueError, match="too little"):
        patch.region_faces(cap(), region, [])


def test_region_off_the_bone_is_refused_with_a_frame_hint():
    region = {"type": "sphere", "center_mm": [500.0, 0.0, 0.0], "radius_mm": 5.0}
    with pytest.raises(ValueError, match="coordinate frame"):
        patch.region_faces(cap(), region, [])


def test_region_keeps_only_the_side_the_screws_come_from():
    """The margin around a screw reaches the far table; the device cannot sit there.

    On a two-table bone the inner table is millimetres below the entries, so a
    pure distance criterion selects both sides and the shell self-intersects.
    """
    bone = vault()
    plan = screws()
    faces = patch.region_faces(bone, {"type": "screw_span", "margin_mm": 12.0}, plan)
    directions = np.array([s["direction"] for s in plan])
    normals = bone.face_normals[faces]
    # Every kept face opposes the drive direction of some screw: it faces outward.
    assert (normals @ -directions.T).max(axis=1).min() > 0.0
    assert len(faces) < len(
        np.flatnonzero(
            (
                np.linalg.norm(
                    bone.vertices[:, None, :]
                    - np.array([s["entry_mm"] for s in plan])[None, :, :],
                    axis=2,
                ).min(axis=1)
                <= 12.0
            )[bone.faces].all(axis=1)
        )
    )


# --- shell construction ------------------------------------------------------


def test_interior_holes_are_spanned_and_the_outer_rim_is_left_open():
    """A device over a defect must cover the defect, not repeat it."""
    sheet = cap()
    hole_center = np.array([0.0, 0.0, RADIUS])
    keep = np.linalg.norm(sheet.triangles_center - hole_center, axis=1) > DEFECT_RADIUS
    annulus = sheet.submesh([np.flatnonzero(keep)], append=True, repair=False)
    assert len(patch._boundary_loops(patch._boundary_edges(annulus))) == 2

    filled = patch.close_interior_holes(annulus)
    assert len(patch._boundary_loops(patch._boundary_edges(filled))) == 1
    assert filled.area > annulus.area

    # The span follows the bone's curvature rather than lidding it flat: the new
    # vertices sit proud of the hole's rim, where the missing bone was, and they are
    # a mesh rather than one apex -- a fan of slivers is what pinches the wall.
    added = filled.vertices[len(annulus.vertices):]
    assert len(added) > 3
    rim = patch._boundary_loops(patch._boundary_edges(annulus))
    inner = min(rim, key=lambda loop: len(loop))
    assert added[:, 2].max() > annulus.vertices[inner][:, 2].max()

    # And they land on the sphere the surrounding bone lies on, to well under the
    # wall thickness -- that is what "spanning it the way the bone did" means.
    radii = np.linalg.norm(added / [1.0, 0.85, 0.95], axis=1)
    assert abs(radii - RADIUS).max() < 0.5


def test_a_hole_in_a_saddle_is_spanned_by_the_saddle_and_not_by_a_dome():
    """The fit has to be quadratic: a sphere through a saddle bulges the wrong way.

    Anatomy is as often saddle-shaped (a scapular blade, an acetabular wall) as
    domed, and the middle of the span is exactly where a reconstruction shows.
    """
    us, vs = np.meshgrid(
        np.linspace(-30.0, 30.0, 61), np.linspace(-30.0, 30.0, 61), indexing="ij"
    )

    def height(u, v):
        return 0.02 * u**2 - 0.025 * v**2

    vertices = np.column_stack([us.ravel(), vs.ravel(), height(us, vs).ravel()])
    n = us.shape[1]
    i = np.arange(us.shape[0] - 1)[:, None] * n + np.arange(n - 1)[None, :]
    faces = np.vstack(
        [
            np.column_stack([i.ravel(), (i + 1).ravel(), (i + n + 1).ravel()]),
            np.column_stack([i.ravel(), (i + n + 1).ravel(), (i + n).ravel()]),
        ]
    )
    sheet = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    radial = np.linalg.norm(sheet.triangles_center[:, :2], axis=1)
    annulus = sheet.submesh([np.flatnonzero(radial > 9.0)], append=True, repair=False)

    filled = patch.close_interior_holes(annulus)
    added = filled.vertices[len(annulus.vertices):]
    assert len(added)
    error = np.abs(added[:, 2] - height(added[:, 0], added[:, 1]))
    assert error.max() < 0.2, f"span deviates from the bone's own surface: {error.max()}"


def test_offset_normals_are_smoothed_across_a_crease():
    """Two panels meeting at an angle must not fold the offset surface."""
    vertices = np.array(
        [
            [0.0, 0.0, 0.0], [0.0, 1.0, 0.0],
            [1.0, 0.0, 0.0], [1.0, 1.0, 0.0],
            [2.0, 0.0, 1.0], [2.0, 1.0, 1.0],
        ]
    )
    faces = np.array([[0, 2, 3], [0, 3, 1], [2, 4, 5], [2, 5, 3]])
    sheet = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    raw = sheet.vertex_normals
    smoothed = patch._smoothed_normals(sheet)
    spread = lambda normals: float(  # noqa: E731 - local, one use
        np.arccos(np.clip(normals @ normals.T, -1.0, 1.0)).max()
    )
    assert spread(smoothed) < spread(raw)
    assert np.allclose(np.linalg.norm(smoothed, axis=1), 1.0)


def test_shell_is_a_watertight_solid_standing_off_the_bone():
    bone = cap()
    faces = patch.region_faces(
        bone, {"type": "sphere", "center_mm": [0.0, 0.0, RADIUS], "radius_mm": 18.0}, []
    )
    shell = patch.build_shell(bone, faces, 0.3, 2.0, np.empty((0, 3)))
    assert shell.is_watertight and shell.volume > 0.0

    # It stands off the bone by the clearance and is as thick as asked. Smoothing the
    # offset normals costs a few microns of that standoff where the region curves
    # away at its rim, which is two orders of magnitude under the 0.05 mm the
    # validator holds the seat to.
    _, distance, _ = bone.nearest.on_surface(shell.vertices)
    assert distance.min() >= 0.3 - 5e-3
    assert 2.0 <= float(np.ptp(distance)) + 1e-6


def test_wall_thickens_around_the_planned_bores():
    points = np.array([[0.0, 0.0, 0.0], [5.0, 0.0, 0.0], [50.0, 0.0, 0.0]])
    spec = {"base_mm": 2.0, "boss_mm": 1.0, "boss_radius_mm": 10.0}
    thickness = patch._wall(spec, points, np.zeros((1, 3)))
    assert thickness[0] == pytest.approx(3.0)
    assert thickness[1] == pytest.approx(2.5)
    assert thickness[2] == pytest.approx(2.0)


def test_a_closed_region_has_no_rim_to_close_against():
    sphere = trimesh.creation.icosphere(subdivisions=2, radius=10.0)
    with pytest.raises(ValueError, match="no rim"):
        patch.build_shell(
            sphere, np.arange(len(sphere.faces)), 0.3, 1.5, np.empty((0, 3))
        )


def test_drilling_leaves_an_open_bore_and_a_watertight_shell():
    bone = cap()
    plan = screws(n=3, ring=10.0)
    faces = patch.region_faces(bone, {"type": "screw_span", "margin_mm": 12.0}, plan)
    shell = patch.build_shell(bone, faces, 0.3, 2.0, np.empty((0, 3)))
    drilled = patch._drill(shell, plan, 2.4)

    assert drilled.is_watertight
    assert drilled.volume < shell.volume
    for screw in plan:
        entry = np.array(screw["entry_mm"])
        direction = np.array(screw["direction"])
        hits, _, _ = drilled.ray.intersects_location(
            ray_origins=(entry - direction * 200.0)[None, :],
            ray_directions=direction[None, :],
        )
        assert len(hits) == 0, f"bore {screw['id']} is obstructed"


def test_solid_conversion_exports_stl_and_step(tmp_path: Path):
    bone = cap()
    faces = patch.region_faces(
        bone, {"type": "sphere", "center_mm": [0.0, 0.0, RADIUS], "radius_mm": 15.0}, []
    )
    shell = patch.build_shell(bone, faces, 0.3, 2.0, np.empty((0, 3)))
    solid = patch.to_solid(shell)

    assert solid.val().isValid()
    assert solid.val().Volume() == pytest.approx(shell.volume, rel=1e-3)

    written = export.export_implant(solid, tmp_path / "patch")
    assert written.exists() and written.with_suffix(".step").exists()
    assert trimesh.load(written, force="mesh").volume > 0.0


# --- family dispatch ---------------------------------------------------------


def test_generator_dispatches_on_family():
    with pytest.raises(ValueError, match="unknown implant family"):
        generator.build_implant({**params.default_params(), "family": "stem"})


def test_case_declaring_a_family_selects_it():
    case = {"implant": {"family": "conformal_patch", "region": {"type": "sphere"}}}
    adopted = params.for_case(params.default_params(), case)
    assert adopted["family"] == "conformal_patch"
    assert adopted["patch"]["region"] == {"type": "sphere"}


def test_params_asking_for_a_non_default_family_win_over_the_case():
    """A design iteration must be able to try another topology on the same case."""
    base = {**params.default_params(), "family": "conformal_patch"}
    adopted = params.for_case(base, {"implant": {"family": "plate"}})
    assert adopted["family"] == "conformal_patch"


def test_a_case_without_a_declared_family_is_untouched():
    base = params.default_params()
    assert params.for_case(base, {"case_id": "X"}) is base


def test_check_params_rejects_an_unknown_family():
    problems = params.check_params({**params.default_params(), "family": "cage"})
    assert any("family must be" in p for p in problems)


# --- honest reporting on the new anatomy -------------------------------------


def _cranial_case(tmp_path: Path, bone: trimesh.Trimesh, plan: list[dict]) -> dict:
    bone.export(tmp_path / "bone.stl")
    (tmp_path / "screw_positions.json").write_text(json.dumps({"screws": plan}))
    case = {
        "case_id": "TEST-CRANIAL",
        "inputs": {"bone_mesh": "bone.stl", "screw_positions": "screw_positions.json"},
        "implant": {"family": "conformal_patch"},
        "material": {"density_g_cm3": 4.43},
        "envelope": {"max_footprint_mm": 120.0, "max_standoff_mm": 6.0},
        "thresholds": {
            "min_wall_mm": 1.5,
            "max_bone_gap_mm": 1.5,
            "min_bone_gap_mm": 0.05,
            "max_implant_mass_g": 40.0,
            "require_watertight": True,
            "require_all_screws": len(plan),
        },
    }
    path = tmp_path / "case.json"
    path.write_text(json.dumps(case))
    return case_io.set_active_case(case, path)


def test_a_case_that_lists_no_keepouts_is_not_given_another_case_s(tmp_path: Path):
    """The demo femur's zones must not follow an unrelated case around."""
    case = _cranial_case(tmp_path, vault(), screws())
    assert case_io.load_keepouts(case) == []


def test_stress_is_skipped_not_passed_when_the_case_declares_no_loads(tmp_path: Path):
    case = _cranial_case(tmp_path, vault(), screws())
    solid = generator.build_implant(
        params.for_case(params.default_params(), {**case, "implant": {
            "family": "conformal_patch",
            "region": {"type": "screw_span", "margin_mm": 10.0},
        }})
    )
    implant = export.export_implant(solid, tmp_path / "implant")

    report = stress_validator.validate(str(implant), case)
    assert {c.status for c in report.checks} == {"SKIP"}
    assert all("no load" in c.message for c in report.checks)


def test_geometry_validates_a_cranial_device_on_its_own_terms(tmp_path: Path):
    """The plate's +X measurements are replaced, not reused, for a patch."""
    bone = vault()
    plan = screws()
    case = _cranial_case(tmp_path, bone, plan)
    case["implant"]["region"] = {"type": "screw_span", "margin_mm": 10.0}

    solid = generator.build_implant(params.for_case(params.default_params(), case))
    implant = export.export_implant(solid, tmp_path / "implant")
    report = geometry_validator.validate(str(implant), case)

    assert report.meta["implant_family"] == "conformal_patch"
    ids = {c.id for c in report.checks}
    assert {"envelope_footprint", "min_wall_thickness", "bone_conformance_gap"} <= ids
    # No length/width check: this device has no length. Reporting one would be a
    # measurement of the frame, not of the design.
    assert "envelope_length" not in ids

    failed = {c.id: c.message for c in report.checks if c.status == "FAIL"}
    assert not failed, failed


def test_a_resection_wall_is_not_treated_as_a_seating_surface(tmp_path: Path):
    """Conformance is measured on bone the device sits on, not on the cut edge.

    The wall a saw leaves faces sideways across the defect. Measuring a gap along
    it reads the depth of the removed bone, which no design can close.
    """
    bone = vault()
    hole = trimesh.creation.cylinder(
        radius=DEFECT_RADIUS, segment=np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 60.0]])
    )
    bone = trimesh.boolean.difference([bone, hole])
    plan = screws()
    case = _cranial_case(tmp_path, bone, plan)

    loaded = trimesh.load(tmp_path / "bone.stl", force="mesh")
    seats, normals = geometry_validator._seating_normals(loaded, case)
    assert 0 < len(seats) < len(loaded.vertices)

    # No kept vertex is on the bore wall: those sit at the defect radius with a
    # normal pointing across the hole rather than out of the bone.
    kept = loaded.vertices[seats]
    on_wall = (
        np.abs(np.linalg.norm(kept[:, :2], axis=1) - DEFECT_RADIUS) < 0.5
    ) & (np.abs(normals[seats][:, 2]) < 0.3)
    assert not on_wall.any()


def test_standoff_is_not_the_distance_across_a_defect(tmp_path: Path):
    """A device spanning a hole is not protruding by the hole's radius."""
    bone = vault()
    hole = trimesh.creation.cylinder(
        radius=DEFECT_RADIUS, segment=np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 60.0]])
    )
    bone = trimesh.boolean.difference([bone, hole])
    plan = screws()
    case = _cranial_case(tmp_path, bone, plan)
    case["implant"]["region"] = {"type": "screw_span", "margin_mm": 10.0}

    solid = generator.build_implant(params.for_case(params.default_params(), case))
    implant = export.export_implant(solid, tmp_path / "implant")
    checks = {
        c.id: c for c in geometry_validator.check_surface_envelope(
            trimesh.load(implant, force="mesh"), case
        )
    }
    assert checks["envelope_standoff"].value < DEFECT_RADIUS
    assert checks["envelope_standoff"].status == "PASS"
