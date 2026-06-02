"""Support for area detector file plugins (HDF5, TIFF, ...).

.. autosummary::
    ~read_ad_plugin_components
    ~set_ad_plugin_components

The set of components visible to either helper is determined by each
component's ``kind`` (``hinted``, ``normal``, or ``config``).  To
expose or hide a component, adjust its ``kind`` attribute (for
example ``eiger2.hdf1.num_capture.kind = "config"``).

The set helper is intended for plugin configuration *before*
acquisition -- an alternative to ophyd's staging process.  Actionable
signals (``capture``, ``acquire``, ...) are normally driven by
staging and should not be set through this helper.  Order of the
``bps.mv`` calls inside the set helper is undefined; if ordering
matters, make separate calls.
"""

from bluesky import plan_stubs as bps
from bluesky.utils import plan
from ophyd import Kind
from ophyd import Signal
from pyRestTable import Table

REPORTABLE_KINDS = Kind.hinted | Kind.normal | Kind.config
_NO_VALUE = object()  # sentinel distinct from any value the IOC might return


def _select_ad_plugin_keys(plugin):
    """Yield component names that are Signals with a reportable kind."""
    # ``Kind.omitted`` is ``0`` and ``IntFlag.__contains__`` treats ``0`` as
    # a subset of any mask, so the kind check uses bitwise intersection
    # instead of ``in``.
    for key in sorted(plugin.component_names):
        obj = getattr(plugin, key)
        if isinstance(obj, Signal) and bool(obj.kind & REPORTABLE_KINDS):
            yield key


def read_ad_plugin_components(plugin):
    """Print a table of plugin components (access, name, current value).

    Diagnostic only.  Use this interactively to discover which keys
    can be passed to :func:`set_ad_plugin_components`.  Components
    whose ``.read()`` returns no ``value`` field are skipped.
    """
    table = Table()
    table.labels = ["Access", "Component", "Value"]
    for key in _select_ad_plugin_keys(plugin):
        obj = getattr(plugin, key)
        access = "R" if obj.read_access else "-"
        access += "W" if obj.write_access else "-"
        reading = obj.read()
        first = next(iter(reading.values()), {})
        value = first.get("value", _NO_VALUE)
        if value is _NO_VALUE:
            continue
        table.addRow([access, key, value])
    if len(table.rows):
        print(table)


@plan
def set_ad_plugin_components(plugin, /, **kwargs):
    """Set plugin components by keyword.

    Each keyword names a component on ``plugin``; the value is the
    new setpoint.  All writes are issued through a single
    ``bps.mv(...)`` and complete in parallel; order is undefined.

    Raises:
        TypeError: any kwarg names a component that is not writable.
        KeyError:  any kwarg names a component the helper does not
                   see (unknown name, or its ``kind`` is
                   ``omitted``).
    """
    known = set(_select_ad_plugin_keys(plugin))
    unknown = sorted(set(kwargs) - known)
    ro_keys = sorted(
        key for key in kwargs if key in known and not getattr(plugin, key).write_access
    )
    if ro_keys:
        raise TypeError(f"Cannot set read-only components: {', '.join(ro_keys)}")
    if unknown:
        raise KeyError(f"Unknown plugin components: {', '.join(unknown)}")
    movables = []
    for key in kwargs:
        movables += [getattr(plugin, key), kwargs[key]]
    if movables:
        yield from bps.mv(*movables)
    else:
        # No kwargs: still emit one message so the @plan wrapper does
        # not raise RuntimeWarning at garbage-collection time.
        yield from bps.null()
