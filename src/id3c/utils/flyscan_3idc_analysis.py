"""Post-run analysis helpers for ``flyscan_3idc`` runs.

This module pairs each detector frame with the motor's
interpolated position at the frame's IOC timestamp, using the
monitor streams that ``flyscan_3idc.flyscan`` sets up via
``@bpp.monitor_during_decorator``.  No bluesky, no ophyd, no
RunEngine — purely operates on ``BlueskyRun``-shaped objects from
tiled / databroker.

Usage
-----

::

    from tiled.client import from_profile
    cat = from_profile("your_profile")["your_tree"]
    run = cat[-1]
    from flyscan_3idc_analysis import pair_frames_to_positions
    df = pair_frames_to_positions(run)
    # df columns: image_number, timestamp,
    #             position_start_acquire, position_end_acquire,
    #             position_end_period
    # df.index: absolute timestamp (float seconds since epoch)
    #
    # optional write to CSV file
    df.to_csv("scan.csv")

Calibrate timestamps: flymotor & area detector
----------------------------------------------

The per-frame positions depend on a constant
``hdf_t_phase_offset`` that maps each frame's IOC timestamp to its
exposure-start moment.

Measure it once per IOC/detector with
``hdf_timestamp_semantic_diagnostic`` and pass the result to
``flyscan(..., hdf_t_phase_offset=...)``; see that function's
docstring for the procedure.

Design notes
------------

- IOC timestamps are the system of record for pairing.  The
  primary-stream snapshots from the plan are a progress indicator;
  this module's output is the high-fidelity pairing.
- Monitor-stream record order is interleaved across CA dispatcher
  segments, so sort by timestamp:
  - the motor monitor stream then yields a strictly increasing
    position trace at constant velocity in the in-scan window.
  - the HDF array_counter monitor stream then yields strictly
    monotonic counter values (0, 1, 2, ..., contiguous).
- The function uses linear interpolation of motor position vs
  motor IOC timestamp.  Linear is exact for a motor at constant
  velocity in the in-scan window (which is the entire reason the
  plan sets velocity = (p_end-p_start)/(num_frames*t_period) and
  taxis the motor up to scan velocity before crossing p_start).
- Frames whose timestamps fall outside the motor stream's time
  range are dropped (extrapolation is rejected, never silent).
- Frames whose interpolated positions fall outside
  ``[p_start, p_end]`` are dropped (this is "frames captured
  during taxi-in / coast-out" — they're in the HDF5 file but not
  part of the scan).
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _interpolate_positions(
    motor_t: np.ndarray,
    motor_pos: np.ndarray,
    hdf_t: np.ndarray,
    hdf_counter: np.ndarray,
    p_start: float,
    p_end: float,
    t_acquire: float,
    t_period: float,
    hdf_t_phase_offset: float,
) -> pd.DataFrame:
    """Pair HDF frames with three per-frame motor positions.

    Pure-array core; no run object, no ophyd, no bluesky.  Tests
    construct inputs directly.

    Each frame has a corresponding cam exposure with three
    physically meaningful per-period moments:

        start_acquire = hdf_t + hdf_t_phase_offset
        end_acquire   = start_acquire + t_acquire
        end_period    = start_acquire + t_period

    The motor position at each of these three moments is reported
    separately, so downstream analysis can choose whichever phase
    best matches its model of "what the cam was looking at".  See
    ``hdf_timestamp_semantic_diagnostic`` for how to determine the
    right ``hdf_t_phase_offset`` value for a given IOC.

    Parameters
    ----------
    motor_t : np.ndarray
        IOC timestamps (seconds since epoch) for motor samples.
        May be in arbitrary order with duplicates; this function
        sorts and dedupes.
    motor_pos : np.ndarray
        Motor positions (engineering units) aligned with
        ``motor_t``.
    hdf_t : np.ndarray
        IOC timestamps (seconds since epoch) for HDF frame
        captures.  May be in arbitrary order; this function sorts.
    hdf_counter : np.ndarray
        Integer HDF frame counter (``hdf1.array_counter``) aligned
        with ``hdf_t``.
    p_start, p_end : float
        Scan range in motor engineering units.  A frame is "in scan"
        if its time interval ``[start_acquire.t, end_period.t]``
        OVERLAPS the time window during which the motor was inside
        ``[p_start, p_end]``.  That window is bracketed by the first
        and last motor-stream samples whose position lies in
        ``[p_start, p_end]``.  The time-overlap rule admits
        leading-edge and trailing-edge frames whose exposure crossed
        a boundary mid-way, as well as frames that fell on the
        wrong side of the boundary only due to motor-stream
        interpolation noise.
    t_acquire : float
        Exposure time per frame, in seconds.  Used to compute
        ``end_acquire = start_acquire + t_acquire``.
    t_period : float
        Period between successive frame starts, in seconds.  Used
        to compute ``end_period = start_acquire + t_period``.
    hdf_t_phase_offset : float
        Offset, in seconds, from each ``hdf_t`` to the
        corresponding ``start_acquire``.  Typically negative
        (``hdf_t`` arrives at or after the frame's cam-end-of-
        exposure event; ``start_acquire`` is one t_acquire earlier).
        See ``hdf_timestamp_semantic_diagnostic`` to determine the
        right value for an IOC; ``flyscan_3idc.build_flyscan_md``
        defaults this to ``-t_acquire``.

    Returns
    -------
    pandas.DataFrame
        Columns:

        - ``image_number`` (int64) — HDF array_counter value.
        - ``timestamp`` (float) — raw ``hdf_t`` for the frame.
        - ``position_start_acquire`` (float) — motor position at
          the start of this frame's exposure.
        - ``position_end_acquire`` (float) — motor position at the
          end of this frame's exposure.
        - ``position_end_period`` (float) — motor position at the
          end of this frame's period (= start of the next frame's
          exposure).

        Indexed by ``timestamp``.  Sorted by timestamp ascending.
        Only frames satisfying both filters are present:

        1. all three phase timestamps fall within the motor
           stream's time range (extrapolation is rejected);
        2. the frame's ``[start_acquire.t, end_period.t]`` interval
           overlaps the motor's in-range time window (the time
           bracket during which the motor was inside
           ``[p_start, p_end]``).

        Image numbers are unique within the returned frame; if the
        IOC's monitor stream emitted a counter value twice (CA
        dispatcher quirk), the first occurrence is kept and a
        WARNING is logged.
    """
    motor_t = np.asarray(motor_t, dtype=float)
    motor_pos = np.asarray(motor_pos, dtype=float)
    hdf_t = np.asarray(hdf_t, dtype=float)
    hdf_counter = np.asarray(hdf_counter, dtype=np.int64)

    if motor_t.shape != motor_pos.shape:
        raise ValueError(
            f"motor_t shape {motor_t.shape} != motor_pos shape {motor_pos.shape}"
        )
    if hdf_t.shape != hdf_counter.shape:
        raise ValueError(
            f"hdf_t shape {hdf_t.shape} != hdf_counter shape {hdf_counter.shape}"
        )
    if motor_t.size < 2:
        raise ValueError(
            f"motor stream has {motor_t.size} sample(s); need >= 2 for interpolation"
        )
    if t_acquire <= 0:
        raise ValueError(f"t_acquire={t_acquire!r} must be positive")
    if t_period <= 0:
        raise ValueError(f"t_period={t_period!r} must be positive")
    if hdf_t.size == 0:
        # Nothing to pair; return an empty correctly-typed frame.
        return _empty_result()

    # Sort motor by timestamp and dedupe — keep first occurrence
    # of each unique timestamp.  ``np.unique(..., return_index=True)``
    # returns indices into the sorted-unique array; combine with
    # argsort to recover the first occurrence in the original order.
    m_order = np.argsort(motor_t, kind="stable")
    m_t_sorted = motor_t[m_order]
    m_p_sorted = motor_pos[m_order]
    # Find duplicates after sort: keep first.
    _, unique_idx = np.unique(m_t_sorted, return_index=True)
    unique_idx.sort()
    m_t = m_t_sorted[unique_idx]
    m_p = m_p_sorted[unique_idx]
    n_dropped_dups = motor_t.size - m_t.size
    if n_dropped_dups:
        logger.debug(
            "_interpolate_positions: deduplicated %d motor sample(s)"
            " with repeated timestamp",
            n_dropped_dups,
        )

    # Sort HDF by timestamp.
    h_order = np.argsort(hdf_t, kind="stable")
    h_t = hdf_t[h_order]
    h_c = hdf_counter[h_order]

    # Compute the three per-frame phase timestamps for every HDF event.
    t_start_acquire = h_t + hdf_t_phase_offset
    t_end_acquire = t_start_acquire + t_acquire
    t_end_period = t_start_acquire + t_period

    # Drop frames whose *any* phase timestamp is outside the motor
    # stream's time range.  Linear interpolation past the motor
    # stream's endpoints would extrapolate, which we disallow.
    t_lo, t_hi = m_t[0], m_t[-1]
    in_time_range = (
        (t_start_acquire >= t_lo)
        & (t_start_acquire <= t_hi)
        & (t_end_acquire >= t_lo)
        & (t_end_acquire <= t_hi)
        & (t_end_period >= t_lo)
        & (t_end_period <= t_hi)
    )
    n_out_of_range = h_t.size - int(in_time_range.sum())
    if n_out_of_range:
        logger.warning(
            "_interpolate_positions: dropping %d HDF frame(s) with"
            " phase timestamps outside motor stream range"
            " [%g, %g] (would require extrapolation)",
            n_out_of_range,
            t_lo,
            t_hi,
        )
    h_t_keep = h_t[in_time_range]
    h_c_keep = h_c[in_time_range]
    ts_start = t_start_acquire[in_time_range]
    ts_end_a = t_end_acquire[in_time_range]
    ts_end_p = t_end_period[in_time_range]
    if h_t_keep.size == 0:
        return _empty_result()

    # Three linear interpolations — np.interp requires monotonic xp;
    # we ensured that via sort+unique on the motor stream above.
    pos_start = np.interp(ts_start, m_t, m_p)
    pos_end_a = np.interp(ts_end_a, m_t, m_p)
    pos_end_p = np.interp(ts_end_p, m_t, m_p)

    # In-scan filter: time-domain overlap.
    #
    # A frame is "in scan" if the time interval over which it was
    # exposing/holding ([start_acquire.t, end_period.t]) OVERLAPS the
    # time window during which the motor was inside [p_start, p_end].
    # The time-overlap rule (rather than testing a single position
    # against [p_start, p_end]) admits three classes of frame that
    # carry valid in-range data:
    #
    #   1. Leading-edge: exposure started just before p_start but
    #      crossed p_start before end_acquire.  The frame DOES carry
    #      data from inside the scan range.
    #   2. Trailing-edge: exposure started just before p_end but
    #      crossed p_end before end_period.  Same logic.
    #   3. Boundary-noise: frames whose true position is inside
    #      [p_start, p_end] but whose interpolated pos_start was
    #      slightly outside due to motor-stream sampling noise near
    #      the boundary.
    #
    # The motor in-range time window is bracketed by the first and
    # last motor-stream samples whose position lies in [p_start, p_end].
    # This handles forward sweeps, reverse sweeps, and the (less common)
    # case where the motor passes back through the range, conservatively:
    # the window spans from the earliest to the latest in-range sample,
    # so a frame in between is admitted.
    #
    # Frames that never overlapped the window at all (taxi-in / coast-
    # out frames whose entire [start_acquire, end_period] falls before
    # the first in-range motor sample or after the last) are dropped:
    # they carry no in-range data.
    in_range_pos = (m_p >= p_start) & (m_p <= p_end)
    if not in_range_pos.any():
        # The motor never entered the scan range in this run; reject
        # every frame.
        in_scan = np.zeros_like(ts_start, dtype=bool)
        motor_t_in_range_start = None
        motor_t_in_range_end = None
    else:
        in_range_idx = np.where(in_range_pos)[0]
        motor_t_in_range_start = float(m_t[in_range_idx[0]])
        motor_t_in_range_end = float(m_t[in_range_idx[-1]])
        in_scan = (ts_end_p >= motor_t_in_range_start) & (
            ts_start <= motor_t_in_range_end
        )

    n_out_of_scan = h_t_keep.size - int(in_scan.sum())
    if n_out_of_scan:
        if motor_t_in_range_start is None:
            logger.info(
                "_interpolate_positions: dropping all %d HDF frame(s);"
                " no motor sample ever fell inside [%g, %g]",
                n_out_of_scan,
                p_start,
                p_end,
            )
        else:
            logger.info(
                "_interpolate_positions: dropping %d HDF frame(s) whose"
                " [start_acquire.t, end_period.t] interval did not"
                " overlap the motor in-range time window"
                " [%g, %g] s (positions [%g, %g]) -- taxi-in / coast-out"
                " frames",
                n_out_of_scan,
                motor_t_in_range_start,
                motor_t_in_range_end,
                p_start,
                p_end,
            )

    # After all filters, also dedup by image_number: the IOC's CA
    # monitor stream very occasionally emits a counter value twice
    # (dispatcher quirk).  Keep first occurrence; log the duplicates.
    counter_in_scan = h_c_keep[in_scan].astype(np.int64)
    _, first_idx = np.unique(counter_in_scan, return_index=True)
    first_idx.sort()
    n_dropped_dup_counter = counter_in_scan.size - first_idx.size
    if n_dropped_dup_counter:
        # Compute the duplicated values for the log message.  Use
        # set arithmetic on the sorted unique-kept counters.
        kept = counter_in_scan[first_idx]
        seen = set()
        dups = []
        for c in counter_in_scan:
            if int(c) in seen and int(c) in set(int(x) for x in kept):
                if int(c) not in dups:
                    dups.append(int(c))
            seen.add(int(c))
        logger.warning(
            "_interpolate_positions: dropping %d frame(s) with"
            " duplicate image_number value(s) %r within in-scan window."
            " Symptom of CA dispatcher firing twice for the same"
            " IOC-side counter value; keeping the first occurrence.",
            n_dropped_dup_counter,
            dups,
        )

    # Apply in-scan + dedup masks together to all arrays in one
    # final indexing pass.
    sel = np.where(in_scan)[0][first_idx]
    df = pd.DataFrame(
        {
            "image_number": h_c_keep[sel].astype(np.int64),
            "timestamp": h_t_keep[sel].astype(float),
            "position_start_acquire": pos_start[sel].astype(float),
            "position_end_acquire": pos_end_a[sel].astype(float),
            "position_end_period": pos_end_p[sel].astype(float),
        }
    )
    df = df.set_index("timestamp", drop=False)
    df = df.sort_index()
    return df


def _empty_result() -> pd.DataFrame:
    """Return a correctly-typed empty result DataFrame."""
    df = pd.DataFrame(
        {
            "image_number": np.array([], dtype=np.int64),
            "timestamp": np.array([], dtype=float),
            "position_start_acquire": np.array([], dtype=float),
            "position_end_acquire": np.array([], dtype=float),
            "position_end_period": np.array([], dtype=float),
        }
    )
    df = df.set_index("timestamp", drop=False)
    return df


EPICS_EPOCH_OFFSET_S = 631_152_000
"""Add this to an EPICS timestamp to get a Unix timestamp.

