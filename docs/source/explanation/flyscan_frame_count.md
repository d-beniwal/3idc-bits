# Flyscan frame count: why the HDF5 has *more* frames than requested

This page explains a question that comes up the first time anyone
counts the images in a flyscan HDF5 file:

> I asked for 301 frames, but the detector HDF5 file consistently has
> 306.  Where do the extra frames come from?

Short answer: **this is expected, by design.**  The raw HDF5 file is a
*superset* that always contains a few extra frames at the start and
end of the scan.  The "useful" frames are selected (and paired with
motor positions) by a downstream analysis step, not by trimming the
raw file.  A *consistent* small overage (here, +5) is a sign the
timing is stable, which is the healthy case.

## Where to find the paired frames and positions

You do not have to run any analysis yourself.  After every flyscan
completes, the plan writes the frame-to-position pairing **into the
NeXus master HDF5 file** (the `*.hdf` file in your run directory).
Look here:

`/entry/flyscan_data` (an `NXdata` group, the **default plot** and the
single primary product)
: A ready-to-plot view of the in-scan data, with **one row per in-scan
  frame** (the pre-roll and tail frames are already excluded -- so this
  group has your 301 rows, not 306).  Its `data` is a *virtual* dataset
  (no bytes copied) that already slices the in-scan 301-frame substack
  out of the full image stack, with `position_start_acquire` as its
  plot axis.  The plan also sets `/entry@default = "flyscan_data"`, so a
  NeXus-aware viewer (e.g. NeXpy, h5web) opens this group by default --
  in-scan images vs. omega position, which is the useful default view of
  a flyscan.  Datasets:

  - `data` -- the in-scan image substack (virtual dataset).
  - `position_start_acquire`, `position_end_acquire`,
    `position_end_period` -- the motor (omega) position at three
    phases of each frame's exposure period (the plot axes).
  - `image_number` -- IOC frame counter (1-based) for each in-scan
    frame.
  - `frame_index` -- `image_number - 1`; the **0-based index into
    `/entry/images/data`** for that frame.  `data` is already this
    substack; use `frame_index` only to map back to the full
    (306-frame) image stack:

    ```python
    import h5py
    with h5py.File("20260618-...-S00031-....hdf", "r") as f:
        images = f["/entry/images/data"]               # all captured frames
        idx = f["/entry/flyscan_data/frame_index"][:]  # in-scan frames only
        in_scan_images = images[idx, :, :]             # the useful 301
        omega = f["/entry/flyscan_data/position_start_acquire"][:]
    ```

  - `timestamp` -- per-frame timestamp.

  Provenance group attributes record that the data came from the
  authoritative area-detector file: `source = "ad_file"`,
  `n_frames_paired`, and `n_frames_expected`.

`/entry/images` (an external link)
: An HDF5 external link to the detector IOC's frame file at its
  `/entry/data`.  This is the **full** stack -- all captured frames,
  including the pre-roll and tail frames.  This is the dataset whose
  length is 306, not 301.

So: read `/entry/flyscan_data` for the paired, trimmed result; read
`/entry/images` only if you specifically want the raw superset.

:::{note}
`/entry/flyscan_data` is written best-effort at the end of the run, and
**only** from the area-detector file.  If the Tiled catalog hasn't
ingested the run yet, or the IOC frame file isn't readable from the
master-file host (most commonly: the per-detector image-files symlink
next to the master is missing or mis-shaped), you will see only
`/entry/images` (the external link) with a `WARNING` in the log and
**no** `/entry/flyscan_data`.  In that case fix the symlink and re-run
the pairing yourself with the analysis function linked below.
:::

:::{admonition} Keep this page in sync with the code
:class: important

The numbers and behavior below are determined by the flyscan plan and
the frame/position pairing code.  If you change either, update this
page in the same commit:

- Frame-count formula and scan geometry:
  [`compute_flyscan_geometry`](../../../src/id3c/plans/flyscan_3idc.py)
  (`num_frames = round(1 + (p_end - p_start) * exposures_per_egu)`).
- Pre-roll behavior (cam starts before the motor launches):
  `takeoff_and_monitor` in
  [`flyscan_3idc.py`](../../../src/id3c/plans/flyscan_3idc.py).
- Post-stop tail behavior (cam stops on the `p_end` crossing):
  `monitor_loop` / `_emit_pending_frames` in
  [`flyscan_3idc.py`](../../../src/id3c/plans/flyscan_3idc.py).
- HDF5 capture over-allocation:
  `hdf_num_capture = int(num_frames * 1.5) + 20` in `flyscan`
  ([`flyscan_3idc.py`](../../../src/id3c/plans/flyscan_3idc.py)).
