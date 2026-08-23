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
import base64
import json
import sys
from functools import lru_cache
from pathlib import Path

from . import case_io
from .contracts import Report
from .validators.fea import CHECK_IDS as FEA_CHECK_IDS
from .validators.fea import FIELD_NAME as STRESS_FIELD_NAME
from .validators.stress import CHECK_IDS as STRESS_CHECK_IDS

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "viewer_template.html"
LANDING_BONE_ASSET = Path(__file__).resolve().parent / "assets" / "human_femur_hero.stl"
FONT_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
POST_SCROLL_FONTS = {
    "{{DM_SANS_FONT}}": FONT_ASSET_DIR / "DMSans-Variable.woff2",
    "{{GEIST_FONT}}": FONT_ASSET_DIR / "Geist-Variable.woff2",
    "{{GEIST_MONO_FONT}}": FONT_ASSET_DIR / "GeistMono-Variable.woff2",
}

# Display budgets. Well below what the validators measure on -- this is for
# looking at, and the sort in the renderer is per-frame.
BONE_FACE_BUDGET = 4000
IMPLANT_FACE_BUDGET = 6000
LANDING_BONE_FACE_BUDGET = 10000

# Rounding on exported coordinates. 0.01 mm is far finer than any threshold in
# the case and roughly halves the payload against full float repr.
COORD_DECIMALS = 2

BONE_COLOR = "#d8cfc0"
IMPLANT_COLOR = "#8fa3b0"
LANDING_BONE_COLOR = "#e7e0d4"


@lru_cache(maxsize=None)
def _font_data(path: Path) -> str:
    """Inline post-scroll fonts so exported review pages still work offline."""
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _status_counts(checks: list[dict]) -> dict[str, int]:
    """Count every report state explicitly; SKIP is never folded into PASS."""
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "ERROR": 0, "TOTAL": len(checks)}
    for check in checks:
        status = str(check.get("status", "ERROR")).upper()
        counts[status if status in counts else "ERROR"] += 1
    return counts


def _case_payload(case: dict) -> dict:
    """The locked inputs a surgeon reviews before the autonomous loop starts."""
    return {
        "id": case.get("case_id", "case"),
        "provenance": case.get("provenance", ""),
        "material": case.get("material", {}),
        "envelope": case.get("envelope", {}),
        "load_cases": case.get("load_cases", []),
        "thresholds": case.get("thresholds", {}),
        "iteration_budget": case.get("iteration_budget", 8),
        "screws": case_io.load_screws(case),
        "keepouts": case_io.load_keepouts(case),
    }


def _mesh_payload(
    path: str | Path,
    name: str,
    color: str,
    budget: int,
    field: str | Path | None = None,
) -> dict:
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
    payload = {
        "name": name,
        "color": color,
        "v": vertices.flatten().tolist(),
        "f": mesh.faces.flatten().tolist(),
        "faces": int(len(mesh.faces)),
    }
    if field is not None and Path(field).exists():
        payload.update(_field_payload(Path(field), mesh))
    return payload


def _field_payload(field_path: Path, mesh) -> dict:
    """Resample a solver stress field onto the display mesh's vertices.

    The solver writes the field on the full exported surface; what reaches the
    browser is a decimated copy of that surface, so the values are carried across
    by nearest vertex. That is a display-only approximation -- the number the
    validator judged is the peak in the report, not what a pixel is coloured by --
    and the peak is passed through separately so the legend cannot understate it.
    """
    import numpy as np
    from scipy.spatial import cKDTree

    field = json.loads(field_path.read_text())
    values = np.asarray(field["von_mises_MPa"], dtype=float)
    source = np.asarray(field["vertices"], dtype=float).reshape(-1, 3)
    sampled = values[cKDTree(source).query(np.asarray(mesh.vertices))[1]]
    return {
        "field": [round(float(v), 2) for v in sampled],
        "field_unit": field.get("unit", "MPa"),
        "field_max": round(float(field.get("max_MPa", values.max())), 1),
        "field_limit": field.get("allowable_MPa") or None,
        "field_note": field.get("note", ""),
    }


