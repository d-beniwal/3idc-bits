# Coming to Bluesky from EPICS

This page is for users who know EPICS Channel Access -- `caget`,
`caput`, `camonitor`, MEDM, and possibly PyEpics or `epics_get(FULL_PV)` from SPEC -- but
have not used Bluesky's device abstraction (ophyd) before.

## SPEC & PyEpics: PVs are addressed by string

In SPEC, you read a PV by typing its full name:

```
SPEC> epics_get("3idxps1:m5.RBV")
```

You write with:

```
SPEC> epics_put("3idxps1:m5.VAL", 30)
```

PVs are accessed by string.  Nothing wraps the PV; the string *is*
the handle.  If you mistype the name, SPEC complains at runtime.

PyEpics equivalents (`import epics`):

- `epics.caget("3idxps1:m5.RBV")`
- `epics.caput("3idxps1:m5.VAL", 30)`

## Bluesky: PVs are wrapped in ophyd Devices

In Bluesky, you do not address PVs directly.  Instead, a Python
object called a **Device** wraps a set of related PVs, and you
interact with the device:

```python
sample_stage.omega.user_readback.get()    # equivalent to caget("3idxps1:m5.RBV")
sample_stage.omega.user_setpoint.put(30)  # equivalent to caput("3idxps1:m5.VAL", 30)
```

A `EpicsMotor` device, for instance, wraps not just `.VAL` and `.RBV`
but also `.DMOV`, `.MOVN`, `.STOP`, `.HLM`, `.LLM`, `.EGU`, and others
-- about a dozen PVs.  You access them as attributes:

```python
sample_stage.omega.motor_done_move        # the .DMOV PV
sample_stage.omega.motor_is_moving        # the .MOVN PV
sample_stage.omega.user_offset            # the .OFF PV
```

This is the trade.  You give up the freedom of "any PV, any time"
in exchange for:

- A self-documenting object you can `tab-complete` in IPython.
- A connection object that knows how to wait for connection, fail
  cleanly on disconnect, and subscribe to monitors.
- A `read()` method that returns a structured dict of all "normal"
  signals at once (useful as a basic snapshot).
- Plug-and-play integration with the Bluesky RunEngine and the
  document stream.

## The connection model

`caget` opens a CA channel, reads once, closes the channel.  Cheap
per-call, but the channel does not persist.

ophyd Devices open a CA channel **per Signal** at instantiation time
and *keep them open*.  This means:

- A device is "connected" or "not connected"; check with
  `device.connected` (bool) or `device.wait_for_connection(timeout=5)`.
- Reads via `signal.get()` are local cache reads (cheap), not CA
  round-trips, after the first monitor establishes the cache.
- Writes via `signal.put(value)` go straight to CA.
- Subscriptions are first-class: `signal.subscribe(callback)` returns
  a subscription id; `signal.unsubscribe(cid)` removes it.

Off-network reality: on a workstation that cannot reach the IOC's
PVs, `device.wait_for_connection()` will time out; `signal.get()`
will raise `DisconnectedError`.  This is the expected failure mode
when testing without EPICS.

## `signal.get()` versus `device.read()`

These are subtly different and both useful:

- `signal.get()` returns the current value of one signal.  It is
  what you reach for when you want one number.
- `device.read()` returns a dict of *all* signals on the device
  whose `kind` is `hinted` or `normal`.  Used internally by the
  RunEngine to record an event document.

Example on an EpicsMotor:

```python
sample_stage.omega.user_readback.get()
# 30.0

sample_stage.omega.read()
# {'sample_stage_omega':            {'value': 30.0, 'timestamp': ...},
#  'sample_stage_omega_user_setpoint': {'value': 30.0, 'timestamp': ...}}
```

`read()` is the right call when you want a snapshot suitable for
saving; `get()` is the right call when you want a number.

## `motor.position` versus `signal.get()`

Instrument control rests on two fundamentals: **move positioners**
and **read detectors**.  Everything else builds on those.  This
section is about the first half: how ophyd models a positioner, and
why "where is this thing?" has two valid answers in Python.

Both `motor.position` and `motor.user_readback.get()` return a
single number representing "where this is right now," and in normal
use they agree.  The distinction is what kind of ophyd object each
is defined on:

