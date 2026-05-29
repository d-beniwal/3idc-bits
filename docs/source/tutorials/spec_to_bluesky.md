# Coming to Bluesky from SPEC

This page is for users with SPEC experience.  We assume you know what
`mv`, `umvr`, `ascan`, `dscan`, and `ct` do; what `wm` shows; and how
SPEC stores data in numbered scan files.

Bluesky is a different beast.  This page is the cross-walk between
SPEC concepts and their Bluesky equivalents -- close enough to bridge
the gap, honest about where Bluesky is *more* trouble than SPEC.

## The single most important difference

In SPEC, you type a command and the hardware moves:

```
SPEC> mv samx 5
```

In Bluesky, you type the *description* of a motion and hand it to the
RunEngine to execute:

```python
RE(bps.mv(sample_stage.x, 5))
```

The `bps.mv(...)` part returns a Python *generator* -- a description
of what should happen.  The `RE(...)` part *does* it.  If you forget
the `RE(...)` and type just `bps.mv(...)`, the motor does not move.

This is the source of more new-user confusion than anything else.
See [The RunEngine](../explanation/run_engine.md) for why Bluesky is
built this way.

## Command cross-walk

| SPEC                                | Bluesky                                                | Notes |
|-------------------------------------|--------------------------------------------------------|-------|
| `mv samx 5`                         | `RE(bps.mv(sample_stage.x, 5))`                        | Move one motor to absolute position. |
| `mv samx 5 samy 3`                  | `RE(bps.mv(sample_stage.x, 5, sample_stage.y, 3))`     | Multi-motor move (parallel). |
| `umv samx 5`                        | `RE(bps.mv(sample_stage.x, 5))`                        | Bluesky always waits; no "update vs. non-update" distinction. |
| `mvr samx 0.1`                      | `RE(bps.mvr(sample_stage.x, 0.1))`                     | Relative move. |
| `wm samx`                           | `sample_stage.x.position`                              | Returns a float; no `RE(...)` -- this is *not* a plan. |
| `wa`                                | `%wa motors` (IPython magic)                           | "where all" for motors with the `motors` label. |
| `ct 1`                              | `RE(bp.count([scaler], num=1))`                        | One count of named detector(s). |
| `ct 1 5`                            | `RE(bp.count([scaler], num=5, delay=...))`             | Five counts; `delay=` is between them. |
| `ascan samx 0 10 10 1`              | `RE(bp.scan([scaler], sample_stage.x, 0, 10, 11))`     | SPEC counts intervals; Bluesky counts **points**.  10 intervals = 11 points. |
| `dscan samx -1 1 10 1`              | `RE(bp.rel_scan([scaler], sample_stage.x, -1, 1, 11))` | Relative scan around current position. |
| `a2scan samx 0 10 samy 0 5 10 1`    | `RE(bp.scan([scaler], sample_stage.x, 0, 10, sample_stage.y, 0, 5, 11))` | Multi-motor synchronized scan. |
| `mesh samx 0 10 5 samy 0 5 5 1`     | `RE(bp.grid_scan([scaler], sample_stage.x, 0, 10, 6, sample_stage.y, 0, 5, 6))` | 2-D mesh; again, +1 on each "intervals" count. |
| `chg_offset samx 0`                 | `sample_stage.x.set_current_position(0)`               | Set the current readback to a new value (changes the offset). |
| `set_lm samx -10 10`                | (use motor record `.LLM` / `.HLM` via `caput`, or write a plan) | Bluesky has no built-in limit-setter shortcut; ophyd exposes them as `sample_stage.x.low_limit_travel` and `.high_limit_travel`. |
| `shopen` / `shclose`                | `RE(bps.mv(shutter, "open"))` / `RE(bps.mv(shutter, "close"))` | (or `shutter.open()` / `shutter.close()` direct, no `RE` -- see "When NOT to use `RE`" below) |
| `# data file management`            | `cat = init_catalog(iconfig)` (Tiled-backed catalog)   | Bluesky writes structured runs to a server; no per-scan file. |
| `newfile mydata`                    | (not applicable; runs are tagged with metadata)        | See `md={...}` in scans. |
| `pdshow`, `pd write`                | (`bec` does this live; see [inspect data](../how_to/inspect_data.md) for after) | The `bec` callback prints tables and opens plots automatically. |

