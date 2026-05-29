"""
Set up the bidirectional sample_stage.omega <-> laser_optics interlock.

This module is specific to the one interlocked pair listed below.
Additional interlocks belong in their own ``<pair>_interlock.py``
modules with their own ``setup_<pair>_interlock`` entry points.

Run once at startup, after ``make_devices()`` has populated the ophyd
registry, e.g.::

    from .devices.omega_laser_interlock import setup_omega_laser_interlock
    setup_omega_laser_interlock(oregistry)

Idempotent: safe to call again after re-loading devices.

Interlock relationships installed
---------------------------------

* ``sample_stage.omega`` is blocked unless ``laser_optics.is_out`` is
  True.  Mid-motion: subscribed to ``laser_optics.us.user_readback``
  and ``laser_optics.ds.user_readback``; an excursion that takes the
  optics out of the OUT window will stop omega and fail the move with
  :class:`~id3c.devices.interlocked_motor.MotionInterlock`.

* ``laser_optics.us`` and ``laser_optics.ds`` are blocked whenever
  ``sample_stage.omega`` is moving.  Mid-motion: subscribed to
  ``omega.motor_is_moving`` (the .MOVN field), so a laser-axis move
  in progress will be aborted if an omega motion starts.  This is
  the conservative, position-free choice; angular danger-zone gating
  is appropriate for an EPICS-IOC interlock, not this Python layer.

Either device missing from the registry is logged and skipped; the
other side's wiring is still attempted.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def setup_omega_laser_interlock(oregistry) -> None:
    """Install laser_optics <-> sample_stage.omega interlocks."""
    laser = oregistry.get("laser_optics", None) if hasattr(oregistry, "get") else None
    if laser is None:
        try:
            laser = oregistry["laser_optics"]
        except Exception:
            laser = None
    stage = oregistry.get("sample_stage", None) if hasattr(oregistry, "get") else None
    if stage is None:
        try:
            stage = oregistry["sample_stage"]
        except Exception:
            stage = None

    if laser is None or stage is None:
        logger.warning(
            "setup_omega_laser_interlock: missing device(s); "
            "laser_optics=%r sample_stage=%r. Interlock NOT installed.",
            laser,
            stage,
        )
        return

    omega = getattr(stage, "omega", None)
    if omega is None:
        logger.warning(
            "setup_omega_laser_interlock: sample_stage has no 'omega' attribute. "
            "Interlock NOT installed."
        )
        return

    # omega blocked unless laser is OUT.
    omega.interlock = lambda: laser.is_out
    omega.interlock_description = "laser_optics OUT"
    omega.interlock_watch = (
        laser.us.user_readback,
        laser.ds.user_readback,
    )

    # laser axes blocked while omega is moving.  See module docstring
    # for the rationale (conservative, position-free).
    def _laser_permit():
        return not bool(omega.motor_is_moving.get())

    for axis in (laser.us, laser.ds):
        axis.interlock = _laser_permit
        axis.interlock_description = "sample_stage.omega stationary"
        axis.interlock_watch = (omega.motor_is_moving,)

    logger.info(
        "setup_omega_laser_interlock: installed omega<->laser_optics interlocks "
        "(omega blocked unless laser OUT; laser axes blocked while omega moves)."
    )
