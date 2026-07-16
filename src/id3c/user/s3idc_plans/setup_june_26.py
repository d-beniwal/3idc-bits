"""3-ID-C data-acquisition plans -- S3IDC commissioning beamtime (June 2026).

These plans were developed and tested during the development cycle from
15 June to 2 July 2026 -- the beamtime in which Bluesky was commissioned for
the S3IDC (3-ID-C) beamline.  Developed and tested by Dishant Beniwal,
Barbara Lavina and Peter Jemian.

Detailed, hand-tuned plans for 3-ID-C data collection.  Author these
as ``@plan``-decorated generators and run them through the RunEngine::

    from id3c.user.s3idc_plans.setup_june_26 import omega_fly
    RE(omega_fly())

Conventions (see ../../AGENTS.md and docs/running_scans_at_3idc.md):

* Decorate every plan / plan-stub with ``@plan`` so a forgotten
  ``RE(...)`` warns instead of silently doing nothing.
* Compose with ``yield from``; never call a plan bare inside another.
* Look devices up by name from the module-level ``oregistry``
  (imported from ``apsbits.core.instrument_init``, the same way the
  library ``flyscan`` plan does) -- not via ``@with_registry`` and not
  via session globals.
* Pass an explicit ``plan_name=`` to wrapped library plans (e.g.
  ``flyscan``) so the run's provenance reflects *this* wrapper.

GUI-parseable docstrings (IMPORTANT -- keep consistent across ALL plans)
-----------------------------------------------------------------------
The Bluesky Plan Runner GUI builds each plan's parameter form directly
from its docstring + signature (no import, AST only), so every plan MUST
document its arguments in one fixed grammar.  Use a standard NumPy
``Parameters`` section with one entry per argument::

    <name> : <dtype>[ [<units>]]
        <short name> :: <long description, may wrap over indented lines>

* ``<dtype>`` is one of ``str``, ``int``, ``float``, ``bool``,
  ``choice{opt1, opt2, ...}`` or ``positions`` (multi-line triples).
* ``[<units>]`` is optional, e.g. ``[deg]``, ``[mm]``, ``[s]``, ``[1/deg]``.
* The body is split on the first `` :: `` -- left = the short field label,
  right = the long help text (shown as a tooltip).
* Defaults and required-ness come from the **signature**, never the
  docstring: no default => required; a ``None`` default => the field is
  optional and left blank omits the argument.
* Arguments left OUT of the ``Parameters`` section (e.g. ``md``) are hidden
  from the GUI.  The plan summary shown in the GUI is this docstring's first
  paragraph.
"""

import logging

from apsbits.core.instrument_init import oregistry
from bluesky import plan_stubs as bps  # noqa: F401
from bluesky import plans as bp  # noqa: F401
from bluesky import preprocessors as bpp
from bluesky.utils import plan
from ophyd.status import SubscriptionStatus

from id3c.plans.flyscan_3idc import flyscan

#logger = logging.getLogger(__name__)


def _wait_cam_idle(det, timeout):
    """Plan stub: wait until the cam stops acquiring (read-only poll).

    Polls ``cam.acquire_busy`` (or ``cam.acquire`` RBV) for a return to
    0.  We poll for the *done* state rather than waiting on a ``set``
    status, because a waited ``set(acquire, 1)`` completes when the RBV
    reaches 1 ("acquiring", the start), not when the frame finishes.
    """
    busy = getattr(det.cam, "acquire_busy", None) or det.cam.acquire
    status = SubscriptionStatus(
        busy, lambda *, value, **_: int(value) == 0, run=True, timeout=timeout
    )
    while not status.done:
        yield from bps.sleep(0.1)


def _wait_for_captured(det, n_target, timeout):
    """Plan stub: wait until ``hdf1.num_captured`` reaches ``n_target``.

    Arming HDF capture resets ``num_captured`` to 0, so this waits for a
    *positive edge* -- the frame(s) actually landing in the capture buffer.
    Use this (NOT ``_wait_cam_idle``) right after firing ``acquire=1``:
    ``acquire_busy`` reads 0 both *before* the cam starts and *after* it
    finishes, so a ``run=True`` idle-wait returns instantly and the frame
    would be aborted before it is ever taken.
    """
    counter = det.hdf1.num_captured
    status = SubscriptionStatus(
        counter,
        lambda *, value, **_: int(value) >= n_target,
        run=True,
        timeout=timeout,
    )
    while not status.done:
        yield from bps.sleep(0.1)


def _flush_hdf_file(det, timeout):
    """Plan stub: stop capture and force the HDF5 file to disk (Capture mode).

    Mirrors the proven flush in ``flyscan_3idc``: ``write_file`` is an
    EpicsSignalWithRBV, so fire the trigger and poll RBV back to 0
    ("Done") rather than waiting on the set (which completes at "Writing").
    """
    yield from bps.mv(det.hdf1.capture, 0)
    try:
        captured = int(det.hdf1.num_captured.get())
    except Exception:  # noqa: BLE001
        captured = 0
    if captured > 0 and hasattr(det.hdf1, "write_file"):
        yield from bps.abs_set(det.hdf1.write_file, 1)
        yield from bps.sleep(0.1)
        status = SubscriptionStatus(
            det.hdf1.write_file,
            lambda *, value, **_: int(value) == 0,
            run=True,
            timeout=timeout,
        )
        while not status.done:
            yield from bps.sleep(0.1)


