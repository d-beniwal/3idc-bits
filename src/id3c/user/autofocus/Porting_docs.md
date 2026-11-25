# Porting docs: using the autofocus plans in the optics-alignment workflow

This folder holds two Bluesky plans for the autofocus program (KB-mirror optics alignment).
They replace the raw-EPICS measurement steps of its existing code (`AI-Beamline-3ID`, built
on `Common-Libraries`; both are read-only references here and were not modified):

| Plan | Module | Replaces in the existing autofocus code |
|---|---|---|
| `diode_fly` / `run_diode_flyscan` | `flyscan_diode.py` | the bare-PV scaler scan loop of `Beamline3IDBeamManagerScan` (motor H/V + scaler counts) |
| `microscope_acquire` / `run_microscope_acquire` | `microscope_acquire.py` | the single-shot image acquisition of `GenericCamera.collect_single_shot_image` (used by `Beamline3IDBeamManagerCamera`) |

**Status:** both are tested offline (33 microscope regressions in this repo, plus
20 scaler tests in `BL_instrumentation/opt/tests`). Nothing has run on the real scaler, motors,
microscope or EPICS. The PVA image read (`_read_pva_frame`) is also checked against a real
`pvapy` stack (loopback server, `opt/tests/pva_reader_check.py`). See "Checklist for the first beamline run" at the end.

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
| Image of the beam | `GenericCamera.collect_single_shot_image` (raw `epics.PV`, `time.sleep`) | `run_microscope_acquire(RE, exposure_time=...).image` reads the frame from the PVA stream the way he does (float32, nothing saved); add `save_files=True` and a `file_path` to also get a TIFF |
| Edge / line scan across the beam | scaler `3ids:scaler1` + motors `3idxps1:m4` (H) and `3idc:m42` (V) via PVs | `run_diode_flyscan(RE, [FlyAxis("sample_stage.xprime", a, b)], ...)` for H, `"sample_stage.base_y"` for V |
| Flux normalization | initial/current `3ida:scaler1.S2` reads | the `ion_chamber` list (`.S2`) comes from the *same count* as every diode point; divide per point |
| Move KB mirror axes | KB motor PVs | not covered: the KB motors are not Bluesky devices yet (`kb_mirrors` is a separate device file) |

The autofocus H/V motors are already Bluesky devices here: `3idxps1:m4` is `sample_stage.xprime`
and `3idc:m42` is `sample_stage.base_y`. Reuse them rather than declaring new motors.

A typical use, one alignment iteration:

```python
# 1. image of the beam on the microscope
image = run_microscope_acquire(RE, exposure_time=0.1).image   # float32 (ny, nx); nothing saved

# 2. horizontal edge scan with the diode; ion chamber normalizes it
diode, ic, pos = run_diode_flyscan(
    RE,
    [FlyAxis("sample_stage.xprime", -0.2, 0.2)],
    n_points=80,
    t_count=0.05,
    taxi_allowance=0.02,
)
profile = [d / i for d, i in zip(diode, ic)]  # guard against ic == 0 in real code

# 3. feed `image` / (pos, profile) to the autofocus analysis, move the mirrors as it
#    directs, and repeat
```

Or compose them in one plan so interlocks and moves happen first:

```python
def alignment_measure():
    # yield from <plan that moves laser_optics OUT>   # placeholder; only if omega is flown
    shot = yield from microscope_acquire(exposure_time=0.1)   # shot.image
    diode, ic, pos = yield from diode_fly(
        [FlyAxis("sample_stage.xprime", -0.2, 0.2)], n_points=80, t_count=0.05
    )
    return shot.image, diode, ic, pos
```

**Things to settle with the autofocus code owners before wiring this in:**

- **Which scaler.** His scan PVs are `3ids:scaler1.*` (`scaler_c`, counts on `.S3`) and his
  flux is `3ida:scaler1.S2`. The defaults here are `3ida:scaler1` (`scaler_b`) S3 for the
  diode and S2 for the ion chamber (from a meeting). Confirm which scaler holds the diode;
  pass `scaler="scaler_c"` if it is the other one.
- **Image reading.** `microscope_acquire` returns the array his `_get_image_stream_data` has
  right after `_, image, ... = adu.reshapeNtNdArray(PV_stream.get(''))` and
  `copy.deepcopy(image).astype(np.float32)` (the same two lines): float32, shape `(ny, nx)`.
  Everything after that line stays his: the `h_coord`/`v_coord` axes from the pixel size,
  `apply_transformations(... image_ops)`, and the HDF5 save. TIFFs are written only with
  `save_files=True`.
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

## 4. `microscope_acquire`: one verified PVA image

```python
image = run_microscope_acquire(RE, exposure_time=0.1).image
shot = run_microscope_acquire(
    RE, "/home/sector3/s3ida/pg3/", "align", exposure_time=0.1,
    save_files=True, file_number=1,
)
```