- Frame-to-position pairing (the thing that selects the useful
  frames): `pair_frames_to_positions_from_ad_file` in
  [`flyscan_3idc_analysis.py`](../../../src/id3c/utils/flyscan_3idc_analysis.py).
- Master-file HDF5 layout written at run end (`/entry/flyscan_data`,
  `/entry/images`, and `/entry@default`):
  `update_master_file` in
  [`flyscan_3idc.py`](../../../src/id3c/plans/flyscan_3idc.py).  If you
  rename or restructure any of those groups/datasets, update the
  "Where to find the paired frames and positions" section below.
:::

## The requested count is correct

The number of *useful* frames is computed from your scan parameters by
`compute_flyscan_geometry`, using fence-post counting (one frame at
each endpoint, plus `exposures_per_egu` frames per engineering unit
between them):

```text
num_frames = round(1 + (p_end - p_start) * exposures_per_egu)
```

For the omega scan in the report -- `p_start = -30`, `p_end = +30`,
`exposures_per_egu = 5` -- that is:

```text
num_frames = round(1 + (30 - (-30)) * 5)
           = round(1 + 60 * 5)
           = 301
```

So 301 is exactly the right *target*.  Nothing is wrong with the
geometry math.  With `t_period = 1.0 s`, the scan also takes
`num_frames * t_period = 301 s`, and omega moves at
`60 deg / 301 s ~= 0.199 deg/s`.

## Where the extra frames come from

The Eiger2 runs in **continuous (Capture) mode**: it acquires frames
the whole time the cam is armed, and the HDF plugin writes every frame
it receives.  The cam is armed for slightly *longer* than the useful
`p_start -> p_end` window, for two deliberate reasons.

### 1. Pre-roll frames (start of scan)

`takeoff_and_monitor` starts the cam (`Acquire = 1`) **and waits for
the first frame to actually land** before it launches the motor.  This
ordering was adopted after observing that the cam sometimes delivered
its first frame several seconds after `Acquire = 1`; by then the motor
had already moved past `p_start`, silently eating into the requested
frame budget.

The trade-off is a few **pre-roll frames** captured while the motor is
still parked at `p_initial` (the taxi start, upstream of `p_start`).
These are written to the file -- harmless, because the downstream
pairing step keys on motor position, not on frame index.

### 2. Post-stop tail frames (end of scan)

`monitor_loop` tells the cam to stop only when the motor's *readback*
crosses `p_end`.  The motor record updates its readback at ~10 Hz, and
the cam may already have one or more exposures in flight, so a few
**tail frames** flush to the file after the crossing.  These are
flagged as post-scan tail (they are suppressed from the position
stream by `_emit_pending_frames`) but are still physically written to
the HDF5 file.

### 3. The file is intentionally sized to hold them

So that none of these extras overflow the dataset, the HDF plugin's
capture count is over-allocated:

```text
hdf_num_capture = int(num_frames * 1.5) + 20
```

The code comment says this directly: the headroom is "comfortable for
any sensible scan size while absorbing takeoff & landing leading
frames, post-stop tail frames, and timing jitter."

## Putting it together: 301 -> 306

```text
 pre-roll frames  (motor parked at p_initial, before p_start)   ~a few
+ 301 useful frames (p_start .. p_end)
+ post-stop tail frames (cam in-flight, after p_end crossing)   ~a few
------------------------------------------------------------------------
= 306 frames written to the raw HDF5 file
```

A *consistent* +5 (rather than a number that jumps around) means the
takeoff latency and stop overshoot are repeatable.  That is the
well-behaved case.  A large or varying overage would be the thing to
investigate.

## How to get exactly your 301 frames

Do **not** expect the raw `/entry/data` dataset to equal `num_frames`.
The raw file is the superset; the *useful* frames are selected by
pairing each frame to the omega position it was exposed at:

- `pair_frames_to_positions_from_ad_file(...)` -- directly from the
  area-detector HDF5 file (the authoritative, lossless source used by
  the plan).

It lives in
[`flyscan_3idc_analysis.py`](../../../src/id3c/utils/flyscan_3idc_analysis.py).
It uses the area-detector file's per-frame timestamps together with the
motor's position stream to drop the pre-roll and tail frames and attach
the correct omega value to each remaining frame.  The result is the 301
position-paired frames that span `p_start -> p_end`.

## What this does *not* mean

- It is **not** a bug in the frame-count formula.  The formula yields
  301 and that is correct.
- It is **not** dropped or duplicated data.  All 306 frames are real
  exposures; the extras simply fall outside the requested angular
  range.
- It is **not** something to "fix" by changing `exposures_per_egu` or
  `t_period`.  Those control the useful count, not the pre-roll/tail
  overhead.