EPICS time is seconds since 1990-01-01T00:00:00 UTC; Unix time is
seconds since 1970-01-01T00:00:00 UTC.  AD plugins store frame
timestamps in EPICS time; bluesky monitor streams use Unix time.
"""


def pair_frames_to_positions_from_ad_file(
    run,
    ad_file_path,
    *,
    timestamp_dset="/entry/instrument/NDAttributes/NDArrayTimeStamp",
    unique_id_dset="/entry/instrument/NDAttributes/NDArrayUniqueId",
) -> pd.DataFrame:
    """Pair frames with motor positions, sourcing timestamps from the AD file.

    Same output shape as ``pair_frames_to_positions`` but reads
    per-frame ``(timestamp, unique_id)`` from the AD HDF1 file's own
    NDAttribute datasets instead of from the CA monitor stream.
    The AD file is authoritative -- the IOC writes one row per
    acquired frame -- so the image-number gaps caused by CA monitor
    coalescing do not apply here.

    The AD file's ``NDArrayTimeStamp`` is in EPICS epoch (seconds
    since 1990-01-01 UTC).  Bluesky's motor monitor stream uses Unix
    epoch.  This helper converts by adding ``EPICS_EPOCH_OFFSET_S``;
    no per-IOC calibration measurement is required as long as the
    AD IOC and the motor IOC are NTP-synchronized.

    ``NDArrayUniqueId`` is 0-based (the IOC counts from 0).  This
    helper adds 1 so the returned ``image_number`` matches the
    1-based ``hdf1.array_counter`` convention used by
    ``pair_frames_to_positions`` and by the downstream
    ``frame_index = image_number - 1`` slicing.

    Parameters
    ----------
    run : BlueskyRun
        Same as ``pair_frames_to_positions``; only the motor monitor
        stream is read from it.
    ad_file_path : str
        Path to the AD HDF1 file.  Caller is responsible for
        resolving relative-link / symlink translation (e.g. via
        ``flyscan_3idc._external_link_target`` + the workstation's
        ``./ad_files/`` symlink).
    timestamp_dset, unique_id_dset : str
        HDF5 paths inside the AD file for the per-frame timestamp
        and UID datasets.  Defaults match the EPICS areaDetector
        NDFileHDF5 plugin's standard NDAttribute layout.

    Returns
    -------
    pandas.DataFrame
        Same columns as ``pair_frames_to_positions``.
    """
    import h5py

    md = _get_start_metadata(run)
    flymotor_name = _require_metadata_key(md, "flymotor_name")
    p_start = float(_require_metadata_key(md, "p_start"))
    p_end = float(_require_metadata_key(md, "p_end"))
    t_acquire = float(_require_metadata_key(md, "t_acquire"))
    t_period = float(_require_metadata_key(md, "t_period"))

    # Read motor monitor stream (Unix epoch).
    motor_stream_name = f"{flymotor_name}_monitor"
    motor_ds = _read_stream(run, motor_stream_name)
    motor_t = _array_from_ds(motor_ds, "time", motor_stream_name)
    motor_pos = _array_from_ds(motor_ds, flymotor_name, motor_stream_name)

    # Read per-frame timestamp + UID from the AD HDF1 file.
    with h5py.File(ad_file_path, "r") as f:
        if timestamp_dset not in f:
            raise KeyError(
                f"AD file {ad_file_path!r} has no dataset {timestamp_dset!r}"
            )
        if unique_id_dset not in f:
            raise KeyError(
                f"AD file {ad_file_path!r} has no dataset {unique_id_dset!r}"
            )
        ad_t_epics = np.asarray(f[timestamp_dset][...], dtype=float)
        ad_uid = np.asarray(f[unique_id_dset][...], dtype=np.int64)

    if ad_t_epics.size != ad_uid.size:
        raise ValueError(
            f"AD timestamp and unique_id arrays disagree on length:"
            f" {ad_t_epics.size} vs {ad_uid.size}"
        )

    # Convert EPICS epoch -> Unix epoch, and convert 0-based UID -> 1-based.
    hdf_t = ad_t_epics + EPICS_EPOCH_OFFSET_S
    hdf_counter = ad_uid + 1

    # Same end_acquire semantic as the CA path: the IOC stamps each
    # frame at end-of-acquire.  Phase offset of -t_acquire takes
    # the frame timestamp back to start_acquire.
    hdf_t_phase_offset = -t_acquire

    logger.info(
        "pair_frames_to_positions_from_ad_file: motor=%r (%d sample(s)),"
        " ad_file=%r (%d frame(s)), p_start=%g p_end=%g"
        " t_acquire=%g t_period=%g",
        motor_stream_name,
        motor_t.size,
        ad_file_path,
        hdf_t.size,
        p_start,
        p_end,
        t_acquire,
        t_period,
    )

    df = _interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start,
        p_end,
        t_acquire=t_acquire,
        t_period=t_period,
        hdf_t_phase_offset=hdf_t_phase_offset,
    )
    logger.info(
        "pair_frames_to_positions_from_ad_file: paired %d in-scan frame(s)",
        len(df),
    )
    return df


def pair_frames_to_positions(run) -> pd.DataFrame:
    """Pair each in-scan HDF frame with three motor positions per period.

    Reads everything from the run's start-document metadata and the
    standard monitor streams set up by ``flyscan_3idc.flyscan``:

    - ``<flymotor_name>_monitor`` — motor position vs IOC timestamp.
    - ``<det_name>_hdf1_array_counter_monitor`` — HDF
      ``array_counter`` vs IOC timestamp.
    - ``p_start``, ``p_end``, ``t_acquire``, ``t_period``,
      ``hdf_t_phase_offset`` from ``run.metadata["start"]``.

    For each in-scan frame, three motor positions are reported,
    one at each of the three per-period phase moments:

        position_start_acquire = motor at hdf_t + hdf_t_phase_offset
        position_end_acquire   = motor at the above + t_acquire
        position_end_period    = motor at the above + t_period

    Frames whose ``position_start_acquire`` is outside
    ``[p_start, p_end]`` are dropped (taxi / coast / before-acquire-
    finished frames).  Frames whose phase timestamps would require
    extrapolating past the motor stream's time range are also
    dropped (no silent extrapolation).  Duplicate image_number
    values within the in-scan window are deduped with a WARNING.

    Parameters
    ----------
    run : BlueskyRun
        Tiled / databroker run object.  Must have
        ``.metadata["start"]`` with the keys ``p_start``,
        ``p_end``, ``flymotor_name``, ``det_name``, ``t_acquire``,
        ``t_period``, ``hdf_t_phase_offset``, and must expose
        monitor streams named per the convention above.

    Returns
    -------
    pandas.DataFrame
        Columns: ``image_number`` (int64, the HDF array_counter
        value at frame capture), ``timestamp`` (float, raw IOC
        ``hdf_t`` of the frame), and three position columns
        (``position_start_acquire``, ``position_end_acquire``,
        ``position_end_period``).  Indexed by ``timestamp``
        ascending.

    Raises
    ------
    KeyError
        Required metadata key or monitor stream is missing from
        the run.
    ValueError
        Motor stream has fewer than 2 samples, or ``t_acquire``
        / ``t_period`` are non-positive.
    """
    md = _get_start_metadata(run)
    flymotor_name = _require_metadata_key(md, "flymotor_name")
    det_name = _require_metadata_key(md, "det_name")
    p_start = float(_require_metadata_key(md, "p_start"))
    p_end = float(_require_metadata_key(md, "p_end"))
    t_acquire = float(_require_metadata_key(md, "t_acquire"))
    t_period = float(_require_metadata_key(md, "t_period"))
    hdf_t_phase_offset = float(_require_metadata_key(md, "hdf_t_phase_offset"))

    motor_stream_name = f"{flymotor_name}_monitor"
    motor_field_name = flymotor_name
    hdf_stream_name = f"{det_name}_hdf1_array_counter_monitor"
    hdf_field_name = f"{det_name}_hdf1_array_counter"

    motor_ds = _read_stream(run, motor_stream_name)
    hdf_ds = _read_stream(run, hdf_stream_name)

    motor_t = _array_from_ds(motor_ds, "time", motor_stream_name)
    motor_pos = _array_from_ds(motor_ds, motor_field_name, motor_stream_name)
    hdf_t = _array_from_ds(hdf_ds, "time", hdf_stream_name)
    hdf_counter = _array_from_ds(hdf_ds, hdf_field_name, hdf_stream_name)

    logger.info(
        "pair_frames_to_positions: motor=%r (%d sample(s)),"
        " hdf=%r (%d frame(s)), p_start=%g p_end=%g"
        " t_acquire=%g t_period=%g hdf_t_phase_offset=%g",
        motor_stream_name,
        motor_t.size,
        hdf_stream_name,
        hdf_t.size,
        p_start,
        p_end,
        t_acquire,
        t_period,
        hdf_t_phase_offset,
    )

    df = _interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start,
        p_end,
        t_acquire=t_acquire,
        t_period=t_period,
        hdf_t_phase_offset=hdf_t_phase_offset,
    )
    logger.info(
        "pair_frames_to_positions: paired %d in-scan frame(s)",
        len(df),
    )
    return df


# ---------------------------------------------------------------------------
# Internal: thin shims around the BlueskyRun shape that let us swap in
# duck-typed mocks for tests.
# ---------------------------------------------------------------------------


def _get_start_metadata(run) -> dict:
    """Return the run's start-document metadata as a dict."""
    md = getattr(run, "metadata", None)
    if md is None:
        raise KeyError("run object has no .metadata attribute")
    # Both dict-style (BlueskyRun) and Mapping-style access work.
    try:
        start = md["start"]
    except (KeyError, TypeError) as exc:
        raise KeyError("run.metadata is missing the 'start' document") from exc
    if not isinstance(start, dict):
        raise KeyError(
            f"run.metadata['start'] is {type(start).__name__}, expected dict"
        )
    return start


