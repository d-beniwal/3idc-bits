# Configuration files

This page documents the YAML configuration files that drive the
`id3c` instrument session.  All live under `src/id3c/configs/`.

| File             | Purpose                                                          |
|------------------|------------------------------------------------------------------|
| `iconfig.yml`    | Top-level session config: catalog, RE metadata, callbacks toggle |
| `devices.yml`    | Device declarations (loaded by `make_devices()`)                 |
| `extra_logging.yml` | Optional log handler config                                   |
| `qserver/qs-config.yml` | Bluesky queueserver host config                          |

The session reads `iconfig.yml` first; everything else is referenced
indirectly.  Hand-edit any of these and restart the IPython session
for changes to take effect.

## `iconfig.yml`

Controls high-level session behaviour:

- Which Tiled catalog the `cat` object connects to.
- Which RE document subscribers are enabled (NeXus writer, SPEC
  writer, etc.).
- The metadata dictionary fields persisted between sessions.

Refer to the upstream `apsbits` docs for the full schema; this
beamline's overrides are intentionally minimal.

## `devices.yml`

The declarative device list.  Each top-level key is a dotted path to
a factory function or class; the value is a list of dicts, each
producing one device.

Supported factories at 3-ID-C:

- `apstools.devices.motor_factory.mb_creator` -- motor bundles.
- `ophyd.EpicsMotor` -- single EPICS motors.
- `ophyd.scaler.ScalerCH` -- channel-access scalers.
- `apstools.devices.ApsPssShutter` -- safety shutter.
- `apsbits.utils.sim_creator.predefined_device` -- simulators
  (`ophyd.sim.motor`, etc.).
- `id3c.devices.laser_optics.LaserOptics` -- our custom bundle.
- Any other dotted path to a class that takes `prefix`, `name`,
  `labels` kwargs.

### `mb_creator` per-axis options

The `motors:` value can be:

- A list of PV strings: `["m1", "m2", "m3"]`
- A dict of name -> PV: `{x: "m1", y: "m2"}`
- A dict of name -> per-axis dict: see below

Per-axis dict keys:

| Key                       | Effect                                        |
|---------------------------|-----------------------------------------------|
| `prefix`                  | PV (or appended to bundle prefix by EpicsMotor) |
| `class`                   | Dotted path for the positioner class          |
| `factory`                 | `{function: dotted.path, ...kwargs}` for a class factory |
| `labels`                  | Override of default `["motors"]`              |
| `kind`                    | Override of default kind                      |
| *anything else*           | Forwarded as kwarg to the axis class `__init__` |

If the axis class accepts custom kwargs (like
`InterlockedEpicsMotor.interlock_description`), they go directly in
the per-axis dict.

### Example: interlocked motor in a bundle

```yaml
apstools.devices.motor_factory.mb_creator:
- name: sample_stage
  labels: ["baseline"]
  prefix: ""
  class_name: SampleStage
  motors:
    x: "3idxps1:m4"
    y: "3idc:m42"
    z: "3idxps1:m3"
    omega:
      prefix: "3idxps1:m5"
      class: id3c.devices.interlocked_motor.InterlockedEpicsMotor
      interlock_description: "laser_optics OUT"
```

The `interlock_description` is consumed by `InterlockedEpicsMotor.__init__`,
which pops it before calling `super().__init__()`.

### Example: a hand-rolled class

```yaml
id3c.devices.laser_optics.LaserOptics:
- name: laser_optics
  labels: ["baseline"]
  prefix: "3idxps1:"
```

This calls `LaserOptics(prefix="3idxps1:", name="laser_optics",
labels=["baseline"])`.

### Labels

`labels:` is a list of strings the device is tagged with.  Two are
special in this repo:

- `motors` -- picked up by `%wa motors`.
- `baseline` -- automatically added to the baseline stream by
  `setup_baseline_stream()` in `startup.py`.  Recorded once at the
  start and once at the end of every run.

Anything else is freeform; use it for grouping in `%wa`.

## See also

- [How to add a device](../how_to/add_a_device.md) -- practical
  recipes by case.
- [`devices.yml` source](../../../src/id3c/configs/devices.yml).
- [`apstools.devices.motor_factory.mb_creator`
  reference](https://bcda-aps.github.io/apstools/main/api/devices/motor_factory.html).
