"""``microscope_acquire``: single-shot microscope acquisition as a Bluesky plan.

Replaces what Luca's ``GenericCamera.collect_single_shot_image`` does through raw
``epics.PV`` calls for the 3-ID-C alignment microscope (``3IDPG3:``, the
``pg3_camera`` ophyd device): configure the microscope + TIFF plugin, fire the
microscope, wait until the frame(s) are written.  You choose where the files go
(``file_path``), their name (``file_name``, ``file_number``, ``index_digits``), the
exposure time, the number of images and (optionally) the pixel format.

**Settings are put back exactly as they were.**  Before changing anything the plan
reads the current value of every setting it is going to change, and afterwards --
also after an error or ``RE.abort()`` -- restores them all.  Always: microscope image
mode, exposure time, TIFF plugin enable, file path, file name, file number, auto
save, auto increment, file write mode.  Only if you use the option: number of images
(``num_images`` > 1), pixel format, file template (``index_digits``).  Plus the
acquire state: a microscope that was acquiring before (e.g. a live view) is
acquiring again afterwards.

No Bluesky *run* is opened: no documents, nothing to Tiled.  The images are the TIFF
files the IOC writes.  Unlike Luca's code this does not read the PVA stream or
convert to HDF5.  The plan returns the list of written file paths (as seen by the
IOC).

The microscope is the ``pg3_camera`` device from the registry (an ``ad_creator``
detector with ``cam`` and ``tiff1``; ``devices_camera.yml`` -- the registry name
keeps "camera"), looked up by name like in the other ``id3c.user`` plans, or passed
as an object::

    from id3c.startup import RE
    from id3c.user.opt_luca.microscope_acquire import microscope_acquire

    RE(microscope_acquire("/home/sector3/s3ida/pg3/", "align", exposure_time=0.1,
                          num_images=3))

``RE(...)`` returns only run uids; to get the list of files use ``yield from``
inside your own plan, or ``run_microscope_acquire(RE, ...)`` below.

``file_path`` must already exist on the IOC's filesystem (the plugin does not create
it).  Existing files with the same name are overwritten by the plugin.
"""

import logging
import time

from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from bluesky.utils import plan

from ._registry import find_device

logger = logging.getLogger(__name__)

__all__ = ["microscope_acquire", "run_microscope_acquire"]

_MARGIN = 30.0  # [s] allowed on top of exposure_time * num_images before giving up


