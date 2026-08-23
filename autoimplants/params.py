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
    # Which topology to build. "plate" sweeps a fitted section along a shaft, so it
    # only means anything on a long bone. "conformal_patch" offsets a region of the
    # bone's own surface into a shell and works on any anatomy -- cranial, pelvic,
    # scapular -- because it never assumes an axis. Every key below marked "plate"
    # is ignored by the patch family and vice versa; the case declares which family
    # its anatomy needs (case["implant"]["family"]).
    "family": "plate",

    # --- conformal_patch family ------------------------------------------------
    # region: which bone surface the device covers.
    #   {"type": "screw_span", "margin_mm": m} -- everything within m of a planned
    #   screw entry. The general default: no extra authoring, and it follows the
    #   surgeon's own fixation pattern.
    #   {"type": "sphere", "center_mm": [...], "radius_mm": r} -- how a defect is
    #   described, for a reconstruction that fills one.
    # wall: a number for a uniform shell, or {"base_mm", "boss_mm",
    #   "boss_radius_mm"} to thicken around each bore -- the surface equivalent of
    #   the plate's hole_bosses, and for the same reason: the bore is where the
    #   section is lost and where the head bears.
    "patch": {
        "region": {"type": "screw_span", "margin_mm": 12.0},
        # Thinner than the plate's wall, deliberately: a shell wrapped over
        # curvature gets its stiffness from that curvature, not from depth, and a
        # patch covers far more area than a plate strip -- 2.6 mm over a cranial
        # region is 50 g of titanium. Raised locally at the bores, where the
        # section is lost to the hole and the screw head bears.
        "wall": {"base_mm": 1.8, "boss_mm": 0.9, "boss_radius_mm": 7.0},
    },

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
    # [[s, t], ...] with s in 0..1 along the plate length, t in mm. Empty: the
    # wall comes from moment_thickness below, which measures the same
    # section-follows-the-moment shape off the plan rather than off fractions of
    # the length. Kept as a handle because it is the only way to add section this
    # rule does not ask for -- the two are combined by taking the larger.
    "thickness_profile": [],
    # {"min_mm", "max_mm", "exponent"} -- section follows the bending moment. The
    # gait moment peaks over the fracture at mid-footprint and tapers to zero at
    # the outermost screws, and the weakest station of a plate is the one through
    # a screw hole, so the wall is thickest over the fracture and tapers to the
    # manufacturing minimum at the ends, where the moment has run out.
    #
    # Measured off this plan's screw span rather than pinned to fractions of the
    # plate length. The fractions were only ever right for the screws they were
    # tuned on: a real CT case put its inner holes
    # at s=0.22 and s=0.78, either side of the profile's peak, and both failed at
    # ~410 MPa on a 3.1 mm wall while the 4.5 mm part of the plate carried no
    # hole. See generator._moment_thickness.
    #
    # exponent 1.0: wall proportional to the moment. Constant fibre stress would
    # want 0.5 (modulus goes as t^2), and it is the better-conditioned design in
    # isolation, but it holds the wall thick far out along the span and lands at
    # 57.4 g against the 55 g budget. 1.0 spends the mass where the moment is.
    #
    # ceiling_mm 4.5 is the maximum in the case envelope's thickness_bounds_mm,
    # which no validator reads (see _check_thickness_bounds -- an earlier design
    # passed 21/21 on a 5.5 mm wall the case forbids). max_mm 3.6 is below it on
    # purpose: the plain wall stops there and the remaining legal millimetre is
    # spent by hole_bosses, at the bores, where the failing checks are.
    # min_mm 2.4 at the ends -- the seat's curvature means the measured x-chord
    # comes out ~0.55 mm above nominal, so this reports 2.97 against the 2.5 mm
    # min_wall_mm floor.
    "moment_thickness": {
        "min_mm": 2.4, "max_mm": 3.6, "exponent": 1.0, "ceiling_mm": 4.5,
    },
    # {"height_mm", "span_mm"} -- a raised pad at every planned bore.
    #
    # The moment rule puts its thickest wall at mid-footprint, which on a plan
    # whose screws straddle the fracture is the one station with no hole in it,
    # while the checks that fail are net-section-plus-Kt *at* the bores. So the
    # last millimetre of legal wall is spent locally: +1.2 mm at each entry,
    # cosine-tapered out over 10 mm and clipped at ceiling_mm, which puts the
    # thickest legal section exactly at the six holes.
    #
    # On the real-CT case this is the difference between failing three hole checks
    # at 362-375 MPa and passing at 327. span_mm is what it costs: the same pads
    # tapered over 14 mm instead of 10 report 1.8 MPa less and 1.7 g more, and mass
    # is the binding constraint. 10 mm is a little over two bore diameters, so the
    # pad is spent beside the hole rather than between holes.
    "hole_bosses": {"height_mm": 1.2, "span_mm": 10.0},
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


def for_case(params: dict, case: dict) -> dict:
    """Adopt the implant family (and its region) the case's anatomy requires.

    The family is a property of the anatomy, not of the design: a cranial defect
    cannot be treated by sweeping a section along a shaft, whatever the params
    say. So a case declaring ``implant.family`` selects it, unless the params
    already ask for a non-default family -- that is how a design iteration tries a
    different topology on the same case.
    """
    declared = (case.get("implant") or {}).get("family")
    if not declared or params.get("family", "plate") != "plate":
        return params

    params = deepcopy(params)
    params["family"] = declared
    region = (case.get("implant") or {}).get("region")
    if region and declared == "conformal_patch":
        params.setdefault("patch", {})
        params["patch"] = {**params["patch"], "region": region}
    return params


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

    if params.get("family") not in (None, "plate", "conformal_patch"):
        problems.append(
            f"family must be 'plate' or 'conformal_patch', got {params['family']!r}"
        )

    for key in ("moment_thickness", "hole_bosses", "patch"):
        spec = params.get(key)
        if spec is not None and not isinstance(spec, dict):
            problems.append(f"{key} must be a dict, got {type(spec).__name__}")

    return problems
