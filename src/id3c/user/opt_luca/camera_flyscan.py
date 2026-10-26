"""3-ID-C alignment camera -- omega fly scan saving TIFF frames (OPT_Luca).

``camera_flyscan`` rotates ``sample_stage.omega`` at constant velocity while
the alignment camera (``pg3_camera``) free-runs, one TIFF file per frame, and
records enough to assign an omega value to every frame.

Software-correlated, like ``id3c.plans.flyscan_3idc`` (no hardware trigger or
gate): the plan monitors ``omega`` readback and the camera's frame counter,
both IOC-timestamped by EPICS.  Two outputs tie frames to omega:

* the Bluesky run holds both as monitor streams (``sample_stage_omega_...``
  readback and ``pg3_camera_cam_array_counter``), so the pairing can always
  be recomputed from the catalog; and
* ``<file_name>_omega_table.csv`` -- one row per frame: TIFF file name,
  arrival time, omega at exposure start / middle / end, and an ``in_scan``
  flag (frame middle inside ``[p_start, p_end]``).  Built by
  :func:`build_frame_table`.

The TIFF file number is reset to 0 before the scan, so frame ``i`` (0-based,
camera ``ArrayCounter`` = ``i + 1``) is file number ``i``.  If the TIFF plugin
drops frames the file count falls short of the frame count; the plan logs a
warning when that happens (the CSV rows are then offset from the files).

Why not ``flyscan_3idc.flyscan``?  It is built around the HDF1 plugin
(Eiger2), and this camera writes TIFF.  Geometry/helpers are reused from it.

Run through the RunEngine like any other plan::

    from id3c.user.opt_luca.camera_flyscan import camera_flyscan
    RE(camera_flyscan("/home/sector3/s3ida/pg3/", "scanA", 0, 10, 1, 0.5))

GUI-parseable docstrings: same fixed NumPy ``Parameters`` grammar as
``setup_june_26.py`` -- see that file's module docstring for the full
grammar spec; not repeated here.
"""

import csv
import logging
import math
import os

import numpy as np
from apsbits.core.instrument_init import oregistry
from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from bluesky.utils import plan

from id3c.plans.flyscan_3idc import compute_flyscan_geometry
from id3c.plans.flyscan_3idc import motor_is_moving
from id3c.plans.flyscan_3idc import read_motor_field
from id3c.user.s3idc_plans.setup_june_26 import _wait_cam_idle

logger = logging.getLogger(__name__)

_DEFAULT_TIFF_TEMPLATE = "%s%s_%3.3d.tif"
"""AreaDetector's default TIFF template (path, name, number)."""

_TABLE_FIELDS = (
    "frame_index",
    "file_name",
    "t_arrival",
    "t_measured",
    "omega_start",
    "omega_mid",
    "omega_end",
    "in_scan",
)


