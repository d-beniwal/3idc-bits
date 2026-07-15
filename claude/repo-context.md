# Repository context (quick grounding)

## What this repo is

A **Bluesky Instrument (BITS)** package named `id3c`, deployed at APS
beamline **3-ID-C**, built on the `apsbits` framework. Authoritative
agent/developer conventions live in the repo's top-level `AGENTS.md` — read
it for anything not covered here.

> Note: This is the **3-ID-C BITS** codebase (`id3c`). It is distinct from
> the Sectors 1/20 legacy codebase — do not conflate the two.

## Top-level shape

```
src/id3c/
    startup.py          # session bootstrap; loads devices, wires interlocks
    configs/            # YAML: devices, iconfig, qserver
    devices/            # ophyd Device subclasses
    plans/              # bluesky plans and plan stubs
    callbacks/          # RE document subscribers
    suspenders/         # (currently empty)
    qserver/            # bluesky-queueserver configuration
    utils/              # shared helpers
docs/source/            # Sphinx docs (Diataxis layout)
.pre-commit-config.yaml # ruff, ruff-format, standard pre-commit-hooks
.github/workflows/      # CI (lint advisory; docs build + deploy advisory)
```

## Working conventions (safety-critical)

- **Never connect to beamline hardware or EPICS PVs.** Read and edit source
  only. Do not run code that talks to instruments.
- Match existing code style; `pre-commit` runs ruff + ruff-format.
- Keep `main` a clean mirror of upstream; do feature work on branches.

## How this fork is used

See `workflow.md`. In short: work on `feature/*` branches off `main`, push
to the `origin` fork, and open PRs against `BCDA-APS/3idc-bits`. This
`claude/` folder is carried only on the `claude-context` branch and never
enters those PRs.
