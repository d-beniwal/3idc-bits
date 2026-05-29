# How to visualize HDF5 image files

This page covers reading area-detector image data acquired during
a Bluesky run.  At 3-ID-C the relevant detector is the Eiger2 500k.

:::{warning}

The end-to-end image-file visibility from the Eiger IOC host to the
Tiled server at 3-ID-C is **not yet validated**.  The API surface
shown below is correct in principle; whether it succeeds today
depends on whether the detector's output directory is reachable from
the Tiled server's file system, the AD HDF5 plugin paths are
correctly configured, and the master file's external links resolve.

If a read fails, the most common causes are:

- The Eiger IOC and the Tiled server do not share a common path to
  the image files.  See the `read_path_template` and
  `write_path_template` in `configs/devices.yml`; these must agree
  with what the IOC actually writes.
- The master HDF5 file's external links reference a path the
  reading process cannot resolve.

These are configuration issues, not code issues; they will be
revisited as the Eiger comes online.

:::

## The moving parts

A Bluesky-driven area-detector exposure produces several files:

1. The **image file**, written by the Eiger IOC's HDF5 plugin (or
   the Eiger's own DCU, depending on configuration).  Lives on the
   detector host's local filesystem.
2. The **master file**, written by the IOC's HDF5 plugin.  Contains
   metadata about the dataset and an **HDF5 external link** to the
   image file.
3. The **Bluesky run documents**, written by the `TiledWriter`
   subscriber to the Tiled server.  References the master file.

When a client reads the image via Bluesky/Tiled, the chain is:

```
client -> Tiled -> master.h5 -> (external link) -> image.h5
```

If every hop succeeds, the client gets a numpy or dask array back.
If any hop fails, an exception bubbles up at `read()` time.

## The intended user workflow

Assuming the Eiger has been instantiated in `devices.yml` (it has
been at minimum sketched, with FIXME markers for the file paths):

```python
RE(bp.count([eiger2], num=1))
run = cat[-1]

list(run.primary)
# ['data']                 # if all went well
# ['data', 'eiger2_image'] # the image dataset is named after the AD prefix

images = run.primary["eiger2_image"]
images.shape
# (1, 514, 1030)            # (num_exposures, height, width)
```

For a multi-frame scan:

```python
RE(bp.count([eiger2], num=10))
images = cat[-1].primary["eiger2_image"]
images.shape
# (10, 514, 1030)

# Slice before reading; this is dask-backed, so reading is lazy.
first = images[0].read()      # numpy array, the first frame
```

Plotting:

```python
import matplotlib.pyplot as plt
plt.imshow(first)
plt.colorbar()
plt.show()
```

## Reading the master file directly

If you want to skip Tiled and look at the on-disk master file (for
example to debug the external-link configuration):

```python
import h5py
path = run.metadata["start"]["plan_args"]["..."]  # or wherever you have the path
with h5py.File(path, "r") as f:
    f.visit(print)            # list every dataset and external link
    img = f["entry/data/data"][...]  # path depends on Eiger plugin config
```

The `f.visit(print)` call is the quickest way to see what is in the
master file and whether the external links resolve.  If you see the
external link path printed but a read of the dataset fails, the link
target is unreachable -- not a Bluesky problem, a filesystem visibility
problem.

## Known limitations at 3-ID-C as of this writing

- The Eiger2 device is declared (but commented out) in
  `configs/devices.yml` with `FIXME` markers on `read_path_template`
  and `write_path_template`.  Those need correct beamline-specific
  paths before the AD HDF5 plugin can write usefully.
- The Tiled server at
  [sn.xray.aps.anl.gov:8000](http://sn.xray.aps.anl.gov:8000) has
  *some* path access; whether it can read files written by the
  Eiger host has not been end-to-end validated.

When this changes, update this page accordingly.

## See also

- [How to inspect data](inspect_data.md) -- non-image data is more
  fully validated.
- The Eiger2 declaration in
  [`src/id3c/configs/devices.yml`](../../../src/id3c/configs/devices.yml)
  -- commented out with FIXMEs.
- [HDF5 external links](https://docs.hdfgroup.org/hdf5/v1_14/group___h5_l.html)
  -- the HDF5 library feature this all depends on.
