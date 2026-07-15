# Eiger2 NeXus / NDAttributes config (3-ID-C)

Embed motor positions (and acquisition params) **into the Eiger2 HDF5 files
themselves**, via the EPICS AreaDetector HDF5 plugin — the robust, IOC-side
alternative to appending with `h5py` after the fact (the
`_record_det_positions_in_hdf` helper in `db_bps.py`, which fought NFS paths).

Based on the NeXus manual's EPICS example:
https://manual.nexusformat.org/examples/epics/index.html

## Files
- **`attributes.xml`** — declares which EPICS PVs to capture as *NDAttributes*
  (det_x / eiger_y / eiger_z, omega / xprime / base_y / zprime, exposure).
  Attributes attach to every NDArray ⇒ **one value per frame**.
- **`layout.xml`** — *optional*. Defines where those land in the HDF5 tree
  (`/entry/instrument/detector_stage/…`, `/entry/instrument/sample_stage/…`).
  Replaces the plugin's default layout, so handle with care (see below).

## Two ways to use this

### A. attributes.xml only — SAFE, recommended first step
Set the camera's attributes file; keep the default HDF layout:
```
caput dp_eiger_sn:cam1:NDAttributesFile "/path/on/ioc/attributes.xml"
caget dp_eiger_sn:cam1:NDAttributesStatus      # expect OK / 0
```
All declared values appear automatically in the file's **default
NDAttributes group** (e.g. `/entry/instrument/NDAttributes/det_x`). No layout
change, no risk to existing structure.

### B. attributes.xml + layout.xml — custom placement
Also load the layout to get tidy `/entry/instrument/detector_stage/det_x` etc.:
```
caput dp_eiger_sn:HDF1:XMLFileName "/path/on/ioc/layout.xml"
caget dp_eiger_sn:HDF1:XMLValid_RBV            # expect 1
caget dp_eiger_sn:HDF1:XMLErrorMsg_RBV         # expect empty
```
**Safest:** rather than swapping in `layout.xml` wholesale, copy just the
`<group name="detector_stage">` / `<group name="sample_stage">` blocks into
your IOC's *current* layout file. The flyscan external-links `/entry/data`
as the image stack, so that group must keep holding the image data (this
file preserves it via the hardlink at the end).

## Before you deploy — VERIFY
1. **det_x PV** — was previously mis-mapped to base_Z (`3idc:m43`). Put the
   corrected det_x motor PV in `attributes.xml`.
2. **cam prefix** — `attributes.xml` assumes `dp_eiger_sn:cam1:`. Confirm
   (`cam1:` vs `cam:`) before setting `NDAttributesFile` / the acquire PVs.
3. **persistence** — `caput` is runtime-only. To survive IOC restarts, set
   these in the IOC startup (`st.cmd` / autosave), or via the plugin GUI and
   save. This is a controls/IOC task, not part of this repo.

## Validate the XML offline (optional)
```
xmllint --noout --schema NDAttributes.xsd attributes.xml
xmllint --noout --schema hdf5_xml_layout_schema.xsd layout.xml
```
(Schemas ship with ADCore: `$ADCORE/iocBoot/` / ADCore docs.)

## Verify it worked
After an acquisition, open a file and look for the values:
```
h5dump -n /path/to/file.h5 | grep -iE "det_x|eiger_|omega|NDAttributes"
```
For a multi-frame file each position dataset should have one element per frame.

## Relation to the Bluesky plans
Once this is live, the per-file position info is in the Eiger HDF5 natively,
so you can drop / stop relying on `_record_det_positions_in_hdf` and the
`det_step_scan` `.txt` sidecar (keep them as belt-and-suspenders if you like).
