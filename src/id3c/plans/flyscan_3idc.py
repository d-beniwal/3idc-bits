"""Fly scan for 3-ID-C: area detector *vs*. motor.

Fly scan an EPICS motor and collect Eiger2 (or any AreaDetector) images.

Software coordination only, no hardware triggering.

Usage
-----

From a command-line session::

    from id3c.startup import *           # provides RE, oregistry, etc.

    # The plan: drive a fly scan and record a Bluesky run.
    uid, = RE(flyscan(p_start=0, p_end=5, exposures_per_egu=10, t_period=0.05))

General outline
---------------

1. Preparation

   * validate inputs
   * collect metadata
   * snapshot anything we will modify, so we can restore it later
   * taxi the motor (at current velocity) to a position just before *p_start*

2. Takeoff

   * stage the detector
   * open the run
   * start the detector acquiring continuously
   * launch the motor toward (actually past) *p_end* (at computed flyscan velocity)

3. Monitor

   * report one event per captured frame in the ``primary`` stream
   * once the motor crosses *p_end*, stop the detector image
     acquisitions and the motor movement

4. Conclusion

   * close the run
   * drain the detector pipeline
   * verify the HDF5 file landed
   * select the in-scan subset by position and write it to the HDF5/NeXus master file
   * restore everything in step 1 snapshots

Implementation note: this plan is software-correlated (no hardware
gate or trigger signal). Frame-to-position pairing happens downstream
from the ``monitor_during_decorator`` streams, joined by IOC timestamp.
Waits inside the plan use ophyd ``Status`` objects (``MoveStatus``,
``SubscriptionStatus``, ``AndStatus``) driven by CA monitor
callbacks rather than busy-poll loops.

The ``hdf_t_phase_offset`` kwarg is an IOC/detector-specific
calibration constant; measure it once with
``flyscan_3idc_analysis.hdf_timestamp_semantic_diagnostic`` (see
that function's docstring for the procedure).
"""

import logging
import queue
import time
import warnings
from dataclasses import dataclass

from apsbits.core.instrument_init import oregistry
from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from bluesky.utils import FailedStatus
from bluesky.utils import plan as bluesky_plan
from epics import caget
from ophyd import ADBase
from ophyd import EpicsMotor
from ophyd import Kind
from ophyd.status import AndStatus
from ophyd.status import SubscriptionStatus
from ophyd.utils.errors import WaitTimeoutError


def ad_files_dirname(det):
    """Name of the image-files symlink adjacent to the master file.

    Per detector: ``"{det.name}_files"`` (e.g. ``eiger2_files``).  A
    beamline with several area detectors can give each its own root.
    """
    return f"{det.name}_files"


def ad_files_root_for(det):
    """Relative-link root for the master's external link to the AD file.

    ``"./{det.name}_files/"``.  MUST stay relative (portability).
    See ``_external_link_target``.
    """
    return f"./{ad_files_dirname(det)}/"


UNLIMITED_FRAMES = 500_000
"""Hard cap on ``cam.num_images`` (Eiger rejects larger values)."""

MAX_ACQUISITION_SECONDS = 300_000
"""Soft cap on ``num_images * acquire_period`` (~3.5 days).

The Eiger refuses to arm when the implied total acquisition
duration exceeds ~600_000 s.  Stay well under that.  See
``effective_num_images``.
"""


def effective_num_images(t_period: float) -> int:
    """Return the ``cam.num_images`` value to stage for a given period.

    Caps at ``UNLIMITED_FRAMES`` and at
    ``MAX_ACQUISITION_SECONDS / t_period``, whichever is smaller.
    """
    by_time = int(MAX_ACQUISITION_SECONDS / max(t_period, 1e-6))
    return max(1, min(UNLIMITED_FRAMES, by_time))


logger = logging.getLogger(__name__)
# Default the module's own logger to INFO so diagnostics show up in a
# fresh CLI session without the user having to configure logging first.
# We deliberately raise *only this module's* level (not the root logger
# or ophyd's loggers) so the pyepics/ophyd control layer stays quiet.
# If a parent logger has been configured (e.g. apsbits set up a console
# handler on the root logger), our records will propagate to it.  If
# nothing is configured at all, attach a minimal handler so messages
# still reach the terminal.
logger.setLevel(logging.DEBUG)
if not logger.handlers and not logging.getLogger().handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(_h)
    # Don't bubble up to a (possibly later-configured) root that might
    # double-print; the local handler is sufficient in this fallback.
    logger.propagate = False


# ---------------------------------------------------------------------------
# Module-level tunable timing constants.
#
# These are the wake-up ticks for plan-side loops that wait on a
# status-object flag.  CA monitor callbacks update
# the flags asynchronously on the pyepics dispatch thread; the plan
# wakes up at these intervals to check the flag and decide whether to
# proceed.
#
# Tradeoff in either direction: smaller tick = lower latency to
# noticing the flag flipped, higher RunEngine wake-up rate.
# Sub-50 ms ticks are imperceptible to human watchers of progress
# events; sub-1 ms ticks waste CPU without observable benefit.
#
# Adjust here rather than at call sites — keeps related knobs together
# and discoverable.

_CONSUMER_TICK_DEFAULT = 0.02
"""Default wake-up tick for monitor_loop's consumer.

Also the default for the ``flyscan(_consumer_tick=...)`` plan kwarg.
20 ms = 50 Hz.
"""

_CLEANUP_DRAIN_TICK = 0.05
"""Wake-up tick for wait_for_acquire_drained in the cleanup path.

Cleanup latency does not matter for live progress, so coarser is
fine; 50 ms = 20 Hz.
"""

_FRAME_QUEUE_SIZE = 64
"""Bounded size for monitor_loop's per-frame producer/consumer queue.

The producer (a CA monitor callback on hdf1.num_captured) pushes
(timestamp, value) tuples; the consumer drains in monitor_loop.  At
typical scan rates (10 Hz frames, 20 ms consumer tick), the queue
stays nearly empty.

This bound exists to detect a producer/consumer mismatch — overflow
is logged once as a WARNING and the entry is dropped.  Increase here
for higher-rate scans.
"""


# Public API of this module.  Other symbols (validators,
# snapshot/restore helpers, monitor_loop, the _wait_for helper,
# motor_is_moving, etc.) are implementation detail — accessible
# via direct import for diagnostics but not advertised as a
# stable interface.
__all__ = [
    "flyscan",  # the bluesky plan
    "configure_adsimdet",  # standalone diagnostic, no plan/RE
]


class CacheParameters(dict):
    """Remember original ophyd signal settings for later restoration.

    Usage::

        cache = CacheParameters()
        # motor1.velocity is 0.5
        yield from cache.override(motor1.velocity, 1.5)
        # motor1.velocity is now 1.5
        # ...
        yield from cache.restore()
        # motor1.velocity is back to 0.5
    """

    @bluesky_plan
    def override(self, signal, value):
        """Cache the current value of ``signal`` and set it to ``value``.

        This is a bluesky plan stub; use with ``yield from``.
        """
        # Only cache the first time we override a given signal so that
        # repeated overrides still restore back to the *original* value.
        if signal not in self:
            self[signal] = signal.get()
        yield from bps.mv(signal, value)

    @bluesky_plan
    def restore(self, clear=True):
        """Restore all cached signals to their original values.

        This is a bluesky plan stub; use with ``yield from``.

        Parameters
        ----------
        clear : bool
            If True (default), clear the cache after restoring.
        """
        if not self:
            return
        # Restore in reverse order of how they were added, so that
        # signals with ordering dependencies (e.g. set velocity *after*
        # restoring acceleration) are handled correctly.
        for signal, value in reversed(self.items()):
            yield from bps.mv(signal, value)
        if clear:
            self.clear()


def read_motor_field(motor, suffix, timeout=1.0):
    """Read an EPICS motor-record field (e.g. ``.VMAX``) ad-hoc.

    Returns the field value, or ``None`` if the PV does not connect
    within ``timeout`` seconds or any other read error occurs.

    Uses ``epics.caget`` directly rather than constructing a throwaway
    ``EpicsSignal``.  ``caget`` runs on the calling thread's CA context
    and does no asynchronous metadata fetching.

    A throwaway ``EpicsSignal`` is avoided because it can segfault when
    pyepics fires a deferred metadata callback after the signal's CA
    channel is torn down, and it would also need suppression to avoid
    polluting the global oregistry.
    """
    pv = motor.prefix + suffix
    try:
        value = caget(pv, timeout=timeout)
        if value is None:
            # caget returns None on connection timeout (no exception).
            logger.debug("read_motor_field(%s) timed out", pv)
            return None
        logger.debug("read_motor_field(%s) -> %r", pv, value)
        return value
    except Exception as exc:
        logger.debug("read_motor_field(%s) failed: %r", pv, exc)
        return None


def preflight_connectivity(det, det_name, flymotor, flymotor_name, timeout=2.0):
    """Quick CA-connectivity sanity check before staging.

    Touches a small, representative set of PVs on ``det`` and
    ``flymotor`` and raises ``RuntimeError`` with a clear, single-line
    message if any of them fail to connect within ``timeout`` seconds.

    The goal is to fail *before* device staging when an IOC is down or
    wedged.  Staging issues many serial writes (``set_and_wait`` with
    a default 5-second connection timeout each), so a dead IOC at
    stage time turns into 30+ seconds of cleanup-of-cleanup noise
    that obscures the actual cause.  Detecting it here gives the
    user a one-line error instead.

    Why these specific PVs:
        * ``flymotor.user_readback``: motor record exists at all
        * ``flymotor.motor_done_move``: motor record is responsive
        * ``det.cam.acquire``: cam IOC is up
        * ``det.hdf1.capture``: HDF plugin is up (if present)
        * ``det.hdf1.num_captured``: HDF plugin readback works

    Components are accessed via ``getattr``, so any not-yet-instantiated
    ophyd Components are instantiated here.  That is the *intended*
    behavior: instantiate them now, while we still have IOC contact,
    so later code (including cleanup) does not pay the
    ``wait_for_connection`` price.
    """
    to_check = [
        ("flymotor.user_readback", getattr(flymotor, "user_readback", None)),
        ("flymotor.motor_done_move", getattr(flymotor, "motor_done_move", None)),
        ("det.cam.acquire", getattr(getattr(det, "cam", None), "acquire", None)),
    ]
    # Optional components: only check if the class declares them.
    if _has_component(det, "hdf1"):
        hdf1 = det.hdf1
        if _has_component(hdf1, "capture"):
            to_check.append(("det.hdf1.capture", hdf1.capture))
        if _has_component(hdf1, "num_captured"):
            to_check.append(("det.hdf1.num_captured", hdf1.num_captured))

    failed = []
    for label, sig in to_check:
        if sig is None:
            failed.append(f"{label} missing")
            continue
        try:
            sig.wait_for_connection(timeout=timeout)
        except Exception as exc:
            failed.append(f"{label} ({sig.pvname}): {exc}")

    if failed:
        msg = (
            f"preflight_connectivity failed for det={det_name!r}"
            f" flymotor={flymotor_name!r} (IOC down?): " + "; ".join(failed)
        )
        logger.error(msg)
        raise RuntimeError(msg)
    logger.info(
        "preflight_connectivity OK: checked %d PV(s) on det=%r flymotor=%r",
        len(to_check),
        det_name,
        flymotor_name,
    )


def check_hdf_file_path(det, settle_timeout=1.0):
    """Verify the HDF plugin can see the configured file_path.

    Returns silently on success.  Raises ``RuntimeError`` if the IOC
    reports ``FilePathExists_RBV == 0`` after ``settle_timeout``
    seconds.

    Why this is a separate gate (not just a stage_sig):

    The HDF plugin's ``file_path`` must be set, *and* the path must
    actually exist on the IOC's filesystem (i.e. inside the IOC's
    container if it's containerized), *before* ``capture`` is set to
    1 at stage time.  If the path doesn't exist when capture starts,
    the plugin fails at file-open with a generic
    ``Error writing file: status=3`` message that surfaces only via
    the plugin's ``WriteMessage`` PV — bluesky/ophyd never raises,
    and ``num_captured`` stays at 0 forever.

    This check must run *after* ``file_path`` has been written to the
    IOC (otherwise we'd be checking a stale value).  ``FilePathExists``
    is updated by the plugin in response to a ``file_path`` write, but
    not instantly — the wait below absorbs that settling delay via the
    ``_wait_for`` helper (precheck + CA monitor subscription).
    """
    if not _has_component(det, "hdf1") or not _has_component(
        det.hdf1, "file_path_exists"
    ):
        logger.debug(
            "check_hdf_file_path: %s lacks hdf1.file_path_exists; skipping",
            det.name,
        )
        return

    try:
        _wait_for(
            det.hdf1.file_path_exists,
            lambda value: value == 1,
            timeout=settle_timeout,
        )
    except WaitTimeoutError as exc:
        exists = det.hdf1.file_path_exists.get(use_monitor=False)
        current_path = det.hdf1.file_path.get(use_monitor=False)
        msg = (
            f"HDF plugin reports file_path does not exist on the IOC's"
            f" filesystem: {det.name}.hdf1.file_path={current_path!r}"
            f" (file_path_exists={exists}). The path must exist (and be"
            f" writable) on the IOC's filesystem, which may be a container"
            f" view distinct from the host filesystem. Create the directory"
            f" inside the IOC, or correct ad_file_path."
        )
        logger.error(msg)
        raise RuntimeError(msg) from exc

    current_path = det.hdf1.file_path.get(use_monitor=False)
    logger.info(
        "check_hdf_file_path: OK (%s.hdf1.file_path=%r)",
        det.name,
        current_path,
    )


