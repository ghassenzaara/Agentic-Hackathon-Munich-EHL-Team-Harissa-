"""Render a case, its implant and its validation report as a standalone page.

    python -m autoimplants.viewer --case inputs/case.json \\
        --implant out/implant.stl --report out/report.json --out out/viewer.html

Every number the validators produce carries a location, because that is what
lets Devin fix a design instead of guessing at it (see autoimplants/contracts.py).
Those same coordinates are wasted on a human reading a table: "the plate stands
8.60 mm off the bone at y=6.0, z=100" is a sentence, whereas the picture is
immediate. This turns the report into the picture.

The output is one self-contained HTML file -- geometry, report and renderer
inlined, no network, no build step -- so it can be committed alongside a run,
attached to a pull request, opened offline, or dropped into a UI in an iframe.

Deliberately not a CAD viewer. It answers one question per page: what did this
iteration build, and where did it fail. Meshes are decimated to a display budget
because a 130k-triangle CT reconstruction is a slideshow in a canvas and no
sharper at the scale anyone looks at it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import case_io
from .contracts import Report

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "viewer_template.html"

# Display budgets. Well below what the validators measure on -- this is for
# looking at, and the sort in the renderer is per-frame.
BONE_FACE_BUDGET = 4000
IMPLANT_FACE_BUDGET = 6000

# Rounding on exported coordinates. 0.01 mm is far finer than any threshold in
# the case and roughly halves the payload against full float repr.
COORD_DECIMALS = 2

BONE_COLOR = "#d8cfc0"
IMPLANT_COLOR = "#8fa3b0"


def _mesh_payload(path: str | Path, name: str, color: str, budget: int) -> dict:
    import trimesh

    mesh = trimesh.load(str(path), force="mesh")
    if mesh.is_empty:
        raise ValueError(f"{name} mesh at {path} is empty or unreadable")

    if len(mesh.faces) > budget:
        try:
            mesh = mesh.simplify_quadric_decimation(face_count=budget)
        except Exception:
            # Decimation is a nicety here, not a requirement: a heavier page
            # still renders, just less smoothly.
            pass

    vertices = mesh.vertices.round(COORD_DECIMALS)
    return {
        "name": name,
        "color": color,
        "v": vertices.flatten().tolist(),
        "f": mesh.faces.flatten().tolist(),
        "faces": int(len(mesh.faces)),
    }


def build_page(
    case: dict,
    implant_path: str | Path | None,
    report: Report | None,
    title: str | None = None,
) -> str:
    """The finished HTML, with geometry and report inlined."""
    meshes = [
        _mesh_payload(case_io.bone_path(case), "bone", BONE_COLOR, BONE_FACE_BUDGET)
    ]
    if implant_path and Path(implant_path).exists():
        meshes.append(
            _mesh_payload(implant_path, "implant", IMPLANT_COLOR, IMPLANT_FACE_BUDGET)
        )

    checks = []
    if report is not None:
        for check in report.checks:
            checks.append(
                {
                    "id": check.id,
                    "status": check.status,
                    "value": check.value,
                    "limit": check.limit,
                    "unit": check.unit,
                    "location": check.location,
                    "message": check.message,
                }
            )

    passing = sum(1 for c in checks if c["status"] in ("PASS", "SKIP"))
    verdict = "PASS" if (report is not None and report.passed) else "FAIL"

    meta_bits = [f"{m['faces']} tri {m['name']}" for m in meshes]
    if report is not None and report.meta.get("volume_mm3"):
        meta_bits.append(f"{report.meta['volume_mm3']:.0f} mm³")
    if report is not None and report.iteration:
        meta_bits.insert(0, f"iteration {report.iteration}")

    payload = {"meshes": meshes, "checks": checks}
    case_id = case.get("case_id", "case")

    # Token substitution, not str.format -- the template is full of CSS and JS
    # braces and escaping every one of them is a source of silent bugs. Same
    # reasoning as harness/loop.py.
    html = TEMPLATE.read_text(encoding="utf-8")
    for token, value in {
        "{{TITLE}}": title or f"{case_id} Implant Review",
        "{{CASE_ID}}": str(case_id),
        "{{VERDICT}}": verdict,
        "{{VERDICT_CLASS}}": verdict.lower(),
        "{{PASS_COUNT}}": str(passing),
        "{{TOTAL_COUNT}}": str(len(checks)),
        "{{META}}": " · ".join(meta_bits),
        # </script> inside the JSON would close the tag holding it.
        "{{DATA}}": json.dumps(payload, separators=(",", ":")).replace("</", "<\\/"),
    }.items():
        html = html.replace(token, value)
    return html


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autoimplants.viewer", description=__doc__)
    ap.add_argument("--case", default=str(REPO_ROOT / "inputs" / "case.json"))
    ap.add_argument("--implant", default="out/implant.stl")
    ap.add_argument("--report", default="out/report.json")
    ap.add_argument("--out", default="out/viewer.html")
    ap.add_argument("--title")
    args = ap.parse_args(argv)

    case_path = Path(args.case)
    if not case_path.exists():
        print(f"case not found: {case_path}", file=sys.stderr)
        return 1
    case = case_io.set_active_case(case_io.load_case(case_path), case_path)

    report = None
    if args.report and Path(args.report).exists():
        report = Report.load(args.report)
    else:
        print(f"[warn] no report at {args.report} -- rendering geometry only", file=sys.stderr)

    html = build_page(case, args.implant, report, title=args.title)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
