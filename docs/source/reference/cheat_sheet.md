# Cheat sheet

One-page reference for everyday Bluesky use at 3-ID-C.  Print or keep
in a tab.

## Start a session

```bash
conda activate 3idc-bits
ipython
```

```python
from id3c.startup import *
```

## See devices

```python
%wa                  # all devices, grouped by label
%wa motors           # only the 'motors' label
%wa baseline         # only the 'baseline' label

sample_stage         # one device by name (tab-completes)
sample_stage.xprime       # one axis of a bundle
oregistry.device_names  # set of all registered device names
```

## Move motors

```python
RE(bps.mv(sample_stage.xprime, 12.3))                              # absolute
RE(bps.mv(sample_stage.xprime, 12.3, sample_stage.base_y, 5.0))         # parallel
RE(bps.mvr(sample_stage.xprime, 0.1))                              # relative

sample_stage.xprime.move(12.3)         # direct ophyd; no metadata, no docs
sample_stage.xprime.position           # last setpoint (float)
sample_stage.xprime.user_readback.get()  # .RBV value
```

## Count and scan

```python
RE(bp.count([scaler]))                                   # one count
RE(bp.count([scaler], num=10))                           # ten counts
RE(bp.count([scaler], num=10, delay=0.5))                # ten counts, 0.5 s apart
RE(bp.scan([scaler], sample_stage.xprime, 0, 10, 11))         # 0..10 in 11 points
RE(bp.rel_scan([scaler], sample_stage.xprime, -1, 1, 11))     # relative
RE(bp.grid_scan([scaler], sample_stage.xprime, 0, 10, 11,
                          sample_stage.base_y, 0, 5, 6))      # 2-D mesh
RE(bp.list_scan([scaler], sample_stage.xprime, [0, 1.5, 3.0])) # listed points
```

Add metadata: `md={"sample": "Si(111)", "purpose": "alignment"}`.

## Shutter

```python
RE(bps.mv(shutter, "open"))            # via plan; recorded
RE(bps.mv(shutter, "close"))

shutter.open()                         # direct; not recorded
shutter.close()
```

## Laser optics (interlocked with omega)

```python
RE(laser_optics.move_out())            # plan method on the device
RE(laser_optics.move_in())

laser_optics.is_out                    # bool (not a plan; call directly)
```

Note: `sample_stage.omega` will **refuse to move** unless the laser
optics are OUT.  See [Motion interlocks](../explanation/interlocks.md).

## Look at the most recent run

```python
run = cat[-1]
run.metadata["start"]["scan_id"]
run.metadata["start"]["plan_name"]
ds = run.primary.read()                # xarray Dataset
ds.to_pandas()                         # pandas DataFrame
ds["scaler_chan01"].plot()             # matplotlib plot

run.baseline.read()                    # devices labelled 'baseline'
```

Filter: `cat.search(Key("plan_name") == "scan")`
(`from tiled.queries import Key`).

```{note}
**Two name forms** -- dotted (`sample_stage.xprime.user_readback`) for
controls in Python, underscored (`sample_stage_xprime_user_readback`)
for keys in stored data.  Same underlying signal; see
[EPICS -> ophyd > Dotted vs. underscored
names](../tutorials/epics_to_ophyd.md#dotted-vs-underscored-names-controls-vs-storage).

**Two ways to ask "where is this motor?"** -- `motor.position`
(positioner property) vs. `motor.user_readback.get()` (Signal
method); they normally agree.  See
[EPICS -> ophyd > `motor.position` versus
`signal.get()`](../tutorials/epics_to_ophyd.md#motorposition-versus-signalget).
```

## Pause / abort

While a plan runs:

- `Ctrl-C` -- deferred pause (waits for current message to finish)
- `Ctrl-C Ctrl-C` -- immediate pause (interrupts await)

At the pause prompt:

```python
RE.resume()         # continue
RE.stop()           # finish cleanly (exit_status='success')
RE.abort()          # finish (exit_status='abort')
RE.halt()           # emergency stop, no documents emitted
```

## Simulators (verify install)

```python
RE(sim_print_plan())
RE(sim_count_plan())
RE(sim_rel_scan_plan())
```

## When to use `RE(...)` and when not

| Use `RE(...)` -- it is a plan         | Do **not** use `RE(...)` -- it is data |
|---------------------------------------|----------------------------------------|
| `bps.mv(motor, 5)`                    | `motor.position`                       |
| `bp.scan([det], motor, 0, 10, 11)`    | `motor.user_readback.get()`            |
| `laser_optics.move_out()`             | `laser_optics.is_out`                  |
| any of *our* plans                    | `motor.read()`                         |
|                                       | `device.summary()`                     |
|                                       | `cat[-1].primary.read()`               |
|                                       | `shutter.open()` (no `RE` needed)      |

Rule: returns a generator -> `RE(...)`; returns data -> call directly.
See [The RunEngine](../explanation/run_engine.md) if confused.

## See also

- [Quick reference](quick_reference.md) -- longer task-oriented reference.
- [Tutorial: first session](../tutorials/first_session.md) -- start here if new.
- [SPEC users](../tutorials/spec_to_bluesky.md) -- command cross-walk.
- [EPICS users](../tutorials/epics_to_ophyd.md) -- device model intro.
