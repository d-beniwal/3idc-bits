# Quick reference

Task-oriented reference for slightly less common things than the
[cheat sheet](cheat_sheet.md) covers.  Skim the headings; jump to
what you need.

## Plans and stubs

A **plan** publishes Bluesky documents -- it brackets a run with
`open_run` / `close_run` and produces a catalog entry.

A **plan stub** does not publish documents -- it is a building block
that does part of a plan's work.

| Module           | What's in it           | Example          |
|------------------|------------------------|------------------|
| `bluesky.plans` (`bp`) | Full plans         | `bp.scan`, `bp.count`, `bp.grid_scan` |
| `bluesky.plan_stubs` (`bps`) | Plan stubs   | `bps.mv`, `bps.sleep`, `bps.read`, `bps.trigger` |

Both can be passed to `RE(...)`.  See
[Plans and stubs](../explanation/plans_and_stubs.md) for the
distinction.

### The `@plan` decorator

```python
from bluesky.utils import plan

@plan
def my_plan(...):
    yield from bps.mv(motor, 5)
    yield from bp.count([det])
```

Apply to every plan and plan stub you author.  Without it, the
common mistake `my_plan(...)` (missing `RE(...)`) silently does
nothing.  With it, you get a `RuntimeWarning` pointing at the
mistake.

## Where to add new code

| Adding...                                  | Goes in...                                     |
|--------------------------------------------|------------------------------------------------|
| A standard EPICS motor                     | `src/id3c/configs/devices.yml` (one line)      |
| A custom motor class (per-axis)            | `devices.yml` `class:` key + existing class    |
| A new Device class (bundle + extras)       | `src/id3c/devices/<name>.py` + `devices.yml`   |
| An interlock between two devices           | `src/id3c/devices/<a>_<b>_interlock.py` + `startup.py` line |
| A plan                                     | `src/id3c/plans/<topic>.py` + `startup.py` import |
| A run-document subscriber callback         | `src/id3c/callbacks/<name>.py` + `startup.py` subscribe |
| A suspender                                | `src/id3c/suspenders/<name>.py` + `startup.py` install |
| A new doc page                             | `docs/source/<section>/<name>.md` + `index.md` toctree entry |
| A new dependency                           | `pyproject.toml`                               |

See the specific how-to guides:
- [Add a device](../how_to/add_a_device.md)
- [Add a plan](../how_to/add_a_plan.md)
- [Edit and build docs](../how_to/edit_and_build_docs.md)

## Editing and rebuilding the docs

```bash
cd docs
make html         # builds to build/html/
make clean        # also clears auto-generated source/api/
```

Requires `pip install -e .[doc]`.

New pages: drop a `.md` in the right `docs/source/<section>/`
subdirectory and add to `<section>/index.md`'s toctree.

Full guide: [Edit and build docs](../how_to/edit_and_build_docs.md).

## IPython magics

```ipython
%wa                # where all -- list devices by label
%wa motors         # filter by label
%mov motor1 5      # interactive move (uses bps.mv)
%movr motor1 0.1   # interactive relative move
%ct                # count once with default detectors
```

These are the apstools magics, registered by `startup.py`.

## Catalog basics

```python
cat                          # session-level Tiled client
len(cat)                     # number of runs
cat[-1]                      # most recent run
cat[uid]                     # by UID
cat.search(Key("plan_name") == "scan")

run = cat[-1]
run.metadata["start"]        # everything from the plan's open_run
run.metadata["stop"]         # success/abort/fail info
run.primary.read()           # main data stream as an xarray Dataset
run.baseline.read()          # baseline snapshot at run start and end
list(run)                    # all streams in the run
```

## Devices: introspection

```python
device.summary()             # pretty-print every Signal + kind
device.read()                # dict of hinted+normal signals
device.read_configuration()  # dict of config signals
device.component_names       # tuple of every Component
device.read_attrs            # what read() will include
device.configuration_attrs   # what read_configuration() will include
device.connected             # True/False
device.wait_for_connection(timeout=5)
```

For signals:

```python
signal.get()                 # one current value
signal.put(value)            # one CA put
signal.subscribe(cb)         # register a monitor callback
signal.unsubscribe(cid)
signal.kind                  # 'hinted' | 'normal' | 'config' | 'omitted'
```

## RunEngine controls

```python
RE.state                     # 'idle' | 'running' | 'paused'
RE.md                        # session-wide metadata dict
RE.md["beamline"] = "3-ID-C" # set persistent metadata

RE.subscribe(cb)             # add a callback
RE.unsubscribe(token)

RE.install_suspender(s)      # install a suspender
RE.remove_suspender(s)

RE.abort()                   # while paused
RE.resume()
RE.stop()
RE.halt()
```

## Interlocks

The omega <-> laser_optics interlock is installed automatically by
`startup.py`.  To inspect:

```python
sample_stage.omega.interlock           # the permit callable
sample_stage.omega.interlock_description
sample_stage.omega.interlock_watch     # tuple of watch signals
```

Setting either to None disables it for the current session.
See [Motion interlocks](../explanation/interlocks.md).

## Time and units

- All Bluesky timestamps are unix epoch seconds (float).
- All ophyd readings carry a `timestamp` field in the dict.
- Motor positions are in the motor's EGU (engineering units) as
  set in the IOC; ophyd does no unit conversion.

## See also

- [Cheat sheet](cheat_sheet.md) -- shorter, denser.
- [Configuration](configuration.md) -- iconfig.yml, devices.yml,
  qserver.
- [Auto-generated API](../api/index.rst) -- module-by-module reference.
