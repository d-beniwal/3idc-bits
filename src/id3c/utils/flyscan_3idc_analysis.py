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
    from id3c.utils.flyscan_3idc_analysis import pair_frames_to_positions
    df = pair_frames_to_positions(run)
    # df columns: image_number, timestamp, position
    # df.index: absolute timestamp (float seconds since epoch)
    df.to_csv("scan.csv")

Design notes
------------

- IOC timestamps are the system of record for pairing (per the
  ``flyscan_3idc`` strategy doc, Phase 0.2 / Phase 0e).  The
  primary-stream snapshots from the plan are a progress indicator;
  this module's output is the high-fidelity pairing.
- Empirically (verified during the 2026-06-08 commissioning
  session against ``adsimdet`` + ``gp:m1``):
  - the m1 monitor stream's record-order is interleaved across
    multiple CA dispatcher segments; sorting by timestamp yields
    a strictly increasing position trace at constant velocity in
    the in-scan window.
  - the HDF array_counter monitor stream's record-order is also
    interleaved, but sorting by timestamp yields strictly
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
) -> pd.DataFrame:
    """Pair HDF frames with linearly-interpolated motor positions.

    Pure-array core; no run object, no ophyd, no bluesky.  Tests
    construct inputs directly.

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
        Scan range in motor engineering units.  Frames whose
        interpolated positions fall outside ``[p_start, p_end]``
        are dropped.

    Returns
    -------
    pandas.DataFrame
        Columns: ``image_number`` (int), ``timestamp`` (float),
        ``position`` (float).  Indexed by ``timestamp``.  Sorted
        by timestamp ascending.  Rows are only those frames inside
        ``[p_start, p_end]`` *and* whose timestamps fall within
        the motor stream's time range.
    """
    motor_t = np.asarray(motor_t, dtype=float)
    motor_pos = np.asarray(motor_pos, dtype=float)
    hdf_t = np.asarray(hdf_t, dtype=float)
    hdf_counter = np.asarray(hdf_counter, dtype=np.int64)

    if motor_t.shape != motor_pos.shape:
        raise ValueError(
            f"motor_t shape {motor_t.shape} != motor_pos shape" f" {motor_pos.shape}"
        )
    if hdf_t.shape != hdf_counter.shape:
        raise ValueError(
            f"hdf_t shape {hdf_t.shape} != hdf_counter shape" f" {hdf_counter.shape}"
        )
    if motor_t.size < 2:
        raise ValueError(
            f"motor stream has {motor_t.size} sample(s); need >= 2" " for interpolation"
        )
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

    # Drop HDF frames outside the motor stream's time range
    # (extrapolation rejected, never silent).
    t_lo, t_hi = m_t[0], m_t[-1]
    in_time_range = (h_t >= t_lo) & (h_t <= t_hi)
    n_out_of_range = h_t.size - int(in_time_range.sum())
    if n_out_of_range:
        logger.warning(
            "_interpolate_positions: dropping %d HDF frame(s) with"
            " timestamps outside motor stream range"
            " [%g, %g] (would require extrapolation)",
            n_out_of_range,
            t_lo,
            t_hi,
        )
    h_t_keep = h_t[in_time_range]
    h_c_keep = h_c[in_time_range]
    if h_t_keep.size == 0:
        return _empty_result()

    # Linear interpolation — np.interp requires monotonic xp; we
    # ensured that via sort+unique.
    positions = np.interp(h_t_keep, m_t, m_p)

    # Trim to in-scan range [p_start, p_end].
    in_scan = (positions >= p_start) & (positions <= p_end)
    n_out_of_scan = h_t_keep.size - int(in_scan.sum())
    if n_out_of_scan:
        logger.info(
            "_interpolate_positions: dropping %d HDF frame(s) with"
            " interpolated position outside [%g, %g] (taxi-in / "
            "coast-out frames)",
            n_out_of_scan,
            p_start,
            p_end,
        )

    df = pd.DataFrame(
        {
            "image_number": h_c_keep[in_scan].astype(np.int64),
            "timestamp": h_t_keep[in_scan].astype(float),
            "position": positions[in_scan].astype(float),
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
            "position": np.array([], dtype=float),
        }
    )
    df = df.set_index("timestamp", drop=False)
    return df


def pair_frames_to_positions(run) -> pd.DataFrame:
    """Pair each in-scan HDF frame with the interpolated motor position.

    Reads everything from the run's start-document metadata and the
    standard monitor streams set up by ``flyscan_3idc.flyscan``:

    - ``<flymotor_name>_monitor`` — motor position vs IOC timestamp.
    - ``<det_name>_hdf1_array_counter_monitor`` — HDF
      ``array_counter`` vs IOC timestamp.
    - ``p_start`` / ``p_end`` from ``run.metadata["start"]``.

    Frames captured outside ``[p_start, p_end]`` are dropped.
    Frames whose IOC timestamps fall outside the motor stream's
    time range are dropped (extrapolation never happens silently).

    Parameters
    ----------
    run : BlueskyRun
        Tiled / databroker run object.  Must have
        ``.metadata["start"]`` with the keys ``p_start``,
        ``p_end``, ``flymotor_name``, ``det_name``, and must
        expose monitor streams named per the convention above.

    Returns
    -------
    pandas.DataFrame
        Columns: ``image_number`` (int64, the HDF array_counter
        value at frame capture), ``timestamp`` (float, IOC time
        of capture), ``position`` (float, motor position in
        engineering units, linearly interpolated).  Indexed by
        ``timestamp`` ascending.

    Raises
    ------
    KeyError
        Required metadata key or monitor stream is missing from
        the run.
    ValueError
        Motor stream has fewer than 2 samples (cannot
        interpolate).
    """
    md = _get_start_metadata(run)
    flymotor_name = _require_metadata_key(md, "flymotor_name")
    det_name = _require_metadata_key(md, "det_name")
    p_start = float(_require_metadata_key(md, "p_start"))
    p_end = float(_require_metadata_key(md, "p_end"))

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
        " hdf=%r (%d frame(s)), p_start=%g p_end=%g",
        motor_stream_name,
        motor_t.size,
        hdf_stream_name,
        hdf_t.size,
        p_start,
        p_end,
    )

    df = _interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start,
        p_end,
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
            f"run.metadata['start'] is {type(start).__name__}," " expected dict"
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
            f"run.{stream_name} has no .read() method" f" (got {type(stream).__name__})"
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
