"""Tests for ``id3c.plans.flyscan_3idc._ensure_ad_files_symlink``.

The plan auto-creates the per-detector image-files symlink
(``{det.name}_files``) next to the master file when it is safe to do
so, and falls back to a descriptive WARNING otherwise.  It must never
raise.
"""

import os
from types import SimpleNamespace

import pytest

from id3c.plans import flyscan_3idc as plan_mod
from id3c.plans.flyscan_3idc import _ensure_ad_files_symlink


def _make_det(read_path_template, name="eiger2"):
    """Duck-typed det with ``.name`` and hdf1.read_path_template."""
    hdf1 = SimpleNamespace()
    if read_path_template is not None:
        hdf1.read_path_template = read_path_template
    return SimpleNamespace(name=name, hdf1=hdf1)


@pytest.fixture(autouse=True)
def _clear_warned_set():
    """Reset the once-per-session warned-set between tests."""
    plan_mod._AD_FILES_WARNED.clear()
    yield
    plan_mod._AD_FILES_WARNED.clear()


def test_creates_symlink_when_target_exists(tmp_path):
    """A resolvable mount yields a real symlink named {det.name}_files."""
    mount = tmp_path / "mount"
    mount.mkdir()
    master_dir = tmp_path / "runs"
    master_dir.mkdir()
    det = _make_det(read_path_template=str(mount))

    ok = _ensure_ad_files_symlink(det, master_dir=str(master_dir))

    link = master_dir / "eiger2_files"
    assert ok is True
    assert link.is_symlink()
    assert os.readlink(link) == str(mount)


def test_symlink_name_follows_detector_name(tmp_path):
    """The link name tracks det.name (e.g. pilatus -> pilatus_files)."""
    mount = tmp_path / "mount"
    mount.mkdir()
    det = _make_det(read_path_template=str(mount), name="pilatus")

    _ensure_ad_files_symlink(det, master_dir=str(tmp_path))

    assert (tmp_path / "pilatus_files").is_symlink()


def test_idempotent_when_link_already_present(tmp_path):
    """An existing link of any kind is left untouched and accepted."""
    other = tmp_path / "other"
    other.mkdir()
    link = tmp_path / "eiger2_files"
    link.symlink_to(other)
    det = _make_det(read_path_template=str(tmp_path / "new_mount"))

    ok = _ensure_ad_files_symlink(det, master_dir=str(tmp_path))

    assert ok is True
    # Untouched: still points at the original target.
    assert os.readlink(link) == str(other)


def test_existing_plain_directory_is_accepted(tmp_path):
    """A plain directory of the right name is accepted, not replaced."""
    (tmp_path / "eiger2_files").mkdir()
    det = _make_det(read_path_template=str(tmp_path / "mount"))

    ok = _ensure_ad_files_symlink(det, master_dir=str(tmp_path))

    assert ok is True
    assert (tmp_path / "eiger2_files").is_dir()
    assert not (tmp_path / "eiger2_files").is_symlink()


def test_warns_when_target_unknown(tmp_path, caplog):
    """No read_path_template -> no link created, a WARNING is emitted."""
    det = _make_det(read_path_template=None)

    with caplog.at_level("WARNING", logger="id3c.plans.flyscan_3idc"):
        ok = _ensure_ad_files_symlink(det, master_dir=str(tmp_path))

    assert ok is False
    assert not (tmp_path / "eiger2_files").exists()
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_warns_when_target_does_not_exist(tmp_path, caplog):
    """A read_path_template that points nowhere -> no link, WARNING."""
    det = _make_det(read_path_template=str(tmp_path / "missing_mount"))

    with caplog.at_level("WARNING", logger="id3c.plans.flyscan_3idc"):
        ok = _ensure_ad_files_symlink(det, master_dir=str(tmp_path))

    assert ok is False
    assert not (tmp_path / "eiger2_files").is_symlink()


def test_never_raises_on_unwritable_master_dir(tmp_path, caplog):
    """A non-existent master_dir (cannot symlink into) -> False, no raise."""
    mount = tmp_path / "mount"
    mount.mkdir()
    det = _make_det(read_path_template=str(mount))
    missing_dir = tmp_path / "no_such_dir"

    with caplog.at_level("WARNING", logger="id3c.plans.flyscan_3idc"):
        ok = _ensure_ad_files_symlink(det, master_dir=str(missing_dir))

    assert ok is False