def configure_adsimdet(
    det,
    *,
    ad_file_path="/tmp/flyscan/",
    ad_file_name="flyscan",
    ad_file_template="%s%s_%6.6d.h5",
    ad_file_number=1,
    acquire_time=0.02,
    acquire_period=0.1,
    capture_duration=2.0,
    num_capture=None,
    capture_arm_timeout=5.0,
    drain_timeout=10.0,
    do_capture=True,
    do_acquire=True,
):
    """Configure & exercise an AD HDF5 detector without a plan.

    Diagnostic helper.  No RunEngine, no plan, no stage_decorator — just
    straight ophyd ``put()`` calls in the order the IOC needs them.

    Simulates the flyscan acquisition protocol:

        1. Configure file destination & cam timings.
        2. Cam in ``Continuous`` image_mode.
        3. ``num_capture = UNLIMITED_FRAMES`` (capture until told to stop).
        4. Arm capture (``hdf1.capture.put(1)``).
        5. **Wait** for ``Capture_RBV == 'Capturing'`` — this avoids a
           race in which the cam starts producing frames before the HDF
           plugin is ready to receive them.  Without this wait, the
           leading frames of a scan are silently dropped (not counted
           in ``dropped_arrays`` because the plugin isn't even
           listening yet).
        6. Start cam acquire.
        7. Sleep ``capture_duration`` seconds (simulates the motor
           trajectory window in a real flyscan).
        8. Stop capture (``hdf1.capture.put(0)``).
        9. Drain: wait until ``num_queued_arrays == 0`` so all
           in-flight frames flush to disk before the file is closed.
        10. Stop cam acquire.
        11. Snapshot relevant PVs and return.

    Returns a dict of the post-operation PV snapshot.

    Usage::

        from flyscan_3idc import configure_adsimdet
        result = configure_adsimdet(adsimdet, capture_duration=3.0)
        for k, v in result.items():
            print(f"  {k}: {v}")

    Parameters
    ----------
    capture_duration : float
        Seconds to leave both capture and acquire active.  Total file
        write count is approximately ``capture_duration / acquire_period``.
    capture_arm_timeout : float
        Maximum seconds to wait for ``Capture_RBV`` to transition to
        ``'Capturing'`` after arming.  Raises ``RuntimeError`` on timeout.
    drain_timeout : float
        Maximum seconds to wait for ``num_queued_arrays`` to reach 0
        after stopping capture.  Logs a warning on timeout but does
        not raise.
    do_capture, do_acquire : bool
        Skip arming capture or starting acquire, respectively.  Useful
        for narrowing down which step misbehaves.
    """
    logger.info(
        "configure_adsimdet(%s): file_path=%r file_name=%r template=%r"
        " number=%d acquire_time=%g acquire_period=%g"
        " capture_duration=%g do_capture=%s do_acquire=%s",
        det.name,
        ad_file_path,
        ad_file_name,
        ad_file_template,
        ad_file_number,
        acquire_time,
        acquire_period,
        capture_duration,
        do_capture,
        do_acquire,
    )

    # UNLIMITED_FRAMES is now a module-level constant; see top of file.

    # 1. File destination (must happen before capture is armed)
    logger.info("configure_adsimdet: setting file_path=%r", ad_file_path)
    det.hdf1.file_path.put(ad_file_path)
    logger.info("configure_adsimdet: setting file_name=%r", ad_file_name)
    det.hdf1.file_name.put(ad_file_name)
    logger.info("configure_adsimdet: setting file_template=%r", ad_file_template)
    det.hdf1.file_template.put(ad_file_template)
    logger.info("configure_adsimdet: setting file_number=%d", ad_file_number)
    det.hdf1.file_number.put(ad_file_number)

    # 2. File save behavior — required so capture actually writes the file
    logger.info("configure_adsimdet: setting auto_save=Yes")
    det.hdf1.auto_save.put("Yes")
    logger.info("configure_adsimdet: setting auto_increment=Yes")
    det.hdf1.auto_increment.put("Yes")
    # file_write_mode='Capture' matches the arm-capture / continuous-cam /
    # stop-on-timer / drain-then-stop protocol below.  In 'Capture' mode
    # the plugin queues frames while Capture=1 and writes them to a
    # single HDF5 file when Capture goes to 0.  'Stream' mode has subtler
    # file-lifecycle semantics that have been associated with
    # num_captured=0 even when cam frames are flowing.
    logger.info("configure_adsimdet: setting file_write_mode='Capture'")
    det.hdf1.file_write_mode.put("Capture")

    # 3. Permit creating the destination directory if missing
    if _has_component(det.hdf1, "create_directory"):
        logger.info("configure_adsimdet: setting create_directory=-5")
        det.hdf1.create_directory.put(-5)

    # 4. Verify the IOC now sees the path
    time.sleep(0.1)  # let the IOC settle
    exists = det.hdf1.file_path_exists.get(use_monitor=False)
    if exists != 1:
        msg = (
            f"configure_adsimdet: HDF plugin reports file_path does not"
            f" exist after setting it to {ad_file_path!r}"
            f" (file_path_exists={exists})."
        )
        logger.error(msg)
        raise RuntimeError(msg)
    logger.info("configure_adsimdet: file_path_exists=1")

    # 5. Capture sizing.
    #
    # In 'Capture' mode, num_capture is an *upper bound*: the plugin
    # stops capturing when this count is reached, and the on-disk
    # dataset is sized to the number of frames actually captured.
    #
    # CAUTION: num_capture cannot be made arbitrarily large.  The IOC's
    # NDFileHDF5 plugin computes byte counts as C int arithmetic during
    # dataset pre-allocation; values around 1e9 with Float64 1024x1024
    # frames overflow, producing a file whose num_captured counter
    # advances but whose /entry/data/data dataset is never written.
    # Keep num_capture <= ~1e6 for typical frame sizes.
    #
    # num_capture is sized for the expected count times 1.5 plus 20,
    # which absorbs takeoff/landing leading edges, post-stop tail
    # frames, and timing jitter for any sensible scan size.
    expected_frames = int(capture_duration / acquire_period)
    if num_capture is None:
        num_capture = int(expected_frames * 1.5) + 20
    logger.info(
        "configure_adsimdet: setting num_capture=%d (upper bound;"
        " expected ~%d frames from capture_duration=%g / period=%g)",
        num_capture,
        expected_frames,
        capture_duration,
        acquire_period,
    )
    det.hdf1.num_capture.put(num_capture)

    # 6. Cam configuration: Continuous mode, run until told to stop.
    logger.info("configure_adsimdet: setting cam.acquire_time=%g", acquire_time)
    det.cam.acquire_time.put(acquire_time)
    logger.info("configure_adsimdet: setting cam.acquire_period=%g", acquire_period)
    det.cam.acquire_period.put(acquire_period)
    logger.info("configure_adsimdet: setting cam.image_mode='Continuous'")
    det.cam.image_mode.put("Continuous")
    # cam.num_images is irrelevant in Continuous mode but some IOC builds
    # enforce a sanity limit, so a large number is safer than 0.  This
    # is *not* the count that sizes the HDF dataset — that's num_capture.
    logger.info("configure_adsimdet: setting cam.num_images=%d", UNLIMITED_FRAMES)
    det.cam.num_images.put(UNLIMITED_FRAMES)

    # 7. Arm the HDF plugin and *wait* for it to reach the 'Capturing'
    #    state.  Without the wait, cam frames that arrive before capture
    #    is ready are silently dropped.
    #
    # ``capture`` is an EpicsSignalWithRBV; ``set(1)`` runs the generic
    # Signal.set() path which puts and then polls Capture_RBV until it
    # equals the setpoint (enum 1 == "Capturing").  That's exactly the
    # condition we want, so we wait on the returned Status object
    # instead of running our own poll loop.
    if do_capture:
        logger.info(
            "configure_adsimdet: arming capture (det.hdf1.capture.set(1))",
        )
        try:
            det.hdf1.capture.set(1).wait(timeout=capture_arm_timeout)
        except (WaitTimeoutError, TimeoutError) as exc:
            state = det.hdf1.capture.get(use_monitor=False, as_string=True)
            msg = (
                f"configure_adsimdet: HDF plugin did not reach 'Capturing'"
                f" state within {capture_arm_timeout:g}s after arming"
                f" (last state={state!r}, write_status="
                f"{det.hdf1.write_status.get(use_monitor=False, as_string=True)!r},"
                f" write_message="
                f"{det.hdf1.write_message.get(use_monitor=False, as_string=True)!r})."
                f" Underlying error: {exc!r}"
            )
            logger.error(msg)
            raise RuntimeError(msg) from exc
        logger.info("configure_adsimdet: capture is 'Capturing' (armed)")

    # 8. Start the cam in continuous mode.
    if do_acquire:
        logger.info(
            "configure_adsimdet: starting acquisition (cam.acquire.put(1))",
        )
        det.cam.acquire.put(1)

        # 9. Hold for the requested duration (simulates motor trajectory).
        logger.info(
            "configure_adsimdet: capturing for %g s ...",
            capture_duration,
        )
        time.sleep(capture_duration)
        captured_during = det.hdf1.num_captured.get(use_monitor=False)
        queued = det.hdf1.num_queued_arrays.get(use_monitor=False)
        logger.info(
            "configure_adsimdet: after %g s: num_captured=%d, queued=%s",
            capture_duration,
            captured_during,
            queued,
        )

    # 10. Stop capture.  In Capture mode this halts further frame
    #     accumulation but does NOT reliably flush the file to disk
    #     when stopped early (i.e. before num_capture is reached),
    #     even with auto_save=Yes.  We must explicitly press WriteFile
    #     below to force the flush.
    if do_capture:
        logger.info("configure_adsimdet: stopping capture (put 0)")
        det.hdf1.capture.put(0)

        # 11. Drain: wait until the plugin has flushed any queued frames
        #     from its in-memory queue into the file buffer.
        #
        # num_queued_arrays is an RBV-only PV (no .set() to lean on);
        # use the _wait_for helper.  Timeout is a warning, not a
        # raise.  int() cast in the predicate guards against pyepics
        # returning the value as a string for certain record types.
        try:
            _wait_for(
                det.hdf1.num_queued_arrays,
                lambda value: int(value) == 0,
                timeout=drain_timeout,
            )
            logger.info(
                "configure_adsimdet: HDF queue drained (num_queued_arrays=0)",
            )
        except (WaitTimeoutError, TimeoutError):
            logger.warning(
                "configure_adsimdet: HDF queue did not drain within"
                " %g s (num_queued_arrays=%s)",
                drain_timeout,
                det.hdf1.num_queued_arrays.get(use_monitor=False),
            )

        # 12. Explicitly flush the file to disk.  WriteFile=1 forces
        #     the plugin to write out whatever it has captured.  This
        #     is required when stopping capture before num_capture is
        #     reached: neither Capture=0 alone nor auto_save=Yes flushes
        #     in that case (the file ends up with the NeXus skeleton but
        #     no image dataset).
        captured = det.hdf1.num_captured.get(use_monitor=False)
        if captured > 0:
            logger.info(
                "configure_adsimdet: flushing file via write_file=1 (num_captured=%d)",
                captured,
            )
            # Note: we use put() + _wait_for here rather than
            # write_file.set(1).wait().  Both write_file and its
            # _RBV are EpicsSignalWithRBV; .set(1) would call
            # _set_and_wait which completes when WriteFile_RBV
            # equals the setpoint (1, "Writing") — i.e. at the
            # *start* of the write, not its completion.  The
            # operation we need to wait for is the back-to-idle
            # transition (RBV returns to 0, "Done").  So we
            # put() the trigger and _wait_for the idle state.
            det.hdf1.write_file.put(1)
            try:
                _wait_for(
                    det.hdf1.write_file,
                    lambda value: int(value) == 0,
                    timeout=drain_timeout,
                )
                logger.info(
                    "configure_adsimdet: write_file completed (full_file_name=%r)",
                    det.hdf1.full_file_name.get(
                        use_monitor=False,
                        as_string=True,
                    ),
                )
            except (WaitTimeoutError, TimeoutError):
                logger.warning(
                    "configure_adsimdet: write_file did not complete"
                    " within %g s (write_status=%r write_message=%r)",
                    drain_timeout,
                    det.hdf1.write_status.get(use_monitor=False, as_string=True),
                    det.hdf1.write_message.get(use_monitor=False, as_string=True),
                )
        else:
            logger.info(
                "configure_adsimdet: skipping write_file (no frames captured)",
            )

    # 13. Stop the cam.
    if do_acquire:
        logger.info("configure_adsimdet: stopping acquire (put 0)")
        det.cam.acquire.put(0)

    # 13. Report.
    result = {
        "cam.acquire": det.cam.acquire.get(use_monitor=False, as_string=True),
        "cam.array_counter": det.cam.array_counter.get(use_monitor=False),
        "cam.detector_state": det.cam.detector_state.get(
            use_monitor=False, as_string=True
        )
        if _has_component(det.cam, "detector_state")
        else None,
        "hdf1.capture": det.hdf1.capture.get(use_monitor=False, as_string=True),
        "hdf1.num_captured": det.hdf1.num_captured.get(use_monitor=False),
        "hdf1.num_queued_arrays": det.hdf1.num_queued_arrays.get(use_monitor=False),
        "hdf1.dropped_arrays": det.hdf1.dropped_arrays.get(use_monitor=False),
        "hdf1.write_status": det.hdf1.write_status.get(
            use_monitor=False, as_string=True
        ),
        "hdf1.write_message": det.hdf1.write_message.get(
            use_monitor=False, as_string=True
        ),
        "hdf1.full_file_name": det.hdf1.full_file_name.get(
            use_monitor=False, as_string=True
        ),
        "hdf1.file_path_exists": det.hdf1.file_path_exists.get(use_monitor=False),
    }
    logger.info("configure_adsimdet: result=%s", result)
    return result


_ACCL_FALLBACK_SECONDS = 0.25
"""Fallback acceleration (seconds) used when ``.ACCL`` cannot be read.

Deliberately generous; over-allocating the taxi region only costs a
bit of extra travel before the first useful frame.
"""


class FlyscanDataLossWarning(UserWarning):
    """Emitted when a flyscan run loses frames at the HDF plugin input.

    A non-zero delta in ``hdf1.dropped_arrays`` over the run means
    the cam produced frames the HDF plugin couldn't accept.  This
    is a data-integrity concern: the on-disk HDF5 file is missing
    frames the cam exposed.  Surfaced as a ``UserWarning`` subclass
    so it appears in IPython/Jupyter's warning channel in addition
    to the log file, and is filterable independently of other
    warnings by user code (``warnings.filterwarnings('error',
    category=FlyscanDataLossWarning)`` to turn it into an
    exception, for example).
    """


@dataclass(frozen=True)
class FlyscanGeometry:
    """Derived geometry for a flyscan: taxi/coast distances + frame count.

    All distances are in motor engineering units; all times are in
    seconds.  ``accl_was_default`` is True when ``.ACCL`` could not be
    read and the fallback was used.
    """

    num_frames: int
    scan_duration: float  # num_frames * t_period
    scan_velocity: float  # (p_end - p_start) / scan_duration
    d_taxi: float  # 0.5 * scan_velocity * motor_accl
    p_initial: float  # p_start - d_taxi - taxi_allowance
    p_final: float  # p_end + d_taxi + taxi_allowance
    motor_accl: float  # seconds (.ACCL or fallback)
    motor_egu: str  # .EGU string for error messages/metadata
    accl_was_default: bool


def compute_flyscan_geometry(
    flymotor,
    p_start,
    p_end,
    exposures_per_egu,
    t_period,
    taxi_allowance,
):
    """Derive flyscan geometry from user-meaningful kwargs.

    Pure function: no CA writes, no bluesky plan messages.  Reads
    ``.ACCL`` and ``.EGU`` from the motor record via the same caget
    path as ``validate_flyscan_inputs``; ``.ACCL`` falls back to
    ``_ACCL_FALLBACK_SECONDS`` (with a warning log) if the read
    fails so the caller still gets a usable geometry object — the
    velocity-bound check in ``validate_flyscan_inputs`` will then
    catch any downstream impossibility.

    Frame count uses fence-post counting: a frame at both endpoints
    of ``[p_start, p_end]`` plus ``exposures_per_egu`` frames per unit
    of motor travel between them::

        num_frames = round(1 + (p_end - p_start) * exposures_per_egu)

    Taxi distance uses the over-allocating form
    ``d_taxi = 0.5 * scan_velocity * motor_accl`` (the motor record's
    ``.ACCL`` is "seconds to reach .VELO", so this slightly
    overestimates the time to reach ``scan_velocity`` — intentional;
    gives the cam a beat to settle before the first useful frame).
    """
    p_fly_dist = p_end - p_start
    if p_fly_dist <= 0:
        raise ValueError(f"p_end={p_end:g} must be greater than p_start={p_start:g}.")
    if exposures_per_egu <= 0:
        raise ValueError(f"exposures_per_egu={exposures_per_egu:g} must be positive.")
    if taxi_allowance < 0:
        raise ValueError(f"taxi_allowance={taxi_allowance:g} must be non-negative.")
    if t_period <= 0:
        raise ValueError(f"t_period={t_period:g} must be positive.")

    num_frames = int(round(1 + p_fly_dist * exposures_per_egu))
    if num_frames < 2:
        raise ValueError(
            f"computed num_frames={num_frames} (from exposures_per_egu="
            f"{exposures_per_egu:g}, p_fly_dist={p_fly_dist:g}) must be"
            " at least 2."
        )
    scan_duration = num_frames * t_period
    scan_velocity = p_fly_dist / scan_duration

    accl = read_motor_field(flymotor, ".ACCL")
    accl_was_default = False
    if accl is None or accl <= 0:
        logger.warning(
            "compute_flyscan_geometry: motor .ACCL unreadable (got %r);"
            " using fallback %g s.  Taxi region may be under-allocated.",
            accl,
            _ACCL_FALLBACK_SECONDS,
        )
        accl = _ACCL_FALLBACK_SECONDS
        accl_was_default = True

    egu = read_motor_field(flymotor, ".EGU")
    if not isinstance(egu, str):
        egu = ""

    d_taxi = 0.5 * scan_velocity * accl
    p_initial = p_start - d_taxi - taxi_allowance
    p_final = p_end + d_taxi + taxi_allowance

    logger.info(
        "compute_flyscan_geometry: num_frames=%d scan_velocity=%g %s/s"
        " d_taxi=%g %s (ACCL=%g s%s, allowance=%g %s)"
        " => p_initial=%g p_final=%g",
        num_frames,
        scan_velocity,
        egu or "?",
        d_taxi,
        egu or "?",
        accl,
        " [fallback]" if accl_was_default else "",
        taxi_allowance,
        egu or "?",
        p_initial,
        p_final,
    )
    return FlyscanGeometry(
        num_frames=num_frames,
        scan_duration=scan_duration,
        scan_velocity=scan_velocity,
        d_taxi=d_taxi,
        p_initial=p_initial,
        p_final=p_final,
        motor_accl=accl,
        motor_egu=egu,
        accl_was_default=accl_was_default,
    )


