"""Tests for ``pair_frames_to_positions_from_ad_file``.

Synthetic AD HDF1 files built via h5py; no IOC, no live detector.
The motor monitor stream is faked via the same SimpleNamespace +
duck-typed stream pattern used by the rest of the analysis tests.
"""

from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from id3c.utils.flyscan_3idc_analysis import EPICS_EPOCH_OFFSET_S
from id3c.utils.flyscan_3idc_analysis import pair_frames_to_positions_from_ad_file


def _fake_xarray_dataset(**columns):
    """Mimic xarray.Dataset for _array_from_ds."""
    return {k: SimpleNamespace(data=np.asarray(v)) for k, v in columns.items()}


def _fake_stream(ds):
    """Duck-typed stream object."""
    return SimpleNamespace(read=lambda: ds)


def _fake_run(
    *,
    p_start,
    p_end,
    t_acquire,
    t_period,
    motor_t,
    motor_pos,
    flymotor_name="m1",
):
    """Minimal run with metadata + motor monitor stream only."""
    md = {
        "start": {
            "p_start": p_start,
            "p_end": p_end,
            "flymotor_name": flymotor_name,
            "t_acquire": t_acquire,
            "t_period": t_period,
        }
    }
    run = SimpleNamespace(metadata=md)
    ds = _fake_xarray_dataset(time=motor_t, **{flymotor_name: motor_pos})
    setattr(run, f"{flymotor_name}_monitor", _fake_stream(ds))
    return run


def _write_ad_file(path, ad_unix_t, unique_ids):
    """Build a synthetic AD HDF1 file at ``path``.

    Stores ``ad_unix_t`` (Unix-epoch seconds) converted to EPICS
    epoch in /entry/instrument/detector/NDAttributes/NDArrayTimeStamp,
    and ``unique_ids`` in NDArrayUniqueId.
    """
    epics_t = np.asarray(ad_unix_t, dtype=float) - EPICS_EPOCH_OFFSET_S
    with h5py.File(path, "w") as f:
        grp = f.create_group("/entry/instrument/detector/NDAttributes")
        grp.create_dataset("NDArrayTimeStamp", data=epics_t)
        grp.create_dataset(
            "NDArrayUniqueId", data=np.asarray(unique_ids, dtype=np.int32)
        )


def test_basic_constant_velocity_recovery(tmp_path):
    """At constant motor velocity, AD-sourced pairing returns the
    expected (position, 1-based image_number) for every in-scan frame."""
    # Motor: pos = t (1 unit/s), 1000 <= t <= 1010 (Unix epoch).
    motor_t = np.linspace(1000.0, 1010.0, 101)
    motor_pos = motor_t - 1000.0  # pos = 0..10

    # 5 frames at Unix-epoch t = 1001.5, 1002.5, ..., 1005.5
    # -> end_acquire timestamps; start_acquire is 0.5 s earlier.
    ad_unix_t = np.array([1001.5, 1002.5, 1003.5, 1004.5, 1005.5])
    unique_ids = np.array([0, 1, 2, 3, 4])

    ad_file = tmp_path / "ad.h5"
    _write_ad_file(ad_file, ad_unix_t, unique_ids)

    run = _fake_run(
        p_start=0.0,
        p_end=10.0,
        t_acquire=0.5,
        t_period=1.0,
        motor_t=motor_t,
        motor_pos=motor_pos,
    )

    df = pair_frames_to_positions_from_ad_file(run, str(ad_file))

    # UID is 0-based; image_number must be 1-based.
    assert list(df["image_number"]) == [1, 2, 3, 4, 5]
    # start_acquire.t = ad_t - t_acquire = ad_t - 0.5, so
    # positions = (ad_t - 0.5) - 1000.
    np.testing.assert_allclose(
        df["position_start_acquire"].to_numpy(),
        [1.0, 2.0, 3.0, 4.0, 5.0],
    )


def test_epics_to_unix_epoch_conversion(tmp_path):
    """The EPICS->Unix conversion is applied exactly once.

    Construct an AD timestamp that, without conversion, would land
    well outside the motor stream's time range (and be dropped as
    extrapolation).  After conversion it falls cleanly inside.
    """
    motor_t = np.linspace(1781560000.0, 1781560010.0, 101)
    motor_pos = motor_t - 1781560000.0  # pos = 0..10

    # Frame timestamps in Unix epoch (mid-scan):
    ad_unix_t = np.array([1781560003.0, 1781560004.0, 1781560005.0])
    unique_ids = np.array([10, 11, 12])

    ad_file = tmp_path / "ad.h5"
    _write_ad_file(ad_file, ad_unix_t, unique_ids)

    run = _fake_run(
        p_start=0.0,
        p_end=10.0,
        t_acquire=0.1,
        t_period=1.0,
        motor_t=motor_t,
        motor_pos=motor_pos,
    )

    df = pair_frames_to_positions_from_ad_file(run, str(ad_file))
    assert list(df["image_number"]) == [11, 12, 13]


def test_image_number_is_one_based(tmp_path):
    """NDArrayUniqueId is 0-based; output image_number must be 1-based."""
    motor_t = np.linspace(1000.0, 1010.0, 101)
    motor_pos = motor_t - 1000.0

    # UIDs start at 0
    ad_unix_t = np.array([1002.0, 1003.0, 1004.0])
    unique_ids = np.array([0, 1, 2])

    ad_file = tmp_path / "ad.h5"
    _write_ad_file(ad_file, ad_unix_t, unique_ids)

    run = _fake_run(
        p_start=0.0,
        p_end=10.0,
        t_acquire=0.1,
        t_period=1.0,
        motor_t=motor_t,
        motor_pos=motor_pos,
    )

    df = pair_frames_to_positions_from_ad_file(run, str(ad_file))
    assert list(df["image_number"]) == [1, 2, 3]