Returns `MicroscopeShot(image, file)`: a private float32 mono `(ny, nx)` array and
an IOC TIFF path (`None` by default). No Bluesky run is opened. The signature is:

```python
microscope_acquire(file_path=None, file_name=None, *, microscope="pg3_camera",
                   exposure_time=None, save_files=False, file_number=1,
                   pixel_format=None, pva_pv=None)
```

`file_path` is an existing IOC directory and is required only when saving.
`file_name` defaults to `sample_<exposure in ms>ms`. The plugin's existing TIFF
filename template is used; an existing file can be overwritten. `exposure_time`
and `pixel_format` default to keeping their current values. `pva_pv` defaults to
the plugin's `PvName_RBV`, rather than assuming its name from the CA prefix.

### Why the acquisition waits and checks identity

The camera stops, then every declared ancestor of PVA/TIFF is drained upstream
first using `NumQueuedArrays`, which includes executing callbacks. A quiet PVA
counter or an empty input queue alone can miss an old frame still processing in
a transform. Undeclared/cyclic upstreams and enabled plugin sorting are rejected;
sorting buffers are not covered by callback queue counts.

TIFF autosave stays off during the shot. The plan requires exactly one new camera
frame and one new PVA frame, waits for acquisition and plugin completion, and
compares the returned NTNDArray's unique ID and EPICS timestamp with the PVA plugin.
Pixels and metadata come from the same PVA object. Camera IDs are not assumed to
be equal to the camera's array counter.

When saving, TIFF must hold that same frame. Its callbacks are disabled to freeze
the verified buffer; an explicit `WriteFile` command waits for EPICS put completion
and writer idle, then checks `WriteStatus`/`WriteMessage`. This catches delayed write
errors that a file-number increment or a fixed sleep cannot. The returned path is
`FullFileName_RBV` from the IOC.

### Restoration and failure behavior

Before any change, the plan snapshots image/trigger mode, any requested exposure
or pixel-format change, and the TIFF settings it changes. It temporarily selects
Internal trigger mode (0). Memory-only shots disable TIFF callbacks and autosave;
save requests buffer with autosave off and write only after frame verification.
Active TIFF capture/writing and active Single/Multiple camera acquisitions are rejected
before mutation: open file buffers and partly completed sequences cannot be reconstructed
by restoring settings.

Cleanup first confirms camera idle, drained plugins, and writer idle. Independent
settings are restored individually; one failed PV does not skip the others.
Autosave/callback enable are restored last and live view resumes only after those
restorations succeed. If stopping/draining fails, dependent settings are left alone
to avoid changing camera mode or filenames during an exposure/write. Failures raise;
if acquisition and restoration both fail, an `ExceptionGroup` preserves both errors.

Requires the existing `pvapy` installation and the configured ADCore 3.4 queue,
identity and TIFF command/status PVs. Regression tests are in
`src/id3c/tests/test_microscope_acquire.py` (also collected from `opt/tests`).
Beamline verification is still required; tests never connect to beamline PVs.

## 5. Checklist for the first beamline run

Both plans are unverified on hardware. Things to confirm:

- `ScalerCH` one-shot `trigger()` (ophyd puts `.CNT=1`, done when the count ends; needs
  `count_mode = OneShot`, which the plan sets), and the real per-count overhead.
- `scaler.channels.chan02` / `chan03` are the ion chamber / diode on the scaler you use.
- `channels.chan<N>.s` reads are fresh (not monitored), and motor backlash (`BDST`) is small
  versus the run-up distance.
- Microscope enum strings the plan sets (`"Single"`/`"Multiple"`, `"Enable"`, `"Yes"`) and the
  `pixel_format` values; that explicit TIFF `WriteFile` put completion waits for
  writing/closing; that restored settings reproduce the saved readbacks (paths may
  carry a trailing `/`); that every declared camera/plugin `NumQueuedArrays` drains;
  that PVA/TIFF IDs and EPICS timestamps match the NTNDArray. Check image orientation
  against live view. Use the existing environment with `pvapy` available.

Suggested order:

1. **Diode fly:** one slow axis, small range, few points. Check the three lists have equal
   length, positions increase steadily, diode/ion chamber look sensible. Then a longer
   range, two axes, and finally omega (interlock out).
2. **Microscope:** read the current settings; take one image (default, nothing saved) and
   check the array shape/dtype/content against the live view and that it is the *new* frame;
   read the settings again and confirm they match; call it 3 times in a row (each image new?);
   then `save_files=True` with a known-good `file_path`: check the file and the returned
   path; with live view running, confirm it stops for the shot and comes back; start a long acquisition, `RE.abort()` it,
   and confirm the microscope stops and the settings return.
