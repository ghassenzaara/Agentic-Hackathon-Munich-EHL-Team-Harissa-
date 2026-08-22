"""Generate the synthetic femur and the geometry-derived constraint files.

SYNTHETIC ANATOMY. Not patient data, not a clinical model. Dimensions are taken
from published adult femoral morphometry so the numbers are defensible, but the
mesh is procedural: deterministic, regenerable, no licence question, and diffable.

Writes (all committed):
    bone.stl               closed manifold triangle mesh
    screw_positions.json   entry points sampled ON the actual bone surface
    keepout_zones.json     anatomical no-go volumes

Screw positions are *derived from this mesh*, never hand-typed -- otherwise the
geometry validator would be checking the implant against numbers that do not
touch the bone.

Run:  python inputs/make_bone.py
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent

# --- morphometry ------------------------------------------------------------
LENGTH_MM = 400.0          # shaft length modelled (adult femur ~430-450 mm total)
R_MIDSHAFT = 13.0          # ~26 mm mid-diaphyseal outer diameter
R_PROXIMAL = 24.0          # trochanteric flare
R_DISTAL = 30.0            # condylar flare
ELLIPSE_RATIO = 0.86       # cross-section is slightly flattened medio-laterally

# Anterior bow. Sagitta of 22 mm over 400 mm => radius of curvature
# R = L^2 / (8*sagitta) = 0.91 m, inside the published femoral range (~0.8-1.5 m).
# This is the feature that makes a FLAT off-the-shelf plate fail on this patient:
# it produces ~4.8 mm of mid-span standoff across the plate footprint.
BOW_MM = 22.0

N_RINGS = 121
N_THETA = 40

# --- implant footprint (must agree with inputs/case.json) -------------------
PLATE_Z0, PLATE_Z1 = 100.0, 280.0
SCREW_Z = [115.0, 145.0, 175.0, 205.0, 235.0, 265.0]

PROVENANCE = (
    "SYNTHETIC -- procedurally generated, not patient data. Morphometry grounded in "
    "published adult femoral literature. No clinical or regulatory claim."
)


def bow(s):
    """Anterior bow of the centreline, peaking at mid-shaft."""
    return BOW_MM * np.sin(np.pi * np.asarray(s, dtype=float))


def radius(s):
    """Outer radius along the shaft: flared at both ends, narrow at mid-diaphysis."""
    s = np.asarray(s, dtype=float)
    return (
        R_MIDSHAFT
        + (R_PROXIMAL - R_MIDSHAFT) * np.exp(-((s / 0.13) ** 2))
        + (R_DISTAL - R_MIDSHAFT) * np.exp(-(((1.0 - s) / 0.16) ** 2))
    )


def lateral_surface_x(z: float) -> float:
    """x of the bone surface on the +X (lateral) aspect at height z, y = 0.

    This is the surface the plate sits against, so it is also the screw entry x.
    """
    s = z / LENGTH_MM
    return float(bow(s) + radius(s))


def build_mesh():
    s = np.linspace(0.0, 1.0, N_RINGS)
    theta = np.linspace(0.0, 2.0 * np.pi, N_THETA, endpoint=False)

    cx = bow(s)
    r = radius(s)
    rx = r[:, None]
    ry = (r * ELLIPSE_RATIO)[:, None]

    x = cx[:, None] + rx * np.cos(theta)[None, :]
    y = ry * np.sin(theta)[None, :]
    z = np.repeat((s * LENGTH_MM)[:, None], N_THETA, axis=1)

    verts = np.stack([x, y, z], axis=-1).reshape(-1, 3)

    def vid(i: int, j: int) -> int:
        return i * N_THETA + (j % N_THETA)

    faces = []
    # Shell. Winding chosen so normals point radially outward.
    for i in range(N_RINGS - 1):
        for j in range(N_THETA):
            a, b = vid(i, j), vid(i, j + 1)
            c, d = vid(i + 1, j + 1), vid(i + 1, j)
            faces.append((a, b, c))
            faces.append((a, c, d))

    # End caps, so the mesh is a closed solid the validators can do volume maths on.
    bottom_c = len(verts)
    top_c = bottom_c + 1
    verts = np.vstack([verts, [[cx[0], 0.0, 0.0], [cx[-1], 0.0, LENGTH_MM]]])
    for j in range(N_THETA):
        faces.append((bottom_c, vid(0, j + 1), vid(0, j)))                      # -Z
        faces.append((top_c, vid(N_RINGS - 1, j), vid(N_RINGS - 1, j + 1)))     # +Z

    return verts, np.array(faces, dtype=np.int64)


def write_binary_stl(path: Path, verts, faces) -> Path:
    tris = verts[faces]
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    norm = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, norm, out=np.zeros_like(n), where=norm > 0)

    with path.open("wb") as fh:
        header = b"AutoImplants synthetic femur -- procedural, not patient data"
        fh.write(header.ljust(80, b" "))
        fh.write(struct.pack("<I", len(faces)))
        for normal, tri in zip(n, tris):
            fh.write(struct.pack("<3f", *normal))
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(b"\x00\x00")
    return path


def screw_positions() -> dict:
    screws = []
    for idx, z in enumerate(SCREW_Z):
        x = lateral_surface_x(z)
        screws.append(
            {
                "id": f"screw_{idx}",
                "index": idx,
                # Entry point ON the bone surface, lateral aspect, y = 0.
                "entry_mm": [round(x, 3), 0.0, z],
                # At y = 0 on an ellipse the outward normal is exactly +X, so the
                # trajectory is a clean -X. Keeps the obstruction check trivial.
                "direction": [-1.0, 0.0, 0.0],
                "diameter_mm": 4.5,
                "length_mm": 34.0,
            }
        )
    return {
        "provenance": PROVENANCE,
        "note": (
            "Surgical planning is a declared PRE-SOLVED input for this project. These "
            "positions are locked: the implant must accommodate them, not move them."
        ),
        "bone_mesh": "bone.stl",
        "screws": screws,
    }


def keepout_zones() -> dict:
    """No-go volumes, placed to make scalar tuning insufficient on purpose.

    Together with the mass and thickness caps in case.json these close off the
    three lazy fixes -- make it thicker, wider, or longer -- and leave contouring
    and local reinforcement (ribs, variable thickness, slots) as the ways out.
    That is the difference between an optimiser and an engineer, and it has to be
    built into the case, not hoped for at demo time.
    """
    z_mid = 0.5 * (PLATE_Z0 + PLATE_Z1)
    return {
        "provenance": PROVENANCE,
        "zones": [
            {
                "id": "perforating_vessel_bundle",
                "type": "sphere",
                "center_mm": [round(lateral_surface_x(z_mid), 3), 14.8, z_mid],
                "radius_mm": 6.0,
                # Centre y=14.8 minus the 6.0 radius puts the wall at a half-width
                # of 8.8 mm, so the plate may widen to 17.6 mm. The 16 mm baseline
                # clears by 0.8 mm. A knife-edge margin here would make the check
                # float-point flaky, which in an autonomous loop reads as the
                # design randomly regressing.
                "rationale": "Blocks widening the plate past 17.6 mm at mid-span.",
            },
            {
                "id": "proximal_neurovascular_corridor",
                "type": "sphere",
                "center_mm": [round(lateral_surface_x(85.0), 3), 0.0, 85.0],
                "radius_mm": 12.0,
                "rationale": "Blocks extending the plate proximally past z=100.",
            },
            {
                "id": "distal_physeal_scar",
                "type": "sphere",
                "center_mm": [round(lateral_surface_x(300.0), 3), 0.0, 300.0],
                "radius_mm": 15.0,
                "rationale": "Blocks extending the plate distally past z=280.",
            },
        ],
    }


def main() -> None:
    verts, faces = build_mesh()
    stl = write_binary_stl(HERE / "bone.stl", verts, faces)

    (HERE / "screw_positions.json").write_text(
        json.dumps(screw_positions(), indent=2) + "\n", encoding="utf-8"
    )
    (HERE / "keepout_zones.json").write_text(
        json.dumps(keepout_zones(), indent=2) + "\n", encoding="utf-8"
    )

    print(f"bone.stl        {len(verts)} verts, {len(faces)} tris, {stl.stat().st_size} bytes")
    print(f"bounds x        {verts[:, 0].min():.2f} .. {verts[:, 0].max():.2f} mm")
    print(f"bounds y        {verts[:, 1].min():.2f} .. {verts[:, 1].max():.2f} mm")
    print(f"bounds z        {verts[:, 2].min():.2f} .. {verts[:, 2].max():.2f} mm")

    # The designed-in opening failure: standoff of a FLAT plate spanning the footprint.
    x0, x1 = lateral_surface_x(PLATE_Z0), lateral_surface_x(PLATE_Z1)
    x_mid = lateral_surface_x(0.5 * (PLATE_Z0 + PLATE_Z1))
    print(f"flat-plate mid standoff  {x_mid - 0.5 * (x0 + x1):.2f} mm  (chord vs bone surface)")

    try:
        import trimesh

        m = trimesh.load(stl)
        print(
            f"trimesh check   watertight={m.is_watertight} "
            f"winding={m.is_winding_consistent} volume={m.volume / 1000:.1f} cm^3"
        )
    except ImportError:
        print("trimesh check   skipped (not installed on this interpreter)")


if __name__ == "__main__":
    main()
