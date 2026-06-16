"""Pure-Python unit tests for ``id3c.utils.flyscan_3idc_analysis``.

No databroker, no tiled, no ophyd, no IOC.  Builds duck-typed run
objects from synthetic monitor-stream data and asserts the
pairing function produces correct ``(image_number, position)``
rows.

Run with::

    pytest src/id3c/tests/test_flyscan_3idc_analysis.py -v
"""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from id3c.utils import flyscan_3idc_analysis as fa

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_xarray_dataset(**columns):
    """Return a dict-like object that mimics xarray.Dataset enough for
    ``_array_from_ds`` to extract columns from it.

    Each column value should be a 1-D numpy array.  The returned
    object supports ``ds[key]`` and the result has a ``.data``
    attribute returning the underlying numpy array (mimicking
    xarray.DataArray).
    """
    wrapped = {k: SimpleNamespace(data=np.asarray(v)) for k, v in columns.items()}
    return wrapped


def _fake_stream(dataset_dict):
    """Return a duck-typed stream object whose ``.read()`` returns
    the given dict-of-DataArrays."""
    return SimpleNamespace(read=lambda: dataset_dict)


def _fake_run(
    *,
    p_start,
    p_end,
    flymotor_name="m1",
    det_name="adsimdet",
    motor_t=None,
    motor_pos=None,
    hdf_t=None,
    hdf_counter=None,
    t_acquire=1e-9,
    t_period=1e-9,
    hdf_t_phase_offset=0.0,
    extra_md=None,
):
    """Build a minimal duck-typed run object suitable for
    ``pair_frames_to_positions``.

    The three timing kwargs (``t_acquire``, ``t_period``,
    ``hdf_t_phase_offset``) are passed into the start-doc
    metadata where ``pair_frames_to_positions`` reads them.
    Defaults of (1e-9, 1e-9, 0.0) make the three computed
    per-frame phase timestamps essentially coincide with the raw
    ``hdf_t``, so existing tests built before the three-phase
    refactor can keep their position assertions valid by reading
    from ``position_start_acquire`` (which equals the old
    ``position`` under these defaults).
    """
    motor_stream_name = f"{flymotor_name}_monitor"
    hdf_stream_name = f"{det_name}_hdf1_array_counter_monitor"
    motor_field_name = flymotor_name
    hdf_field_name = f"{det_name}_hdf1_array_counter"

    md = {
        "start": {
            "p_start": p_start,
            "p_end": p_end,
            "flymotor_name": flymotor_name,
            "det_name": det_name,
            "t_acquire": t_acquire,
            "t_period": t_period,
            "hdf_t_phase_offset": hdf_t_phase_offset,
            **(extra_md or {}),
        }
    }
    run = SimpleNamespace(metadata=md)
    if motor_t is not None:
        ds = _fake_xarray_dataset(
            time=motor_t,
            **{motor_field_name: motor_pos},
        )
        setattr(run, motor_stream_name, _fake_stream(ds))
    if hdf_t is not None:
        ds = _fake_xarray_dataset(
            time=hdf_t,
            **{hdf_field_name: hdf_counter},
        )
        setattr(run, hdf_stream_name, _fake_stream(ds))
    return run


# ---------------------------------------------------------------------------
# _interpolate_positions: pure-array core
# ---------------------------------------------------------------------------


def test_interpolate_positions_simple_linear():
    """At constant velocity, each frame should land at the
    velocity * (t - t0) position.

    With t_acquire=1e-9 and hdf_t_phase_offset=0, the three
    per-phase positions all coincide with the position at hdf_t,
    so the old-shape assertion translates to position_start_acquire.
    """
    # Motor: position = 2.0 * t (velocity 2 unit/s), 0<=t<=5.
    motor_t = np.linspace(0.0, 5.0, 51)
    motor_pos = 2.0 * motor_t
    # HDF frames at 0.5, 1.5, 2.5, 3.5, 4.5 -> expect pos 1, 3, 5, 7, 9.
    # Restrict scan range to [0, 10] so all are kept.
    hdf_t = np.array([0.5, 1.5, 2.5, 3.5, 4.5])
    hdf_counter = np.array([10, 11, 12, 13, 14])

    df = fa._interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start=0.0,
        p_end=10.0,
        t_acquire=1e-9,
        t_period=1e-9,
        hdf_t_phase_offset=0.0,
    )
    assert list(df["image_number"]) == [10, 11, 12, 13, 14]
    np.testing.assert_allclose(
        df["position_start_acquire"].to_numpy(),
        [1.0, 3.0, 5.0, 7.0, 9.0],
    )
    np.testing.assert_allclose(
        df["timestamp"].to_numpy(),
        [0.5, 1.5, 2.5, 3.5, 4.5],
    )


def test_interpolate_positions_trims_outside_scan_range():
    """Frames whose interpolated positions fall outside
    [p_start, p_end] are dropped."""
    # Same setup; positions span 0 to 10.
    motor_t = np.linspace(0.0, 5.0, 51)
    motor_pos = 2.0 * motor_t
    # Frames at 0.1, 1.0, 2.5, 4.0, 4.9 -> positions 0.2, 2.0, 5.0, 8.0, 9.8.
    hdf_t = np.array([0.1, 1.0, 2.5, 4.0, 4.9])
    hdf_counter = np.array([0, 1, 2, 3, 4])
    # Scan range [1.0, 8.0] excludes frames 0 (pos 0.2) and 4 (pos 9.8).
    df = fa._interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start=1.0,
        p_end=8.0,
        t_acquire=1e-9,
        t_period=1e-9,
        hdf_t_phase_offset=0.0,
    )
    assert list(df["image_number"]) == [1, 2, 3]
    np.testing.assert_allclose(
        df["position_start_acquire"].to_numpy(),
        [2.0, 5.0, 8.0],
    )


