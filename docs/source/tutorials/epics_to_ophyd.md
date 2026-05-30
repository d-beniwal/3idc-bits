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
status = sample_stage.x.set(5)
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
you write `yield from bps.mv(sample_stage.x, 5)` -- the stub knows
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
