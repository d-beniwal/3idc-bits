# Porting docs: using the autofocus plans in the optics-alignment workflow

This folder holds two Bluesky plans for the autofocus program (KB-mirror optics alignment).
They replace the raw-EPICS measurement steps of its existing code (`AI-Beamline-3ID`, built
on `Common-Libraries`; both are read-only references here and were not modified):

| Plan | Module | Replaces in the existing autofocus code |
|---|---|---|
| `diode_fly` / `run_diode_flyscan` | `flyscan_diode.py` | the bare-PV scaler scan loop of `Beamline3IDBeamManagerScan` (motor H/V + scaler counts) |
| `microscope_acquire` / `run_microscope_acquire` | `microscope_acquire.py` | the single-shot image acquisition of `GenericCamera.collect_single_shot_image` (used by `Beamline3IDBeamManagerCamera`) |

**Status:** both are tested against simulated devices only (30 tests, kept in
`BL_instrumentation/opt/tests`, outside this repo). Nothing has run on the real scaler, motors,
microscope or EPICS. See "Checklist for the first beamline run" at the end.

## 1. Setup

The modules are not imported by `id3c.startup`, so import them after the startup has run
(the autofocus environment already imports it, which connects the devices on the beamline network;
`id3c` must be installed there, not just `apsbits`):

```python
from id3c.startup import RE
from id3c.user.autofocus.flyscan_diode import FlyAxis, diode_fly, run_diode_flyscan
from id3c.user.autofocus.microscope_acquire import (
    microscope_acquire,
    run_microscope_acquire,
)
```

- Devices are looked up **by name in the instrument registry** (like the other `id3c.user`
  plans) or passed as objects. A name used before the startup has run raises a clear error.
- Defaults: scaler `"scaler_b"` (`3ida:scaler1`), microscope `"pg3_camera"` (`3IDPG3:`, from
  `devices_camera.yml`; the registry name keeps "camera").
- Neither plan opens a Bluesky run: no documents, nothing to Tiled/databroker, no `scan_id`.
  Hardware is still driven through the RunEngine.

### Getting results out of a plan

`RE(plan)` returns only run uids, and with no run that means nothing useful. The plans
*return* their results, so:

- from blocking code (the autofocus scripts), use the helpers `run_diode_flyscan(RE, ...)` and
  `run_microscope_acquire(RE, ...)`; they run the plan and hand back its return value;
- inside your own plan, write `result = yield from diode_fly(...)`;
- `RE(diode_fly(...))` / `RE(microscope_acquire(...))` run fine but discard the results.
  `RE(...)` cannot be made to return them: `call_returns_result` is fixed when the RunEngine
  is created.

### Pause and abort

Ctrl-C pauses the RunEngine with hardware possibly still moving or acquiring. Call
`RE.abort()`, which stops the motors/microscope and restores every setting the plan changed.
Do not `RE.resume()`: a half-flown scan or half-taken image is not resumable.

## 2. Where the plans fit in the alignment workflow

The autofocus loop is: measure the beam, estimate the shift needed, move the KB mirror axes, measure
again, normalizing against the ion-chamber flux. The plans cover the two *measure* steps.

| Alignment step | Existing code today | With these plans |
|---|---|---|
| Image of the beam | `GenericCamera.collect_single_shot_image` (raw `epics.PV`, `time.sleep`) | `run_microscope_acquire(RE, file_path, file_name, exposure_time=..., index_digits=5)` returns the TIFF paths; keep the existing image analysis on those files |
| Edge / line scan across the beam | scaler `3ids:scaler1` + motors `3idxps1:m4` (H) and `3idc:m42` (V) via PVs | `run_diode_flyscan(RE, [FlyAxis("sample_stage.xprime", a, b)], ...)` for H, `"sample_stage.base_y"` for V |
| Flux normalization | initial/current `3ida:scaler1.S2` reads | the `ion_chamber` list (`.S2`) comes from the *same count* as every diode point; divide per point |
| Move KB mirror axes | KB motor PVs | not covered: the KB motors are not Bluesky devices yet (`kb_mirrors` is a separate device file) |

