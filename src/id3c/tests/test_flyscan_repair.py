"""Tests for ``id3c.utils.flyscan_repair`` (the repair CLI).

Synthetic master + area-detector HDF5 files; the catalog run is faked
and injected by monkeypatching ``_get_run``.  No IOC, no live catalog.
"""

from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from id3c.utils import flyscan_repair
from id3c.utils.flyscan_3idc_analysis import EPICS_EPOCH_OFFSET_S

UID = "abc123de-0000-1111-2222-333344445555"
UID_DSET = "/entry/instrument/detector/NDAttributes/NDArrayUniqueId"
TS_DSET = "/entry/instrument/detector/NDAttributes/NDArrayTimeStamp"


def _write_ad_file(path, n_frames=5, h=4, w=6, t_acquire=0.5, t0=1000.0):
    """AD HDF1 file with an image stack + NDAttributes."""
    ad_unix_t = np.array([t0 + 1.0 + i for i in range(n_frames)])
    epics_t = ad_unix_t - EPICS_EPOCH_OFFSET_S
    with h5py.File(path, "w") as f:
        f.create_dataset("/entry/data/data", data=np.zeros((n_frames, h, w), "uint16"))
        grp = f.create_group("/entry/instrument/detector/NDAttributes")
        grp.create_dataset("NDArrayTimeStamp", data=epics_t)
        grp.create_dataset("NDArrayUniqueId", data=np.arange(n_frames, dtype=np.int32))
    return ad_unix_t


def _write_master(path, ad_file, *, use_link=True, ad_path=None, ad_name=None):
    """Master with entry_identifier and either an external link or metadata."""
    with h5py.File(path, "w") as f:
        f.create_dataset("/entry/entry_identifier", data=UID)
        if use_link:
            f["/entry/images"] = h5py.ExternalLink(str(ad_file), "/entry/data")
        if ad_path is not None:
            base = "/entry/instrument/bluesky/metadata/"
            f.create_dataset(base + "ad_file_path", data=ad_path)
            f.create_dataset(base + "ad_file_name", data=ad_name)


def _fake_run(ad_unix_t, *, p_start=0.0, p_end=10.0, t_acquire=0.5, t_period=1.0):
    """Duck-typed run with metadata + motor monitor stream."""
    motor_t = np.linspace(1000.0, 1010.0, 101)
    motor_pos = motor_t - 1000.0  # pos = 0..10
    md = {
        "start": {
            "p_start": p_start,
            "p_end": p_end,
            "flymotor_name": "m1",
            "t_acquire": t_acquire,
            "t_period": t_period,
        }
    }
    ds = {
        "time": SimpleNamespace(data=motor_t),
        "m1": SimpleNamespace(data=motor_pos),
    }
    run = SimpleNamespace(metadata=md, m1_monitor=SimpleNamespace(read=lambda: ds))
    return run


@pytest.fixture
def _patch_run(monkeypatch):
    """Inject a fake catalog run for any uid."""

    def _factory(run):
        monkeypatch.setattr(flyscan_repair, "_get_run", lambda uid: run)

    return _factory


def test_read_run_uid(tmp_path):
    """The uid is read from /entry/entry_identifier."""
    ad = tmp_path / "ad.h5"
    _write_ad_file(ad)
    master = tmp_path / "m.hdf"
    _write_master(master, ad)
    assert flyscan_repair.read_run_uid(str(master)) == UID


def test_resolve_external_file_from_link(tmp_path):
    """The external link target is resolved relative to the master dir."""
    ad = tmp_path / "sub" / "ad.h5"
    ad.parent.mkdir()
    _write_ad_file(ad)
    master = tmp_path / "m.hdf"
    # Link stored as a relative path from the master directory.
    with h5py.File(master, "w") as f:
        f.create_dataset("/entry/entry_identifier", data=UID)
        f["/entry/images"] = h5py.ExternalLink("./sub/ad.h5", "/entry/data")
    resolved = flyscan_repair.resolve_external_file(str(master))
    assert resolved == str(ad)