def test_interpolate_positions_handles_out_of_order_motor_stream():
    """Record-order-shuffled motor stream still produces correct
    interpolation (mirrors what we observed in real BlueskyRuns:
    the m1_monitor stream's events are interleaved across CA
    dispatcher segments)."""
    # Real motor trace: position = t (1 unit/s), 0<=t<=10.
    n = 21
    motor_t_sorted = np.linspace(0.0, 10.0, n)
    motor_pos_sorted = motor_t_sorted.copy()
    # Shuffle deterministically.
    rng = np.random.default_rng(seed=42)
    shuffle_idx = rng.permutation(n)
    motor_t = motor_t_sorted[shuffle_idx]
    motor_pos = motor_pos_sorted[shuffle_idx]
    # Frames at 2.5, 5.5, 8.5 -> expect positions 2.5, 5.5, 8.5.
    hdf_t = np.array([2.5, 5.5, 8.5])
    hdf_counter = np.array([0, 1, 2])
    df = fa._interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start=0.0,
        p_end=10.0,
        t_acquire=1e-9,
        t_period=1e-9,
        hdf_t_phase_offset=0.0,
    )
    np.testing.assert_allclose(
        df["position_start_acquire"].to_numpy(),
        [2.5, 5.5, 8.5],
    )


def test_interpolate_positions_handles_duplicate_timestamps():
    """Motor samples with repeated timestamps are deduped (keep
    first occurrence) — observed in the live m1_monitor stream
    where the same readback value appeared at the same IOC
    timestamp on multiple consecutive events."""
    # Mostly linear, but with two duplicate timestamps.
    motor_t = np.array([0.0, 1.0, 1.0, 2.0, 3.0, 3.0, 4.0])
    # Different positions at the duplicate timestamps — function
    # must use the *first* one it sees.
    motor_pos = np.array([0.0, 1.0, 999.0, 2.0, 3.0, 888.0, 4.0])
    hdf_t = np.array([0.5, 1.5, 2.5, 3.5])
    hdf_counter = np.array([0, 1, 2, 3])
    df = fa._interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start=0.0,
        p_end=5.0,
        t_acquire=1e-9,
        t_period=1e-9,
        hdf_t_phase_offset=0.0,
    )
    # Duplicates were deduped; linear interpolation through
    # (0,0), (1,1), (2,2), (3,3), (4,4) yields positions
    # 0.5, 1.5, 2.5, 3.5 — not 999 or 888 anywhere.
    np.testing.assert_allclose(
        df["position_start_acquire"].to_numpy(),
        [0.5, 1.5, 2.5, 3.5],
    )


def test_interpolate_positions_drops_extrapolation(caplog):
    """HDF frames with timestamps outside the motor stream's time
    range are dropped (extrapolation rejected, logged at WARNING).
    """
    motor_t = np.array([10.0, 11.0, 12.0])
    motor_pos = np.array([10.0, 11.0, 12.0])
    # Frames at 5 (before), 11 (in range), 20 (after).
    hdf_t = np.array([5.0, 11.0, 20.0])
    hdf_counter = np.array([0, 1, 2])
    with caplog.at_level("WARNING", logger="flyscan_3idc_analysis"):
        df = fa._interpolate_positions(
            motor_t,
            motor_pos,
            hdf_t,
            hdf_counter,
            p_start=0.0,
            p_end=20.0,
            t_acquire=1e-9,
            t_period=1e-9,
            hdf_t_phase_offset=0.0,
        )
    # Only the in-range frame should survive.
    assert list(df["image_number"]) == [1]
    assert df.iloc[0]["position_start_acquire"] == pytest.approx(11.0)
    # And a warning should have been emitted about the two drops.
    assert any("outside motor stream range" in rec.message for rec in caplog.records)


def test_interpolate_positions_empty_hdf_returns_empty_frame():
    """No HDF frames -> empty result, no crash."""
    motor_t = np.linspace(0, 5, 51)
    motor_pos = motor_t.copy()
    df = fa._interpolate_positions(
        motor_t,
        motor_pos,
        np.array([]),
        np.array([], dtype=np.int64),
        p_start=0.0,
        p_end=5.0,
        t_acquire=1e-9,
        t_period=1e-9,
        hdf_t_phase_offset=0.0,
    )
    assert len(df) == 0
    assert list(df.columns) == [
        "image_number",
        "timestamp",
        "position_start_acquire",
        "position_end_acquire",
        "position_end_period",
    ]


def test_interpolate_positions_requires_two_motor_samples():
    """Single-sample motor stream cannot be interpolated."""
    with pytest.raises(ValueError, match="need >= 2"):
        fa._interpolate_positions(
            np.array([1.0]),
            np.array([1.0]),
            np.array([0.5]),
            np.array([0]),
            p_start=0.0,
            p_end=5.0,
            t_acquire=1e-9,
            t_period=1e-9,
            hdf_t_phase_offset=0.0,
        )


