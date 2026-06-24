"""Tests for ``id3c.plans.flyscan_3idc._expected_frame_count``.

The helper provides the "expected" frame count stamped as provenance
on ``/entry/flyscan_data`` (issue #12, phase A).  It must:

- prefer the authoritative AD HDF1 file count when a path is given;
- fall back to the start document's ``num_frames`` otherwise;
- return ``None`` when neither source is available;
- never raise.

Synthetic AD files are built via h5py; runs are duck-typed dicts.
No IOC, no live detector, no catalog.
"""

from types import SimpleNamespace

import h5py
import numpy as np

from id3c.plans.flyscan_3idc import _expected_frame_count

UID_DSET = "/entry/instrument/detector/NDAttributes/NDArrayUniqueId"


def _write_ad_file(path, n_frames):
    """Write a minimal AD HDF1 file with ``n_frames`` UID rows."""
    with h5py.File(path, "w") as f:
        f.create_dataset(UID_DSET, data=np.arange(n_frames, dtype=np.int32))


def _run_with_num_frames(num_frames):
    """Duck-typed run carrying a start document with ``num_frames``."""
    return SimpleNamespace(metadata={"start": {"num_frames": num_frames}})


def test_ad_file_count_is_authoritative(tmp_path):
    """When the AD file is openable, its UID row count is returned."""
    ad = tmp_path / "ad.h5"
    _write_ad_file(ad, 106)
    # Even with a (different) num_frames in metadata, the AD file wins.
    run = _run_with_num_frames(101)
    assert _expected_frame_count(str(ad), run) == 106


def test_falls_back_to_num_frames_when_no_ad_path():
    """With ad_file_path=None, the start-doc num_frames is used."""
    run = _run_with_num_frames(101)
    assert _expected_frame_count(None, run) == 101


def test_falls_back_to_num_frames_when_ad_file_missing(tmp_path):
    """A non-openable AD path falls through to num_frames."""
    run = _run_with_num_frames(77)
    missing = tmp_path / "does_not_exist.h5"
    assert _expected_frame_count(str(missing), run) == 77


def test_falls_back_when_ad_file_lacks_uid_dataset(tmp_path):
    """An AD file without the UID dataset falls through to num_frames."""
    ad = tmp_path / "ad_no_uid.h5"
    with h5py.File(ad, "w") as f:
        f.create_group("/entry")  # present, but no NDArrayUniqueId
    run = _run_with_num_frames(42)
    assert _expected_frame_count(str(ad), run) == 42


def test_returns_none_when_no_source(tmp_path):
    """No AD file and no num_frames -> None (not an exception)."""
    missing = tmp_path / "nope.h5"
    run = SimpleNamespace(metadata={"start": {}})
    assert _expected_frame_count(str(missing), run) is None


def test_returns_none_when_num_frames_is_none():
    """An explicit num_frames=None is treated as absent."""
    run = SimpleNamespace(metadata={"start": {"num_frames": None}})
    assert _expected_frame_count(None, run) is None


def test_never_raises_on_bad_run_object():
    """A run object with no usable metadata yields None, not an error."""
    assert _expected_frame_count(None, SimpleNamespace()) is None
    assert _expected_frame_count(None, None) is None


def test_ad_count_used_even_when_metadata_unusable(tmp_path):
    """AD file count is returned regardless of a broken run object."""
    ad = tmp_path / "ad.h5"
    _write_ad_file(ad, 5)
    assert _expected_frame_count(str(ad), None) == 5


def test_returns_plain_int(tmp_path):
    """The result is a built-in int (HDF5/JSON attr friendly)."""
    ad = tmp_path / "ad.h5"
    _write_ad_file(ad, 9)
    val = _expected_frame_count(str(ad), None)
    # Built-in int, not numpy integer (cleaner HDF5/JSON attr write).
    assert isinstance(val, int) and not isinstance(val, np.integer)
