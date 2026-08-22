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

## Parallelism is the headline, not a footnote

The brief treats concurrency as an implementation detail behind a throughput number. It is actually the strongest argument in the deck, and this domain supports it better than most. Seven points, roughly in order of how much they buy:

**The output is a Pareto front, not a winner.** The constraints are two-sided by design — strong but not too stiff, light but not too thin. There is no single optimum, there is a trade-off surface. A serial human designer produces *one point* on that surface and calls it the answer, because producing a second one costs another three days. The layer returns the frontier and lets the surgeon choose the trade-off. That is a **better product, not merely a faster one** — and it exists only because of parallelism. This is the best sentence available to the team and the brief currently doesn't contain it.

**Parallelism is what a copilot structurally cannot do.** An engineer cannot drive twenty CAD sessions at once — not slowly, not ever. This is not "the same work, faster," it is a mode of working unavailable to a human-in-the-loop tool at any speed. Put this directly beside the existing autonomy rebuttal; it is the same argument and it is the stronger half.

**The candidates are genuinely independent.** A candidate is a point in a parameter space: plate path, cross-section profile, thickness taper, screw count, screw placement. No shared state, no merge, no coordination between sessions. Embarrassingly parallel in the strict sense — worth saying in those words, because it tells a technical judge the fan-out is real rather than decorative.

**The numeric verdict makes parallel results self-ranking.** Safety factor, stiffness, mass, and clearance are numbers. Twenty candidates return and the layer sorts them with no human comparing CAD models side by side. Parallelism only pays if you can *choose automatically* at the end — here you can, and that closes the loop between the verdict engine and the fan-out. Most teams miss this half.

**A Devin session is a whole machine, not a thread.** This answers the judge's question "why Devin and not a for-loop around an LLM call?" Each candidate must write code, mesh geometry, run a CalculiX solve, read results, and revise — it needs its own filesystem and toolchain, not a chat turn. Devin's unit of concurrency is exactly that unit. Twenty parallel sessions are twenty engineers at twenty workstations.

**Failure isolation buys demo robustness.** A session that hangs gmsh, emits a non-watertight solid, or diverges costs one candidate out of N, not the run. In a serial loop, one bad geometry ends the demo. Saying this converts the brief's own *"the geometry pipeline is the likely point of failure"* risk into a reason the architecture is right.

**Two axes to fan out over, and the demo should show both.** Anatomies (five unseen tibiae) × design strategies (K parameterisations each) is a live matrix filling in on screen. It reads from the back of the room in a way a single progressing session never does.

### What the orchestrator actually has to do

Scheduling *is* the visible work of the layer, so name it concretely:

- **Concurrency cap** — a bounded worker pool over the candidate queue, sized by ACU budget rather than ambition.
- **Per-session budget** — `max_acu_limit` at create time, so no runaway candidate eats the event's credits.
- **Uniform boot** — one `snapshot_id` for every session, so all N start from an identical toolchain and their results are actually comparable.
- **Machine-readable verdicts** — `structured_output_schema` so results merge into a leaderboard without parsing prose.
- **Grouping** — `tags` per case and per candidate; this is what the fan-out board queries.
- **Escalation policy** — what happens to a candidate that fails repeatedly: kill it, or respawn with a relaxed seed or a different parameterisation. A scheduler that reallocates budget from dead candidates to live ones is a real orchestration story, not a loop.
- **Front assembly** — collect survivors, drop near-identical designs, rank, publish the Pareto set.

Those field names are verified against the Devin v3 API — see [devin-api-setup.md](devin-api-setup.md).

**One caution: parallelism multiplies cost, so state the number.** N candidates × 40 iterations is a real ACU bill. A judge who asks "what did this run cost?" should get an answer, and a per-session cap is the evidence you thought about it before they asked.

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
- **Fan-out** — *lead with this.* N concurrent sessions over anatomies × design strategies, each an independent candidate; the survivors form a Pareto front ranked by mass and stiffness margin, not a single winner. See the parallelism section above for the arguments to compress into this bullet.
- **Scheduling** — concurrency cap, per-session ACU budget, escalation and respawn for repeatedly failing candidates.
- **Termination policy** — pass, iteration cap, wall-clock cap, hard-fail.
- **Artifact store** — where STL / G-code / report / design-history JSON land, keyed by case and candidate.
- **Environment** — one machine snapshot with gmsh, CalculiX, CadQuery, trimesh pre-baked, shared by every session.

A diagram would carry this section on a slide better than prose — and it should show the width, not just the sequence: `intake → orchestrator → { N Devin sessions in parallel } → verifier gate → Pareto front → artifact store`. Drawing the fan as a fan is half the argument.

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
2. **The board explodes** — N sessions spawn at once, the anatomy × strategy matrix fills in live. This is the money shot; give it screen time and let the count climb while you talk.
3. Zoom into one cell: that session's transcript, fail → revise → pass, with the stiffness ceiling visibly rejecting a brick. Then zoom back out to show the other nineteen still running.
4. Artifacts open: STL, sliced G-code, ASTM F382 report — and the **Pareto front**, several valid designs trading mass against stiffness, with the line "a human picks the trade-off, not the geometry."
5. Close on the counter: one design in three engineer-days → twenty validated candidates in an hour, and the ACU cost of having done it.

### G. Optional trims

The brief is dense and well-argued, but "Scope decisions already made" and "Known risks" overlap (cropping/decimation appears in both). Merging the duplicated reasoning buys you the space for section B without growing the document.

## One-line framing to carry through

The slide's closing line is *"Every industry has an engineering problem. Give it an engineer."* The brief's own best sentence — **"those are tools a designer drives, we're building the driver"** — is the same idea, sharper. Consider promoting it out of the risks section and into the opening.

Parallelism gives it its ending: *give it an engineer* → **give it twenty, working at once, and keep the ones that pass.** The slide asks for an engineer; the answer this domain allows is a department.