def test_interpolate_positions_shape_mismatch_raises():
    """Shape mismatches between paired arrays raise ValueError."""
    with pytest.raises(ValueError, match="motor_t shape"):
        fa._interpolate_positions(
            np.array([1.0, 2.0]),
            np.array([1.0, 2.0, 3.0]),
            np.array([1.5]),
            np.array([0]),
            p_start=0.0,
            p_end=5.0,
            t_acquire=1e-9,
            t_period=1e-9,
            hdf_t_phase_offset=0.0,
        )
    with pytest.raises(ValueError, match="hdf_t shape"):
        fa._interpolate_positions(
            np.array([1.0, 2.0]),
            np.array([1.0, 2.0]),
            np.array([1.5]),
            np.array([0, 1]),
            p_start=0.0,
            p_end=5.0,
            t_acquire=1e-9,
            t_period=1e-9,
            hdf_t_phase_offset=0.0,
        )


def test_interpolate_positions_three_phase_arithmetic_at_constant_velocity():
    """At constant motor velocity, the three per-phase positions
    must satisfy:

        position_end_acquire = position_start_acquire + v * t_acquire
        position_end_period  = position_start_acquire + v * t_period

    where v is the motor's velocity.  Pure arithmetic check that the
    three np.interp calls use the correct phase timestamps.
    """
    # Motor: position = 2.0 * t (velocity 2.0 unit/s), 0 <= t <= 10.
    motor_t = np.linspace(0.0, 10.0, 101)
    motor_pos = 2.0 * motor_t
    velocity = 2.0
    # Three frames in the middle of the motor range.
    hdf_t = np.array([3.0, 5.0, 7.0])
    hdf_counter = np.array([10, 11, 12], dtype=np.int64)
    t_acquire = 0.1
    t_period = 0.5
    # Phase 0 verdict: hdf_t ≈ end_acquire, so offset = -t_acquire.
    hdf_t_phase_offset = -t_acquire
    df = fa._interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start=0.0,
        p_end=20.0,
        t_acquire=t_acquire,
        t_period=t_period,
        hdf_t_phase_offset=hdf_t_phase_offset,
    )
    # start_acquire = hdf_t - t_acquire = hdf_t - 0.1
    # position_start_acquire = 2.0 * (hdf_t - 0.1)
    expected_start = 2.0 * (hdf_t - 0.1)
    expected_end_acquire = expected_start + velocity * t_acquire
    expected_end_period = expected_start + velocity * t_period
    np.testing.assert_allclose(df["position_start_acquire"].to_numpy(), expected_start)
    np.testing.assert_allclose(
        df["position_end_acquire"].to_numpy(), expected_end_acquire
    )
    np.testing.assert_allclose(
        df["position_end_period"].to_numpy(), expected_end_period
    )
    # Cross-check: end_acquire - start_acquire == v * t_acquire.
    diff_acq = (
        df["position_end_acquire"].to_numpy() - df["position_start_acquire"].to_numpy()
    )
    np.testing.assert_allclose(diff_acq, velocity * t_acquire)
    # And end_period - start_acquire == v * t_period.
    diff_per = (
        df["position_end_period"].to_numpy() - df["position_start_acquire"].to_numpy()
    )
    np.testing.assert_allclose(diff_per, velocity * t_period)


def test_interpolate_positions_in_scan_filter_uses_start_acquire():
    """The in-scan filter uses position_start_acquire (not the
    other two phases).  Construct a frame whose start_acquire is
    just inside p_start but whose end_acquire/end_period are
    further into the scan: the frame is kept (start_acquire in
    scan).  Conversely a frame whose start_acquire is just outside
    p_end but whose end_acquire is past p_end is dropped (start
    out of scan).
    """
    # Motor: position = t (velocity 1.0 unit/s), 0 <= t <= 10.
    motor_t = np.linspace(0.0, 10.0, 101)
    motor_pos = motor_t.copy()
    t_acquire = 0.5
    t_period = 1.0
    hdf_t_phase_offset = -t_acquire
    # Frame A: hdf_t = 1.5 -> start_acquire = 1.0 (= p_start exactly).
    # Frame B: hdf_t = 5.5 -> start_acquire = 5.0 (= p_end exactly).
    # Frame C: hdf_t = 5.6 -> start_acquire = 5.1 (> p_end; dropped).
    hdf_t = np.array([1.5, 5.5, 5.6])
    hdf_counter = np.array([0, 1, 2], dtype=np.int64)
    df = fa._interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start=1.0,
        p_end=5.0,
        t_acquire=t_acquire,
        t_period=t_period,
        hdf_t_phase_offset=hdf_t_phase_offset,
    )
    # Frame A (start_acquire=1.0) and B (start_acquire=5.0) are kept
    # (both endpoints inclusive).  Frame C (start_acquire=5.1) is dropped.
    assert list(df["image_number"]) == [0, 1]


def test_interpolate_positions_widening_admits_leading_edge_frame():
    """Widening admits a leading-edge frame the old rule rejected.

    Frame's [start_acquire.t, end_period.t] overlaps the motor's
    in-range window even though start_acquire is before p_start.
    """
    # Motor: position = t (velocity 1.0 unit/s), 0 <= t <= 10.
    motor_t = np.linspace(0.0, 10.0, 101)
    motor_pos = motor_t.copy()
    t_acquire = 0.5
    t_period = 1.0
    hdf_t_phase_offset = -t_acquire

    # Leading-edge frame: hdf_t=0.8 -> start_acquire=0.3, end_period=1.3.
    # start_acquire < p_start=1.0, so OLD rule rejects.
    # [0.3, 1.3] overlaps motor in-range window [1.0, 5.0] at [1.0, 1.3],
    # so NEW rule admits.
    hdf_t = np.array([0.8, 2.0, 3.0])
    hdf_counter = np.array([0, 1, 2], dtype=np.int64)
    df = fa._interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start=1.0,
        p_end=5.0,
        t_acquire=t_acquire,
        t_period=t_period,
        hdf_t_phase_offset=hdf_t_phase_offset,
    )
    assert list(df["image_number"]) == [
        0,
        1,
        2,
    ], "leading-edge frame 0 should be admitted by the widened criterion"


