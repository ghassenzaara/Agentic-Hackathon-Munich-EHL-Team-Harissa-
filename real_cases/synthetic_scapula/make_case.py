"""The hard surface case: saddle curvature, a ragged defect, oblique screws.

The cranial case is a fair test of "no shaft axis", but it is still a convex,
almost-symmetric dome, and a dome flatters any offset-based device. This one does
not: it is a scapula-like blade -- concave in one direction and convex in the
other (a saddle, so no single offset direction works), with a raised ridge and a
thin fossa, a bone thickness that varies more than twofold, an irregular defect
that is nothing like a disc, and screws driven obliquely rather than along the
local normal, the way a surgeon angles them into the thicker bone.

    python real_cases/synthetic_scapula/make_case.py
    python -m autoimplants.run --case real_cases/synthetic_scapula/case.json \\
        --validators geometry,stress

Everything here is fabricated analytic geometry, not patient data: it exists to
put the conformal-patch family on a surface that can actually break it. Stress is
undeclared and therefore reported SKIP -- a reconstruction of this class needs FEA
with a real load case, which this repository does not have.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

HERE = Path(__file__).resolve().parent
CASE_ID = "SYNTH-SCAPULA-001"

HALF_U_MM = 52.0          # blade half-extent along the "spine" direction
HALF_V_MM = 40.0          # blade half-extent across it
N_U, N_V = 121, 95        # grid resolution: facets ~0.9 mm, finer than any wall
SADDLE_U_MM = 0.011       # concave along u
SADDLE_V_MM = -0.016      # convex along v -- the saddle
RIDGE_MM = 5.5            # a spine-like crest
FOSSA_MM = 3.0            # secondary undulation, so no single radius fits
THIN_MM, THICK_MM = 2.4, 6.0   # bone thins into the fossa, thickens at the ridge
DEFECT_CENTER_UV = (-8.0, 6.0)
DEFECT_RADIUS_MM = 19.0   # mean radius of a deliberately ragged defect
N_SCREWS = 7
SCREW_TILT_DEG = 22.0     # obliquity: the screws do not follow the local normal


def _height(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """The blade's outer surface: a saddle plus a crest plus an undulation."""
    return (
        SADDLE_U_MM * u**2
        + SADDLE_V_MM * v**2
        + RIDGE_MM * np.exp(-((v + 12.0) ** 2) / 260.0)
        + FOSSA_MM * np.sin(u / 17.0) * np.cos(v / 12.0)
    )


