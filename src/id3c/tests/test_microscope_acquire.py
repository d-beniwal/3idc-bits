"""Offline acquisition regressions: identity, delayed callbacks/writes, and cleanup."""

import importlib
import threading
import time

import numpy as np
import pytest
from bluesky import RunEngine
from bluesky.utils import RunEngineInterrupted
from ophyd import Signal

from id3c.tests._sim_microscope import SimMicroscope

m = importlib.import_module("id3c.user.autofocus.microscope_acquire")


@pytest.fixture
def cam(monkeypatch):
    """Replace only the PVA transport; keep asynchronous hardware-like behavior."""
    c = SimMicroscope(name="pg3")

    def read(pv, timeout):
        c.read_pvs.append(pv)
        return m._PvaFrame(c.pva.image.copy(), c.key(c.pva))

    monkeypatch.setattr(m, "_read_pva_frame", read)
    monkeypatch.setattr(m, "_STATE_TIMEOUT", 0.8)
    monkeypatch.setattr(m, "_MARGIN", 0.8)
    yield c
    c.close()


def snapshot(c):
    """Settings, excluding acquisition counters and result/error metadata."""
    return {
        s.name: s.get()
        for s in (
            c.cam.image_mode,
            c.cam.trigger_mode,
            c.cam.acquire_time,
            c.cam.pixel_format,
            c.cam.acquire,
            c.tiff1.enable,
            c.tiff1.file_path,
            c.tiff1.file_name,
            c.tiff1.file_number,
            c.tiff1.auto_increment,
            c.tiff1.auto_save,
            c.tiff1.file_write_mode,
        )
    }


def run(c, **kw):
    """Execute the public helper, without instrument startup or EPICS."""
    return m.run_microscope_acquire(RunEngine({}), microscope=c, **kw)


def test_default_image_and_settings(cam):
    """Return one fresh float32 copy, restore all settings, and save nothing."""
    before = snapshot(cam)
    shot = run(cam, exposure_time=0.02, pixel_format=0)
    assert shot.file is None and not cam.written
    assert shot.image.dtype == np.float32 and shot.image.shape == (4, 5)
    assert (shot.image == 901).all()  # Hardware ID deliberately differs from count 1.
    assert snapshot(cam) == before
    cam.pva.image[:] = 0
    assert (shot.image == 901).all()


def test_configured_pva_name_and_override(cam):
    """PVA PV names need not be derived from the CA plugin prefix."""
    run(cam, exposure_time=0.02)
    run(cam, exposure_time=0.02, pva_pv="OTHER:PVA")
    assert cam.read_pvs == ["SIM:Pva1:Image", "OTHER:PVA"]


def test_autosave_is_suppressed_and_restored(cam):
    """A previously enabled TIFF writer must not write our memory-only shot."""
    cam.tiff1.enable.put("Enable")
    cam.tiff1.auto_save.put("Yes")
    before = snapshot(cam)
    run(cam, exposure_time=0.02)
    assert not cam.written and snapshot(cam) == before


def test_matching_tiff_written_once(cam):
    """PVA and TIFF must refer to the same frame, with settings restored afterward."""
    before = snapshot(cam)
    shot = run(
        cam,
        file_path="/data",
        file_name="align",
        file_number=7,
        save_files=True,
        exposure_time=0.02,
    )
    assert shot.file == "/data/align_0007.tif"
    assert cam.written == [("/data", "align", 7)]
    assert cam.written_keys == [cam.key(cam.pva)]
    assert snapshot(cam) == before


def test_default_exposure_and_filename(cam):
    """Use the existing exposure when none is supplied."""
    cam.cam.acquire_time.put(0.02)
    shot = run(cam, file_path="/data/", save_files=True)
    assert shot.file == "/data/sample_20ms_0001.tif"


@pytest.mark.parametrize("save", [False, True])
def test_late_old_frame_drained_before_new_settings(cam, save):
    """An old frame delayed longer than 0.2s cannot become the shot or its TIFF."""
    Signal.put(cam.cam.acquire, 1)
    cam.queue_frame(0.35)
    started = time.monotonic()
    opts = dict(file_path="/data", file_name="align", save_files=True) if save else {}
    shot = run(cam, exposure_time=0.12, **opts)
    assert time.monotonic() - started >= 0.47
    assert (shot.image == 902).all()
    assert cam.cam.acquire.get() == 1
    assert cam.written == ([("/data", "align", 1)] if save else [])


