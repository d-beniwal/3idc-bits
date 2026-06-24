"""Tests for the hdf1 path-template reader helpers.

``_read_path_template`` / ``_write_path_template`` feed the
``ad_read_path_template`` / ``ad_write_path_template`` start-metadata
that lets a master file reconstruct its image-files symlink.
"""

from types import SimpleNamespace

from id3c.plans.flyscan_3idc import _read_path_template
from id3c.plans.flyscan_3idc import _write_path_template


def test_read_path_template_primary():
    """read_path_template is returned when present."""
    det = SimpleNamespace(hdf1=SimpleNamespace(read_path_template="/net/mount/"))
    assert _read_path_template(det) == "/net/mount/"


def test_write_path_template_primary():
    """write_path_template is returned when present."""
    det = SimpleNamespace(hdf1=SimpleNamespace(write_path_template="/ioc/path/"))
    assert _write_path_template(det) == "/ioc/path/"


def test_read_falls_back_to_underscore_attr():
    """The _read_path_template attribute is the documented fallback."""
    det = SimpleNamespace(hdf1=SimpleNamespace(_read_path_template="/net/mount/"))
    assert _read_path_template(det) == "/net/mount/"


def test_write_falls_back_to_underscore_attr():
    """The _write_path_template attribute is the documented fallback."""
    det = SimpleNamespace(hdf1=SimpleNamespace(_write_path_template="/ioc/path/"))
    assert _write_path_template(det) == "/ioc/path/"


def test_returns_none_when_absent():
    """Missing templates return None (stamped as '' in metadata)."""
    det = SimpleNamespace(hdf1=SimpleNamespace())
    assert _read_path_template(det) is None
    assert _write_path_template(det) is None
