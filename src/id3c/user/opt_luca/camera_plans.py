"""3-ID-C alignment camera -- simple snapshot plan (OPT_Luca workstream).

Camera-only plans for the 3-ID-C alignment camera (``pg3_camera``, a Point
Grey GigE camera at ``3IDPG3:``). Part of the staged migration of Luca
Rebuffi's KB-mirror optics-alignment automation to Bluesky -- see
``.context/OPT_LUCA.md`` for the full PV inventory and remaining stages
(beam flux, KB motors, the alignment scan itself).

``camera_snapshot`` is a diagnostic tool, not a documented Bluesky run: it
sets the camera's exposure time/image mode and the TIFF plugin's file
path/name directly via ``bps.mv()`` (no ``stage()``/run-document
emission), fires the camera, and returns once it reports idle. This
mirrors what the EPICS/SPEC side already does for a quick alignment
snapshot.

Run through the RunEngine like any other plan::

    from id3c.user.opt_luca.camera_plans import camera_snapshot
    RE(camera_snapshot(0.1, "/home/sector3/s3ida/pg3/", "align"))

GUI-parseable docstrings: same fixed NumPy ``Parameters`` grammar as
``setup_june_26.py`` -- see that file's module docstring for the full
grammar spec; not repeated here.
"""

import logging

from apsbits.core.instrument_init import oregistry
from bluesky import plan_stubs as bps
from bluesky.utils import plan

from id3c.user.s3idc_plans.setup_june_26 import _wait_cam_idle

logger = logging.getLogger(__name__)


@plan
def camera_snapshot(
    exposure_time: float,
    file_path: str,
    file_name: str,
    num_images: int = 1,
):
    """Fire the 3-ID-C alignment camera and save snapshot(s) as TIFF.

    Sets the camera's exposure time and image mode, points the TIFF
    plugin at ``file_path``/``file_name``, then acquires. With
    ``num_images`` > 1 the TIFF plugin auto-increments the file number so
    each frame lands in its own file.

    ``file_path`` must already exist on the camera IOC's filesystem --
    the TIFF plugin does not create directories, and raises on the IOC
    side if the path is missing.

    Parameters
    ----------
    exposure_time : float [s]
        Exposure time :: per-frame acquire time (``cam.acquire_time``).
    file_path : str
        Save directory :: directory on the camera IOC's filesystem to
        write TIFF files to (``tiff1.file_path``); must already exist.
    file_name : str
        File name prefix :: TIFF file name prefix (``tiff1.file_name``);
        the IOC appends a zero-padded frame number and ``.tif``.
    num_images : int
        Number of snapshots :: number of frames to acquire and save
        (``cam.num_images``); each frame gets its own auto-incremented
        file.
    """
    camera = oregistry["pg3_camera"]
    image_mode = "Single" if num_images <= 1 else "Multiple"

    yield from bps.mv(
        camera.cam.image_mode,
        image_mode,
        camera.cam.acquire_time,
        exposure_time,
        camera.tiff1.enable,
        "Enable",
        camera.tiff1.file_path,
        file_path,
        camera.tiff1.file_name,
        file_name,
        camera.tiff1.auto_increment,
        "Yes",
        camera.tiff1.auto_save,
        "Yes",
    )
    if image_mode == "Multiple":
        yield from bps.mv(camera.cam.num_images, num_images)

    yield from bps.abs_set(camera.cam.acquire, 1)
    # acquire_busy reads 0 before the cam ramps up too, so give it a beat
    # before polling for idle (same reasoning as setup_june_26's
    # _acquire_single_file, Single-mode branch).
    yield from bps.sleep(min(exposure_time, 1.0))
    yield from _wait_cam_idle(camera, timeout=exposure_time * num_images + 30.0)
    # Best-effort settle time for the TIFF plugin to finish writing the
    # last frame -- this plugin exposes no per-frame "write complete"
    # counter to wait on precisely (see .context/OPT_LUCA.md).
    yield from bps.sleep(0.5)

    logger.info(
        "camera_snapshot: wrote %d frame(s) to %s%s*.tif",
        num_images,
        file_path,
        file_name,
    )