def _require_metadata_key(md: dict, key: str):
    if key not in md:
        raise KeyError(f"run.metadata['start'] is missing required key {key!r}")
    return md[key]


def _read_stream(run, stream_name: str):
    """Return ``run.<stream_name>.read()`` (an xarray.Dataset)."""
    stream = getattr(run, stream_name, None)
    if stream is None:
        raise KeyError(f"run has no stream named {stream_name!r}")
    if not hasattr(stream, "read"):
        raise KeyError(
            f"run.{stream_name} has no .read() method (got {type(stream).__name__})"
        )
    return stream.read()


def _array_from_ds(ds, key: str, stream_name: str) -> np.ndarray:
    """Pull a column from an xarray.Dataset (or dict-like) as a 1-D numpy array."""
    try:
        col = ds[key]
    except (KeyError, TypeError) as exc:
        raise KeyError(f"stream {stream_name!r} has no column {key!r}") from exc
    # xarray DataArray exposes .data; numpy arrays are already arrays;
    # pandas Series have .to_numpy().  Try them in order.
    if hasattr(col, "data"):
        arr = col.data
    elif hasattr(col, "to_numpy"):
        arr = col.to_numpy()
    else:
        arr = np.asarray(col)
    return np.asarray(arr)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def _by_counter(t: np.ndarray, c: np.ndarray):
    """Sort by timestamp, then keep the first event seen for each
    unique counter value.  Returns (counter_unique, t_unique) in
    counter-value-ascending order.

    Mirrors the dedup-on-duplicate-timestamps pattern used in
    ``_interpolate_positions`` but keyed on the counter rather than
    the timestamp: the IOC's array_counter monitor stream emits one
    event per increment, but the CA dispatcher occasionally repeats
    a value across segments.  Keeping the first-seen event per
    counter value gives a single timestamp per frame number.
    """
    t = np.asarray(t, dtype=float)
    c = np.asarray(c, dtype=np.int64)
    order = np.argsort(t, kind="stable")
    t_sorted = t[order]
    c_sorted = c[order]
    # First-occurrence dedup by counter value: argsort-stable on
    # counter, then unique returns the first index per group.
    c_order = np.argsort(c_sorted, kind="stable")
    c_sorted2 = c_sorted[c_order]
    t_sorted2 = t_sorted[c_order]
    _, first_idx = np.unique(c_sorted2, return_index=True)
    first_idx.sort()
    return c_sorted2[first_idx], t_sorted2[first_idx]


