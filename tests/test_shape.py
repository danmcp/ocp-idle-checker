"""Table-driven tests for the spike/shape evaluator."""

from __future__ import annotations

import pytest

import ocp_idle_check as oic
from helpers import base_config


@pytest.mark.parametrize(
    ("points", "expected_active", "why"),
    [
        # Peak branch: any 15-min window above the threshold.
        ([5.0, 5.0, 45.0], True, "peak above threshold"),
        ([5.0] * 10 + [30.0], True, "shape: median 5, peak 30, ratio 6, above floor"),
        # Quiet baselines must not trip the ratio alone.
        ([2.0, 2.0, 15.0], False, "ratio 7.5 but peak below the floor"),
        ([20.0] * 10, False, "steady moderate load: ratio 1, peak at floor"),
        ([25.0] * 10, False, "steady load above the floor but below the peak threshold"),
        # A zero baseline with a nonzero peak is an unbounded ratio, floored.
        ([0.0, 0.0, 25.0], True, "zero median, peak above floor"),
        ([0.0, 0.0, 10.0], False, "zero median, peak below floor"),
        ([0.0] * 5, False, "all-zero data"),
        ([], False, "no data"),
        # Boundaries: strictly greater-than on both branches.
        ([40.0] * 4, False, "peak exactly at the threshold is not exceeded"),
        ([10.0, 10.0, 20.0], False, "ratio exactly 2 is not exceeded"),
    ],
)
def test_evaluate_shape(points, expected_active, why):
    stats = oic.evaluate_shape(
        points,
        peak_threshold=40.0,
        ratio_threshold=2.0,
        floor=20.0,
    )
    assert stats.active is expected_active, why


def test_evaluate_shape_reports_stats():
    stats = oic.evaluate_shape(
        [4.0, 5.0, 6.0, 30.0],
        peak_threshold=40.0,
        ratio_threshold=2.0,
        floor=20.0,
    )
    assert stats.points == 4
    assert stats.peak == pytest.approx(30.0)
    # Median of an even-count list: mean of the middle two.
    assert stats.baseline == pytest.approx(5.5)
    assert stats.ratio == pytest.approx(30.0 / 5.5)
    assert stats.shape_exceeded
    assert not stats.peak_exceeded


def test_evaluate_shape_zero_baseline_ratio_is_none():
    stats = oic.evaluate_shape(
        [0.0, 0.0, 22.0],
        peak_threshold=40.0,
        ratio_threshold=2.0,
        floor=20.0,
    )
    assert stats.ratio is None
    assert stats.shape_exceeded  # unbounded ratio, gated only by the floor


def test_defaults_catch_the_false_idle_core_shapes():
    """The shapes that motivated the rule: a hot node diluted by a quiet
    cluster, and a hibernated cluster with a genuinely quiet uptime."""
    cfg = base_config()
    # sridhartest shape: median 5%, peak 96.9% - caught by the peak branch.
    hot = oic.evaluate_shape(
        [5.0] * 20 + [96.9],
        peak_threshold=cfg.cpu_peak_threshold,
        ratio_threshold=cfg.cpu_shape_ratio,
        floor=cfg.cpu_shape_floor,
    )
    assert hot.active
    # mmacik-sno shape: steady ~20% single-node load - stays idle.
    steady = oic.evaluate_shape(
        [19.5, 19.5, 19.5, 19.5],
        peak_threshold=cfg.cpu_peak_threshold,
        ratio_threshold=cfg.cpu_shape_ratio,
        floor=cfg.cpu_shape_floor,
    )
    assert not steady.active
