"""
EpicsMotor with a Bluesky-session interlock.

Provides:

* :class:`MotionInterlock` -- exception raised when a move is blocked.
* :class:`InterlockedEpicsMotor` -- ``ophyd.EpicsMotor`` subclass that
  consults a caller-supplied ``interlock()`` callable both *before*
  starting a motion (pre-flight) and *during* the motion (mid-flight,
  via subscriptions on caller-supplied signals).

Scope and limitations
---------------------

This interlock lives entirely in the running Bluesky/Python session.
It does **not** write to any EPICS protection field (no ``DISP``,
no ``SPMG`` Stop, no sequencer record).  If this Python process
crashes, exits, or is bypassed (MEDM jog, ``caput``, a different
Bluesky session, SPEC, etc.), the underlying EPICS motor is
unaffected by anything in this module.

For session-independent, hardware-grade protection (e.g. preventing
collisions regardless of which client commands the move), implement
the interlock in the IOC: a CALC/SCALC record, a state-notation
sequencer, or a soft record driving the motor's ``DISP`` field.

Wiring pattern
--------------

``InterlockedEpicsMotor`` does not know about any particular other
device.  The interlock condition is supplied late, typically in
``startup.py`` after all devices have been created, e.g.::

    omega = sample_stage.omega
    omega.interlock = lambda: laser_optics.is_out
    omega.interlock_description = "laser_optics OUT"
    omega.interlock_watch = (
        laser_optics.us.user_readback,
        laser_optics.ds.user_readback,
    )

This keeps the class reusable and avoids import cycles between
mutually-interlocked devices.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from ophyd import EpicsMotor

logger = logging.getLogger(__name__)


class MotionInterlock(RuntimeError):
    """Raised when an :class:`InterlockedEpicsMotor` move is blocked.

    The exception message is intended to be self-diagnostic so that the
    final line of a (typically long) Bluesky traceback identifies both
    the affected motor and the interlock that blocked it.
    """


class InterlockedEpicsMotor(EpicsMotor):
    """EpicsMotor that consults a callable interlock before and during moves.

    Parameters
    ----------
    interlock_description : str, optional
        Short human-readable description of the interlock condition,
        used in :class:`MotionInterlock` messages.  May be supplied via
        YAML (it is popped from kwargs before ``super().__init__``).

    Notes
    -----
    The ``interlock`` callable and ``interlock_watch`` signals are
    assigned as plain attributes (not Components) and are expected to
    be wired *after* construction; see the module docstring.

    If ``interlock`` is ``None`` (the default), this class behaves
    identically to a plain ``EpicsMotor``.
    """

    def __init__(self, *args, interlock_description: str = "", **kwargs):
        # Pop custom kwargs before delegating to EpicsMotor, which does
        # not tolerate unknown kwargs.
        self.interlock_description: str = interlock_description
        self.interlock: Optional[Callable[[], bool]] = None
        self.interlock_watch: Iterable = ()
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    # Internals

    def _interlock_diagnostic(self) -> str:
        """Build the message body for a :class:`MotionInterlock`."""
        desc = self.interlock_description or "(unspecified)"
        return (
            f"{self.name}.move blocked by interlock {desc!r}. "
            "See the device(s) referenced by this interlock for state."
        )

    def _install_mid_motion_watch(self, status) -> None:
        """Subscribe to ``interlock_watch`` signals for the duration of ``status``.

        On any update, re-evaluate ``self.interlock()``.  If it returns
        False while the move is still in flight, stop the motor and
        fail the status with :class:`MotionInterlock`.
        """
        if not self.interlock_watch or self.interlock is None:
            return

        cids: list[tuple] = []  # (signal, cid)

        def _on_change(*args, **kwargs):
            # Guard against duplicate fires after stop().
            if status.done:
                return
            try:
                permitted = bool(self.interlock())
            except Exception:
                logger.exception(
                    "%s: interlock callable raised; treating as blocked.",
                    self.name,
                )
                permitted = False
            if permitted:
                return
            logger.warning(
                "%s: mid-motion interlock trip; stopping motor.", self.name
            )
            try:
                self.stop(success=False)
            except Exception:
                logger.exception("%s: stop() raised during interlock trip.", self.name)
            # set_exception is a no-op if the status is already finished.
            try:
                status.set_exception(MotionInterlock(self._interlock_diagnostic()))
            except Exception:
                logger.exception(
                    "%s: set_exception raised during interlock trip.", self.name
                )

        for sig in self.interlock_watch:
            try:
                cid = sig.subscribe(_on_change)
                cids.append((sig, cid))
            except Exception:
                logger.exception(
                    "%s: failed to subscribe interlock watch on %r.",
                    self.name,
                    sig,
                )

        def _cleanup(*args, **kwargs):
            for sig, cid in cids:
                try:
                    sig.unsubscribe(cid)
                except Exception:
                    logger.exception(
                        "%s: failed to unsubscribe interlock watch from %r.",
                        self.name,
                        sig,
                    )

        status.add_callback(_cleanup)

    # ------------------------------------------------------------------
    # Public API

    def move(self, position, wait=True, **kwargs):
        """Pre-flight interlock check, then EpicsMotor.move.

        Raises
        ------
        MotionInterlock
            If ``self.interlock`` is wired and returns ``False`` at
            the time of the call.  No EPICS write is performed in
            that case.
        """
        if self.interlock is not None:
            try:
                permitted = bool(self.interlock())
            except Exception as exc:
                # Fail closed: an interlock that cannot be evaluated
                # must not silently permit motion.
                raise MotionInterlock(
                    f"{self.name}.move blocked: interlock evaluation raised "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if not permitted:
                raise MotionInterlock(self._interlock_diagnostic())

        status = super().move(position, wait=False, **kwargs)
        self._install_mid_motion_watch(status)

        if wait:
            status.wait()
        return status