def hdf_timestamp_semantic_diagnostic(run) -> dict:
    """Empirically determine what moment hdf1.array_counter timestamps mark.

    The flyscan plan records two array_counter monitor streams:

    - ``<det>_cam_array_counter_monitor``: the cam's frame counter,
      incremented when the cam finishes capturing a frame.  Closest
      to ``end_acquire`` from the cam's perspective.
    - ``<det>_hdf1_array_counter_monitor``: the HDF plugin's frame
      counter, incremented after the plugin has accepted (and
      typically written) the frame.

    ``pair_frames_to_positions`` uses the **hdf** stream's timestamps
    as the "when did this frame happen" coordinate for motor-position
    interpolation.  But the exact moment those timestamps mark within
    each cam exposure period is IOC-/plugin-dependent.  Three
    plausible semantics:

    - ``hdf_t ~= start_acquire``: counter timestamped at the *start*
      of the exposure that produced the frame.  Would predict
      ``hdf_t - cam_t ~= -t_acquire``.
    - ``hdf_t ~= end_acquire``: counter timestamped at the *end* of
      the exposure (when the cam finishes).  Most common AD HDF
      plugin behavior.  Would predict ``hdf_t - cam_t ~= 0`` (plus
      a small plugin-pipeline lag).
    - ``hdf_t ~= end_period``: counter timestamped at the *end* of
      the period (start of the next exposure).  Would predict
      ``hdf_t - cam_t ~= t_period - t_acquire``.

    This diagnostic pairs cam and HDF events by frame counter,
    computes the mean ``hdf_t - cam_t`` over in-scan frames, picks
    the closest-matching semantic, and prints a verdict plus a
    recommended ``hdf_t_phase_offset`` value to use when computing
    each frame's ``start_acquire`` timestamp from its ``hdf_t``:

        start_acquire = hdf_t + hdf_t_phase_offset
        end_acquire   = start_acquire + t_acquire
        end_period    = start_acquire + t_period

    Run once after any IOC / detector / plugin change to confirm
    the semantic.  Returns a dict of the computed values for
    programmatic use (testing, scripting).

    Calibration procedure
    ---------------------

    1. Run a flyscan slow enough that the CA monitor publish path
       can keep up with every counter increment (typically
       ``t_period >= 0.5`` s).
    2. ``result = hdf_timestamp_semantic_diagnostic(cat[-1])``.
       Read the printed report.
    3. If the verdict is ``RELIABLE``, use
       ``result["recommended_hdf_t_phase_offset_s"]`` as the
       calibration constant.  If ``UNRELIABLE``, slow the scan
       further and repeat.
    4. Pass the constant to subsequent flyscans via
       ``RE(flyscan(..., hdf_t_phase_offset=...))`` or change the
       plan's default.

    Parameters
    ----------
    run : BlueskyRun
        A run produced by ``flyscan_3idc.flyscan``.  Must have the
        three monitor streams (``<flymotor>_monitor``,
        ``<det>_cam_array_counter_monitor``,
        ``<det>_hdf1_array_counter_monitor``) and the standard
        start-document metadata (``p_start``, ``p_end``,
        ``flymotor_name``, ``det_name``, ``t_acquire``,
        ``t_period``).

    Returns
    -------
    dict
        Keys:

        - ``n_in_scan_frames`` : int — frames paired and inside
          ``[p_start, p_end]``.
        - ``d1_mean_s`` : float — observed mean ``hdf_t - cam_t``
          over in-scan frames, in seconds.
        - ``d1_std_s`` : float — stddev of the same.
        - ``d2_mean_s`` : float — mean ``diff(hdf_t)`` over
          in-scan frames; should equal ``t_period``.
        - ``t_acquire``, ``t_period`` : float — copies from the
          start metadata, in seconds.
        - ``verdict`` : str — one of ``"start_acquire"``,
          ``"end_acquire"``, ``"end_period"``.
        - ``recommended_hdf_t_phase_offset_s`` : float — the value
          to use as ``hdf_t_phase_offset`` for the chosen verdict
          (negative of the predicted ``hdf_t - cam_t`` for that
          semantic, since ``start_acquire = cam_t - t_acquire``
          under the ``hdf_t == end_acquire`` model).
        - ``is_reliable`` : bool — True iff none of the reliability
          guards tripped.  When False, ``verdict`` is still populated
          but should not be trusted for production phase-offset
          choices.
        - ``sparse_data`` : bool — True iff ``D2 > 2*t_period`` (CA
          monitor publish path is coalescing events).
        - ``noisy_data`` : bool — True iff ``D1 stddev > t_acquire``
          (per-event timestamp jitter is comparable to or larger
          than the time-scale we're trying to discriminate).
        - ``indecisive`` : bool — True iff no candidate semantic is
          meaningfully closer to the observed D1 than any other.

    Raises
    ------
    KeyError
        Required metadata key or monitor stream is missing.
    ValueError
        No frames are in the scan range (cannot determine semantic).
    """
    md = _get_start_metadata(run)
    flymotor_name = _require_metadata_key(md, "flymotor_name")
    det_name = _require_metadata_key(md, "det_name")
    p_start = float(_require_metadata_key(md, "p_start"))
    p_end = float(_require_metadata_key(md, "p_end"))
    t_acquire = float(_require_metadata_key(md, "t_acquire"))
    t_period = float(_require_metadata_key(md, "t_period"))

    cam_stream_name = f"{det_name}_cam_array_counter_monitor"
    cam_field_name = f"{det_name}_cam_array_counter"
    hdf_stream_name = f"{det_name}_hdf1_array_counter_monitor"
    hdf_field_name = f"{det_name}_hdf1_array_counter"
    motor_stream_name = f"{flymotor_name}_monitor"
    motor_field_name = flymotor_name

    cam_ds = _read_stream(run, cam_stream_name)
    hdf_ds = _read_stream(run, hdf_stream_name)
    motor_ds = _read_stream(run, motor_stream_name)

    cam_t_raw = _array_from_ds(cam_ds, "time", cam_stream_name)
    cam_c_raw = _array_from_ds(cam_ds, cam_field_name, cam_stream_name)
    hdf_t_raw = _array_from_ds(hdf_ds, "time", hdf_stream_name)
    hdf_c_raw = _array_from_ds(hdf_ds, hdf_field_name, hdf_stream_name)
    motor_t_raw = _array_from_ds(motor_ds, "time", motor_stream_name)
    motor_pos_raw = _array_from_ds(motor_ds, motor_field_name, motor_stream_name)

    # Dedup-by-counter so we have one timestamp per frame number.
    cam_c, cam_t = _by_counter(cam_t_raw, cam_c_raw)
    hdf_c, hdf_t = _by_counter(hdf_t_raw, hdf_c_raw)

    # Pair cam and HDF events by frame counter (intersection).
    common, ci, hi = np.intersect1d(cam_c, hdf_c, return_indices=True)
    if common.size == 0:
        raise ValueError(
            f"no common frame counter values between {cam_stream_name!r}"
            f" and {hdf_stream_name!r}; cannot pair events"
        )
    cam_t_pair = cam_t[ci]
    hdf_t_pair = hdf_t[hi]
    delta = hdf_t_pair - cam_t_pair  # D1 per frame

    # Filter to in-scan frames using the motor stream.  Dedup motor
    # timestamps (same as _interpolate_positions does internally).
    m_order = np.argsort(motor_t_raw, kind="stable")
    m_t_sorted = np.asarray(motor_t_raw, dtype=float)[m_order]
    m_p_sorted = np.asarray(motor_pos_raw, dtype=float)[m_order]
    _, m_uidx = np.unique(m_t_sorted, return_index=True)
    m_uidx.sort()
    m_t = m_t_sorted[m_uidx]
    m_p = m_p_sorted[m_uidx]

    # Drop pairings outside the motor stream's time range.
    in_motor_range = (hdf_t_pair >= m_t[0]) & (hdf_t_pair <= m_t[-1])
    hdf_pos = np.full(hdf_t_pair.shape, np.nan, dtype=float)
    hdf_pos[in_motor_range] = np.interp(
        hdf_t_pair[in_motor_range],
        m_t,
        m_p,
    )
    in_scan = in_motor_range & (hdf_pos >= p_start) & (hdf_pos <= p_end)
    n_in_scan = int(in_scan.sum())
    if n_in_scan < 2:
        raise ValueError(
            f"only {n_in_scan} frame(s) in scan range [{p_start}, {p_end}];"
            " need >= 2 for meaningful statistics"
        )

    delta_in_scan = delta[in_scan]
    d1_mean = float(delta_in_scan.mean())
    d1_std = float(delta_in_scan.std())
    d1_min = float(delta_in_scan.min())
    d1_max = float(delta_in_scan.max())

    hdf_t_in_scan_sorted = np.sort(hdf_t_pair[in_scan])
    d2_mean = (
        float(np.diff(hdf_t_in_scan_sorted).mean()) if n_in_scan >= 2 else float("nan")
    )

    # Pick the closest-matching semantic.  Candidates: predicted
    # hdf_t - cam_t for each semantic.  Tie-break preferring
    # end_acquire (the most common AD HDF plugin behavior).
    candidates = [
        ("end_acquire", 0.0, -t_acquire),
        ("start_acquire", -t_acquire, 0.0),
        ("end_period", t_period - t_acquire, -t_period),
    ]
    # Each tuple: (name, predicted_d1_seconds, phase_offset_to_get_start_acquire)
    # Picker: minimize |observed_d1 - predicted_d1|; ties broken by
    # the list order above (end_acquire wins ties).
    best_name = None
    best_offset = None
    best_resid = float("inf")
    for name, predicted_d1, offset in candidates:
        resid = abs(d1_mean - predicted_d1)
        if resid < best_resid:
            best_resid = resid
            best_name = name
            best_offset = offset

    # Reliability checks: this diagnostic only works if the two
    # streams carry one timestamp-per-counter-increment AND those
    # timestamps are stable from one frame to the next.  Two known
    # ways the IOC's CA monitor publish path breaks both assumptions:
    #
    #   1. CA monitor coalescing: rapidly-incrementing integer
    #      counters publish fewer monitor events than increments.
    #      Symptom: D2 (mean inter-event gap) >> t_period.
    #   2. Per-event timestamp jitter: the monitor's timestamp is
    #      when the publish was queued, not when the underlying
    #      record processed; jitter can be many ms.  Symptom: D1
    #      stddev comparable to or larger than t_acquire (the
    #      time-scale we're trying to discriminate among the
    #      candidate semantics).
    #
    # If either check fails, mark the verdict unreliable.  The
    # caller should treat the verdict as a hint, not a conclusion.
    sparseness_factor = (d2_mean / t_period) if t_period > 0 else float("nan")
    sparse_data = d2_mean > 2.0 * t_period
    noisy_data = d1_std > t_acquire
    # Also: if the best residual is larger than the spread within
    # the candidates, no candidate is meaningfully closer than any
    # other.  Spread = max predicted - min predicted (over candidates).
    predicted_d1s = [p for _n, p, _o in candidates]
    candidate_spread = max(predicted_d1s) - min(predicted_d1s)
    indecisive = best_resid > 0.5 * candidate_spread
    is_reliable = not (sparse_data or noisy_data or indecisive)

    # Render a human-readable report.
    print("hdf_timestamp_semantic_diagnostic:")
    print(
        f"  frames paired:  cam={cam_c.size}  hdf={hdf_c.size}"
        f"  common={common.size}  in-scan={n_in_scan}"
    )
    print(
        f"  D1 (hdf_t - cam_t, in-scan): "
        f"mean={d1_mean * 1000:+8.3f} ms  std={d1_std * 1000:6.3f} ms"
        f"  min={d1_min * 1000:+8.3f}  max={d1_max * 1000:+8.3f}"
    )
    print(
        f"  D2 (diff(hdf_t) in-scan):    "
        f"mean={d2_mean * 1000:8.3f} ms"
        f"  (expected t_period = {t_period * 1000:.3f} ms,"
        f" sparseness x{sparseness_factor:.1f})"
    )
    print(
        f"  t_acquire = {t_acquire * 1000:.3f} ms,  t_period = {t_period * 1000:.3f} ms"
    )
    print("")
    print("  candidate semantics for hdf_t (predicted D1 vs observed):")
    for name, predicted_d1, _offset in candidates:
        marker = " <- " if name == best_name else "    "
        print(
            f"    {marker}{name:14s}"
            f"  predicted D1 = {predicted_d1 * 1000:+8.3f} ms"
            f"   residual = {abs(d1_mean - predicted_d1) * 1000:7.3f} ms"
        )
    print("")
    if is_reliable:
        print(f"  verdict: hdf_t ~= {best_name}  (RELIABLE)")
        print(f"  recommended hdf_t_phase_offset = {best_offset:+.6f} s")
        print("    (use as: start_acquire = hdf_t + hdf_t_phase_offset)")
    else:
        # Verdict is still reported (so the dict is always populated)
        # but the user is warned not to trust it without more data.
        print(f"  verdict: hdf_t ~= {best_name}  *** UNRELIABLE ***")
        print("  reasons:")
        if sparse_data:
            print(
                f"    - sparse: D2 (mean inter-event gap) = "
                f"{d2_mean * 1000:.1f} ms > 2 x t_period "
                f"({2 * t_period * 1000:.1f} ms);"
                f" CA monitor publish path is coalescing events,"
                f" so cam-vs-hdf timestamps may not correspond to"
                f" the same physical moment."
            )
        if noisy_data:
            print(
                f"    - noisy: D1 stddev = {d1_std * 1000:.1f} ms"
                f" > t_acquire ({t_acquire * 1000:.1f} ms);"
                f" per-event timestamp jitter is too large to"
                f" discriminate among the candidate semantics"
                f" (which differ by units of t_acquire / t_period)."
            )
        if indecisive:
            print(
                f"    - indecisive: best residual"
                f" ({best_resid * 1000:.1f} ms) > half the spread of"
                f" candidate predictions"
                f" ({0.5 * candidate_spread * 1000:.1f} ms);"
                f" no candidate is meaningfully closer than any other."
            )
        print(
            "  recommended action: do NOT trust this verdict for"
            " production phase-offset choices.  Re-run the diagnostic"
            " against a longer/cleaner run, or investigate IOC monitor"
            " coalescing (e.g. lower the cam frame rate so the CA"
            " publish path can keep up with every counter increment)."
        )

    return {
        "n_in_scan_frames": n_in_scan,
        "d1_mean_s": d1_mean,
        "d1_std_s": d1_std,
        "d2_mean_s": d2_mean,
        "t_acquire": t_acquire,
        "t_period": t_period,
        "verdict": best_name,
        "recommended_hdf_t_phase_offset_s": best_offset,
        "is_reliable": is_reliable,
        "sparse_data": sparse_data,
        "noisy_data": noisy_data,
        "indecisive": indecisive,
    }


