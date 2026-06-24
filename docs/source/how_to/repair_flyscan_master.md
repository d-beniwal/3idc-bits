# Repair a flyscan master file missing `/entry/flyscan_data`

A flyscan NeXus master file (`*.hdf`) holds its primary product in the
`/entry/flyscan_data` group: the in-scan image substack plus the
per-frame motor positions.  That group is written at run end **only**
when the area-detector image file is reachable from the master file's
directory through the image-files symlink (`{detector}_files`,
e.g. `eiger2_files`).

If the symlink was missing or mis-shaped at run end, the master will
have `/entry/images` (the external link) but **no**
`/entry/flyscan_data`, and a `WARNING` will be in the run log.  Once
the symlink is fixed, recover the group with the repair tool.

## Step 1 -- fix the image-files symlink

From the directory that contains the master file, create the symlink
pointing at the workstation mount where the detector's image files are
visible.  For an `eiger2` detector whose files are mounted at
`/net/s3data/export/sector3/s3ida/XRD/`:

```bash
cd /path/to/your/run/directory
ln -s /net/s3data/export/sector3/s3ida/XRD/ eiger2_files
ls -l eiger2_files          # should show 'eiger2_files -> .../XRD/'
ls eiger2_files/ | head     # should list at least one entry
```

The symlink must map directly to the image-file root.  A common
mistake is creating a directory `eiger2_files/` that *contains* a
child symlink (e.g. `eiger2_files/XRD -> .../XRD`); that adds an extra
path level and the external link will not resolve.

## Step 2 -- run the repair tool

```bash
id3c-flyscan-repair /path/to/your/run/20260618-...-S00024-....hdf
```

The tool:

1. reads the run uid from the master's `/entry/entry_identifier`;
2. locates the area-detector file from the master's `/entry/images`
   external link (or, if absent, composes it from the `ad_file_path` /
   `ad_file_name` start metadata);
3. recomputes the per-frame pairing from the authoritative
   area-detector file; and
4. writes `/entry/flyscan_data` (replacing any existing copy) and sets
   `/entry@default = "flyscan_data"`.

### Preview without writing

```bash
id3c-flyscan-repair --dry-run /path/to/.../master.hdf
```

### Point at a specific area-detector file

If the master cannot resolve the area-detector path (or you want to
override it), pass it explicitly:

```bash
id3c-flyscan-repair --external-file /net/.../XRDS4_..._000001.h5 \
    /path/to/.../master.hdf
```

Add `-v` for INFO-level logging.

## When repair is not possible

The tool exits non-zero and explains the cause if:

- the master has no run uid (`/entry/entry_identifier`);
- the area-detector file cannot be located or does not resolve (most
  often the symlink from Step 1 is still wrong);
- the area-detector file is not openable; or
- pairing produces zero in-scan frames.

The repair reads only the area-detector file (the lossless,
authoritative per-frame source).  It never falls back to a lossy
source, so a repaired `/entry/flyscan_data` is always the full-count
result.
