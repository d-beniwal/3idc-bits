# Agent notes for the 3-ID-C BITS instrument

Context for AI coding agents (and curious humans) working in this
repository.  These conventions were established interactively and are
expected to apply to all subsequent work unless explicitly overridden.

Human-facing developer guidance lives in `docs/source/contributing.md`
(which links back here for the conventions themselves).

## What this repo is

A [Bluesky Instrument (BITS)](https://github.com/BCDA-APS/BITS) package
named `id3c`, deployed at APS beamline 3-ID-C.  Built on the
`apsbits` framework.

Top-level shape:

```
src/id3c/
    startup.py          # session bootstrap; loads devices, wires interlocks
    configs/            # YAML: devices, iconfig, qserver
    devices/            # ophyd Device subclasses
    plans/              # bluesky plans and plan stubs
    callbacks/          # RE document subscribers
    suspenders/         # currently empty
    qserver/            # bluesky-queueserver configuration
    utils/              # shared helpers
docs/source/            # Sphinx docs (Diataxis layout)
.pre-commit-config.yaml # ruff, ruff-format, standard pre-commit-hooks
.github/workflows/      # CI (lint advisory; docs build + deploy advisory)
```

## Off-network reality

This repo is developed on `wow.xray.aps.anl.gov`, which **cannot reach
the beamline's EPICS PVs**.  All ophyd `connect()`/`wait_for_connection()`
calls will time out on this host.  Tests and verification must therefore:

- exercise object construction, attribute wiring, and pre-flight logic;
- not depend on live EPICS;
- accept "passed pre-flight; failed elsewhere with `DisconnectedError`"
  as the expected positive result for any code path that ultimately
  issues a CA put.

See the operator notes at `~/.config/opencode/AGENTS.md` for further
host-environment specifics (rootless podman, NFS gotchas, conda-forge
local-build workarounds).

## Established conventions

### Plan invocation pattern

User-facing examples must use the **`RE(plan_or_stub(...))`** pattern
consistently, never the bare `plan_or_stub(...)` form (which silently
does nothing — the generator is created and discarded).

```python
# Correct in examples and docs:
RE(bps.mv(sample_stage.xprime, 12.3))
RE(bp.scan([scaler], sample_stage.xprime, 0, 10, 11))
RE(laser_optics.move_out())            # plan method on a Device

# Wrong in examples (silently does nothing):
bps.mv(sample_stage.xprime, 12.3)
laser_optics.move_out()
```

Direct ophyd calls (`motor.move(...)`, `signal.get()`,
`device.read()`, property access like `laser_optics.is_out`) are **not**
plans and must **not** be wrapped in `RE(...)`.  This is the single
biggest source of confusion for SPEC and EPICS users moving to Bluesky;
docs must call it out explicitly.

### `@plan` decorator on our own plans

All plan and plan-stub functions we author are decorated with
`bluesky.utils.plan`.  The decorator wraps the generator in a `Plan`
object that emits a `RuntimeWarning` at garbage-collection time if it
was never iterated.  This makes "I forgot to wrap with `RE(...)`" a
visible warning instead of silent no-op.

```python
from bluesky.utils import plan

@plan
def my_plan(...):
    yield from bps.mv(...)
```

This applies to both **plans** (functions that emit run documents via
`open_run`/`create`/`save`/`close_run`) and **plan stubs** (functions
that compose into plans but do not emit documents on their own).  The
decorator does not distinguish the two; it warns equally about either.

### Interlock pattern

Bluesky-session interlocks between motors live in
`src/id3c/devices/<a>_<b>_interlock.py`, each exposing a
`setup_<a>_<b>_interlock(oregistry)` entry point that `startup.py` calls
after `make_devices()`.  Example: `omega_laser_interlock.py` with
`setup_omega_laser_interlock`.

The protected motor must be an instance of
`id3c.devices.interlocked_motor.InterlockedEpicsMotor` (subclass of
`ophyd.EpicsMotor`).  Its three interlock attributes (`interlock`,
`interlock_description`, `interlock_watch`) are assigned **late** by the
setup function, not at construction.  This avoids import cycles between
mutually-interlocked devices and keeps `InterlockedEpicsMotor`
generic (no knowledge of any particular other device).

When adding a new interlocked pair: new
`<a>_<b>_interlock.py` module, new `setup_<a>_<b>_interlock` function,
one new line in `startup.py`.  Do **not** generalize into a shared
"interlock framework" until at least three concrete cases exist.

### Scope: Python only, no EPICS-side protection

This Python-layer interlock works only inside the running Bluesky
session.  It does **not** write to motor `DISP` fields, does **not**
install IOC sequencer code, and cannot protect against MEDM jogs,
`caput`, other Bluesky sessions, SPEC, or a crashed Python process.
For session-independent hardware-grade protection, the right place is
the EPICS IOC (CALC/SCALC, state-notation sequencer, or a soft record
driving `DISP`).  Always say this honestly in docs and code comments.

### `mb_creator` per-axis custom class

`apstools.devices.motor_factory.mb_creator` accepts per-axis dicts in
the `motors:` mapping, with keys `prefix`, `class`, `factory`, plus any
kwargs to be forwarded to the axis class constructor.  Use this to
declare custom motor classes inline in `devices.yml` rather than
hand-writing a `MotorBundle` subclass:

```yaml
motors:
  omega:
    prefix: "3idxps1:m5"
    class: id3c.devices.interlocked_motor.InterlockedEpicsMotor
    interlock_description: "laser_optics OUT"
```

Any custom kwarg the axis class wants (e.g. `interlock_description`)
must be popped from `**kwargs` in the class `__init__` before
`super().__init__(**kwargs)`, because `EpicsMotor` does not tolerate
unknown kwargs.

A hand-rolled `MotorBundle` subclass is still required when the bundle
needs non-motor Components (config Signals, AttributeSignals), property
methods, or plan methods on the bundle itself.  Example:
`id3c.devices.laser_optics.LaserOptics`.

## Code style and QC

- `ruff` + `ruff-format`, configured in `pyproject.toml`.
- `pre-commit` hooks run them locally (after `pre-commit install`).
  The hook is **optional**; CI lint is **advisory** (does not block
  merging).  Opt-out is fully supported.  See
  `docs/source/contributing.md`.
- The pre-commit hook will reformat files it modifies; expect to
  re-stage after a failed commit.
- D-series docstring rules are enabled (`D100`-`D107`).  Every public
  module, class, and function (including `__init__`) must have a
  docstring.

## Documentation conventions

- Source in `docs/source/`, output in `docs/build/` (gitignored).
- New pages default to **Markdown** (`.md`).  ReStructuredText is fine
  where it's genuinely easier (e.g. autoapi templates).
- MyST extensions enabled: `colon_fence`, `deflist`, `tasklist`,
  `linkify`, `attrs_inline`, `attrs_block`, `dollarmath`.
- API reference is generated by `sphinx-autoapi` at build time, under
  `docs/source/api/`.  **Not committed.**
- Diataxis structure: `tutorials/` (learning), `how_to/` (task),
  `reference/` (lookup), `explanation/` (understanding).  When in
  doubt, see [diataxis.fr](https://diataxis.fr/).
- Docs **CI** is advisory: builds on every push, deploys to `gh-pages`
  only from `main`.  A failed doc build will not block a PR.

## External services (3-ID-C specific facts)

- **Tiled server**: `http://sn.xray.aps.anl.gov:8000`.  Bluesky runs
  are stored under its `/raw` tree, written by `TiledWriter` as an RE
  subscriber.  This repo does **not** use `databroker` as a subscriber.
- **HDF5 visibility**: area-detector image files written by the
  detector IOC are linked from the run's master HDF5 file via the
  HDF5 external-link feature.  As of the date of this note, the
  detector-host-to-tiled-server file visibility path at 3-ID-C is not
  yet validated end-to-end.  When writing docs that show image reads,
  flag this honestly.

## Working style

These are imported from the user's host-wide notes
(`~/.config/opencode/AGENTS.md`) and apply here:

- Do **not** push or open PRs without an explicit request.  Commit
  freely when asked.
- Working notes in `.nogit_*.md` (gitignored in most BCDA-APS repos;
  worth checking this repo's `.gitignore`).
- `gh` is authenticated as `prjemian`; reading is fine, PR creation
  needs explicit request.

## When in doubt

Ask the user.  Most of the design decisions captured above came from
back-and-forth in chat sessions; not all of them are "obvious," and
deviating from them silently usually produces work that has to be
re-done.