def validate_flyscan_inputs(
    det,
    det_name,
    flymotor,
    flymotor_name,
    geometry,
    t_acquire,
    t_period,
    compression,
    velocity_minimum=None,
):
    """Validate flyscan arguments against derived geometry and IOC state.

    ``geometry`` is the ``FlyscanGeometry`` returned by
    ``compute_flyscan_geometry``.  This function does the checks that
    require either a connected device or values that must be
    cross-checked against the motor's velocity limits / the HDF
    plugin's compression enum.

    Velocity-bracket policy (per user spec, flyscan_3idc.py:94-102)::

        v_max = .VELO                         # the motor's currently
                                              # configured target velocity
                                              # is the ceiling for the
                                              # flyscan.  .VMAX is not used
                                              # as the cap (an unset
                                              # .VMAX == 0 in EPICS means
                                              # "no limit", and the user
                                              # specifically wants .VELO
                                              # to govern).
        v_min = max(.VBAS, velocity_minimum)  # honour both the IOC's
                                              # posted base velocity and
                                              # any user-supplied floor.

    Then validate: ``v_min <= geometry.scan_velocity <= v_max``.

    ``.VELO`` is required; if it cannot be read, this function raises
    ``ValueError`` (we refuse to run a flyscan without a known velocity
    ceiling).

    Returns the 5-tuple ``(v_velo, v_vmax, v_vbas, v_max, v_min)``:

    * ``v_velo``: raw ``.VELO`` (always present; equal to ``v_max``).
    * ``v_vmax``: raw ``.VMAX`` (``None`` or ``0`` if not posted by IOC).
    * ``v_vbas``: raw ``.VBAS`` (``None`` or ``0`` if not posted by IOC).
    * ``v_max``: effective ceiling used for the bracket check.
    * ``v_min``: effective floor used for the bracket check.

    All five are recorded in run metadata by ``build_flyscan_md`` so
    downstream analysis can tell raw IOC limits apart from the
    effective bracket the plan chose.

    Raises ``KeyError`` for missing/wrong-type devices and ``ValueError``
    for out-of-range numeric arguments, an unsupported compression, an
    unreadable ``.VELO``, or a ``scan_velocity`` outside the effective
    ``[v_min, v_max]`` bracket.
    """
    if not isinstance(det, ADBase):
        raise KeyError(f"Area Detector {det_name!r} not found in registry.")
    if not isinstance(flymotor, EpicsMotor):
        raise KeyError(f"Motor {flymotor_name!r} not found in registry.")
    if not 0 < t_acquire <= t_period:
        raise ValueError(
            "Acquisition time must be positive and less than or equal to the period."
        )
    # Sanity-check the derivation: this should always hold after a
    # successful compute_flyscan_geometry, but assert it so any future
    # refactor that changes the formula doesn't silently produce an
    # invalid geometry.
    if not (
        geometry.p_initial
        < (geometry.p_initial + geometry.d_taxi)  # p_start
        < (geometry.p_final - geometry.d_taxi)  # p_end
        < geometry.p_final
    ):
        raise ValueError(
            "Derived geometry violates p_initial < p_start < p_end <"
            f" p_final: {geometry!r}"
        )
    if geometry.scan_velocity <= 0:
        raise ValueError(f"scan_velocity={geometry.scan_velocity:g} must be positive.")
    if velocity_minimum is not None and velocity_minimum < 0:
        raise ValueError(f"velocity_minimum={velocity_minimum:g} must be non-negative.")

    # EpicsMotor does not expose .VELO / .VMAX / .VBAS as components.
    # Read ad-hoc; for .VMAX / .VBAS, None or 0 means "no limit on that
    # side" (matches EPICS convention).  .VELO is required: it is the
    # ceiling for the flyscan, and we refuse to run without one.
    v_velo = read_motor_field(flymotor, ".VELO")
    v_vmax = read_motor_field(flymotor, ".VMAX")
    v_vbas = read_motor_field(flymotor, ".VBAS")
    if v_velo is None or v_velo <= 0:
        raise ValueError(
            f"Cannot determine velocity ceiling for {flymotor_name!r}:"
            f" .VELO unreadable or non-positive (got {v_velo!r})."
        )

    # Effective ceiling: .VELO is the cap, independent of .VMAX.
    v_max = float(v_velo)

    # Effective floor: start with .VBAS (None/0 sentinel => no IOC
    # floor), then lift by velocity_minimum if the user supplied one.
    v_min = float(v_vbas) if (v_vbas is not None and v_vbas > 0) else 0.0
    if velocity_minimum is not None:
        v_min = max(v_min, float(velocity_minimum))

    # Bracket check: v_min <= scan_velocity <= v_max.  Compare against
    # the effective bracket and surface the underlying inputs in the
    # error message so the user can tell which knob to turn.
    if geometry.scan_velocity > v_max:
        raise ValueError(
            f"scan_velocity={geometry.scan_velocity:g} exceeds motor"
            f" .VELO={v_velo:g} (effective v_max={v_max:g}) for"
            f" {flymotor_name!r}."
        )
    if v_min > 0 and geometry.scan_velocity < v_min:
        raise ValueError(
            f"scan_velocity={geometry.scan_velocity:g} is below effective"
            f" v_min={v_min:g} (.VBAS={v_vbas!r}, velocity_minimum="
            f"{velocity_minimum!r}) for {flymotor_name!r}."
        )

    # Compression validation against the HDF plugin's enum_strs.  The
    # PV is an mbbi/mbbo enumeration; rejecting bad values here gives
    # the user a useful message ("got 'gzip', expected one of [...]")
    # instead of a much-later IOC-side "invalid value" at write time.
    # Defensive: skip if enum_strs is not populated (offline IOC,
    # mock, etc.) — falls through to IOC rejection at write time.
    enum_strs = ()
    try:
        comp_sig = det.hdf1.compression
        enum_strs = tuple(getattr(comp_sig, "enum_strs", ()) or ())
    except Exception as exc:
        logger.debug(
            "validate_flyscan_inputs: cannot read compression enum_strs: %r",
            exc,
        )
    if enum_strs and compression not in enum_strs:
        raise ValueError(
            f"compression={compression!r} not in HDF plugin's allowed"
            f" set {list(enum_strs)!r}."
        )

    logger.info(
        "validated inputs: det=%r flymotor=%r p=%g/%g/%g/%g num_frames=%d"
        " t_acquire=%g t_period=%g scan_active_duration=%g"
        " scan_velocity=%g (VELO=%r VMAX=%r VBAS=%r ->"
        " effective v_max=%g v_min=%g, velocity_minimum=%r)"
        " compression=%r",
        det_name,
        flymotor_name,
        geometry.p_initial,
        geometry.p_initial + geometry.d_taxi,  # p_start
        geometry.p_final - geometry.d_taxi,  # p_end
        geometry.p_final,
        geometry.num_frames,
        t_acquire,
        t_period,
        geometry.scan_duration,
        geometry.scan_velocity,
        v_velo,
        v_vmax,
        v_vbas,
        v_max,
        v_min,
        velocity_minimum,
        compression,
    )
    return v_velo, v_vmax, v_vbas, v_max, v_min


def build_flyscan_md(
    *,
    plan_name,
    det_name,
    flymotor_name,
    p_start,
    p_end,
    exposures_per_egu,
    t_acquire,
    t_period,
    taxi_allowance,
    compression,
    geometry,
    v_velo,
    v_vmax,
    v_vbas,
    v_max,
    v_min,
    velocity_minimum,
    ad_file_name,
    ad_file_path,
    ad_read_path_template="",
    ad_write_path_template="",
    hdf_num_capture,
    hdf_flush_timeout_max,
    consumer_tick,
    hdf_t_phase_offset,
    detector_names=(),
):
    """Assemble the metadata dict recorded with the run.

    Any plan kwarg that affects scan behavior is recorded here so the
    run document preserves the value used.  Derived values from
    ``geometry`` (``p_initial``, ``p_final``, ``num_frames``,
    ``scan_velocity``, ...) are also recorded so downstream readers
    have both the user-supplied inputs and the resulting plan.
    Internal underscore-prefixed kwargs (e.g. ``_consumer_tick``)
    appear without the underscore in the metadata for readability.

    ``plan_name`` is recorded explicitly here (defaulting to
    ``"flyscan"`` in the calling ``flyscan(...)`` plan).  Bluesky's
    ``RunEngine`` start-doc auto-derivation
    (``getattr(self._plan, "__name__", "")``) cannot be used: the
    ``@bluesky.utils.plan`` decorator wraps the generator in a
    ``Plan`` instance with no ``__name__`` attribute, so the
    auto-derived value is the empty string.  Wrapper plans should
    pass their own name via ``flyscan(..., plan_name="my_wrapper",
    ...)`` so the run's provenance reflects the wrapper, not the
    inner flyscan call.

    ``None``-substitution policy
    ----------------------------

    Every value in the returned dict is HDF5-serialisable: no ``None``
    values escape this function.  This matters because
    ``apstools.callbacks.nexus_writer.NXWriter.write_metadata`` runs
    ``h5py.Group.create_dataset(k, data=v)`` on each item, and
    ``data=None`` raises ``TypeError: One of data, shape or dtype
    must be specified``.

    For each input that may be ``None``, substitute the value the
    validator / plan effectively used in its place, and record a
    sibling boolean so the provenance (real value vs substituted
    default) is recoverable from metadata, matching the
    ``motor_accl_was_default`` pattern:

    +------------------------------+------------------+--------------------------------+
    | metadata key                 | None substitute  | companion boolean              |
    +==============================+==================+================================+
    | ``motor_velocity_max_raw``   | ``float('nan')`` | ``motor_velocity_max_``        |
    |                              |                  | ``was_unreadable``             |
    +------------------------------+------------------+--------------------------------+
    | ``motor_velocity_base_raw``  | ``float('nan')`` | ``motor_velocity_base_``       |
    |                              |                  | ``was_unreadable``             |
    +------------------------------+------------------+--------------------------------+
    | ``velocity_minimum_``        | ``0.0``          | ``velocity_minimum_``          |
    | ``requested``                |                  | ``was_default``                |
    +------------------------------+------------------+--------------------------------+

    Rationale per key:

    * ``.VMAX`` / ``.VBAS``: ``read_motor_field`` returns ``None`` when
      the PV can't be read within its 1.0s timeout.  ``NaN`` is the
      truthful "unknown" sentinel — it remains distinguishable from
      ``0.0`` (which the EPICS motor record uses to mean "no
      IOC-posted limit on that side").  ``NaN`` is a first-class
      float64 value that h5py serialises natively.
    * ``velocity_minimum``: when the user passes ``None`` (the
      default), the validator's effective floor is
      ``max(.VBAS, 0)``, i.e. the kwarg contributes nothing to
      ``v_min``.  Recording ``0.0`` truthfully represents what the
      plan actually used.

    Velocity-related metadata keys (post-substitution):

    * ``motor_velo``: raw ``.VELO`` at scan start (= ``effective_v_max``);
      always present (validator raises if ``.VELO`` is unreadable).
    * ``motor_velocity_max_raw``: raw ``.VMAX`` (informational only;
      the plan does NOT use this as the cap); ``NaN`` if unreadable
      (companion bool: ``motor_velocity_max_was_unreadable``).  A
      finite ``0.0`` here means the IOC posted 0, which in EPICS
      convention means "no hardware max" — different from NaN.
    * ``motor_velocity_base_raw``: raw ``.VBAS`` (informational);
      ``NaN`` if unreadable (companion bool:
      ``motor_velocity_base_was_unreadable``).  A finite ``0.0``
      here means the IOC posted 0 (no hardware base).
    * ``effective_v_max``: ceiling actually used to validate
      ``scan_velocity`` (= ``motor_velo``).
    * ``effective_v_min``: floor actually used (= ``max(.VBAS,
      velocity_minimum_or_0)``).
    * ``velocity_minimum_requested``: the ``velocity_minimum`` kwarg
      as the user supplied it; ``0.0`` if the user passed ``None``
      (companion bool: ``velocity_minimum_was_default``).
    * ``velocity_minimum_was_default``: ``True`` iff the user did
      not supply ``velocity_minimum`` (and the recorded
      ``velocity_minimum_requested = 0.0`` is the substituted
      default, not an explicit user choice).

    Other metadata:

    * ``hdf_t_phase_offset``: seconds to add to each
      ``hdf1.array_counter`` monitor-stream timestamp to obtain the
      corresponding frame's start-of-acquire moment.  Consumed by
      ``flyscan_3idc_analysis.pair_frames_to_positions`` for
      per-frame motor-position interpolation at the three
      meaningful per-period phases (start_acquire, end_acquire,
      end_period).  Defaults to ``-t_acquire`` in the calling
      ``flyscan(...)`` plan.  Pass a per-call override if your IOC's
      HDF plugin timestamps counter events at a different phase.
    """  # noqa E501
    # Substitute h5py-serialisable defaults for any None values, and
    # record companion booleans preserving the provenance.  See the
    # docstring for the full policy + rationale.
    motor_velocity_max_was_unreadable = v_vmax is None
    v_vmax_md = float("nan") if motor_velocity_max_was_unreadable else float(v_vmax)

    motor_velocity_base_was_unreadable = v_vbas is None
    v_vbas_md = float("nan") if motor_velocity_base_was_unreadable else float(v_vbas)

    velocity_minimum_was_default = velocity_minimum is None
    velocity_minimum_md = (
        0.0 if velocity_minimum_was_default else float(velocity_minimum)
    )

    return {
        # plan_name comes in as an explicit kwarg from flyscan() (default
        # "flyscan"; wrappers override).  We can't let bluesky auto-derive
        # it because @bluesky_plan wraps the generator in a Plan() class
        # that has no __name__ attribute (bluesky's
        # getattr(self._plan, "__name__", "") returns "").
        "plan_name": plan_name,
        "det_name": det_name,
        "flymotor_name": flymotor_name,
        # User-supplied scan parameters
        "p_start": p_start,
        "p_end": p_end,
        "exposures_per_egu": exposures_per_egu,
        "t_acquire": t_acquire,
        "t_period": t_period,
        "taxi_allowance": taxi_allowance,
        "compression": compression,
        # Derived geometry
        "p_initial": geometry.p_initial,
        "p_final": geometry.p_final,
        "num_frames": geometry.num_frames,
        "scan_active_duration": geometry.scan_duration,
        "scan_velocity": geometry.scan_velocity,
        "d_taxi": geometry.d_taxi,
        "motor_accl": geometry.motor_accl,
        "motor_accl_was_default": geometry.accl_was_default,
        "motor_egu": geometry.motor_egu,
        # Velocity bounds: raw IOC values (None -> NaN) + effective
        # bracket used + provenance booleans.  See docstring for the
        # full substitution policy.
        "motor_velo": v_velo,  # .VELO (always present)
        "motor_velocity_max_raw": v_vmax_md,  # .VMAX; NaN if unreadable
        "motor_velocity_max_was_unreadable": motor_velocity_max_was_unreadable,
        "motor_velocity_base_raw": v_vbas_md,  # .VBAS; NaN if unreadable
        "motor_velocity_base_was_unreadable": motor_velocity_base_was_unreadable,
        "effective_v_max": v_max,  # ceiling used (= .VELO)
        "effective_v_min": v_min,  # floor used
        "velocity_minimum_requested": velocity_minimum_md,  # 0.0 if user passed None
        "velocity_minimum_was_default": velocity_minimum_was_default,
        # Detector data-file destination and the IOC->workstation path
        # mapping (write = IOC side, read = workstation side), so a
        # reader can locate the files from the master alone.  Empty
        # string when a template is unavailable.
        "ad_file_name": ad_file_name,
        "ad_file_path": ad_file_path,
        "ad_read_path_template": ad_read_path_template or "",
        "ad_write_path_template": ad_write_path_template or "",
        "hdf_num_capture": hdf_num_capture,  # HDF plugin upper bound
        "hdf_flush_timeout_max": hdf_flush_timeout_max,  # worst-case (s)
        "consumer_tick": consumer_tick,  # monitor_loop wake-up tick (s)
        # Phase offset (seconds) from each hdf_t monitor timestamp to
        # the corresponding frame's start-of-acquire moment.  Used by
        # flyscan_3idc_analysis.pair_frames_to_positions to compute
        # per-frame start_acquire / end_acquire / end_period
        # timestamps.  Default -t_acquire; per-call overridable via
        # flyscan(..., hdf_t_phase_offset=...).
        "hdf_t_phase_offset": hdf_t_phase_offset,
        # Names of any extra readables passed to flyscan(detectors=).
        "detector_names": list(detector_names),
    }


def snapshot_stage_sigs(*devices):
    """Shallow-copy each device's ``stage_sigs`` for later restore.

    Returns a list of (device, dict-copy) pairs.  Use with
    ``restore_stage_sigs``.
    """
    return [(dev, dict(dev.stage_sigs)) for dev in devices]


def restore_stage_sigs(snapshot):
    """Restore device ``stage_sigs`` dicts from a ``snapshot_stage_sigs``.

    Clear-then-update is intentional: mutate the *same* dict object the
    device already holds (don't reassign the attribute).
    """
    for dev, saved in snapshot:
        dev.stage_sigs.clear()
        dev.stage_sigs.update(saved)


_AD_FILES_WARNED = set()
"""Once-per-(master_dir, target) dedup set for _check_ad_files_symlink."""


def _read_path_template(det):
    """Workstation mount path for the detector's image files, or None.

    This is the value the image-files symlink must point at.  Sourced
    from ``det.hdf1.read_path_template``.
    """
    return getattr(det.hdf1, "read_path_template", None) or getattr(
        det.hdf1, "_read_path_template", None
    )


def _write_path_template(det):
    """IOC-side write path prefix for the detector's image files, or None.

    Sourced from ``det.hdf1.write_path_template``.
    """
    return getattr(det.hdf1, "write_path_template", None) or getattr(
        det.hdf1, "_write_path_template", None
    )


def _check_ad_files_symlink(det, master_dir):
    """Warn if the image-files symlink is missing in ``master_dir``.

    Once per ``(master_dir, target)``.  Never creates the link.
    Returns ``True`` if present, ``False`` if a warning was emitted.
    """
    import os

    master_dir = str(master_dir)
    name = ad_files_dirname(det)
    if os.path.lexists(os.path.join(master_dir, name)):
        return True

    read_tmpl = _read_path_template(det)
    if read_tmpl:
        target = read_tmpl.rstrip("/") or "/"
    else:
        target = (
            "<the directory on this workstation"
            " where the area-detector files are mounted>"
        )

    key = (master_dir, target)
    if key in _AD_FILES_WARNED:
        return False
    _AD_FILES_WARNED.add(key)

    logger.warning(
        "\n"
        "The directory or symlink './%s' is missing in %s.\n"
        "\n"
        "The NeXus master files written by this plan use external HDF5\n"
        "links that point at the area-detector image files through a\n"
        "directory or symlink named '%s' adjacent to the master.\n"
        "Without it, tools that read the master cannot reach the image\n"
        "data.\n"
        "\n"
        "To fix, run this command from %s:\n"
        "\n"
        "    ln -s %s %s\n"
        "\n"
        "To verify:\n"
        "    ls -l %s       # should show '%s -> %s'\n"
        "    ls %s/ | head  # should list at least one entry",
        name,
        master_dir,
        name,
        master_dir,
        target,
        name,
        name,
        name,
        target,
        name,
    )
    return False


def _ensure_ad_files_symlink(det, master_dir):
    """Create the image-files symlink in ``master_dir`` if absent.

    The symlink (``{det.name}_files``) maps the relative external-link
    root in the master to the workstation mount where the IOC's image
    files are visible (``det.hdf1.read_path_template``).  Without it,
    tools that read the master cannot reach the image data.

    Creates the link when it is safe to do so.  Falls back to a
    descriptive WARNING (via ``_check_ad_files_symlink``) when it is
    not: the target mount is unknown or does not exist, or the link
    cannot be created.  Never raises.

    Returns ``True`` if the link is present (pre-existing or created),
    ``False`` otherwise.
    """
    import os

    master_dir = str(master_dir)
    name = ad_files_dirname(det)
    link_path = os.path.join(master_dir, name)

    if os.path.lexists(link_path):
        return True

    target = _read_path_template(det)
    if target:
        target = target.rstrip("/") or "/"
    if not target or not os.path.isdir(target):
        # Unknown or non-existent mount: do not guess.  Warn instead.
        return _check_ad_files_symlink(det, master_dir)

    try:
        os.symlink(target, link_path)
    except OSError as exc:
        logger.warning(
            "flyscan: could not create image-files symlink %r -> %r:"
            " %r.  Falling back to a manual-fix warning.",
            link_path,
            target,
            exc,
        )
        return _check_ad_files_symlink(det, master_dir)

    logger.info(
        "flyscan: created image-files symlink %r -> %r so the master"
        " file's external links resolve to the area-detector images.",
        link_path,
        target,
    )
    return True