- **`motor.position`** is a property defined on *positioners* --
  any device that inherits from `ophyd.positioner.PositionerBase`
  (`EpicsMotor`, `SoftPositioner`, `PVPositioner`,
  `PseudoPositioner`, and our `InterlockedEpicsMotor` via
  `EpicsMotor`).  It returns Python's best understanding of the
  positioner's current position.

- **`signal.get()`** is the method on *Signals* -- anything that
  inherits from `ophyd.signal.Signal` (`EpicsSignal`,
  `EpicsSignalRO`, `Signal`, `AttributeSignal`, `DerivedSignal`).
  It returns the current value of that one signal.

A motor is a Device containing Signals (and possibly other
Devices).  Devices nest arbitrarily -- `sample_stage` is a Device
of motor devices; `laser_optics` adds config Signals
(`in_position`, `out_position`, `tolerance`) and derived state on
top.  The recursive structure is what makes paths like
`sample_stage.xprime.user_readback` meaningful: each `.` walks one level
down the tree.

### A note on freshness for EPICS-backed objects

`EpicsSignal` and `EpicsSignalRO` keep their cached value
up-to-date in the background via CA monitors.  `signal.get()`
therefore returns the most recent monitor-event value without a CA
round-trip.  Same for any positioner property whose update is wired
to those signals -- `EpicsMotor.position` reflects the latest
readback monitor it has received.

A `SoftPositioner` might not have CA in the loop -- it could be a
pure-Python simulator or a wrapper with greater complexity.  Its
`.position` reflects whatever its local logic has set, whether that
comes from a simulation, a derived calculation, or values pulled
from elsewhere.

To force a fresh CA read (skip the cache), use
`signal.get(use_monitor=False)`.

### What positioners provide beyond signals

