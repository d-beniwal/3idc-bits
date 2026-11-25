"""Take one verified PVA microscope image; optionally save its matching TIFF.

Returns ``MicroscopeShot(image, file)``: a private float32 mono image and an IOC
file path (None unless ``save_files=True``). No Bluesky run is opened. Camera and
TIFF settings are restored on completion/error/abort; failed restoration raises.

Requires the configured ADCore 3.4 camera and its declared upstream plugins.
``NumQueuedArrays`` includes executing callbacks, unlike queue length or a quiet
counter, so old live-view frames must finish before settings or baselines change.
"""

import math
import time
from collections import namedtuple

import numpy as np
from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from bluesky.utils import plan
from ophyd.status import Status

from ._registry import find_device

__all__ = ["MicroscopeShot", "microscope_acquire", "run_microscope_acquire"]
MicroscopeShot = namedtuple("MicroscopeShot", ["image", "file"])
_PvaFrame = namedtuple("_PvaFrame", ["image", "key"])
_MARGIN = 30.0
_STATE_TIMEOUT = 30.0
_EPICS_EPOCH = 631152000


class _PutComplete:
    """Command status follows EPICS put completion, not a self-resetting value."""

    def __init__(self, signal):
        """Wrap an existing command signal without changing its configuration."""
        self.signal, self.name = signal, signal.name

    def set(self, value, *, timeout):
        """Avoid missing a fast 1->0 command or treating its initial zero as done."""
        status = Status(self.signal, timeout=timeout)
        try:
            self.signal.put(
                value,
                wait=False,
                timeout=timeout,
                use_complete=True,
                callback=lambda **kw: status.set_finished(),
            )
        except Exception as exc:
            status.set_exception(exc)
        return status


def _command(signal, value, timeout):
    """Issue a self-resetting command through the RunEngine."""
    yield from bps.abs_set(_PutComplete(signal), value, timeout=timeout, wait=True)


def _read_pva_frame(pv_name, timeout):
    """Copy pixels and identity from one NTNDArray, never from separate gets."""
    import pvapy as pva
    from pvapy.utility.adImageUtility import AdImageUtility as adu

    channel = pva.Channel(pv_name)
    channel.setTimeout(timeout)
    array = channel.get("")
    uid, image, *_ = adu.reshapeNtNdArray(array)
    if image is None or image.ndim != 2:
        raise RuntimeError(f"PVA channel {pv_name!r} has no mono 2-D image")
    stamp = array["timeStamp"]  # NDArray.epicsTS, not the camera-clock dataTimeStamp.
    key = (int(uid), int(stamp["secondsPastEpoch"]), int(stamp["nanoseconds"]))
    return _PvaFrame(np.array(image, dtype=np.float32, copy=True), key)


def _idle(value):
    """Accept numeric and enum representations of a stopped command."""
    return value in (0, "Done", "No")


def _enabled(value):
    """Accept numeric and enum representations of enabled callbacks."""
    return value in (1, "Enable", "Yes")


def _wait_for(signal, predicate, timeout, message):
    """Poll with an elapsed-time deadline; read delays also consume the budget."""
    deadline = time.monotonic() + timeout
    while True:
        value = yield from bps.rd(signal)
        if time.monotonic() > deadline:
            raise TimeoutError(message)
        if predicate(value):
            return value
        yield from bps.sleep(min(0.05, max(0, deadline - time.monotonic())))


def _pipeline(microscope):
    """Resolve camera->plugin dependencies so every upstream queue is drained."""
    devices = [getattr(microscope, name) for name in microscope.component_names]
    ports = {}
    for device in devices:
        if hasattr(device, "port_name"):
            ports[(yield from bps.rd(device.port_name))] = device
    ordered, visiting = [], set()

    def visit(device):
        if device in ordered:
            return
        if device in visiting:
            raise ValueError("cycle in microscope plugin pipeline")
        visiting.add(device)
        if device is not microscope.cam:
            # Sorting can retain frames outside the callback queue counters.
            if hasattr(device, "sort_mode") and (
                yield from bps.rd(device.sort_mode)
            ) not in (0, "Unsorted"):
                raise ValueError(
                    f"disable plugin sorting before capture: {device.name}"
                )
            port = yield from bps.rd(device.nd_array_port)
            if port not in ports:
                raise ValueError(f"undeclared microscope upstream port {port!r}")
            yield from visit(ports[port])
        ordered.append(device)
        visiting.remove(device)

    for device in (microscope.pva, microscope.tiff1):
        yield from visit(device)
    # Missing queue PVs must fail before we stop an otherwise working live view.
    for device in ordered:
        yield from bps.rd(device.num_queued_arrays)
    return ordered