def _external_link_target(det, ad_files_root=None):
    """Relative external-link target: ``{ad_files_root}<suffix>``.

    ``ad_files_root`` defaults to the per-detector ``./{det.name}_files/``.
    ``<suffix>`` is ``det.hdf1.full_file_name`` with
    ``hdf1.write_path_template`` stripped (the host-specific prefix).
    Falls back to the full absolute path with a WARNING if the
    template is missing or doesn't match.
    """
    if ad_files_root is None:
        ad_files_root = ad_files_root_for(det)
    ioc_file = det.hdf1.full_file_name.get(use_monitor=False)
    write_tmpl = _write_path_template(det)
    if write_tmpl:
        prefix = write_tmpl if write_tmpl.endswith("/") else write_tmpl + "/"
        if ioc_file.startswith(prefix):
            return f"{ad_files_root}{ioc_file[len(prefix) :]}"
        logger.warning(
            "_external_link_target: hdf1.write_path_template=%r does"
            " not prefix hdf1.full_file_name=%r; using legacy"
            " absolute-path-encapsulated target (NOT portable).",
            prefix,
            ioc_file,
        )
    else:
        logger.warning(
            "_external_link_target: hdf1.write_path_template not"
            " available on %r; using legacy absolute-path target"
            " (NOT portable).",
            type(det.hdf1).__name__,
        )
    return f"{ad_files_root}{ioc_file.lstrip('/')}"


def _wait_for_openable(path, mode="r", retries=5, timeout_s=10.0):
    """Try opening ``path`` with retries within ``timeout_s``.

    Returns ``True`` on success, ``False`` on timeout.  Never raises.
    """
    import time

    import h5py

    deadline = time.monotonic() + timeout_s
    delay = max(timeout_s / (max(retries, 1) + 1), 0.1)
    attempt = 0
    last_exc = None
    while attempt < retries and time.monotonic() < deadline:
        attempt += 1
        try:
            with h5py.File(path, mode):
                pass
            return True
        except (OSError, IOError) as exc:
            last_exc = exc
            time.sleep(delay)
    logger.debug(
        "_wait_for_openable: %r mode=%r failed after %d attempt(s) (last error: %r)",
        path,
        mode,
        attempt,
        last_exc,
    )
    return False


def _expected_frame_count(
    ad_file_path,
    run,
    *,
    unique_id_dset="/entry/instrument/NDAttributes/NDArrayUniqueId",
):
    """Best-effort total acquired-frame count, for provenance.

    Returns the authoritative count from the AD HDF1 file when
    ``ad_file_path`` is given and openable (one row per acquired
    frame).  Otherwise falls back to the scan-derived ``num_frames``
    from the run's start document.  Returns ``None`` if neither
    source is available.  Never raises.

    Note: this is the *total* acquired-frame expectation (including
    taxi-in / coast-out frames the AD IOC wrote).  The in-scan
    paired count can legitimately be smaller; a mismatch is a hint
    to inspect, not proof of error.  When the source is the lossy
    CA-monitor stream, however, a shortfall is the expected failure
    mode this provenance is meant to surface.
    """
    if ad_file_path is not None:
        try:
            import h5py

            with h5py.File(ad_file_path, "r") as f:
                if unique_id_dset in f:
                    return int(f[unique_id_dset].shape[0])
        except Exception as exc:  # never let provenance break the write
            logger.debug(
                "_expected_frame_count: could not read %r from AD file %r: %r",
                unique_id_dset,
                ad_file_path,
                exc,
            )

    try:
        md = getattr(run, "metadata", None)
        start = md["start"] if md is not None else None
        if isinstance(start, dict) and start.get("num_frames") is not None:
            return int(start["num_frames"])
    except Exception as exc:
        logger.debug(
            "_expected_frame_count: could not read num_frames from run metadata: %r",
            exc,
        )
    return None


_CAM_ERROR_STATES = {"Error", "Aborted", "Aborting", "Disconnected"}
"""Cam DetectorState_RBV values meaning the cam failed to arm / is unusable.

On the Eiger a failed arm transitions to 'Error' within ~2 ms.
"""


def _check_cam_armed(det, poll_s=0.05, max_wait_s=0.5):
    """Plan stub: verify the cam armed after ``cam.acquire = 1``.

    Polls ``cam.detector_state``; on transition to an error state,
    raises ``RuntimeError`` carrying ``cam.status_message`` so the
    operator sees the IOC's real failure reason instead of the
    downstream "Path '/' does not exist on IOC" message from
    apstools.
    """
    cam = det.cam
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_wait_s:
        state = ""
        try:
            state = cam.detector_state.get(use_monitor=False, as_string=True)
        except Exception:
            pass
        if state in _CAM_ERROR_STATES:
            msg = ""
            try:
                msg = cam.status_message.get(use_monitor=False, as_string=True)
            except Exception:
                pass
            raise RuntimeError(
                f"{det.name} failed to arm: detector_state={state!r}"
                f" status_message={msg!r}"
            )
        if state == "Acquire":
            return
        yield from bps.sleep(poll_s)
    # Fall through: didn't see Error, didn't see Acquire.  Don't
    # raise -- the first-frame timeout below will catch genuinely
    # stuck cams.
    return


def snapshot_kinds(*signals):
    """Capture each signal's ``.kind`` for later restore.

    Returns a list of ``(signal, original_kind)`` pairs.  Use with
    ``restore_kinds``.  Intended for plan-local mutations of ``kind``
    (e.g. to make ``cam.array_counter`` ``Kind.hinted`` for the
    duration of a fly scan so it appears in primary-stream events,
    without imposing that choice on every other plan that touches the
    device).
    """
    return [(sig, sig.kind) for sig in signals]


def restore_kinds(snapshot):
    """Restore each signal's ``.kind`` from a ``snapshot_kinds`` result."""
    for sig, original_kind in snapshot:
        sig.kind = original_kind


def motor_is_moving(motor):
    """True iff the motor is currently moving, checked via DMOV (no cache).

    Reads ``motor_done_move`` (the motor record's ``.DMOV`` field) with
    ``use_monitor=False`` to bypass the pyepics monitor cache, matching
    this module's "bypass cache for timing-critical reads" discipline.

    Prefer this function over ``motor.moving`` (the EpicsMotor property),
    which depending on ophyd version may read ``MOVN`` rather than
    ``DMOV``.  ``MOVN`` transitions briefly to "not moving" between the
    primary move and the backlash-correction move when BDST != 0;
    ``DMOV`` only goes to 1 when both phases are complete.

    Used by ``_cleanup`` to decide whether to issue ``bps.stop(flymotor)``
    (skipped if the motor is already idle).
    """
    return motor.motor_done_move.get(use_monitor=False, as_string=False) != 1


def _wait_for(signal, predicate, timeout, *, settle_time=0.0):
    """Wait for ``signal`` to satisfy ``predicate``, using CA monitors.

    Two-step pattern:

    1. **Precheck** the signal's current value via
       ``signal.get(use_monitor=False)``.  If ``predicate(value)`` is
       already true, return immediately without subscribing.
    2. Otherwise create an
       ``ophyd.status.SubscriptionStatus(..., run=True)`` whose
       callback fires whenever a CA monitor update arrives, and
       ``.wait(timeout=timeout)``.

    The precheck defends against a known IOC failure mode: CA monitors
    are posted **on change**.  If the PV's current value already
    satisfies the predicate, an IOC may have no reason to post the
    "next" monitor update the status is waiting for, and the wait will
    hang to timeout (surfacing as ``ophyd.utils.errors.WaitTimeoutError``,
    or ``FailedStatus`` if the status was used inside a bluesky plan).
    The precheck short-circuits this case.

    ``SubscriptionStatus(run=True)`` (the ophyd default) also evaluates
    the predicate once at subscribe time against the value most recently
    seen by the monitor stream, which is a *second* layer of defense in
    case a monitor update arrived between the precheck and the
    subscription.

    Parameters
    ----------
    signal : ophyd.Signal
        The signal to watch.  Must support both
        ``.get(use_monitor=False)`` (used by the precheck) and
        subscription via ``SubscriptionStatus`` (i.e. a CA-backed
        signal).
    predicate : callable
        ``predicate(value) -> bool``.  Returns true when the wait
        should end.  Called with positional ``value`` only; the
        ``SubscriptionStatus`` callback wraps it to accept the
        ``value=..., **kwargs`` shape ophyd uses.
    timeout : float
        Seconds to wait for the predicate to become true.  Raises
        ``ophyd.utils.errors.WaitTimeoutError`` on timeout.
    settle_time : float, optional
        Forwarded to ``SubscriptionStatus``.  If non-zero, ophyd
        requires the predicate to remain true for this many seconds
        before completing the status.  Default 0.

    Returns
    -------
    The current value of ``signal`` (read via ``use_monitor=False``)
    once the predicate is satisfied.

    Raises
    ------
    ophyd.utils.errors.WaitTimeoutError
        If the predicate does not become true within ``timeout``.

    Notes
    -----
    This is a synchronous helper for use **outside** bluesky plans
    (diagnostic functions, preflight checks).  Inside a plan, prefer
    ``signal.set(value).wait(...)`` where applicable, or build a
    plan-stub wrapper that yields control while waiting.
    """
    current = signal.get(use_monitor=False)
    if predicate(current):
        logger.debug(
            "_wait_for(%s): precheck satisfied (value=%r); no subscription",
            signal.name,
            current,
        )
        return current
    logger.debug(
        "_wait_for(%s): precheck not satisfied (value=%r); subscribing",
        signal.name,
        current,
    )
    status = SubscriptionStatus(
        signal,
        lambda *, value, **_: predicate(value),
        run=True,
        settle_time=settle_time,
    )
    status.wait(timeout=timeout)
    return signal.get(use_monitor=False)


def _safe_get(device, name, **get_kwargs):
    """Best-effort ``device.<name>.get(**get_kwargs)`` for diagnostics.

    Returns ``None`` if the component does not exist on the class or if
    the read raises any exception.  Used to harvest extra context for
    error messages (e.g. ``write_status`` / ``write_message`` from a
    sick HDF plugin) without letting the diagnostic itself raise and
    mask the real error.
    """
    if not _has_component(device, name):
        return None
    try:
        sig = getattr(device, name)
        return sig.get(**get_kwargs)
    except Exception as exc:
        logger.debug("_safe_get(%s.%s) failed: %r", device.name, name, exc)
        return None


def _has_component(device, name):
    """True if ``name`` is declared as an ophyd component on ``device``'s class.

    Uses class-level introspection only — does **not** trigger lazy
    instantiation or any CA traffic.  Safe to call against detectors
    whose IOC is unresponsive (e.g. inside cleanup paths after a
    failure).

    Compare with ``hasattr(device, name)``, which calls ``getattr``
    and therefore *will* instantiate a not-yet-touched ophyd component,
    blocking for ``wait_for_connection`` (default 5s) when the IOC is
    down.
    """
    if device is None:
        return False
    return name in type(device).component_names


def _hdf_flush_timeout(
    det,
    n_captured,
    floor=10.0,
    headroom=3.0,
    assumed_rate_mb_s=50.0,
    assumed_bytes_per_pixel=8,
):
    """Estimate a generous timeout for HDF5 ``write_file`` to complete.

    Computes the expected file size from frame dimensions and pixel
    depth, divides by a conservative write rate, multiplies by
    ``headroom``, and floors at ``floor`` seconds.

    Parameters
    ----------
    det : ophyd Device
        The area detector; ``det.hdf1.width`` and ``det.hdf1.height``
        are read to estimate frame size.
    n_captured : int
        Number of frames the IOC will be writing.
    floor : float
        Minimum timeout, regardless of computed value.  Protects very
        small captures from a near-zero timeout.
    headroom : float
        Multiplier applied to the estimated wall time.  3.0 gives a
        comfortable margin without being silly.
    assumed_rate_mb_s : float
        Conservative write rate (MB/s).  The IOC typically achieves
        much more; 50 leaves a 4x margin on our measured ~200 MB/s.
    assumed_bytes_per_pixel : int
        Worst case for the cam's data type (Float64 = 8).  Smaller
        cam types (Int8, UInt16) will write faster but the headroom
        absorbs the over-estimate.

    Returns
    -------
    float
        Timeout in seconds.  Always >= ``floor``.
    """
    try:
        width = int(det.hdf1.width.get(use_monitor=False))
        height = int(det.hdf1.height.get(use_monitor=False))
    except Exception:
        return max(floor, floor * headroom)
    mb = width * height * assumed_bytes_per_pixel * int(n_captured) / (1024 * 1024)
    estimated = mb / assumed_rate_mb_s
    return max(floor, estimated * headroom)


def wait_for_acquire_drained(det, poll=0.001, timeout=10.0):
    """Plan stub: wait for cam to report idle *and* HDF queue to drain.

    With ``cam.wait_for_plugins='Yes'``, ``cam.acquire_busy`` goes 0
    only after every enabled plugin has finished processing the last
    frame.  Add the HDF-queue check as belt-and-suspenders for cases
    where the cam class does not expose ``acquire_busy``.

    Implementation: builds an ``AndStatus`` of one ``SubscriptionStatus``
    per available signal (cam ``acquire_busy``, HDF ``num_queued_arrays``
    with race-window-safe corroboration on ``queue_free``/
    ``queue_size``) and yields ``bps.sleep`` ticks until ``status.done``
    is true or ``timeout`` elapses.  Each sub-status is driven by its
    own CA monitor stream (one subscription per signal), so neither
    signal can mask the other through a missed monitor edge.

    Returns after ``timeout`` seconds even if drained signals do not
    settle, so this is safe to call from cleanup paths.

    Short-circuit: if the cam is not acquiring *and* the HDF plugin
    has not captured any frames, there is nothing to drain.  This
    spares cleanup paths a needless ``timeout``-second wait when the
    plan failed before acquisition started (e.g. an ``AttributeError``
    in the preparation phase).  This precheck uses ``use_monitor=False``
    to avoid stale-cache reads (notably ``num_captured`` is cumulative
    across runs unless explicitly reset).

    Capability discovery uses class-level ``component_names``
    introspection rather than ``hasattr``, so we don't pay a 5-second
    CA timeout to answer "does this device have ``queue_free``?" when
    the IOC is dead during a crash-cleanup.

    The ``poll`` parameter is the *plan wake-up tick* (how often the
    plan checks ``status.done``), not a PV-polling interval.  The
    underlying status updates happen on the CA monitor thread; ``poll``
    only controls how soon the plan notices.  A future-proofing
    alternative would be ``bps.wait_for([asyncio_future])`` (would tie
    the plan to the RunEngine's asyncio loop).
    """
    has_busy = _has_component(det.cam, "acquire_busy")
    has_hdf_queue = _has_component(det, "hdf1") and _has_component(
        det.hdf1, "queue_free"
    )

    acquire_off = det.cam.acquire.get(use_monitor=False) == 0
    nothing_captured = (
        not _has_component(det, "hdf1")
        or not _has_component(det.hdf1, "num_captured")
        or det.hdf1.num_captured.get(use_monitor=False) == 0
    )
    if acquire_off and nothing_captured:
        logger.info(
            "wait_for_acquire_drained(%s): nothing to drain"
            " (acquire=0, num_captured=0); short-circuiting",
            det.name,
        )
        # finalize_wrapper consumes this as a generator, so we must
        # yield at least one message before returning.
        yield from bps.null()
        return

    # Build sub-statuses for whichever signals the device exposes.
    sub_statuses = []
    if has_busy:
        sub_statuses.append(
            SubscriptionStatus(
                det.cam.acquire_busy,
                lambda *, value, **_: int(value) == 0,
                run=True,
            )
        )
    if has_hdf_queue:
        # HDF-drain predicate: queue empty AND all slots free (i.e. no
        # frame is currently being written).  The two corroboration
        # reads use use_monitor=False so the predicate sees consistent
        # post-update values rather than a stale cache.
        hdf = det.hdf1
        sub_statuses.append(
            SubscriptionStatus(
                hdf.num_queued_arrays,
                lambda *, value, **_: (
                    int(value) == 0
                    and int(hdf.queue_free.get(use_monitor=False))
                    == int(hdf.queue_size.get(use_monitor=False))
                ),
                run=True,
            )
        )

    t0 = time.time()
    logger.info(
        "wait_for_acquire_drained(%s): has_busy=%s has_hdf_queue=%s timeout=%gs",
        det.name,
        has_busy,
        has_hdf_queue,
        timeout,
    )

    if not sub_statuses:
        # No drained signals to wait on — nothing more to do.
        logger.info(
            "wait_for_acquire_drained(%s): no drainable signals on"
            " this device; returning immediately",
            det.name,
        )
        yield from bps.null()
        return

    # Combine via AndStatus (no-op if there's only one sub-status).
    status = sub_statuses[0]
    for s in sub_statuses[1:]:
        status = AndStatus(status, s)

    deadline = t0 + timeout
    while time.time() < deadline:
        if status.done:
            logger.info(
                "wait_for_acquire_drained(%s): drained after %.3fs",
                det.name,
                time.time() - t0,
            )
            return
        yield from bps.sleep(poll)
    logger.warning(
        "wait_for_acquire_drained(%s): TIMEOUT after %gs (status.done=%s)",
        det.name,
        timeout,
        status.done,
    )