def test_interpolate_positions_widening_admits_trailing_edge_frame():
    """Trailing-edge analogue of the leading-edge test.

    A frame whose start_acquire is just inside p_end but whose
    end_period crosses past p_end is admitted (its exposure
    started inside the scan range).  In contrast a frame whose
    entire interval is past the motor's last in-range timestamp
    is still rejected.
    """
    motor_t = np.linspace(0.0, 10.0, 101)
    motor_pos = motor_t.copy()
    t_acquire = 0.5
    t_period = 1.0
    hdf_t_phase_offset = -t_acquire

    # Trailing-edge frame: hdf_t=5.4 -> start_acquire=4.9, end_period=5.9.
    # [4.9, 5.9] overlaps motor in-range window [1.0, 5.0] at [4.9, 5.0].
    # Out-frame: hdf_t=6.5 -> start_acquire=6.0, end_period=7.0. No overlap.
    hdf_t = np.array([3.0, 5.4, 6.5])
    hdf_counter = np.array([0, 1, 2], dtype=np.int64)
    df = fa._interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start=1.0,
        p_end=5.0,
        t_acquire=t_acquire,
        t_period=t_period,
        hdf_t_phase_offset=hdf_t_phase_offset,
    )
    assert list(df["image_number"]) == [
        0,
        1,
    ], "trailing-edge frame 1 should be admitted; far-out frame 2 dropped"


def test_interpolate_positions_widening_rejects_all_when_motor_never_in_range():
    """No frames are admitted if the motor never enters the range."""
    # Motor ranges 0..0.5; scan range [1.0, 5.0] -- motor never enters.
    motor_t = np.linspace(0.0, 10.0, 101)
    motor_pos = np.linspace(0.0, 0.5, 101)
    hdf_t = np.array([1.0, 2.0, 3.0])
    hdf_counter = np.array([0, 1, 2], dtype=np.int64)
    df = fa._interpolate_positions(
        motor_t,
        motor_pos,
        hdf_t,
        hdf_counter,
        p_start=1.0,
        p_end=5.0,
        t_acquire=0.1,
        t_period=0.1,
        hdf_t_phase_offset=0.0,
    )
    assert len(df) == 0


def test_interpolate_positions_dedups_duplicate_image_numbers(caplog):
    """Duplicate image_number values within the in-scan window are
    deduped (keep first occurrence) with a WARNING.  Mirrors the
    motor-timestamp dedup behavior at the counter level.
    """
    # Motor: position = t, 0 <= t <= 10.
    motor_t = np.linspace(0.0, 10.0, 101)
    motor_pos = motor_t.copy()
    # HDF frames at four timestamps, with counter 1 repeated.  The
    # IOC's CA dispatcher occasionally fires twice for the same
    # counter increment.
    hdf_t = np.array([1.0, 2.0, 2.0, 3.0])
    hdf_counter = np.array([0, 1, 1, 2], dtype=np.int64)
    with caplog.at_level("WARNING", logger="flyscan_3idc_analysis"):
        df = fa._interpolate_positions(
            motor_t,
            motor_pos,
            hdf_t,
            hdf_counter,
            p_start=0.0,
            p_end=10.0,
            t_acquire=1e-9,
            t_period=1e-9,
            hdf_t_phase_offset=0.0,
        )
    # Three unique image numbers should survive.
    assert list(df["image_number"]) == [0, 1, 2]
    # A WARNING with the duplicate value(s) should have been emitted.
    assert any(
        "duplicate image_number" in rec.message and "1" in rec.message
        for rec in caplog.records
    ), [rec.message for rec in caplog.records]


# ---------------------------------------------------------------------------
# pair_frames_to_positions: end-to-end with a fake run object
# ---------------------------------------------------------------------------


def test_pair_frames_to_positions_end_to_end():
    """Build a fake BlueskyRun-shaped object and pair frames."""
    # Realistic scenario: scan_velocity=0.5 deg/s, in-scan [0, 5].
    # Motor at 10 Hz from -1 to 6 (taxi-in to coast-out): 14.0s of
    # motion, 141 samples.
    n = 141
    motor_t = np.linspace(1000.0, 1014.0, n)
    motor_pos = 0.5 * (motor_t - 1002.0)  # crosses 0 at t=1002, 5 at t=1012
    # HDF: 5 frames spaced through the in-scan window.
    hdf_t = np.array([1003.0, 1005.0, 1007.0, 1009.0, 1011.0])
    hdf_counter = np.array([0, 1, 2, 3, 4])
    run = _fake_run(
        p_start=0.0,
        p_end=5.0,
        motor_t=motor_t,
        motor_pos=motor_pos,
        hdf_t=hdf_t,
        hdf_counter=hdf_counter,
    )
    df = fa.pair_frames_to_positions(run)
    assert list(df["image_number"]) == [0, 1, 2, 3, 4]
    # Position at frame's timestamp = 0.5 * (t - 1002).  With
    # t_acquire = t_period = 1e-9 and hdf_t_phase_offset = 0, all
    # three per-phase positions ~= position at hdf_t.
    expected = 0.5 * (hdf_t - 1002.0)
    np.testing.assert_allclose(df["position_start_acquire"].to_numpy(), expected)
    # Timestamps preserved.
    np.testing.assert_allclose(df["timestamp"].to_numpy(), hdf_t)