def build_frame_table(
    frame_counts,
    frame_times,
    omega_times,
    omega_values,
    *,
    t_acquire,
    p_start,
    p_end,
    file_name,
    t_latency=0.0,
    file_template=_DEFAULT_TIFF_TEMPLATE,
    first_file_number=0,
):
    """Assign an omega value to every camera frame (pure function).

    Parameters
    ----------
    frame_counts, frame_times : sequence
        Camera ``ArrayCounter`` values seen by the monitor and the IOC
        timestamp of each (seconds).  Counts the monitor missed (CA can
        coalesce updates) get an interpolated time, flagged
        ``t_measured=False``.
    omega_times, omega_values : sequence
        IOC timestamps and omega readback values from the omega monitor.
    t_acquire : float
        Exposure time [s].  Frame exposure runs from
        ``t_arrival - t_latency - t_acquire`` to ``t_arrival - t_latency``.
    p_start, p_end : float
        In-scan omega window; ``in_scan`` is True when the frame's middle
        omega lies inside it.
    file_name : str
        TIFF file-name prefix (``tiff1.file_name``).
    t_latency : float
        Delay between end of exposure and the counter timestamp [s]
        (readout + transfer); an instrument calibration, default 0.
    file_template : str
        ``tiff1.file_template`` (``% (path, name, number)``).
    first_file_number : int
        TIFF file number of the first frame (plan resets it to 0).

    Returns
    -------
    list of dict
        One row per frame ``1..max(frame_counts)``, keys as in
        ``_TABLE_FIELDS``.  Omega is NaN for frames outside the time span
        covered by the omega samples.
    """
    counts = np.asarray(frame_counts, dtype=float)
    f_times = np.asarray(frame_times, dtype=float)
    o_times = np.asarray(omega_times, dtype=float)
    o_vals = np.asarray(omega_values, dtype=float)
    if counts.size == 0:
        return []
    if o_times.size < 2:
        raise ValueError(f"need >= 2 omega samples, got {o_times.size}")

    order = np.argsort(o_times, kind="stable")
    o_times, o_vals = o_times[order], o_vals[order]
    order = np.argsort(counts, kind="stable")
    counts, f_times = counts[order], f_times[order]

    n_frames = int(counts[-1])
    all_counts = np.arange(1, n_frames + 1)
    t_arrival = np.interp(all_counts, counts, f_times)
    measured = np.isin(all_counts, counts.astype(int))

    t_end = t_arrival - t_latency
    t_beg = t_end - t_acquire
    t_mid = 0.5 * (t_beg + t_end)

    def omega_at(t):
        out = np.interp(t, o_times, o_vals)
        out[(t < o_times[0]) | (t > o_times[-1])] = np.nan
        return out

    om_beg, om_mid, om_end = omega_at(t_beg), omega_at(t_mid), omega_at(t_end)
    lo, hi = min(p_start, p_end), max(p_start, p_end)

    rows = []
    for i in range(n_frames):
        number = first_file_number + i
        try:
            fname = file_template % ("", file_name, number)
        except (TypeError, ValueError):
            fname = f"{file_name}_{number:03d}.tif"
        rows.append(
            {
                "frame_index": i,
                "file_name": fname,
                "t_arrival": float(t_arrival[i]),
                "t_measured": bool(measured[i]),
                "omega_start": float(om_beg[i]),
                "omega_mid": float(om_mid[i]),
                "omega_end": float(om_end[i]),
                "in_scan": bool(lo <= om_mid[i] <= hi),
            }
        )
    return rows


