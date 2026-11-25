"""Autofocus program: Bluesky plans for the KB-mirror optics-alignment automation.

Bluesky plans for the 3-ID-C optics-alignment tools; none opens a Bluesky run
(no documents, nothing to Tiled).  Nothing here is imported by ``id3c.startup``;
import what you need.

* ``flyscan_diode.diode_fly`` -- fly motor(s) while counting diode + ion
  chamber on ``scaler_b``; returns ``(diode, ic, positions)`` lists
  (``run_diode_flyscan(RE, ...)`` for blocking code).
* ``microscope_acquire.microscope_acquire`` -- take one image on the microscope
  (``pg3_camera``), read from the PVA stream as a float32 array (TIFF only with
  ``save_files=True``), restoring every setting it touched
  (``run_microscope_acquire(RE, ...)`` for blocking code, returns ``(image, file)``).

See ``.context/AUTOFOCUS.md`` for the PV inventory and remaining stages.
"""
