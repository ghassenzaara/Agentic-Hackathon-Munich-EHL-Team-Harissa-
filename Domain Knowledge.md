# DOMAIN_KNOWLEDGE.md — Bone plate design for the CAD generator

You are designing an orthopedic bone fixation plate. This file is everything you need
about the domain. Read it fully before editing the generator.

**Your job:** edit `generate_plate.py` so the plate it produces passes every check in the
verifier. You may change parameter values AND the structure of the geometry code.

**What you may not touch:** the verifier service. Loads, boundary conditions, material
properties and pass thresholds live outside your repo and are read-only. If a check
fails, change the plate, never the test.

**Never stop to ask a question.** If something is ambiguous, choose, state the choice in
your log, and continue. If you cannot converge within the iteration cap, emit the best
candidate plus a failure report.

**Integrity rules.** Never stub, mock, cache or fabricate a verifier result. Never write
a fallback that returns a passing score when the solver errors. If the verifier is
unreachable or the solve fails, log the failure and retry — a failed solve is data, a
fake pass is a corrupted run. A converged design whose numbers did not come from the
remote verifier is worthless.

---

## 1. What the object is

A bone plate is a curved metal strip screwed onto the OUTSIDE of a bone to hold a
fracture while it heals. It is not inside the bone (that is an intramedullary nail) and
it does not replace bone (that is a prosthesis).

Physical facts that constrain everything:

- It sits between bone and skin. Anything standing proud irritates soft tissue and can
  erode through it. Thickness is limited by anatomy, not just by stress.
- It is screwed to bone. Screws must pass through cortical bone (the dense outer shell)
  to hold. The spongy interior holds almost nothing.
- It is printed in Ti-6Al-4V by laser powder bed fusion, so it must be printable.
- It shares load with the bone. This is the non-obvious part — see §5.

### Anatomy words you need

| Term | Meaning |
|---|---|
| Diaphysis | the straight tubular shaft in the middle of a long bone |
| Metaphysis | the flaring region near each end |
| Epiphysis | the joint end itself |
| Cortex / cortical bone | dense outer shell, roughly 5–7 mm thick in an adult tibia or femur shaft |
| Trabecular bone | spongy interior, mechanically weak |
| Lateral / medial | outer side / inner side of the limb |
| Proximal / distal | toward the body / away from the body |

The default target is the **diaphysis**: nearly a tube, no joint surface to avoid, and
the simplest geometry. Typical adult tibia/femur shaft: about 27 mm outer diameter with
about 7 mm cortical wall.

### Coordinate frame — read this before using any position

Every position in the prescription and in your code is expressed in the bone's
**anatomical frame**, not raw scanner coordinates. The pipeline establishes it before
you receive the mesh:

- **Z axis** = the shaft's long axis, from PCA over the shaft vertices. Positive Z points
  distal (away from the body).
- **Origin** = the proximal end of the cropped segment, at Z = 0.
- **X/Y** = fixed by two anatomical landmarks so rotation about Z is reproducible.

So `fracture_level: 120` means 120 mm distal of the segment origin along Z. `approach:
"lateral"` selects the surface at a known angle about Z. Without this frame the same
prescription would place the plate differently on every patient — if the mesh you receive
is not already transformed, stop and log it rather than guessing an orientation.

### The load case (you cannot change it, but you must reason about it)

The verifier applies a fixed physiological case. You need to know it to route load
sensibly:

- **Axial compression** along Z, magnitude `patient_weight × gait_factor` (default gait
  factor 3.0 for human walking). This dominates in weight-bearing long bones.
- **Distal end fixed**, load applied at the proximal end.
- Screws are represented as constraints at the hole surfaces.

Consequences: the plate is loaded primarily in bending about the axis perpendicular to
the approach direction, and in axial compression. Material added in the bending plane
(thickness) does far more than material added across the plate (width). See §3.

### Screws, clearance and why the gap exists

- **Locking screws** (3.5 mm) thread into the plate itself. The construct behaves as a
  fixed-angle frame, so the plate does NOT need to be pressed against the bone.
- **Compression screws** (4.5 mm) pull the plate down onto the bone by friction.

The 0.1–1.0 mm clearance is deliberate, not slop: pressing a plate hard against bone
crushes the periosteum and interrupts periosteal blood supply, which impairs healing.
This is exactly why limited-contact plate designs exist. **Never set clearance to zero to
"improve fit."** Zero clearance is a failure, not an optimum.

**Adequate screw purchase** means each screw passes through the near cortex and engages
the far cortex without protruding beyond it. Rule of thumb for the shaft: at least three
screws (six cortices) on each side of the fracture.

---

## 2. Function is a mode of use, not a shape

Critical: the same physical plate serves different clinical functions depending on how
it is placed and where the screws go. You do not need a different archetype per
function.