def monitor_loop(
    flymotor,
    det,
    p_end,
    *,
    exit_when,
    watchdog=None,
    tick=_CONSUMER_TICK_DEFAULT,
    motor_stopped_flag=None,
    extra_readables=(),
):
    """Plan stub: emit one primary-stream event per HDF frame written.

    Producer/consumer design:

    * **Producer:** a CA monitor callback on ``det.hdf1.num_captured``
      pushes ``(timestamp, new_value)`` onto a small bounded
      ``queue.Queue`` whenever the IOC publishes a monitor update.
      The producer runs on the pyepics dispatch thread.
    * **Consumer:** this plan-stub loop wakes up every ``tick`` seconds,
      drains the queue, and emits one ``primary``-stream event per
      newly-captured frame.  The consumer runs on the plan/RunEngine
      thread; ``yield from bps.sleep(tick)`` keeps the RunEngine in
      control of pause/abort.

    The primary stream is a progress indicator.  Pairing of detector
    frames to flymotor positions happens downstream via the
    IOC-timestamped monitor streams set up by
    ``@bpp.monitor_during_decorator``; the per-frame
    ``bps.read(det)`` + ``bps.read(flymotor)`` here is a snapshot,
    not the system of record, and uses the cached monitor values
    (no extra CA traffic).

    When the motor's readback crosses ``p_end``, two stop actions
    fire in sequence on the same tick:

    1. ``bps.mv(det.cam.acquire, 0)`` tells the cam to stop.
    2. ``bps.stop(flymotor)`` issues a controlled stop on the
       motor.  For an ``EpicsMotor`` this writes the motor record's
       ``.STOP`` field, which decelerates the motor at ``.ACCL``
       (a normal controlled ramp-down, not an emergency stop) so
       it comes to rest somewhere between ``p_end`` and
       ``p_final``.  This avoids wasting motion (and the
       associated coast time) traversing all the way to the
       conservatively-chosen ``p_final``.

    The motor stop has a side effect: the ``MoveStatus`` previously
    registered with the RunEngine by
    ``bps.abs_set(flymotor, p_final, group="scan")`` completes with
    ``success=False`` once the motor's stop callback fires.  Callers
    that ``bps.wait(group="scan")`` after ``monitor_loop`` must be
    prepared for that wait to raise ``bluesky.utils.FailedStatus``;
    pass a ``motor_stopped_flag`` (see below) to discriminate
    "stopped on purpose" from "stopped because something else broke."

    This check happens on each consumer tick; overshoot is dominated
    by the motor record's ~10 Hz update rate, not by the tick.

    Exit: ``exit_when.done``.  The caller constructs ``exit_when`` as
    ``AndStatus(cam_stopped_status, drain_status)`` so the loop
    exits only when the cam has been stopped AND every in-flight
    frame has been flushed by the HDF plugin.

    Watchdog: ``watchdog`` is an ``ophyd.status.SubscriptionStatus``
    constructed with ``timeout=no_frames_timeout`` watching
    ``num_captured > 0``.  ophyd's StatusBase timeout machinery
    completes the status as failed if no frame arrives in time; the
    consumer checks ``watchdog.done and not watchdog.success`` per
    tick and raises ``RuntimeError`` annotated with the HDF
    plugin's ``WriteStatus`` and ``WriteMessage``.  The raise lets
    the RunEngine send STOP to all in-motion movables (including
    ``flymotor``), which is exactly
    what we want when something is wrong with the IOC/cam/HDF
    chain.

    Parameters
    ----------
    flymotor : EpicsMotor
        The fly-scan motor.  Position read with ``use_monitor=False``
        for the ``p_end`` crossing check, to bypass cached value.
    det : ADBase
        The area detector (with ``cam`` and ``hdf1`` plugin
        sub-devices).
    p_end : float
        Stop the cam acquisition when ``flymotor.user_readback``
        meets or exceeds this value.
    exit_when : ophyd.status.StatusBase
        Loop exits when this status's ``.done`` is True.  See
        "Exit" above.
    watchdog : ophyd.status.StatusBase, optional
        If supplied, the loop checks ``.done and not .success``
        per tick and raises ``RuntimeError`` if the watchdog
        timed out.  None disables the watchdog.
    tick : float
        Consumer wake-up tick in seconds.  See module-level
        ``_CONSUMER_TICK_DEFAULT``.
    motor_stopped_flag : list of one bool, optional
        If supplied, set ``motor_stopped_flag[0] = True`` after the
        ``bps.stop(flymotor)`` call at the ``p_end`` crossing.  The
        caller uses this to discriminate the expected ``FailedStatus``
        from the now-failed scan-group ``MoveStatus`` (which the
        caller can swallow) from any other ``FailedStatus`` (which
        should propagate).  None disables this signal (and the
        caller will have no way to tell the two cases apart — only
        valid if the caller doesn't ``bps.wait`` on a motor-related
        group after the loop).
    """
    # --- Producer setup: CA monitor callback into a bounded queue ---
    frame_queue = queue.Queue(maxsize=_FRAME_QUEUE_SIZE)
    overflow_warned = [False]  # list-of-one so callback can mutate

    def _on_num_captured(*, value, timestamp, **kwargs):
        """CA monitor callback (runs on pyepics dispatch thread).

        Pushes ``(timestamp, value)`` onto ``frame_queue``.  Drops
        the entry and logs a single WARNING if the queue is full
        (never block the CA thread).
        """
        try:
            frame_queue.put_nowait((timestamp, int(value)))
        except queue.Full:
            if not overflow_warned[0]:
                overflow_warned[0] = True
                logger.warning(
                    "monitor_loop: frame queue overflow (size=%d);"
                    " consumer falling behind producer."
                    " New events will be dropped silently until the"
                    " queue drains.  Adjust _FRAME_QUEUE_SIZE in"
                    " flyscan_3idc.py if this recurs.",
                    _FRAME_QUEUE_SIZE,
                )

    # --- Inner helpers (named for clarity)

    def _emit_pending_frames(last_captured, *, acquire_stopped):
        """Drain the queue and emit one primary event per new frame.

        Returns the updated ``last_captured`` count.

        After the cam has been stopped at the p_end crossing and the
        motor reports it is no longer moving, suppress further event
        emission: any frames the IOC reports past that point are
        post-scan tail (the cam finishing its in-flight burst) and
        are not associated with new in-scan motor positions.  The
        bookkeeping return value (``highest``) is still advanced so
        the loop's exit condition (driven by ``cam_stopped_status``
        / ``hdf_drain_status``) is unaffected.
        """
        # Snapshot the highest value seen in the queue this tick.
        # The IOC publishes monotonically-increasing values; we
        # only care about the delta count.
        highest = last_captured
        n_drained = 0
        while True:
            try:
                _ts, value = frame_queue.get_nowait()
            except queue.Empty:
                break
            n_drained += 1
            if value > highest:
                highest = value
        if highest > last_captured:
            n_new = highest - last_captured
            motor_done = True
            if hasattr(flymotor, "motor_done_move"):
                try:
                    motor_done = bool(
                        int(flymotor.motor_done_move.get(use_monitor=False))
                    )
                except Exception:
                    motor_done = True
            suppress = acquire_stopped and motor_done
            logger.debug(
                "monitor_loop: %d new frame(s) (highest=%d,"
                " drained %d queue entries) motor=%g"
                " acquire_stopped=%s motor_done=%s suppress=%s",
                n_new,
                highest,
                n_drained,
                flymotor.user_readback.get(use_monitor=False),
                acquire_stopped,
                motor_done,
                suppress,
            )
            if not suppress:
                # det + flymotor + any user-supplied extra readables.
                readables = [det, flymotor, *extra_readables]
                for _ in range(n_new):
                    yield from bps.create(name="primary")
                    for obj in readables:
                        yield from bps.read(obj)
                    yield from bps.save()
        return highest

    def _check_p_end_crossing(acquire_stopped, last_captured):
        """Stop the cam *and* the motor if the motor has crossed p_end.

        Ordering: cam first, motor second.  Stopping the cam is the
        immediate priority for ending the acquisition window cleanly;
        the motor's deceleration ramp can run in parallel with the
        HDF drain that follows.

        Motor stop is a normal controlled stop (``EpicsMotor.stop()``
        writes the motor record's ``.STOP`` field, which decelerates
        at ``.ACCL`` — not an emergency stop).  The motor will come
        to rest somewhere past ``p_end`` (within one deceleration
        distance, ~``0.5 * scan_velocity * .ACCL``) rather than
        coasting all the way to ``p_final``.  This is what the
        caller wanted; ``p_final`` was always a conservative upper
        bound, not a target.
        """
        if acquire_stopped:
            return acquire_stopped
        # Bypass cache for the position read — pairing timing matters.
        if flymotor.user_readback.get(use_monitor=False) >= p_end:
            logger.info(
                "monitor_loop: motor crossed p_end (%g); stopping acquire"
                " and motor at num_captured=%d",
                p_end,
                last_captured,
            )
            yield from bps.mv(det.cam.acquire, 0)
            # Controlled stop on the motor: decelerates at .ACCL, then
            # the registered scan-group MoveStatus will fire with
            # success=False.  See monitor_loop docstring + the
            # try/except wrapping bps.wait(group="scan") in the caller.
            yield from bps.stop(flymotor)
            if motor_stopped_flag is not None:
                motor_stopped_flag[0] = True
            return True
        return acquire_stopped

    def _check_watchdog():
        """If the watchdog timed out, harvest context and raise.

        Returns nothing; raises RuntimeError on watchdog trip.
        Watchdog timeout is signalled by ophyd's StatusBase as
        ``done=True, success=False`` after the timeout elapses
        without the predicate becoming true.
        """
        if watchdog is None:
            return
        if not (watchdog.done and not watchdog.success):
            return
        # Watchdog has tripped.  Harvest diagnostics from the HDF
        # plugin to annotate the exception, then raise so the
        # RunEngine STOPs movables.
        write_status = _safe_get(det.hdf1, "write_status", as_string=True)
        write_message = _safe_get(det.hdf1, "write_message", as_string=True)
        full_file_name = _safe_get(det.hdf1, "full_file_name", as_string=True)
        msg = (
            f"monitor_loop: HDF watchdog tripped — no frames captured"
            f" within the timeout on {det.name}."
            f" hdf1.write_status={write_status!r}"
            f" hdf1.write_message={write_message!r}"
            f" hdf1.full_file_name={full_file_name!r}."
            f" Stop acquire and abort."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    # --- Main loop ---
    t0 = time.time()
    last_captured = int(det.hdf1.num_captured.get(use_monitor=False))
    logger.info(
        "monitor_loop: starting at num_captured=%d, motor=%g, p_end=%g"
        " (tick=%gs, watchdog=%s)",
        last_captured,
        flymotor.user_readback.get(use_monitor=False),
        p_end,
        tick,
        "enabled" if watchdog is not None else "disabled",
    )

    acquire_stopped = False
    cid = det.hdf1.num_captured.subscribe(_on_num_captured)
    try:
        while True:
            # Check watchdog first — if it tripped, no point in
            # emitting events or checking other conditions.
            _check_watchdog()

            # Drain producer queue, emit primary events.
            # Pass acquire_stopped so emission is suppressed once the
            # cam has been stopped and the motor has come to rest.
            last_captured = yield from _emit_pending_frames(
                last_captured, acquire_stopped=acquire_stopped
            )

            # Stop cam when motor crosses p_end.
            acquire_stopped = yield from _check_p_end_crossing(
                acquire_stopped, last_captured
            )

            # Exit when the caller's status fires.
            if exit_when.done:
                logger.info(
                    "monitor_loop: exit after %.3fs"
                    " (final num_captured=%d, motor=%g,"
                    " acquire_stopped=%s)",
                    time.time() - t0,
                    last_captured,
                    flymotor.user_readback.get(use_monitor=False),
                    acquire_stopped,
                )
                break

            yield from bps.sleep(tick)
    finally:
        # Always unsubscribe — even on RuntimeError from the
        # watchdog, on plan abort, or on any other exception.
        try:
            det.hdf1.num_captured.unsubscribe(cid)
        except Exception as exc:
            logger.warning("monitor_loop: unsubscribe failed: %r", exc)


@bluesky_plan
def flyscan(
    detectors: list = None,
    det_name: str = "adsimdet",
    flymotor_name: str = "m1",
    p_start: float = 0,
    p_end: float = 5,
    exposures_per_egu: float = 2.0,
    t_period: float = 0.1,
    t_acquire: float = None,
    taxi_allowance: float = 0.5,
    compression: str = "zlib",
    ad_file_name: str = "flyscan",
    ad_file_path: str = "/tmp/flyscan",
    velocity_minimum: float = None,
    plan_name: str = "flyscan",
    hdf_t_phase_offset: float = None,
    # Internal parameters (underscore-prefixed); see Parameters.
    _consumer_tick: float = _CONSUMER_TICK_DEFAULT,
    _force_hdf_nonblocking: bool = False,
    # User-supplied metadata: always last.
    md: dict = None,
):
    """Fly scan: move motor through range while acquiring detector frames.

    The motor traverses ``p_initial → ≤ p_final``, maintaining constant
    velocity between ``p_start → p_end`` to deliver ``num_frames`` frames
    within ``[p_start, p_end]``.  ``p_initial`` and ``p_final`` are
    computed from ``p_start``, ``p_end``, the motor's ``.ACCL``, and
    ``taxi_allowance``; ``num_frames`` is computed from
    ``(p_end - p_start) * exposures_per_egu``.

    Detector frames are acquired continuously during the traverse;
    downstream processing trims the data to ``[p_start, p_end]`` by
    motor position.

    An HDF5 file containing every captured frame is written next to the
    run (the path is in the run metadata under ``ad_file_path`` /
    ``ad_file_name``).

    Position geometry
    -----------------

    User-supplied: ``p_start`` and ``p_end`` (in-scan range).  Derived:
    ``p_initial`` (parked, pre-scan) and ``p_final`` (a conservative
    upper bound the motor is almost never actually allowed to reach —
    see below)::

        p_initial     <  p_start <  p_end   <  p_final
        |             |          |          |
        |             |--scan----|          |
        |--takeoff----|          |--stop----|

    * ``p_start``: the position at which the first useful frame should
      be captured.  Downstream processing trims frames captured before
      this point.
    * ``p_end``: the position at which the last useful frame should be
      captured.  When the motor crosses this point, the plan stops the
      cam (no more frames) *and* issues a controlled stop on the motor
      (decelerates at ``.ACCL``).  The motor comes to rest somewhere
      between ``p_end`` and ``p_final``, within roughly one deceleration
      distance (``≈ 0.5 * scan_velocity * .ACCL``) past ``p_end``.
    * ``p_initial`` (derived): where the motor is parked before the
      scan, far enough below ``p_start`` that the motor reaches its
      scan velocity *before* it enters the acquisition region.
      Computed as ``p_start - d_taxi - taxi_allowance`` where
      ``d_taxi = 0.5 * scan_velocity * motor.ACCL``.
    * ``p_final`` (derived): the conservative upper bound used as the
      *target* of the scan move (``bps.abs_set(flymotor, p_final,
      group="scan")``).  The plan stops the motor before it reaches
      ``p_final`` — this target only matters as a "should never be
      exceeded" sentinel and as a fallback stopping point if something
      prevents the planned controlled stop.  Computed symmetric to
      ``p_initial``.

    ``taxi_allowance`` (default ``0.5``, in motor EGU) is added to both
    ends as a slack margin on top of the acceleration-based distance.
    Increase it if the cam's first/last frame is observed to fall
    outside ``[p_start, p_end]``; decrease it if the scan takes too
    long to taxi.

    Position units are whatever the motor reports (``user_readback``);
    typically engineering units (mm, degrees, etc.) — the motor's
    ``.EGU`` field is recorded in run metadata.

    Frame timing
    ------------

    * ``exposures_per_egu``: target frame density.  Combined with the
      scan range, gives ``num_frames = round(1 + (p_end - p_start)
      * exposures_per_egu)`` (fence-post counting: one frame at each
      endpoint plus ``exposures_per_egu`` frames per unit between).
    * ``t_period``: seconds between successive frame exposures.
    * ``t_acquire``: per-frame exposure time, in seconds.  Defaults to
      ``t_period`` (continuous exposure).  Must satisfy
      ``0 < t_acquire <= t_period``.

    The scan velocity is computed as ``(p_end - p_start) / (num_frames
    * t_period)``.  Pre-scan validation requires it to fall in the
    bracket ``v_min <= scan_velocity <= v_max`` where:

    * ``v_max`` is the motor's currently-configured ``.VELO`` (the
      operator's chosen target velocity governs the ceiling — ``.VMAX``
      is recorded as metadata but not used as the cap).  ``.VELO`` must
      be readable; if it isn't, the plan refuses to run.
    * ``v_min`` is ``max(.VBAS, velocity_minimum)``, where
      ``velocity_minimum`` is the kwarg below (``None`` ⇒ floor is
      ``.VBAS`` alone).

    The motor's pre-run ``.VELO`` is automatically restored at scan end.

    Detector & file
    ---------------

    * ``det_name``: ophyd device registry key for the area detector
      (default ``"adsimdet"``).  Must be an AreaDetector with an HDF5
      plugin attached.
    * ``flymotor_name``: ophyd device registry key for the motor
      (default ``"m1"``).
    * ``compression``: HDF5 chunk compression name (default ``"zlib"``).
      Validated against the HDF plugin's ``compression.enum_strs`` at
      scan start; raises ``ValueError`` with the allowed list if the
      value isn't supported by the IOC's HDF plugin build.
    * ``ad_file_name``: stem for the saved HDF5 file (default
      ``"flyscan"``); the IOC appends an auto-incrementing number and
      the ``.h5`` extension.
    * ``ad_file_path``: directory on the IOC's filesystem where the
      HDF5 file is written (default ``"/tmp/flyscan"``).  **Must exist on
      the IOC's filesystem.**  If the IOC runs in a container, this
      is the container's view of the path, not the host's.  The plan
      checks this before staging and raises ``RuntimeError`` with a
      clear message if the path doesn't exist.

    What gets recorded
    ------------------

    Each call to ``RE(flyscan(...))`` produces one bluesky run
    containing:

    * A ``primary`` event stream with one event per HDF frame
      accepted by the writer.  Each event records the cam and HDF
      array counters and the motor's reported position at the moment
      the consumer drained that frame from its queue.  Treat this as
      a progress indicator and at-the-bench snapshot; use the monitor
      streams below for high-precision pairing.
    * Three monitor streams (``adsimdet_cam_array_counter_monitor``,
      ``adsimdet_hdf1_array_counter_monitor``, ``m1_monitor``)
      carrying IOC-timestamped values for downstream synchronization
      of frame counters with motor position.
    * A ``baseline`` stream (whatever ``apsbits`` configures).
    * Metadata under ``start``: user-supplied scan parameters
      (``p_start``, ``p_end``, ``exposures_per_egu``, ``t_period``,
      ``t_acquire``, ``taxi_allowance``, ``compression``,
      ``velocity_minimum_requested``), derived geometry (``p_initial``,
      ``p_final``, ``num_frames``, ``scan_velocity``, ``d_taxi``,
      ``motor_accl``, ``motor_egu``), raw motor velocity values
      (``motor_velo``, ``motor_velocity_max_raw``,
      ``motor_velocity_base_raw``) and the effective bracket
      (``effective_v_max``, ``effective_v_min``) the plan used, file
      destination, watchdog timeout, ``consumer_tick``, plus anything
      you pass in ``md``.
    * An HDF5 file with the actual image data at
      ``ad_file_path/ad_file_name_NNNNNN.h5``.

    Common usage
    ------------

    From a 3-ID-C IPython session::

        from id3c.startup import *           # provides RE, oregistry
        from flyscan_3idc import flyscan

        # 50 frames over a 5-EGU range at 20 Hz:
        uid, = RE(flyscan(p_start=0, p_end=5, exposures_per_egu=10,
                          t_period=0.05))

    Override more defaults for a specific run::

        uid, = RE(flyscan(
            flymotor_name="m1",
            p_start=0, p_end=10,
            exposures_per_egu=10, t_period=0.05, t_acquire=0.01,
            taxi_allowance=1.0,
            compression="lz4",
            ad_file_path="/tmp/myexperiment/",
            ad_file_name="sample42",
            md={"sample": "Ag behenate", "operator": "your-name"},
        ))

    Common pitfalls
    ---------------

    * **"file_path does not exist" RuntimeError at scan start.**  The
      directory in ``ad_file_path`` doesn't exist on the IOC's
      filesystem.  If the IOC is containerized, create the directory
      inside the container or use a path that's visible there.
    * **"scan_velocity exceeds motor .VELO" ValueError.** The
      requested combination of position range and frame rate would
      require the motor to move faster than its currently configured
      ``.VELO``.  Either reduce ``exposures_per_egu``, increase
      ``t_period``, shorten ``p_end - p_start``, or raise the motor's
      ``.VELO`` (caveat: ``.VELO`` is restored to its pre-run value
      after the scan; you must change it *before* invoking the plan).
    * **"scan_velocity is below effective v_min" ValueError.** The
      computed velocity is below ``max(.VBAS, velocity_minimum)``.
      Either increase ``exposures_per_egu``, decrease ``t_period``,
      lengthen ``p_end - p_start``, or lower ``velocity_minimum`` /
      the motor's ``.VBAS``.
    * **"Cannot determine velocity ceiling ... .VELO unreadable"
      ValueError.** The motor's ``.VELO`` field could not be read
      (IOC down, PV typo, network drop).  Fix the IOC connection
      before retrying.
    * **"compression=... not in HDF plugin's allowed set" ValueError.**
      The IOC's HDF plugin doesn't support the requested compression
      algorithm.  Inspect ``det.hdf1.compression.enum_strs`` to see
      what *is* supported by this IOC build.
    * **An extra detector's per-frame timestamp never changes
      during the scan.**  The device isn't self-updating; the
      flyscan plan does not trigger ancillary devices.  Check the
      ``timestamp`` field of the reading, not the value (a
      genuinely steady-state value with a moving timestamp is
      fine).  Use a CA-monitor-driven signal, or put a scaler in
      continuous mode, before adding it to ``detectors``.
    * **Watchdog: "no frames captured" RuntimeError mid-scan.**  The
      cam isn't delivering frames to the HDF plugin.  Likely the HDF
      plugin's ``EnableCallbacks`` is ``Disable``, the cam's
      ``ArrayCallbacks`` is ``Disable``, or the HDF plugin's
      ``NDArrayPort`` doesn't point at the cam.  The RunEngine will
      have stopped the motor; investigate the IOC and try again.
    * **The scan completes but the data dictionary's ``num_captured``
      is 0.**  The IOC resets ``NumCaptured_RBV`` to 0 after the
      HDF5 file is closed.  Look at ``full_file_name`` (in
      ``_cleanup``'s log line) and the actual file on disk to confirm
      what was saved.
    * **"HDF plugin dropped N frame(s) during this run"
      FlyscanDataLossWarning at scan end.**  The HDF plugin couldn't
      keep up with the cam at the requested rate, and ``N`` frames
      the cam produced are missing from the on-disk HDF5 file.  The
      warning is emitted both to the log (WARNING level) and via
      Python's ``warnings`` machinery (subclass of ``UserWarning``).
      The plan uses ``blocking_callbacks="Yes"`` on the HDF plugin
      to throttle the cam to HDF's write rate, so this should be
      rare — when it does occur, it usually means the cam emitted a
      burst before back-pressure propagated, or the HDF queue size
      is too small.  Treat ``N > 0`` as a data-integrity concern:
      increase ``t_period`` or reduce ``exposures_per_egu``.
      Promote the warning to an exception with
      ``warnings.filterwarnings("error",
      category=flyscan_3idc.FlyscanDataLossWarning)`` to fail-fast
      in strict environments.

    Parameters
    ----------
    detectors : list of ophyd Readable, optional
        Extra readables reported in the primary stream once per HDF
        frame, alongside ``det`` and ``flymotor``.  The plan does
        **not** call ``.trigger()`` on these -- a flyscan has no
        room to pause for ancillary triggers.  Each entry must
        update its reported value(s) on its own (CA-monitor-driven
        ``EpicsSignal``, scaler in continuous mode, etc.).  A
        trigger-required device will simply report stale values
        every frame.  Default: empty.
    det_name : str, default ``"adsimdet"``
        ophyd registry name of the area detector to fly.
    flymotor_name : str, default ``"m1"``
        ophyd registry name of the motor to fly.
    p_start : float, default ``0``
        First in-scan position (motor units).
    p_end : float, default ``5``
        Last in-scan position (motor units).
    exposures_per_egu : float, default ``2.0``
        Frame density: frames per motor engineering unit.  Total
        frame count is ``round(1 + (p_end - p_start) *
        exposures_per_egu)``.  Must be positive.
    t_period : float, default ``0.1``
        Time between successive frame exposures (seconds).
    t_acquire : float or None, default ``None``
        Per-frame exposure time (seconds).  ``None`` (default) means
        "use ``t_period``" (continuous exposure).  Must satisfy
        ``0 < t_acquire <= t_period``.
    taxi_allowance : float, default ``0.5``
        Extra distance (in motor EGU) added past the
        acceleration-based taxi region at each end of the scan.
        Increase if the first/last useful frame falls outside
        ``[p_start, p_end]``; must be non-negative.
    compression : str, default ``"zlib"``
        HDF5 chunk compression name.  Must match one of
        ``det.hdf1.compression.enum_strs`` if the IOC is reachable.
    ad_file_name : str, default ``"flyscan"``
        HDF5 filename stem (IOC appends a number and ``.h5``).
    ad_file_path : str, default ``"/tmp/flyscan"``
        Directory on the IOC's filesystem to write the HDF5 file.
    velocity_minimum : float or None, default ``None``
        Optional lower bound on the computed scan velocity, in motor
        EGU per second.  The effective floor is ``max(.VBAS,
        velocity_minimum)``; ``None`` (default) defers to ``.VBAS``
        alone.  Must be non-negative if supplied.
    plan_name : str, default ``"flyscan"``
        Recorded in the run's ``start`` document under ``plan_name``.
        Wrappers should pass their own name (e.g.
        ``flyscan(plan_name="my_3idc_scan", ...)``) so the run's
        provenance reflects the wrapper, not the inner flyscan call.
        This is explicit because bluesky's ``RunEngine`` cannot
        auto-derive ``plan_name`` for ``@bluesky_plan``-decorated
        plans (the decorator wraps the generator in a ``Plan`` class
        instance with no ``__name__`` attribute, so the auto-derived
        value would be the empty string).
    hdf_t_phase_offset : float or None, default ``None``
        Seconds to add to each ``hdf1.array_counter`` monitor-stream
        timestamp to obtain the corresponding frame's start-of-
        acquire moment.  Used by
        ``flyscan_3idc_analysis.pair_frames_to_positions`` to
        compute the three per-frame positions (start_acquire,
        end_acquire, end_period) recorded in the analysis output
        and (eventually) the NeXus master file.  ``None`` (default)
        means "use ``-t_acquire``" (``hdf_t`` arrives at
        ~``end_acquire``, so ``start_acquire = hdf_t - t_acquire``).
        See
        ``flyscan_3idc_analysis.hdf_timestamp_semantic_diagnostic``
        for determining the right value on a different IOC.
    _consumer_tick : float, default ``_CONSUMER_TICK_DEFAULT`` (20 ms)
        Internal: wake-up tick for the per-frame event consumer.
        Increase if your run-engine subscriptions can't keep up;
        decrease only for very high frame rates.  Rarely needs to
        be changed.
    _force_hdf_nonblocking : bool, default ``False``
        Internal/diagnostic.  ``False`` keeps the safe mode
        (``blocking_callbacks="Yes"``), where the cam back-throttles
        to the HDF write rate so no frames are dropped.  ``True``
        forces ``blocking_callbacks="No"``, letting the HDF plugin
        drop frames.  Its only legitimate use is to demonstrate the
        ``FlyscanDataLossWarning`` code path: set ``True``, push
        ``exposures_per_egu`` past what the HDF can sustain, and watch
        the post-scan warning fire.  Not for production data collection.
    md : dict, optional
        Additional metadata to record under the run's ``start``
        document.  Merged on top of the plan's computed metadata.

    Returns
    -------
    None (yields bluesky messages — pass to ``RE()`` to execute).

    Raises
    ------
    KeyError
        ``det_name`` or ``flymotor_name`` does not resolve to the
        expected ophyd device type in the registry.
    ValueError
        Position ordering is wrong (``p_end <= p_start``),
        ``exposures_per_egu`` is non-positive, ``taxi_allowance`` is
        negative, ``velocity_minimum`` is negative,
        ``t_acquire > t_period``, computed ``num_frames`` is too small,
        the motor's ``.VELO`` is unreadable, computed ``scan_velocity``
        is outside the effective ``[v_min, v_max]`` bracket, or
        ``compression`` is not in the HDF plugin's enumeration.
    RuntimeError
        IOC preflight failed (an expected PV did not connect), or
        the HDF plugin's file path does not exist on the IOC's
        filesystem, or the no-frames watchdog tripped during the
        scan.

    See Also
    --------
    configure_adsimdet :
        Standalone diagnostic that exercises the same AD acquisition
        protocol without a plan or RunEngine.  Useful for triaging
        an IOC that's misbehaving.
    compute_flyscan_geometry :
        Pure-function helper that derives ``p_initial``, ``p_final``,
        and ``num_frames`` from the user-supplied kwargs; unit-
        testable without an IOC.
    """
    ## Preparation
    # t_acquire defaults to t_period (continuous exposure).
    if t_acquire is None:
        t_acquire = t_period
    # hdf_t_phase_offset defaults to -t_acquire: hdf_t arrives at
    # ~end_acquire, so start_acquire = hdf_t - t_acquire.
    if hdf_t_phase_offset is None:
        hdf_t_phase_offset = -t_acquire
    # Validate each extra readable up front so a bad entry fails
    # before any IOC traffic.
    if detectors is None:
        detectors = []
    from bluesky.protocols import Readable

    for i, obj in enumerate(detectors):
        if not isinstance(obj, Readable):
            raise TypeError(
                f"detectors[{i}] = {obj!r} is not a Readable"
                " (a flyscan cannot trigger ancillary devices;"
                " each must update its own value)."
            )
    detector_names = [getattr(d, "name", repr(d)) for d in detectors]
    logger.info(
        "flyscan: entered. det_name=%r flymotor_name=%r"
        " p_start=%g p_end=%g exposures_per_egu=%g"
        " t_acquire=%g t_period=%g taxi_allowance=%g compression=%r"
        " detectors=%r",
        det_name,
        flymotor_name,
        p_start,
        p_end,
        exposures_per_egu,
        t_acquire,
        t_period,
        taxi_allowance,
        compression,
        detector_names,
    )
    det = oregistry.find(det_name, allow_none=True)
    flymotor = oregistry.find(flymotor_name, allow_none=True)
    logger.info(
        "flyscan: lookup -> det=%r flymotor=%r",
        getattr(det, "name", det),
        getattr(flymotor, "name", flymotor),
    )
    # Fail fast if the IOCs are down, before we incur the much longer
    # staging-time cost of discovering it via per-PV 5-second timeouts.
    # validate_flyscan_inputs (below) does device-type validation that
    # makes no CA calls, so preflight runs only after we know det and
    # flymotor are the right kinds of objects.  Reuse that validation
    # first by doing isinstance() guards here that mirror the validator;
    # cleaner would be to split validate_flyscan_inputs in two, but for
    # now we accept the small redundancy.
    if isinstance(det, ADBase) and isinstance(flymotor, EpicsMotor):
        preflight_connectivity(det, det_name, flymotor, flymotor_name)
    # Derive geometry first (motor must be the right type for the
    # .ACCL/.EGU caget calls in compute_flyscan_geometry to be
    # meaningful), then validate.
    if not isinstance(flymotor, EpicsMotor):
        # Mirror validate_flyscan_inputs' error so the user sees the
        # same message either way.
        raise KeyError(f"Motor {flymotor_name!r} not found in registry.")
    geometry = compute_flyscan_geometry(
        flymotor,
        p_start,
        p_end,
        exposures_per_egu,
        t_period,
        taxi_allowance,
    )
    v_velo, v_vmax, v_vbas, v_max, v_min = validate_flyscan_inputs(
        det,
        det_name,
        flymotor,
        flymotor_name,
        geometry,
        t_acquire,
        t_period,
        compression,
        velocity_minimum=velocity_minimum,
    )

    # Rebind derived values to plain locals so the rest of the plan
    # (taxi/scan takeoff, watchdog timing, _cleanup) keeps reading
    # them by their familiar names.
    p_initial = geometry.p_initial
    p_final = geometry.p_final
    num_frames = geometry.num_frames
    scan_active_duration = geometry.scan_duration
    scan_velocity = geometry.scan_velocity

    # IOC HDF plugin pre-allocation overflows somewhere between 1e6
    # and 1e9 for Float64 1024x1024 frames (likely a C int byte-count
    # overflow in NDFileHDF5).  num_capture = num_frames * 1.5 + 20 is
    # comfortable for any sensible scan size while absorbing takeoff &
    # landing leading frames, post-stop tail frames, and timing jitter.
    #
    # NOTE: the raw HDF5 file therefore holds MORE frames than
    # ``num_frames`` (pre-roll + post-stop tail).  This is explained in
    # docs/source/explanation/flyscan_frame_count.md -- keep that page
    # in sync if you change the pre-roll/tail behavior, this
    # over-allocation, or the frame/position pairing.
    hdf_num_capture = int(num_frames * 1.5) + 20

    # Worst-case flush timeout assumes the HDF plugin fills to
    # ``hdf_num_capture``.  Actual flush time at run-end uses the real
    # ``num_captured`` (recomputed in _cleanup).  This metadata lets a
    # downstream reader see the planning assumption.
    hdf_flush_timeout_max = _hdf_flush_timeout(det, hdf_num_capture)

    _md = build_flyscan_md(
        plan_name=plan_name,
        det_name=det_name,
        flymotor_name=flymotor_name,
        p_start=p_start,
        p_end=p_end,
        exposures_per_egu=exposures_per_egu,
        t_acquire=t_acquire,
        t_period=t_period,
        taxi_allowance=taxi_allowance,
        compression=compression,
        geometry=geometry,
        v_velo=v_velo,
        v_vmax=v_vmax,
        v_vbas=v_vbas,
        v_max=v_max,
        v_min=v_min,
        velocity_minimum=velocity_minimum,
        ad_file_name=ad_file_name,
        ad_file_path=ad_file_path,
        ad_read_path_template=_read_path_template(det) or "",
        ad_write_path_template=_write_path_template(det) or "",
        hdf_num_capture=hdf_num_capture,
        hdf_flush_timeout_max=hdf_flush_timeout_max,
        consumer_tick=_consumer_tick,
        hdf_t_phase_offset=hdf_t_phase_offset,
        detector_names=detector_names,
    )
    _md.update(md or {})

    original_cache = CacheParameters()

    # Closure flag used by _cleanup to decide whether to flush the
    # HDF5 file.  Stays False if the plan dies before acquisition
    # actually starts (e.g. a FailedStatus during the override loop),
    # so _cleanup doesn't waste time waiting for a non-existent flush
    # or, worse, flush a stale num_captured value left over from a
    # prior run.  List-of-one so takeoff_and_monitor can flip it
    # without a nonlocal declaration chain.
    capture_started = [False]

    # Snapshot the HDF plugin's cumulative dropped-arrays counter
    # (the IOC does not reset it across runs).  ``_cleanup`` reads
    # it again and reports the per-run delta.  Initialize with the
    # current value if readable, else None to signal "no baseline"
    # (then the delta isn't computed).  Defensive against IOCs that
    # don't expose ``dropped_arrays``.
    dropped_arrays_baseline = (
        _safe_get(
            det.hdf1,
            "dropped_arrays",
            use_monitor=False,
        )
        if _has_component(det, "hdf1")
        else None
    )
    if dropped_arrays_baseline is not None:
        logger.info(
            "flyscan: hdf1.dropped_arrays baseline = %d",
            int(dropped_arrays_baseline),
        )

    # Snapshot stage_sigs on every component we may mutate below.
    plugins = [
        getattr(det, nm)
        for nm in det.component_names
        if hasattr(getattr(det, nm), "blocking_callbacks")
    ]
    logger.info(
        "flyscan: snapshotting stage_sigs on det, cam, hdf1, and %d plugin(s): %s",
        len(plugins),
        [p.name for p in plugins],
    )
    saved_stage_sigs = snapshot_stage_sigs(det, det.cam, det.hdf1, *plugins)

    # Snapshot the ``kind`` of the two array_counter signals we want
    # in the primary-stream events for this run.  Setting Kind.hinted
    # makes bps.read(det) include them and live displays plot them.
    # We restore in _cleanup so other plans against this detector see
    # whatever kind it was configured with by default (typically
    # Kind.config or Kind.omitted for these counters).
    saved_kinds = snapshot_kinds(
        det.cam.array_counter,
        det.hdf1.array_counter,
    )
    logger.info(
        "flyscan: snapshotted kinds for %d signal(s): %s",
        len(saved_kinds),
        [s.name for s, _ in saved_kinds],
    )
    for sig, _ in saved_kinds:
        sig.kind = Kind.hinted

    ### Send motor to initial position (start moving; we wait below)
    # The takeoff is grouped so the RunEngine tracks the MoveStatus
    # and we can wait on it explicitly after the AD setup work in
    # _main has completed (see bps.wait(group="taxi") below).  This
    # preserves the deliberate concurrency: motor moves while AD
    # parameters are configured, then we wait, then we change
    # velocity.  Group name "taxi" matches the idiom used by other
    # APS fly scans.
    logger.info(
        "flyscan: taxi -> sending %s to p_initial=%g (non-blocking)",
        flymotor.name,
        p_initial,
    )
    yield from bps.abs_set(flymotor, p_initial, group="taxi")

    def update_master_file():
        """Update the NeXus master file once run is complete.

        Two updates, in order:

        1. Add an HDF5 external link at ``/entry/images`` pointing
           at the IOC's frame file's ``/entry/data`` group.
        2. Add the single primary-product ``NXdata`` group at
           ``/entry/flyscan_data`` (default plot).  Group
           attributes: ``signal="data"``,
           ``axes=["position_start_acquire"]``, plus provenance
           (``source``, ``n_frames_paired``, ``n_frames_expected``).
           Contents:

           - ``data``: a virtual dataset (``h5py.VirtualLayout`` +
             ``VirtualSource``) that slices the in-scan substack out
             of the externally-linked IOC frame file -- so the bytes
             are not copied; the virtual dataset just describes which
             rows of the source dataset are in-scan.
           - ``position_start_acquire`` / ``position_end_acquire`` /
             ``position_end_period``: the per-frame motor-position
             triple used as plot axes.
           - ``image_number`` / ``frame_index`` / ``timestamp``:
             subordinate per-frame correlation data.  ``frame_index``
             (= ``image_number - 1``) 0-based-indexes into the full
             ``/entry/images/data`` stack along its first axis
             (handles the row-count mismatch: only in-scan frames are
             paired here, while the images dataset has all captured
             frames including taxi/coast).

           All contents are computed by
           ``flyscan_3idc_analysis.pair_frames_to_positions_from_ad_file``
           against the AD HDF1 file (the only per-frame source).  Also
           sets ``/entry@default = "flyscan_data"`` so a NeXus-aware
           viewer opens this group by default (in-scan images vs. motor
           position).

        Called from ``_main`` after ``takeoff_and_monitor`` returns
        — i.e. after the inner run_decorator has emitted its stop
        document, which means nxwriter's background _threaded_writer
        has been launched.  ``wait_writer_plan_stub`` blocks until
        that thread finishes and ``nxwriter.output_nexus_file`` is
        populated.

        Discovers ``nxwriter`` and ``cat`` (the Tiled catalog) by
        importing them from ``id3c.startup``.  If that import
        fails (id3c not installed, or NEXUS_DATA_FILES.ENABLE
        was False at startup so ``nxwriter`` was never bound), this
        function is a no-op.  If only the flyscan_data-group write
        fails (e.g. catalog isn't ingestable, or
        pair_frames_to_positions_from_ad_file raises), the external
        link is still written and a WARNING is logged.
        """
        # Resolve nxwriter BEFORE any references to it.  Doing the
        # import inside the `if nxwriter is not None:` body (the
        # natural-looking shape) trips Python's local-variable
        # binding rule: the `from ... import nxwriter` later in the
        # function makes `nxwriter` a local for the whole function,
        # and the `if` check then UnboundLocalErrors before the
        # import has run.
        try:
            from id3c.startup import nxwriter
        except ImportError:
            # Either id3c isn't installed or nxwriter wasn't
            # enabled at startup (the name isn't bound, which
            # `from ... import` reports as ImportError).
            nxwriter = None

        if nxwriter is None:
            return  # generator returns empty; yield from sees StopIteration

        import h5py

        yield from nxwriter.wait_writer_plan_stub()
        # Two independent write steps follow (external link, then
        # flyscan_data).  Each is wrapped in its own try so one failure
        # does not mask the other.
        external_addr = "/entry/data"
        external_file = _external_link_target(det)
        master_addr = "/entry/images"
        master_file = nxwriter.output_nexus_file  # AttributeError if not written

        from pathlib import Path

        # Create the image-files symlink before resolving the external
        # link, so the link (and the later AD-file open) resolve.
        _ensure_ad_files_symlink(det, master_dir=Path(master_file).absolute().parent)

        # Loop A: master file openable for append?
        if not _wait_for_openable(master_file, mode="a"):
            logger.error(
                "flyscan._main: NeXus master file %r not openable for"
                " append after wait_writer_plan_stub; skipping all"
                " post-stub updates.",
                master_file,
            )
            return

        # Step 1 of 3: /entry/images external link.
        try:
            with h5py.File(master_file, "a") as root:
                root[master_addr] = h5py.ExternalLink(external_file, external_addr)
            logger.info(
                "flyscan._main: linked %s:%r into %s:%r",
                external_file,
                external_addr,
                master_file,
                master_addr,
            )
        except Exception as exc:
            logger.warning(
                "flyscan._main: external link write failed (%s -> %s:%r in %s): %r",
                master_addr,
                external_file,
                external_addr,
                master_file,
                exc,
            )

        # Loop B (test the external AD HDF1 file once, used by both
        # steps 2 and 3 below).  Step 2 prefers AD-file timestamps
        # when the file is openable (lossless source: the IOC writes
        # one row per acquired frame, so no CA-monitor coalescing
        # gaps).  Step 3 also requires the file for the VirtualLayout.
        external_file_openable = _wait_for_openable(external_file, mode="r")
        # /entry/flyscan_data — the single primary product.  One NXdata
        # group holding the in-scan image substack (VirtualLayout into
        # the external AD file) plus its per-frame correlation data,
        # sourced entirely from the AD HDF1 file (the only authoritative,
        # lossless per-frame source).  If the AD file is not openable we
        # write nothing and warn; there is no degraded fallback.
        df = None  # set below only if the AD file paired successfully
        try:
            from id3c.startup import cat
            from id3c.utils.flyscan_3idc_analysis import (
                pair_frames_to_positions_from_ad_file,
            )

            uid = nxwriter.uid
            run = cat[uid]
            if not external_file_openable:
                logger.warning(
                    "flyscan._main: AD file %r is not openable; skipping"
                    " /entry/flyscan_data.  Fix the image-files symlink"
                    " next to the master file so it resolves, then re-run"
                    " the analysis to recover /entry/flyscan_data.",
                    external_file,
                )
            else:
                df = pair_frames_to_positions_from_ad_file(run, external_file)
                if len(df) == 0:
                    logger.warning(
                        "flyscan._main: pair_frames_to_positions_from_ad_file"
                        " returned 0 rows for uid=%r; skipping"
                        " /entry/flyscan_data",
                        uid,
                    )
                    df = None
        except Exception as exc:
            logger.warning(
                "flyscan._main: per-frame pairing failed for"
                " /entry/flyscan_data in %s: %r.  The group will not"
                " be written.",
                master_file,
                exc,
            )
            df = None

        if df is not None:
            try:
                from id3c.utils.flyscan_3idc_analysis import write_flyscan_data

                # Expected total acquired-frame count (authoritative AD
                # file row count).  The in-scan paired count can be
                # legitimately smaller (taxi-in / coast-out frames);
                # recorded as provenance for the reader.
                n_frames_expected = _expected_frame_count(external_file, run)
                write_flyscan_data(
                    master_file,
                    external_file,
                    df,
                    external_addr=external_addr,
                    n_frames_expected=n_frames_expected,
                )
            except Exception as exc:
                logger.warning(
                    "flyscan._main: failed to write"
                    " /entry/flyscan_data into NeXus master file %s:"
                    " %r.  The master file is otherwise intact (the"
                    " external link /entry/images has been written);"
                    " the default-plot annotation"
                    " (/entry@default='flyscan_data') is not set.",
                    master_file,
                    exc,
                )

    def _main():
        """Preparation (no data collection) and takeoff (data collection)."""
        # AD runtime parameters (overridden, restored on exit)
        # Set these before device staging
        logger.info("flyscan._main: overriding AD runtime parameters")
        # must set before it is used when setting hdf1.file_path
        yield from original_cache.override(det.hdf1.create_directory, -5)
        # then, a list of them
        for obj, value in [
            (det.cam.array_counter, 0),  # optional
            (det.hdf1.array_counter, 0),  # optional
            (det.hdf1.auto_increment, "Yes"),
            (det.hdf1.auto_save, "Yes"),
            (det.hdf1.compression, compression),
            (det.hdf1.dropped_arrays, 0),  # optional
            (det.hdf1.dropped_output_arrays, 0),  # optional
            (det.hdf1.file_name, ad_file_name),
            (det.hdf1.file_number, 1),
            (det.hdf1.file_path, ad_file_path),
            (det.hdf1.file_template, "%s%s_%6.6d.h5"),
        ]:
            yield from original_cache.override(obj, value)
        # Verify the IOC can see the path.  Must happen before staging.
        # kicks the HDF plugin into capture mode.
        check_hdf_file_path(det)

        # Set parameters for staging
        # stage_decorator: sets before run, then restores after the run.
        det.stage_sigs["cam.image_mode"] = "Continuous"
        det.cam.stage_sigs["acquire_time"] = t_acquire
        det.cam.stage_sigs["acquire_period"] = t_period
        # cam.num_images must be effectively unbounded for continuous
        # acquisition: the cam is stopped by the boundary-detection
        # logic (motor crossing p_end), not by a frame count.  Some
        # IOC builds nonetheless honour cam.num_images as a hard cap
        # even in continuous image_mode -- if a user had pre-set
        # cam.num_images to a small value (e.g. 50) via MEDM, the cam
        # would stop after that many frames regardless of motor
        # position (a cam pre-set to 50 caps acquisition at 50 frames).
        # Setting num_images here via stage_sigs guarantees boundary
        # detection is the only stop condition; the user's pre-scan
        # value is snapshotted by the earlier snapshot_stage_sigs(det,
        # det.cam, ...) call and restored on unstage by
        # restore_stage_sigs.
        det.cam.stage_sigs["num_images"] = effective_num_images(t_period)
        det.hdf1.stage_sigs["num_capture"] = hdf_num_capture
        # AD callback-chain throttling.  Under
        # ``blocking_callbacks="No"`` the cam doesn't wait for HDF to
        # consume the previous frame before producing the next; HDF's
        # input queue overflows when the file-write throughput is
        # below the cam's rate, and frames are silently dropped.
        #
        # Trade-off chosen here: prefer fidelity (no drops) over rate.
        # Set ``blocking_callbacks="Yes"`` on the HDF plugin so the
        # cam auto-throttles to the HDF write rate.  Result: every
        # cam frame the IOC produces is captured; the achieved
        # frame rate may be below the user's requested ``1/t_period``
        # if the IOC can't keep up.  Look at the post-scan WARNING
        # log line that compares cam.array_counter to hdf1.array_counter
        # — if they diverge, increase ``t_period`` or reduce
        # ``exposures_per_egu``.
        #
        # Non-HDF plugins (image, pva, stats1, roi1) keep
        # ``blocking_callbacks="No"`` because they're typically
        # display/analysis sinks where dropped frames are tolerable
        # and we don't want them gating the cam.
        if hasattr(det.cam, "wait_for_plugins"):
            det.cam.stage_sigs["wait_for_plugins"] = "Yes"
        hdf_blocking_setting = "No" if _force_hdf_nonblocking else "Yes"
        if _force_hdf_nonblocking:
            logger.warning(
                "flyscan._main: _force_hdf_nonblocking=True — staging"
                " HDF with blocking_callbacks='No'.  This DELIBERATELY"
                " disables the cam-to-HDF back-pressure and will drop"
                " frames if HDF can't keep up with the cam.  This"
                " escape hatch is for testing the data-loss warning"
                " path; do NOT use for production data collection.",
            )
        for plugin in plugins:
            if plugin is det.hdf1:
                plugin.stage_sigs["blocking_callbacks"] = hdf_blocking_setting
            else:
                plugin.stage_sigs["blocking_callbacks"] = "No"
        det.hdf1.stage_sigs.move_to_end("capture")  # always last
        logger.debug(
            "flyscan._main: stage_sigs configured. det=%s cam=%s hdf1=%s",
            dict(det.stage_sigs),
            dict(det.cam.stage_sigs),
            dict(det.hdf1.stage_sigs),
        )

        # Wait for the takeoff move to finish (the landing), *then* change
        # velocity. bps.wait(group="taxi") consumes the MoveStatus that
        # bps.abs_set(..., group="taxi") registered with the
        # RunEngine; this replaces the old hand-rolled
        # wait_for_motor_done polling loop with the RunEngine's own
        # status-tracking machinery.
        logger.info(
            "flyscan._main: waiting for %s to reach p_initial",
            flymotor.name,
        )
        yield from bps.wait(group="taxi")
        logger.info(
            "flyscan._main: setting %s velocity to scan_velocity=%g",
            flymotor.name,
            scan_velocity,
        )
        # CacheParameters.override captures the pre-run .VELO on this
        # first call and restores it in _cleanup via original_cache.
        # restore() — satisfies the requirement (flyscan_3idc.py:97-98)
        # that the motor's .VELO be returned to its pre-run value once
        # the run is finished.
        yield from original_cache.override(flymotor.velocity, scan_velocity)

        logger.info("flyscan._main: entering takeoff_and_monitor")
        yield from takeoff_and_monitor()
        logger.info("flyscan._main: takeoff_and_monitor returned")

        yield from update_master_file()
        logger.info("flyscan._main: update_master_file completed")

    ## Takeoff & In-flight Monitor
    @bpp.stage_decorator([det])  # Don't stage the flymotor!
    # Three bespoke monitor streams (one per signal):
    #   * det.hdf1.array_counter: HDF writer's frame count (with
    #     EPICS timestamp, used downstream to sync with flymotor)
    #   * det.cam.array_counter: camera's frame count; cheap to
    #     collect, lets users compare cam & hdf
    #   * flymotor.user_readback: motor position at CA monitor rate
    @bpp.monitor_during_decorator(
        [
            det.hdf1.array_counter,
            det.cam.array_counter,
            flymotor.user_readback,
        ]
    )
    @bpp.run_decorator(md=_md)
    def takeoff_and_monitor():
        # Takeoff ordering.  The cam can deliver its first frame
        # several seconds after Acquire=1; launching the motor before
        # then would let it move past p_start, costing the user a
        # chunk of their requested frame budget.  So:
        #
        #   1. Start the cam acquiring (Acquire=1).
        #   2. Wait for the HDF plugin to receive its first frame
        #      (num_captured >= 1), bounded by a generous timeout.
        #      This is the "cam is genuinely producing frames" gate.
        #   3. Only then launch the motor toward p_final.
        #
        # Cost: a few pre-roll frames captured while the motor is
        # still parked at p_initial.  These are written to the HDF5
        # file (no harm — downstream trims by motor position anyway)
        # and counted into the IOC's num_capture allocation (already
        # generously sized at 1.5*num_frames+20).
        #
        # group="scan" registers the MoveStatus with the RunEngine so
        # the bps.wait(group="scan") below absorbs any post-scan
        # motor settling after monitor_loop returns.
        # Diagnostic: log what the IOC actually has for cam timings
        # right before we start.  Helps catch staging defects (e.g.
        # acquire_period got overridden, or acquire_time > t_period
        # silently capped by the IOC) without a "why is my frame rate
        # wrong" investigation.
        actual_acquire_time = _safe_get(det.cam, "acquire_time", use_monitor=False)
        actual_acquire_period = _safe_get(det.cam, "acquire_period", use_monitor=False)
        actual_image_mode = _safe_get(
            det.cam, "image_mode", use_monitor=False, as_string=True
        )
        actual_num_capture = _safe_get(det.hdf1, "num_capture", use_monitor=False)
        logger.info(
            "takeoff_and_monitor: IOC state pre-acquire:"
            " cam.acquire_time=%r cam.acquire_period=%r"
            " cam.image_mode=%r hdf1.num_capture=%r"
            " (requested: t_acquire=%g t_period=%g num_capture=%d)",
            actual_acquire_time,
            actual_acquire_period,
            actual_image_mode,
            actual_num_capture,
            t_acquire,
            t_period,
            hdf_num_capture,
        )
        logger.info("takeoff_and_monitor: starting %s acquisition", det.name)
        # Use bps.mv (not bps.abs_set) so we don't proceed until
        # Acquire_RBV has caught up to 1 ("Acquiring").  Otherwise
        # cam_stopped_status (built below) could fire immediately
        # at run=True evaluation if the RBV hadn't yet updated past
        # its pre-scan value of 0.  In continuous image_mode (set by
        # stage_sigs above), Acquire_RBV reaches 1 and stays there
        # until monitor_loop stops the cam.
        yield from bps.mv(det.cam.acquire, 1)
        # Surface arm failures fast.  The Eiger reports them within
        # ~2 ms via cam.detector_state -> Error + cam.status_message.
        # Without this check the run would otherwise time out 5 s later
        # in the first-frame wait below, with a misleading apstools
        # "Path '/' does not exist on IOC" error from the unwinding
        # path.
        yield from _check_cam_armed(det)
        # From this point on, _cleanup should treat the HDF plugin as
        # active (drain, flush, verify).  If we never get here,
        # _cleanup skips that work entirely.
        capture_started[0] = True

        # Wait for the cam to actually start producing frames before
        # launching the motor.  This gates on hdf1.num_captured (the
        # downstream-of-everything signal: cam produced a frame AND
        # the HDF plugin accepted it).  Timeout generously: if the
        # cam can't produce a single frame within 5 t_periods (floor
        # 5 s), something is wrong with the IOC chain and we'd rather
        # raise than launch the motor into a dead scan.
        first_frame_timeout = max(5.0 * t_period, 5.0)
        logger.info(
            "takeoff_and_monitor: waiting for first frame (timeout=%gs)",
            first_frame_timeout,
        )
        first_frame_status = SubscriptionStatus(
            det.hdf1.num_captured,
            lambda *, value, **_: int(value) >= 1,
            run=True,  # OK to fire at subscribe time if the IOC's
            # num_captured cache happens to already be >0
            # (e.g. from a prior run that left it set);
            # the alternative — missing a fast first
            # frame between subscribe and the first plan
            # tick — is worse.
            timeout=first_frame_timeout,
        )
        try:
            # Yield ticks until the status fires or its timeout
            # expires.  Using the same _consumer_tick that
            # monitor_loop uses below so plan-wakeup cadence is
            # consistent.
            t0_first = time.time()
            while not first_frame_status.done:
                yield from bps.sleep(_consumer_tick)
            if not first_frame_status.success:
                msg = (
                    "takeoff_and_monitor: cam did not deliver a first"
                    f" frame within {first_frame_timeout:g}s after"
                    f" Acquire=1 on {det.name}."
                    f" hdf1.num_captured={int(det.hdf1.num_captured.get(use_monitor=False))}"  # noqa: E501
                    f" hdf1.write_status={_safe_get(det.hdf1, 'write_status', as_string=True)!r}"  # noqa: E501
                    f" hdf1.write_message={_safe_get(det.hdf1, 'write_message', as_string=True)!r}"  # noqa: E501
                )
                logger.error(msg)
                raise RuntimeError(msg)
            first_frame_latency = time.time() - t0_first
            logger.info(
                "takeoff_and_monitor: first frame received after %.3fs"
                " (num_captured=%d)",
                first_frame_latency,
                int(det.hdf1.num_captured.get(use_monitor=False)),
            )
        except Exception:
            # Best-effort cleanup of the watchdog status; SubscriptionStatus
            # auto-unsubscribes on .done, so this only matters if we
            # raise before it fires.
            raise

        # Cam is live.  Now launch the motor toward p_final.
        logger.info(
            "takeoff_and_monitor: launching %s -> p_final=%g (non-blocking)",
            flymotor.name,
            p_final,
        )
        yield from bps.abs_set(flymotor, p_final, group="scan")

        # Watchdog grace period for the *rest* of the scan: the
        # expected scan duration plus a couple of periods of slack,
        # with a floor of 5 s for tiny scans.  At this point we
        # already know the cam delivered at least one frame, so the
        # watchdog below is now guarding "cam went silent mid-scan"
        # rather than "cam never started".
        no_frames_timeout = max(scan_active_duration + 2 * t_period, 5.0)

        # Status-based exit condition for monitor_loop.  Uses
        # cam.acquire_busy rather than cam.acquire: cam.acquire
        # (== Acquire_RBV) can stay at 1 for many seconds after
        # Acquire=0 is written (the IOC finishes its current burst
        # before the RBV drops), which would hang the loop.
        # cam.acquire_busy drops to 0 promptly when
        # wait_for_plugins=Yes (set via stage_sigs); the same signal
        # wait_for_acquire_drained uses in cleanup.  Falls back to
        # cam.acquire only on devices that don't expose acquire_busy.
        # This is an AndStatus of two sub-statuses:
        #   1. cam_stopped_status: cam.acquire_busy == 0 (preferred)
        #      or cam.acquire == 0 (fallback).  The busy signal
        #      goes 0 only after every enabled plugin (including
        #      hdf1) has finished processing the last frame, so
        #      this also implicitly covers cam-to-plugin drain.
        #      monitor_loop tells the cam to stop (Acquire=0) when
        #      the motor crosses p_end, then this status fires.
        #   2. drain_status: the HDF plugin queue is fully idle
        #      (no frames waiting AND no frame currently being
        #      written).  Same predicate shape as
        #      wait_for_acquire_drained's HDF sub-status.
        # Together: "the cam has stopped *and* every frame it
        # produced has been flushed by the HDF plugin" — the
        # actual condition for "scan is done."
        # ``run=False`` is critical here: the subscribe-time precheck
        # path of ``SubscriptionStatus(run=True)`` evaluates the
        # predicate on the cached value at subscribe time, which on
        # ``acquire_busy`` may transiently be 0 (the cam hasn't yet
        # started the burst from our recent ``bps.mv(cam.acquire, 1)``)
        # and would immediately satisfy ``value == 0`` — firing the
        # AndStatus at scan-start and exiting monitor_loop before any
        # frame arrives.  With ``run=False`` the callback fires only on
        # real CA monitor edges *after* subscribe time, so the first
        # interesting edge is "busy went to 0" later in the run when
        # the cam actually stops.
        if _has_component(det.cam, "acquire_busy"):
            cam_stopped_sig = det.cam.acquire_busy
            cam_stopped_signal_name = "cam.acquire_busy"
        else:
            cam_stopped_sig = det.cam.acquire
            cam_stopped_signal_name = "cam.acquire"
        logger.info(
            "takeoff_and_monitor: using %s for cam-stopped status",
            cam_stopped_signal_name,
        )
        cam_stopped_status = SubscriptionStatus(
            cam_stopped_sig,
            lambda *, value, **_: int(value) == 0,
            run=False,
        )
        # ``drain_status`` keeps ``run=True``: pre-scan the queue is
        # trivially empty so this fires immediately at subscribe time,
        # which on its own would be a defect — but the AndStatus is
        # gated by ``cam_stopped_status`` (which uses ``run=False``
        # above), so this just means "drain check is a no-op when
        # using acquire_busy with wait_for_plugins=Yes" (since
        # acquire_busy already implies HDF drain).  On detectors
        # without wait_for_plugins, drain_status's *later* edge from
        # queue going > 0 then back to 0 carries the real signal —
        # AndStatus completes the first time both halves are
        # simultaneously done, and StatusBase is monotonic so this
        # half being already-done is fine.
        drain_status = SubscriptionStatus(
            det.hdf1.num_queued_arrays,
            lambda *, value, **_: (
                int(value) == 0
                and int(det.hdf1.queue_free.get(use_monitor=False))
                == int(det.hdf1.queue_size.get(use_monitor=False))
            ),
            run=True,
        )
        hdf_drain_status = AndStatus(cam_stopped_status, drain_status)

        # No-frames watchdog: a status that times out if num_captured
        # doesn't reach > 0 within no_frames_timeout seconds.  ophyd's
        # StatusBase timeout mechanism sets the status to
        # done-with-exception (StatusTimeoutError) on its own thread;
        # the consumer in monitor_loop checks `watchdog_status.done and
        # not watchdog_status.success` per tick to detect the trip and
        # raise RuntimeError (the RE then sends STOP to all in-motion
        # movables, including flymotor).
        watchdog_status = SubscriptionStatus(
            det.hdf1.num_captured,
            lambda *, value, **_: int(value) > 0,
            run=True,
            timeout=no_frames_timeout,
        )

        # Closure flag so monitor_loop can signal "I issued a
        # controlled stop on the motor at p_end crossing."  When set,
        # the bps.wait(group="scan") below will see a FailedStatus
        # (the scan-group MoveStatus completes with success=False
        # when EpicsMotor.stop() fires), and we swallow it as
        # expected.  Without this flag, we'd have to either re-raise
        # (wrong: this is the planned, normal end-of-scan path) or
        # swallow unconditionally (wrong: would hide a real failure
        # on the abs_set path).
        motor_stopped_flag = [False]
        yield from monitor_loop(
            flymotor,
            det,
            p_end,
            exit_when=hdf_drain_status,
            watchdog=watchdog_status,
            tick=_consumer_tick,
            motor_stopped_flag=motor_stopped_flag,
            extra_readables=detectors,
        )

        # Wait for the scan-group MoveStatus to settle.  Two cases:
        #
        # 1. monitor_loop issued a controlled stop on the motor at
        #    the p_end crossing (the normal path).  The MoveStatus
        #    from bps.abs_set(..., group="scan") completes with
        #    success=False once the EpicsMotor.stop() callback runs,
        #    and bps.wait raises FailedStatus.  We swallow it: the
        #    "failure" is exactly what we asked for, and the motor
        #    is now decelerating cleanly under .ACCL control.
        # 2. monitor_loop exited without crossing p_end (e.g. the
        #    HDF drain finished before the motor reached p_end —
        #    unusual but legal).  motor_stopped_flag stays False, and
        #    bps.wait completes normally when the motor reaches
        #    p_final.
        #
        # FailedStatus from any other cause (e.g. the motor IOC
        # rejected the move) propagates out: those are real failures
        # the RunEngine should see.
        if motor_stopped_flag[0]:
            logger.info(
                "takeoff_and_monitor: waiting for %s to finish"
                " controlled-stop deceleration",
                flymotor.name,
            )
            try:
                yield from bps.wait(group="scan")
            except FailedStatus:
                # Expected: bps.stop() caused the scan-group
                # MoveStatus to fire with success=False.  The motor
                # is decelerating per .ACCL; _cleanup will verify it
                # has actually come to rest.
                logger.info(
                    "takeoff_and_monitor: %s scan-group MoveStatus"
                    " completed with success=False as expected after"
                    " controlled stop",
                    flymotor.name,
                )
        else:
            logger.info(
                "takeoff_and_monitor: waiting for %s to reach p_final"
                " (no early stop was issued)",
                flymotor.name,
            )
            yield from bps.wait(group="scan")

    def _cleanup():
        # Best-effort cleanup; swallow secondary failures so the
        # original exception (if any) reaches the RunEngine.
        #
        # Ordering matters here:
        #   1. Stop motor (no more position changes)
        #   2. Stop cam.acquire (no more new frames)
        #   3. Stop hdf1.capture (close the capture window)
        #   4. Drain HDF queue (flush in-flight frames to plugin buffer)
        #   5. write_file=1 (force HDF5 file to disk; required because
        #      auto_save=Yes does not flush when capture is stopped
        #      early, as we do here)
        #   6. Verify full_file_name (the "tell" that the file landed)
        #   7. Restore overridden signals
        #   8. Restore stage_sigs snapshots
        logger.info("flyscan._cleanup: starting")
        try:
            if motor_is_moving(flymotor):
                logger.info("flyscan._cleanup: stopping moving %s", flymotor.name)
                yield from bps.stop(flymotor)
        except Exception as exc:
            logger.exception("flyscan._cleanup: stop(flymotor) failed: %r", exc)

        try:
            logger.info("flyscan._cleanup: stopping %s acquire", det.name)
            yield from bps.mv(det.cam.acquire, 0)
        except Exception as exc:
            logger.exception("flyscan._cleanup: stop acquire failed: %r", exc)

        # Steps 3-6 below only matter if the plan actually got as far
        # as starting acquisition.  If we died earlier (e.g. a
        # FailedStatus during the override loop), skip them to avoid:
        #   * waiting for drains that will never come,
        #   * flushing a num_captured value left over from a prior run
        #     (the PV is cumulative across runs unless explicitly
        #     reset, which we do not do).
        if not capture_started[0]:
            logger.info(
                "flyscan._cleanup: capture was never armed in this run;"
                " skipping stop-capture / drain / flush",
            )
        else:
            try:
                logger.info("flyscan._cleanup: stopping %s.hdf1 capture", det.name)
                yield from bps.mv(det.hdf1.capture, 0)
            except Exception as exc:
                logger.exception("flyscan._cleanup: stop capture failed: %r", exc)

            # Let the cam settle and the HDF plugin drain its queue
            # before we ask it to write the file.  Cleanup latency
            # does not affect user-visible behavior, so we use a
            # coarser tick than monitor_loop's _consumer_tick.
            try:
                yield from wait_for_acquire_drained(det, poll=_CLEANUP_DRAIN_TICK)
            except Exception as exc:
                logger.exception(
                    "flyscan._cleanup: wait_for_acquire_drained failed: %r",
                    exc,
                )

            # Verify the file landed on disk.  We rely on auto_save=Yes
            # (set in the override list) to flush the HDF5 file when
            # capture stops: from a bluesky plan with auto_save=Yes,
            # Capture=0 causes the IOC to write the file without our
            # needing to set WriteFile=1.  (Do NOT add WriteFile=1 here:
            # with auto_save=Yes it runs after the file is already
            # written and the IOC rejects it with status=3 — a spurious
            # error on an otherwise-fine file.  WriteFile=1 is only
            # needed for manual GUI use with auto_save=No.)
            #
            # FullFileName_RBV is populated by the IOC after a
            # successful write.  An empty value here is the "tell"
            # that no file was saved.
            try:
                n_captured = _safe_get(det.hdf1, "num_captured", use_monitor=False) or 0
                full_name = _safe_get(
                    det.hdf1, "full_file_name", use_monitor=False, as_string=True
                )
                if n_captured > 0 and full_name:
                    logger.info(
                        "flyscan._cleanup: HDF5 file saved: %s (num_captured=%d)",
                        full_name,
                        n_captured,
                    )
                elif n_captured > 0:
                    logger.warning(
                        "flyscan._cleanup: HDF5 file not saved"
                        " (full_file_name empty; num_captured=%d,"
                        " write_status=%r, write_message=%r)",
                        n_captured,
                        _safe_get(
                            det.hdf1, "write_status", use_monitor=False, as_string=True
                        ),
                        _safe_get(
                            det.hdf1, "write_message", use_monitor=False, as_string=True
                        ),
                    )
                else:
                    logger.info(
                        "flyscan._cleanup: no frames captured; no file expected",
                    )
            except Exception as exc:
                logger.exception(
                    "flyscan._cleanup: file verification failed: %r",
                    exc,
                )

            # Report frames the HDF input dropped this run (cam
            # produced them, HDF couldn't keep up).  The counter is
            # cumulative across runs in the IOC, so we compare to the
            # baseline snapshotted at plan entry.  Non-zero drops
            # indicate the cam is producing faster than the HDF can
            # write — even with blocking_callbacks=Yes there are edge
            # cases (e.g. the cam's first burst before back-pressure
            # propagates).  Cleanup logs at WARNING for visibility.
            try:
                if dropped_arrays_baseline is not None:
                    dropped_now = _safe_get(
                        det.hdf1,
                        "dropped_arrays",
                        use_monitor=False,
                    )
                    if dropped_now is not None:
                        delta = int(dropped_now) - int(dropped_arrays_baseline)
                        if delta > 0:
                            cam_counter = _safe_get(
                                det.cam, "array_counter", use_monitor=False
                            )
                            hdf_counter = _safe_get(
                                det.hdf1, "array_counter", use_monitor=False
                            )
                            msg = (
                                f"HDF plugin dropped {delta} frame(s)"
                                f" during this run"
                                f" (hdf1.dropped_arrays:"
                                f" {int(dropped_arrays_baseline)}"
                                f" -> {int(dropped_now)})."
                                f" cam.array_counter={cam_counter!r},"
                                f" hdf1.array_counter={hdf_counter!r}."
                                f" The HDF plugin cannot keep up with the"
                                f" cam at the requested rate on this IOC"
                                f" / filesystem / host combination."
                                f" Frames produced by the cam are missing"
                                f" from the on-disk HDF5 file."
                                f" Increase t_period or reduce"
                                f" exposures_per_egu and retry."
                            )
                            # Two channels for visibility:
                            #   * logger.warning: lands in the log file
                            #     and any stream handlers (apsbits sets
                            #     these up to print to the console).
                            #   * warnings.warn: appears in IPython's
                            #     warning channel (rendered distinctly
                            #     from normal output) and can be
                            #     filtered/escalated to exception via
                            #     ``warnings.filterwarnings(...)``.
                            logger.warning("flyscan._cleanup: %s", msg)
                            warnings.warn(
                                msg,
                                category=FlyscanDataLossWarning,
                                stacklevel=2,
                            )
                        else:
                            logger.info(
                                "flyscan._cleanup: HDF dropped 0 frames"
                                " this run (dropped_arrays unchanged at %d)",
                                int(dropped_now),
                            )
            except Exception as exc:
                logger.exception(
                    "flyscan._cleanup: dropped-arrays check failed: %r",
                    exc,
                )

        try:
            logger.info(
                "flyscan._cleanup: restoring %d cached signal(s)",
                len(original_cache),
            )
            yield from original_cache.restore()
        except Exception as exc:
            logger.exception("flyscan._cleanup: restore failed: %r", exc)

        try:
            logger.info("flyscan._cleanup: restoring stage_sigs snapshot")
            restore_stage_sigs(saved_stage_sigs)
        except Exception as exc:
            logger.exception(
                "flyscan._cleanup: restore stage_sigs failed: %r",
                exc,
            )

        try:
            logger.info(
                "flyscan._cleanup: restoring kinds snapshot (%d signal(s))",
                len(saved_kinds),
            )
            restore_kinds(saved_kinds)
        except Exception as exc:
            logger.exception(
                "flyscan._cleanup: restore kinds failed: %r",
                exc,
            )

        logger.info("flyscan._cleanup: done")
        # finalize_wrapper consumes _cleanup as a generator, so it must
        # yield at least one message.
        yield from bps.null()

    # finalize_wrapper guarantees _cleanup runs on success, exception,
    # *and* RE-injected exceptions (Ctrl-C, RequestAbort, ...), and
    # re-raises the original exception so the RunEngine sees it.
    yield from bpp.finalize_wrapper(_main(), _cleanup())
