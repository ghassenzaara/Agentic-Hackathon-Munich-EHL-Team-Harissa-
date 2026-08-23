from __future__ import annotations

from autoimplants.contracts import FAIL, Check, Report
from harness.loop import render_patch_prompt


def _report() -> Report:
    return Report.from_checks(
        [Check(id="implant_mass", status=FAIL, value=61.2, limit=55.0, unit="g")],
        iteration=1,
        params={"peak_wall_mm": 4.5},
    )


def test_the_patch_prompt_tells_the_agent_where_to_post_and_never_to_commit():
    prompt = render_patch_prompt(
        2,
        _report(),
        ["iter 1: raised the pad over the distal bores"],
        "http://127.0.0.1:8000/api/patch/token-abc",
        ["autoimplants/generator.py", "autoimplants/params.py"],
        "REAL-CT-001",
    )
    assert "{{" not in prompt  # every placeholder is substituted
    assert "http://127.0.0.1:8000/api/patch/token-abc" in prompt
    assert "REAL-CT-001" in prompt
    assert "implant_mass" in prompt
    assert "peak_wall_mm" in prompt
    assert "raised the pad over the distal bores" in prompt
    assert "`autoimplants/generator.py`" in prompt
    assert "must not commit, push or open a" in prompt
    # The template header is documentation for us, not instructions for the agent.
    assert "str.replace" not in prompt


def test_surgeon_feedback_is_appended_as_an_immutable_requirement():
    prompt = render_patch_prompt(
        1,
        _report(),
        [],
        "http://localhost:8000/api/patch/t",
        ["autoimplants/generator.py"],
        "SYNTH-FEMUR-001",
        feedback="Reduce the proximal prominence.",
    )
    assert "nothing yet -- first iteration" in prompt
    assert "Surgeon revision requirement" in prompt
    assert "Reduce the proximal prominence." in prompt