def test_acquisition_waits_for_delayed_pipeline(cam):
    """Camera idle is insufficient when its frame is still processing downstream."""
    cam.pipeline_delay = 0.35
    started = time.monotonic()
    shot = run(cam, exposure_time=0.02)
    assert time.monotonic() - started >= 0.37
    assert (shot.image == 901).all()


@pytest.mark.parametrize("fails", [False, True])
def test_delayed_write_completion_and_error(cam, fails):
    """A late disk-full error must be observed before returning or restoring paths."""
    cam.write_delay, cam.tiff_fails = 0.35, fails
    before = snapshot(cam)
    started = time.monotonic()
    if fails:
        with pytest.raises(RuntimeError, match="disk full"):
            run(cam, file_path="/data", exposure_time=0.02, save_files=True)
    else:
        run(cam, file_path="/data", exposure_time=0.02, save_files=True)
    assert time.monotonic() - started >= 0.37
    assert cam.tiff1.write_file.get() == 0
    assert snapshot(cam) == before


def test_mismatched_pva_rejected(cam, monkeypatch):
    """Even a fresh counter cannot justify returning a stale/incorrect PVA object."""
    monkeypatch.setattr(
        m, "_read_pva_frame", lambda *a: m._PvaFrame(cam.pva.image, (0, 0, 0))
    )
    before = snapshot(cam)
    with pytest.raises(RuntimeError, match="PVA image does not match"):
        run(cam, exposure_time=0.02)
    assert snapshot(cam) == before


def test_tiff_mismatch_does_not_write(cam):
    """A TIFF branch that lost this frame must not save its previous buffer."""
    publish = cam._publish

    def corrupt(*args):
        publish(*args)
        cam.tiff1.unique_id.put(123)

    cam._publish = corrupt
    with pytest.raises(RuntimeError, match="TIFF buffer does not match"):
        run(cam, file_path="/data", exposure_time=0.02, save_files=True)
    assert not cam.written


@pytest.mark.parametrize("field", ["capture", "write_file"])
def test_active_writer_rejected_before_mutation(cam, field):
    """An open capture/write cannot be restored by restoring its configuration."""
    Signal.put(getattr(cam.tiff1, field), 1)
    before = snapshot(cam)
    with pytest.raises(RuntimeError, match="already active"):
        run(cam)
    assert snapshot(cam) == before
    Signal.put(getattr(cam.tiff1, field), 0)


def test_missing_pva_frame_restores(cam):
    """A dropped PVA callback is an error even though the camera exposed."""
    publish = cam._publish

    def drop(*args):
        publish(*args)
        cam.pva.array_counter.put(100)

    cam._publish = drop
    before = snapshot(cam)
    with pytest.raises(RuntimeError, match="exactly one new frame from the PVA"):
        run(cam, exposure_time=0.02)
    assert snapshot(cam) == before


def test_stop_failure_does_not_restore_settings_while_acquiring(cam, monkeypatch):
    """A refused stop must not be followed by mode changes or a live-view restart."""
    monkeypatch.setattr(m, "_STATE_TIMEOUT", 0.12)
    command = cam.cam.acquire.put
    changes = []
    setting = cam.cam.image_mode.set

    def hang(stop):
        cam.queue_frame(0)

    def refuse(value, callback=None, **kw):
        if value:
            return command(value, callback=callback, **kw)
        if callback:
            callback()

    def record(value, **kw):
        changes.append((value, cam.cam.acquire.get()))
        return setting(value, **kw)

    cam._run = hang
    cam.cam.acquire.put = refuse
    cam.cam.image_mode.set = record
    with pytest.raises(ExceptionGroup, match="acquisition and restoration"):
        run(cam, exposure_time=0.02)
    assert ("Continuous", 1) not in changes
    cam.cam.acquire.put = command


def test_restore_failure_attempts_remaining_settings_without_restart(cam):
    """A bad PV must not skip exposure restoration, or rearm writers/live view."""
    Signal.put(cam.cam.acquire, 1)
    mode = cam.cam.image_mode.set

    def refuse(value, **kw):
        if value == "Continuous":
            raise RuntimeError("image-mode restore refused")
        return mode(value, **kw)

    cam.cam.image_mode.set = refuse
    with pytest.raises(RuntimeError, match="image-mode restore refused"):
        run(cam, exposure_time=0.02)
    assert cam.cam.acquire_time.get() == 0.2
    assert cam.cam.trigger_mode.get() == 3
    assert cam.cam.acquire.get() == 0
    assert cam.tiff1.enable.get() == "Disable" and cam.tiff1.auto_save.get() == "No"


