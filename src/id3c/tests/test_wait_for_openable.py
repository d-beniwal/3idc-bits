"""Tests for ``id3c.plans.flyscan_3idc._wait_for_openable``.

Pure-Python: builds real HDF5 files in a tmp dir, no IOC, no
ophyd, no bluesky.  Exercises the success path, the
missing-file path, and the bounded-timeout behaviour.
"""

import time

import h5py

from id3c.plans.flyscan_3idc import _wait_for_openable


def test_wait_for_openable_returns_true_for_existing_file(tmp_path):
    """A valid HDF5 file opens on the first try."""
    p = tmp_path / "ok.h5"
    with h5py.File(p, "w") as f:
        f.create_group("entry")
    assert _wait_for_openable(str(p), mode="r") is True


def test_wait_for_openable_returns_false_for_missing_file(tmp_path):
    """A nonexistent path returns False after retries (no raise)."""
    missing = tmp_path / "does_not_exist.h5"
    t0 = time.monotonic()
    assert _wait_for_openable(str(missing), mode="r", retries=3, timeout_s=1.0) is False
    # Bounded by the timeout, not by retries x infinite sleep.
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5, f"helper took too long: {elapsed:.2f}s"


def test_wait_for_openable_returns_false_for_non_hdf5_path(tmp_path):
    """A path that exists but is not a valid HDF5 file returns False."""
    p = tmp_path / "not_hdf5.txt"
    p.write_text("plain text, not HDF5")
    assert _wait_for_openable(str(p), mode="r", retries=2, timeout_s=0.5) is False


def test_wait_for_openable_append_mode_works(tmp_path):
    """Append mode opens a writable file successfully."""
    p = tmp_path / "writable.h5"
    with h5py.File(p, "w") as f:
        f.create_group("entry")
    assert _wait_for_openable(str(p), mode="a") is True


def test_wait_for_openable_respects_timeout_budget(tmp_path):
    """Helper exits within the timeout budget on persistent failure."""
    missing = tmp_path / "never_appears.h5"
    t0 = time.monotonic()
    # Many retries but a small timeout: the timeout should win.
    result = _wait_for_openable(str(missing), mode="r", retries=100, timeout_s=0.5)
    elapsed = time.monotonic() - t0
    assert result is False
    assert elapsed < 1.0, f"helper exceeded timeout budget: {elapsed:.2f}s"
