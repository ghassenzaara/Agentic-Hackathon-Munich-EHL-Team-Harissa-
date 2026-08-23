"""The parameter surface Devin is allowed to change.

Note the four topology handles (``thickness_profile``, ``ribs``, ``hole_slots``,
``contour_spline``). They are unused by the baseline plate on purpose. They exist
from the first commit because they are *how Devin changes topology* rather than
nudging floats -- and adding them later would mean renegotiating the contract
with three other people mid-hackathon.
"""

from __future__ import annotations

from copy import deepcopy

DEFAULT_PARAMS: dict = {
    # --- overall plate envelope ------------------------------------------------
    "length_mm": 180.0,
    # 19.8 of the 20 mm allowed -- the widest station, with width_profile below
    # taking the plate in from there. Width is the cheapest section this design
    # has: because the seat wraps the cortex, a wider plate is also a *deeper*
    # one (the arc's sagitta grows with the chord), so section modulus at the
    # hole stations goes up faster than linearly -- 36.7 mm^3 at 17 mm against
    # 53.0 at 19.6. That is what pays for the wall staying inside the 4.5 mm
    # thickness_bounds_mm the case declares and no validator reads.
    "width_mm": 19.8,
    "thickness_mm": 3.0,

    # Gap held between the bone-facing surface and the bone. Deliberate, not slop:
    # a plate pressed onto bone crushes the periosteum and interrupts the blood
    # supply the fracture heals through, so thresholds.min_bone_gap_mm makes zero
    # clearance a failure. Mounting exactly tangent also puts the measured gap on a
    # knife edge, where its sign depends on ray-sample alignment.
    # 0.4 mm on the analytic bone; 0.25 mm here because the same nominal clearance
    # is spent twice on a reconstructed surface, which also adds the between-sample
    # dilation and the smoothing lift on top of it -- 0.4 put the CT case at
    # 1.57 mm against the 1.5 mm gap limit while the tight side still had 0.44 mm
    # of the 0.1 mm it needs. 0.25 is a quarter of the mesh's own ripple and still
    # 2.5x the minimum.
    "mount_clearance_mm": 0.25,

    # --- topology handles ------------------------------------------------------
    # [[s, t], ...] with s in 0..1 along the plate length, t in mm.
    #
    # Section follows the bending moment. The gait moment peaks over the fracture
    # at z=190 mm and tapers to zero at the outermost screws, and the weakest
    # station of a plate is the one through a screw hole, so the wall is thickest
    # over the fracture and tapers to the manufacturing minimum at the ends,
    # where the moment has run out.
    #
    # The peak is 4.5 mm because that is the maximum in the case envelope's
    # thickness_bounds_mm, which no validator reads -- see _check_thickness_bounds
    # in the generator. An earlier profile peaked at 5.5 mm and passed 21/21 on a
    # wall the case forbids. What replaces that illegal millimetre is width and
    # ribs: both are section, and both are inside limits that are checked.
    "thickness_profile": [
        [0.0, 2.6],
        [0.0833, 2.6],
        [0.25, 3.2],
        [0.4167, 4.5],
        [0.5833, 4.5],
        [0.75, 3.2],
        [0.9167, 2.6],
        [1.0, 2.6],
    ],
    # [[s, width_mm], ...] -- the plate is waisted rather than narrow.
    #
    # perforating_vessel_bundle is a 6 mm sphere at y=14.8, z=190: it blocks width
    # past ~17.6 mm, but only over the ~12 mm of length it spans. Narrowing the
    # whole plate to clear it would throw away section at the two inner screw
    # holes (z=175 and 205), which are the stations that actually fail on stress,
    # to satisfy a constraint that does not apply there. So the waist is local:
    # 17.0 mm across z=184..196, opening back to full width by z=172/208, which is
    # outside the sphere and inboard of both holes. The waist sits at the moment
    # peak, where the plate is at 154 of 350 MPa, so it can afford the section.
    # The ends come in to 16.5 mm as well: holes 0 and 5 sit where the moment has
    # tapered out (164 of 350 MPa), so width there buys nothing and only costs
    # mass -- and mass is what pays for the ribs below.
    "width_profile": [
        [0.0, 16.5],
        [0.0833, 16.5],
        [0.20, 19.8],
        [0.40, 19.8],
        [0.4667, 17.0],
        [0.5333, 17.0],
        [0.60, 19.8],
        [0.80, 19.8],
        [0.9167, 16.5],
        [1.0, 16.5],
    ],
    # [{"s", "length_mm", "height_mm", "width_mm", "y_offset_mm"}, ...]
    #
    # A pair of ribs straddling the bores, y = +-5.5 mm, 4 mm wide, 1 mm high,
    # running 75 mm over all four inner holes. They exist because the hole checks
    # at z=175 and 205 cannot be passed any other legal way: the wall is already
    # at the 4.5 mm bound and the plate at 19.8 of 20 mm, and that combination
    # still reports 372 MPa against a 350 limit.
    #
    # Why this shape. Off the centreline, because a rib down y=0 is exactly where
    # the six bores are and would be cut into six pieces. At y=+-5.5 because the
    # cortex has already fallen ~1.3 mm below its apex there, so a 1 mm rib adds
    # section modulus while barely touching envelope_standoff -- 5.39 of 6.0 mm,
    # against 4.90 without it. 1 mm rather than the 2.5 mm the standoff would
    # still allow, because past ~1 mm the rib raises the extreme fibre (c_x)
    # faster than it raises inertia and the return turns over.
    #
    # Rejected: thickening the wall (illegal past 4.5 mm); widening further (at
    # the envelope, and it raises Kt, which is referenced to the widest section);
    # hole_slots (the Kt correlation is a function of diameter and width only, so
    # a slot would change the mesh and not the reported stress).
    "ribs": [
        {"s": 0.5, "length_mm": 75.0, "height_mm": 1.0,
         "width_mm": 4.0, "y_offset_mm": -5.5},
        {"s": 0.5, "length_mm": 75.0, "height_mm": 1.0,
         "width_mm": 4.0, "y_offset_mm": 5.5},
    ],
    # indices of screw holes to convert from round hole to sliding slot
    "hole_slots": [],
    # [[s, offset_mm], ...] contour bend fitted to the bone surface
    "contour_spline": [],

    # --- screw interface ------------------------------------------------------
    "hole_diameter_mm": 4.5,
    "hole_countersink_mm": 0.0,

    # --- stress relief / cosmetics -------------------------------------------
    # Zero, deliberately. Any relief on the lateral edge -- fillet, chamfer, draft
    # -- thins the x chord there, and check_min_wall measures exactly those chords
    # and takes the minimum: a 1 mm edge bevel reported a 0.56 mm wall against a
    # 2.5 mm floor. It also cost 4.3 g and ~15% of i_yy at the hole stations,
    # because it removes material from the outer edge, which is the fibre furthest
    # from the neutral axis. A manufactured part would still be deburred; that
    # belongs in the CAM step, not in a solid this validator measures walls on.
    "fillet_mm": 0.0,
    "edge_chamfer_mm": 0.0,
}

# Keys the generator and validators rely on existing. If Devin deletes one we want
# a loud, specific error rather than a KeyError six frames deep.
REQUIRED_KEYS = tuple(DEFAULT_PARAMS.keys())


def default_params() -> dict:
    """A fresh deep copy -- never hand out the module-level dict."""
    return deepcopy(DEFAULT_PARAMS)


def check_params(params: dict) -> list[str]:
    """Return a list of human-readable problems. Empty list means usable."""
    problems = [f"missing required param: {k}" for k in REQUIRED_KEYS if k not in params]

    for key in ("length_mm", "width_mm", "thickness_mm", "hole_diameter_mm"):
        v = params.get(key)
        if v is not None and (not isinstance(v, (int, float)) or v <= 0):
            problems.append(f"{key} must be a positive number, got {v!r}")

    for key in (
        "thickness_profile", "width_profile", "ribs", "hole_slots", "contour_spline"
    ):
        v = params.get(key)
        if v is not None and not isinstance(v, list):
            problems.append(f"{key} must be a list, got {type(v).__name__}")

    return problems