def test_pair_frames_to_positions_trims_taxi_frames():
    """Frames captured during taxi-in (motor below p_start) are
    dropped — this is the empirically-observed flyscan_3idc
    behaviour where the cam delivers a first frame at p_initial."""
    n = 141
    motor_t = np.linspace(1000.0, 1014.0, n)
    motor_pos = 0.5 * (motor_t - 1002.0)
    # Frame 0 captured at t=1000.5 -> position -0.75 (taxi-in).
    # Frame 1-3 in scan range.
    # Frame 4 captured at t=1013 -> position 5.5 (coast-out).
    hdf_t = np.array([1000.5, 1003.0, 1006.0, 1009.0, 1013.0])
    hdf_counter = np.array([0, 1, 2, 3, 4])
    run = _fake_run(
        p_start=0.0,
        p_end=5.0,
        motor_t=motor_t,
        motor_pos=motor_pos,
        hdf_t=hdf_t,
        hdf_counter=hdf_counter,
    )
    df = fa.pair_frames_to_positions(run)
    # Frames 0 and 4 are outside [0, 5] -> dropped.
    assert list(df["image_number"]) == [1, 2, 3]


def test_pair_frames_to_positions_index_is_timestamp():
    """The returned DataFrame is indexed by timestamp."""
    n = 41
    motor_t = np.linspace(0.0, 10.0, n)
    motor_pos = motor_t.copy()
    hdf_t = np.array([2.0, 5.0, 8.0])
    hdf_counter = np.array([0, 1, 2])
    run = _fake_run(
        p_start=0.0,
        p_end=10.0,
        motor_t=motor_t,
        motor_pos=motor_pos,
        hdf_t=hdf_t,
        hdf_counter=hdf_counter,
    )
    df = fa.pair_frames_to_positions(run)
    assert df.index.name == "timestamp"
    np.testing.assert_allclose(df.index.to_numpy(), hdf_t)


def test_pair_frames_to_positions_missing_metadata_raises():
    """Missing start metadata -> KeyError with helpful message."""
    n = 11
    motor_t = np.linspace(0.0, 1.0, n)
    motor_pos = motor_t.copy()
    hdf_t = np.array([0.5])
    hdf_counter = np.array([0])
    run = _fake_run(
        p_start=0.0,
        p_end=1.0,
        motor_t=motor_t,
        motor_pos=motor_pos,
        hdf_t=hdf_t,
        hdf_counter=hdf_counter,
    )
    # Strip out a required key.
    del run.metadata["start"]["p_end"]
    with pytest.raises(KeyError, match="p_end"):
        fa.pair_frames_to_positions(run)


def test_pair_frames_to_positions_missing_motor_stream_raises():
    """Missing motor stream -> KeyError."""
    n = 11
    motor_t = np.linspace(0.0, 1.0, n)
    motor_pos = motor_t.copy()
    hdf_t = np.array([0.5])
    hdf_counter = np.array([0])
    run = _fake_run(
        p_start=0.0,
        p_end=1.0,
        motor_t=motor_t,
        motor_pos=motor_pos,
        hdf_t=hdf_t,
        hdf_counter=hdf_counter,
    )
    # Strip the motor monitor stream.
    delattr(run, "m1_monitor")
    with pytest.raises(KeyError, match="m1_monitor"):
        fa.pair_frames_to_positions(run)


# ---------------------------------------------------------------------------
# Round-trip: realistic data matching the 17:21 scan we measured
# ---------------------------------------------------------------------------


def test_pair_frames_to_positions_against_observed_run_shape():
    """Build the same data we observed in the 17:21 run (scan_id=6,
    UID 0f397d39) and confirm the pairing matches what an analyst
    would expect.

    The HDF counter stream was [0..36] sorted-by-time, spanning
    12.27 s.  Motor stream was 127 samples spanning the
    taxi+scan+coast range with constant scan_velocity=0.495 deg/s
    in the in-scan window.

    This isn't an exact replay (we don't have a frame-by-frame
    timestamp dump), but the shape is realistic and tests the
    integration."""
    # Simulate: scan from p=0 to p=5 at v=0.495 deg/s, plus
    # taxi-in/coast-out at the same velocity for simplicity.
    p_start, p_end = 0.0, 5.0
    p_initial, p_final = -0.55, 5.55
    v = 0.495
    # Motor samples at 10 Hz for the full taxi+scan+coast (~12.3 s):
    t0 = 1.78095726e9
    motor_t = np.arange(t0, t0 + 13.0, 0.1)
    motor_pos = p_initial + v * (motor_t - motor_t[0])
    # Trim motor_pos > p_final
    keep = motor_pos <= p_final + 0.01
    motor_t = motor_t[keep]
    motor_pos = motor_pos[keep]
    # HDF at 3 Hz: 37 frames spanning the same window.
    hdf_t = t0 + np.linspace(0.0, 12.27, 37)
    hdf_counter = np.arange(0, 37, dtype=np.int64)
    run = _fake_run(
        p_start=p_start,
        p_end=p_end,
        motor_t=motor_t,
        motor_pos=motor_pos,
        hdf_t=hdf_t,
        hdf_counter=hdf_counter,
    )
    df = fa.pair_frames_to_positions(run)
    # The motor takes 0.55/0.495 ≈ 1.11 s to reach p_start from
    # p_initial, and 5.55/0.495 ≈ 11.21 s to reach p_end.  So
    # frames with hdf_t in [t0+1.11, t0+11.21] are in-scan.
    # At 3 Hz that's ~30 of the 37 frames.
    assert (
        25 < len(df) < 37
    ), f"expected most-but-not-all frames in scan range, got {len(df)}"
    # All paired positions must be in [p_start, p_end].
    assert df["position_start_acquire"].min() >= p_start
    assert df["position_start_acquire"].max() <= p_end
    # image_numbers must be a contiguous slice of [0..36].
    img = df["image_number"].to_numpy()
    assert np.all(
        np.diff(img) == 1
    ), f"expected contiguous image_numbers, got {img.tolist()}"
    # And monotonic by timestamp (the index).
    assert df.index.is_monotonic_increasing


