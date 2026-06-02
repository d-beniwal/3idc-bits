"""Unit tests for :mod:`id3c.plans.ad_plugins`.

These tests use an ophyd ``Device`` with in-memory ``Signal``
components as a stand-in for an area-detector file plugin.  No
EPICS PVs are required, so the tests run on the off-network
development host.
"""

import io
from contextlib import redirect_stdout

import pytest
from ophyd import Component as Cpt
from ophyd import Device
from ophyd import Signal

from id3c.plans.ad_plugins import _select_ad_plugin_keys
from id3c.plans.ad_plugins import read_ad_plugin_components
from id3c.plans.ad_plugins import set_ad_plugin_components


class _ReadOnlySignal(Signal):
    """A Signal that advertises itself as not writable."""

    @property
    def write_access(self):
        """Always False; mimics ``EpicsSignalRO``."""
        return False


class _FakePlugin(Device):
    """Minimal stand-in for an area-detector file plugin.

    Mix of kinds and access modes so the helpers' selection rules
    can be exercised.
    """

    cfg_a = Cpt(Signal, value=10, kind="config")
    cfg_b = Cpt(Signal, value=20, kind="config")
    normal_c = Cpt(Signal, value=30, kind="normal")
    hinted_d = Cpt(Signal, value=40, kind="hinted")
    omitted_e = Cpt(Signal, value=50, kind="omitted")
    ro_f = Cpt(_ReadOnlySignal, value=60, kind="config")


@pytest.fixture
def plugin():
    """Return a freshly constructed fake plugin."""
    return _FakePlugin(name="plugin")


# --- _select_ad_plugin_keys ------------------------------------------------


def test_select_skips_omitted(plugin):
    """``omitted`` kind components are not yielded."""
    keys = list(_select_ad_plugin_keys(plugin))
    assert "omitted_e" not in keys


def test_select_includes_config_normal_hinted_readonly(plugin):
    """All other reportable kinds (including read-only) are yielded."""
    keys = list(_select_ad_plugin_keys(plugin))
    assert set(keys) == {"cfg_a", "cfg_b", "normal_c", "hinted_d", "ro_f"}


def test_select_is_sorted(plugin):
    """Selection order is alphabetical for predictability."""
    keys = list(_select_ad_plugin_keys(plugin))
    assert keys == sorted(keys)


# --- read_ad_plugin_components --------------------------------------------


def test_read_prints_table_with_expected_rows(plugin):
    """The diagnostic prints one row per reportable component."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        read_ad_plugin_components(plugin)
    out = buf.getvalue()
    # One row per reportable signal; omitted should be absent.
    for name in ("cfg_a", "cfg_b", "normal_c", "hinted_d", "ro_f"):
        assert name in out
    assert "omitted_e" not in out


def test_read_marks_readonly_access(plugin):
    """Read-only component shows ``R-``; read-write shows ``RW``."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        read_ad_plugin_components(plugin)
    out = buf.getvalue()
    # The ro_f row contains 'R-'; the cfg_a row contains 'RW'.
    ro_line = next(line for line in out.splitlines() if "ro_f" in line)
    rw_line = next(line for line in out.splitlines() if "cfg_a" in line)
    assert "R-" in ro_line
    assert "RW" in rw_line


# --- set_ad_plugin_components ---------------------------------------------


def _msgs(plan_stub):
    """Materialize a plan stub's yielded Msg sequence."""
    return list(plan_stub)


def test_set_emits_one_mv_per_call(plugin):
    """Multiple kwargs collapse to a single ``bps.mv`` invocation."""
    msgs = _msgs(set_ad_plugin_components(plugin, cfg_a=1, cfg_b=2))
    set_msgs = [m for m in msgs if m.command == "set"]
    # bps.mv issues one set per signal plus a wait; both signals appear.
    set_targets = {m.obj.attr_name for m in set_msgs}
    assert set_targets == {"cfg_a", "cfg_b"}


def test_set_values_propagate_through_messages(plugin):
    """Each setpoint appears in the corresponding ``Msg.args``."""
    msgs = _msgs(set_ad_plugin_components(plugin, cfg_a=99))
    set_msgs = [m for m in msgs if m.command == "set"]
    assert len(set_msgs) == 1
    assert set_msgs[0].args == (99,)


def test_set_no_kwargs_yields_null_message(plugin):
    """An empty call still yields one message (so @plan does not warn)."""
    msgs = _msgs(set_ad_plugin_components(plugin))
    assert len(msgs) == 1
    assert msgs[0].command == "null"


def test_set_rejects_unknown_kwarg(plugin):
    """Unknown kwargs raise ``KeyError`` before any message is yielded."""
    with pytest.raises(KeyError, match="bogus"):
        _msgs(set_ad_plugin_components(plugin, bogus=1))


def test_set_rejects_omitted_kwarg(plugin):
    """A kwarg naming an ``omitted``-kind component is unknown."""
    with pytest.raises(KeyError, match="omitted_e"):
        _msgs(set_ad_plugin_components(plugin, omitted_e=1))


def test_set_rejects_readonly_kwarg(plugin):
    """A kwarg naming a read-only component raises ``TypeError``."""
    with pytest.raises(TypeError, match="ro_f"):
        _msgs(set_ad_plugin_components(plugin, ro_f=1))


def test_set_reports_readonly_before_unknown(plugin):
    """Both errors at once: ``TypeError`` (read-only) wins."""
    with pytest.raises(TypeError, match="ro_f"):
        _msgs(set_ad_plugin_components(plugin, ro_f=1, bogus=2))