The autofocus H/V motors are already Bluesky devices here: `3idxps1:m4` is `sample_stage.xprime`
and `3idc:m42` is `sample_stage.base_y`. Reuse them rather than declaring new motors.

A typical use, one alignment iteration:

```python
# 1. image of the beam on the microscope
files = run_microscope_acquire(
    RE, "/home/sector3/s3ida/pg3/", "kb_align", exposure_time=0.1, index_digits=5
)

# 2. horizontal edge scan with the diode; ion chamber normalizes it
diode, ic, pos = run_diode_flyscan(
    RE,
    [FlyAxis("sample_stage.xprime", -0.2, 0.2)],
    n_points=80,
    t_count=0.05,
    taxi_allowance=0.02,
)
profile = [d / i for d, i in zip(diode, ic)]  # guard against ic == 0 in real code

# 3. feed `files` / (pos, profile) to the autofocus analysis, move the mirrors as it
#    directs, and repeat
```

Or compose them in one plan so interlocks and moves happen first:

```python
def alignment_measure():
    # yield from <plan that moves laser_optics OUT>   # placeholder; only if omega is flown
    files = yield from microscope_acquire(
        "/home/sector3/s3ida/pg3/", "kb_align", exposure_time=0.1
    )
    diode, ic, pos = yield from diode_fly(
        [FlyAxis("sample_stage.xprime", -0.2, 0.2)], n_points=80, t_count=0.05
    )
    return files, diode, ic, pos
```

**Things to settle with the autofocus code owners before wiring this in:**

- **Which scaler.** His scan PVs are `3ids:scaler1.*` (`scaler_c`, counts on `.S3`) and his
  flux is `3ida:scaler1.S2`. The defaults here are `3ida:scaler1` (`scaler_b`) S3 for the
  diode and S2 for the ion chamber (from a meeting). Confirm which scaler holds the diode;
  pass `scaler="scaler_c"` if it is the other one.
- **Image reading.** `microscope_acquire` writes TIFF files and returns their paths. It does
  not read the PVA stream or convert to HDF5 as his code does, so his analysis must read the
  files.
- **Return shape.** `motor_values` is a list of floats for one axis and a list of tuples for
  several; confirm his code is fine with that.
- **Python entry point.** `RE` must be the startup's RunEngine, in one process at a time.

## 3. `diode_fly`: scaler fly scan

### What it measures

`diode_fly(axes, *, n_points, t_count, ...)` flies one or more axes while the scaler counts,
and returns three equal-length lists (entry `i` is the same point):

- `diode_values`: scaler channel for the diode (default `3ida:scaler1.S3`, `chan03`)
- `ic_values`: ion-chamber channel (default `.S2`, `chan02`), which normalizes the diode
- `motor_values`: position of the point: floats for one axis, tuples for several

How it works:

1. Before anything moves it reads each axis' acceleration time, computes the scan velocity
   `|end - start| / (n_points * t_count)` and the run-up/run-out positions, and checks soft
   limits (and the velocity ceiling where the PV exposes one). It saves the motor velocity
   and the scaler's preset time and count mode.
2. The axes taxi to the run-up position, the scan velocity is set, the scaler goes to
   `OneShot` with `preset_time = t_count`, and the axes start toward the run-out position.
3. While they move, it repeats one point at a time: read position, trigger the scaler (the
   call returns when the count ends, `t_count` s later), read position, then read the diode
   and ion-chamber channels. The point's position is the mean of the two readbacks.
4. Points whose position is outside `[start, end]` (run-up/run-out) are dropped. Settings are
   restored at the end, also after an abort.