def _wait_for(signal, predicate, timeout):
    """Plan stub: poll ``signal`` until ``predicate(value)`` holds.

    Returns True, or False on timeout.

    Polling with ``bps.rd`` keeps the plan pure messages (no side effects outside the
    RunEngine), and it never raises on timeout so the caller decides, even in cleanup.
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate((yield from bps.rd(signal))):
            return True
        if time.monotonic() > deadline:
            return False
        yield from bps.sleep(0.1)


def _idle(value):
    """True for the "not acquiring" value (0, or "Done" if read as a string)."""
    return value in (0, "Done")


def _file_names(template, file_path, file_name, first_number, n):
    """The ``n`` file paths the TIFF plugin writes, from its ``file_template``."""
    if not file_path.endswith("/"):
        file_path += "/"  # the IOC adds the slash; the template is "%s%s_%4.4d.tif"
    return [template % (file_path, file_name, first_number + i) for i in range(n)]


@plan
def microscope_acquire(
    file_path: str,
    file_name=None,
    *,
    microscope="pg3_camera",
    exposure_time=None,
    num_images: int = 1,
    file_number: int = 1,
    index_digits=None,
    pixel_format=None,
):
    """Acquire ``num_images`` frames on ``microscope`` as TIFF; restore all settings.

    Parameters
    ----------
    file_path : str
        Directory on the microscope IOC's filesystem; must already exist.
    file_name : str, optional
        File name prefix.  Default (as in Luca's code): ``sample_<exposure in ms>ms``.
        Files are ``<file_path>/<file_name>_<number>.tif``.
    microscope : area detector with ``cam`` and ``tiff1``, or its registry name
        Default ``"pg3_camera"``.
    exposure_time : float [s], optional
        Acquire time per frame.  Default: leave the microscope's current value.
    num_images : int
        Frames to take (1 = Single mode, more = Multiple mode); one file each, numbered
        ``file_number``, ``file_number + 1``, ...
    file_number : int
        Number of the first file (Luca's ``image_index``).
    index_digits : int, optional
        Digits of the number in the file name (Luca's ``INDEX_DIGITS``, 5, i.e.
        ``name_00001.tif``).  Default: leave the plugin's file template as it is.
    pixel_format : int or str, optional
        Microscope pixel format (Luca's ``PIXEL_FORMAT``, e.g. 0 = Mono8).  Default:
        leave it.

    Returns
    -------
    list of str
        Paths of the files written (as seen by the IOC).
    """
    microscope = find_device(microscope)
    if not file_path:
        raise ValueError("file_path must be given")
    if num_images < 1:
        raise ValueError(f"num_images={num_images} must be >= 1")
    if exposure_time is not None and exposure_time <= 0:
        raise ValueError(f"exposure_time={exposure_time:g} must be positive")
    if index_digits is not None and index_digits < 1:
        raise ValueError(f"index_digits={index_digits} must be >= 1")

    cam, tiff = microscope.cam, microscope.tiff1

    # What we will set (signal -> new value); everything in here is restored afterwards.
    exposure = exposure_time
    if exposure is None:
        exposure = yield from bps.rd(cam.acquire_time)
    if file_name is None:
        file_name = f"sample_{int(round(exposure * 1000))}ms"
    new = {
        cam.image_mode: "Single" if num_images == 1 else "Multiple",
        cam.acquire_time: exposure,
        tiff.enable: "Enable",
        tiff.file_write_mode: "Single",
        tiff.file_path: file_path,
        tiff.file_name: file_name,
        tiff.file_number: file_number,
        tiff.auto_increment: "Yes",
        tiff.auto_save: "Yes",
    }
    if num_images > 1:
        new[cam.num_images] = num_images
    if pixel_format is not None:
        new[cam.pixel_format] = pixel_format
    if index_digits is not None:
        new[tiff.file_template] = f"%s%s_%{index_digits}.{index_digits}d.tif"

    # Remember the current value of each (before anything is changed) and the acquire
    # state.
    saved = {}
    for sig in new:
        saved[sig] = yield from bps.rd(sig)
    was_acquiring = not _idle((yield from bps.rd(cam.acquire)))
    timeout = exposure * num_images + _MARGIN

    def stop_microscope():
        yield from bps.abs_set(cam.acquire, 0)
        return (yield from _wait_for(cam.acquire, _idle, 30.0))

    def acquire():
        if was_acquiring and not (yield from stop_microscope()):
            raise TimeoutError("microscope did not stop acquiring")
        yield from bps.mv(*[x for pair in new.items() for x in pair])
        # The plugin bumps the file number after writing each file, so waiting for it to
        # advance means "all files are on disk" (it cannot fire early: it starts at
        # ``file_number``).
        yield from bps.abs_set(cam.acquire, 1)
        written = yield from _wait_for(
            tiff.file_number, lambda v: int(v) >= file_number + num_images, timeout
        )
        if not written:
            raise TimeoutError(
                f"{num_images} file(s) not written within {timeout:g} s "
                f"(is {file_path!r} a directory on the IOC's filesystem?)"
            )
        yield from _wait_for(cam.acquire, _idle, 30.0)
        yield from bps.sleep(0.1)  # let the plugin finish closing the last file
        template = yield from bps.rd(tiff.file_template)
        return _file_names(template, file_path, file_name, file_number, num_images)

    def restore():
        now_acquiring = not _idle((yield from bps.rd(cam.acquire)))
        if now_acquiring and not (yield from stop_microscope()):
            logger.warning(
                "microscope_acquire: microscope did not stop; restoring settings anyway"
            )
        yield from bps.mv(*[x for pair in saved.items() for x in pair])
        if was_acquiring:
            yield from bps.abs_set(cam.acquire, 1)

    files = yield from bpp.finalize_wrapper(acquire(), restore())
    logger.info("microscope_acquire: wrote %d file(s) to %s", len(files), file_path)
    return files


def run_microscope_acquire(RE, file_path, file_name=None, **plan_kwargs):
    """Run :func:`microscope_acquire` with ``RE``; return the list of files written.

    ``RE(plan)`` only returns run uids, so the plan's return value is captured here.
    Raises ``RuntimeError`` if the plan did not finish.  After a pause (Ctrl-C) call
    ``RE.abort()``: it restores the microscope/plugin settings.
    """
    captured = []

    def run():
        captured.append(
            (yield from microscope_acquire(file_path, file_name, **plan_kwargs))
        )

    RE(run())
    if not captured:
        raise RuntimeError(
            "microscope_acquire did not finish (RunEngine stopped or aborted it)"
        )
    return captured[0]