def _thickness(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Bone thickness: thick along the crest, thin in the fossa."""
    crest = np.exp(-((v + 12.0) ** 2) / 300.0)
    return THIN_MM + (THICK_MM - THIN_MM) * np.clip(
        0.35 + 0.65 * crest + 0.2 * np.sin(u / 21.0), 0.0, 1.0
    )


def _sheet() -> tuple[trimesh.Trimesh, np.ndarray]:
    """The outer surface as a triangulated open sheet, with per-vertex thickness."""
    us = np.linspace(-HALF_U_MM, HALF_U_MM, N_U)
    vs = np.linspace(-HALF_V_MM, HALF_V_MM, N_V)
    uu, vv = np.meshgrid(us, vs, indexing="ij")
    vertices = np.column_stack(
        [uu.ravel(), vv.ravel(), _height(uu, vv).ravel()]
    )

    i = np.arange(N_U - 1)[:, None] * N_V + np.arange(N_V - 1)[None, :]
    a, b, c, d = i, i + 1, i + N_V, i + N_V + 1
    faces = np.vstack(
        [
            np.column_stack([a.ravel(), b.ravel(), d.ravel()]),
            np.column_stack([a.ravel(), d.ravel(), c.ravel()]),
        ]
    )
    sheet = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    sheet.fix_normals()
    return sheet, _thickness(uu, vv).ravel()


def blade() -> trimesh.Trimesh:
    """Close the sheet into a solid bone of varying thickness.

    Written out here rather than reusing the implant's own shell builder, because
    that one takes a single wall spec and this bone's thickness is a field. Same
    operation: offset along the vertex normals, stitch the rim.
    """
    sheet, thickness = _sheet()
    normals = sheet.vertex_normals
    outer = sheet.vertices
    inner = outer - normals * thickness[:, None]

    unique, counts = np.unique(sheet.edges_sorted, axis=0, return_counts=True)
    rim = unique[counts == 1]
    n = len(outer)
    walls = np.vstack(
        [
            np.column_stack([rim[:, 0], rim[:, 1], rim[:, 1] + n]),
            np.column_stack([rim[:, 0], rim[:, 1] + n, rim[:, 0] + n]),
        ]
    )
    bone = trimesh.Trimesh(
        vertices=np.vstack([outer, inner]),
        faces=np.vstack([sheet.faces, sheet.faces[:, ::-1] + n, walls]),
    )
    bone.fix_normals()
    return bone


def _surface_point(u: float, v: float) -> np.ndarray:
    uu, vv = np.array([u]), np.array([v])
    return np.array([u, v, float(_height(uu, vv)[0])])


def _surface_normal(u: float, v: float) -> np.ndarray:
    """Outward normal of the analytic surface, by finite difference."""
    h = 0.05
    du = _surface_point(u + h, v) - _surface_point(u - h, v)
    dv = _surface_point(u, v + h) - _surface_point(u, v - h)
    normal = np.cross(du, dv)
    normal /= np.linalg.norm(normal)
    return normal if normal[2] > 0.0 else -normal


def defect(bone: trimesh.Trimesh) -> trimesh.Trimesh:
    """Cut a ragged, non-circular full-thickness hole -- a resection, not a drill.

    A disc is the one defect shape a radially symmetric span handles for free. This
    boundary wobbles by a third of its radius, so the device has to close whatever
    loop the bone actually leaves behind.
    """
    u0, v0 = DEFECT_CENTER_UV
    n = 48
    angles = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    radii = DEFECT_RADIUS_MM * (
        1.0 + 0.30 * np.sin(3.0 * angles) + 0.14 * np.cos(5.0 * angles + 0.7)
    )
    ring = np.column_stack([u0 + radii * np.cos(angles), v0 + radii * np.sin(angles)])

    # Prism, fanned from the centre: the outline is star-shaped about (u0, v0), so
    # a fan is a valid triangulation and no polygon library is needed.
    low, high = -200.0, 200.0
    vertices = np.vstack(
        [
            np.column_stack([ring, np.full(n, low)]),
            np.column_stack([ring, np.full(n, high)]),
            [[u0, v0, low], [u0, v0, high]],
        ]
    )
    nxt = (np.arange(n) + 1) % n
    faces = np.vstack(
        [
            np.column_stack([np.arange(n), nxt, nxt + n]),
            np.column_stack([np.arange(n), nxt + n, np.arange(n) + n]),
            np.column_stack([np.full(n, 2 * n), nxt, np.arange(n)]),
            np.column_stack([np.full(n, 2 * n + 1), np.arange(n) + n, nxt + n]),
        ]
    )
    cutter = trimesh.Trimesh(vertices=vertices, faces=faces)
    cutter.fix_normals()
    return trimesh.boolean.difference([bone, cutter])


def plan() -> dict:
    """Screws around the defect, tilted off the local normal like a surgeon's.

    Each is angled ``SCREW_TILT_DEG`` from the surface normal, rotated about the
    surface, so no two trajectories are parallel and none matches the face it
    enters. The plate family has one mount direction and cannot express this; the
    patch family only ever uses each screw's own axis.
    """
    u0, v0 = DEFECT_CENTER_UV
    screws = []
    for i in range(N_SCREWS):
        angle = 2.0 * np.pi * i / N_SCREWS
        reach = DEFECT_RADIUS_MM + 9.0 + 3.5 * np.sin(2.0 * angle)
        u = u0 + reach * np.cos(angle)
        v = v0 + reach * np.sin(angle)
        entry = _surface_point(u, v)
        normal = _surface_normal(u, v)

        # Tilt the inward normal about an axis lying in the local surface.
        axis = np.cross(normal, [np.cos(angle), np.sin(angle), 0.0])
        axis /= np.linalg.norm(axis)
        rotation = trimesh.transformations.rotation_matrix(
            np.radians(SCREW_TILT_DEG), axis
        )[:3, :3]
        direction = rotation @ -normal
        direction /= np.linalg.norm(direction)

        screws.append(
            {
                "id": f"blade_{i + 1}",
                "entry_mm": [round(float(c), 3) for c in entry],
                "direction": [round(float(c), 4) for c in direction],
                "diameter_mm": 2.7,
                "length_mm": 12.0,
                "note": (
                    f"driven {SCREW_TILT_DEG:.0f} deg off the local normal, into the "
                    f"thicker bone"
                ),
            }
        )
    return {"screws": screws}


def keepouts() -> dict:
    """A neurovascular corridor crossing the blade, so the region is not free.

    Declared explicitly: a case that lists its inputs and omits keepouts is taken
    to have none, rather than inheriting another case's zones.
    """
    return {
        "zones": [
            {
                "id": "suprascapular_notch_corridor",
                "type": "sphere",
                "center_mm": [round(float(c), 3) for c in _surface_point(34.0, -30.0)],
                "radius_mm": 9.0,
                "note": (
                    "SYNTHETIC stand-in for a neurovascular corridor near the "
                    "blade margin; no anatomy was measured to place it."
                ),
            }
        ]
    }


def case() -> dict:
    return {
        "case_id": CASE_ID,
        "provenance": (
            "SYNTHETIC -- fabricated analytic saddle-shaped blade with a ragged "
            "defect and oblique screws. Exists to test the conformal-patch family "
            "on doubly curved anatomy with varying bone thickness. Not patient "
            "data. No clinical claim."
        ),
        "inputs": {
            "bone_mesh": "bone.stl",
            "screw_positions": "screw_positions.json",
            "keepout_zones": "keepout_zones.json",
        },
        "implant": {
            "family": "conformal_patch",
            "region": {"type": "screw_span", "margin_mm": 15.0},
            "region_note": (
                "The screws ring the resection at ~28 mm with a wobble; a 15 mm "
                "margin joins them into one patch that spans the defect and lands "
                "on intact bone all round."
            ),
        },
        "material": {
            "name": "Ti-6Al-4V (Grade 5), annealed",
            "youngs_modulus_GPa": 114.0,
            "poisson_ratio": 0.34,
            "yield_strength_MPa": 880.0,
            "density_g_cm3": 4.43,
            "allowable_stress_MPa": 350.0,
        },
        "envelope": {
            "aspect": "scapular blade, dorsal surface (synthetic)",
            "max_footprint_mm": 120.0,
            "max_standoff_mm": 6.0,
            "thickness_bounds_mm": {"min": 1.5, "max": 4.5},
        },
        "thresholds": {
            "min_wall_mm": 1.5,
            "max_bone_gap_mm": 1.5,
            "min_bone_gap_mm": 0.05,
            "max_implant_mass_g": 45.0,
            "max_keepout_encroach_mm": 0.0,
            "require_watertight": True,
            "require_all_screws": N_SCREWS,
        },
        "threshold_notes": {
            "max_bone_gap_mm": (
                "The seating surface is a saddle, so a device that conforms on a "
                "sphere will not conform here: this threshold is the point of the "
                "case."
            ),
            "stress": (
                "No load case is declared. Scapular reconstruction loading is not "
                "a beam problem, and no beam surrogate in this repository applies, "
                "so the stress validator reports SKIP rather than a number."
            ),
        },
        "defect": {
            "type": "irregular_prism",
            "center_mm": [
                round(float(c), 3) for c in _surface_point(*DEFECT_CENTER_UV)
            ],
            "mean_radius_mm": DEFECT_RADIUS_MM,
            "note": "ragged full-thickness resection cut out of the bone mesh",
        },
    }


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    bone = defect(blade())
    bone.export(HERE / "bone.stl")
    for name, payload in (
        ("screw_positions.json", plan()),
        ("keepout_zones.json", keepouts()),
        ("case.json", case()),
    ):
        (HERE / name).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    print(
        f"{CASE_ID}: bone {len(bone.faces)} faces, watertight={bone.is_watertight}, "
        f"thickness {THIN_MM}-{THICK_MM} mm"
    )
    print(f"wrote {HERE / 'bone.stl'}, screw_positions.json, keepout_zones.json, case.json")


if __name__ == "__main__":
    main()
