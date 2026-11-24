"""``diode_fly``: fly axes while counting the diode and ion chamber on a scaler.

``diode_fly`` is an ordinary Bluesky plan.  Its return value (what ``yield from``
gives you) is ``(diode_values, ic_values, motor_values)``, three lists of equal
length where entry ``i`` belongs to the same point:

* ``diode_values`` -- diode scaler channel (3-ID-A ``scaler1.S3``, channel 3)
* ``ic_values``    -- ion-chamber channel (``scaler1.S2``, channel 2), which normalizes
  the diode
* ``motor_values`` -- axis position of the point: a float for one axis, a tuple of
  floats (one per axis, in ``axes`` order) for several

A point is one scaler count of ``t_count`` seconds; both channels come from the same
count.  Its position is the mean of the axis readback just before and just after the
count, i.e. the position at the middle of the count.  Points outside every axis'
``[start, end]`` (run-up / run-out) are dropped.

No Bluesky *run* is opened (no ``open_run``), so the RunEngine emits no documents:
nothing reaches Tiled/databroker, the NeXus writer or any other subscriber, and no
``scan_id`` is used.  Motors and scaler are still driven through the RunEngine, and
all hardware reads and writes are messages (``bps.rd``/``mv``/``trigger``), never a
direct
``.get()``/``.put()`` (only cached metadata such as ``limits`` is read directly).

Devices are looked up by name in the instrument registry (``oregistry``, as in the other
``id3c.user`` plans), or passed as objects.  Defaults: scaler ``"scaler_b"``
(``3ida:scaler1``), motors by registry name such as ``"sample_stage.xprime"``.

Run it through the RunEngine like any other plan -- ``RE(diode_fly(...))`` -- but note
that ``RE(...)`` returns only run uids (there is no run here), so to *get the lists* use
:func:`run_diode_flyscan`, or ``yield from diode_fly(...)`` inside your own plan (so
interlock moves etc. simply come before it)::

    from id3c.startup import RE
    from id3c.user.autofocus.flyscan_diode import FlyAxis, run_diode_flyscan

    diode, ic, pos = run_diode_flyscan(
        RE, [FlyAxis("sample_stage.xprime", -0.5, 0.5)],
        n_points=50, t_count=0.1, taxi_allowance=0.05)
"""

import logging
import time
from dataclasses import dataclass
from typing import Any
from typing import Optional

from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from bluesky.utils import plan

from ._registry import find_device

logger = logging.getLogger(__name__)

__all__ = ["FlyAxis", "diode_fly", "run_diode_flyscan"]

# --- Axes: what moves, and how fast ---------------------------------------------------
# An axis is an EpicsMotor or anything shaped like one: settable, with
# ``user_readback``, ``velocity`` and ``acceleration`` (seconds to reach ``velocity``)
# signals, and optionally ``limits``.  All axes of one fly cross their own start -> end
# span in the same time, ``n_points * t_count``, so a 2-axis fly is a straight diagonal.
# The helpers below are pure arithmetic on values the plan has already read; they never
# touch hardware.


@dataclass
class FlyAxis:
    """One axis to fly: move ``device`` from ``start`` to ``end``.

    ``device`` is an ``EpicsMotor`` or its registry name (e.g.
    ``"sample_stage.xprime"``).  ``end < start`` flies in the negative direction.
    ``name`` defaults to ``device.name`` and must be unique among the axes of one fly.
    """

    device: Any
    start: float
    end: float
    name: Optional[str] = None

    def __post_init__(self):
        """Resolve a registry name and check the device looks like a motor."""
        self.device = find_device(self.device)
        for attr in ("user_readback", "velocity", "acceleration"):
            if not hasattr(self.device, attr):
                raise TypeError(
                    f"{self.device!r} is not a motor (no .{attr}); cannot fly it"
                )
        if self.name is None:
            self.name = self.device.name
        if self.start == self.end:
            raise ValueError(f"axis {self.name!r}: start == end ({self.start:g})")


@dataclass(frozen=True)
class AxisGeometry:
    """Motion of one axis (engineering units, seconds)."""

    start: float
    end: float
    direction: int  # +1 or -1
    velocity: float  # always positive
    accl: float  # seconds to reach velocity
    p_initial: float  # run-up position, before ``start``
    p_final: float  # run-out position, after ``end``

    @property
    def window(self):
        """``(low, high)`` of the span whose points are kept."""
        return (min(self.start, self.end), max(self.start, self.end))

    @property
    def travel_time(self):
        """Time to go ``p_initial -> p_final`` (used for a generous timeout)."""
        return abs(self.p_final - self.p_initial) / self.velocity + 2 * self.accl


