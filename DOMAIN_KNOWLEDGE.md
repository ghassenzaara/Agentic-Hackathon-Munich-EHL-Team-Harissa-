# DOMAIN_KNOWLEDGE.md — Bone plate design for the CAD generator

You are designing an orthopedic bone fixation plate. This file is everything you need
about the domain. Read it fully before editing the generator.

**Your job:** edit `autoimplants/generator.py` so the plate it produces passes every check
in the validators. You may change parameter values AND the structure of the geometry code.

**What you may not touch.** The validators run locally, in this repository, but they are
**locked**: `autoimplants/validators/**`, `inputs/**` (anatomy, surgical plan, load cases,
pass thresholds), `autoimplants/bone.py`, `autoimplants/contracts.py` and `harness/**`. You
may edit `autoimplants/generator.py`, `autoimplants/params.py`, `autoimplants/export.py`,
and write into `runs/` and `out/`. That allowlist is enforced mechanically by
`harness/guard.py`, which diffs your commits after every session; touching anything else
makes the iteration invalid. **If a check fails, change the plate, never the test.**

Relaxing a threshold is not a fix. If you believe a constraint is genuinely infeasible, say
so in the structured output and stop — do not edit your way around it.

**Never stop to ask a question.** If something is ambiguous, choose, state the choice in
your log, and continue. If you cannot converge within the iteration cap, emit the best
candidate plus a failure report.

**Integrity rules.** Never stub, mock, cache or fabricate a validator result. Never write a
fallback that returns a passing score when the run errors. If a validator crashes, log the
failure and fix the cause — a failed run is data, a fake pass is a corrupted run. A
converged design whose numbers did not come from an actual validator run is worthless.

> **Architecture note.** An earlier draft of this file described the validators as a remote
> service behind `POST {VERIFIER_URL}/verify`, reachable only over the network. That is not
> how this repository is built and there is no `VERIFIER_URL`. The validators are local
> Python modules, made tamper-evident by the git allowlist above rather than by network
> isolation. Wherever the sections below still read as though scoring happens elsewhere,
> §6 is the authority on how you actually invoke it.

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

> **This is the target, not the current state.** The real entry point is
> `build_implant(params) -> cq.Workplane` in `autoimplants/generator.py`, and its signature is
> frozen — the bone is reached through `autoimplants/bone.py` (`surface_profile`,
> `max_surface_x`) rather than passed in, and there is no `prescription` argument (§11).
> Today it builds a **flat** plate standing clear of the apex of the bow, which is exactly why
> `bone_conformance_gap` fails. Turning that flat plate into the swept solid below is the job.
> `params.py` already declares the handles for it — `contour_spline`, `thickness_profile`,
> `ribs`, `hole_slots` — and `build_implant()` raises `NotImplementedError` if you set one
> without writing the geometry behind it. That guard is deliberate: setting a parameter is not
> a substitute for building the shape.

