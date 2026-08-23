"""A non-long-bone case, so "any anatomy" is a thing you can run rather than a claim.

Writes a synthetic cranial vault -- a doubly curved bone with no shaft, no
proximal/distal axis and no mount aspect -- plus a plan whose screws sit around a
defect on it. Nothing about this geometry suits the plate family: there is no
direction to sweep a section along. It is the case the conformal-patch family
exists for.

    python real_cases/synthetic_patch/make_case.py

The bone is analytic on purpose: this is about the *design* generalising to a
curved surface, not about segmentation, which the CT cases already cover.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import trimesh

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from autoimplants.patch import build_shell  # noqa: E402

HERE = Path(__file__).resolve().parent
CASE_ID = "SYNTH-CRANIAL-001"

RADIUS_MM = 78.0          # outer radius of an adult vault, near the parietal
THICKNESS_MM = 6.5        # vault bone thickness
DEFECT_RADIUS_MM = 22.0   # the craniectomy the device covers
SCREW_RING_MM = 30.0      # screws on intact bone outside the defect
N_SCREWS = 6


def vault() -> trimesh.Trimesh:
    """A thick ellipsoidal cap: outer and inner tables, closed at the cut edge.

    Ellipsoidal rather than spherical so no single radius fits it -- a device that
    only matched a sphere would be flattering itself here.

    Built by the same offset-and-close routine the implant uses, with a negative
    clearance so the wall goes *inward* from the outer table. Nothing about that
    routine is implant-specific: closing an open sheet into a solid is the same
    operation whether the sheet is bone or device.
    """
    outer = trimesh.creation.icosphere(subdivisions=5, radius=RADIUS_MM)
    outer.apply_scale([1.0, 0.86, 0.92])
    keep = outer.vertices[:, 2] > 0.15 * RADIUS_MM
    faces = np.flatnonzero(keep[outer.faces].all(axis=1))
    return build_shell(outer, faces, -THICKNESS_MM, THICKNESS_MM, np.empty((0, 3)))


def defect(bone: trimesh.Trimesh, center: np.ndarray) -> trimesh.Trimesh:
    """Remove a full-thickness disc of bone -- the craniectomy being reconstructed."""
    axis = center / np.linalg.norm(center)
    cutter = trimesh.creation.cylinder(
        radius=DEFECT_RADIUS_MM,
        segment=np.vstack([center - axis * 40.0, center + axis * 40.0]),
    )
    return trimesh.boolean.difference([bone, cutter])


def plan(center: np.ndarray) -> dict:
    """Screws around the defect rim, each driven along the local bone normal.

    A cranial plate's screws are not parallel: they follow the vault, which is
    exactly the case the plate family's single mount direction cannot express.
    """
    axis = center / np.linalg.norm(center)
    seed = np.array([0.0, 0.0, 1.0]) if abs(axis[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(axis, seed)
    u /= np.linalg.norm(u)
    v = np.cross(axis, u)

    screws = []
    for i in range(N_SCREWS):
        angle = 2.0 * np.pi * i / N_SCREWS
        offset = SCREW_RING_MM * (np.cos(angle) * u + np.sin(angle) * v)
        entry = center + offset
        entry = entry / np.linalg.norm(entry) * RADIUS_MM * 0.995
        direction = -entry / np.linalg.norm(entry)  # inward along the local normal
        screws.append(
            {
                "id": f"cranial_{i + 1}",
                "entry_mm": [round(float(c), 3) for c in entry],
                "direction": [round(float(c), 4) for c in direction],
                "diameter_mm": 2.4,
                "length_mm": 6.0,
                "note": "self-drilling cranial screw, driven along the local vault normal",
            }
        )
    return {"screws": screws}


def case(center: np.ndarray) -> dict:
    return {
        "case_id": CASE_ID,
        "provenance": (
            "SYNTHETIC -- fabricated engineering constraints on an analytic cranial "
            "vault. Exists to exercise the conformal-patch implant family on anatomy "
            "with no shaft axis. Not patient data. No clinical claim."
        ),
        "inputs": {
            "bone_mesh": "bone.stl",
            "screw_positions": "screw_positions.json",
        },
        # The device family is a property of the anatomy: there is no axis here to
        # sweep a plate section along, so the case asks for a conformal shell.
        "implant": {
            "family": "conformal_patch",
            "region": {"type": "screw_span", "margin_mm": 16.0},
            "region_note": (
                "The screws ring the defect at 30 mm, so a 16 mm margin around them "
                "spans the craniectomy and lands on intact bone all the way round."
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
            "aspect": "right parietal vault, outer table",
            # One span limit, no length/width: the device is not aligned to the frame.
            "max_footprint_mm": 110.0,
            "max_standoff_mm": 5.0,
            "thickness_bounds_mm": {"min": 1.5, "max": 4.0},
        },
        "thresholds": {
            "min_wall_mm": 1.5,
            "max_bone_gap_mm": 1.5,
            "min_bone_gap_mm": 0.05,
            "max_implant_mass_g": 40.0,
            "max_keepout_encroach_mm": 0.0,
            "require_watertight": True,
            "require_all_screws": N_SCREWS,
        },
        "threshold_notes": {
            "min_wall_mm": (
                "Cranial devices are thin: 1.5 mm is a plausible manufacturing floor "
                "for a titanium cranioplasty plate, against 2.5 mm for a load-bearing "
                "femoral plate."
            ),
            "stress": (
                "No load case is declared. A cranioplasty is not load-bearing in the "
                "way a femoral plate is, and inventing a load to make a stress number "
                "appear would be worse than reporting none: the stress validator "
                "reports SKIP for this case."
            ),
        },
        "defect": {
            "type": "sphere",
            "center_mm": [round(float(c), 3) for c in center],
            "radius_mm": DEFECT_RADIUS_MM,
            "note": "the craniectomy the device reconstructs, cut out of the bone mesh",
        },
    }


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    center = np.array([1.0, 0.35, 1.15])
    center = center / np.linalg.norm(center) * RADIUS_MM

    bone = defect(vault(), center)
    bone.export(HERE / "bone.stl")
    (HERE / "screw_positions.json").write_text(
        json.dumps(plan(center), indent=2) + "\n", encoding="utf-8"
    )
    (HERE / "case.json").write_text(
        json.dumps(case(center), indent=2) + "\n", encoding="utf-8"
    )
    print(f"{CASE_ID}: bone {len(bone.faces)} faces, watertight={bone.is_watertight}")
    print(f"wrote {HERE/'bone.stl'}, screw_positions.json, case.json")


if __name__ == "__main__":
    main()