def write_frame_table(rows, path):
    """Write :func:`build_frame_table` rows to ``path`` as CSV."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_TABLE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _check_omega_range(omega, geom):
    """Raise ValueError if the taxi range or velocity is unreachable."""
    low, high = omega.limits
    if (low, high) != (0, 0):  # (0, 0) means "no soft limits" in the record
        for label, p in (("p_initial", geom.p_initial), ("p_final", geom.p_final)):
            if not low <= p <= high:
                raise ValueError(
                    f"{label}={p:g} is outside omega limits [{low:g}, {high:g}];"
                    " reduce the range or taxi_allowance."
                )
    vmax = read_motor_field(omega, ".VMAX")
    if vmax and geom.scan_velocity > vmax:
        raise ValueError(
            f"scan velocity {geom.scan_velocity:g} exceeds omega VMAX {vmax:g};"
            " lower exposures_per_egu or raise t_period."
        )


@plan
def camera_flyscan(
    file_path: str,
    file_name: str,
    p_start: float = 0.0,
    p_end: float = 10.0,
    exposures_per_egu: float = 1.0,
    t_period: float = 0.5,
    t_acquire: float = None,
    taxi_allowance: float = 0.5,
    t_latency: float = 0.0,
    table_dir: str = None,
    md: dict = None,
):
    """Fly omega while the alignment camera saves one TIFF per frame.

    Retracts ``laser_optics`` (the omega interlock), taxis omega to just
    before ``p_start``, sets the scan velocity, starts the camera, and
    rotates omega through ``p_end``.  Frames are taken from the moment the
    camera starts until omega stops, so some lie outside ``[p_start,
    p_end]``; the ``in_scan`` column of the omega table marks the ones
    inside.  Omega and the camera frame counter are recorded as monitor
    streams in the run, and ``<file_name>_omega_table.csv`` pairs every TIFF
    with its omega (start / middle / end of exposure).

    ``file_path`` must already exist on the camera IOC's filesystem.

    Parameters
    ----------
    file_path : str
        Save directory :: directory on the camera IOC's filesystem to write
        TIFF files to (``tiff1.file_path``); must already exist.
    file_name : str
        File name prefix :: TIFF file name prefix (``tiff1.file_name``);
        the IOC appends ``_<number>.tif``, numbered from 0.
    p_start : float [deg]
        Scan start :: omega at which in-scan frames begin.
    p_end : float [deg]
        Scan end :: omega at which in-scan frames end; must exceed
        ``p_start``.
    exposures_per_egu : float [1/deg]
        Frames per degree :: sets the omega velocity together with
        ``t_period`` (``velocity = 1 / (exposures_per_egu * t_period)``).
    t_period : float [s]
        Frame period :: time between frame starts (``cam.acquire_period``).
    t_acquire : float [s]
        Exposure time :: per-frame exposure (``cam.acquire_time``); blank
        uses 0.9 * ``t_period``.
    taxi_allowance : float [deg]
        Extra run-up :: omega distance added before ``p_start`` and after
        ``p_end`` on top of the acceleration distance.
    t_latency : float [s]
        Counter latency :: delay from end of exposure to the frame-counter
        timestamp, subtracted when pairing frames to omega; calibrate once.
    table_dir : str
        Omega table directory :: workstation directory for the CSV; blank
        uses ``file_path`` if it exists on this machine, else no CSV (the
        monitor streams in the run still hold the data).
    md : dict
        Metadata :: extra run metadata, merged last.
    """
    camera = oregistry["pg3_camera"]
    omega = oregistry["sample_stage"].omega
    laser_optics = oregistry["laser_optics"]

    if t_acquire is None:
        t_acquire = 0.9 * t_period
    if not 0 < t_acquire <= t_period:
        raise ValueError(
            f"t_acquire={t_acquire:g} must be in (0, t_period={t_period:g}]"
        )

    geom = compute_flyscan_geometry(
        omega, p_start, p_end, exposures_per_egu, t_period, taxi_allowance
    )
    _check_omega_range(omega, geom)

    # Camera runs from before the motor launches until omega stops, so size
    # the frame budget for the whole traverse (+ start margin) and stop it
    # explicitly once the motor is done.
    t_traverse = (
        geom.p_final - geom.p_initial
    ) / geom.scan_velocity + 2 * geom.motor_accl
    n_cap = int(math.ceil((t_traverse + 2.0) / t_period)) + 5

    tiff = camera.tiff1
    saved = [
        (omega.velocity, omega.velocity.get()),
        (camera.cam.image_mode, camera.cam.image_mode.get()),
        (camera.cam.acquire_time, camera.cam.acquire_time.get()),
        (camera.cam.acquire_period, camera.cam.acquire_period.get()),
        (camera.cam.num_images, camera.cam.num_images.get()),
    ]
    omega_samples = []  # (timestamp, readback)
    frame_samples = []  # (timestamp, ArrayCounter)
    cids = []

    def _on_omega(*, value, timestamp, **_):
        omega_samples.append((timestamp, float(value)))

    def _on_frame(*, value, timestamp, **_):
        frame_samples.append((timestamp, int(value)))

    run_md = {
        "plan_name": "camera_flyscan",
        "detectors": [camera.name],
        "motors": [omega.name],
        "p_start": p_start,
        "p_end": p_end,
        "exposures_per_egu": exposures_per_egu,
        "t_period": t_period,
        "t_acquire": t_acquire,
        "t_latency": t_latency,
        "scan_velocity": geom.scan_velocity,
        "p_initial": geom.p_initial,
        "p_final": geom.p_final,
        "num_frames_in_scan": geom.num_frames,
        "tiff_file_path": file_path,
        "tiff_file_name": file_name,
        "tiff_first_file_number": 0,
        "camera_max_frames": n_cap,
    }
    run_md.update(md or {})

    @bpp.monitor_during_decorator([omega.user_readback, camera.cam.array_counter])
    @bpp.run_decorator(md=run_md)
    def _scan():
        # Camera first so it is already free-running when omega launches.
        yield from bps.abs_set(camera.cam.acquire, 1)
        yield from bps.sleep(min(t_period, 1.0))
        yield from bps.abs_set(omega, geom.p_final, group="fly")
        yield from bps.wait("fly")
        yield from bps.mv(camera.cam.acquire, 0)
        yield from _wait_cam_idle(camera, timeout=30.0)
        # No per-frame "write complete" signal on the TIFF plugin; give the
        # last frame(s) a moment to land (same reasoning as camera_snapshot).
        yield from bps.sleep(1.0)

    def _main():
        # Interlock: omega will not move unless the laser optics are OUT.
        yield from laser_optics.move_out()
        yield from bps.mv(omega, geom.p_initial)  # taxi at the normal velocity
        yield from bps.mv(omega.velocity, geom.scan_velocity)
        yield from bps.mv(
            camera.cam.image_mode,
            "Multiple",
            camera.cam.num_images,
            n_cap,
            camera.cam.acquire_time,
            t_acquire,
            camera.cam.acquire_period,
            t_period,
            camera.cam.array_counter,
            0,
            tiff.enable,
            "Enable",
            tiff.file_write_mode,
            "Single",
            tiff.file_path,
            file_path,
            tiff.file_name,
            file_name,
            tiff.file_number,
            0,
            tiff.auto_increment,
            "Yes",
            tiff.auto_save,
            "Yes",
        )
        cids.append(
            (omega.user_readback, omega.user_readback.subscribe(_on_omega, run=False))
        )
        cids.append(
            (
                camera.cam.array_counter,
                camera.cam.array_counter.subscribe(_on_frame, run=False),
            )
        )
        yield from _scan()
        # Snapshot before cleanup(): number of files the plugin wrote.
        n_files = int(tiff.file_number.get())
        template = tiff.file_template.get()
        return n_files, template

    def _cleanup():
        for sig, cid in cids:
            sig.unsubscribe(cid)
        yield from bps.mv(camera.cam.acquire, 0)
        if motor_is_moving(omega):
            yield from bps.stop(omega)
        # Restore everything we changed on omega / the cam (TIFF settings
        # stay, as in camera_snapshot).
        for sig, value in saved:
            yield from bps.mv(sig, value)

    n_files, template = yield from bpp.finalize_wrapper(_main(), _cleanup())

    if not frame_samples:
        logger.warning("camera_flyscan: no camera frames were counted")
        return
    rows = build_frame_table(
        [c for _, c in frame_samples],
        [t for t, _ in frame_samples],
        [t for t, _ in omega_samples],
        [v for _, v in omega_samples],
        t_acquire=t_acquire,
        p_start=p_start,
        p_end=p_end,
        file_name=file_name,
        t_latency=t_latency,
        file_template=template or _DEFAULT_TIFF_TEMPLATE,
    )
    n_in_scan = sum(r["in_scan"] for r in rows)
    logger.info(
        "camera_flyscan: %d frame(s) (%d in scan, %d expected), %d TIFF file(s)",
        len(rows),
        n_in_scan,
        geom.num_frames,
        n_files,
    )
    if n_files != len(rows):
        logger.warning(
            "camera_flyscan: %d frames counted but %d TIFF files written --"
            " the TIFF plugin dropped frames; table rows are offset from files.",
            len(rows),
            n_files,
        )

    out_dir = table_dir or file_path
    if os.path.isdir(out_dir):
        table_path = os.path.join(out_dir, f"{file_name}_omega_table.csv")
        write_frame_table(rows, table_path)
        logger.info("camera_flyscan: wrote omega table %s", table_path)
    else:
        logger.warning(
            "camera_flyscan: %r is not a directory on this machine; omega table"
            " not written (monitor streams in the run still hold the data).",
            out_dir,
        )