```python
# Target shape. Adapt to build_implant(params) / autoimplants.bone, per the note above.
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

The right-hand column is what `DEFAULT_PARAMS` in `autoimplants/params.py` actually ships
with on this case. Where it differs from the general guess, the case wins.

| Parameter | General start | Range | This case |
|---|---|---|---|
| `length` | 140 mm | 80–190 | `length_mm` **180** — at the `max_length_mm` ceiling; keepouts block both ends |
| `width` | 12 mm | 10–14 | `width_mm` **16** — broad plate; hard ceiling 17.6 mm at the vessel keepout |
| `thickness` | 3.5 mm | 3.0–5.0 | `thickness_mm` **3.0**, bounded 2.5–4.5 |
| `n_holes` | 8 | 6–12 | **6, fixed** — locked surgical input, not a parameter |
| `hole_spacing` | 13 mm | 10–16 | **30 mm, fixed** — from `inputs/screw_positions.json` |
| `hole_dia` | 3.5 mm | 3.5 or 4.5 | `hole_diameter_mm` **4.5** (compression) |
| `clearance` | 0.2 mm | 0.1–1.0 | `mount_clearance_mm` **0.4**, held at the apex of the bow; enforced 0.1–1.5 |
| `fillet_radius` | 0.5 mm | 0.5–2.0 | `fillet_mm` **1.0** |

Note `length` and `width` are already at or near their ceilings. The mass budget is 37.0 g
of 55, so there *is* headroom — but the sweep in `design_space_note` shows no
constant-thickness design spends it well enough to pass (best case 396 MPa against a 350
limit). The headroom is there to be redistributed, not to be spread evenly. That is what
makes this a geometry problem rather than a tuning problem.

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

> **This lever is not available on the current case, and you should not try to use it.**
> Two reasons. First, all six screw positions are pre-solved surgical planning input and
> `require_all_screws` is 6 — the implant accommodates them, they do not move. Second, and
> more fundamentally, nothing here measures stiffness (§6), so there is no quantity for a
> working-length change to improve. Omitting a hole in this model just leaves solid material
> where the bore was, which raises local stiffness — the opposite of the intent above.
> Modelling this properly needs an explicit "which screws are engaged" input plus a stiffness
> check; neither exists yet. Reach for section geometry instead: a thickness profile, a rib,
> a contour that follows the bone.

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

These are the general targets for this class of device. **This case tightens several of
them** — `inputs/case.json` is authoritative, and the right-hand column is what you are
actually measured against.

| Quantity | General target | This case (`inputs/case.json`) |
|---|---|---|
| `safety_factor` | `>= 2.5` | expressed as enforced `max_stress_MPa` 350 — 880 / 2.5 = 352, rounded down. **Same constraint, not a different one.** |
| `stiffness_N_per_mm` | `100 <= k <= 400` | **not implemented** — no stiffness check exists |
| `mass_g` | `<= 40` | **55** for this calibrated synthetic design-space challenge |
| `max_thickness_mm` | `<= 6.0` | thickness bounded **2.5–4.5** (tighter). `max_standoff_mm` 6.0 separately caps outer protrusion |
| clearance | `0.1 <= c <= 1.0` | **`0.1 <= c <= 1.5`** — floor as stated, ceiling relaxed to 1.5 |
| plate width | 10–14 mm typical | **16 mm baseline, hard ceiling 17.6 mm** at the vessel keepout. This is a broad plate on a 26 mm shaft; the keepout, not soft tissue, is what binds |
| `overhang_deg` | `<= 45` | **not implemented** |
| `min_feature_mm` | `>= 0.8` | expressed as `min_wall_mm` **2.5** (much tighter) |

Do not rewrite the case-specific numbers from generic guidance. See `design_space_note` in
`inputs/case.json`: the mass cap, gait moment, and keepouts are calibrated together so scalar
parameter tuning remains insufficient on purpose.

Why safety factor 2.5 and not 1.0: the plate is loaded thousands of times per day and
fails by **fatigue**, not single overload. Material has scatter, printed parts have
defects, loads are estimates, and a patient may stumble. The margin covers all of that.
Treat 2.5 as a floor, not a target to sit exactly on.

### How you actually run the validators

```bash
bash setup.sh                                       # first time only
.venv/bin/python -m autoimplants.run --validators geometry,fea
```

Exit code **0** means the design passes, **1** means it does not — branch on `$?`. The full
report is written to `out/report.json`, and the same content is printed as a table:

```
CHECK                        STATUS      VALUE / LIMIT      UNIT
------------------------------------------------------------------------------
implant_mass                 PASS       36.996 / 55         g
bone_conformance_gap         FAIL        8.596 / 1.5        mm     at (35.4, 6, 100)
    -> implant stands 8.60 mm off the bone at y=6.0, z=100 mm; the plate must follow the contour
