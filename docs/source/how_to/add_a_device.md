# How to add a new device

This page covers the practical steps for adding a new ophyd device to
the 3-ID-C BITS instrument.

Three common cases, in increasing order of complexity:

1. **Standard motor / signal**: one entry in `devices.yml`, no Python.
2. **Custom motor class** (e.g. `InterlockedEpicsMotor`): per-axis
   `class:` in `devices.yml`, no Python.
3. **Custom Device class** (`MotorBundle` subclass with extra
   Components or methods): new Python module + YAML entry.

## Where things live

- `src/id3c/configs/devices.yml` -- declarative device list.
- `src/id3c/devices/` -- custom Python device classes.
- `src/id3c/startup.py` -- session bootstrap; runs after
  `make_devices()` has populated the registry.

The repository's
[AGENTS.md](https://github.com/BCDA-APS/3idc-bits/blob/main/AGENTS.md)
captures the relevant conventions.

## Case 1: standard EPICS motor

Edit `devices.yml`, add one entry under the appropriate creator
section.  For a single motor:

```yaml
ophyd.EpicsMotor:
- {name: my_motor, prefix: "3idxps1:m7", labels: ["motor", "baseline"]}
```

For a bundle of motors:

```yaml
apstools.devices.motor_factory.mb_creator:
- name: my_stage
  labels: ["baseline"]
  prefix: ""                  # bundle prefix; "" means component prefixes are full
  class_name: MyStage         # name for the synthesized class (cosmetic)
  motors:
    x: "3idc:m20"
    y: "3idc:m21"
    z: "3idc:m22"
```

Restart the IPython session.  The device will be available as
`my_motor` or `my_stage` at the prompt.

## Case 2: custom motor class via `mb_creator`

`mb_creator` accepts a per-axis dict where one of the keys is `class`
(a dotted path).  Use this when one of the axes needs a specific
ophyd subclass (most commonly, our `InterlockedEpicsMotor`):

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

Any custom kwarg the axis class wants (here, `interlock_description`)
**must be popped** from `**kwargs` in the class `__init__` before
calling `super().__init__(**kwargs)`, because `EpicsMotor` does not
tolerate unknown kwargs.  See
[`InterlockedEpicsMotor.__init__`](../../../src/id3c/devices/interlocked_motor.py).

## Case 3: hand-rolled `MotorBundle` subclass

You need a real class when the device has any of:

- Non-motor Components (Signals for configuration, `AttributeSignal`
  for derived state).
- Properties or methods that operate on the bundle as a whole.
- Plan methods (used as `RE(my_device.do_something())`).

Reference example: `id3c.devices.laser_optics.LaserOptics`.  It has
all three.

Skeleton for a new bundle:

```python
# src/id3c/devices/my_stage.py
"""Description of the stage."""

from __future__ import annotations

import logging

from bluesky import plan_stubs as bps
from bluesky.utils import plan
from ophyd import Component as Cpt
from ophyd import EpicsMotor
from ophyd import MotorBundle
from ophyd import Signal

logger = logging.getLogger(__name__)


class MyStage(MotorBundle):
    """One-line summary of the device."""

    x = Cpt(EpicsMotor, "m1")
    y = Cpt(EpicsMotor, "m2")

    park_position = Cpt(Signal, value=0.0, kind="config")

    @plan
    def park(self):
        """Move both axes to the parked position."""
        yield from bps.mv(self.x, self.park_position.get(),
                          self.y, self.park_position.get())
```

Then declare it in `devices.yml`:

```yaml
id3c.devices.my_stage.MyStage:
- name: my_stage
  prefix: "3idxps1:"
  labels: ["baseline"]
```

The dotted YAML key (`id3c.devices.my_stage.MyStage`) names the
class to instantiate.  The list under it gives the constructor
kwargs; each entry produces one device.

## Late-binding wiring (interlocks)

If the new device needs to coordinate with another device after both
have been instantiated (e.g. installing an interlock callable), put
the wiring into a small function in
`src/id3c/devices/<a>_<b>_interlock.py` and call it from
`startup.py` after `make_devices()`:

```python
# src/id3c/devices/laser_omega_interlock.py
def setup_omega_laser_interlock(oregistry):
    laser = oregistry["laser_optics"]
    omega = oregistry["sample_stage"].omega
    omega.interlock = lambda: laser.is_out
    omega.interlock_description = "laser_optics OUT"
    omega.interlock_watch = (
        laser.us.user_readback, laser.ds.user_readback,
    )
```

```python
# src/id3c/startup.py  (excerpt)
from .devices.omega_laser_interlock import setup_omega_laser_interlock

make_devices(...)
setup_omega_laser_interlock(oregistry)
```

See [Motion interlocks](../explanation/interlocks.md) for the
design rationale and [AGENTS.md > Interlock
pattern](https://github.com/BCDA-APS/3idc-bits/blob/main/AGENTS.md#interlock-pattern)
for the naming convention.

## Verifying without EPICS

This repo is developed on a host that **cannot reach the beamline
EPICS PVs**.  Standard verification at instantiation:

```python
from id3c.devices.my_stage import MyStage
ms = MyStage("3idxps1:", name="my_stage")
ms                                # repr should look right
ms.component_names                # tuple of Component names
ms.read_attrs                     # what `read()` would return
ms.configuration_attrs            # what `read_configuration()` would return
```

`ms.wait_for_connection(timeout=2)` will time out on the dev host;
that is expected.  See [AGENTS.md > Off-network
reality](https://github.com/BCDA-APS/3idc-bits/blob/main/AGENTS.md#off-network-reality).

## See also

- [How to add a plan](add_a_plan.md) -- once the device exists, how
  to write plans that use it.
- [Motion interlocks](../explanation/interlocks.md) -- design
  rationale for the interlock pattern.
- [`devices.yml` reference](../reference/configuration.md).