| Mode | What it must do | How it shows up in geometry |
|---|---|---|
| `compression` | squeeze the fracture closed | screw holes placed eccentrically, away from the fracture; path slightly overbent so the far cortex also compresses |
| `neutralization` | protect a separately placed lag screw from bending, shear and torsion | screw holes in neutral positions, symmetric about the fracture |
| `bridging` | span a shattered zone, carry load end to end without compressing the fracture | screws ONLY in the outer segments, none within roughly 20 mm of the fracture level |
| `antiglide` | stop one fragment sliding under axial load | plate positioned so the fragment wedges against it; placement relative to fracture level matters |

**Hard clinical rule, encode it:** never use bridging mode on a simple transverse
fracture. Stress concentrates over a small area and the plate fails. Bridging is for
comminuted (multi-fragment) patterns only. If `mode == "bridging"` and the prescription
says the fracture is simple, that is a design error, not a parameter to tune.

Compression numbers, for context: a compression plate generates roughly 600 N across the
fracture. A lag screw generates 2000–4000 N. The plate is not the primary compressor.

---

## 3. The archetype and how to build it

The plate is a swept solid, not a downloaded model. Nothing is fetched. The shape is
derived from the patient's bone surface.

```python
def generate_plate(bone_mesh, params, prescription):
    # 1. points ON the bone, along the chosen approach surface
    pts     = sample_surface(bone_mesh, axis=long_axis,
                             region=prescription.approach, n=20)
    normals = surface_normals(bone_mesh, pts)

    # 2. push them outward: half thickness + clearance
    offset  = params.thickness / 2 + params.clearance
    path    = spline(pts + normals * offset)

    # 3. sweep a cross-section along the path -> this creates the solid
    section = cross_section(params)
    solid   = sweep(section, path)

    # 4. screw holes, drilled along the local surface normal
    for pos in hole_positions(params, prescription):
        solid = solid.cut(cylinder(at=pos, dia=params.hole_dia,
                                   direction=normal_at(pos)))

    # 5. round sharp edges
    return fillet(solid, params.fillet_radius)
```

Step 3 is where the object comes into existence. A cross-section dragged along a curve
produces a solid, like squeezing toothpaste along a path.

### Why the cross-section is rectangular

Bending stiffness follows the second moment of area, `I = w·h³/12`. Note the **cube** on
height. Doubling thickness gives 8x the bending resistance; doubling width gives only 2x.
Thickness is therefore your most powerful lever — and also the one most limited by soft
tissue. A round section would be wrong here: it is equally stiff in all directions, which
wastes material in directions carrying no load, and a cylinder touches bone along a line
instead of a surface.

You may change the section shape — taper it along the length, thin the ends, thicken
around holes — but keep the bone-facing face flat or gently concave.

---

## 4. Starting parameters

From published finite element studies of real plates. These are iteration-one guesses,
not targets.

| Parameter | Start | Range | Notes |
|---|---|---|---|
| `length` | 140 mm | 80–190 | must span the fracture with adequate purchase both sides |
| `width` | 12 mm | 10–14 | limited by the flat area available on the bone surface |
| `thickness` | 3.5 mm | 3.0–5.0 | strongest lever on stiffness; capped by soft tissue |
| `n_holes` | 8 | 6–12 | minimum 3 screws per side of the fracture |
| `hole_spacing` | 13 mm | 10–16 | |
| `hole_dia` | 3.5 mm | 3.5 or 4.5 | 3.5 locking, 4.5 compression |
| `clearance` | 0.2 mm | 0.1–1.0 | gap between plate and bone |
| `fillet_radius` | 0.5 mm | 0.5–2.0 | direct fix for stress concentration |

Real reference plates for sanity: a 10-hole diaphyseal LCP is about 138 x 10 x 4 mm; a
4.5 mm narrow LCP is about 188 x 14 mm; an 8-hole proximal tibia plate is about
79 x 11 x 3.5 mm.

---

## 5. The trap: a stronger plate is not a better plate

This is the single most important section in this file.

The obvious way to pass a stress check is to make everything thicker. **That produces a
clinically worse device and the verifier will reject it.**

