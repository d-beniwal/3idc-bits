"""Tests for ``id3c.utils.flyscan_3idc_analysis.write_flyscan_data``.

Verifies the on-disk ``/entry/flyscan_data`` layout, the virtual
dataset selection, the provenance attributes, and idempotency.
Synthetic AD file + a hand-built paired DataFrame; no IOC, no catalog.
"""

import h5py
import numpy as np
import pandas as pd

from id3c.utils.flyscan_3idc_analysis import write_flyscan_data


def _write_ad_file(path, n_frames=6, h=3, w=4):
    """AD HDF1 file whose /entry/data/data[i] is filled with value i."""
    data = np.stack([np.full((h, w), i, dtype="uint16") for i in range(n_frames)])
    with h5py.File(path, "w") as f:
        f.create_dataset("/entry/data/data", data=data)
    return data


def _paired_df(image_numbers):
    """Minimal pairing DataFrame with the required columns."""
    n = len(image_numbers)
    return pd.DataFrame(
        {
            "image_number": np.asarray(image_numbers, dtype=np.int64),
            "timestamp": np.arange(n, dtype=float),
            "position_start_acquire": np.arange(n, dtype=float),
            "position_end_acquire": np.arange(n, dtype=float) + 0.1,
            "position_end_period": np.arange(n, dtype=float) + 0.2,
        }
    )


def test_writes_expected_layout(tmp_path):
    """All datasets and group attributes are present and correct."""
    ad = tmp_path / "ad.h5"
    _write_ad_file(ad, n_frames=6)
    master = tmp_path / "m.hdf"
    with h5py.File(master, "w") as f:
        f.create_group("/entry")
    # In-scan frames are image_number 2,3,4 (1-based) -> idx 1,2,3.
    df = _paired_df([2, 3, 4])

    summary = write_flyscan_data(str(master), str(ad), df, n_frames_expected=6)

    assert summary["n_frames_paired"] == 3
    with h5py.File(master, "r") as f:
        grp = f["/entry/flyscan_data"]
        assert grp.attrs["NX_class"] == "NXdata"
        assert grp.attrs["signal"] == "data"
        assert grp.attrs["source"] == "ad_file"
        assert grp.attrs["n_frames_paired"] == 3
        assert grp.attrs["n_frames_expected"] == 6
        assert f["/entry"].attrs["default"] == "flyscan_data"
        for name in (
            "data",
            "position_start_acquire",
            "position_end_acquire",
            "position_end_period",
            "image_number",
            "frame_index",
            "timestamp",
        ):
            assert name in grp, name
        assert list(grp["frame_index"][()]) == [1, 2, 3]
        assert list(grp["image_number"][()]) == [2, 3, 4]


def test_virtual_dataset_selects_correct_frames(tmp_path):
    """The virtual 'data' maps frame_index into the source stack."""
    ad = tmp_path / "ad.h5"
    _write_ad_file(ad, n_frames=6)
    master = tmp_path / "m.hdf"
    with h5py.File(master, "w") as f:
        f.create_group("/entry")
    df = _paired_df([2, 4, 6])  # idx 1, 3, 5

    write_flyscan_data(str(master), str(ad), df)

    with h5py.File(master, "r") as f:
        data = f["/entry/flyscan_data/data"][()]
    # Each source frame i is filled with value i; expect 1, 3, 5.
    assert [int(plane.flat[0]) for plane in data] == [1, 3, 5]


def test_idempotent_rewrite(tmp_path):
    """Writing twice replaces the group without error."""
    ad = tmp_path / "ad.h5"
    _write_ad_file(ad, n_frames=6)
    master = tmp_path / "m.hdf"
    with h5py.File(master, "w") as f:
        f.create_group("/entry")
    df = _paired_df([2, 3])

    write_flyscan_data(str(master), str(ad), df)
    write_flyscan_data(str(master), str(ad), df)  # must not raise

    with h5py.File(master, "r") as f:
        assert f["/entry/flyscan_data/data"].shape[0] == 2


def test_n_frames_expected_omitted_when_none(tmp_path):
    """No n_frames_expected attribute when the argument is None."""
    ad = tmp_path / "ad.h5"
    _write_ad_file(ad, n_frames=4)
    master = tmp_path / "m.hdf"
    with h5py.File(master, "w") as f:
        f.create_group("/entry")
    df = _paired_df([1, 2])

    write_flyscan_data(str(master), str(ad), df, n_frames_expected=None)

    with h5py.File(master, "r") as f:
        assert "n_frames_expected" not in f["/entry/flyscan_data"].attrs