def _compute_axis_geometry(axis, duration, accl, taxi_allowance=0.0):
    """Velocity and run-up/run-out positions for ``axis`` to cross start->end in
    ``duration``.

    ``velocity = |end - start| / duration``.  Reaching it takes ``0.5 * velocity *
    accl`` of distance; the axis is parked that far (plus ``taxi_allowance``) before
    ``start`` and runs out the same distance beyond ``end``.
    """
    if duration <= 0:
        raise ValueError(f"duration={duration:g} must be positive")
    if taxi_allowance < 0:
        raise ValueError(f"taxi_allowance={taxi_allowance:g} must be non-negative")
    if accl < 0:
        raise ValueError(
            f"axis {axis.name!r}: acceleration time {accl:g} s is negative"
        )
    direction = 1 if axis.end > axis.start else -1
    velocity = abs(axis.end - axis.start) / duration
    margin = 0.5 * velocity * accl + taxi_allowance
    return AxisGeometry(
        start=axis.start,
        end=axis.end,
        direction=direction,
        velocity=velocity,
        accl=float(accl),
        p_initial=axis.start - direction * margin,
        p_final=axis.end + direction * margin,
    )


def _check_axis_limits(axis, geom):
    """Raise ValueError if the run-up/run-out range or the velocity is unreachable.

    Soft limits come from ``device.limits`` ((0, 0) means "no limits", as in the motor
    record); the velocity ceiling from ``device.velocity.limits`` (the PV's control
    limits) when the signal provides them.
    """
    limits = getattr(axis.device, "limits", None)
    if limits is not None and tuple(limits) != (0, 0):
        low, high = limits
        for label, p in (("run-up", geom.p_initial), ("run-out", geom.p_final)):
            if not low <= p <= high:
                raise ValueError(
                    f"axis {axis.name!r}: {label} position {p:g} is outside limits"
                    f" [{low:g}, {high:g}]; shrink the range or taxi_allowance."
                )
    vlimits = getattr(axis.device.velocity, "limits", None)
    vmax = vlimits[1] if vlimits else None
    if vmax and geom.velocity > vmax:
        raise ValueError(
            f"axis {axis.name!r}: scan velocity {geom.velocity:g} exceeds"
            f" VMAX {vmax:g}; use fewer points per span or a longer t_count."
        )


# --- The plan -------------------------------------------------------------------