def test_resolve_external_file_from_metadata(tmp_path):
    """With no link, the AD path is composed from start metadata."""
    master = tmp_path / "m.hdf"
    _write_master(
        master,
        tmp_path / "unused.h5",
        use_link=False,
        ad_path="/data/run/",
        ad_name="scan42",
    )
    resolved = flyscan_repair.resolve_external_file(str(master))
    assert resolved == "/data/run/scan42_000001.h5"


def test_repair_writes_flyscan_data(tmp_path, _patch_run):
    """A full repair writes /entry/flyscan_data with the paired frames."""
    ad = tmp_path / "ad.h5"
    ad_unix_t = _write_ad_file(ad, n_frames=5)
    master = tmp_path / "m.hdf"
    _write_master(master, ad)
    _patch_run(_fake_run(ad_unix_t))

    summary = flyscan_repair.repair_master_file(str(master))

    assert summary["written"] is True
    assert summary["n_frames_paired"] > 0
    with h5py.File(master, "r") as f:
        assert "/entry/flyscan_data" in f
        grp = f["/entry/flyscan_data"]
        assert grp.attrs["NX_class"] == "NXdata"
        assert grp.attrs["source"] == "ad_file"
        assert grp.attrs["n_frames_expected"] == 5
        assert f["/entry"].attrs["default"] == "flyscan_data"


def test_repair_is_idempotent(tmp_path, _patch_run):
    """Running twice yields the same group (no crash on re-write)."""
    ad = tmp_path / "ad.h5"
    ad_unix_t = _write_ad_file(ad)
    master = tmp_path / "m.hdf"
    _write_master(master, ad)
    _patch_run(_fake_run(ad_unix_t))

    first = flyscan_repair.repair_master_file(str(master))
    second = flyscan_repair.repair_master_file(str(master))
    assert first["n_frames_paired"] == second["n_frames_paired"]


def test_dry_run_does_not_write(tmp_path, _patch_run):
    """--dry-run reports but leaves the master unchanged."""
    ad = tmp_path / "ad.h5"
    ad_unix_t = _write_ad_file(ad)
    master = tmp_path / "m.hdf"
    _write_master(master, ad)
    _patch_run(_fake_run(ad_unix_t))

    summary = flyscan_repair.repair_master_file(str(master), dry_run=True)

    assert summary["written"] is False
    with h5py.File(master, "r") as f:
        assert "/entry/flyscan_data" not in f


def test_missing_uid_raises(tmp_path):
    """A master with no entry_identifier is an error."""
    master = tmp_path / "m.hdf"
    with h5py.File(master, "w") as f:
        f.create_group("/entry")
    with pytest.raises(ValueError, match="run uid"):
        flyscan_repair.repair_master_file(str(master))


def test_unresolvable_ad_file_raises(tmp_path):
    """A non-existent area-detector file is an error."""
    master = tmp_path / "m.hdf"
    _write_master(master, tmp_path / "nope.h5")  # link points at missing file
    with pytest.raises(FileNotFoundError):
        flyscan_repair.repair_master_file(str(master))


def test_cli_main_success(tmp_path, _patch_run, capsys):
    """The CLI returns 0 and prints a summary on success."""
    ad = tmp_path / "ad.h5"
    ad_unix_t = _write_ad_file(ad)
    master = tmp_path / "m.hdf"
    _write_master(master, ad)
    _patch_run(_fake_run(ad_unix_t))

    rc = flyscan_repair.main([str(master)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "/entry/flyscan_data" in out
    assert UID in out


def test_cli_main_failure_returns_1(tmp_path, capsys):
    """The CLI returns 1 and reports the error on failure."""
    master = tmp_path / "m.hdf"
    with h5py.File(master, "w") as f:
        f.create_group("/entry")
    rc = flyscan_repair.main([str(master)])
    assert rc == 1
    assert "FAILED" in capsys.readouterr().err