def write_flyscan_data(
    master_file,
    external_file,
    df,
    *,
    external_addr="/entry/data",
    n_frames_expected=None,
):
    """Write the ``/entry/flyscan_data`` group into the NeXus master file.

    This is the single primary-product group: an ``NXdata`` holding the
    in-scan image substack (an ``h5py.VirtualLayout`` into the external
    area-detector file, no bytes copied) plus the per-frame correlation
    data, all from the authoritative AD HDF1 file.

    Both the live flyscan plan and the offline repair tool call this so
    the on-disk layout is identical regardless of when it is written.
    Any pre-existing ``/entry/flyscan_data`` is replaced (idempotent).

    Parameters
    ----------
    master_file : str
        Path to the NeXus master HDF5 file (opened for append).
    external_file : str
        Path to the area-detector HDF1 file, resolvable from the
        master file's directory (i.e. through the image-files symlink).
    df : pandas.DataFrame
        Output of ``pair_frames_to_positions_from_ad_file``; one row
        per in-scan frame with ``image_number``, ``timestamp``, and the
        three ``position_*`` columns.
    external_addr : str
        Group inside ``external_file`` holding ``data`` (the image
        stack).  Defaults to ``/entry/data``.
    n_frames_expected : int or None
        Total acquired-frame count, recorded as provenance.  ``None``
        omits the attribute.

    Returns
    -------
    dict
        Summary: ``n_frames_paired``, ``out_shape``, ``src_dtype``.
    """
    import h5py

    flyscan_data_addr = "/entry/flyscan_data"
    # frame_index = image_number - 1: IOC array_counter is 1-based,
    # HDF5 dataset axes are 0-based.
    image_number_arr = df["image_number"].to_numpy()
    frame_index_arr = image_number_arr - 1
    n_frames_paired = int(len(df))

    with h5py.File(external_file, "r") as src:
        src_ds = src[external_addr + "/data"]
        src_shape = src_ds.shape  # (N, H, W)
        src_dtype = src_ds.dtype
    # VirtualLayout: out-shape (n_in_scan, H, W), each row sourced
    # from src[frame_index[i], :, :].
    n_in_scan = len(frame_index_arr)
    out_shape = (n_in_scan,) + tuple(src_shape[1:])
    layout = h5py.VirtualLayout(shape=out_shape, dtype=src_dtype)
    vsrc = h5py.VirtualSource(
        external_file,
        name=external_addr + "/data",
        shape=src_shape,
        dtype=src_dtype,
    )
    for out_i, src_i in enumerate(frame_index_arr):
        layout[out_i] = vsrc[int(src_i)]

    with h5py.File(master_file, "a") as root:
        if flyscan_data_addr in root:
            del root[flyscan_data_addr]
        fs_grp = root.create_group(flyscan_data_addr)
        fs_grp.attrs["NX_class"] = "NXdata"
        fs_grp.attrs["signal"] = "data"
        fs_grp.attrs["axes"] = ["position_start_acquire"]

        # Provenance: this group is sourced entirely from the
        # authoritative AD HDF1 file.
        fs_grp.attrs["source"] = "ad_file"
        fs_grp.attrs["source_description"] = (
            "Per-frame data read from the authoritative area-detector"
            " HDF1 file (lossless, one row per acquired frame)."
        )
        fs_grp.attrs["n_frames_paired"] = n_frames_paired
        if n_frames_expected is not None:
            fs_grp.attrs["n_frames_expected"] = int(n_frames_expected)

        # Update the path to the NeXus default plot.
        root["/entry"].attrs["default"] = "flyscan_data"

        # Primary signal: the in-scan image substack.
        fs_grp.create_virtual_dataset("data", layout)

        # Plot axes (the position arrays).
        fs_grp.create_dataset(
            "position_start_acquire",
            data=df["position_start_acquire"].to_numpy(),
        )
        fs_grp.create_dataset(
            "position_end_acquire",
            data=df["position_end_acquire"].to_numpy(),
        )
        fs_grp.create_dataset(
            "position_end_period",
            data=df["position_end_period"].to_numpy(),
        )

        # Subordinate per-frame correlation data.
        ds_img = fs_grp.create_dataset("image_number", data=image_number_arr)
        ds_img.attrs["description"] = (
            "IOC-side hdf1.array_counter value at frame capture; 1-based"
            " per EPICS areaDetector NDFileHDF5 plugin convention."
        )
        ds_idx = fs_grp.create_dataset("frame_index", data=frame_index_arr)
        ds_idx.attrs["target"] = "/entry/images/data"
        ds_idx.attrs["description"] = (
            "0-based index into /entry/images/data along its first axis;"
            " equal to image_number - 1.  /entry/flyscan_data/data is"
            " already this substack; use frame_index only to map back to"
            " the full /entry/images/data stack:"
            " images = f['/entry/images/data'];"
            " idx = f['/entry/flyscan_data/frame_index'][:];"
            " in_scan_images = images[idx, :, :]"
        )
        fs_grp.create_dataset("timestamp", data=df["timestamp"].to_numpy())

    logger.info(
        "write_flyscan_data: wrote %s (virtual 'data' shape=%r dtype=%r"
        " from %s::%s, %d in-scan frame(s), frame_index 0-based %d..%d)"
        " into %s and set /entry@default='flyscan_data'",
        flyscan_data_addr,
        out_shape,
        src_dtype,
        external_file,
        external_addr + "/data",
        n_frames_paired,
        int(frame_index_arr[0]),
        int(frame_index_arr[-1]),
        master_file,
    )
    return {
        "n_frames_paired": n_frames_paired,
        "out_shape": out_shape,
        "src_dtype": src_dtype,
    }
