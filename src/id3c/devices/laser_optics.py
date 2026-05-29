"""
Laser pickoff optics bundle (us / ds axes) with IN/OUT state.

The ``us`` and ``ds`` axes are upstream and downstream halves of a
retractable laser pickoff.  At nominal positions they are either
fully ``IN`` (in the beam path) or fully ``OUT`` (retracted).

This device declares its two axes as
:class:`~id3c.devices.interlocked_motor.InterlockedEpicsMotor`.  The
actual interlock callable (against ``sample_stage.omega``) is wired
late, in ``id3c.startup``.

Configuration Components (plain :class:`ophyd.Signal`, ``kind="config"``):

* ``in_position``   -- nominal IN location (mm), applied to both axes
* ``out_position``  -- nominal OUT location (mm), applied to both axes
* ``tolerance``     -- +/- window (mm) for IN/OUT comparison
* ``settle_time``   -- post-move delay (s) in ``move_in``/``move_out``

Derived Components (:class:`ophyd.signal.AttributeSignal`, ``kind="omitted"``):

* ``in_status``  -- mirrors :attr:`is_in`
* ``out_status`` -- mirrors :attr:`is_out`

These derived signals are subscribable, which is what the mid-motion
interlock watcher on ``sample_stage.omega`` uses.  Note however that
``AttributeSignal`` itself does not emit on EPICS updates; the
watcher should subscribe to the underlying ``us.user_readback`` and
``ds.user_readback`` signals (which it does, by wiring in
``startup.py``).  ``in_status`` / ``out_status`` are exposed for
manual ``.get()`` queries and for any code that just wants the
boolean.
"""

from __future__ import annotations

import logging

from bluesky import plan_stubs as bps
from ophyd import Component as Cpt
from ophyd import MotorBundle
from ophyd import Signal
from ophyd.signal import AttributeSignal

from .interlocked_motor import InterlockedEpicsMotor
from .interlocked_motor import MotionInterlock

logger = logging.getLogger(__name__)


class LaserOptics(MotorBundle):
    """Retractable laser pickoff with IN/OUT state and motion plans."""

    us = Cpt(InterlockedEpicsMotor, "m1")
    ds = Cpt(InterlockedEpicsMotor, "m2")

    # Tunable configuration.  Defaults match the beamline note in
    # devices.yml: IN = +75 mm, OUT = -75 mm, tolerance = +/- 1 mm.
    in_position = Cpt(Signal, value=75.0, kind="config")
    out_position = Cpt(Signal, value=-75.0, kind="config")
    tolerance = Cpt(Signal, value=1.0, kind="config")
    settle_time = Cpt(Signal, value=0.0, kind="config")

    # Derived state, exposed as signals so other code can subscribe
    # or .get() without poking at properties.
    in_status = Cpt(AttributeSignal, attr="is_in", kind="omitted")
    out_status = Cpt(AttributeSignal, attr="is_out", kind="omitted")

    # ------------------------------------------------------------------
    # Property logic

    def _within(self, axis, reference: Signal) -> bool:
        """True if ``axis.user_readback`` is within tolerance of ``reference``."""
        return abs(axis.user_readback.get() - reference.get()) <= self.tolerance.get()

    @property
    def is_in(self) -> bool:
        """True iff both axes are within tolerance of ``in_position``."""
        ds_in = self._within(self.ds, self.in_position)
        us_in = self._within(self.us, self.in_position)
        return us_in and ds_in

    @property
    def is_out(self) -> bool:
        """True iff both axes are within tolerance of ``out_position``."""
        ds_out = self._within(self.ds, self.out_position)
        us_out = self._within(self.us, self.out_position)
        return us_out and ds_out

    # ------------------------------------------------------------------
    # Plan methods (use as ``yield from laser_optics.move_out()``)

    def move_out(self):
        """Move both axes to ``out_position`` and verify."""
        target = self.out_position.get()
        yield from bps.mv(self.us, target, self.ds, target)
        settle = self.settle_time.get()
        if settle > 0:
            yield from bps.sleep(settle)
        if not self.is_out:
            raise MotionInterlock(
                f"{self.name}.move_out: axes did not reach OUT "
                f"({target} +/- {self.tolerance.get()} mm). "
                f"us={self.us.user_readback.get()}, "
                f"ds={self.ds.user_readback.get()}."
            )

    def move_in(self):
        """Move both axes to ``in_position`` and verify."""
        target = self.in_position.get()
        yield from bps.mv(self.us, target, self.ds, target)
        settle = self.settle_time.get()
        if settle > 0:
            yield from bps.sleep(settle)
        if not self.is_in:
            raise MotionInterlock(
                f"{self.name}.move_in: axes did not reach IN "
                f"({target} +/- {self.tolerance.get()} mm). "
                f"us={self.us.user_readback.get()}, "
                f"ds={self.ds.user_readback.get()}."
            )