def _drain(pipeline, timeout):
    """Wait upstream first: a late transform must not refill a drained child."""
    deadline = time.monotonic() + timeout
    for device in pipeline:
        yield from _wait_for(
            device.num_queued_arrays,
            lambda value: int(value) == 0,
            max(0, deadline - time.monotonic()),
            f"plugins did not drain: {device.name}",
        )


def _frame_key(plugin):
    """Identity includes timestamp because camera frame IDs can wrap or reset."""
    uid = yield from bps.rd(plugin.unique_id)
    sec = yield from bps.rd(plugin.epics_ts_sec)
    nsec = yield from bps.rd(plugin.epics_ts_nsec)
    return int(uid), int(sec) + _EPICS_EPOCH, int(nsec)


@plan
def microscope_acquire(
    file_path=None,
    file_name=None,
    *,
    microscope="pg3_camera",
    exposure_time=None,
    save_files: bool = False,
    file_number: int = 1,
    pixel_format=None,
    pva_pv=None,
):
    """Return one fresh ``MicroscopeShot`` and restore the pre-plan settings.

    Parameters
    ----------
    file_path, file_name : str, optional
        IOC TIFF directory/name; used only with save_files=True. Directory must exist.
    microscope : device or str
        Default registry name: pg3_camera. Needs cam, pva, tiff1 and their upstreams.
    exposure_time : float, optional
        Seconds; default leaves the current exposure unchanged.
    save_files : bool
        Default False. True explicitly writes the TIFF buffered for this PVA frame.
    file_number : int
        TIFF number, default 1. An existing file with this name can be overwritten.
    pixel_format : int or str, optional
        Default leaves the current pixel format unchanged.
    pva_pv : str, optional
        Default uses the PVA plugin's configured PvName_RBV.

    Notes
    -----
    Active TIFF capture/writing is rejected: its open file/buffers cannot be restored
    by putting settings back. Trigger mode is temporarily Internal (0). Hardware
    failure can prevent restoration; errors identify the settings that failed.
    """
    microscope = find_device(microscope)
    if save_files and not file_path:
        raise ValueError("file_path must be given when save_files=True")
    if not save_files and (file_path or file_name):
        raise ValueError("file_path / file_name only apply with save_files=True")
    if not isinstance(file_number, int) or file_number < 0:
        raise ValueError("file_number must be a non-negative integer")
    cam, pva, tiff = microscope.cam, microscope.pva, microscope.tiff1
    exposure = exposure_time
    if exposure is None:
        exposure = yield from bps.rd(cam.acquire_time)
    if not math.isfinite(exposure) or exposure <= 0:
        raise ValueError("exposure_time must be finite and positive")
    for signal in (tiff.capture, tiff.write_file):
        if not _idle((yield from bps.rd(signal))):
            raise RuntimeError(
                f"microscope TIFF operation already active: {signal.name}"
            )
    if not _enabled((yield from bps.rd(pva.enable))):
        raise RuntimeError("microscope PVA callbacks are disabled")
    pipeline = yield from _pipeline(microscope)
    yield from bps.rd(cam.array_counter)
    yield from bps.rd(pva.array_counter)
    yield from _frame_key(pva)
    if save_files:
        yield from _frame_key(tiff)
    pva_pv = pva_pv or (yield from bps.rd(pva.pv_name))
    camera_settings = {cam.image_mode: "Single", cam.trigger_mode: 0}
    if exposure_time is not None:
        camera_settings[cam.acquire_time] = exposure
    if pixel_format is not None:
        camera_settings[cam.pixel_format] = pixel_format
    # Even memory-only shots suppress autosave: a previously enabled writer must
    # not save our exposure, and queued old frames must not take its new filename.
    writer_settings = {tiff.enable: "Disable", tiff.auto_save: "No"}
    if save_files:
        writer_settings.update(
            {
                tiff.file_write_mode: "Single",
                tiff.file_path: file_path,
                tiff.file_name: file_name or f"sample_{round(exposure * 1000)}ms",
                tiff.file_number: file_number,
                tiff.auto_increment: "Yes",
            }
        )
    saved = {}
    for signal in (*camera_settings, *writer_settings):
        saved[signal] = yield from bps.rd(signal)
    was_acquiring = not _idle((yield from bps.rd(cam.acquire)))
    # Restarting a partly completed Single/Multiple sequence would acquire it again.
    if was_acquiring and saved[cam.image_mode] not in (2, "Continuous"):
        raise RuntimeError("only continuous microscope live view can be interrupted")
    timeout = exposure + _MARGIN
    changed = set()
    acquisition_error = None

    def set_value(signal, value):
        # Record before the write: a failing put may already have changed hardware.
        changed.add(signal)
        yield from bps.abs_set(signal, value, wait=True, timeout=_STATE_TIMEOUT)

    def stop():
        yield from _command(cam.acquire, 0, _STATE_TIMEOUT)
        yield from _wait_for(
            cam.acquire, _idle, _STATE_TIMEOUT, "microscope did not stop acquiring"
        )
        yield from _drain(pipeline, _STATE_TIMEOUT)
        yield from _wait_for(
            tiff.write_file, _idle, _STATE_TIMEOUT, "TIFF writer is still busy"
        )

    def shoot():
        yield from stop()
        for signal, value in writer_settings.items():
            yield from set_value(signal, value)
        for signal, value in camera_settings.items():
            yield from set_value(signal, value)
        if save_files:
            yield from set_value(
                tiff.enable, "Enable"
            )  # Buffer only; autosave stays off.
        camera_before = yield from bps.rd(cam.array_counter)
        pva_before = yield from bps.rd(pva.array_counter)
        yield from _command(cam.acquire, 1, _STATE_TIMEOUT)
        yield from _wait_for(
            cam.array_counter,
            lambda n: n > camera_before,
            timeout,
            "microscope produced no new frame",
        )
        yield from _wait_for(
            cam.acquire, _idle, timeout, "microscope acquisition did not finish"
        )
        yield from _drain(pipeline, timeout)
        # Exact increments reject dropped frames or unsolicited acquisition. Quiet
        # counters alone cannot distinguish an old queued image from our exposure.
        if (yield from bps.rd(cam.array_counter)) != camera_before + 1:
            raise RuntimeError("expected exactly one new microscope frame")
        if (yield from bps.rd(pva.array_counter)) != pva_before + 1:
            raise RuntimeError("expected exactly one new frame from the PVA plugin")
        key = yield from _frame_key(pva)
        frame = _read_pva_frame(pva_pv, _STATE_TIMEOUT)
        if frame.key != key:
            raise RuntimeError(
                "PVA image does not match the completed microscope frame"
            )
        file = None
        if save_files:
            if (yield from _frame_key(tiff)) != key:
                raise RuntimeError("TIFF buffer does not match the PVA image")
            yield from set_value(tiff.enable, "Disable")  # Freeze the verified buffer.
            # Put completion belongs to THIS write; FileNumber advances before open,
            # and WriteStatus=OK while busy does not rule out a later disk-full error.
            yield from _command(tiff.write_file, 1, timeout)
            yield from _wait_for(
                tiff.write_file, _idle, timeout, "TIFF write did not finish"
            )
            if (yield from bps.rd(tiff.write_status)) not in (0, "Write OK"):
                message = yield from bps.rd(tiff.write_message)
                raise RuntimeError(
                    f"the TIFF plugin could not write the file: {message}"
                )
            file = yield from bps.rd(tiff.full_file_name)
            if not file:
                raise RuntimeError("TIFF writer returned an empty filename")
        return MicroscopeShot(frame.image, file)

    def restore():
        problems = []
        try:
            yield from stop()
        except Exception as exc:
            # Do not change pixel format/mode or rearm writers while frames or a
            # write are still in flight; this can reject settings or corrupt output.
            raise RuntimeError(f"microscope_acquire could not restore: {exc}") from exc
        # Restore independent settings individually. One disconnected PV must not
        # skip every remaining restore; writer enable/autosave and restart go last.
        tail = (tiff.auto_save, tiff.enable)
        ordered = [s for s in saved if s not in tail] + list(tail)
        for signal in ordered:
            if signal not in changed:
                continue
            if signal in tail and problems:
                continue
            try:
                yield from bps.abs_set(
                    signal, saved[signal], wait=True, timeout=_STATE_TIMEOUT
                )
            except Exception as exc:
                problems.append(f"{signal.name}: {exc}")
        if was_acquiring and not problems:
            try:
                yield from _command(cam.acquire, 1, _STATE_TIMEOUT)
                yield from _wait_for(
                    cam.acquire,
                    lambda v: not _idle(v),
                    _STATE_TIMEOUT,
                    "microscope did not resume acquiring",
                )
            except Exception as exc:
                problems.append(str(exc))
        if problems:
            raise RuntimeError(
                "microscope_acquire could not restore: " + "; ".join(problems)
            )

    def guarded_shoot():
        nonlocal acquisition_error
        try:
            return (yield from shoot())
        except Exception as exc:
            acquisition_error = exc
            raise

    def guarded_restore():
        try:
            yield from restore()
        except Exception as exc:
            if acquisition_error is not None:
                raise ExceptionGroup(
                    "microscope acquisition and restoration failed",
                    [acquisition_error, exc],
                ) from None
            raise

    return (yield from bpp.finalize_wrapper(guarded_shoot(), guarded_restore()))


def run_microscope_acquire(RE, file_path=None, file_name=None, **plan_kwargs):
    """Execute the plan and return its shot; after a pause use RE.abort()."""
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
