"""The stress validator, which until now returned SKIP for everything.

A stress model inside an autonomous loop has two jobs beyond being roughly right:
it must be *stable*, because a number that moves when the design has not is noise
the loop will chase, and it must *respond to geometry*, because otherwise it
cannot reward the engineering it exists to provoke. Both are tested here.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from autoimplants import case_io
from autoimplants.section import sections
from autoimplants.validators import stress

W, T, L = 16.0, 3.0, 180.0
SCREW_ZS = [115.0, 145.0, 175.0, 205.0, 235.0, 265.0]


@pytest.fixture
def plain_bar(tmp_path):
    """A featureless bar spanning the synthetic footprint, no holes."""
    bar = trimesh.creation.box(extents=(T, W, L))
    bar.apply_translation([35.0, 0.0, 190.0])
    path = tmp_path / "bar.stl"
    bar.export(str(path))
    return bar, str(path)


@pytest.fixture
def case(synthetic_case):
    case_io.set_active_case(synthetic_case, case_io.DEFAULT_CASE_PATH)
    return synthetic_case


# -- the model itself ---------------------------------------------------------


def test_peak_stress_matches_beam_theory(plain_bar):
    """sigma = M*c/I for a rectangle, with no axial load."""
    bar, _ = plain_bar
    secs = sections(bar, n_stations=5)
    moment = 12_000.0  # N*mm
    moments = np.full(len(secs), moment)

    peak, section, axis = stress._peak_stress(secs, moments, axial_n=0.0)
    expected = moment * (T / 2.0) / (W * T**3 / 12.0)

    assert np.isclose(peak, expected, rtol=5e-3)
    assert "thin" in axis


def test_axial_load_adds_uniformly(plain_bar):
    bar, _ = plain_bar
    secs = sections(bar, n_stations=5)
    moments = np.zeros(len(secs))

    peak, _, _ = stress._peak_stress(secs, moments, axial_n=2100.0)
    assert np.isclose(peak, 2100.0 / (W * T), rtol=5e-3)


def test_moment_tapers_from_mid_span_to_the_end_screws():
    zs = np.array([115.0, 152.5, 190.0, 227.5, 265.0])
    profile = stress._moment_profile(zs, z_mid=190.0, half_span=75.0, peak_nmm=15_000.0)

    assert np.isclose(profile[2], 15_000.0)          # over the fracture
    assert np.isclose(profile[0], 0.0, atol=1e-6)     # at the outermost screws
    assert np.isclose(profile[1], 7_500.0, rtol=1e-6)
    assert profile[1] == pytest.approx(profile[3])    # symmetric


def test_hole_kt_is_bounded_and_monotonic():
    assert np.isclose(stress.hole_kt(0.0, 16.0), 3.0)      # vanishing hole
    wide = stress.hole_kt(4.5, 16.0)
    narrow = stress.hole_kt(4.5, 10.0)
    assert 2.0 < wide < 3.0
    # A hole taking more of the width concentrates harder.
    assert narrow < wide or np.isclose(narrow, wide)


# -- behaviour inside the loop ------------------------------------------------


def test_report_is_stable_against_station_count(case):
    """This is a regression: the peak moved 669 -> 765 MPa with n_stations alone,
    because whether a station landed on a hole centre was left to the grid."""
    import autoimplants.section as section_module

    implant = "out/implant.stl"
    if not __import__("pathlib").Path(implant).exists():
        pytest.skip("no exported implant; run autoimplants.run first")

    original = section_module.N_STATIONS
    try:
        values = []
        for n in (21, 41, 81):
            section_module.N_STATIONS = n
            report = stress.validate(implant, case)
            values.append(report.by_id("stress_max_bending").value)
    finally:
        section_module.N_STATIONS = original

    assert len(set(values)) == 1, f"peak stress depends on station count: {values}"


def test_stress_responds_to_added_section(tmp_path, case):
    """Material where the moment is must lower the number, or the model cannot
    reward engineering and is not worth shipping."""
    plain = trimesh.creation.box(extents=(T, W, L))
    plain.apply_translation([35.0, 0.0, 190.0])

    rib = trimesh.creation.box(extents=(T + 3.0, W, 40.0))
    rib.apply_translation([35.0 + 1.5, 0.0, 190.0])
    ribbed = trimesh.boolean.union([plain, rib])

    a = tmp_path / "plain.stl"
    b = tmp_path / "ribbed.stl"
    plain.export(str(a))
    ribbed.export(str(b))

    plain_peak = stress.validate(str(a), case).by_id("stress_max_bending").value
    ribbed_peak = stress.validate(str(b), case).by_id("stress_max_bending").value

    assert ribbed_peak < plain_peak, (plain_peak, ribbed_peak)


def test_every_check_id_is_still_emitted(plain_bar, case):
    """The prompt and DOMAIN_KNOWLEDGE refer to these by name."""
    _, path = plain_bar
    report = stress.validate(path, case)
    assert {c.id for c in report.checks} == set(stress.CHECK_IDS)


def test_pullout_is_skipped_for_a_stated_reason(plain_bar, case):
    """Pull-out depends on bone and screw, both locked inputs. SKIP is the honest
    answer -- but the message must say why, not read as an unfinished TODO."""
    _, path = plain_bar
    check = stress.validate(path, case).by_id("screw_pullout_min")

    assert check.status == "SKIP"
    assert "not a design variable" in check.message
    assert "not implemented" not in check.message.lower()


def test_empty_mesh_errors_without_crashing_the_loop(tmp_path, case):
    """The loop must survive a bad export.

    `validate()` itself may raise -- the geometry validator does the same -- and
    `run_one` is the contracted safety net that turns it into an ERROR check. The
    guarantee being tested is the one the loop actually relies on.
    """
    from autoimplants.validators import run_one

    empty = tmp_path / "empty.stl"
    empty.write_text("solid empty\nendsolid empty\n", encoding="utf-8")

    report = run_one("stress", str(empty), case)
    assert not report.passed
    assert report.by_id("stress_crashed") is not None
