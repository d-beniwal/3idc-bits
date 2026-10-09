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

from apsbits.core.instrument_init import oregistry
from bluesky import plan_stubs as bps  # noqa: F401
from bluesky.utils import plan


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
