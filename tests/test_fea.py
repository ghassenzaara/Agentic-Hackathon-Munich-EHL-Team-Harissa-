"""The FEA solver is checked against closed-form elasticity, not against itself.

A stress number nobody can reproduce by hand is worse than a SKIP, so the solver
is pinned to three things with known answers: uniform tension (which Tet4
integrates exactly), cantilever tip deflection (Euler-Bernoulli, where Tet4 is
expected to come out *stiff*), and rigid-body restraint. The validator tests then
cover the honest-reporting behaviour -- SKIP where there is no load, a field
written for the viewer, and a real case solved end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import trimesh

from autoimplants import case_io, fea
from autoimplants.validators import fea as fea_validator

E_MPA = 114_000.0
NU = 0.34


def box_mesh(
    length: float, width: float, height: float, n: tuple[int, int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """A structured Tet4 mesh of a box: six tets per hexahedral cell.

    Built here rather than by gmsh so the solver tests exercise the solver alone
    and run in milliseconds.
    """
    nx, ny, nz = n
    xs, ys, zs = (
        np.linspace(0.0, length, nx + 1),
        np.linspace(0.0, width, ny + 1),
        np.linspace(0.0, height, nz + 1),
    )
    grid = np.stack(np.meshgrid(xs, ys, zs, indexing="ij"), axis=-1)
    nodes = grid.reshape(-1, 3)

    def node_id(i, j, k):
        return (i * (ny + 1) + j) * (nz + 1) + k

    tets = []
    for i in range(nx):
        for j in range(ny):
            for k in range(nz):
                c = [
                    node_id(i, j, k), node_id(i + 1, j, k),
                    node_id(i + 1, j + 1, k), node_id(i, j + 1, k),
                    node_id(i, j, k + 1), node_id(i + 1, j, k + 1),
                    node_id(i + 1, j + 1, k + 1), node_id(i, j + 1, k + 1),
                ]
                # Kuhn decomposition: six tets sharing the cell's main diagonal.
                for a, b, c2, d in (
                    (0, 1, 2, 6), (0, 2, 3, 6), (0, 3, 7, 6),
                    (0, 7, 4, 6), (0, 4, 5, 6), (0, 5, 1, 6),
                ):
                    tets.append([c[a], c[b], c[c2], c[d]])
    return nodes, np.array(tets, dtype=np.int64)


def test_uniform_tension_recovers_force_over_area():
    """sigma = F/A, and the elongation is F*L/AE to machine precision.

    The stress is not asked to be exact to machine precision even though Tet4
    integrates a uniform state exactly: fixing all three DOF at the clamp forbids
    Poisson contraction there, and that disturbance decays over roughly one width.
    A percent is the honest tolerance; the elongation, being an integral, is not
    disturbed and is checked tightly.
    """
    length, width, height = 40.0, 10.0, 5.0
    nodes, tets = box_mesh(length, width, height, (8, 2, 2))
    force_n = 2000.0

    fixed = np.flatnonzero(nodes[:, 0] <= 1e-9)
    loaded = np.flatnonzero(nodes[:, 0] >= length - 1e-9)
    forces = np.zeros_like(nodes)
    forces[loaded, 0] = force_n / len(loaded)

    (displacement, von_mises), = fea.solve(nodes, tets, E_MPA, NU, fixed, forces)

    expected = force_n / (width * height)
    interior = np.flatnonzero((nodes[:, 0] > 12.0) & (nodes[:, 0] < length - 12.0))
    assert von_mises[interior] == pytest.approx(expected, rel=0.02)

    assert displacement[loaded, 0].mean() == pytest.approx(
        force_n * length / (width * height * E_MPA), rel=0.01
    )


def test_cantilever_bends_toward_beam_theory_as_the_mesh_refines():
    """Below PL^3/3EI, and closer to it on the finer mesh.

    Tet4 is famously stiff in bending, so a single coarse solve landing under the
    beam answer proves nothing by itself -- it could be under it for being wrong.
    Convergence *upward* under refinement is the property that distinguishes a
    correct stiff element from a broken one. Landing above the beam answer would be
    the alarming direction: too soft means deflection is over-predicted while the
    stress that matters is smeared away.
    """
    length, width, height = 60.0, 8.0, 4.0
    tip_n = 100.0

    solved = []
    for divisions in ((12, 2, 2), (30, 5, 5)):
        nodes, tets = box_mesh(length, width, height, divisions)
        fixed = np.flatnonzero(nodes[:, 0] <= 1e-9)
        loaded = np.flatnonzero(nodes[:, 0] >= length - 1e-9)
        forces = np.zeros_like(nodes)
        forces[loaded, 2] = tip_n / len(loaded)

        (displacement, _), = fea.solve(nodes, tets, E_MPA, NU, fixed, forces)
        solved.append(float(np.abs(displacement[loaded, 2]).mean()))

    inertia = width * height**3 / 12.0
    beam = tip_n * length**3 / (3.0 * E_MPA * inertia)
    assert solved[0] < solved[1] <= beam
    assert solved[1] > 0.5 * beam


def test_an_unrestrained_part_is_refused_rather_than_solved():
    nodes, tets = box_mesh(10.0, 10.0, 10.0, (2, 2, 2))
    with pytest.raises(ValueError, match="no node is restrained"):
        fea.solve(nodes, tets, E_MPA, NU, np.array([], dtype=int), np.zeros_like(nodes))


def test_load_cases_share_one_factorisation_and_stay_independent():
    """A stack of load cases must give what solving them one at a time gives.

    This is the shortcut that makes two bending planes cost one solve, so it is
    worth pinning: reuse of the factorisation must not leak state between cases.
    """
    # Deliberately not square in section, so the two planes are different problems.
    nodes, tets = box_mesh(30.0, 8.0, 3.0, (6, 2, 2))
    fixed = np.flatnonzero(nodes[:, 0] <= 1e-9)
    tip = np.flatnonzero(nodes[:, 0] >= 30.0 - 1e-9)

    cases = []
    for axis in (1, 2):
        forces = np.zeros_like(nodes)
        forces[tip, axis] = 50.0 / len(tip)
        cases.append(forces)

    stacked = fea.solve(nodes, tets, E_MPA, NU, fixed, np.stack(cases))
    assert len(stacked) == 2
    for forces, (_, field) in zip(cases, stacked):
        (_, alone), = fea.solve(nodes, tets, E_MPA, NU, fixed, forces)
        assert field == pytest.approx(alone)
    # The two planes are genuinely different problems, not a repeated answer.
    assert stacked[0][1].max() != pytest.approx(stacked[1][1].max(), rel=1e-3)


def test_tetrahedralize_refines_a_coarse_surface_and_conserves_volume(tmp_path: Path):
    """A box arrives as 12 triangles; the mesh must still resolve its thickness.

    gmsh will happily fill that with a handful of tets, which would report a wall as
    a single element. The refinement loop is what prevents it, and volume is the
    check that refinement did not distort the part.
    """
    box = trimesh.creation.box(extents=(20.0, 10.0, 6.0))
    path = tmp_path / "box.stl"
    box.export(path)

    nodes, tets = fea.tetrahedralize(path, 2.0)
    assert 100 < len(tets) <= fea.MAX_ELEMENTS
    volume = fea._shape_gradients(nodes, tets)[1].sum()
    assert volume == pytest.approx(box.volume, rel=0.02)


def test_tetrahedralize_refuses_an_open_surface(tmp_path: Path):
    sheet = trimesh.Trimesh(
        vertices=[[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]],
        faces=[[0, 1, 2], [0, 2, 3]],
    )
    path = tmp_path / "sheet.stl"
    sheet.export(path)
    with pytest.raises(Exception):
        fea.tetrahedralize(path, 2.0)


def test_load_path_splits_the_screws_along_their_own_span():
    screws = [
        {
            "entry_mm": [0.0, 0.0, z],
            "direction": [1.0, 0.0, 0.0],
            "diameter_mm": 4.0,
            "length_mm": 20.0,
        }
        for z in (0.0, 10.0, 40.0, 50.0)
    ]
    # Nodes on every bore wall, plus one far away that belongs to neither.
    nodes = np.array(
        [[1.0, 2.0, z] for z in (0.0, 10.0, 40.0, 50.0)] + [[0.0, 40.0, 25.0]]
    )

    fixed, loaded, axis, lever = fea.load_path(nodes, screws)
    assert abs(axis @ np.array([0.0, 0.0, 1.0])) == pytest.approx(1.0)
    assert set(fixed.tolist()) == {0, 1}
    assert set(loaded.tolist()) == {2, 3}
    assert lever == pytest.approx(40.0)


def test_a_single_cluster_of_screws_has_no_load_path():
    screws = [
        {
            "entry_mm": [0.0, 0.0, 0.0],
            "direction": [1.0, 0.0, 0.0],
            "diameter_mm": 4.0,
            "length_mm": 10.0,
        }
    ] * 3
    with pytest.raises(ValueError, match="no load path"):
        fea.load_path(np.array([[1.0, 1.0, 0.0]]), screws)


def test_moment_becomes_a_transverse_force_over_the_lever():
    axis = np.array([0.0, 0.0, 1.0])
    transverse, _ = fea.transverse_axes(axis)
    forces = fea.nodal_forces(
        5, np.array([1, 2]), axis, transverse, moment_nmm=7000.0, axial_n=2100.0,
        lever_mm=70.0
    )
    total = forces.sum(axis=0)
    assert total @ axis == pytest.approx(2100.0)
    assert total @ transverse == pytest.approx(100.0)  # 7000 N*mm over a 70 mm lever
    assert not forces[[0, 3, 4]].any()


def test_element_size_follows_the_wall_not_the_part():
    fine = fea.element_size({"thresholds": {"min_wall_mm": 1.5}}, 120.0)
    coarse = fea.element_size({"thresholds": {"min_wall_mm": 4.0}}, 120.0)
    assert fine < coarse
    assert fine <= 1.5 / fea.ELEMENTS_THROUGH_WALL


# --- validator behaviour -----------------------------------------------------


def test_stress_is_skipped_when_the_case_declares_no_loads(tmp_path: Path):
    case = {"thresholds": {"max_stress_MPa": 350.0}}
    report = fea_validator.validate(str(tmp_path / "nothing.stl"), case)
    assert {c.status for c in report.checks} == {"SKIP"}
    assert all("no load" in c.message for c in report.checks)


def test_the_demo_plate_is_solved_and_a_heatmap_field_is_written(tmp_path: Path):
    """End to end on the case the repo ships: a number, a field, and a location."""
    from autoimplants import export, generator, params

    case = case_io.load_case(Path("inputs/case.json"))
    case = case_io.set_active_case(case, Path("inputs/case.json"))
    solid = generator.build_implant(params.default_params())
    implant = export.export_implant(solid, tmp_path / "implant")

    report = fea_validator.validate(str(implant), case)
    peak = next(c for c in report.checks if c.id == "fea_max_von_mises")
    assert peak.status in ("PASS", "FAIL")
    assert 0.0 < peak.value < 5_000.0
    assert peak.location is not None
    assert report.meta["elements"] > 1_000

    field = json.loads((tmp_path / fea_validator.FIELD_NAME).read_text())
    surface = trimesh.load(implant, force="mesh")
    assert len(field["von_mises_MPa"]) == len(surface.vertices)
    assert len(field["vertices"]) == 3 * len(surface.vertices)
    assert field["max_MPa"] == pytest.approx(max(field["von_mises_MPa"]), rel=1e-3)


def test_a_thinner_section_is_reported_as_more_stressed():
    """The field has to respond to section, or it is decoration.

    Same load, less depth: bending stress goes as 1/h^2, so halving the depth must
    raise the peak severalfold. Checked on a plain beam rather than on two built
    implants -- the plate's legal thickness window is a few tenths of a millimetre
    wide, which is too little section change to distinguish from mesh noise.
    """
    peaks = []
    for height in (8.0, 4.0):
        nodes, tets = box_mesh(48.0, 8.0, height, (12, 2, 2))
        fixed = np.flatnonzero(nodes[:, 0] <= 1e-9)
        tip = np.flatnonzero(nodes[:, 0] >= 48.0 - 1e-9)
        forces = np.zeros_like(nodes)
        forces[tip, 2] = 200.0 / len(tip)
        (_, field), = fea.solve(nodes, tets, E_MPA, NU, fixed, forces)
        # Away from the clamp, whose stress is singular by construction.
        mid = np.flatnonzero(nodes[:, 0] > 12.0)
        peaks.append(float(field[mid].max()))

    assert peaks[1] > 3.0 * peaks[0]


def test_an_overloaded_case_fails_so_the_iteration_loop_has_something_to_fix(
    tmp_path: Path,
):
    """A FAIL here is what makes the design loop run, so it must be reachable.

    The same solid is judged against an allowable it cannot meet; the field is still
    written, because the location of the overload is what the next iteration needs.
    """
    from autoimplants import export, generator, params

    case = case_io.load_case(Path("inputs/case.json"))
    case = case_io.set_active_case(case, Path("inputs/case.json"))
    solid = generator.build_implant(params.default_params())
    implant = export.export_implant(solid, tmp_path / "implant")

    overloaded = {
        **case,
        "thresholds": {**case["thresholds"], "max_stress_MPa": 1.0},
    }
    report = fea_validator.validate(str(implant), overloaded)
    peak = next(c for c in report.checks if c.id == "fea_max_von_mises")
    assert peak.status == "FAIL"
    assert peak.value > 1.0 and peak.limit == 1.0
    assert peak.location is not None
    assert (tmp_path / fea_validator.FIELD_NAME).exists()
