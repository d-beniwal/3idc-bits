"""OPT_Luca workstream: Luca Rebuffi's KB-mirror optics-alignment automation.

Bluesky plans for the 3-ID-C optics-alignment tools; none opens a Bluesky run
(no documents, nothing to Tiled).  Nothing here is imported by ``id3c.startup``;
import what you need.

* ``flyscan_diode.diode_fly`` -- fly motor(s) while counting diode + ion
  chamber on ``scaler_b``; returns ``(diode, ic, positions)`` lists
  (``run_diode_flyscan(RE, ...)`` for blocking code).
* ``microscope_acquire.microscope_acquire`` -- acquire frame(s) on the microscope
  (``pg3_camera``) into TIFF
  files, restoring every setting it touched (``acquire_images(RE, ...)`` to get the
  file list).

See ``.context/OPT_LUCA.md`` for the PV inventory and remaining stages.
"""