def _record_det_positions_in_hdf(det, positions, requested=None):
    """Append detector-stage positions into the AD HDF5 file just written.

    Best-effort, non-plan helper (plain file I/O, like ``flyscan``'s own
    ``update_master_file``): after a flyscan has finished and flushed its
    file, resolve that file's local-readable path the same way ``flyscan``
    does (``_external_link_target`` -> ``{AD_FILES_ROOT}<suffix>``) and add
    an ``NXcollection`` group ``/entry/instrument/detector_stage`` holding
    the actual readback of each axis (full precision), with the requested
    value recorded as an attribute.

    ``positions`` / ``requested`` are ``{axis_name: value}`` mappings (the
    actual PV readback and the requested target, respectively).  Never
    raises -- a failure to annotate must not fail the scan; it only prints.
    """
    import h5py

    from id3c.plans.flyscan_3idc import _external_link_target

    requested = requested or {}
    try:
        path = _external_link_target(det)
    except Exception as exc:  # noqa: BLE001
        print(f"_record_det_positions_in_hdf: cannot resolve AD file path: {exc!r}")
        return
    try:
        with h5py.File(path, "a") as root:
            grp = root.require_group("/entry/instrument/detector_stage")
            grp.attrs["NX_class"] = "NXcollection"
            for axis, value in positions.items():
                if axis in grp:
                    del grp[axis]
                ds = grp.create_dataset(axis, data=float(value))
                ds.attrs["units"] = "mm"
                if axis in requested:
                    ds.attrs["requested"] = float(requested[axis])
        print(f"_record_det_positions_in_hdf: wrote {positions} into {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"_record_det_positions_in_hdf: failed to write into {path}: {exc!r}")


def _acquire_single_file(det, fname, file_write_mode, exposure_time):
    """Plan stub: open cam+HDF, expose one frame, then close+write ONE file.

    Names the file ``fname`` and (in Capture mode) arms capture, takes one
    frame, then flushes -- so each call writes a single self-contained file.
    Used per-point by ``fixed_exp_at_det_steps``.
    """
    yield from bps.mv(
        det.hdf1.file_name, fname,
        det.hdf1.file_write_mode, file_write_mode,
        det.hdf1.num_capture, 1,
        det.hdf1.auto_save, "Yes",
        det.hdf1.auto_increment, "Yes",
    )
    if file_write_mode == "Capture":
        yield from bps.mv(det.hdf1.capture, 1)  # open HDF capture (resets num_captured)
    yield from bps.abs_set(det.cam.acquire, 1)  # open cam acquisition
    # Wait for the frame to actually be captured -- NOT for acquire_busy to
    # be idle, which is already 0 before the cam ramps up (would abort the
    # frame instantly).  num_captured was reset to 0 by arming capture.
    if file_write_mode == "Capture":
        yield from _wait_for_captured(det, 1, timeout=exposure_time + 30.0)
    else:
        # Single write mode: let the cam start, then wait for it to finish.
        yield from bps.sleep(min(exposure_time, 1.0))
        yield from _wait_cam_idle(det, timeout=exposure_time + 30.0)
    yield from bps.abs_set(det.cam.acquire, 0)  # close cam acquisition
    yield from _wait_cam_idle(det, timeout=30.0)
    if file_write_mode == "Capture":
        yield from _flush_hdf_file(det, timeout=exposure_time + 30.0)


def _ensure_detector_idle(det, timeout=30.0):
    """Plan stub: make sure the cam and HDF plugin are closed (idempotent).

    Safety net on top of a plan's own cleanup.  Fires ``Acquire=0``
    non-blocking (the Eiger keeps ``Acquire_RBV`` at 1, so a *waited* mv
    would time out), confirms the cam reached idle via ``acquire_busy``,
    and makes sure HDF ``capture`` is off.  Safe to call when already idle.
    """
    try:
        yield from bps.abs_set(det.cam.acquire, 0)
        yield from _wait_cam_idle(det, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"_ensure_detector_idle: cam stop failed: {exc!r}")
    try:
        yield from bps.mv(det.hdf1.capture, 0)
    except Exception as exc:  # noqa: BLE001
        print(f"_ensure_detector_idle: hdf capture stop failed: {exc!r}")


def _fmt_pos(value):
    """Compact, dot-free position token for file names (2 decimal places).

    Renders a coordinate so it is safe to embed in an HDF5 base name (the
    IOC appends ``_NNNNNN.h5``): trailing zeros trimmed and the decimal
    point written as ``p`` so there is no ``.`` before ``.h5``.  The sign
    is kept as ``-`` (the established ``EigPos`` file-name convention keeps
    ``-``).  Examples: ``1.25 -> "1p25"``, ``-0.5 -> "-0p5"``,
    ``2.0 -> "2"``, ``0 -> "0"``.
    """
    s = f"{value:.2f}".rstrip("0").rstrip(".")
    if s in ("", "-0"):
        s = "0"
    return s.replace(".", "p")


@plan
def sweep_xy_integrate_one(
    x_start: float,
    x_end: float,
    y_start: float,
    y_end: float,
    exposure_time: float,
    n_rows: int = 5,
    file_name: str = "sweep1",
    file_path: str = "/home/sector3/s3ida/XRD/2026-2/setup/June17/",
    open_shutter: bool = True,
    file_write_mode: str = "Capture",
):
    """Integrate ONE detector image while rastering the sample in X-Y.

    omega is left fixed.  ``sample_stage.xprime`` (X) sweeps
    back-and-forth across ``[x_start, x_end]`` while ``sample_stage.base_y``
    (Y) steps from ``y_start`` to ``y_end`` over ``n_rows`` rows (a
    serpentine raster) -- all during a SINGLE Eiger exposure of
    ``exposure_time`` seconds.  The detector integrates the diffraction
    from the whole X-Y area into one image, which is then saved to a
    single HDF5 file.  Conceptually like ``omega_fly`` but the sample
    is translated (not rotated) and only one collective frame is recorded.

    The X sweep speed is derived so the raster fills the exposure window::

        v_x = n_rows * |x_end - x_start| / exposure_time

    (Y-step and acceleration overhead make the real raster slightly
    exceed ``exposure_time``; if the last row looks cut off, raise
    ``exposure_time`` or lower ``n_rows``.)

    Parameters
    ----------
    x_start : float [mm]
        X start :: Start of the X (sample_stage.xprime) sweep range.
    x_end : float [mm]
        X end :: End of the X (sample_stage.xprime) sweep range.
    y_start : float [mm]
        Y start :: Start of the Y (sample_stage.base_y) range.
    y_end : float [mm]
        Y end :: End of the Y range, split into n_rows rows.
    exposure_time : float [s]
        Exposure time :: Single-frame integration time; also the target
        raster duration.
    n_rows : int
        Number of Y rows :: Rows in the serpentine raster (>= 1).
    file_name : str
        File name :: Output HDF5 base file name.
    file_path : str
        File path :: IOC-side directory the HDF5 file is written to.
    open_shutter : bool
        Open shutter :: Open shutterc for the exposure, then close it (always,
        even on error).  Needed or the frame integrates with no beam.
    file_write_mode : choice{Capture, Single}
        HDF write mode :: HDF1 plugin file write mode.  Capture arms capture
        and explicitly flushes the file (the mode tested on this Eiger).

    Note
    ----
    This plan drives the detector directly (no Bluesky run / catalog entry
    is opened) -- it produces the HDF5 file only, like a manual capture.

    Example::

        RE(sweep_xy_integrate_one(-1, 1, -1, 1, exposure_time=10))
        RE(sweep_xy_integrate_one(0, 2, 0, 2, exposure_time=30, n_rows=10, file_name="sampleA"))
    """
    if n_rows < 1:
        raise ValueError("n_rows must be >= 1")
    if exposure_time <= 0:
        raise ValueError("exposure_time must be > 0")
    lx = abs(x_end - x_start)
    if lx == 0:
        raise ValueError("x_start and x_end must differ (X is the swept axis).")

    sample_stage = oregistry["sample_stage"]
    eiger2 = oregistry["eiger2"]
    xm = sample_stage.xprime
    ym = sample_stage.base_y

    # sweep velocity derived so the raster fills the exposure window
    v_x = (n_rows * lx) / exposure_time

    # Y row positions (X sweeps serpentine between them)
    if n_rows == 1:
        y_rows = [y_start]
    else:
        dy = (y_end - y_start) / (n_rows - 1)
        y_rows = [y_start + i * dy for i in range(n_rows)]

    v_x_original = xm.velocity.get()  # restored in cleanup

    print(
        f"sweep_xy_integrate_one: raster X[{x_start},{x_end}] x Y[{y_start},{y_end}] in "
        f"{n_rows} rows, v_x={v_x:g} EGU/s, single {exposure_time:g}s frame "
        f"-> {file_name}"
    )

    def _expose_while_rastering():
        if open_shutter:
            yield from bps.mv(oregistry["shutterc"], "open")

        # taxi to the raster start at the current (normal) velocity
        yield from bps.mv(xm, x_start, ym, y_rows[0])
        # set the derived sweep velocity on the X axis
        yield from bps.mv(xm.velocity, v_x)

        # configure the detector for ONE integrated frame
        yield from bps.mv(
            eiger2.cam.image_mode, "Single",
            eiger2.cam.num_images, 1,
            eiger2.cam.acquire_time, exposure_time,
            eiger2.cam.acquire_period, exposure_time,
        )
        # The Eiger leaves Acquire_RBV at "Acquiring" after a frame and
        # only drops acquire_busy promptly with wait_for_plugins=Yes, so
        # the cam reports idle and stops cleanly (mirrors flyscan_3idc).
        if hasattr(eiger2.cam, "wait_for_plugins"):
            yield from bps.mv(eiger2.cam.wait_for_plugins, "Yes")
        # file_template is a char-waveform PV whose RBV reads back oddly
        # (e.g. '%'), so a *waited* set (bps.mv) times out.  Set it
        # fire-and-forget with .put() like configure_adsimdet does.
        eiger2.hdf1.file_template.put("%s%s_%6.6d.h5")
        yield from bps.mv(
            eiger2.hdf1.file_path, file_path,
            eiger2.hdf1.file_name, file_name,
            eiger2.hdf1.file_write_mode, file_write_mode,
            eiger2.hdf1.num_capture, 1,
            eiger2.hdf1.auto_save, "Yes",
            eiger2.hdf1.auto_increment, "Yes",
        )
        if file_write_mode == "Capture":
            yield from bps.mv(eiger2.hdf1.capture, 1)  # arm capture

        # Start the single-frame acquisition WITHOUT waiting, so we can
        # move the sample during the integration.  (Do not wait on the
        # set status: it completes at Acquire_RBV==1, the start of the
        # frame, not its end.)
        yield from bps.abs_set(eiger2.cam.acquire, 1)

        # serpentine raster during the exposure
        forward = True
        for i, yrow in enumerate(y_rows):
            if i > 0:
                yield from bps.mv(ym, yrow)  # step Y to the next row
            target = x_end if forward else x_start
            yield from bps.mv(xm, target)  # sweep X across the range at v_x
            forward = not forward

        # wait for the single frame to finish integrating + read out
        yield from _wait_cam_idle(eiger2, timeout=exposure_time + 30.0)
        # NOTE: the detector is *closed* in _cleanup (stop acquire, stop
        # capture, flush) so it happens on every exit path -- normal
        # completion, an error, or an RE abort/Ctrl-C during the raster.

    def _cleanup():
        # ALWAYS close the detector and restore state.  Each step is
        # best-effort so a failure of one still attempts the rest.  This
        # lives here (not in the body) so an error or Ctrl-C mid-raster
        # still stops the camera and writes the file.

        # 1. Stop the camera acquisition.  Do NOT rely on "Single"
        #    image-mode auto-stopping the Eiger: if the cam was left in a
        #    continuous mode, or the scan aborted mid-exposure, Acquire
        #    would otherwise stay on -- the "detector never closes" bug.
        # The Eiger keeps Acquire_RBV at 1 ("Acquiring") after a frame, so
        # a *waited* mv(acquire, 0) blocks on the readback and times out,
        # leaving the cam shown as acquiring.  Fire the stop non-blocking,
        # then confirm the cam actually reached idle via acquire_busy
        # (which drops promptly with wait_for_plugins=Yes).
        try:
            yield from bps.abs_set(eiger2.cam.acquire, 0)
            yield from _wait_cam_idle(eiger2, timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            print(f"sweep_xy_integrate_one: failed to stop cam.acquire: {exc!r}")

        # 2. Stop HDF capture and flush the file to disk (Capture mode).
        if file_write_mode == "Capture":
            try:
                yield from _flush_hdf_file(eiger2, timeout=exposure_time + 30.0)
            except Exception as exc:  # noqa: BLE001
                print(f"sweep_xy_integrate_one: failed to flush HDF file: {exc!r}")
        # (Single mode: auto_save writes the frame as it arrives.)

        # 3. Restore the X velocity.
        try:
            yield from bps.mv(xm.velocity, v_x_original)
        except Exception as exc:  # noqa: BLE001
            print(f"sweep_xy_integrate_one: failed to restore X velocity: {exc!r}")

        # 4. Close the shutter.
        if open_shutter:
            try:
                yield from bps.mv(oregistry["shutterc"], "close")
            except Exception as exc:  # noqa: BLE001
                print(f"sweep_xy_integrate_one: failed to close shutter: {exc!r}")

    yield from bpp.finalize_wrapper(_expose_while_rastering(), _cleanup())


@plan
def sweep_xy_integrate_steps(
    x_start: float,
    x_end: float,
    y_start: float,
    y_end: float,
    total_time: float,
    frame_period: float,
    frame_exposure: float = None,
    n_rows: int = 5,
    file_name: str = "sweepN",
    file_path: str = "/home/sector3/s3ida/XRD/2026-2/setup/June17/",
    open_shutter: bool = True,
    file_write_mode: str = "Capture",
):
    """Raster the sample in X-Y while saving SEVERAL frames along the path.

    Like ``sweep_xy_integrate_one`` (omega fixed; ``sample_stage.xprime`` (X) sweeps
    serpentine across ``[x_start, x_end]`` while ``sample_stage.base_y``
    (Y) steps over ``n_rows`` rows), **except** instead of integrating the
    whole raster into one image, the detector free-runs at a fixed
    ``frame_period`` so it records a *series* of frames evenly spaced in
    time -- i.e. at regular positions along the continuous raster path.
    All frames land in a single HDF5 (Capture-mode) file.

    Frame count is derived from the requested timing::

        n_frames       = round(total_time / frame_period)   # >= 1
        effective_total = n_frames * frame_period            # actual sweep duration
        v_x            = n_rows * |x_end - x_start| / effective_total

    ``effective_total`` (and thus the X sweep velocity) is snapped to a
    whole number of frames so the motion and the acquisition span the same
    window -- frame ``i`` is exposed during ``[i*frame_period,
    (i+1)*frame_period]`` and so corresponds to a known sub-segment of the
    serpentine path.  Each frame integrates ``frame_exposure`` seconds
    (<= ``frame_period``; ``None`` => ``frame_period``, continuous).

    Parameters
    ----------
    x_start : float [mm]
        X start :: Start of the X (sample_stage.xprime) sweep range.
    x_end : float [mm]
        X end :: End of the X (sample_stage.xprime) sweep range.
    y_start : float [mm]
        Y start :: Start of the Y (sample_stage.base_y) range.
    y_end : float [mm]
        Y end :: End of the Y range, split into n_rows rows.
    total_time : float [s]
        Total raster time :: Target total duration of the whole raster;
        snapped to a whole number of frame_period frames.
    frame_period : float [s]
        Frame period :: Time between successive saved frames; sets how finely
        the path is sampled (n_frames = round(total_time / frame_period)).
    frame_exposure : float [s]
        Frame exposure :: Per-frame integration time (<= frame period); blank
        uses the frame period.
    n_rows : int
        Number of Y rows :: Rows in the serpentine raster (>= 1).
    file_name : str
        File name :: Output HDF5 base file name.
    file_path : str
        File path :: IOC-side directory the HDF5 file is written to.
    open_shutter : bool
        Open shutter :: Open shutterc for the sweep, then close it (always,
        even on error).  Needed or the frames integrate with no beam.
    file_write_mode : choice{Capture, Single}
        HDF write mode :: HDF1 plugin file write mode.  Capture arms capture
        and explicitly flushes the file (the mode tested on this Eiger).

    Note
    ----
    Like ``sweep_xy_integrate_one``, this drives the detector directly (no Bluesky run /
    catalog entry is opened) -- it produces one HDF5 file with ``n_frames``
    images.  Frame<->position mapping is implicit in the path geometry and
    even time spacing (this plan does NOT do the timestamp-based pairing
    that ``flyscan`` does); the first frame may carry a little extra
    latency before motion is fully underway.

    Example::

        # 20 s sweep, one frame every 2 s -> 10 frames along the path
        RE(sweep_xy_integrate_steps(-1, 1, -1, 1, total_time=20, frame_period=2))
        RE(sweep_xy_integrate_steps(0, 2, 0, 2, total_time=60, frame_period=1,
                           n_rows=10, file_name="sampleA"))
    """
    if n_rows < 1:
        raise ValueError("n_rows must be >= 1")
    if total_time <= 0:
        raise ValueError("total_time must be > 0")
    if frame_period <= 0:
        raise ValueError("frame_period must be > 0")
    if frame_exposure is None:
        frame_exposure = frame_period
    if not (0 < frame_exposure <= frame_period):
        raise ValueError("frame_exposure must satisfy 0 < frame_exposure <= frame_period")
    lx = abs(x_end - x_start)
    if lx == 0:
        raise ValueError("x_start and x_end must differ (X is the swept axis).")

    sample_stage = oregistry["sample_stage"]
    eiger2 = oregistry["eiger2"]
    xm = sample_stage.xprime
    ym = sample_stage.base_y

    # snap to a whole number of frames so motion + acquisition co-span
    n_frames = max(1, round(total_time / frame_period))
    effective_total = n_frames * frame_period

    # sweep velocity derived so the raster fills the (snapped) window
    v_x = (n_rows * lx) / effective_total

    # Y row positions (X sweeps serpentine between them)
    if n_rows == 1:
        y_rows = [y_start]
    else:
        dy = (y_end - y_start) / (n_rows - 1)
        y_rows = [y_start + i * dy for i in range(n_rows)]

    v_x_original = xm.velocity.get()  # restored in cleanup

    print(
        f"sweep_xy_integrate_steps: raster X[{x_start},{x_end}] x Y[{y_start},{y_end}] in "
        f"{n_rows} rows, {n_frames} frame(s) @ {frame_period:g}s period "
        f"(exposure {frame_exposure:g}s), total~{effective_total:g}s, "
        f"v_x={v_x:g} EGU/s -> {file_name}"
    )

    def _expose_while_rastering():
        if open_shutter:
            yield from bps.mv(oregistry["shutterc"], "open")

        # taxi to the raster start at the current (normal) velocity
        yield from bps.mv(xm, x_start, ym, y_rows[0])
        # set the derived sweep velocity on the X axis
        yield from bps.mv(xm.velocity, v_x)

        # configure the detector for a SERIES of n_frames frames
        yield from bps.mv(
            eiger2.cam.image_mode, "Multiple",
            eiger2.cam.num_images, n_frames,
            eiger2.cam.acquire_time, frame_exposure,
            eiger2.cam.acquire_period, frame_period,
        )
        # The Eiger leaves Acquire_RBV at "Acquiring" after frames and
        # only drops acquire_busy promptly with wait_for_plugins=Yes, so
        # the cam reports idle and stops cleanly (mirrors flyscan_3idc).
        if hasattr(eiger2.cam, "wait_for_plugins"):
            yield from bps.mv(eiger2.cam.wait_for_plugins, "Yes")
        # file_template is a char-waveform PV whose RBV reads back oddly
        # (e.g. '%'), so a *waited* set (bps.mv) times out.  Set it
        # fire-and-forget with .put() like configure_adsimdet does.
        eiger2.hdf1.file_template.put("%s%s_%6.6d.h5")
        yield from bps.mv(
            eiger2.hdf1.file_path, file_path,
            eiger2.hdf1.file_name, file_name,
            eiger2.hdf1.file_write_mode, file_write_mode,
            eiger2.hdf1.num_capture, n_frames,
            eiger2.hdf1.auto_save, "Yes",
            eiger2.hdf1.auto_increment, "Yes",
        )
        if file_write_mode == "Capture":
            yield from bps.mv(eiger2.hdf1.capture, 1)  # arm capture

        # Start the multi-frame acquisition WITHOUT waiting, so we can
        # move the sample while the frames are recorded.  (Do not wait on
        # the set status: it completes at Acquire_RBV==1, the start of the
        # series, not its end.)
        yield from bps.abs_set(eiger2.cam.acquire, 1)

        # serpentine raster spanning the same window as the acquisition
        forward = True
        for i, yrow in enumerate(y_rows):
            if i > 0:
                yield from bps.mv(ym, yrow)  # step Y to the next row
            target = x_end if forward else x_start
            yield from bps.mv(xm, target)  # sweep X across the range at v_x
            forward = not forward

        # wait for all n_frames to finish integrating + read out
        yield from _wait_cam_idle(eiger2, timeout=effective_total + 30.0)
        # NOTE: the detector is *closed* in _cleanup (stop acquire, stop
        # capture, flush) so it happens on every exit path -- normal
        # completion, an error, or an RE abort/Ctrl-C during the raster.

    def _cleanup():
        # ALWAYS close the detector and restore state.  Each step is
        # best-effort so a failure of one still attempts the rest.  This
        # lives here (not in the body) so an error or Ctrl-C mid-raster
        # still stops the camera and writes the file.

        # 1. Stop the camera acquisition.  The Eiger keeps Acquire_RBV at 1
        # ("Acquiring") after frames, so a *waited* mv(acquire, 0) blocks on
        # the readback and times out, leaving the cam shown as acquiring.
        # Fire the stop non-blocking, then confirm the cam actually reached
        # idle via acquire_busy (drops promptly with wait_for_plugins=Yes).
        try:
            yield from bps.abs_set(eiger2.cam.acquire, 0)
            yield from _wait_cam_idle(eiger2, timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            print(f"sweep_xy_integrate_steps: failed to stop cam.acquire: {exc!r}")

        # 2. Stop HDF capture and flush the file to disk (Capture mode).
        if file_write_mode == "Capture":
            try:
                yield from _flush_hdf_file(eiger2, timeout=effective_total + 30.0)
            except Exception as exc:  # noqa: BLE001
                print(f"sweep_xy_integrate_steps: failed to flush HDF file: {exc!r}")
        # (Single mode: auto_save writes each frame as it arrives.)

        # 3. Restore the X velocity.
        try:
            yield from bps.mv(xm.velocity, v_x_original)
        except Exception as exc:  # noqa: BLE001
            print(f"sweep_xy_integrate_steps: failed to restore X velocity: {exc!r}")

        # 4. Close the shutter.
        if open_shutter:
            try:
                yield from bps.mv(oregistry["shutterc"], "close")
            except Exception as exc:  # noqa: BLE001
                print(f"sweep_xy_integrate_steps: failed to close shutter: {exc!r}")

    yield from bpp.finalize_wrapper(_expose_while_rastering(), _cleanup())


@plan
def omega_fly(
    file_name: str = "omeFly",
    file_path: str = "/home/sector3/s3ida/XRD/2026-2/setup/June17/",
    p_start: float = -5,
    p_end: float = 5,
    exposures_per_egu: float = 2,  # 2 frames/deg -> 21 frames over 10 deg
    t_period: float = 1.0,  # 1 s/frame; with 2 frames/deg => 1 s/degree
    t_acquire: float = None,  # exposure per frame (<= t_period); None => t_period
    md: dict = None,
):
    """Continuous fly scan of ``sample_stage.omega`` with the Eiger2.

    Rotates omega from ``p_start`` to ``p_end`` while ``eiger2`` acquires
    continuously, saving one HDF5 (Capture-mode) file in ``file_path``.

    omega is interlocked against ``laser_optics`` (it will not move
    unless the optics are OUT), so this plan retracts the laser optics
    first.

    Defaults reproduce: omega -45 -> +45 deg, 900 steps, ~10 s/degree.

    Parameters
    ----------
    file_name : str
        File name :: Output HDF5 base file name.
    file_path : str
        File path :: IOC-side directory the HDF5 file is written to.
    p_start : float [deg]
        omega start :: Omega angle at which the continuous rotation begins.
    p_end : float [deg]
        omega end :: Omega angle at which the rotation ends.
    exposures_per_egu : float [1/deg]
        Exposures per degree :: Frames acquired per degree of omega travel.
    t_period : float [s]
        Frame period :: Time between successive frames.
    t_acquire : float [s]
        Exposure per frame :: Per-frame exposure (<= frame period); blank uses
        the frame period.

    Example::

        RE(omega_fly())
        RE(omega_fly(file_name="sampleA", p_start=0, p_end=180))
    """
    laser_optics = oregistry["laser_optics"]

    # Clear the interlock: omega is blocked unless the laser optics
    # are OUT.  See ../../devices/omega_laser_interlock.py.
    yield from laser_optics.move_out()

    yield from flyscan(
        det_name="eiger2",
        flymotor_name="sample_stage_omega",
        p_start=p_start,
        p_end=p_end,
        exposures_per_egu=exposures_per_egu,
        t_period=t_period,
        t_acquire=t_acquire,
        ad_file_name=file_name,
        ad_file_path=file_path,
        plan_name="omega_fly",
        md=md,
    )


@plan
def omega_fly_at_det_steps(
    positions,
    file_name: str = "omeFly_det",
    file_path: str = "/home/sector3/s3ida/XRD/2026-2/setup/June17/",
    p_start: float = -5,
    p_end: float = 5,
    exposures_per_egu: float = 2,
    t_period: float = 1.0,
    t_acquire: float = None,
    start_index: int = 1,
    md: dict = None,
):
    """Run ``omega_fly`` once at each detector ``(det_x, eiger_y, eiger_z)``.

    ``positions`` is a list of ``(det_x, eiger_y, eiger_z)`` triples --
    always in that fixed axis order.  For each triple (in order): move
    ``detector_stage`` ``det_x``, ``eiger_y`` and ``eiger_z`` together,
    then run ``omega_fly`` with identical scan settings.

    File naming uses the **actual detector positions read back from the
    motor PVs after the move** (not the requested values), each rounded
    to the nearest integer::

        <file_name>_<X>_<Y>_<Z>_000001

    where ``<X>``/``<Y>``/``<Z>`` are the rounded integer readbacks of
    ``det_x`` / ``eiger_y`` / ``eiger_z``, and ``_000001`` is the IOC's
    trailing counter (constant, since the flyscan resets it each run).

    The series index plus the requested and actual positions of all three
    axes are recorded in each run's metadata, and the full-precision actual
    positions are also written into the produced HDF5 file under
    ``/entry/instrument/detector_stage`` (best-effort).

    Parameters
    ----------
    positions : positions [mm]
        Detector positions :: One (det_x, eiger_y, eiger_z) triple per line;
        omega_fly runs once at each, in order.
    file_name : str
        File name :: Output HDF5 base file name (per-position suffix appended).
    file_path : str
        File path :: IOC-side directory the HDF5 files are written to.
    p_start : float [deg]
        omega start :: Omega angle at which each rotation begins.
    p_end : float [deg]
        omega end :: Omega angle at which each rotation ends.
    exposures_per_egu : float [1/deg]
        Exposures per degree :: Frames acquired per degree of omega travel.
    t_period : float [s]
        Frame period :: Time between successive frames.
    t_acquire : float [s]
        Exposure per frame :: Per-frame exposure (<= frame period); blank uses
        the frame period.
    start_index : int
        Start index :: First value of the per-run series index recorded in metadata.

    Example::

        RE(omega_fly_at_det_steps([(100, 0, 50), (150, 0, 50), (200, 0, 50)]))
        RE(omega_fly_at_det_steps([(0, 0, 0), (5, 0, 10)], file_name="sampleA",
                                    p_start=-45, p_end=45))
    """
    detector_stage = oregistry["detector_stage"]
    for axis in ("det_x", "eiger_y", "eiger_z"):
        if not hasattr(detector_stage, axis):
            raise ValueError(
                f"detector_stage has no axis {axis!r}; expected "
                "'det_x', 'eiger_y', 'eiger_z'."
            )
    x_motor = detector_stage.det_x
    y_motor = detector_stage.eiger_y
    z_motor = detector_stage.eiger_z
    eiger2 = oregistry["eiger2"]
    positions = list(positions)

    def _series():
        for offset, pos in enumerate(positions):
            try:
                x, y, z = pos
            except (TypeError, ValueError):
                raise ValueError(
                    f"positions[{offset}]={pos!r} is not a "
                    "(det_x, eiger_y, eiger_z) triple."
                ) from None
            index = start_index + offset

            # 1. move all three detector axes together (blocking)
            yield from bps.mv(x_motor, x, y_motor, y, z_motor, z)

            # 2. read the ACTUAL positions back from the motor PVs; the
            #    file name (and HDF annotation) reflect where the detector
            #    really is, not the requested target.
            rx = yield from bps.rd(x_motor)
            ry = yield from bps.rd(y_motor)
            rz = yield from bps.rd(z_motor)

            # 3. unique per-position file name: counter + EigPos suffix
            #    built from the rounded-to-integer actual readbacks.
            fname = f"{file_name}_{round(rx)}_{round(ry)}_{round(rz)}"
            print(
                f"omega_fly_at_det_steps: [{offset + 1}/{len(positions)}] "
                f"requested det_x={x}, eiger_y={y}, eiger_z={z}; "
                f"actual=({rx:g}, {ry:g}, {rz:g}) -> {fname}"
            )

            # 4. record the detector context alongside any user metadata
            run_md = dict(md or {})
            run_md.update(
                {
                    "series_index": index,
                    "det_x_position_requested": x,
                    "eiger_y_position_requested": y,
                    "eiger_z_position_requested": z,
                    "det_x_position_actual": rx,
                    "eiger_y_position_actual": ry,
                    "eiger_z_position_actual": rz,
                }
            )

            # 5. repeat omega_fly with identical scan settings
            yield from omega_fly(
                file_name=fname,
                file_path=file_path,
                p_start=p_start,
                p_end=p_end,
                exposures_per_egu=exposures_per_egu,
                t_period=t_period,
                t_acquire=t_acquire,
                md=run_md,
            )

            # 6. annotate the just-written HDF5 file with the detector
            #    positions (full precision).  Best-effort; never fatal.
            _record_det_positions_in_hdf(
                eiger2,
                {"det_x": rx, "eiger_y": ry, "eiger_z": rz},
                requested={"det_x": x, "eiger_y": y, "eiger_z": z},
            )

    def _close():
        # Safety net: each omega_fly -> flyscan already closes the
        # detector in its own cleanup (stop cam.acquire, stop hdf1.capture,
        # drain + flush).  This guarantees the cam + HDF plugin are left
        # idle once the whole series ends -- or is aborted partway through.
        yield from _ensure_detector_idle(eiger2)

    yield from bpp.finalize_wrapper(_series(), _close())


@plan
def omega_fly_at_sam_steps(
    x_start: float,
    x_end: float,
    n_x: int,
    y_start: float,
    y_end: float,
    n_y: int,
    file_name: str = "omeFly_sam",
    file_path: str = "/home/sector3/s3ida/XRD/2026-2/setup/June17/",
    p_start: float = -5,
    p_end: float = 5,
    exposures_per_egu: float = 2,
    t_period: float = 1.0,
    t_acquire: float = None,
    start_index: int = 1,
    md: dict = None,
):
    """Run an ``omega_fly`` at each sample (samX, samY) grid position.

    Rasters the *sample* over a rectangular grid of ``n_x`` x ``n_y``
    positions -- ``samX`` is ``sample_stage.xprime`` and ``samY`` is
    ``sample_stage.base_y`` (the same axes ``sweep_xy_integrate_one``/``sweep_xy_integrate_steps``
    translate) -- and at every grid point runs a full ``omega_fly``
    (continuous omega fly with the Eiger2, one HDF5 file per point).

    Grid traversal (NOT serpentine).  Rows are visited from ``y_start`` to
    ``y_end``; within **every** row ``samX`` always runs from the negative
    (lower) X to the positive (higher) X.  When ``samY`` steps to the next
    row, ``samX`` resets back to the negative end -- so the X sweep
    direction is identical for every row::

        samY = y_rows[0]:  x_lo -> ... -> x_hi
        samY = y_rows[1]:  x_lo -> ... -> x_hi      (samX restarts at x_lo)
        ...

    (``x_start``/``x_end`` may be passed in either order; the samX columns
    are always ordered low->high so the sweep goes negative->positive.)

    File naming.  Each point's HDF5 base name carries that point's
    *requested* sample coordinates::

        <file_name>_<x>_<y>_000001

    where ``<x>``/``<y>`` are the commanded samX/samY grid values rounded
    to 2 decimals (decimal point written as ``p``, e.g. ``-0p5_1p25``) and
    ``_000001`` is the IOC's trailing counter (constant per run), e.g.
    ``omeFly_sam_-1_0p5_000001``.

    Parameters
    ----------
    x_start : float [mm]
        samX start :: samX (sample_stage.xprime) grid limit; ordered
        internally so the sweep is always low->high.
    x_end : float [mm]
        samX end :: samX (sample_stage.xprime) grid limit; ordered internally
        so the sweep is always low->high.
    n_x : int
        Number of samX columns :: Number of samX grid columns (>= 1).
    y_start : float [mm]
        samY start :: samY (sample_stage.base_y) grid limit; rows are visited
        in the order given (y_start -> y_end).
    y_end : float [mm]
        samY end :: samY (sample_stage.base_y) grid limit; rows are visited in
        the order given (y_start -> y_end).
    n_y : int
        Number of samY rows :: Number of samY grid rows (>= 1).
    file_name : str
        File name :: Output HDF5 base name; a _<x>_<y> position suffix is
        appended per point.
    file_path : str
        File path :: IOC-side directory the HDF5 files are written to.
    p_start : float [deg]
        omega start :: Omega angle at which each rotation begins.
    p_end : float [deg]
        omega end :: Omega angle at which each rotation ends.
    exposures_per_egu : float [1/deg]
        Exposures per degree :: Frames acquired per degree of omega travel.
    t_period : float [s]
        Frame period :: Time between successive frames.
    t_acquire : float [s]
        Exposure per frame :: Per-frame exposure (<= frame period); blank uses
        the frame period.
    start_index : int
        Start index :: First value of the per-point series index recorded in metadata.

    Note
    ----
    Each ``omega_fly`` retracts ``laser_optics`` (the omega interlock)
    and opens its own Bluesky run + HDF5 file, exactly as a standalone
    ``omega_fly`` would.  The detector is forced idle once the whole
    series finishes -- or is aborted partway through -- best-effort.

    Example::

        # 3 samX columns x 2 samY rows = 6 omega flyscans
        RE(omega_fly_at_sam_steps(-1, 1, 3, 0, 0.5, 2))
        RE(omega_fly_at_sam_steps(-2, 2, 5, -1, 1, 3, file_name="sampleA",
                              p_start=-45, p_end=45))
    """
    if n_x < 1 or n_y < 1:
        raise ValueError("n_x and n_y must both be >= 1")

    sample_stage = oregistry["sample_stage"]
    eiger2 = oregistry["eiger2"]
    xm = sample_stage.xprime
    ym = sample_stage.base_y

    # samX columns: ALWAYS ordered low -> high so every row sweeps
    # negative -> positive, regardless of the order x_start/x_end were
    # passed in.
    x_lo, x_hi = sorted((x_start, x_end))
    if n_x == 1:
        x_cols = [x_lo]
    else:
        dx = (x_hi - x_lo) / (n_x - 1)
        x_cols = [x_lo + i * dx for i in range(n_x)]

    # samY rows: visited in the order given (y_start -> y_end).
    if n_y == 1:
        y_rows = [y_start]
    else:
        dy = (y_end - y_start) / (n_y - 1)
        y_rows = [y_start + i * dy for i in range(n_y)]

    n_points = n_x * n_y
    print(
        f"omega_fly_at_sam_steps: {n_x}x{n_y}={n_points} sample positions "
        f"(samX {x_lo:g}->{x_hi:g} ascending every row, samY {y_start:g}->"
        f"{y_end:g}); omega flyscan {p_start:g}->{p_end:g} deg at each "
        f"-> {file_name}"
    )

    def _series():
        offset = 0
        for yval in y_rows:
            for xval in x_cols:  # always x_lo -> x_hi (negative -> positive)
                index = start_index + offset
                offset += 1

                # 1. move the sample to this grid point (blocking)
                yield from bps.mv(xm, xval, ym, yval)

                # 2. read the actual sample positions back (metadata /
                #    provenance only; the file name uses the REQUESTED grid
                #    values, per the naming spec).
                rx = yield from bps.rd(xm)
                ry = yield from bps.rd(ym)

                # 3. per-point file name: requested coords + point number.
                fname = f"{file_name}_{_fmt_pos(xval)}_{_fmt_pos(yval)}"
                print(
                    f"omega_fly_at_sam_steps: [{offset}/{n_points}] "
                    f"samX={xval:g}, samY={yval:g} "
                    f"(actual {rx:g}, {ry:g}) -> {fname}"
                )

                # 4. record the sample context alongside any user metadata
                run_md = dict(md or {})
                run_md.update(
                    {
                        "series_index": index,
                        "samX_position_requested": xval,
                        "samY_position_requested": yval,
                        "samX_position_actual": rx,
                        "samY_position_actual": ry,
                    }
                )

                # 5. omega flyscan at this sample position, identical scan
                #    settings every point.  omega_fly retracts the laser
                #    optics (omega interlock) and writes its own HDF5 file.
                yield from omega_fly(
                    file_name=fname,
                    file_path=file_path,
                    p_start=p_start,
                    p_end=p_end,
                    exposures_per_egu=exposures_per_egu,
                    t_period=t_period,
                    t_acquire=t_acquire,
                    md=run_md,
                )

    def _close():
        # Safety net: each omega_fly -> flyscan already closes the
        # detector in its own cleanup.  This guarantees the cam + HDF
        # plugin are left idle once the whole series ends -- or is aborted
        # partway through.
        yield from _ensure_detector_idle(eiger2)

    yield from bpp.finalize_wrapper(_series(), _close())


@plan
def fixed_exp_at_det_steps(
    scan_axis: str = "det_x",
    start: float = -25,
    end: float = 100,
    spacing: float = 5,
    exposure_time: float = 2.0,
    det_x: float = None,
    eiger_y: float = None,
    eiger_z: float = None,
    file_name: str = "fixExp_det",
    file_path: str = "/home/sector3/s3ida/XRD/2026-2/setup/June17/",
    open_shutter: bool = True,
    file_write_mode: str = "Capture",
    md: dict = None,
):
    """Step scan of ONE detector_stage axis, built on ``bp.list_scan``.

    Uses ``list_scan`` as the engine -- it opens a Bluesky **run**
    (documents + Tiled catalog entry) over the list of positions -- but a
    custom ``per_step`` drives the detector at each point so every position
    gets its **own** HDF5 file named by *that point's* detector geometry as
    ``<file_name>_<X>_<Y>_<Z>_NNNNNN`` (X=det_x, Y=eiger_y, Z=eiger_z,
    rounded to int; ``_NNNNNN`` is the IOC counter), e.g.
    ``fixExp_det_-25_7_30_000001``.  The
    detector is therefore NOT passed to ``list_scan`` as a staged detector
    (``detectors=[]``); instead the per-step opens the cam + HDF plugin,
    exposes one frame, writes the file, and closes them -- and records the
    three axis readbacks (and the data file name) into the run's ``primary``
    stream, so positions are also in the catalog.

    Moves ``detector_stage.<scan_axis>`` from ``start`` to ``end`` in
    ``spacing``-sized steps (inclusive of ``end`` when divisible; never
    overshoots); the other two axes are held at the values you pass.

    Parameters
    ----------
    scan_axis : choice{det_x, eiger_y, eiger_z}
        Scan axis :: Which detector_stage axis to step.
    start : float [mm]
        Start :: First position of scan_axis.
    end : float [mm]
        End :: Last position of scan_axis (inclusive when divisible).
    spacing : float [mm]
        Step size :: Step between successive positions (> 0).
    exposure_time : float [s]
        Exposure time :: Per-point exposure; sets eiger2.cam.acquire_time.
    det_x : float [mm]
        det_x fixed :: Fixed det_x for the non-scanned axis; leave blank when
        det_x is the scan axis.
    eiger_y : float [mm]
        eiger_y fixed :: Fixed eiger_y for the non-scanned axis; leave blank
        when eiger_y is the scan axis.
    eiger_z : float [mm]
        eiger_z fixed :: Fixed eiger_z for the non-scanned axis; leave blank
        when eiger_z is the scan axis.
    file_name : str
        File name :: Output HDF5 base name; a _<x>_<y>_<z> position suffix is
        appended per point.
    file_path : str
        File path :: IOC-side directory the HDF5 files are written to.
    open_shutter : bool
        Open shutter :: Open shutterc for the whole scan, then close it (held
        open across points; always closed, even on error).
    file_write_mode : choice{Capture, Single}
        HDF write mode :: Per-point HDF1 plugin file write mode.

    Example::

        RE(fixed_exp_at_det_steps("det_x", -25, 100, 5, exposure_time=2,
                         eiger_y=7, eiger_z=30))
    """
    AXES = ("det_x", "eiger_y", "eiger_z")
    if scan_axis not in AXES:
        raise ValueError(f"scan_axis must be one of {AXES}, got {scan_axis!r}")
    if spacing <= 0:
        raise ValueError("spacing must be > 0")
    if start == end:
        raise ValueError("start and end must differ")
    if exposure_time <= 0:
        raise ValueError("exposure_time must be > 0")

    fixed_values = {"det_x": det_x, "eiger_y": eiger_y, "eiger_z": eiger_z}
    fixed_axes = [a for a in AXES if a != scan_axis]
    for a in fixed_axes:
        if fixed_values[a] is None:
            raise ValueError(
                f"provide a fixed value for {a!r}; the two non-scanned axes "
                "need fixed positions."
            )

    detector_stage = oregistry["detector_stage"]
    eiger2 = oregistry["eiger2"]
    for a in AXES:
        if not hasattr(detector_stage, a):
            raise ValueError(f"detector_stage has no axis {a!r}")
    motors = {a: getattr(detector_stage, a) for a in AXES}
    scan_motor = motors[scan_axis]

    # inclusive list of scan positions; never overshoot `end`
    direction = 1.0 if end >= start else -1.0
    positions = []
    i = 0
    while True:
        p = start + direction * spacing * i
        if (direction > 0 and p > end + 1e-9) or (direction < 0 and p < end - 1e-9):
            break
        positions.append(p)
        i += 1

    state = {"count": 0}  # per-point counter for the file-name prefix

    def per_step(detectors, step, pos_cache):
        """Custom list_scan step: move, write this point's own EigPos file."""
        # move the scan motor to this point (list_scan supplies {motor: pos})
        yield from bps.move_per_step(step, pos_cache)

        rx = yield from bps.rd(motors["det_x"])
        ry = yield from bps.rd(motors["eiger_y"])
        rz = yield from bps.rd(motors["eiger_z"])
        state["count"] += 1
        fname = f"{file_name}_{round(rx)}_{round(ry)}_{round(rz)}"

        # open cam+HDF, expose one frame, write THIS point's file, close
        yield from _acquire_single_file(eiger2, fname, file_write_mode, exposure_time)

        # record the positions (+ data file name) into the run's primary stream
        yield from bps.create("primary")
        for a in AXES:
            yield from bps.read(motors[a])
        yield from bps.read(eiger2.hdf1.full_file_name)
        yield from bps.save()
        print(
            f"fixed_exp_at_det_steps: [{state['count']}/{len(positions)}] "
            f"{scan_axis}={step[scan_motor]:g} -> {fname}"
        )

    run_md = dict(md or {})
    run_md.setdefault("plan_name", "fixed_exp_at_det_steps")
    run_md.update(
        {
            "scan_axis": scan_axis,
            "scan_start": start,
            "scan_end": end,
            "scan_spacing": spacing,
            "exposure_time": exposure_time,
        }
    )
    for a in fixed_axes:
        run_md[f"{a}_fixed"] = fixed_values[a]

    print(
        f"fixed_exp_at_det_steps: {scan_axis} {start}->{end} step {spacing} "
        f"({len(positions)} pts, {exposure_time:g}s each, one file/point); fixed "
        + ", ".join(f"{a}={fixed_values[a]}" for a in fixed_axes)
    )

    def _body():
        # 1. position the fixed axes; only the detector moves during the scan
        move_args = []
        for a in fixed_axes:
            move_args += [motors[a], fixed_values[a]]
        yield from bps.mv(*move_args)

        # 2. open the shutter for the whole scan
        if open_shutter:
            yield from bps.mv(oregistry["shutterc"], "open")

        # 3. configure the cam (per-point file name is set in _acquire_single_file)
        yield from bps.mv(
            eiger2.cam.image_mode, "Single",
            eiger2.cam.num_images, 1,
            eiger2.cam.acquire_time, exposure_time,
            eiger2.cam.acquire_period, exposure_time,
        )
        if hasattr(eiger2.cam, "wait_for_plugins"):
            yield from bps.mv(eiger2.cam.wait_for_plugins, "Yes")
        # file_template's RBV reads back oddly ('%'), so a waited set times
        # out; set it fire-and-forget with .put() (see file_template note).
        eiger2.hdf1.file_template.put("%s%s_%6.6d.h5")
        yield from bps.mv(eiger2.hdf1.file_path, file_path)

        # 4. list_scan as the engine; detectors=[] (we drive eiger2 in per_step)
        return (
            yield from bp.list_scan(
                [], scan_motor, positions, per_step=per_step, md=run_md
            )
        )

    def _close():
        # best-effort detector close + shutter close on every exit path
        try:
            yield from bps.abs_set(eiger2.cam.acquire, 0)
            yield from _wait_cam_idle(eiger2, timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            print(f"fixed_exp_at_det_steps: failed to stop cam.acquire: {exc!r}")
        try:
            yield from bps.mv(eiger2.hdf1.capture, 0)
        except Exception as exc:  # noqa: BLE001
            print(f"fixed_exp_at_det_steps: failed to stop hdf capture: {exc!r}")
        if open_shutter:
            try:
                yield from bps.mv(oregistry["shutterc"], "close")
            except Exception as exc:  # noqa: BLE001
                print(f"fixed_exp_at_det_steps: failed to close shutter: {exc!r}")

    return (yield from bpp.finalize_wrapper(_body(), _close()))
