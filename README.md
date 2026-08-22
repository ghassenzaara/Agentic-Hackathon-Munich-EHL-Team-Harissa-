# ehl-munich-august-2026

Cognition "Devin for X" track — an autonomous layer for patient-specific
orthopedic implants (tibial fixation plates).

![X-rays of a fractured tibia repaired with metal fixation plates and screws, front and side views](artifacts/tibial_fixation_plate.png)

*A broken shin bone, front (a) and side (b). The metal screwed along the bone to hold the fragments in place while
they heal is the **fixation plate**. 

Today an engineer shapes each one by hand from a CT
scan, over weeks. That is the bottleneck.*

## Repository structure

```
.
├── README.md                          this file
├── LICENSE
│
├── devin-challenge-brief.md           the pitch: domain, verdict engine, autonomy story, risks
├── devin-challenge-alignment-note.md  gap analysis of the brief vs. the track requirements
├── resources.md                       datasets + software the build needs, and their status
├── devin-api-setup.md                 Devin v3 API contract, credential handling (key pending)
│
├── .env.example                       config template — copy to .env, never commit .env
├── .gitignore
│
├── pixi.toml                          the environment: toolchain + ccx solver + tasks
├── pixi.lock                          cross-platform lock (osx-arm64 + linux-64) — commit this
├── src/                               resources, fetch scripts, SSM loader (see src/README.md)
│
├── artifacts/
│   ├── image.png                      the track brief slide
│   └── tibial_fixation_plate.png      post-op X-rays, used in this README
│
├── skills-lock.json                   pinned Entire skill versions
├── .agents/skills/                    Entire agent skills (canonical copies, 12 skills)
├── .claude/
│   ├── settings.json
│   └── skills/                        symlinks into ../../.agents/skills/
├── .codex/
│   └── hooks.json                     Codex session hooks
├── agent/skills/                      Entire agent skills (generic-agent copy, 12 skills)
└── .entire/                           Entire session tracking (logs + metadata, gitignored)
    ├── .gitignore
    └── settings.json
```

## Getting set up

```bash
pixi install          # toolchain + CalculiX solver, from the lockfile
pixi run setup        # verify it, fetch the anatomy data, sample 5 unseen tibias
```

`pixi.lock` pins both `osx-arm64` (laptops) and `linux-64` (the Devin machine
snapshot, `DEVIN_SNAPSHOT_ID` in [.env.example](.env.example)), so the agent
solves against the same toolchain we develop against. Details and the one
deliberate version bump are in [pixi.toml](pixi.toml); resource status is in
[src/README.md](src/README.md).

The anatomy data is not committed. `pixi run setup` downloads it, or
`pixi run fetch-ssm` on its own (~550 MB, md5-checked, safe to re-run) into
gitignored `src/data/`; `pixi run fetch-ts` adds the optional CT mask set. Which
sources we picked, which ones failed to deliver, and why, is in
[resources.md](resources.md).

The design loop itself is not built yet, and the Devin API key is still to be
provided; see [devin-api-setup.md](devin-api-setup.md).
