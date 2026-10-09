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
        ([5.0] * 10 + [30.0], True, "shape: median 5, peak 30, ratio 6"),
        # No floor: a quiet baseline with a bursty peak trips the ratio alone.
        ([2.0, 2.0, 15.0], True, "ratio 7.5 on a quiet baseline"),
        ([20.0] * 10, False, "steady moderate load: ratio 1"),
        ([25.0] * 10, False, "steady load below the peak threshold"),
        # A zero baseline with a nonzero peak is an unbounded ratio.
        ([0.0, 0.0, 10.0], True, "zero median, unbounded ratio"),
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
    )
    assert stats.active is expected_active, why


def test_evaluate_shape_reports_stats():
    stats = oic.evaluate_shape(
        [4.0, 5.0, 6.0, 30.0],
        peak_threshold=40.0,
        ratio_threshold=2.0,
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
    )
    assert stats.ratio is None
    assert stats.shape_exceeded  # unbounded ratio


def test_evaluate_shape_optional_floor_gates_the_ratio_branch():
    # Only the API spike rule passes a floor: a peak below it cannot trip
    # the ratio branch, even with an unbounded one.
    below = oic.evaluate_shape(
        [0.0, 0.0, 40.0],
        peak_threshold=1000.0,
        ratio_threshold=2.0,
        floor=50.0,
    )
    assert below.shape_exceeded is False
    above = oic.evaluate_shape(
        [0.0, 0.0, 60.0],
        peak_threshold=1000.0,
        ratio_threshold=2.0,
        floor=50.0,
    )
    assert above.shape_exceeded


def test_defaults_catch_the_false_idle_core_shapes():
    """The shapes that motivated the rule: a hot node diluted by a quiet
    cluster, and a hibernated cluster with a genuinely quiet uptime."""
    cfg = base_config()
    # sridhartest shape: median 5%, peak 96.9% - caught by the peak branch.
    hot = oic.evaluate_shape(
        [5.0] * 20 + [96.9],
        peak_threshold=cfg.cpu_peak_threshold,
        ratio_threshold=cfg.cpu_shape_ratio,
    )
    assert hot.active
    # mmacik-sno shape: steady ~20% single-node load - stays idle.
    steady = oic.evaluate_shape(
        [19.5, 19.5, 19.5, 19.5],
        peak_threshold=cfg.cpu_peak_threshold,
        ratio_threshold=cfg.cpu_shape_ratio,
    )
    assert not steady.active
