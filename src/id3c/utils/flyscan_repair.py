"""Repair tool: add ``/entry/flyscan_data`` to an existing master file.

A flyscan master file is missing ``/entry/flyscan_data`` when the
area-detector file was not reachable at run end (most commonly the
image-files symlink next to the master was missing or mis-shaped).
Once the symlink is fixed, this tool recomputes the per-frame pairing
and writes the group, producing the same on-disk layout the live plan
would have written.

Console script (see ``pyproject.toml``)::

    id3c-flyscan-repair MASTER.hdf [--external-file PATH] [--dry-run]

The run is identified by the uid stored in the master file at
``/entry/entry_identifier``; the catalog is read from
``id3c.startup.cat``.  The area-detector file is located from the
master's existing ``/entry/images`` external link, or composed from
the ``ad_file_path`` / ``ad_file_name`` start-document metadata.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logger = logging.getLogger(__name__)

ENTRY_IDENTIFIER = "/entry/entry_identifier"
IMAGES_ADDR = "/entry/images"
AD_FILE_NUMBER_SUFFIX = "_000001.h5"


def _read_str(h5obj, addr):
    """Return a string dataset/attr value at ``addr`` or None."""
    if addr not in h5obj:
        return None
    val = h5obj[addr][()]
    if isinstance(val, bytes):
        return val.decode()
    return str(val)


def read_run_uid(master_file):
    """Return the run uid stored in the master file, or None."""
    import h5py

    with h5py.File(master_file, "r") as f:
        return _read_str(f, ENTRY_IDENTIFIER)


def resolve_external_file(master_file):
    """Return the area-detector file path resolvable from the master.

    Prefers the existing ``/entry/images`` external-link target
    (resolved relative to the master's directory through the
    image-files symlink).  Falls back to composing the IOC path from
    the ``ad_file_path`` / ``ad_file_name`` start metadata.  Returns
    ``None`` if neither is available.
    """
    import h5py

    master_dir = os.path.dirname(os.path.abspath(master_file))
    with h5py.File(master_file, "r") as f:
        link = f.get(IMAGES_ADDR, getlink=True)
        if isinstance(link, h5py.ExternalLink):
            return os.path.normpath(os.path.join(master_dir, link.filename))

        base = "/entry/instrument/bluesky/metadata/"
        ad_path = _read_str(f, base + "ad_file_path")
        ad_name = _read_str(f, base + "ad_file_name")

    if ad_path and ad_name:
        return os.path.join(ad_path, ad_name + AD_FILE_NUMBER_SUFFIX)
    return None


def _get_run(uid):
    """Return the catalog run for ``uid`` (imported lazily)."""
    from id3c.startup import cat

    return cat[uid]


def repair_master_file(master_file, *, external_file=None, dry_run=False):
    """Recompute and write ``/entry/flyscan_data`` into ``master_file``.

    Returns a summary dict.  Raises on unrecoverable conditions
    (no uid, no AD file, AD file not openable, 0 paired frames) so the
    CLI can report a non-zero exit.
    """
    import h5py

    from id3c.utils.flyscan_3idc_analysis import pair_frames_to_positions_from_ad_file

    uid = read_run_uid(master_file)
    if not uid:
        raise ValueError(
            f"{master_file!r} has no {ENTRY_IDENTIFIER} (run uid); cannot"
            " locate the catalog run."
        )

    if external_file is None:
        external_file = resolve_external_file(master_file)
    if not external_file:
        raise ValueError(
            f"could not determine the area-detector file for {master_file!r};"
            " pass --external-file explicitly."
        )

    if not os.path.exists(external_file):
        raise FileNotFoundError(
            f"area-detector file {external_file!r} does not exist or does"
            " not resolve (check the image-files symlink next to the"
            " master file)."
        )
    try:
        with h5py.File(external_file, "r"):
            pass
    except OSError as exc:
        raise OSError(
            f"area-detector file {external_file!r} is not openable: {exc!r}"
        ) from exc

    run = _get_run(uid)
    df = pair_frames_to_positions_from_ad_file(run, external_file)
    n_paired = int(len(df))
    if n_paired == 0:
        raise ValueError(
            f"pairing produced 0 in-scan frames for uid={uid!r}; nothing to write."
        )

    if dry_run:
        logger.info(
            "flyscan-repair: DRY RUN — would write /entry/flyscan_data"
            " (%d in-scan frame(s)) into %s from %s",
            n_paired,
            master_file,
            external_file,
        )
        return {
            "uid": uid,
            "external_file": external_file,
            "n_frames_paired": n_paired,
            "written": False,
        }

    # Write only the function's own provenance (n_frames_expected from
    # the AD file frame count) without depending on the plan module.
    n_frames_expected = _ad_frame_count(external_file)
    from id3c.utils.flyscan_3idc_analysis import write_flyscan_data

    summary = write_flyscan_data(
        master_file,
        external_file,
        df,
        n_frames_expected=n_frames_expected,
    )
    logger.info(
        "flyscan-repair: wrote /entry/flyscan_data (%d in-scan frame(s)) into %s",
        summary["n_frames_paired"],
        master_file,
    )
    return {
        "uid": uid,
        "external_file": external_file,
        "n_frames_paired": summary["n_frames_paired"],
        "written": True,
    }


def _ad_frame_count(
    external_file,
    unique_id_dset="/entry/instrument/detector/NDAttributes/NDArrayUniqueId",
):
    """Total acquired-frame count from the AD file, or None."""
    import h5py

    try:
        with h5py.File(external_file, "r") as f:
            if unique_id_dset in f:
                return int(f[unique_id_dset].shape[0])
    except Exception as exc:
        logger.debug("_ad_frame_count: %r", exc)
    return None


def main(argv=None):
    """Console-script entry point."""
    parser = argparse.ArgumentParser(
        prog="id3c-flyscan-repair",
        description=(
            "Add the missing /entry/flyscan_data group to a flyscan"
            " NeXus master file by recomputing the per-frame pairing"
            " from the authoritative area-detector file."
        ),
    )
    parser.add_argument("master_file", help="path to the NeXus master HDF5 file")
    parser.add_argument(
        "--external-file",
        default=None,
        help=(
            "explicit path to the area-detector HDF1 file (overrides the"
            " path resolved from the master file)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be done without modifying the master file",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable INFO-level logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    try:
        summary = repair_master_file(
            args.master_file,
            external_file=args.external_file,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"flyscan-repair: FAILED: {exc}", file=sys.stderr)
        return 1

    action = "would write" if args.dry_run else "wrote"
    print(
        f"flyscan-repair: {action} /entry/flyscan_data"
        f" ({summary['n_frames_paired']} in-scan frame(s)) for uid"
        f" {summary['uid']} using {summary['external_file']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