def iteration_payload(
    report: Report,
    context: dict | None = None,
    artifacts: dict | None = None,
) -> dict:
    """Browser/API representation of one independently validated iteration."""
    checks = [
        {
            "id": check.id,
            "status": check.status,
            "value": check.value,
            "limit": check.limit,
            "unit": check.unit,
            "location": check.location,
            "message": check.message,
        }
        for check in report.checks
    ]
    # Both stress lanes count as stress coverage: the beam surrogate and the
    # solver report different check ids, and grouping either with geometry would
    # let a stress SKIP read as geometry convergence.
    stress_ids = set(STRESS_CHECK_IDS) | set(FEA_CHECK_IDS)
    geometry_counts = _status_counts([c for c in checks if c["id"] not in stress_ids])
    stress_counts = _status_counts([c for c in checks if c["id"] in stress_ids])
    # A solved stress FAIL is a real result, so it cannot read as convergence; a
    # stress SKIP still can, because a case with no declared load has nothing to
    # solve and the UI labels that coverage separately.
    geometry_converged = bool(geometry_counts["TOTAL"]) and not (
        geometry_counts["FAIL"]
        or geometry_counts["ERROR"]
        or geometry_counts["SKIP"]
        or stress_counts["FAIL"]
        or stress_counts["ERROR"]
    )
    defaults = {
        "number": report.iteration,
        "label": "Baseline" if report.iteration == 0 else f"Iteration {report.iteration}",
        "rationale": "Baseline design: no autonomous geometry edit has been committed yet.",
        "commit_sha": "",
        "session_url": "",
        "topology_changed": False,
    }
    for key in ("rationale", "commit_sha", "session_url", "topology_changed"):
        if key in report.meta:
            defaults[key] = report.meta[key]
    if context:
        defaults.update({key: value for key, value in context.items() if value is not None})
    return {
        **defaults,
        "checks": checks,
        "report": report.to_dict(),
        "coverage": {"geometry": geometry_counts, "stress": stress_counts},
        "geometry_converged": geometry_converged,
        "artifacts": artifacts or {},
    }


def build_page(
    case: dict,
    implant_path: str | Path | None,
    report: Report | None,
    title: str | None = None,
    iteration_context: dict | None = None,
    server_mode: bool = False,
) -> str:
    """The finished HTML, with geometry and report inlined."""
    meshes = [
        _mesh_payload(case_io.bone_path(case), "bone", BONE_COLOR, BONE_FACE_BUDGET)
    ]
    if implant_path and Path(implant_path).exists():
        meshes.append(
            _mesh_payload(
                implant_path,
                "implant",
                IMPLANT_COLOR,
                IMPLANT_FACE_BUDGET,
                field=Path(implant_path).with_name(STRESS_FIELD_NAME),
            )
        )

    empty = Report(iteration=0)
    iteration = iteration_payload(report or empty, iteration_context)
    checks = iteration["checks"]
    geometry_counts = iteration["coverage"]["geometry"]
    stress_counts = iteration["coverage"]["stress"]
    geometry_converged = iteration["geometry_converged"]

    verdict = "GEOMETRY CONVERGED" if geometry_converged else "ENGINEERING"

    meta_bits = [f"{m['faces']} tri {m['name']}" for m in meshes]
    if report is not None and report.meta.get("volume_mm3"):
        meta_bits.append(f"{report.meta['volume_mm3']:.0f} mm³")
    if report is not None and report.iteration:
        meta_bits.insert(0, f"iteration {report.iteration}")

    implant_name = Path(implant_path).name if implant_path else "implant.stl"
    step_name = str(Path(implant_name).with_suffix(".step"))
    iteration["artifacts"] = {
        "stl": implant_name,
        "step": step_name,
        "report": "report.json",
    }
    payload = {
        "case": _case_payload(case),
        "verdict": verdict,
        "meshes": meshes,
        "landing_mesh": _mesh_payload(
            LANDING_BONE_ASSET,
            "landing-femur",
            LANDING_BONE_COLOR,
            LANDING_BONE_FACE_BUDGET,
        ),
        "checks": checks,
        "coverage": iteration["coverage"],
        "iterations": [iteration],
        "active_iteration": 0,
        "server_mode": server_mode,
        "review": {
            "geometry_converged": geometry_converged,
            "prototype_only": True,
            "stress_skipped": stress_counts["SKIP"],
        },
    }
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
        "{{PASS_COUNT}}": str(geometry_counts["PASS"]),
        "{{TOTAL_COUNT}}": str(geometry_counts["TOTAL"]),
        "{{META}}": " · ".join(meta_bits),
        **{token: _font_data(path) for token, path in POST_SCROLL_FONTS.items()},
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
    ap.add_argument("--rationale", help="verbatim engineering rationale / commit message")
    ap.add_argument("--commit-sha", help="git commit that produced this solid")
    ap.add_argument("--session-url", help="live Devin session for this iteration")
    ap.add_argument(
        "--topology-changed",
        action="store_true",
        help="mark this iteration as a structural geometry change",
    )
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

    context = {
        "rationale": args.rationale,
        "commit_sha": args.commit_sha,
        "session_url": args.session_url,
        "topology_changed": True if args.topology_changed else None,
    }
    html = build_page(
        case,
        args.implant,
        report,
        title=args.title,
        iteration_context=context,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
