"""Shared fixtures. The synthetic case is the ground truth every test measures against."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUTS = REPO_ROOT / "inputs"
EXAMPLE = REPO_ROOT / "real_cases" / "example"


@pytest.fixture(autouse=True)
def _reset_active_case():
    """No test may leak its active case into the next one.

    case_io holds the active case in module state so the frozen
    build_implant(params) signature can still find the right screw file. That
    state is exactly the kind of thing that makes tests pass in isolation and
    fail in a suite, so it is cleared around every test.
    """
    from autoimplants import case_io

    case_io._ACTIVE = None
    yield
    case_io._ACTIVE = None


@pytest.fixture
def synthetic_case() -> dict:
    return json.loads((INPUTS / "case.json").read_text(encoding="utf-8"))


@pytest.fixture
def synthetic_screws() -> list[dict]:
    data = json.loads((INPUTS / "screw_positions.json").read_text(encoding="utf-8"))
    return data["screws"]


@pytest.fixture
def example_plan_path() -> Path:
    path = EXAMPLE / "surgical_plan.json"
    if not path.exists():
        pytest.skip("run real_cases/example/make_example.py to generate the example case")
    return path


@pytest.fixture
def example_bone_path() -> Path:
    path = EXAMPLE / "bone.stl"
    if not path.exists():
        pytest.skip("run real_cases/example/make_example.py to generate the example case")
    return path


@pytest.fixture
def example_plan(example_plan_path) -> dict:
    return json.loads(example_plan_path.read_text(encoding="utf-8"))