# ---------------------------------------------------------------------------
# _array_from_ds: tolerates xarray-shaped, dict-shaped, and DataFrame-shaped
# ---------------------------------------------------------------------------


def test_array_from_ds_handles_xarray_like():
    """Pulls a 1-D array from an xarray-like Dataset."""
    ds = _fake_xarray_dataset(time=[1.0, 2.0, 3.0])
    arr = fa._array_from_ds(ds, "time", "test")
    np.testing.assert_array_equal(arr, [1.0, 2.0, 3.0])


def test_array_from_ds_handles_dataframe_like():
    """Pulls a 1-D array from a pandas DataFrame column."""
    # pandas DataFrame yields columns as Series, which have
    # .to_numpy() but no .data attribute.
    df = pd.DataFrame({"x": [10, 20, 30]})
    arr = fa._array_from_ds(df, "x", "test")
    np.testing.assert_array_equal(arr, [10, 20, 30])


def test_array_from_ds_missing_key_raises():
    """A missing column raises KeyError naming the stream."""
    ds = _fake_xarray_dataset(time=[1.0])
    with pytest.raises(KeyError, match="no column 'missing'"):
        fa._array_from_ds(ds, "missing", "teststream")


# ---------------------------------------------------------------------------
# hdf_timestamp_semantic_diagnostic
# ---------------------------------------------------------------------------


def _fake_diagnostic_run(
    *,
    p_start,
    p_end,
    t_acquire,
    t_period,
    n_frames,
    scan_velocity,
    hdf_minus_cam_offset_s,
    motor_pos_start=None,
    motor_pos_end=None,
    flymotor_name="m1",
    det_name="adsimdet",
):
    """Build a duck-typed run with cam + hdf + motor monitor streams.

    Synthesizes a scenario where:
    - the motor moves at constant ``scan_velocity`` from
      ``motor_pos_start`` to ``motor_pos_end`` (defaults to p_start
      and p_end so all frames are in-scan; override to test
      out-of-scan cases);
    - the cam emits ``cam_array_counter`` events at end-of-acquire moments,
      one per period, with timestamps ``t_first + i * t_period``;
    - the hdf plugin emits ``hdf1_array_counter`` events with timestamps
      offset from the cam events by ``hdf_minus_cam_offset_s``.

    By varying ``hdf_minus_cam_offset_s`` the test can simulate each
    of the three semantics:
        0.0                    -> hdf_t ~= end_acquire
        -t_acquire             -> hdf_t ~= start_acquire
        +(t_period - t_acquire) -> hdf_t ~= end_period
    """
    if motor_pos_start is None:
        motor_pos_start = p_start
    if motor_pos_end is None:
        motor_pos_end = p_end

    motor_stream_name = f"{flymotor_name}_monitor"
    cam_stream_name = f"{det_name}_cam_array_counter_monitor"
    cam_field_name = f"{det_name}_cam_array_counter"
    hdf_stream_name = f"{det_name}_hdf1_array_counter_monitor"
    hdf_field_name = f"{det_name}_hdf1_array_counter"

    # Motor moves through [motor_pos_start, motor_pos_end] at constant
    # scan_velocity.  Build the motor stream over the full duration
    # that covers all cam events.
    t_motor_end = max(
        (motor_pos_end - motor_pos_start) / scan_velocity,
        n_frames * t_period,
    )
    motor_t = np.linspace(0.0, t_motor_end, max(200, n_frames * 4))
    motor_pos = motor_pos_start + scan_velocity * motor_t

    # Cam events: end-of-acquire of frame i is at t_acquire + i*t_period
    # (first exposure runs from t=0 to t=t_acquire).
    cam_n = np.arange(n_frames, dtype=np.int64)
    cam_t = t_acquire + cam_n * t_period

    # HDF events: same counter values, timestamps offset by the
    # caller-supplied delta.
    hdf_n = cam_n.copy()
    hdf_t = cam_t + hdf_minus_cam_offset_s

    md = {
        "start": {
            "p_start": p_start,
            "p_end": p_end,
            "flymotor_name": flymotor_name,
            "det_name": det_name,
            "t_acquire": t_acquire,
            "t_period": t_period,
        }
    }
    run = SimpleNamespace(metadata=md)
    setattr(
        run,
        motor_stream_name,
        _fake_stream(_fake_xarray_dataset(time=motor_t, **{flymotor_name: motor_pos})),
    )
    setattr(
        run,
        cam_stream_name,
        _fake_stream(_fake_xarray_dataset(time=cam_t, **{cam_field_name: cam_n})),
    )
    setattr(
        run,
        hdf_stream_name,
        _fake_stream(_fake_xarray_dataset(time=hdf_t, **{hdf_field_name: hdf_n})),
    )
    return run


