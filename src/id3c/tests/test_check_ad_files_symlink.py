"""Tests for ``id3c.plans.flyscan_3idc._check_ad_files_symlink``.

Verifies:
- existing ad_files (symlink OR plain dir) is silently accepted;
- missing ad_files emits a WARNING containing the suggested
  ``ln -s`` command and the verification steps;
- the warning fires once per (master_dir, target) combination;
- a custom read_path_template is reflected in the suggested target;
- when read_path_template is missing the helper uses a placeholder
  the operator must fill in;
- the helper never creates or modifies the link.
"""

from types import SimpleNamespace

import pytest

from id3c.plans import flyscan_3idc as plan_mod
from id3c.plans.flyscan_3idc import _check_ad_files_symlink


def _make_det(read_path_template="/net/s3data/export/sector3/s3ida/XRD/"):
    """Duck-typed det with hdf1.read_path_template."""
    hdf1 = SimpleNamespace()
    if read_path_template is not None:
        hdf1.read_path_template = read_path_template
    return SimpleNamespace(hdf1=hdf1)


@pytest.fixture(autouse=True)
def _clear_warned_set():
    """Reset the once-per-session warned-set between tests."""
    plan_mod._AD_FILES_WARNED.clear()
    yield
    plan_mod._AD_FILES_WARNED.clear()


def test_existing_ad_files_symlink_is_accepted_silently(tmp_path, caplog):
    """A pre-existing ad_files symlink (any target) means no warning."""
    target = tmp_path / "some_mount"
    target.mkdir()
    link = tmp_path / "ad_files"
    link.symlink_to(target)
    det = _make_det()
    with caplog.at_level("WARNING", logger="id3c.plans.flyscan_3idc"):
        ok = _check_ad_files_symlink(det, master_dir=str(tmp_path))
    assert ok is True
    assert not [
        rec for rec in caplog.records if "ad_files" in rec.message
    ], "no warning should be emitted when ad_files exists"


def test_existing_ad_files_directory_is_accepted_silently(tmp_path, caplog):
    """A plain directory named ad_files (no symlink) is also accepted."""
    (tmp_path / "ad_files").mkdir()
    det = _make_det()
    with caplog.at_level("WARNING", logger="id3c.plans.flyscan_3idc"):
        ok = _check_ad_files_symlink(det, master_dir=str(tmp_path))
    assert ok is True


def test_missing_ad_files_emits_warning_with_ln_s_command(tmp_path, caplog):
    """Missing ad_files emits a WARNING with the ln -s recipe.

    Verifies the warning includes the suggested command, the
    verification step, and the audience-appropriate explanation.
    """
    det = _make_det(read_path_template="/mnt/data/XRD/")
    with caplog.at_level("WARNING", logger="id3c.plans.flyscan_3idc"):
        ok = _check_ad_files_symlink(det, master_dir=str(tmp_path))
    assert ok is False
    msgs = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
    assert msgs, "warning was not emitted"
    msg = msgs[0]
    # Substantive content the operator needs:
    assert "missing" in msg
    assert "ln -s /mnt/data/XRD ad_files" in msg
    assert "ls -l ad_files" in msg
    assert str(tmp_path) in msg
    assert "does NOT create this link automatically" in msg
    # Helper does NOT actually create the link.
    assert not (tmp_path / "ad_files").exists()


def test_warning_fires_only_once_per_master_dir_target_combination(tmp_path, caplog):
    """Second call with the same (master_dir, target) is silent."""
    det = _make_det(read_path_template="/mnt/data/XRD/")
    with caplog.at_level("WARNING", logger="id3c.plans.flyscan_3idc"):
        _check_ad_files_symlink(det, master_dir=str(tmp_path))
        warnings_after_first = [r for r in caplog.records if r.levelname == "WARNING"]
        _check_ad_files_symlink(det, master_dir=str(tmp_path))
        warnings_after_second = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings_after_first) == 1
    assert (
        len(warnings_after_second) == 1
    ), "warning should not fire twice for the same (master_dir, target)"


def test_warning_fires_again_when_master_dir_changes(tmp_path, caplog):
    """A different master_dir legitimately warns again."""
    other = tmp_path / "other_session"
    other.mkdir()
    det = _make_det(read_path_template="/mnt/data/XRD/")
    with caplog.at_level("WARNING", logger="id3c.plans.flyscan_3idc"):
        _check_ad_files_symlink(det, master_dir=str(tmp_path))
        _check_ad_files_symlink(det, master_dir=str(other))
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2


def test_missing_read_path_template_uses_placeholder(tmp_path, caplog):
    """Missing read_path_template yields a placeholder target.

    The operator must fill it in; we explicitly do NOT guess `/`.
    """
    det = _make_det(read_path_template=None)
    with caplog.at_level("WARNING", logger="id3c.plans.flyscan_3idc"):
        _check_ad_files_symlink(det, master_dir=str(tmp_path))
    msg = next(r.message for r in caplog.records if r.levelname == "WARNING")
    assert "the directory on this workstation" in msg
    assert "where the area-detector files are mounted" in msg
    # And the placeholder is not literally `/`.
    assert "ln -s / ad_files" not in msg


def test_helper_never_creates_the_symlink(tmp_path):
    """Pure paranoia: the helper must never create the link."""
    det = _make_det()
    _check_ad_files_symlink(det, master_dir=str(tmp_path))
    assert not (tmp_path / "ad_files").exists()
