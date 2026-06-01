# How to run a scan

This page covers the common Bluesky scan plans and how to invoke them
in an interactive session.

For the conceptual background, see [The
RunEngine](../explanation/run_engine.md) and [Plans and
stubs](../explanation/plans_and_stubs.md).

For coming-from-SPEC users, the [cross-walk
table](../tutorials/spec_to_bluesky.md#command-cross-walk) has the
short version of everything below.

## The invocation pattern

All scan plans live in `bluesky.plans`, imported as `bp` in the
session.  All take a list of detectors as the first argument:

```python
RE(bp.count([detector], num=1))
RE(bp.scan([detector], motor, start, stop, num_points))
RE(bp.rel_scan([detector], motor, -dx, +dx, num_points))
RE(bp.grid_scan([detector], motor1, s1, e1, n1, motor2, s2, e2, n2))
RE(bp.list_scan([detector], motor, [0, 1.5, 3.0, 5.0]))
RE(bp.spiral([detector], xmotor, ymotor, x_start, y_start, x_range, y_range, dr, nth))
```

Replace `[detector]` with `[scaler, eiger2]` for multiple detectors.

## Count

```python
RE(bp.count([scaler]))                       # one count
RE(bp.count([scaler], num=10))               # ten counts in a row
RE(bp.count([scaler], num=10, delay=0.5))    # ten counts, 0.5 s apart
```

## Absolute and relative scans

```python
# Absolute -- start and stop are the actual positions:
RE(bp.scan([scaler], sample_stage.xprime, 0, 10, 11))   # 0, 1, 2, ..., 10

# Relative -- start and stop are offsets from the current position:
RE(bp.rel_scan([scaler], sample_stage.xprime, -1, 1, 11))
```

**Counting note for SPEC users:** Bluesky's `num` is the **number of
points**, not the number of intervals.  An ascan covering 0 to 10 in
SPEC's `ascan 0 10 10` is `bp.scan(..., 0, 10, 11)` in Bluesky.

## Multi-motor synchronized scan

`bp.scan` takes pairs of `motor, start, stop` -- all motors move
synchronously:

```python
RE(bp.scan(
    [scaler],
    sample_stage.xprime, 0, 10,
    sample_stage.base_y, 0, 5,
    num=11,
))
```

This is SPEC's `a2scan`/`a3scan`.

## Mesh

`bp.grid_scan` takes `motor, start, stop, num` triples; motors are
nested innermost-last:

```python
RE(bp.grid_scan(
    [scaler],
    sample_stage.xprime, 0, 10, 11,    # outer; varies slowest
    sample_stage.base_y, 0, 5, 6,      # inner; varies fastest
))
```

Add `snake_axes=True` to alternate row directions and save the
extra traverse:

```python
RE(bp.grid_scan(
    [scaler],
    sample_stage.xprime, 0, 10, 11,
    sample_stage.base_y, 0, 5, 6,
    snake_axes=True,
))
```

## Attaching metadata

Every scan accepts `md={...}`, a dictionary of arbitrary metadata
that travels with the run:

```python
RE(bp.scan(
    [scaler], sample_stage.xprime, 0, 10, 11,
    md={"sample": "Si(111)", "experimenter": "ECP", "purpose": "alignment"},
))
```

You can later filter the catalog by these keys.

## Live plots and tables

When you call `RE(...)`, two callbacks are already subscribed:

- `bec` (`BestEffortCallback`) -- opens a live plot for any 1-D
  scan against a `hinted`-kind detector, and prints a table.
- The Tiled writer -- sends documents to the Tiled server
  ([sn.xray.aps.anl.gov:8000](http://sn.xray.aps.anl.gov:8000)).

You do not have to do anything special; both fire automatically for
every `RE(...)` invocation.

To peek at recent runs in code, see [How to inspect
data](inspect_data.md).

## Pausing and resuming

Press `Ctrl-C` twice during a scan to pause the RunEngine.  At the
pause prompt you can:

- `RE.resume()` -- continue the plan from where it paused.
- `RE.stop()` -- end the run cleanly (a "stop" document with
  `exit_status='success'` is emitted).
- `RE.abort()` -- end the run with `exit_status='abort'`.
- `RE.halt()` -- emergency stop without any document emission.

This works for any plan, including custom ones; the RunEngine
pauses between messages, so any in-flight motion completes before
the pause takes effect.

## Stopping a misbehaving plan

If a plan is doing something you do not like and Ctrl-C is not
working (e.g. the plan is wedged in an external call), the path is:

1. `Ctrl-C` once -- requests a deferred pause.
2. `Ctrl-C` again -- requests an immediate pause (interrupts the
   current await).
3. If the prompt does not return: in a *separate terminal*, find the
   IPython PID and `kill -INT <pid>`.

The RunEngine catches `KeyboardInterrupt` cleanly; the IOC keeps
running whatever was commanded last.

## Custom plans

If you find yourself typing the same multi-line sequence repeatedly,
turn it into a plan.  See [How to add a plan](add_a_plan.md).

## See also

- [Plans and stubs](../explanation/plans_and_stubs.md) -- the
  difference between `bp.*` (full plans) and `bps.*` (stubs).
- [Inspect data](inspect_data.md) -- after the scan finishes.
- [Cheat sheet](../reference/cheat_sheet.md) -- one-page summary.