def test_hdf_timestamp_diagnostic_picks_end_acquire():
    """When hdf_t == cam_t (the end_acquire semantic), the diagnostic
    picks 'end_acquire' and recommends offset = -t_acquire."""
    run = _fake_diagnostic_run(
        p_start=0.0,
        p_end=5.0,
        t_acquire=0.01,
        t_period=0.1,
        n_frames=51,
        scan_velocity=1.0,
        hdf_minus_cam_offset_s=0.0,
    )
    result = fa.hdf_timestamp_semantic_diagnostic(run)
    assert result["verdict"] == "end_acquire"
    assert result["recommended_hdf_t_phase_offset_s"] == pytest.approx(-0.01)
    # D1 should be ~0 since hdf == cam.
    assert abs(result["d1_mean_s"]) < 1e-9
    # D2 should equal t_period.
    assert result["d2_mean_s"] == pytest.approx(0.1)


def test_hdf_timestamp_diagnostic_picks_start_acquire():
    """When hdf_t == cam_t - t_acquire (the start_acquire semantic),
    the diagnostic picks 'start_acquire' and recommends offset = 0."""
    t_acquire = 0.01
    run = _fake_diagnostic_run(
        p_start=0.0,
        p_end=5.0,
        t_acquire=t_acquire,
        t_period=0.1,
        n_frames=51,
        scan_velocity=1.0,
        hdf_minus_cam_offset_s=-t_acquire,
    )
    result = fa.hdf_timestamp_semantic_diagnostic(run)
    assert result["verdict"] == "start_acquire"
    assert result["recommended_hdf_t_phase_offset_s"] == pytest.approx(0.0)


def test_hdf_timestamp_diagnostic_picks_end_period():
    """When hdf_t == cam_t + (t_period - t_acquire) (the end_period
    semantic), the diagnostic picks 'end_period' and recommends
    offset = -t_period."""
    t_acquire = 0.01
    t_period = 0.1
    run = _fake_diagnostic_run(
        p_start=0.0,
        p_end=5.0,
        t_acquire=t_acquire,
        t_period=t_period,
        n_frames=51,
        scan_velocity=1.0,
        hdf_minus_cam_offset_s=(t_period - t_acquire),
    )
    result = fa.hdf_timestamp_semantic_diagnostic(run)
    assert result["verdict"] == "end_period"
    assert result["recommended_hdf_t_phase_offset_s"] == pytest.approx(-t_period)


def test_hdf_timestamp_diagnostic_picks_closest_with_noise():
    """Realistic case: HDF lags cam by a small plugin-pipeline
    latency (e.g. 1 ms when t_acquire is 10 ms and t_period is
    100 ms).  The end_acquire candidate predicts D1=0 ms; the
    other two predict +/-10 ms or +90 ms.  1 ms is closest to 0
    so the verdict should still be 'end_acquire'.
    """
    run = _fake_diagnostic_run(
        p_start=0.0,
        p_end=5.0,
        t_acquire=0.01,
        t_period=0.1,
        n_frames=51,
        scan_velocity=1.0,
        hdf_minus_cam_offset_s=0.001,  # 1 ms plugin lag
    )
    result = fa.hdf_timestamp_semantic_diagnostic(run)
    assert result["verdict"] == "end_acquire"
    # Recommended offset is still -t_acquire under the end_acquire
    # semantic; the small lag is acknowledged in D1 but doesn't
    # change the categorical choice.
    assert result["recommended_hdf_t_phase_offset_s"] == pytest.approx(-0.01)


def test_hdf_timestamp_diagnostic_returns_expected_dict_keys():
    """Lock the public return-dict contract."""
    run = _fake_diagnostic_run(
        p_start=0.0,
        p_end=5.0,
        t_acquire=0.01,
        t_period=0.1,
        n_frames=51,
        scan_velocity=1.0,
        hdf_minus_cam_offset_s=0.0,
    )
    result = fa.hdf_timestamp_semantic_diagnostic(run)
    expected_keys = {
        "n_in_scan_frames",
        "d1_mean_s",
        "d1_std_s",
        "d2_mean_s",
        "t_acquire",
        "t_period",
        "verdict",
        "recommended_hdf_t_phase_offset_s",
        "is_reliable",
        "sparse_data",
        "noisy_data",
        "indecisive",
    }
    assert set(result.keys()) == expected_keys
    assert result["t_acquire"] == 0.01
    assert result["t_period"] == 0.1
    # The clean-synthetic case (no jitter, exact cadence) should be
    # reliable on all three reliability axes.
    assert result["is_reliable"] is True
    assert result["sparse_data"] is False
    assert result["noisy_data"] is False
    assert result["indecisive"] is False


