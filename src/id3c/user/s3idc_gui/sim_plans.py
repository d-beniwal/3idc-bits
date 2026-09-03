"""GUI-facing wrappers for the ophyd simulator plans.

Thin ``@plan`` wrappers around :mod:`id3c.plans.sim_plans`.  The library
module is maintained upstream and its docstrings do not carry the
``Parameters`` grammar the plan-runner GUIs parse, so the forms are
described here instead.  Each wrapper only documents and delegates -- the
simulator logic itself has exactly one home, in the library module.

For development and testing only: these drive ophyd's simulated detector
and motor, never real hardware, which makes them the safe way to exercise
a GUI end-to-end without beam.
"""

from bluesky.utils import plan

from id3c.plans.sim_plans import sim_count_plan as _sim_count_plan
from id3c.plans.sim_plans import sim_print_plan as _sim_print_plan
from id3c.plans.sim_plans import sim_rel_scan_plan as _sim_rel_scan_plan

__all__ = ["sim_count_plan", "sim_print_plan", "sim_rel_scan_plan"]


@plan
def sim_count_plan(num: int = 1, imax: float = 10_000):
    """Count the simulated detector a number of times.

    Wraps :func:`id3c.plans.sim_plans.sim_count_plan`.  Sets the simulated
    detector's peak intensity, then runs ``count()`` -- one Bluesky run
    with ``num`` readings, recorded to the catalog like any real scan.

    Parameters
    ----------
    num : int
        Readings :: How many times to read the simulated detector (one
        event per reading, all in a single run).
    imax : float [counts]
        Peak intensity :: Maximum value the simulated detector reports at
        the centre of its peak.

    Example::

        RE(sim_count_plan(num=5))
        RE(sim_count_plan(num=20, imax=5000))
    """
    yield from _sim_count_plan(num=num, imax=imax)


@plan
def sim_print_plan():
    """Print the simulated motor position and detector reading.

    Wraps :func:`id3c.plans.sim_plans.sim_print_plan`.  Takes no
    arguments and opens no run -- nothing is written to the catalog.  A
    zero-risk smoke test that the kernel, the RunEngine, and the device
    registry are all live.

    Example::

        RE(sim_print_plan())
    """
    yield from _sim_print_plan()


@plan
def sim_rel_scan_plan(
    span: float = 5,
    num: int = 11,
    imax: float = 10_000,
    center: float = 0,
    sigma: float = 1,
    noise: str = "uniform",
):
    """Scan the simulated motor through a peak, relative to its position.

    Wraps :func:`id3c.plans.sim_plans.sim_rel_scan_plan`.  Configures the
    simulated detector's peak shape, then runs ``rel_scan()`` over
    ``[-span/2, +span/2]`` around the motor's current position.

    Parameters
    ----------
    span : float [egu]
        Scan span :: Total width scanned, centred on the motor's current
        position (it moves from ``-span/2`` to ``+span/2``).
    num : int
        Points :: Number of steps across the span.
    imax : float [counts]
        Peak intensity :: Maximum value at the centre of the simulated
        peak.
    center : float [egu]
        Peak centre :: Motor position of the simulated peak's centre.
    sigma : float [egu]
        Peak sigma :: Gaussian width of the simulated peak.
    noise : choice{none, poisson, uniform}
        Noise :: Noise model added to the simulated detector signal.

    Example::

        RE(sim_rel_scan_plan(span=10, num=41))
        RE(sim_rel_scan_plan(span=4, num=21, sigma=0.5, noise="poisson"))
    """
    yield from _sim_rel_scan_plan(
        span=span,
        num=num,
        imax=imax,
        center=center,
        sigma=sigma,
        noise=noise,
    )
