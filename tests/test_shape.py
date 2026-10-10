"""Table-driven tests for the spike/shape evaluator."""

from __future__ import annotations

import pytest

import ocp_idle_check as oic
from helpers import base_config, make_range_series


@pytest.mark.parametrize(
    ("points", "expected_active", "why"),
    [
        # Peak branch: any 15-min window above the threshold.
        ([5.0, 5.0, 45.0], True, "peak above threshold"),
        ([5.0] * 10 + [30.0], True, "shape: median 5, peak 30, ratio 6 vs required 5.66"),
        # Soft floor: a quiet baseline demands a bigger burst.  Median 2
        # requires 8.9x, so a 7.5x burst no longer fires, but a 10x does.
        ([2.0, 2.0, 15.0], False, "ratio 7.5 below the 8.94 required at a median of 2"),
        ([2.0, 2.0, 20.0], True, "ratio 10 clears the 8.94 required at a median of 2"),
        ([20.0] * 10, False, "steady moderate load: ratio 1"),
        ([25.0] * 10, False, "steady load below the peak threshold"),
        # A zero baseline with a nonzero peak is an unbounded ratio.
        ([0.0, 0.0, 10.0], True, "zero median, unbounded ratio"),
        ([0.0] * 5, False, "all-zero data"),
        ([], False, "no data"),
        # Boundaries: strictly greater-than on both branches.
        ([40.0] * 4, False, "peak exactly at the threshold is not exceeded"),
        # At a median of 10 (a quarter of the threshold) the requirement is
        # exactly 4x and the peak lands exactly at the threshold: neither
        # branch fires on the boundary.
        ([10.0, 10.0, 40.0], False, "ratio exactly the required 4 is not exceeded"),
    ],
)
def test_evaluate_shape(points, expected_active, why):
    stats = oic.evaluate_shape(
        points,
        peak_threshold=40.0,
        ratio_threshold=2.0,
    )
    assert stats.active is expected_active, why


def test_required_ratio_scales_with_baseline():
    """The multiplier is ratio-threshold at the scale (the criterion's normal
    threshold), doubles for every fourfold drop in the baseline, and is
    pinned at ratio-threshold above the scale."""
    points = [10.0] * 6  # ratio 1: never fires, but the requirement is computed

    def required(baseline):
        return oic.evaluate_shape(
            [baseline] * len(points),
            peak_threshold=40.0,
            ratio_threshold=2.0,
        ).required_ratio

    assert required(40.0) == pytest.approx(2.0)
    assert required(10.0) == pytest.approx(4.0)
    assert required(2.5) == pytest.approx(8.0)
    assert required(80.0) == pytest.approx(2.0)  # pinned above the scale
    assert required(0.0) is None  # zero baseline = unbounded


def test_evaluate_shape_scaled_requirement_is_strict():
    # Median 5: the requirement is 5.66x.  A 5.4x peak stays idle, a 5.8x
    # peak fires, and both sit below the peak threshold so only the shape
    # branch is in play.
    below = oic.evaluate_shape([5.0] * 4 + [27.0], peak_threshold=40.0, ratio_threshold=2.0)
    assert not below.shape_exceeded
    assert below.required_ratio == pytest.approx(5.66, abs=0.01)
    above = oic.evaluate_shape([5.0] * 4 + [29.0], peak_threshold=40.0, ratio_threshold=2.0)
    assert above.shape_exceeded


def test_evaluate_shape_reports_stats():
    stats = oic.evaluate_shape(
        [4.0, 5.0, 6.0, 50.0],
        peak_threshold=40.0,
        ratio_threshold=2.0,
    )
    assert stats.points == 4
    assert stats.peak == pytest.approx(50.0)
    # Median of an even-count list: mean of the middle two.
    assert stats.baseline == pytest.approx(5.5)
    assert stats.ratio == pytest.approx(50.0 / 5.5)
    assert stats.shape_exceeded
    assert stats.peak_exceeded