def test_setup_failure_and_restore_failure_both_reported(cam):
    """Cleanup errors must not hide the acquisition's original error."""
    mode = cam.cam.image_mode.set

    def fail(value, **kw):
        if value == "Continuous":
            raise RuntimeError("restore refused")
        return mode(value, **kw)

    cam.cam.image_mode.set = fail
    cam.cam.pixel_format.set = lambda *a, **k: (_ for _ in ()).throw(
        ValueError("bad format")
    )
    with pytest.raises(ExceptionGroup) as caught:
        run(cam, exposure_time=0.02, pixel_format=0)
    assert "bad format" in str(caught.value.exceptions[0])
    assert "restore refused" in str(caught.value.exceptions[1])
    assert cam.cam.acquire_time.get() == 0.2


def test_restart_failure_reported(cam, monkeypatch):
    """Returning a shot must not imply live view resumed when the start was ignored."""
    monkeypatch.setattr(m, "_STATE_TIMEOUT", 0.12)
    Signal.put(cam.cam.acquire, 1)
    put, starts = cam.cam.acquire.put, []

    def ignore_restart(value, callback=None, **kw):
        if value:
            starts.append(1)
            if len(starts) == 2:
                if callback:
                    callback()
                return
        return put(value, callback=callback, **kw)

    cam.cam.acquire.put = ignore_restart
    with pytest.raises(RuntimeError, match="did not resume"):
        run(cam, exposure_time=0.02)


def test_abort_restores_settings(cam):
    """Aborting a long exposure stops it and restores its original settings."""
    before = snapshot(cam)
    re = RunEngine({})
    timer = threading.Timer(0.12, re.request_pause)
    timer.start()
    with pytest.raises(RunEngineInterrupted):
        m.run_microscope_acquire(re, microscope=cam, exposure_time=0.6)
    re.abort()
    timer.join()
    assert snapshot(cam) == before


@pytest.mark.parametrize(
    "kw",
    [
        dict(exposure_time=-1),
        dict(exposure_time=float("nan")),
        dict(exposure_time=float("inf")),
        dict(save_files=True),
        dict(file_path="/data"),
        dict(file_name="x"),
        dict(file_number=-1),
    ],
)
def test_bad_arguments_do_not_mutate(cam, kw):
    """Validate arguments before stopping live view or changing any settings."""
    before = snapshot(cam)
    with pytest.raises(ValueError):
        run(cam, **kw)
    assert snapshot(cam) == before


def test_timeout_counts_read_delays(monkeypatch):
    """Slow reads must consume the deadline, not extend it by the sleep budget."""
    signal = Signal(name="slow", value=0)
    read = signal.read

    def slow():
        time.sleep(0.08)
        return read()

    monkeypatch.setattr(signal, "read", slow)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        RunEngine({})(m._wait_for(signal, lambda x: False, 0.1, "deadline"))
    assert time.monotonic() - started < 0.3


def test_sorting_rejected_before_mutation(cam):
    """Sort buffers are not represented by callback queue counts."""
    cam.transform1.sort_mode.put("Sorted")
    before = snapshot(cam)
    with pytest.raises(ValueError, match="disable plugin sorting"):
        run(cam)
    assert snapshot(cam) == before


def test_undeclared_upstream_rejected_before_mutation(cam):
    """An omitted ancestor could still publish old frames after our baseline."""
    cam.pva.nd_array_port.put("MISSING")
    before = snapshot(cam)
    with pytest.raises(ValueError, match="undeclared microscope upstream"):
        run(cam)
    assert snapshot(cam) == before


def test_pipeline_cycle_rejected_before_mutation(cam):
    """Miswired ports must fail early, not recurse or hang during queue draining."""
    cam.transform1.nd_array_port.put("PVA")
    before = snapshot(cam)
    with pytest.raises(ValueError, match="cycle"):
        run(cam)
    assert snapshot(cam) == before


def test_missing_queue_pv_does_not_stop_live_view(cam):
    """Required ADCore PVs must be checked before issuing any camera command."""
    Signal.put(cam.cam.acquire, 1)
    before = snapshot(cam)

    def unavailable():
        raise RuntimeError("queue PV unavailable")

    cam.transform1.num_queued_arrays.read = unavailable
    with pytest.raises(RuntimeError, match="queue PV unavailable"):
        run(cam)
    assert snapshot(cam) == before


def test_active_finite_acquisition_is_not_restarted(cam):
    """A partly completed finite acquisition cannot be resumed by starting over."""
    cam.cam.image_mode.put("Single")
    Signal.put(cam.cam.acquire, 1)
    before = snapshot(cam)
    with pytest.raises(RuntimeError, match="only continuous"):
        run(cam)
    assert snapshot(cam) == before
