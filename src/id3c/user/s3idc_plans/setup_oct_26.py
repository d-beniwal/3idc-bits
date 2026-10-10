"""3-ID-C data-acquisition plans -- properly-staged rewrite (Oct 2026).

``setup_june_26.py`` has a batch of "direct-AD" plans that configure
``eiger2.cam`` / ``eiger2.hdf1`` / ``eiger2.pva`` with ad-hoc ``bps.mv()``
calls and restore state with a hand-written ``_cleanup()`` that resets a few
signals to hardcoded "known good" values -- not whatever was actually there
before the plan ran. That is a known high-priority gap (see
``../../../.context/DECISIONS.md``, 2026-10-07 entry): it does not use
ophyd's built-in ``stage()``/``stage_sigs``/``unstage()`` mechanism, which
records the REAL prior value of every signal it touches and can restore it
exactly on ``unstage()``.

This file is where that migration lives, plan by plan, so it can be
validated incrementally **without touching the working plans in
``setup_june_26.py``**. Only ``cont_aq`` has been redone so far.

Pattern used here for any new plan added to this file:

1. Set the specific keys this plan needs on each affected sub-device's
   ``stage_sigs`` dict (``eiger2.cam.stage_sigs[...]``,
   ``eiger2.hdf1.stage_sigs[...]``, ``eiger2.pva.stage_sigs[...]``) --
   these are per-Component dicts; mutating them is how you say "stage
   this signal to this value."
2. ``yield from bps.stage(eiger2)``. ``Device.stage()`` cascades into every
   sub-device Component automatically, and for each staged signal it saves
   the CURRENT value into that Component's own ``_original_vals`` before
   applying the new one.
3. Immediately pop the keys added in step 1 back out of each
   ``stage_sigs`` dict. ``unstage()`` restores from ``_original_vals``, NOT
   from ``stage_sigs``, so this does not affect this plan's own revert --
   it only prevents the override values from silently leaking into some
   unrelated future ``bps.stage(eiger2)`` call made by another plan.
4. Whoever is done using the device calls ``RE(bps.unstage(eiger2))`` to
   restore everything this plan changed to its pre-existing value.
   ``bps.stage``/``bps.unstage`` refuse to double-stage/double-unstage
   (``RedundantStaging``), which is a deliberate safety net, not a bug.

GUI-parseable docstrings: same fixed NumPy ``Parameters`` grammar as
``setup_june_26.py`` -- see that file's module docstring for the full
grammar spec; not repeated here.
"""

import logging
import time

from apsbits.core.instrument_init import oregistry
from bluesky import plan_stubs as bps  # noqa: F401
from bluesky import preprocessors as bpp
from bluesky.utils import plan
from ophyd.status import SubscriptionStatus

from id3c.plans.flyscan_3idc import CacheParameters
from id3c.plans.flyscan_3idc import _safe_get
from id3c.plans.flyscan_3idc import check_hdf_file_path
from id3c.plans.flyscan_3idc import restore_stage_sigs
from id3c.plans.flyscan_3idc import snapshot_stage_sigs
from id3c.plans.flyscan_3idc import wait_for_acquire_drained

logger = logging.getLogger(__name__)


def _pop_keys(stage_sigs, keys):
    """Remove `keys` from a Component's `stage_sigs` dict, if present."""
    for key in keys:
        stage_sigs.pop(key, None)


