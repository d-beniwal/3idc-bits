# Motion interlocks

This page explains the motion-interlock mechanism in this repository:
what it protects, what it does *not* protect, and why it is built the
way it is.

The only concrete interlock today is between `sample_stage.omega` and
`laser_optics`, but the design generalizes to any "block motor A
unless device B is in some state" relationship.

## What an interlock here actually is

An **interlock** in `id3c` is a Python callable that returns `True`
(motion permitted) or `False` (motion blocked), evaluated at two
moments:

1. **Pre-flight**, just before any EPICS write -- if the callable
   returns `False`, the move raises `MotionInterlock` and no PV is
   touched.
2. **Mid-flight**, on every update of certain "watch" signals -- if
   the callable returns `False` while a move is in progress, the
   motor is stopped and the move's `MoveStatus` is failed with
   `MotionInterlock`.

The implementation lives in
[`id3c.devices.interlocked_motor.InterlockedEpicsMotor`](../../../src/id3c/devices/interlocked_motor.py),
an `ophyd.EpicsMotor` subclass.  The protected motor instance is
configured at startup time with three attributes:

```python
motor.interlock = lambda: laser_optics.is_out   # the permit callable
motor.interlock_description = "laser_optics OUT" # for error messages
motor.interlock_watch = (laser_optics.us.user_readback,
                         laser_optics.ds.user_readback)
```

These are assigned **after** device construction, by a setup function
in `id3c.devices.<pair>_interlock` that `startup.py` calls after
`make_devices()`.  This late-binding keeps the interlock class
generic (it does not need to know about `laser_optics`) and avoids
import cycles between mutually-interlocked devices.

## What is protected today

[`id3c.devices.omega_laser_interlock.setup_omega_laser_interlock`](../../../src/id3c/devices/omega_laser_interlock.py)
installs a **bidirectional** interlock:

- **`sample_stage.omega` blocked unless `laser_optics.is_out`.**
  Reason: when the laser optics are not retracted, they could
  collide with the rotating sample stage.  The protected motor is
  `omega`; the gating condition is "both `us` and `ds` axes are
  within tolerance of `out_position` (-75 +/- 1 mm)".
- **`laser_optics.us` and `laser_optics.ds` blocked while
  `sample_stage.omega` is moving.**  Reason: pulling the laser optics
  in or out while the sample is rotating is also a collision risk.
  The gating condition is "`omega.motor_is_moving` is False".

The second direction uses a deliberately broad rule: any omega
motion blocks any laser-optics motion.  An angle-aware rule (block
only inside a danger zone) is **intentionally not implemented in
Python**; see [What is *not* protected](#what-is-not-protected) below.

## What is *not* protected

The interlock lives entirely in the running Bluesky/Python session.
It does **not**:

- Write to any EPICS protection field (no `DISP`, no `SPMG=Stop`, no
  sequencer record).
- Survive a Python process crash, exit, or `kill -9`.
- Apply to motion commanded from MEDM screens.
- Apply to `caput` from a shell.
- Apply to a different Bluesky session running against the same IOCs.
- Apply to SPEC or any other client.

If any of these scenarios matter for safety, the protection must be
implemented in the IOC -- typically as a CALC/SCALC record, a state
notation sequencer, or a soft record that drives the motor's `DISP`
field.  The Python interlock is a *convenience* layer for the
Bluesky workflow; it is not a substitute for IOC-level protection.

The module docstring of
[`interlocked_motor.py`](../../../src/id3c/devices/interlocked_motor.py)
restates this honestly so anyone reading the code learns the limit at
the same time they learn the API.

## Why not use a Suspender?

`bluesky.suspenders.SuspendBoolLow` and friends are the standard
Bluesky mechanism for "pause the RunEngine when condition X goes
False."  They are a poor fit here for two reasons:

1. **Suspenders pause everything**, not just one motor.  An entire
   plan is paused, with no obvious link to the device that caused
   the suspension.  This is correct behaviour for global conditions
   (beam dump, shutter close) and surprising behaviour for local
   conditions (one motor's interlock).
2. **Suspenders are installed by the user**, typically via
   `RE.install_suspender(...)`.  New users routinely forget to do
   this, leaving the protection inert.  An `InterlockedEpicsMotor`
   is protective the moment it is instantiated; there is nothing to
   opt in to.

## Why `MotionInterlock` is its own exception

`MotionInterlock(RuntimeError)` is defined in `interlocked_motor.py`
alongside the class that raises it.  The exception message is the
self-diagnostic the user sees at the bottom of an otherwise long
Bluesky traceback:

```
MotionInterlock: sample_stage_omega.move blocked by interlock
                 'laser_optics OUT'. See the device(s) referenced
                 by this interlock for state.
```

Bluesky tracebacks are routinely 100+ lines (asyncio frames, plan
runner frames, status callback frames, RunEngine frames).  The
exception message is the only line most users will read; making it
self-contained is what makes the error actionable.

## Pre-flight versus mid-flight

The two evaluations use different mechanisms and have different
failure modes:

| Phase       | Mechanism                                | If interlock=False  |
|-------------|------------------------------------------|---------------------|
| Pre-flight  | Direct call to `interlock()` from `move`| `raise MotionInterlock(...)` before any caput |
| Mid-flight  | Watcher on `interlock_watch` signals    | `motor.stop()`; `status.set_exception(MotionInterlock(...))` |

The mid-flight path is the subtle one.  Raising an exception inside a
pyepics CA callback does **not** propagate to the RunEngine -- the
exception is caught and logged by the CA dispatcher and the plan
continues happily.  To actually halt the plan, the watcher must
*fail the `MoveStatus`* that the RunEngine is waiting on.

`InterlockedEpicsMotor` does this correctly: the watcher does
`status.set_exception(MotionInterlock(...))`, which causes the
`status.wait()` call inside the RunEngine to raise.  The plan halts;
the RunEngine reports the exception; the user sees the diagnostic
message.

## See also

- [How to add a device](../how_to/add_a_device.md)
- [`id3c.devices.interlocked_motor` API reference](../api/id3c/devices/interlocked_motor/index.rst)
- [`id3c.devices.omega_laser_interlock` API reference](../api/id3c/devices/omega_laser_interlock/index.rst)
- [`AGENTS.md > Interlock pattern`](https://github.com/BCDA-APS/3idc-bits/blob/main/AGENTS.md#interlock-pattern)