**Limitation: each point integrates only its own `t_count` window, not the whole step.** The
scaler sums the diode and ion chamber over exactly the same window, so their ratio is
consistent. But between two counts there is dead time (trigger round trip, readbacks) during
which the motor keeps moving and nothing is counted. Point spacing is
`velocity * (t_count + overhead)`, each point covers only `velocity * t_count` of it, and
you get slightly **fewer than `n_points`** points. Keep `velocity * t_count` small compared
with the feature you want to resolve. A gap-free scan would need the scaler to count
continuously and take step values as differences of cumulative counts; that is not
implemented.

### Arguments

```python
diode, ic, pos = run_diode_flyscan(
    RE,
    [FlyAxis("sample_stage.xprime", -0.5, 0.5)],
    n_points=50,
    t_count=0.1,
    taxi_allowance=0.05,
    # scaler="scaler_b",                     # default: 3ida:scaler1
    # diode=scaler.channels.chan03.s,        # default: 3ida:scaler1.S3
    # ion_chamber=scaler.channels.chan02.s,  # default: 3ida:scaler1.S2
)
```

| Argument | Meaning |
|---|---|
| `RE` | The RunEngine that executes the plan (the startup's). |
| `axes` | List of `FlyAxis(motor, start, end)`. `motor` is a registry name (e.g. `"sample_stage.xprime"`) or an `EpicsMotor`. `end < start` flies backwards. Several axes fly together on a straight diagonal, each crossing its span in the same time. |
| `n_points` | Nominal number of points across the span. With `t_count` it sets the speed: the span is crossed in `n_points * t_count` (here 5 s, so 1.0 / 5 s = 0.2 units/s). |
| `t_count` | Counting time of each point in seconds (the scaler preset time). |
| `taxi_allowance` | Extra run-up/run-out distance (motor units) beyond the distance needed to reach full speed, so the motor is at constant speed on entering the span. Default 0. |
| `scaler=` | Scaler that counts, registry name or object; default `"scaler_b"`. |
| `diode=`, `ion_chamber=` | Channels to record, as signals, e.g. `scaler.channels.chan04.s`. They must be channels of the triggered scaler (checked before anything moves). |
| `return_to_start=` | Default `False`: axes stay at their run-out position. If `True`, each flown axis returns to where it was before the fly, also after an abort. |

Any motor with `user_readback`, `velocity` and `acceleration` signals works. Motor velocity,
scaler preset time and count mode are always restored, also after an error or abort.

### Things to know

- The scaler must be free (nothing else counting); it is put in `OneShot` mode during the run.
- Channels are named `chan02`, `chan03`, ... (zero-padded) on `ScalerCH`.
- `sample_stage.omega` is an `InterlockedEpicsMotor`: if `laser_optics` is not OUT the move is
  refused (`MotionInterlock`) and settings are restored. Run the interlock plan first.
- Don't move the same motor from raw-PV code while the scan runs.
- KB mirror motors are not Bluesky devices yet, so only `sample_stage`-type axes fly today.

## 4. `microscope_acquire`: single-shot microscope images

### What it does

`microscope_acquire(file_path, file_name=None, *, microscope="pg3_camera", exposure_time=None,
num_images=1, file_number=1, index_digits=None, pixel_format=None)` sets the microscope and
TIFF plugin through the RunEngine, takes `num_images` frames, waits until the files are
written, **puts every setting back**, and returns the list of written file paths (as the IOC
sees them).

```python
RE(
    microscope_acquire(
        "/home/sector3/s3ida/pg3/", "align", exposure_time=0.1, num_images=3
    )
)

files = run_microscope_acquire(
    RE,
    "/home/sector3/s3ida/pg3/",
    "align",
    exposure_time=0.1,
    num_images=3,
    file_number=1,
    index_digits=5,
)
# ['/home/sector3/s3ida/pg3/align_00001.tif', ..._00002.tif, ..._00003.tif]
```

| Argument | Meaning |
|---|---|
| `file_path` | Directory the TIFFs go to, **on the microscope IOC's filesystem**; must already exist (the plugin does not create it). the existing code's `measurement_directory`. |
| `file_name` | File name prefix. Default, as in the existing code: `sample_<exposure in ms>ms`. Files are `<file_path>/<file_name>_<number>.tif`. |
| `microscope=` | Area-detector device with `cam` and `tiff1`; registry name or object, default `"pg3_camera"`. |
| `exposure_time` | Seconds per frame (`cam.acquire_time`). Default: leave the current value. |
| `num_images` | Frames to take. 1 uses image mode `Single`, more uses `Multiple`. One file per frame. |
| `file_number` | Number of the first file, default 1 (the existing code's `image_index`). |
| `index_digits` | Digits in the file number; the existing code uses 5 (`name_00001.tif`). Default: leave the plugin's file template (normally 4 digits). |
| `pixel_format` | Pixel format (the existing code's `PIXEL_FORMAT`, e.g. 0 = Mono8). Default: leave it. |

Arguments after `file_name` are keyword-only.

### What is changed, and what is restored

The plan reads the current value of every setting it will change **before** touching
anything and restores all of them at the end, also after an error, timeout or `RE.abort()`:

- always: `cam.image_mode`, `cam.acquire_time`, `tiff1.enable`, `tiff1.file_write_mode` (set
  to `Single`, one file per frame), `tiff1.file_path`, `tiff1.file_name`,
  `tiff1.file_number`, `tiff1.auto_increment`, `tiff1.auto_save`;
- only if you use the option: `cam.num_images`, `cam.pixel_format`, `tiff1.file_template`;
- the acquire state: a microscope that was acquiring (live view) is stopped for the shot and
  acquiring again afterwards.

Anything else on the microscope or its other plugins (PVA, overlay, transform) is untouched.

### How it decides it is done

It starts the microscope and polls until `tiff1.file_number` has advanced by `num_images`
(the plugin bumps it after each file is written), then until `cam.acquire` is idle, then
waits 0.1 s for the last file to close. If the files do not appear within
`exposure_time * num_images + 30 s` it raises `TimeoutError` (usual cause: `file_path` does
not exist on the IOC), and the settings are still restored.

### Things to know

- The same file name is overwritten: the plugin does not ask.
- `file_path` and the returned paths are as the **IOC** sees them (e.g.
  `/home/sector3/s3ida/pg3/`), not necessarily as the machine running the plan does (e.g.
  `/net/s3data/export/sector3/s3ida/pg3/`; see `read_path_template` in `devices_camera.yml`).
- `pg3_camera` comes from `devices_camera.yml`, a separate YAML so an offline microscope IOC
  does not block the core devices; it must be connected.
- Don't change the same microscope from raw-PV code while the plan runs.

## 5. Checklist for the first beamline run

Both plans are unverified on hardware. Things to confirm:

- `ScalerCH` one-shot `trigger()` (ophyd puts `.CNT=1`, done when the count ends; needs
  `count_mode = OneShot`, which the plan sets), and the real per-count overhead.
- `scaler.channels.chan02` / `chan03` are the ion chamber / diode on the scaler you use.
- `channels.chan<N>.s` reads are fresh (not monitored), and motor backlash (`BDST`) is small
  versus the run-up distance.
- Microscope enum strings the plan sets (`"Single"`/`"Multiple"`, `"Enable"`, `"Yes"`) and the
  `pixel_format` values; that `tiff1.file_number` advances after each file; that restoring
  saved values reproduces the readbacks (e.g. the path readback may carry a trailing `/`).

Suggested order:

1. **Diode fly:** one slow axis, small range, few points. Check the three lists have equal
   length, positions increase steadily, diode/ion chamber look sensible. Then a longer
   range, two axes, and finally omega (interlock out).
2. **Microscope:** read the current settings; take one image with a known-good `file_path`
   and check the file and returned path; read the settings again and confirm they match;
   take `num_images=3` and check three consecutively numbered files; with live view running,
   confirm it stops for the shot and comes back; start a long acquisition, `RE.abort()` it,
   and confirm the microscope stops and the settings return.