bone_clearance_min           PASS        0.398 / 0.1        mm     at (35.4, 0, 202)
stress_max_bending           FAIL       389.85 / 350        MPa    at (36.9, 0, 175)
```

Every check carries a measured `value`, the `limit` it was tested against, and where in the
part it happened. **Read the locations** — they exist so you do not have to guess. Re-run
after every change; never commit a design you have not validated.

On Windows the interpreter is `.venv/Scripts/python.exe`.

### What is enforced today

The single most important table in this file. A check marked SKIP **cannot fail your run** —
`SKIP` counts as OK. Do not optimise against a number nothing is measuring.

| Check id | Limit source | Status |
|---|---|---|
| `manifold_watertight` | `require_watertight` | **enforced** |
| `envelope_length` / `envelope_width` / `envelope_standoff` | `envelope.max_*` | **enforced** |
| `min_wall_thickness` | `min_wall_mm` = 2.5 mm | **enforced** |
| `implant_mass` | `max_implant_mass_g` = 55 g | **enforced** |
| `no_bone_collision` | 0 vertices inside bone | **enforced** |
| `bone_conformance_gap` | `max_bone_gap_mm` = 1.5 mm | **enforced** |
| `bone_clearance_min` | `min_bone_gap_mm` = 0.1 mm | **enforced** |
| `screw_trajectories_clear` | `require_all_screws` = 6 | **enforced** |
| `keepout_*` (3 zones) | `max_keepout_encroach_mm` = 0 | **enforced** |
| `stress_max_bending`, `stress_hole_0..5` | `max_stress_MPa` = 350 | **enforced** — beam model over sections measured off the exported solid (`autoimplants/section.py`) |
| `screw_pullout_min` | `min_screw_pullout_N` = 1200 | **SKIP — not a design variable.** Pull-out depends on bone quality and screw geometry, both locked planning inputs. No change to the plate moves it. |

Consequences you must reason about, because they are not obvious:

- **There is still no FEA.** No mesh convergence data, no von Mises field, no stiffness
  value. What `validators/stress.py` now runs is beam theory over cross-sections measured
  off the exported solid by ray casting (`autoimplants/section.py`): area, second moment
  and extreme-fibre distance at every station, plus a Heywood `Kt` at each hole. It is a
  reduced-order surrogate and says so — but it is measured, not asserted, and it responds
  to ribs, thickness profiles and slots because it reads the geometry rather than the
  parameters.
- **`load_cases` in `inputs/case.json` is live.** The 2100 N axial force and 7 Nm bending
  moment are applied: the moment peaks over mid-footprint and tapers to the outermost
  screws, on the assumption that the plate bridges the fracture and load re-enters the bone
  through the end screws. The plate is assumed to carry the moment alone, with no load
  sharing — conservative for a bridging plate, badly wrong for an intact shaft.
- **Read `validators/stress.py`'s docstring before trusting a number from it.** The four
  modelling assumptions are listed there, and they are the first thing anyone will ask
  about.
- **The stiffness window and the printability checks described elsewhere in this file do not
  exist here.** Treat them as design guidance, not as gates.
- The failure you are being asked to fix is therefore **geometric**: the plate does not
  follow the bone.

---

## 7. Reading the FEA result correctly

> **Partly applicable — read it anyway, once.** There is no FEA in this repository: the
> stress checks are beam theory over measured sections (§6), not a solved field. So there is
> no hotspot to locate and no `mesh_convergence` block to read, though the stress numbers
> themselves are now live. The
> section is retained because the reasoning below is why the stress thresholds are shaped the
> way they are, and because it must not be mistaken for a description of live behaviour. In
> particular, the claim that "the verifier already excludes elements within 1–2 element
> lengths of any constrained node" describes an FEA harness that is **not wired up here** —
> do not rely on it.

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

- **Iteration cap: 8**, set by `iteration_budget` in `inputs/case.json` — the harness reads
  it, so that file is the one number. This is an ACU cost guardrail, not a physics limit.
- A validator run is local and takes a few seconds, so checking your work is nearly free.
  Run it after every edit; there is no call budget on validation, only on sessions.
- If you have used half your iterations and no check has improved, you are stuck on a shape
  problem. Change the geometry structurally instead of continuing to tune.
- If two consecutive iterations return identical metrics, your edit did not reach the
  geometry. Confirm the exported solid actually changed before spending another iteration.

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
- **Export both, and keep STEP as the deliverable.** `autoimplants/export.py` already does
  this. STEP carries the real curved surfaces; the STL is what the geometric checks measure,
  because ray casts and volume queries need a mesh. Its tolerance is set tight enough that
  faceting cannot fail a threshold on its own — so do not "fix" a check by loosening it.
- **Tag your geometry** *(not yet supported — `Check` carries an `[x, y, z]` location but no
  parameter tag; skip this until the schema gains one).* When you create a face, record which
  parameter produced it. The verifier maps a hotspot back to `fillet_radius_hole_3` through
  those tags. Untagged
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

Renders are advisory: they tell you *why* something failed and roughly where to look. The
verdict that counts is the one `python -m autoimplants.run` writes to `out/report.json` — the
locked validators are the authority, and a render that looks right is not a passing run.

---

## 11. Prescription input

> **Not implemented on this case.** There is no `prescription` object in this repository — no
> `mode`, no `fracture_level`, no `fracture_type`, no `species`. The equivalent inputs are
> fixed in `inputs/`: the footprint is `case.json`'s `footprint_z_mm` `[100, 280]`, the
> approach is the lateral (+X) aspect, and the six screw positions are pre-solved in
> `screw_positions.json`. So the hole-placement modes in §2 have nothing to switch on yet, and
> the species scaling below does not apply — this is one adult human femur. Read the section
> for the reasoning, not for fields to consume.

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

A run succeeds when `python -m autoimplants.run --validators geometry,fea` exits 0, with an
iteration log recording each change and its rationale. Ties are broken by lower mass, then by
fewer iterations.

`autoimplants/export.py` writes both formats on every build. **STEP is the deliverable** — the
artefact a manufacturer would receive, with real curved surfaces. **The STL is the measurement
surface**: the geometric checks are ray casts and volume queries, which need a mesh, and its
tessellation tolerance (0.02 mm linear deflection) is set an order of magnitude tighter than
any threshold, so faceting error alone cannot fail a check. Both must exist at the end of a
run; if the STEP export warns, treat that as a defect to fix, not noise.
