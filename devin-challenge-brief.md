# Devin Challenge — Team Brief

**Domain: patient-specific orthopedic implants (tibial fixation plates)**

## The problem

Custom bone plates are designed by hand. A biomedical engineer takes a patient's CT scan, sculpts a plate in CAD over days, runs FEA, and reshapes it when stress concentrations fail. Two to six weeks per case. That engineer is the scaling bottleneck, which is why patient-specific implants stay a premium product instead of the default.

## Why this domain has the loop

FEA is the industry's test suite and it already exists. Safety factor and von Mises stress are numeric pass/fail. We don't have to invent a verdict, we have to wire an agent into one that surgeons already trust.

## What we build

Trigger: a segmented bone mesh drops in. Devin writes a parametric CadQuery script — plate path, cross-section profile, thickness taper, screw hole count, placement, and trajectory. The verifier meshes it, solves it, and returns structured failure metrics. Devin revises. The loop runs unsupervised until every constraint passes.

Output: printable STL, sliced G-code, and a simulated **ASTM F382** four-point bend report plus a physiological gait load case.

## The verdict engine

This is the actual product. Not just "safety factor >= 2.5" — a dumb agent passes that by thickening everything into a brick, so we constrain from both sides.

| Constraint | Why |
|---|---|
| Safety factor >= 2.5 | Standard structural requirement |
| **Stiffness ceiling** | Too stiff causes stress shielding and refracture. The target is a window, not a maximum. This is what stops Devin gaming the test. |
| Mass budget + max thickness | Soft-tissue impingement |
| Non-intersection, 0.2 mm clearance to bone | Anatomical fit |
| Screw trajectory vs. bone axis | Purchase and cortex breach |
| Printability (overhang angle, min feature size) | Must survive DMLS |

## Autonomy story

Judges will say "medtech requires human sign-off, so this is a copilot." The answer: **nobody touches the design loop.** A human signs the design history file the same way a human merges a PR. Iteration 1 and iteration 40 are seen, 2 through 39 are not. The engineer goes from one design in three days to twenty validated candidates in an hour.

## Scope decisions already made

- **Tibia, not distal radius.** Loads are unambiguous (body weight x gait factor), safety factor genuinely binds, and stress shielding is documented, which gives us the stiffness ceiling.
- **No CAD booleans against bone.** Spline path along the bone surface, sweep the cross-section, check clearance numerically in `trimesh`. Booleaning against a 200k-triangle mesh is where this build dies.
- **Crop and decimate the bone.** Cylinder around the plated segment only, roughly 20k triangles. Drops DOF by an order of magnitude, which is what makes 40 iterations demoable.
- **Generality is non-negotiable.** TotalSegmentator on public CT, or meshes from MedShapeNet. We run five unseen anatomies live. Wiring it to one prepared example is explicitly called out in the brief as a losing move.
- **Verifier before generator.** If the CAD is mediocre but the verifier works, we still have a loop to show. Reverse the order and we have pretty geometry that nothing judges.

## Known risks — say them before the jury does

- **FEA can be confidently wrong.** A bad boundary condition or a coarse mesh returns a number that looks like a verdict and isn't. Mitigation: closed-form cantilever benchmark, mesh convergence check, unit-consistency assertion.
- **Devin will try to cheat the test.** Any single-sided constraint gets gamed. Mitigated by the stiffness ceiling, mass budget, and thickness cap above.
- **The geometry pipeline is the likely point of failure**, not the agent. Watertight volumes that gmsh will mesh and CalculiX will solve is the six-hour trap.
- **Solve time caps iteration count.** If a single FEA run is slow, the loop can't converge inside a demo. Cropping and decimation are the mitigation.
- **nTop and Materialise 3-matic already do implicit modeling with integrated FEA.** Name them before a judge does. The line: those are tools a designer drives, we're building the driver.
