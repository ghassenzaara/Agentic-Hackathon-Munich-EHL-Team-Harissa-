"""Per-case file resolution, including the fallback that keeps the demo working."""

from __future__ import annotations

import json

import numpy as np
import pytest

from autoimplants import case_io
from autoimplants import run


def test_default_case_resolves_to_the_synthetic_inputs():
    case = case_io.load_case(case_io.DEFAULT_CASE_PATH)
    assert case_io.bone_path(case) == (case_io.REPO_ROOT / "inputs" / "bone.stl").resolve()
    assert len(case_io.load_screws(case)) == 6


def test_case_without_inputs_block_falls_back_to_the_old_paths():
    """The historical hard-coded paths, so a minimal case still runs."""
    assert case_io.bone_path({}) == (case_io.REPO_ROOT / "inputs" / "bone.stl").resolve()


def test_relative_paths_resolve_against_the_case_file(tmp_path):
    """A self-contained real case refers to its own siblings."""
    bone = tmp_path / "bone.stl"
    bone.write_bytes(b"")
    case_path = tmp_path / "case.json"
    case_path.write_text(json.dumps({"inputs": {"bone_mesh": "bone.stl"}}), encoding="utf-8")

    case = case_io.load_case(case_path)
    assert case_io.bone_path(case) == bone.resolve()


def test_absolute_paths_are_used_verbatim(tmp_path):
    target = tmp_path / "elsewhere.stl"
    case = {"inputs": {"bone_mesh": str(target)}}
    assert case_io.bone_path(case) == target


def test_active_case_is_what_the_generator_sees(tmp_path):
    """build_implant() has a frozen signature and never receives the case."""
    screws = {"screws": [{
        "id": "s0", "entry_mm": [1.0, 0.0, 42.0], "direction": [-1.0, 0.0, 0.0],
        "diameter_mm": 4.5, "length_mm": 30.0,
    }]}
    path = tmp_path / "screws.json"
    path.write_text(json.dumps(screws), encoding="utf-8")

    case_io.set_active_case({"inputs": {"screw_positions": str(path)}})
    assert [s["entry_mm"][2] for s in case_io.load_screws()] == [42.0]


def test_screw_directions_are_normalised_on_load(tmp_path):
    """Ray offsets and trajectory lengths are both silently wrong otherwise."""
    screws = {"screws": [{
        "id": "s0", "entry_mm": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 5.0],
        "diameter_mm": 4.5, "length_mm": 30.0,
    }]}
    path = tmp_path / "screws.json"
    path.write_text(json.dumps(screws), encoding="utf-8")

    loaded = case_io.load_screws({"inputs": {"screw_positions": str(path)}})
    assert np.allclose(loaded[0]["direction"], [0.0, 0.0, 1.0])


def test_zero_length_direction_is_an_error(tmp_path):
    screws = {"screws": [{
        "id": "s0", "entry_mm": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 0.0],
        "diameter_mm": 4.5, "length_mm": 30.0,
    }]}
    path = tmp_path / "screws.json"
    path.write_text(json.dumps(screws), encoding="utf-8")

    with pytest.raises(ValueError, match="zero-length"):
        case_io.load_screws({"inputs": {"screw_positions": str(path)}})


def test_missing_keepout_file_means_no_zones(tmp_path):
    case = {"inputs": {"keepout_zones": str(tmp_path / "absent.json")}}
    assert case_io.load_keepouts(case) == []


def test_run_refuses_to_substitute_a_missing_case(tmp_path):
    exit_code = run.main(
        [
            "--case",
            str(tmp_path / "does-not-exist.json"),
            "--validators",
            "stub",
            "--no-build",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert exit_code == 2