@plan
def cont_aq(
    frame_exposure_time: float = 1.0,
):
    """Start the Eiger2 free-running via proper staging (PVA stream only).

    Staged rewrite of ``cont_aq`` from ``setup_june_26.py``. Configures
    ``eiger2.cam`` for ``Continuous`` image mode at the given per-frame
    exposure time and fires ``Acquire=1`` **without waiting** -- in
    Continuous mode the cam never stops on its own, so a waited set would
    block the console forever. No HDF5 file is written and no Bluesky run /
    catalog entry is opened: this plan only arms the camera so a separate
    piece of software (not Bluesky) can read the live frames off the
    Eiger's PVA plugin (``eiger2.pva``).

    Unlike the ``setup_june_26.py`` version, every signal this plan changes
    (``cam.image_mode``, ``cam.acquire_time``, ``cam.acquire_period``,
    ``cam.array_callbacks``, ``hdf1.capture``, ``hdf1.auto_save``,
    ``pva.enable``) is applied through ophyd's ``stage()`` /
    ``stage_sigs`` mechanism instead of a plain ``bps.mv()``. That means
    the value each signal had *before* this plan ran is recorded, and a
    later ``RE(bps.unstage(eiger2))`` puts every one of them back exactly
    as found -- not a hardcoded "safe default."

    The acquisition keeps running after this plan returns. When done,
    stop and restore the original state from the console with::

        RE(bps.mv(eiger2.cam.acquire, 0))
        RE(bps.unstage(eiger2))

    Calling ``cont_aq`` again before unstaging raises ``RedundantStaging``
    -- that is intentional, it means the previous run was never properly
    closed out.

    Parameters
    ----------
    frame_exposure_time : float [s]
        Frame exposure time :: Per-frame exposure (cam.acquire_time);
        acquire_period is set equal to it for back-to-back frames.

    Example::

        RE(cont_aq(0.1))
        # ... read the PVA stream from other software ...
        RE(bps.mv(eiger2.cam.acquire, 0))
        RE(bps.unstage(eiger2))
    """
    if frame_exposure_time <= 0:
        raise ValueError("frame_exposure_time must be > 0")

    eiger2 = oregistry["eiger2"]

    cam_keys = ["image_mode", "acquire_time", "acquire_period", "array_callbacks"]
    eiger2.cam.stage_sigs["image_mode"] = "Continuous"
    eiger2.cam.stage_sigs["acquire_time"] = frame_exposure_time
    eiger2.cam.stage_sigs["acquire_period"] = frame_exposure_time
    eiger2.cam.stage_sigs["array_callbacks"] = 1

    # Safety net: stage HDF1 off so nothing gets saved while free-running,
    # even if a prior plan left capture armed / auto_save on.
    hdf_keys = []
    if hasattr(eiger2, "hdf1"):
        hdf_keys = ["capture", "auto_save"]
        eiger2.hdf1.stage_sigs["capture"] = 0
        eiger2.hdf1.stage_sigs["auto_save"] = "No"

    # Make sure frames actually reach the PVA plugin.
    pva_keys = []
    if hasattr(eiger2, "pva"):
        pva_keys = ["enable"]
        eiger2.pva.stage_sigs["enable"] = "Enable"

    yield from bps.stage(eiger2)

    # stage() already copied the real originals into each Component's own
    # _original_vals; drop our overrides from stage_sigs now so they don't
    # leak into some OTHER future bps.stage(eiger2) call by a different plan.
    _pop_keys(eiger2.cam.stage_sigs, cam_keys)
    _pop_keys(eiger2.hdf1.stage_sigs, hdf_keys)
    _pop_keys(eiger2.pva.stage_sigs, pva_keys)

    print(
        f"cont_aq: eiger2 staged + free-running (Continuous, "
        f"{frame_exposure_time:g}s/frame) -- PVA stream only, nothing saved.\n"
        "Stop and restore the ORIGINAL cam/HDF/PVA state with:\n"
        "    RE(bps.mv(eiger2.cam.acquire, 0))\n"
        "    RE(bps.unstage(eiger2))"
    )

    # Fire-and-forget: Continuous mode runs until explicitly stopped, so we
    # must NOT wait on this set (same reasoning as every cam.acquire
    # fire-and-forget in setup_june_26.py).
    yield from bps.abs_set(eiger2.cam.acquire, 1)