def test_hdf_timestamp_diagnostic_flags_noisy_data():
    """When per-event D1 jitter is larger than t_acquire, the verdict
    is flagged unreliable via the noisy_data guard.

    Real symptom observed on the gp:m1 + adsimdet IOC at 14:22:52
    on 2026-06-10 (UID 9fac2530): D1 stddev ~ 37 ms with t_acquire =
    10 ms.  The mean alone would falsely suggest a confident verdict;
    the stddev reveals the per-event jitter swamps the inter-candidate
    spacing (which is t_acquire wide).
    """
    # Simulate noisy timestamps: HDF events are nominally at
    # cam_t (end_acquire), but each one is jittered by a uniform
    # random offset in [-50, +50] ms.  That stddev (~29 ms) easily
    # exceeds t_acquire = 10 ms.
    rng = np.random.default_rng(seed=42)
    flymotor_name = "m1"
    det_name = "adsimdet"
    n_frames = 51
    t_acquire = 0.01
    t_period = 0.1
    cam_n = np.arange(n_frames, dtype=np.int64)
    cam_t = t_acquire + cam_n * t_period
    jitter = rng.uniform(-0.050, 0.050, size=n_frames)
    hdf_t = cam_t + jitter

    motor_t = np.linspace(0.0, n_frames * t_period, 200)
    motor_pos = motor_t  # velocity 1.0; spans 0..5.1

    md = {
        "start": {
            "p_start": 0.0,
            "p_end": 5.0,
            "flymotor_name": flymotor_name,
            "det_name": det_name,
            "t_acquire": t_acquire,
            "t_period": t_period,
        }
    }
    run = SimpleNamespace(metadata=md)
    setattr(
        run,
        f"{flymotor_name}_monitor",
        _fake_stream(_fake_xarray_dataset(time=motor_t, **{flymotor_name: motor_pos})),
    )
    setattr(
        run,
        f"{det_name}_cam_array_counter_monitor",
        _fake_stream(
            _fake_xarray_dataset(time=cam_t, **{f"{det_name}_cam_array_counter": cam_n})
        ),
    )
    setattr(
        run,
        f"{det_name}_hdf1_array_counter_monitor",
        _fake_stream(
            _fake_xarray_dataset(
                time=hdf_t, **{f"{det_name}_hdf1_array_counter": cam_n}
            )
        ),
    )

    result = fa.hdf_timestamp_semantic_diagnostic(run)
    assert result["noisy_data"] is True
    assert result["is_reliable"] is False
    # D2 is still ~t_period (cadence is preserved; only per-event
    # phase is jittered), so sparse_data should NOT trip.
    assert result["sparse_data"] is False


def test_hdf_timestamp_diagnostic_flags_sparse_data():
    """When the cam/HDF monitor streams arrive at much less than the
    expected period (CA monitor coalescing), the verdict is flagged
    unreliable via the sparse_data guard.

    Real symptom observed on the gp:m1 + adsimdet IOC at 14:22:52
    on 2026-06-10: D2 mean ~ 343 ms with t_period = 100 ms (about
    3x sparser than the cam was actually producing frames).
    """
    # Construct a scenario where only every 4th frame's counter
    # actually publishes a monitor event.  Cam at 10 Hz nominal;
    # observed at ~2.5 Hz.
    flymotor_name = "m1"
    det_name = "adsimdet"
    n_total = 51
    keep_every = 4
    t_acquire = 0.01
    t_period = 0.1
    cam_n_full = np.arange(n_total, dtype=np.int64)
    cam_t_full = t_acquire + cam_n_full * t_period
    keep = cam_n_full % keep_every == 0
    cam_n = cam_n_full[keep]
    cam_t = cam_t_full[keep]
    hdf_t = cam_t.copy()

    motor_t = np.linspace(0.0, n_total * t_period, 200)
    motor_pos = motor_t

    md = {
        "start": {
            "p_start": 0.0,
            "p_end": 5.0,
            "flymotor_name": flymotor_name,
            "det_name": det_name,
            "t_acquire": t_acquire,
            "t_period": t_period,
        }
    }
    run = SimpleNamespace(metadata=md)
    setattr(
        run,
        f"{flymotor_name}_monitor",
        _fake_stream(_fake_xarray_dataset(time=motor_t, **{flymotor_name: motor_pos})),
    )
    setattr(
        run,
        f"{det_name}_cam_array_counter_monitor",
        _fake_stream(
            _fake_xarray_dataset(time=cam_t, **{f"{det_name}_cam_array_counter": cam_n})
        ),
    )
    setattr(
        run,
        f"{det_name}_hdf1_array_counter_monitor",
        _fake_stream(
            _fake_xarray_dataset(
                time=hdf_t, **{f"{det_name}_hdf1_array_counter": cam_n}
            )
        ),
    )

    result = fa.hdf_timestamp_semantic_diagnostic(run)
    assert result["sparse_data"] is True
    assert result["is_reliable"] is False
    # D1 jitter is zero (hdf == cam exactly), so noisy_data does NOT trip.
    assert result["noisy_data"] is False


def test_hdf_timestamp_diagnostic_raises_when_no_in_scan_frames():
    """If no frames fall in [p_start, p_end] (e.g. the scan window
    is offset from where the motor was), the diagnostic raises
    rather than silently producing meaningless statistics."""
    # Motor actually travels 0 -> 5 (motor_pos_start/end), but the
    # scan range is set to 10 -> 15 so no frame is in scan.
    run = _fake_diagnostic_run(
        p_start=10.0,
        p_end=15.0,
        motor_pos_start=0.0,
        motor_pos_end=5.0,
        t_acquire=0.01,
        t_period=0.1,
        n_frames=51,
        scan_velocity=1.0,
        hdf_minus_cam_offset_s=0.0,
    )
    with pytest.raises(ValueError, match="in scan range"):
        fa.hdf_timestamp_semantic_diagnostic(run)
