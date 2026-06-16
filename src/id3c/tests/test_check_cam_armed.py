"""Tests for ``_check_cam_armed``.

Mocks the cam via SimpleNamespace.  The function is a plan stub
(yields ``bps.sleep(...)`` messages), so we exhaust the generator
manually and assert behaviour.
"""

from types import SimpleNamespace

import pytest

from id3c.plans.flyscan_3idc import _CAM_ERROR_STATES
from id3c.plans.flyscan_3idc import _check_cam_armed


def _make_det(state_sequence, message="some IOC message"):
    """Build a duck-typed det whose ``cam.detector_state`` returns
    successive values from ``state_sequence`` on each .get() call.
    """
    state_iter = iter(state_sequence)
    last = [state_sequence[-1] if state_sequence else ""]

    def state_get(use_monitor=False, as_string=False):
        try:
            v = next(state_iter)
        except StopIteration:
            v = last[0]
        return v

    def msg_get(use_monitor=False, as_string=False):
        return message

    cam = SimpleNamespace(
        detector_state=SimpleNamespace(get=state_get),
        status_message=SimpleNamespace(get=msg_get),
    )
    return SimpleNamespace(cam=cam, name="testdet")


def _drain(plan, max_msgs=10_000):
    """Exhaust a plan generator, actually sleeping on each bps.sleep msg.

    Sleeping (rather than no-op'ing) lets us exercise the time-based
    exit condition in ``_check_cam_armed``.
    """
    import time as _t

    msgs = []
    try:
        for i, msg in enumerate(plan):
            msgs.append(msg)
            # bps.sleep yields a Msg with command='sleep' and args=(delay,)
            cmd = getattr(msg, "command", None)
            args = getattr(msg, "args", ())
            if cmd == "sleep" and args:
                _t.sleep(args[0])
            if i >= max_msgs:
                raise RuntimeError("plan did not terminate")
    except StopIteration:
        pass
    return msgs


def test_armed_state_returns_immediately():
    """Acquire on first poll -> generator returns with no sleeps."""
    det = _make_det(["Acquire"])
    plan = _check_cam_armed(det)
    msgs = _drain(plan)
    # We never yielded a sleep -- the first state read returned Acquire.
    assert msgs == []


def test_error_state_raises_with_status_message():
    """Error state on first poll -> RuntimeError with msg."""
    det = _make_det(["Error"], message="Failed to arm the detector")
    plan = _check_cam_armed(det)
    with pytest.raises(RuntimeError, match="Failed to arm the detector"):
        _drain(plan)


def test_each_error_state_raises():
    """All four error states trigger the raise."""
    for state in _CAM_ERROR_STATES:
        det = _make_det([state], message=f"msg for {state}")
        plan = _check_cam_armed(det)
        with pytest.raises(RuntimeError, match=state):
            _drain(plan)


def test_eventual_acquire_after_polls():
    """If state goes Initializing -> Initializing -> Acquire, the
    helper yields sleeps and then returns cleanly."""
    det = _make_det(["Initializing", "Initializing", "Acquire"])
    plan = _check_cam_armed(det, poll_s=0.01, max_wait_s=1.0)
    msgs = _drain(plan)
    # Two non-terminal polls before the Acquire -> two sleeps.
    assert len(msgs) == 2


def test_timeout_does_not_raise():
    """If state never becomes Acquire or Error within max_wait_s,
    the helper returns without raising (the downstream first-frame
    timeout handles genuinely stuck cams)."""
    det = _make_det(["Initializing"] * 100)
    plan = _check_cam_armed(det, poll_s=0.01, max_wait_s=0.05)
    msgs = _drain(plan)
    # Some sleeps were issued; no raise.
    assert msgs  # at least one sleep happened


def test_error_after_initializing_still_raises():
    """A transition Initializing -> Error mid-poll is caught."""
    det = _make_det(["Initializing", "Error"], message="Failed to arm the detector")
    plan = _check_cam_armed(det, poll_s=0.01, max_wait_s=1.0)
    with pytest.raises(RuntimeError, match="Failed to arm the detector"):
        _drain(plan)


def test_status_message_get_failure_is_safe():
    """If status_message.get() raises, we still raise with the
    detector_state info."""
    det = _make_det(["Error"])

    def broken_msg(use_monitor=False, as_string=False):
        raise RuntimeError("CA timeout")

    det.cam.status_message.get = broken_msg
    plan = _check_cam_armed(det)
    with pytest.raises(RuntimeError, match="detector_state='Error'"):
        _drain(plan)


def test_detector_state_get_failure_is_safe():
    """If detector_state.get() raises, the helper does not raise
    spuriously; it just keeps polling until the max_wait_s expires."""
    det = _make_det([])

    def broken_state(use_monitor=False, as_string=False):
        raise RuntimeError("CA timeout")

    det.cam.detector_state.get = broken_state
    plan = _check_cam_armed(det, poll_s=0.01, max_wait_s=0.05)
    # Should not raise.  Some sleeps happen.
    msgs = _drain(plan)
    assert msgs
