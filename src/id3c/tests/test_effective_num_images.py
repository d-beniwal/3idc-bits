"""Tests for ``effective_num_images``."""

from id3c.plans.flyscan_3idc import MAX_ACQUISITION_SECONDS
from id3c.plans.flyscan_3idc import UNLIMITED_FRAMES
from id3c.plans.flyscan_3idc import effective_num_images


def test_short_period_caps_at_unlimited_frames():
    """Short t_period -> capped by UNLIMITED_FRAMES, not by duration."""
    assert effective_num_images(0.01) == UNLIMITED_FRAMES
    assert effective_num_images(0.1) == UNLIMITED_FRAMES


def test_long_period_caps_at_max_seconds():
    """Long t_period -> capped by MAX_ACQUISITION_SECONDS / t_period."""
    n = effective_num_images(10.0)
    assert n == MAX_ACQUISITION_SECONDS // 10
    assert n * 10.0 <= MAX_ACQUISITION_SECONDS


def test_crossover_at_max_over_unlimited():
    """Crossover happens at t_period = MAX_ACQUISITION_SECONDS / UNLIMITED_FRAMES."""
    crossover = MAX_ACQUISITION_SECONDS / UNLIMITED_FRAMES
    # Just below crossover: should be UNLIMITED_FRAMES.
    assert effective_num_images(crossover * 0.9) == UNLIMITED_FRAMES
    # Just above crossover: should be duration-limited.
    n = effective_num_images(crossover * 1.1)
    assert n < UNLIMITED_FRAMES
    assert n * (crossover * 1.1) <= MAX_ACQUISITION_SECONDS


def test_returns_at_least_one():
    """Even a pathologically long t_period returns >= 1."""
    assert effective_num_images(1e12) == 1


def test_zero_period_clamped():
    """t_period <= 0 must not divide-by-zero; returns UNLIMITED_FRAMES."""
    assert effective_num_images(0.0) == UNLIMITED_FRAMES
    assert effective_num_images(-1.0) == UNLIMITED_FRAMES


def test_observed_failing_combo_is_capped():
    """The 2026-06-16 failing combo (1_000_000 frames at 1 s period,
    = 1_000_000 s ~= 11.6 days) is reduced to a safe value."""
    n = effective_num_images(1.0)
    assert n <= UNLIMITED_FRAMES
    assert n * 1.0 <= MAX_ACQUISITION_SECONDS
