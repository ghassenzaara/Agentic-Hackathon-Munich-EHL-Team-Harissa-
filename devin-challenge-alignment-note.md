# Alignment note — `devin-challenge-brief.md` vs. the "Devin for X" track brief

Source of truth: the track slide (`artifacts/image.png`).

> **Devin for X — Building the Autonomous Layer**
> Your job is to find another industry with that same structure and build the layer that puts Devin to work inside it **using our API**.
> 01 — Pick a domain where the output can be expressed as code.
> 02 — Then build a system that **programmatically triggers Devin sessions**, **gives them a way to check their own work**, and **produces real artifacts** **without a human in the loop**.
> *Every industry has an engineering problem. Give it an engineer.*

## Scorecard

| Requirement | Status | Evidence in the brief |
|---|---|---|
| Domain where output is code | **Strong** | Parametric CadQuery script is the artifact; geometry *is* source code, diffable and revisable. Best-in-class fit. |
| Domain has an existing engineering problem | **Strong** | Hand-sculpted plates, 2–6 weeks/case, one engineer is the bottleneck. |
| A way for Devin to check its own work | **Strong** | The verdict engine, and specifically the two-sided constraints (stiffness ceiling, mass budget) that stop reward hacking. This is the brief's best section. |
| Produces real artifacts | **Strong** | STL, G-code, ASTM F382 bend report, gait load case. |
| Without a human in the loop | **Strong (as argument)** | The "iteration 1 and 40 are seen, 2–39 are not" line pre-answers the copilot objection. |
| **Programmatically triggers Devin sessions** | **Missing** | The word "trigger" appears once, passively: *"a segmented bone mesh drops in."* No mechanism. |
| **Using the Devin API** | **Missing** | The Devin API is never mentioned. "Devin" reads as a stand-in for "an agent." |
| **Building the *layer*** | **Weak** | The brief describes a domain and a verdict function. It does not describe the system that operates them. |
| **Parallel fan-out / orchestration** | **Badly under-sold** | Exists only as the number *"twenty validated candidates in an hour."* This is the single most visible difference between a layer and a script, and it is currently a throwaway clause. |

**Summary:** the brief nails requirement 01 and two of the three clauses in 02. The whole gap is on the left-hand clause — *the layer itself*. Right now this reads as an excellent domain pitch that a judge could mistake for "a CAD script with an FEA check," rather than an orchestration layer that happens to point at orthopedics. The fastest way to close that gap is to put **parallelism** at the front: it is the one thing on screen that cannot be mistaken for a script, and this domain happens to parallelize unusually cleanly.

## The five specific gaps

1. **No trigger mechanism.** "A mesh drops in" is not a trigger. Needs to name the concrete path: watched inbox / object-store event / webhook → a service that calls the Devin API to create a session, with the case ID, mesh URI, and constraint spec in the prompt.
2. **No session lifecycle.** Nothing on how the verifier's structured failure metrics get *back into* the running session, when a session is terminated vs. restarted, what the max-iteration and wall-clock stop conditions are, or what happens on a hard failure.
3. **No fan-out.** The brief's own headline claim — *"twenty validated candidates in an hour"* — only makes sense as N concurrent Devin sessions over a design-parameter sweep, but the brief never says so. This is the largest missed opportunity in the document and it gets its own section below.
4. **Verifier placement is undecided.** "Gives them a way to check their own work" is a hint about *architecture*, not just about having a metric. The verifier should be a command Devin can invoke inside its own session (a test suite: `verify --case X` → structured JSON verdict), *and* be re-run by the layer as an independent gate before an artifact is accepted. Self-check and gate are two different runs of the same code. The brief doesn't distinguish them.
5. **No environment story.** gmsh, CalculiX, trimesh, CadQuery must exist in the Devin machine snapshot, or every session burns its first ten minutes on `apt install`. This is a real hackathon-day failure mode and belongs next to the other risks.

## Proposed edits to `devin-challenge-brief.md`

Keep every existing section — the domain reasoning, the constraint table, the scope decisions, and the risks are all doing work. The changes are additive plus two rewrites.

### A. Rewrite the opening line of "What we build"

Replace *"Trigger: a segmented bone mesh drops in. Devin writes…"* with a named trigger and an explicit API call. Something with this shape:

> A case lands in the intake bucket (segmented mesh + patient load profile). The orchestrator creates a Devin session through the Devin API, seeded with the mesh URI, the constraint spec, and the verifier contract. Devin writes the parametric CadQuery script, runs `verify` inside its own session, reads the structured failure metrics, and revises. The orchestrator polls session state, re-runs the verifier independently as an acceptance gate, and closes the session when every constraint passes or the iteration cap is hit.

### B. Add a new section: **The layer** (place it directly after "What we build")

The section the challenge is actually asking for. Cover, briefly:

- **Trigger** — what event creates a session, and the API call that does it.
- **Session seeding** — what goes into the initial prompt: mesh URI, constraint spec, verifier contract, repo with the verifier pre-installed.
- **Feedback channel** — how a verdict re-enters a live session (a message to the session vs. Devin re-invoking the verifier itself; state which and why).
- **Fan-out** — N sessions over a parameter sweep, each an independent candidate; leaderboard ranks survivors by mass and stiffness margin.
- **Termination policy** — pass, iteration cap, wall-clock cap, hard-fail.
- **Artifact store** — where STL / G-code / report / design-history JSON land, keyed by case and candidate.
- **Environment** — the machine snapshot with gmsh, CalculiX, CadQuery, trimesh pre-baked.

A one-line ASCII or Mermaid diagram of `intake → orchestrator → N Devin sessions → verifier gate → artifact store` would carry this section on a slide better than prose.

### C. Add a **Devin API surface** subsection or appendix

Name the endpoints the layer uses — session create, session status/poll, message-into-session, secrets, and structured output if you rely on it — so a judge can see the integration is real. Confirm the exact shapes against current Devin API docs before the write-up; don't ship guessed endpoint names in a document judges will read.

### D. Add two risks to "Known risks"

- **Cold-start environment cost.** Every session that installs its own toolchain is ten minutes of the demo. Mitigation: pre-baked machine snapshot, verified the night before.
- **API-level failure modes.** Rate limits, session stalls, ACU burn during a 40-iteration loop. Mitigation: concurrency cap, per-session iteration budget, cost estimate stated up front. Knowing your own ACU number is a credibility signal.

### E. Sharpen the autonomy section with evidence, not just argument

The copilot rebuttal is good but currently rhetorical. Make it checkable: state that the demo will show the session transcript with **zero human messages** between session creation and the passing verdict, and that the design history file records every iteration. "Look at the log" beats "trust the framing."

### F. Add **What judges see** (60–90 seconds)

Not in the brief at all, and it is what the score is actually assigned to. Suggested beat sheet:

1. Drop five unseen anatomies into intake. Nothing else is touched.
2. Sessions spawn live; the fan-out board fills in.
3. One session's transcript on screen: fail → revise → pass, with the stiffness ceiling visibly rejecting a brick.
4. Artifacts open: STL, sliced G-code, ASTM F382 report.
5. Close on the counter: engineer-days per case → candidates per hour.

### G. Optional trims

The brief is dense and well-argued, but "Scope decisions already made" and "Known risks" overlap (cropping/decimation appears in both). Merging the duplicated reasoning buys you the space for section B without growing the document.

## One-line framing to carry through

The slide's closing line is *"Every industry has an engineering problem. Give it an engineer."* The brief's own best sentence — **"those are tools a designer drives, we're building the driver"** — is the same idea, sharper. Consider promoting it out of the risks section and into the opening.
