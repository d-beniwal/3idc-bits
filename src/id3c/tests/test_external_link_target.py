"""Tests for ``id3c.plans.flyscan_3idc._external_link_target``.

Pure-Python: builds duck-typed det.hdf1 objects from synthetic
templates and IOC paths, exercises the common-suffix construction,
the legacy fallback, and the warning paths.
"""

from types import SimpleNamespace

from id3c.plans.flyscan_3idc import _external_link_target


def _make_hdf1(full_file_name, write_path_template=None):
    """Build a duck-typed ``det.hdf1`` for the helper."""
    sig = SimpleNamespace(get=lambda use_monitor=False: full_file_name)
    hdf1 = SimpleNamespace(full_file_name=sig)
    if write_path_template is not None:
        hdf1.write_path_template = write_path_template
    return SimpleNamespace(hdf1=hdf1)


def test_strips_write_path_template_prefix():
    """write_path_template prefix is stripped from full_file_name.

    The remaining common suffix is appended to ad_files_root.
    """
    det = _make_hdf1(
        full_file_name="/home/sector3/s3ida/XRD/2026-2/setup/Jun15/foo.h5",
        write_path_template="/home/sector3/s3ida/XRD/",
    )
    target = _external_link_target(det)
    assert target == "./ad_files/2026-2/setup/Jun15/foo.h5"


def test_handles_template_without_trailing_slash():
    """write_path_template without a trailing slash is normalized."""
    det = _make_hdf1(
        full_file_name="/home/sector3/s3ida/XRD/2026-2/setup/Jun15/foo.h5",
        write_path_template="/home/sector3/s3ida/XRD",  # no trailing /
    )
    target = _external_link_target(det)
    assert target == "./ad_files/2026-2/setup/Jun15/foo.h5"


def test_custom_ad_files_root():
    """A non-default ad_files_root parameter is honoured."""
    det = _make_hdf1(
        full_file_name="/home/sector3/s3ida/XRD/2026-2/foo.h5",
        write_path_template="/home/sector3/s3ida/XRD/",
    )
    target = _external_link_target(det, ad_files_root="./alt_root/")
    assert target == "./alt_root/2026-2/foo.h5"


def test_falls_back_when_template_missing(caplog):
    """Missing write_path_template falls back to legacy target.

    Encapsulates the full absolute path and logs a WARNING.
    """
    det = _make_hdf1(
        full_file_name="/home/sector3/s3ida/XRD/2026-2/setup/Jun15/foo.h5",
        write_path_template=None,
    )
    with caplog.at_level("WARNING", logger="id3c.plans.flyscan_3idc"):
        target = _external_link_target(det)
    # Legacy form: ad_files + the full absolute path (without leading /).
    assert target == "./ad_files/home/sector3/s3ida/XRD/2026-2/setup/Jun15/foo.h5"
    assert any(
        "write_path_template not available" in rec.message for rec in caplog.records
    )


def test_falls_back_when_template_does_not_prefix_full_file_name(caplog):
    """Stale write_path_template (no prefix match) falls back.

    Logs a WARNING and uses the legacy absolute-path target.
    """
    det = _make_hdf1(
        full_file_name="/some/other/path/foo.h5",
        write_path_template="/home/sector3/s3ida/XRD/",
    )
    with caplog.at_level("WARNING", logger="id3c.plans.flyscan_3idc"):
        target = _external_link_target(det)
    assert target == "./ad_files/some/other/path/foo.h5"
    assert any("does not prefix" in rec.message for rec in caplog.records)


def test_template_equal_to_full_file_name_yields_empty_suffix():
    """Empty suffix when full_file_name equals the template.

    The helper handles this rather than raising.
    """
    det = _make_hdf1(
        full_file_name="/home/sector3/s3ida/XRD/",
        write_path_template="/home/sector3/s3ida/XRD/",
    )
    target = _external_link_target(det)
    assert target == "./ad_files/"


def test_uses_underscore_write_path_template_fallback():
    """Falls back to _write_path_template if write_path_template is absent."""
    sig = SimpleNamespace(
        get=lambda use_monitor=False: "/home/sector3/s3ida/XRD/foo.h5"
    )
    hdf1 = SimpleNamespace(
        full_file_name=sig,
        _write_path_template="/home/sector3/s3ida/XRD/",
    )
    det = SimpleNamespace(hdf1=hdf1)
    target = _external_link_target(det)
    assert target == "./ad_files/foo.h5"