Bluesky's built-in scan plans live in `bluesky.plans` and are
imported as `bp` in the session.  Plan stubs (one-shot operations
that compose into plans) live in `bluesky.plan_stubs` and are imported
as `bps`.  See [Plans and stubs](../explanation/plans_and_stubs.md)
for the distinction.

## When *not* to use `RE(...)`

SPEC has no distinction: every command at the prompt is a command.
Bluesky has two kinds of expressions:

```python
# Plans -- use RE(...)
RE(bps.mv(motor, 5))
RE(bp.scan([detector], motor, 0, 10, 11))
RE(laser_optics.move_out())

# NOT plans -- call directly, no RE(...)
sample_stage.x.position             # returns a float
sample_stage.x.user_readback.get()  # returns a float
laser_optics.is_out                 # returns a bool
sample_stage.x.read()               # returns a dict
shutter.open()                      # returns immediately; the bare device method
cat[-1].primary.read()              # returns an xarray Dataset
```

The rule: if it returns a generator, wrap with `RE`; if it returns
data, call directly.  See [The
RunEngine](../explanation/run_engine.md) for the full reasoning.

## What Bluesky has that SPEC does not

These are the upsides:

- **Structured metadata.**  Every run has a unique UID, a `scan_id`,
  a start time, an end time, and a customizable metadata dict (`md=`).
  No more "what was scan 47 of yesterday?".
- **Pause / resume.**  Two Ctrl-C's pause the RunEngine between
  steps.  Inspect, fix, then `RE.resume()`.
- **Suspenders.**  "Pause on beam dump, resume when it comes back"
  is a generic Bluesky mechanism, not bespoke per-beamline code.
- **Document streams.**  Every run emits a stream of structured
  documents (`start`, `descriptor`, `event`, `stop`).  Subscribers
  consume the stream live: BEC for plots, TiledWriter for storage,
  SPEC-format writer for SPEC-style files (if enabled).
- **Live plotting.**  The `BestEffortCallback` (`bec`) opens a plot
  for any 1-D scan automatically.

## What SPEC has that Bluesky does not

These are real downsides; pretending otherwise wastes everyone's time:

- **Compactness.**  `ascan samx 0 10 10 1` is much shorter than
  `RE(bp.scan([scaler], sample_stage.x, 0, 10, 11))`.  You can
  alias common scans to save typing, but the bare commands are
  longer.
- **Inline command editing.**  SPEC lets you edit and re-run scans
  by recalling the command line.  Bluesky has IPython history, which
  is close but not the same.
- **Macros.**  SPEC's `do.mac` is sometimes faster to write than
  authoring a Python plan, especially for one-off ideas.  Bluesky
  plans are more reusable but more verbose to write.
- **One command, one file.**  SPEC files are human-readable text with
  per-scan headers.  Bluesky data lives in a Tiled catalog; reading
  it requires a client (Python, or a Tiled web browser).
- **Decades of stability.**  SPEC's command set has not changed in
  ages.  Bluesky is younger and the ecosystem is still moving.

## Data files

In SPEC, you set a data file with `newfile`, every scan appends to
it, and you read with `pdshow` / external tools.

In Bluesky at 3-ID-C:

- Runs are sent to a [Tiled server](http://sn.xray.aps.anl.gov:8000)
  by the `TiledWriter` callback.  Runs land under the `/raw` tree on
  that server.
- Area-detector image files (Eiger HDF5) are written by the IOC into
  a beamline-specific directory and *linked* into the Bluesky master
  HDF5 file via HDF5 external links.
- The catalog client (`cat` in your session) is the way to look at
  runs after the fact: `run = cat[-1]; run.primary.read()`.

See [How to inspect data](../how_to/inspect_data.md) and
[How to visualize HDF5](../how_to/visualize_hdf5.md) for the practical
workflows.

## See also

- [EPICS user perspective](epics_to_ophyd.md) -- if you also know
  EPICS but not Python, that page may help more than this one.
- [The RunEngine](../explanation/run_engine.md) -- why Bluesky
  insists on the `RE(...)` wrapping.
- [Cheat sheet](../reference/cheat_sheet.md) -- print this and tape
  it to your monitor.