| Member | What it is |
|--------|------------|
| `.position` | property -- current position (positioner's best understanding) |
| `.moving` | property -- bool "is it moving" indication |
| `.move(p, wait=True)` | start a move; returns a `MoveStatus` |
| `.set(p)` | RE-friendly `move(p, wait=False)`; returns a `Status` |
| `.stop(success=False)` | abort an in-progress move |
| `.subscribe(cb, event_type='readback')` | callback fires on each position update |

### Positioner is an interface, not a hardware kind

Every positioner -- whether it wraps an EPICS motor record, a
temperature controller, a virtual axis, or a pure-Python simulator
-- implements the same small interface.  The methods are `.move()`,
`.set()`, `.stop()`.  The Bluesky-relevant attributes are:

- **`.position`** -- current readback (the positioner's best
  understanding)
- **`.setpoint`** (or, for `EpicsMotor`, `.user_setpoint`) -- the
  most recently commanded target
- **`.readback`** (or `.user_readback` for `EpicsMotor`) -- the
  signal whose value drives `.position`
- **`.moving`** -- bool "is it moving" indication

Motion completion is reported through a `MoveStatus` object
returned by `.set()` / `.move(wait=False)`.

What changes between positioner implementations is *how the
interface is implemented*, not *how a plan uses the positioner*.
Common base classes:

| Class | Underlying hardware | Typical use |
|-------|---------------------|-------------|
| `ophyd.EpicsMotor` | An EPICS motor record (`.VAL`, `.RBV`, `.DMOV`, `.STOP`, ...) | Stepper / servo motors driven by motor records.  The default. |
| `ophyd.PVPositioner` | A setpoint PV + a readback PV + a "done" PV | Things like a temperature controller: real EPICS, but no motor record. |
| `apstools.devices.PVPositionerSoftDoneWithStop` | Same, but "done" is computed in Python via a tolerance | Temperature controllers without a hardware "stable" PV. |
| `ophyd.SoftPositioner` | Local Python logic, optionally with EPICS in the wrapper | Simulators, derived axes, custom controllers without a clean PV mapping.  Our `sim_motor`. |
| `ophyd.PseudoPositioner` | One or more real positioners | Computed axes derived from real ones -- e.g. an `(h, k, l)` reciprocal-space pseudo-positioner derived from real `omega`, `chi`, `phi` axes.  The [`hklpy2`](https://blueskyproject.io/hklpy2/) package provides this for single-crystal diffractometry. |

Because all of these expose the same positioner interface, a plan
written for `EpicsMotor` works without change on a `PVPositioner`.
Concrete and runnable today against the simulator:

```python
RE(bp.scan([sim_det], sim_motor, -1, 1, 11))
```

The same plan against a hypothetical temperature controller (no
code change, just a different object):

```python
# When a temperature controller is added to this instrument, e.g. as
# a PVPositioner subclass named 'temperature' in devices.yml:
RE(bp.scan([scaler], temperature, 300, 400, 11))
```

This is a real temperature scan -- recorded in the catalog the same
way a motor scan would be.  The plan doesn't know or care which
kind of positioner it's driving; it just uses the positioner
interface.

### What Signals provide

| Member | What it is |
|--------|------------|
| `.get()` | scalar value (cached for EPICS-backed signals) |
| `.put(v)` | direct CA put (no RE involved) |
| `.set(v)` | RE-friendly put; returns a `Status` |
| `.read()` | one-entry dict `{name: {value, timestamp}}` |
| `.subscribe(cb)` | callback on value updates |
| `.name`, `.kind`, `.connected` | identity, classification, connection state |

### `.read()` is universal

Every Device *and* every Signal has a `.read()` method:

- `signal.read()` -- one-entry dict.
- `device.read()` -- merged dict of every contained Signal's
  `.read()` whose `kind` is `hinted` or `normal`, recursively
  walking nested Devices.  This is what the RunEngine calls during
  a scan to populate the event document.

### Addendum: the Bluesky plan-stub equivalent

When a plan needs to record a value into the run's event stream:

| Plan stub | Equivalent direct call | What happens |
|-----------|------------------------|--------------|
| `yield from bps.read(signal_or_device)` | `obj.read()` | The value is added to the run's event document under the signal's storage-form name. |
| `yield from bps.mv(motor, p)` | `motor.move(p)` | Move; nothing returned at the plan level. |
| `yield from bps.abs_set(motor, p)` | `motor.set(p)` | Returns the `Status` to the plan via `yield from`. |

The point: at the IPython prompt you call ophyd methods directly
to interact with hardware.  Inside a plan you `yield from` the
corresponding stub so the RunEngine can orchestrate document
publication, pauses, suspenders, and cleanup.

### Concise rule-of-thumb table

| You want... | Use... |
|-------------|--------|
| "Where is this motor?" (interactive) | `motor.position` |
| One specific Signal's value (interactive) | `signal.get()` |
| A force-fresh CA read | `signal.get(use_monitor=False)` |
| A snapshot dict for storage | `device.read()` |
| The same operations from inside a plan | `yield from bps.*` |

## Dotted vs. underscored names: controls vs. storage

You may have noticed something in the `read()` output earlier in
this page: the keys are spelled with **underscores**
(`sample_stage_omega_user_setpoint`), while in Python you address
the same signal with **dots**
(`sample_stage.omega.user_setpoint`).  Both name the same
underlying signal.  They belong to two different use cases:

- **Dotted form** -- for **controls** in Python.
  `sample_stage` is a Python object; `.omega` is an attribute on
  it; `.user_setpoint` is a Signal you can `.get()` / `.put()` /
  `.subscribe()`.  Dots are how Python walks an object tree.
- **Underscored form** -- for **storage**: keys in event documents,
  columns in xarray Datasets, dataset paths in HDF5, search terms
  in the Tiled catalog.  None of those support nested attribute
  access; each recorded signal needs a single flat string that
  uniquely names it across the whole instrument.

The translation is mechanical: replace `.` with `_`, starting from
the device's `name` attribute.

```python
# Controls side -- you type dotted, you get a Signal object
sample_stage.omega.user_readback
# EpicsSignalRO(...)

# Storage side -- the same signal carries a flat string name
sample_stage.omega.user_readback.name
# 'sample_stage_omega_user_readback'

# That string is what shows up as a key in read() and as a column
# in catalog data
ds = cat[uid].primary.read()
ds["sample_stage_omega_user_readback"]
```

### Why the two forms?

The split was forced by what the storage layer could accept.
Historically Bluesky run documents were stored as JSON in MongoDB,
and **MongoDB does not allow `.` in document field names** (it
reserves the dot for sub-document path syntax).  The convention of
flattening Python attribute paths to underscored strings dates from
that constraint.  The underlying tools have evolved -- the Tiled
server we use today does not have the MongoDB restriction -- but
the underscored convention persists, and changing it now would
break every existing analysis script that reads Bluesky data.

### Practical rules

| You are... | Use... | Example |
|------------|--------|---------|
| commanding the hardware | dotted | `sample_stage.omega.move(30)` |
| reading a value in code | dotted | `sample_stage.omega.user_readback.get()` |
| reading data back from a run | underscored | `ds["sample_stage_omega"]` |
| writing a plan that mentions a Signal | dotted | `yield from bps.read(sample_stage.omega.user_readback)` |
| referencing a Signal by name in a callback | underscored | `event["data"]["sample_stage_omega"]` |

If unsure, ask the signal itself:

```python
sample_stage.omega.user_readback.name
# 'sample_stage_omega_user_readback'   <-- the storage name
```

The `.name` is *usually* derived mechanically from the Python path,
but it can be overridden when a Device is constructed
(`Signal(..., name="custom")`).  When in doubt, trust `.name`.

## The `kind` attribute

Every ophyd Signal and Device has a `kind`:

- `hinted` -- included in `read()`, plotted by default.
- `normal` -- included in `read()`, not plotted.
- `config` -- included in `read_configuration()`, recorded once per
  scan as "this is how the instrument was set up."
- `omitted` -- not included anywhere by default; you have to ask
  for it specifically.

You will see all four in this repo.  The interlock-related signals on
`LaserOptics`, for instance, use `kind="config"` for the tunable
positions and `kind="omitted"` for the derived status signals.

## `Status` objects

A Bluesky plan often does not block; it asks a Device to "set this
value" and gets back a `Status` object that completes when the
hardware finishes.  This is what makes Bluesky's pause/resume work:
the RunEngine can `await` a `Status` between message steps.

You will rarely create a `Status` yourself, but you will see them in
the output of direct ophyd calls:

```python
status = sample_stage.xprime.set(5)
status                       # <MoveStatus name=... done=False>
status.wait()                # block until the move finishes (or fails)
status.success               # True if it finished cleanly
```

For interactive use, `device.move(5)` is the convenience wrapper
that calls `set` and waits.

## How does this relate to `RE(...)` and `yield from`?

`RE` (the RunEngine) does not care about the *PVs*.  It cares about
the *messages* a plan yields: "set this signal," "wait for this
status," "read these devices," "trigger this detector."  Each
message references a Device by Python object, not a PV name.

The PV-level conveniences (`get`, `put`, `read`) are still available
when you need them outside a plan.  Most of the time, inside a plan,
you write `yield from bps.mv(sample_stage.xprime, 5)` -- the stub knows
how to translate the device + value into the right sequence of
messages.

See [The RunEngine](../explanation/run_engine.md) for the full
picture, and [Plans and stubs](../explanation/plans_and_stubs.md)
for the message-vocabulary distinction.

## Cheat table

| EPICS / CA                 | ophyd                                      |
|----------------------------|--------------------------------------------|
| `caget("3idxps1:m5.RBV")`  | `sample_stage.omega.user_readback.get()`   |
| `caput("3idxps1:m5.VAL",30)` | `sample_stage.omega.user_setpoint.put(30)` |
| `caput` + wait for DMOV    | `sample_stage.omega.move(30)`              |
| `camonitor 3idxps1:m5.RBV` | `sig.subscribe(cb)` on `user_readback`     |
| `medm -x my.adl`           | `%wa motors`, or write a phoebus screen    |
| `dbpr 3idxps1:m5 3`        | `device.summary()`, or `dir(device)`        |

## See also

- [SPEC user perspective](spec_to_bluesky.md) -- if you came from
  SPEC, that page may be more directly useful.
- [How to add a device](../how_to/add_a_device.md) -- the practical
  steps to bring a new EPICS device into a session.
- [`ophyd` documentation](https://blueskyproject.io/ophyd/) -- the
  authoritative reference for the device library.