**Stress shielding:** bone is alive and remodels according to the load it carries
(Wolff's law). Load it, it thickens. Unload it, it atrophies. A very stiff plate carries
almost all the load itself, the bone underneath stops being loaded, and it resorbs. Then
either the plate eventually fatigues, or the plate is removed and the weakened bone
refractures.

This is documented, not theoretical: a custom 3D-printed Ti-6Al-4V plate on a dog radius
produced bone resorption, reduced bone mineral density, and degraded apatite orientation
in the bone directly under the plate at seven months post-op.

**Therefore the target is a WINDOW, not a maximum.** Too flexible fails; too stiff also
fails. When the verifier reports stiffness near the upper bound, thickening is the wrong
move — reshape instead.

### Working length — your best stiffness lever

**Working length** is the distance between the innermost screw on one side of the
fracture and the innermost screw on the other side: the unsupported span of plate
bridging the fracture.

It is the single most effective way to move construct stiffness without touching
thickness. Longer working length means a more flexible construct and lower peak plate
stress; shorter means stiffer. Changing working length alone shifts axial stiffness by
28–37%.

Practical consequence: when stiffness is too high, **leave a screw hole empty near the
fracture** before you thin the plate. When peak stress is too high, lengthening the
working length often reduces it more cleanly than adding material. Reach for hole
placement before reaching for thickness.

---

## 6. What the verifier checks

Every check returns `{passed, measured, limit, where}`. Read `where` — it names the
parameter that produced the failing geometry, not just a coordinate.

| Check | Rule | If it fails |
|---|---|---|
| Safety factor | `880 / peak_von_mises >= 2.5` | thicken locally, increase fillet, add a rib, reroute load |
| Stiffness window | inside band, both bounds binding | see §5 — above the band, reshape, do not thin blindly |
| Mass budget | `<= 40 g` | anti-gaming; you cannot pass by making a brick |
| Max thickness | `<= 6.0 mm` | soft tissue proxy |
| Bone intersection | no negative signed distance | plate must not sink into bone |
| Clearance | 0.1–1.0 mm | must not float off the bone either |
| Screw trajectory | passes through cortex, adequate purchase, no wrong-side exit | move or re-angle holes |
| Printability | overhang `<= 45 deg`, min feature size respected | reorient features, thicken thin walls |

Material: Ti-6Al-4V. `E = 113800 MPa`, `nu = 0.342`, yield `880 MPa`, density
`4.43e-3 g/mm^3`.

Units are **N, mm, MPa** everywhere. No exceptions. A unit error produces a
plausible-looking wrong answer with no warning.

### Numeric thresholds

| Quantity | Limit |
|---|---|
| `safety_factor` | `>= 2.5` |
| `stiffness_N_per_mm` | `100 <= k <= 400` (band, both bounds binding) |
| `mass_g` | `<= 40` |
| `max_thickness_mm` | `<= 6.0` |
| `min_clearance_mm` | `0.1 <= c <= 1.0` |
| `overhang_deg` | `<= 45` |
| `min_feature_mm` | `>= 0.8` |

Why safety factor 2.5 and not 1.0: the plate is loaded thousands of times per day and
fails by **fatigue**, not single overload. Material has scatter, printed parts have
defects, loads are estimates, and a patient may stumble. The margin covers all of that.
Treat 2.5 as a floor, not a target to sit exactly on.

### Verifier API

```
POST {VERIFIER_URL}/verify
body: { "step_file": <base64 or path>, "prescription": {...}, "tags": {...} }
```

Response:

```json
{
  "passed": false,
  "checks": [
    {"name": "safety_factor", "passed": false, "measured": 1.42,
     "limit": 2.5, "where": {"xyz": [14.2, 3.8, 91.5],
                             "tag": "fillet_radius_hole_3"}},
    {"name": "stiffness_N_per_mm", "passed": true, "measured": 271.0,
     "limit": [100, 400], "where": null},
    {"name": "mass_g", "passed": true, "measured": 31.4, "limit": 40, "where": null}
  ],
  "mesh_convergence": {"sizes_mm": [2.0, 1.0, 0.5],
                       "peak_vm": [601.2, 618.9, 620.1]},
  "render_urls": ["..."],
  "solve_time_s": 24.1
}
```

Always read `mesh_convergence`. If `peak_vm` is still climbing steeply at the finest
size, the peak is mesh-dependent and probably a singularity — see §7 before acting on it.

---

## 7. Reading the FEA result correctly

**Peak stress near a fixed boundary is usually an artifact.** A perfectly rigid
constraint is physically impossible and mathematically produces infinite stress at the
corner; refining the mesh makes the number climb forever instead of converging.

Verified on this project's own harness with a cantilever benchmark: the global peak von
Mises converged cleanly (238.9 -> 240.7 -> 244.4 MPa against an analytical 240 MPa) while
the stress at the clamped wall diverged (275.6 -> 294.9 -> 329.0 MPa) as the mesh
refined. The wall number is a singularity, not a real stress.

**Consequence for you:** the equivalent artifact appears at the constrained screw holes.
Do not chase it. The verifier already excludes elements within 1–2 element lengths of any
constrained node when locating the hotspot; if a reported hotspot still sits exactly on a
fixed node, treat it as suspect and look at the next-highest region instead.

**Real stress concentrations** to act on:
- Hole edges. A hole in a plate under tension sees roughly 3x the average stress at its
  sides. This is geometric (`Kt`), so adding material locally may just relocate the peak
   — increasing the fillet or easing the hole spacing is usually better.
- Sharp internal corners. Mathematically infinite. Always fillet.
- Abrupt section changes. Taper instead of stepping.

---

## 8. How to iterate

Decide first whether the failure is a **shape** problem or a **number** problem.

- **Number problem** — everything is qualitatively right, values are off. Run the local
  optimizer over the continuous parameters (thickness taper, fillet radii, width
  profile). 100–200 cheap evaluations, converges in about two minutes. Do not do this by
  hand one value at a time.
- **Shape problem** — no setting of the current parameters can pass. Change the geometry
  code: add a rib, taper the section, split the head into two screw columns, reroute the
  path, change hole pattern. This is the part only you can do.

If you find yourself trying the third variation of the same single number, it is a shape
problem.

### Budget

- **Iteration cap: 15 verifier calls per patient.** Wall clock cap: 40 minutes.
- Each remote verify costs roughly 25–60 s. Local optimizer runs are cheap — use them.
- If you have used 10 calls and no check has improved, you are stuck on a shape problem.
  Change the geometry structurally instead of continuing to tune.
- If two consecutive iterations return identical metrics, your edit did not reach the
  geometry. Verify the STEP file actually changed before spending another call.

Log every iteration in `iterations.json`: iteration number, what changed, why, the full
returned metrics, and whether it was a shape edit or an optimizer run. The log is a
deliverable, not debug output.

---

## 9. CAD pitfalls that will cost you hours

- **Never boolean the plate against the bone mesh.** OCCT fails on 20k-triangle solids
  with `BRep_API: command not done` and no diagnostic. Clearance is checked NUMERICALLY
  with trimesh signed distance. Never enforce it as a CAD operation.
- **Sweep failures** usually mean the path curvature is too tight for the section width.
  Reduce sample point count, smooth the spline, or narrow the section.
- **Fillet failures** mean the radius exceeds the local wall thickness. Fillet radius must
  stay below roughly half the minimum adjacent thickness.
- **Export STEP, not STL,** for the verifier. STL loses the curved surfaces and meshes
  badly.
- **Tag your geometry.** When you create a face, record which parameter produced it. The
  verifier maps a hotspot back to `fillet_radius_hole_3` through those tags. Untagged
  geometry gives you coordinates instead of actionable edits.

---

## 10. Rendering to understand a failure

You have your own machine. Use it. A coordinate is not actionable; a picture is.

With pyvista, render offscreen to PNG and look at it:
- deformed model with the von Mises field, hotspot marked
- cross-section through the hotspot
- plate overlaid on bone, showing seating and standoff
- iteration-over-iteration diff

Vision on contour plots is coarse — it reliably localizes to a feature, it does not read
subtle field structure. Use renders to reason about WHY; use the geometry tags to know
WHERE.

Renders and local solves on your machine are advisory. Only the remote verifier's verdict
counts.

---

## 11. Prescription input

The CT tells you where the bone is. It does not tell you what device is needed. That is a
clinical decision supplied separately:

```json
{
  "device_class":    "diaphyseal_fixation_plate",
  "mode":            "bridging",
  "fracture_level":  120,
  "fracture_type":   "comminuted",
  "approach":        "lateral",
  "screws_per_side": 3,
  "patient_weight":  78,
  "species":         "human"
}
```

Field semantics:

| Field | Meaning |
|---|---|
| `device_class` | fixed for now: `diaphyseal_fixation_plate` |
| `mode` | `compression` / `neutralization` / `bridging` / `antiglide` — drives hole placement, see §2 |
| `fracture_level` | mm along +Z from the segment origin in the anatomical frame (§1) |
| `fracture_type` | `transverse` / `oblique` / `comminuted` — gates which modes are valid |
| `approach` | which bone surface the plate sits on; selects the sampling region |
| `screws_per_side` | minimum screws each side of the fracture; 3 is standard for the shaft |
| `patient_weight` | kg; load = weight × gait factor (§1) |
| `species` | see below |

**Species handling.** The pipeline is species-agnostic because the input is just a mesh,
but three things scale:

- Load: `patient_weight × gait_factor`. Use 3.0 for human and dog walking unless the
  prescription overrides it.
- Screw diameter must scale with cortical thickness. Measure local cortical thickness
  from the mesh rather than assuming 3.5 mm — a toy-breed dog radius will not take a
  human-sized screw.
- The stiffness band scales with the bone's own stiffness. A band tuned for a human tibia
  is wrong for a bird humerus. If the prescription does not supply a band, derive one
  from the bone's section properties and **log that you did so**.

If a combination is clinically invalid (per §2, bridging on a simple transverse
fracture), that is a design error to report, not a parameter to tune around.

---

## 12. Definition of done

A run succeeds when a valid STEP file passes every check in §6 with an iteration log
recording each change and its rationale. Ties are broken by lower mass, then by fewer
iterations.