# How to inspect data from past runs

Bluesky runs are stored in a [Tiled](https://blueskyproject.io/tiled/)
catalog.  This page shows how to read them back at the IPython prompt.

## The catalog object

The session-level catalog client is bound to `cat`:

```python
cat                # <Container ...>  -- top-level Tiled client
len(cat)           # number of runs available
cat[-1]            # most recent run
cat[-5:]           # last five runs
cat["<uid>"]       # a specific run by UID
```

You can also instantiate a fresh client without restarting your
session:

```python
from tiled.client import from_uri
fresh = from_uri("http://sn.xray.aps.anl.gov:8000")
fresh                # /raw under that server
```

## Structure of a run

A Bluesky run is a small tree of streams.  The two you will see most
often are `primary` (the main event stream the scan emits) and
`baseline` (the once-at-start, once-at-stop snapshot of every device
tagged `baseline`):

```python
run = cat[-1]
list(run)
# ['primary', 'baseline']

primary = run.primary
list(primary)
# ['data', 'config', 'time', 'seq_num', ...]
```

The `primary.data` group holds the actual readings; everything else
is bookkeeping.

## Reading the data

`run.primary.read()` returns an
[xarray.Dataset](https://docs.xarray.dev/) -- a labeled
multi-dimensional array container that plays well with pandas, numpy,
and matplotlib:

```python
ds = run.primary.read()
ds
# <xarray.Dataset>
# Dimensions:  (time: 11)
# Coordinates:
#   * time     (time) datetime64[ns] ...
# Data variables:
#     sample_stage_x  (time) float64 0.0 1.0 2.0 ...
#     scaler_chan01    (time) float64 1234.0 1180.0 ...

ds.to_pandas()
# pandas DataFrame

ds["scaler_chan01"].plot()
# matplotlib plot
```

For single columns: `ds["sample_stage_x"]` returns an `xarray.DataArray`.

## Metadata

Every run carries metadata accessible without reading the bulk data:

```python
run.metadata["start"]
# {'uid': '...', 'time': 1719000000.0, 'scan_id': 1, 'plan_name': 'scan',
#  'plan_args': {...}, 'plan_type': 'generator',
#  'detectors': ['scaler'], 'motors': ['sample_stage_x'], 'num_points': 11,
#  ...}

run.metadata["stop"]
# {'uid': '...', 'time': ..., 'exit_status': 'success', 'num_events': {...}}

run.metadata["start"]["scan_id"]
# 1
```

## Baseline stream

If the device is `baseline`-labeled in `devices.yml`, it appears in
the baseline stream automatically:

```python
run.baseline.read()
# <xarray.Dataset>
# Dimensions: (time: 2)
# Data variables (one entry per baseline device, both at start and at end):
#     sample_stage_x  (time) float64 ...
#     sample_stage_y  (time) float64 ...
#     ...
```

Useful for "what was the rest of the instrument doing while I scanned
this one motor?".

## Filtering and searching

Tiled supports basic filtering on metadata:

```python
from tiled.queries import Key

# Runs by plan name:
cat.search(Key("plan_name") == "scan")

# Runs from the last hour (server-side time field is unix epoch):
import time
cat.search(Key("time") > time.time() - 3600)
```

## Image (area-detector) data

If a scan included an area detector like the Eiger2, the image data
is referenced from the run's master HDF5 file via HDF5 external
links.  Reading it from `cat[-1]` *should* return a dask-backed
array, but as of the date of this writing the end-to-end path is not
yet validated at 3-ID-C.  See [How to visualize
HDF5](visualize_hdf5.md) for the current state.

## Common pitfalls

- **Wrapping reads in `RE(...)`** is wrong; `cat[-1].primary.read()`
  is not a plan.  See [The
  RunEngine](../explanation/run_engine.md).
- **Reading a very large image array eagerly.** `read()` will pull
  the entire dataset into memory.  For Eiger-class data, slice
  first: `run.primary["eiger2_image"][0]` to get the first frame.
- **Stale `cat` after a long session.** Tiled clients cache
  schemas; if a new run is not showing up, refresh with
  `cat.refresh()`.

## See also

- [How to visualize HDF5 image files](visualize_hdf5.md) -- area
  detector workflow.
- [How to run a scan](run_a_scan.md) -- to produce the data.
- [Tiled documentation](https://blueskyproject.io/tiled/).