def test_missing_timestamp_dataset_raises(tmp_path):
    """A file without the timestamp dataset raises KeyError."""
    ad_file = tmp_path / "ad.h5"
    with h5py.File(ad_file, "w") as f:
        f.create_group("/entry/instrument/detector/NDAttributes")
        # Only UID, no timestamp.
        f["/entry/instrument/detector/NDAttributes"].create_dataset(
            "NDArrayUniqueId", data=np.array([0, 1, 2], dtype=np.int32)
        )

    motor_t = np.linspace(0.0, 10.0, 11)
    run = _fake_run(
        p_start=0.0,
        p_end=10.0,
        t_acquire=0.1,
        t_period=1.0,
        motor_t=motor_t,
        motor_pos=motor_t,
    )

    with pytest.raises(KeyError, match="NDArrayTimeStamp"):
        pair_frames_to_positions_from_ad_file(run, str(ad_file))


def test_missing_unique_id_dataset_raises(tmp_path):
    """A file without the UID dataset raises KeyError."""
    ad_file = tmp_path / "ad.h5"
    with h5py.File(ad_file, "w") as f:
        grp = f.create_group("/entry/instrument/detector/NDAttributes")
        grp.create_dataset("NDArrayTimeStamp", data=np.array([1.0, 2.0, 3.0]))

    motor_t = np.linspace(0.0, 10.0, 11)
    run = _fake_run(
        p_start=0.0,
        p_end=10.0,
        t_acquire=0.1,
        t_period=1.0,
        motor_t=motor_t,
        motor_pos=motor_t,
    )

    with pytest.raises(KeyError, match="NDArrayUniqueId"):
        pair_frames_to_positions_from_ad_file(run, str(ad_file))


def test_mismatched_lengths_raise(tmp_path):
    """timestamp + UID arrays of different length raise ValueError."""
    ad_file = tmp_path / "ad.h5"
    with h5py.File(ad_file, "w") as f:
        grp = f.create_group("/entry/instrument/detector/NDAttributes")
        grp.create_dataset("NDArrayTimeStamp", data=np.array([1.0, 2.0, 3.0]))
        grp.create_dataset("NDArrayUniqueId", data=np.array([0, 1], dtype=np.int32))

    motor_t = np.linspace(0.0, 10.0, 11)
    run = _fake_run(
        p_start=0.0,
        p_end=10.0,
        t_acquire=0.1,
        t_period=1.0,
        motor_t=motor_t,
        motor_pos=motor_t,
    )

    with pytest.raises(ValueError, match="disagree on length"):
        pair_frames_to_positions_from_ad_file(run, str(ad_file))


def test_custom_dataset_paths(tmp_path):
    """The timestamp / UID HDF5 paths are overridable."""
    motor_t = np.linspace(1000.0, 1010.0, 101)
    motor_pos = motor_t - 1000.0
    ad_unix_t = np.array([1002.0, 1003.0])
    unique_ids = np.array([5, 6])

    ad_file = tmp_path / "ad.h5"
    with h5py.File(ad_file, "w") as f:
        grp = f.create_group("/custom")
        grp.create_dataset("ts", data=ad_unix_t - EPICS_EPOCH_OFFSET_S)
        grp.create_dataset("uid", data=np.asarray(unique_ids, dtype=np.int32))

    run = _fake_run(
        p_start=0.0,
        p_end=10.0,
        t_acquire=0.1,
        t_period=1.0,
        motor_t=motor_t,
        motor_pos=motor_pos,
    )

    df = pair_frames_to_positions_from_ad_file(
        run,
        str(ad_file),
        timestamp_dset="/custom/ts",
        unique_id_dset="/custom/uid",
    )
    assert list(df["image_number"]) == [6, 7]


def test_recovers_frames_that_ca_path_would_drop(tmp_path):
    """The AD path returns every frame the IOC wrote, regardless of
    whether the CA monitor stream would have lost some.

    This is the core motivation: the AD file is authoritative.  The
    test asserts that for N frames written by the IOC, the helper
    returns N (modulo in-range filtering), independent of whether a
    parallel CA monitor stream would have dropped any.
    """
    motor_t = np.linspace(1000.0, 1020.0, 201)
    motor_pos = motor_t - 1000.0  # pos = 0..20

    # 15 frames evenly spaced in time, all in-range:
    ad_unix_t = np.linspace(1002.0, 1016.0, 15)
    unique_ids = np.arange(15)

    ad_file = tmp_path / "ad.h5"
    _write_ad_file(ad_file, ad_unix_t, unique_ids)

    run = _fake_run(
        p_start=1.0,
        p_end=17.0,
        t_acquire=0.1,
        t_period=1.0,
        motor_t=motor_t,
        motor_pos=motor_pos,
    )

    df = pair_frames_to_positions_from_ad_file(run, str(ad_file))
    # Every frame is in-range; helper returns 15 rows with 1-based
    # image_numbers 1..15.
    assert len(df) == 15
    assert list(df["image_number"]) == list(range(1, 16))