@plan
def fixed_count_acquire(
    num_images: int = 500,
    acquire_time: float = 0.02,
    file_name: str = "fixedCount",
    file_path: str = "/home/sector3/s3ida/XRD/2026-2/setup/June17/",
    compression: str = "zlib",
    drain_timeout: float = None,
    md: dict = None,
):
    """Acquire a fixed number of Eiger2 frames, no motor motion (HDF throughput test).

    Diagnostic plan -- isolates whether the multi-minute post-scan HDF5
    "Writing"/"Capturing" hold-up seen after ``omega_fly``/``flyscan`` (see
    the bluesky skill's project memory, "Post-p_end 'scan hangs for
    minutes'", 2026-10-08) comes from something in ``flyscan_3idc.py``'s
    own plan logic (the CA-monitor frame queue, consumer-tick event
    emission, motor-coordination/``monitor_loop``) or whether it is purely
    a property of the Eiger2 HDF plugin's write throughput.

    This plan stages ``eiger2`` as close as possible to how ``flyscan``
    stages it for a real scan:

    * ``cam.image_mode = "Multiple"``, ``cam.num_images = num_images`` --
      a real, finite count (unlike flyscan's geometry-driven, open-ended
      acquisition). **Not trusted to self-stop at exactly
      ``num_images``:** ``flyscan_3idc.flyscan`` deliberately never
      relies on frame-count-based auto-stop on this Eiger/IOC
      combination -- it always runs in ``Continuous`` mode and stops the
      cam itself via explicit boundary-detection logic (see the comment
      on ``det.cam.stage_sigs["num_images"] = effective_num_images(...)``
      in ``flyscan_3idc.py``). Confirmed on real hardware 2026-10-10:
      an earlier version of this plan trusted ``Multiple``-mode auto-stop
      and the cam overran well past ``num_images`` (~700 frames captured
      for a 500-frame request). This plan now actively stops the cam
      itself once ``hdf1.num_captured`` reaches ``num_images`` (see
      below) instead of trusting the cam to do it.
    * ``cam.wait_for_plugins = "Yes"`` so ``cam.acquire_busy`` only drops
      to 0 once every plugin (including ``hdf1``) has finished processing
      the last frame -- the same signal ``flyscan_3idc.flyscan`` uses to
      decide the scan is actually done, and the signal this plan polls
      after issuing its own explicit stop.
    * ``hdf1.file_write_mode = "Stream"`` -- flyscan never overrides this,
      so a real flyscan run writes in Stream mode too (frames land on
      disk incrementally, as they're captured, rather than being
      buffered and flushed in one shot at ``Capture=0``); set explicitly
      here so the match to flyscan is documented, not incidental.
      ``hdf1.num_capture`` is set to **exactly** ``num_images`` -- unlike
      flyscan's inflated ``int(expected_frames * 1.5) + 20`` upper bound
      (needed there because the real frame count is unknown ahead of
      time), this plan's count *is* known exactly, so ``num_capture``
      doubles as an independent safety net: AreaDetector's Stream-mode
      HDF plugin auto-sets ``Capture=0`` once ``num_captured ==
      num_capture``, capping the written file at ``num_images`` even if
      the cam itself does not stop cleanly. Paired with the same
      ``hdf1.blocking_callbacks = "Yes"`` / every other plugin's
      ``blocking_callbacks = "No"`` throughput-throttling convention (no
      dropped frames; the cam self-paces to the HDF write rate). Which
      plugins count as "every other plugin" is discovered the same way
      flyscan does it -- scanning ``eiger2.component_names`` for anything
      exposing ``blocking_callbacks`` -- rather than a fixed, hand-picked
      list, so it tracks ``devices.yml`` automatically if a plugin is
      added or removed later.
    * ``hdf1.compression`` -- same parameter flyscan exposes (default
      ``"zlib"``, matching ``flyscan_3idc.flyscan``'s own default).
      **Important for this specific investigation:** the beamtime runs
      already analyzed (bluesky skill project memory, 2026-10-08 entry)
      were actually taken with compression overridden to the literal
      string ``"None"`` somewhere upstream of ``flyscan()`` (source of
      that override not yet found) -- pass ``compression="None"`` here to
      reproduce what those runs actually did, not the library default.
    * ``hdf1.create_directory``, ``file_name``, ``file_number``,
      ``file_path``, ``file_template``, ``auto_save``, ``auto_increment``
      are set the same way ``flyscan_3idc.flyscan`` sets them: through
      ``flyscan_3idc.CacheParameters`` (reused directly, not
      re-implemented) -- a plan-stub cache that records each signal's
      value with ``yield from cache.override(signal, value)`` and puts it
      all back with ``yield from cache.restore()``. This includes
      ``file_template`` via a normal *waited* ``bps.mv`` (matching
      ``flyscan``'s own override list exactly): an earlier bluesky-skill
      memory note claimed ``file_template``'s RBV reads back oddly and a
      waited set times out, but that referred to the hand-rolled
      ``setup_june_26.py`` plans -- the current ``flyscan_3idc.flyscan``
      source does not special-case it, so this plan doesn't either.
      ``check_hdf_file_path(eiger2)`` (also reused from
      ``flyscan_3idc.py``) verifies the path actually exists on the IOC's
      filesystem right after, before staging -- the same fail-fast flyscan
      relies on instead of a confusing later "num_captured stuck at 0."

    Deliberately **not** replicated: flyscan also resets
    ``cam.array_counter``/``hdf1.array_counter``/``hdf1.dropped_arrays``/
    ``hdf1.dropped_output_arrays`` to 0 per run via the same
    ``CacheParameters`` mechanism. That is bookkeeping for flyscan's own
    frame/position pairing, not part of the detector/plugin write path,
    and some of those signals may not be writable on every IOC build
    (flyscan tags them "(optional)"). This plan instead reads
    ``hdf1.dropped_arrays`` once before arming and once after, and reports
    the delta -- same diagnostic value (did *this* run drop frames) without
    assuming those counters are settable.

    but drives **none** of flyscan's own machinery: no motor, no
    CA-monitor frame queue, no consumer tick, no per-point event
    emission. The post-acquire wait reuses
    ``id3c.plans.flyscan_3idc.wait_for_acquire_drained`` directly
    (imported, not re-implemented), so this plan's notion of "drained" is
    identical to flyscan's own -- any difference in measured drain
    time/rate between this plan and a real ``omega_fly`` run is therefore
    attributable to flyscan's extra plan logic, not to a different
    definition of "done." ``hdf1.num_captured`` and
    ``cam.array_counter`` are monitored throughout the run (same
    diagnostic-stream idea flyscan uses) so the capture-rate-over-time
    curve can be pulled from the Tiled catalog afterward and compared
    directly against a flyscan run's.

    Interpretation: if this plan shows the same few-fps drain ceiling
    flyscan's post-p_end hang does (flyscan measured ~4.7-5.6 fps at
    ``t_acquire=0.02s``), the bottleneck is confirmed to live outside
    flyscan's plan logic (IOC HDF write throughput / filesystem /
    network path). If this plan drains much faster, that points back at
    something in flyscan's own monitor-loop/CA-queue implementation.

    Like ``cont_aq``, every signal this plan changes on ``eiger2.cam``
    and the plugins is applied through ``stage_sigs`` and reverted via
    ``bps.unstage`` (automatic here, via ``bpp.stage_decorator``) --
    the ``stage_sigs`` dicts themselves are snapshotted first and
    restored in a ``finally`` so the overrides don't leak into some
    other plan's later ``bps.stage(eiger2)`` call.

    Parameters
    ----------
    num_images : int
        Frame count :: Total number of frames to acquire in one burst
        (sets ``cam.num_images`` and sizes the HDF plugin's capture
        upper bound).
    acquire_time : float [s]
        Exposure per frame :: ``cam.acquire_time``; ``cam.acquire_period``
        is set equal to it for back-to-back frames (matches flyscan's
        ``t_acquire=t_period`` runs).
    file_name : str
        Output file prefix :: HDF5 file name prefix (``hdf1.file_name``).
    file_path : str
        Output directory :: HDF5 write directory, IOC's view
        (``hdf1.file_path``).
    compression : str
        HDF5 compression :: ``hdf1.compression``; must match one of
        ``eiger2.hdf1.compression.enum_strs`` if the IOC is reachable.
        Pass ``"None"`` to reproduce the already-analyzed beamtime runs
        (see Notes below), or try ``"lz4"`` for the untested faster-codec
        idea from that same analysis.
    drain_timeout : float [s]
        Max drain wait :: Longest time to wait for the HDF plugin to
        finish writing after this plan explicitly stops the cam; blank
        sizes it from ``num_images`` (generous: measured flyscan drain
        rates have been as low as ~3.6 fps). Also used as a floor for
        the internal timeout on reaching ``num_images`` captured frames
        in the first place.

    Example::

        RE(fixed_count_acquire(500, 0.02))
        RE(fixed_count_acquire(500, 0.02, compression="None"))  # match the
        # actual beamtime flyscan runs already analyzed, not flyscan's
        # library default of "zlib"

    Notes
    -----
    ``num_capture`` should stay well under ~1e6 for typical frame sizes
    (the IOC's NDFileHDF5 plugin can overflow its internal byte-count
    arithmetic for very large values -- see ``flyscan_3idc.
    configure_adsimdet``'s docstring); not a concern at ``num_images=500``.
    """
    if num_images <= 0:
        raise ValueError("num_images must be > 0")
    if acquire_time <= 0:
        raise ValueError("acquire_time must be > 0")

    eiger2 = oregistry["eiger2"]

    # Same compression validation flyscan_3idc.validate_flyscan_inputs does
    # against the HDF plugin's enum_strs: a clear ValueError here beats a
    # much-later, confusing IOC-side rejection at write time. Defensive:
    # skip if enum_strs can't be read (offline IOC, mock, etc.).
    try:
        enum_strs = tuple(getattr(eiger2.hdf1.compression, "enum_strs", ()) or ())
    except Exception as exc:
        logger.debug("fixed_count_acquire: cannot read compression enum_strs: %r", exc)
        enum_strs = ()
    if enum_strs and compression not in enum_strs:
        raise ValueError(
            f"compression={compression!r} not in HDF plugin's allowed set"
            f" {list(enum_strs)!r}."
        )

    # Exact, not flyscan's inflated upper bound: the count here is known
    # ahead of time (it's the caller's own parameter), so num_capture
    # doubles as an independent safety net -- the HDF plugin's own
    # Stream-mode auto-stop (Capture -> 0 once num_captured ==
    # num_capture) caps the written file at num_images even if the cam
    # itself does not stop cleanly (see cam.image_mode docstring note).
    hdf_num_capture = num_images
    if drain_timeout is None:
        # Generous: measured flyscan drain rates have been as low as
        # ~3.6 fps (bluesky skill memory, 2026-10-08 entry) -- size for
        # a worse rate than anything seen so far, plus headroom, rather
        # than guessing a fixed constant.
        drain_timeout = max(60.0, num_images / 3.0)

    _md = {
        "plan_name": "fixed_count_acquire",
        "num_images": num_images,
        "acquire_time": acquire_time,
        "hdf_num_capture": hdf_num_capture,
        "ad_file_name": file_name,
        "ad_file_path": file_path,
        "purpose": (
            "HDF throughput isolation test: same eiger2 detector staging"
            " as flyscan_3idc.flyscan, no motor / monitor-loop logic"
        ),
    }
    _md.update(md or {})

    # file_name/file_path/file_template/file_number/auto_save/
    # auto_increment/compression/create_directory are "caller is
    # responsible for setting" fields (apstools.devices
    # .AD_EpicsFileNameMixin._remove_caller_stage_sigs) -- set them the
    # same way flyscan_3idc.flyscan does, via CacheParameters (plan-stub
    # cache: records each signal's current value on override, restores
    # it in one shot on restore()), reused directly here rather than
    # re-implemented. create_directory must be set before file_path is
    # used (same ordering flyscan uses).
    original_cache = CacheParameters()
    yield from original_cache.override(eiger2.hdf1.create_directory, -5)
    for obj, value in [
        (eiger2.hdf1.auto_increment, "Yes"),
        (eiger2.hdf1.auto_save, "Yes"),
        (eiger2.hdf1.compression, compression),
        (eiger2.hdf1.file_name, file_name),
        (eiger2.hdf1.file_number, 1),
        (eiger2.hdf1.file_path, file_path),
        (eiger2.hdf1.file_template, "%s%s_%6.6d.h5"),
    ]:
        yield from original_cache.override(obj, value)
    # Verify the IOC can actually see file_path -- must happen before
    # staging (same reasoning/helper as flyscan_3idc.flyscan: without
    # this, a bad path fails silently inside the IOC and num_captured
    # just stays 0 forever instead of raising here).
    check_hdf_file_path(eiger2)

    # Protect stage_sigs from leaking into some other plan's later
    # bps.stage(eiger2) call -- same snapshot/restore flyscan_3idc.flyscan
    # itself uses (reused directly here, not re-implemented).  Plugin
    # discovery matches flyscan exactly: scan component_names for
    # anything exposing blocking_callbacks, rather than a fixed list.
    plugins = [
        getattr(eiger2, nm)
        for nm in eiger2.component_names
        if hasattr(getattr(eiger2, nm), "blocking_callbacks")
    ]
    saved_stage_sigs = snapshot_stage_sigs(eiger2, eiger2.cam, eiger2.hdf1, *plugins)

    try:
        eiger2.cam.stage_sigs["image_mode"] = "Multiple"
        eiger2.cam.stage_sigs["num_images"] = num_images
        eiger2.cam.stage_sigs["acquire_time"] = acquire_time
        eiger2.cam.stage_sigs["acquire_period"] = acquire_time
        eiger2.cam.stage_sigs["array_callbacks"] = 1
        if hasattr(eiger2.cam, "wait_for_plugins"):
            eiger2.cam.stage_sigs["wait_for_plugins"] = "Yes"

        # "Stream" (not "Capture") -- this is apstools' own class default
        # (apstools.devices.AD_EpicsHdf5FileName.__init__ sets
        # file_write_mode="Stream" unconditionally) and flyscan_3idc.flyscan
        # never overrides it, so a real flyscan run writes in Stream mode
        # too (frames land on disk as they're captured, not buffered and
        # flushed in one shot at Capture=0). Set explicitly here so that
        # match is documented, not incidental. ("Capture" mode is only
        # used by configure_adsimdet, a standalone, non-plan helper for
        # the generic adsimdet test IOC -- not by flyscan or this plan.)
        eiger2.hdf1.stage_sigs["file_write_mode"] = "Stream"
        eiger2.hdf1.stage_sigs["num_capture"] = hdf_num_capture
        # Same throughput convention as flyscan_3idc.flyscan: block the
        # HDF plugin (cam back-pressures to HDF write rate, no dropped
        # frames) while every other plugin stays non-blocking. `plugins`
        # already includes hdf1 itself (see the component_names scan
        # above), so this loop distinguishes it by identity, same as
        # flyscan's own loop over the identical list.
        for plugin in plugins:
            plugin.stage_sigs["blocking_callbacks"] = (
                "Yes" if plugin is eiger2.hdf1 else "No"
            )
        # "capture" is already in hdf1.stage_sigs by default (apstools
        # .AD_EpicsHdf5FileName.__init__); move it to the end so it is
        # applied last, after every other override above (same ordering
        # flyscan_3idc.flyscan enforces, for the same reason).
        eiger2.hdf1.stage_sigs.move_to_end("capture")

        dropped_baseline = _safe_get(eiger2.hdf1, "dropped_arrays", use_monitor=False)

        @bpp.stage_decorator([eiger2])
        @bpp.monitor_during_decorator(
            [eiger2.hdf1.num_captured, eiger2.cam.array_counter]
        )
        @bpp.run_decorator(md=_md)
        def _acquire():
            logger.info(
                "fixed_count_acquire: staged; starting acquisition"
                " (%d frames @ %gs/frame, hdf_num_capture=%d exact,"
                " drain_timeout=%gs)",
                num_images,
                acquire_time,
                hdf_num_capture,
                drain_timeout,
            )
            t0 = time.time()
            # Fire-and-forget: Acquire_RBV can lag the real state (Eiger
            # stop gotcha, bluesky skill memory) -- never wait on it.
            yield from bps.abs_set(eiger2.cam.acquire, 1)

            # Actively wait for exactly num_images frames rather than
            # trusting cam.image_mode="Multiple" to self-stop there --
            # flyscan_3idc.flyscan never relies on frame-count-based
            # auto-stop on this Eiger/IOC (it always uses Continuous mode
            # + explicit boundary-detection stop instead; see this
            # plan's docstring). hdf1.num_captured is the
            # downstream-of-everything signal (cam produced the frame
            # AND hdf1 accepted it), same signal
            # takeoff_and_monitor's first_frame_status uses in
            # flyscan_3idc.py.
            capture_timeout = max(drain_timeout, 2.0 * acquire_time * num_images + 30.0)
            count_reached_status = SubscriptionStatus(
                eiger2.hdf1.num_captured,
                lambda *, value, **_: int(value) >= num_images,
                run=True,
                timeout=capture_timeout,
            )
            while not count_reached_status.done:
                yield from bps.sleep(0.1)
            t_captured = time.time()
            capture_elapsed = t_captured - t0
            if not count_reached_status.success:
                logger.warning(
                    "fixed_count_acquire: only %d/%d frames captured"
                    " within capture_timeout=%gs -- stopping the cam"
                    " anyway and reporting what was captured",
                    int(eiger2.hdf1.num_captured.get(use_monitor=False) or 0),
                    num_images,
                    capture_timeout,
                )

            # Explicit Eiger-safe stop (same sequence flyscan_3idc.py
            # uses everywhere it stops this cam): fire non-blocking,
            # then confirm idle via wait_for_acquire_drained's
            # acquire_busy/HDF-queue check -- NOT Acquire_RBV, which can
            # stay at 1 for seconds after Acquire=0 is written.
            yield from bps.abs_set(eiger2.cam.acquire, 0)
            yield from wait_for_acquire_drained(eiger2, poll=0.1, timeout=drain_timeout)
            elapsed = time.time() - t0
            drain_elapsed = time.time() - t_captured
            n_captured = _safe_get(eiger2.hdf1, "num_captured", use_monitor=False) or 0
            rate = n_captured / elapsed if elapsed > 0 else float("nan")
            logger.info(
                "fixed_count_acquire: captured %d/%d frames in %.1fs,"
                " then drained after %.1fs more (%.1fs total, %.2f fps"
                " overall)",
                n_captured,
                num_images,
                capture_elapsed,
                drain_elapsed,
                elapsed,
                rate,
            )
            if n_captured > num_images:
                logger.warning(
                    "fixed_count_acquire: %d frames captured, more than"
                    " the requested %d -- the HDF plugin's num_capture"
                    " safety net should have prevented this; check the"
                    " IOC's Stream-mode auto-stop behavior",
                    n_captured,
                    num_images,
                )
            elif n_captured < num_images:
                logger.warning(
                    "fixed_count_acquire: only %d/%d frames captured"
                    " -- rerun with a larger capture_timeout or check"
                    " the IOC/network path",
                    n_captured,
                    num_images,
                )
            dropped_now = _safe_get(eiger2.hdf1, "dropped_arrays", use_monitor=False)
            if (
                dropped_baseline is not None
                and dropped_now is not None
                and int(dropped_now) - int(dropped_baseline) > 0
            ):
                logger.warning(
                    "fixed_count_acquire: HDF plugin dropped %d frame(s)"
                    " during this run (hdf1.dropped_arrays: %s -> %s)",
                    int(dropped_now) - int(dropped_baseline),
                    dropped_baseline,
                    dropped_now,
                )

            print(
                f"fixed_count_acquire: {n_captured}/{num_images} frames in"
                f" {elapsed:.1f}s ({rate:.2f} fps overall) -> "
                f"{eiger2.hdf1.full_file_name.get(use_monitor=False, as_string=True)}"
            )

        yield from _acquire()
    finally:
        # stage_sigs dict contents don't un-mutate on their own (unlike
        # device *values*, which bps.unstage -- fired automatically by
        # bpp.stage_decorator above -- already restores).
        restore_stage_sigs(saved_stage_sigs)
        # Put file_name/file_path/file_template/file_number/auto_save/
        # auto_increment/compression/create_directory back to whatever
        # they were before this plan ran (the CacheParameters overrides
        # above, mirroring flyscan_3idc.flyscan's own original_cache).
        yield from original_cache.restore()