@plan
def diode_fly(
    axes,
    *,
    n_points: int,
    t_count: float,
    taxi_allowance: float = 0.0,
    scaler="scaler_b",
    diode=None,
    ion_chamber=None,
    return_to_start: bool = False,
):
    """Fly ``axes`` while counting the diode and ion chamber on ``scaler``.

    Parameters
    ----------
    axes : sequence of FlyAxis
        Axes to fly together (at least one).  Each crosses ``start -> end`` in
        ``n_points * t_count``.
    n_points : int
        Nominal number of points inside the span.  Every count also costs a little
        trigger overhead, so slightly fewer points than this land inside
        ``[start, end]``.
    t_count : float [s]
        Counting time per point (the scaler preset time).
    taxi_allowance : float
        Extra run-up/run-out distance (axis units) beyond the acceleration distance.
    scaler : ophyd ``ScalerCH`` or its registry name
        Default ``"scaler_b"`` (``3ida:scaler1``).  Needs ``trigger()``,
        ``preset_time``, ``count_mode``, ``channels.chan<N>.s``.
    diode, ion_chamber : readable signal, optional
        Which scaler channels to record, passed as devices like the detectors/motors of
        ``bluesky.plans``, e.g. ``scaler.channels.chan04.s``.  They must be channels of
        ``scaler`` (it is what is triggered).  Defaults: ``scaler.channels.chan03.s``
        (diode, ``3ida:scaler1.S3``) and ``scaler.channels.chan02.s`` (ion chamber,
        ``3ida:scaler1.S2``).

    return_to_start : bool
        If True, afterwards move every flown axis back to where it was before the fly
        (at its normal velocity), also after an abort.  Default False: axes are left at
        their run-out position.  Anything else you moved beforehand is not touched.

    Returns
    -------
    ``(diode_values, ic_values, motor_values)`` -- see the module docstring.
    """
    axes = list(axes)
    if not axes:
        raise ValueError("diode_fly needs at least one axis to fly")
    if not all(isinstance(ax, FlyAxis) for ax in axes):
        raise TypeError(
            "axes must be FlyAxis objects, e.g. FlyAxis('sample_stage.xprime', 0, 1)"
        )
    scaler = find_device(scaler)
    if n_points < 1:
        raise ValueError(f"n_points={n_points} must be >= 1")
    if t_count <= 0:
        raise ValueError(f"t_count={t_count:g} must be positive")
    names = [ax.name for ax in axes]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate axis names: {names}")

    # Plan everything (and fail early) before anything moves.
    geoms = []
    for ax in axes:
        accl = yield from bps.rd(ax.device.acceleration)
        g = _compute_axis_geometry(ax, n_points * t_count, accl, taxi_allowance)
        _check_axis_limits(ax, g)
        geoms.append(g)
    timeout = max(g.travel_time for g in geoms) + 30.0
    windows = [g.window for g in geoms]

    diode_sig = diode if diode is not None else scaler.channels.chan03.s
    ic_sig = ion_chamber if ion_chamber is not None else scaler.channels.chan02.s
    for label, sig in (("diode", diode_sig), ("ion_chamber", ic_sig)):
        # only the triggered scaler updates its channels
        if getattr(sig, "root", None) is not scaler:
            raise ValueError(
                f"{label} {sig!r} is not a channel of scaler {scaler.name!r}"
            )

    # Settings the fly changes; put back afterwards, even on abort.
    restore = [scaler.preset_time, (yield from bps.rd(scaler.preset_time))]
    restore += [scaler.count_mode, (yield from bps.rd(scaler.count_mode))]
    for ax in axes:
        restore += [ax.device.velocity, (yield from bps.rd(ax.device.velocity))]

    start_positions = []
    if return_to_start:
        for ax in axes:
            start_positions.append((yield from bps.rd(ax.device.user_readback)))

    diode_values, ic_values, motor_values = [], [], []
    statuses = []  # the axes' fly moves, so cleanup knows which are still running

    def count_one_point():
        before = []
        for ax in axes:
            before.append((yield from bps.rd(ax.device.user_readback)))
        yield from bps.trigger(scaler, wait=True)
        after = []
        for ax in axes:
            after.append((yield from bps.rd(ax.device.user_readback)))
        pos = [0.5 * (b + a) for b, a in zip(before, after, strict=True)]
        if all(lo <= p <= hi for p, (lo, hi) in zip(pos, windows, strict=True)):
            diode_values.append(float((yield from bps.rd(diode_sig))))
            ic_values.append(float((yield from bps.rd(ic_sig))))
            motor_values.append(pos[0] if len(pos) == 1 else tuple(pos))

    def fly():
        # Taxi to the run-up positions at the normal speed, then set the scan speed.
        yield from bps.mv(
            *[
                x
                for ax, g in zip(axes, geoms, strict=True)
                for x in (ax.device, g.p_initial)
            ]
        )
        yield from bps.mv(
            *[
                x
                for ax, g in zip(axes, geoms, strict=True)
                for x in (ax.device.velocity, g.velocity)
            ],
            scaler.count_mode,
            "OneShot",
            scaler.preset_time,
            t_count,
        )
        # Start all axes toward their run-out positions, then count until they arrive.
        for ax, g in zip(axes, geoms, strict=True):
            statuses.append((yield from bps.abs_set(ax.device, g.p_final, group="fly")))
        deadline = time.monotonic() + timeout
        while not all(st.done for st in statuses):
            if time.monotonic() > deadline:
                raise TimeoutError(f"axes still moving after {timeout:g} s")
            yield from count_one_point()
        yield from bps.wait("fly")  # raises if a move failed

    def cleanup():
        # statuses may be shorter than axes (a failure before every axis was launched)
        for ax, st in zip(axes, statuses):  # noqa: B905
            if not st.done:
                yield from bps.stop(ax.device)
        yield from bps.mv(*restore)  # includes the normal velocity
        if return_to_start:
            yield from bps.mv(
                *[
                    x
                    for ax, p in zip(axes, start_positions, strict=True)
                    for x in (ax.device, p)
                ]
            )

    yield from bpp.finalize_wrapper(fly(), cleanup())

    if not diode_values:
        logger.warning("diode_fly: no point fell inside the span; nothing recorded")
    logger.info("diode_fly: kept %d point(s) (%d nominal)", len(diode_values), n_points)
    return diode_values, ic_values, motor_values


def run_diode_flyscan(RE, axes, **plan_kwargs):
    """Run :func:`diode_fly` with ``RE``; return the lists it returns.

    Returns ``(diode_values, ic_values, motor_values)``.  Convenience for blocking
    code: ``RE(plan)`` only returns run uids, and ``RE.call_returns_result`` (the
    alternative) can only be chosen when the RunEngine is created, so the plan's
    return value is captured here.  Raises ``RuntimeError`` if the plan did not finish.

    A pause (Ctrl-C) leaves the RunEngine paused with the axes still moving; a
    half-flown scan is not worth resuming, so call ``RE.abort()``: it stops the axes
    and restores the motor velocity, scaler preset time and count mode.
    """
    captured = []

    def run():
        captured.append((yield from diode_fly(axes, **plan_kwargs)))

    RE(run())
    if not captured:
        raise RuntimeError("diode_fly did not finish (RunEngine stopped or aborted it)")
    return captured[0]