def test_evaluate_shape_zero_baseline_ratio_is_none():
    stats = oic.evaluate_shape(
        [0.0, 0.0, 22.0],
        peak_threshold=40.0,
        ratio_threshold=2.0,
    )
    assert stats.ratio is None
    assert stats.required_ratio is None
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


def test_evaluate_shape_by_node_judges_each_node_alone():
    """The mmacik-sno shape: a steady busy node pooled with a steady quiet
    sibling.  Pooled, the median lands between the modes and the busy node's
    peak over it reads as a 2x+ burst; per node, both are steady."""
    series = [
        make_range_series("busy-node", ["17"] * 10 + ["19.5"]),
        make_range_series("quiet-node", ["2"] * 10 + ["2.4"]),
    ]
    per_node = oic.evaluate_shape_by_node(series, peak_threshold=40.0, ratio_threshold=2.0)
    assert [s.node for s in per_node] == ["busy-node", "quiet-node"]
    assert all(not s.shape_exceeded for s in per_node)

    summary = oic.summarize_shape(per_node)
    assert not summary.active
    assert summary.points == 22
    assert summary.peak == pytest.approx(19.5)  # pooled max
    assert summary.node == "quiet-node"  # highest ratio: 1.2 vs 1.15
    assert summary.ratio == pytest.approx(1.2)


def test_evaluate_shape_by_node_fires_on_one_bursty_node():
    series = [
        make_range_series("steady", ["5"] * 10),
        make_range_series("bursty", ["5"] * 10 + ["35"]),
        make_range_series("busy", ["8"] * 11),
    ]
    per_node = oic.evaluate_shape_by_node(series, peak_threshold=40.0, ratio_threshold=2.0)
    summary = oic.summarize_shape(per_node)
    assert summary.shape_exceeded
    assert summary.node == "bursty"  # the driver, not the steady siblings
    assert summary.ratio == pytest.approx(7.0)
    assert summary.required_ratio == pytest.approx(5.66, abs=0.01)
    assert [s.node for s in per_node] == ["bursty", "busy", "steady"]  # sorted by name


def test_evaluate_shape_by_node_pools_cards_on_one_node():
    # DCGM: one series per card.  Cards on the same node pool into that
    # node's windows; the per-node grouping keys on `instance`.
    series = [
        {"metric": {"gpu": "0", "instance": "gpu-a:9400"}, "values": [[0, "5"], [1, "6"]]},
        {"metric": {"gpu": "1", "instance": "gpu-a:9400"}, "values": [[0, "5"], [1, "6"]]},
    ]
    per_node = oic.evaluate_shape_by_node(series, peak_threshold=40.0, ratio_threshold=2.0)
    assert len(per_node) == 1
    assert per_node[0].node == "gpu-a"
    assert per_node[0].points == 4


def test_evaluate_shape_by_node_filters_dead_nodes():
    series = [
        make_range_series("dead-node", ["50"] * 5),
        make_range_series("live-node", ["2"] * 5),
    ]
    per_node = oic.evaluate_shape_by_node(
        series, peak_threshold=40.0, ratio_threshold=2.0, live_nodes={"live-node"}
    )
    assert [s.node for s in per_node] == ["live-node"]


def test_evaluate_shape_by_node_prefers_unbounded_driver():
    # Severity ranks unbounded ratios (zero baseline, nonzero peak) above
    # any finite ratio, even a higher one.
    series = [
        make_range_series("finite", ["5"] * 10 + ["45"]),  # ratio 9, fires on shape
        make_range_series("unbounded", ["0"] * 10 + ["3"]),  # fires unbounded
    ]
    summary = oic.summarize_shape(
        oic.evaluate_shape_by_node(series, peak_threshold=40.0, ratio_threshold=2.0)
    )
    assert summary.node == "unbounded"
    assert summary.ratio is None
    assert summary.shape_exceeded
    assert summary.peak == pytest.approx(45.0)  # pooled max, from the other node


def test_summarize_shape_empty_is_zeroed():
    stats = oic.summarize_shape([])
    assert stats.points == 0
    assert not stats.active
    assert stats.ratio is None
    assert stats.required_ratio is None


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
