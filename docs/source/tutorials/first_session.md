# Your first Bluesky session at 3-ID-C

This walkthrough assumes:

- You are on a workstation with the `id3c` package and its conda
  environment installed.
- You have a terminal open.
- You have ever used IPython before (if not: it is Python with
  better history, tab completion, and magic commands prefixed by `%`.
  Type `help` at any time.)

We will start a Bluesky session, list the devices that came up,
move a motor, run a short scan against a simulated detector, and
look at the result.

## 1. Start the session

```bash
conda activate <your-environment>
ipython
```

Then at the IPython prompt:

```ipython
In [1]: from id3c.startup import *
```

You will see a few INFO log lines as devices are loaded.  When the
prompt returns, the session is ready.  What just happened:

- `iconfig.yml` was loaded.
- The RunEngine `RE` was created and configured (with the best-effort
  callback `bec` for live plots, and the Tiled writer for run
  documents).
- Devices declared in `configs/devices.yml` were instantiated.
- Interlocks were wired (`setup_omega_laser_interlock`).
- The baseline stream was set up: every device with the `baseline`
  label will be recorded once at the start and once at the end of
  every run.

## 2. See what is available

```ipython
In [2]: %wa
```

`%wa` is short for "where all" -- it prints every device the
RunEngine knows about, organized by label.  You will see motors,
detectors, and the shutter.  `%wa motors` filters to the `motors`
label.

You can also inspect by name:

```ipython
In [3]: sample_stage
Out[3]: SampleStage(prefix='', name='sample_stage', ...)

In [4]: sample_stage.omega
Out[4]: InterlockedEpicsMotor(prefix='3idxps1:m5', name='sample_stage_omega', ...)
```

## 3. Move a motor

This is the place every new Bluesky user trips.  There are **two**
correct ways and **one** very tempting wrong way.

```ipython
In [5]: sample_stage.x.move(12.3)         # direct ophyd; works
In [6]: RE(bps.mv(sample_stage.x, 12.3))  # Bluesky plan; also works
```

Both move the motor.  The difference: the second call goes through
the RunEngine, which produces no documents for a plain `bps.mv`
(it is a plan stub) but does respect interlocks, suspenders, and
pause/resume.

The wrong way:

```ipython
In [7]: bps.mv(sample_stage.x, 12.3)
Out[7]: <generator object mv at 0x7f...>
```

The motor does **not** move.  `bps.mv` returns a generator; without
`RE(...)`, you are throwing that generator away.  This is the single
most common confusion for new Bluesky users.  See
[The RunEngine](../explanation/run_engine.md) for the full
explanation.

## 4. Run a scan against the simulated detector

The session ships with two simulators (`sim_motor`, `sim_det`) and
three demo plans you can use to verify the install:

```ipython
In [8]: RE(sim_print_plan())
sim_print_plan(): This is a test.
sim_print_plan():  sim_motor.position=0.0  sim_det.read()={'noisy_det': ...}.

In [9]: RE(sim_count_plan())
<run UID printed, BEC table>

In [10]: RE(sim_rel_scan_plan())
<plot opens, scan runs, table prints>
```

If you forget the `RE(...)`:

```ipython
In [11]: sim_count_plan()
<no output, no scan, no warning>
```

Bluesky used to do nothing at all in that case.  Because our plans
are decorated with `@plan`, you will now see a warning shortly after
the prompt returns:

```
RuntimeWarning: plan `sim_count_plan` was never iterated,
                did you mean to use `yield from`?
```

That warning is your hint to retype with `RE(...)`.

## 5. Look at the result

Bluesky runs are stored in the Tiled catalog `cat` (one of the things
loaded by `startup`).  The most recent run is `cat[-1]`:

```ipython
In [12]: cat[-1]
Out[12]: <Container ...>
```

The `BestEffortCallback` (`bec`) opened a plot during the
`sim_rel_scan_plan` call and printed a table to the terminal.
That happens automatically; you do not have to do anything special.

To re-inspect the data programmatically:

```ipython
In [13]: run = cat[-1]
In [14]: run.metadata["start"]["scan_id"]
Out[14]: 1
In [15]: run.primary.read()              # an xarray Dataset of the event data
```

For details on what `cat`, `BEC`, and `run` are, see
[How to inspect data](../how_to/inspect_data.md).

## 6. Move on

You now have the muscle memory for:

- Start a session.
- List devices.
- Move a motor (the right way).
- Run a plan.
- Look at the result.

Where to go next:

- [SPEC user perspective](spec_to_bluesky.md) -- the SPEC → Bluesky
  cross-walk for users coming from spec-style command-line scanning.
- [EPICS user perspective](epics_to_ophyd.md) -- how Bluesky's
  device model (ophyd) relates to bare EPICS PVs.
- [How to run a scan](../how_to/run_a_scan.md) -- the full menu of
  Bluesky's built-in scan plans.
- [Cheat sheet](../reference/cheat_sheet.md) -- one-page dense
  reference for daily use.
