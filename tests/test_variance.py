"""Table-driven tests for the variance evaluator."""

from __future__ import annotations

import pytest

import ocp_idle_check as oic
from helpers import base_config, make_range_series


@pytest.mark.parametrize(
    ("points", "expected_active", "why"),
    [
        # Peak branch: any 15-min window above the threshold.
        ([5.0, 5.0, 45.0], True, "peak above threshold"),
        ([5.0] * 10 + [30.0], True, "variance: median 5, peak 30 clears the required 25.8"),
        # The motivating pattern: a node that idles low and loads under use.
        # At a 1% median the requirement is exactly 10x, at 2% it is ~7.5x
        # (a required peak of 15.0).
        ([1.0, 1.0, 11.0], True, "median 1, peak 11 clears the required 10"),
        ([1.0, 1.0, 10.0], False, "median 1, peak 10 sits exactly on the 10x requirement"),
        ([2.0, 2.0, 16.0], True, "median 2, peak 16 clears the required 15.0"),
        ([2.0, 2.0, 15.0], False, "peak 15 just below the required 15.0 at a median of 2"),
        ([20.0] * 10, False, "steady moderate load: requirement of 58.3"),
        ([25.0] * 10, False, "steady load below the peak threshold"),
        # A zero baseline has no floor: any positive peak fires.
        ([0.0, 0.0, 10.0], True, "zero median: any positive peak fires"),
        ([0.0, 0.0, 0.5], True, "zero median: however small the peak, it fires"),
        ([0.0] * 5, False, "all-zero data"),
        ([], False, "no data"),
        # Boundaries: strictly greater-than on both branches.
        ([30.0] * 4, False, "peak exactly at the threshold is not exceeded"),
        # At a 15% median the required peak is 49.2, well above the peak
        # threshold, so neither branch fires on a 30% peak.
        ([15.0, 15.0, 30.0], False, "required peak of 49.2 is not reached"),
    ],
)
def test_evaluate_variance(points, expected_active, why):
    stats = oic.evaluate_variance(
        points,
        peak_threshold=30.0,
        ratio_threshold=2.0,
    )
    assert stats.active is expected_active, why


def test_required_peak_scales_with_baseline():
    """The required peak follows one power law: 10x the baseline at a
    baseline of 1, falling to 2x at a baseline of 50, with no floor - a
    zero baseline demands only a positive peak."""
    points = [10.0] * 6  # ratio 1: never fires, but the requirement is computed

    def required(baseline):
        return oic.evaluate_variance(
            [baseline] * len(points),
            peak_threshold=30.0,
            ratio_threshold=2.0,
        ).required_peak

    assert required(1.0) == pytest.approx(10.0)  # 10x at a baseline of 1
    assert required(50.0) == pytest.approx(100.0)  # 2x at a baseline of 50
    assert required(25.0) == pytest.approx(66.50, abs=0.01)  # ~2.66x in between
    assert required(5.0) == pytest.approx(25.79, abs=0.01)  # ~5.2x on quiet baselines
    assert required(0.0) == pytest.approx(0.0)  # zero baseline: no floor


def test_evaluate_variance_scaled_requirement_is_strict():
    # Median 5: the requirement is a peak of 25.8.  A 25% peak stays idle,
    # a 26% peak fires, and both sit below the peak threshold so only the
    # variance branch is in play.
    below = oic.evaluate_variance([5.0] * 4 + [25.0], peak_threshold=30.0, ratio_threshold=2.0)
    assert not below.variance_exceeded
    assert below.required_peak == pytest.approx(25.79, abs=0.01)
    assert below.required_ratio == pytest.approx(5.16, abs=0.01)  # 25.79 / 5
    above = oic.evaluate_variance([5.0] * 4 + [26.0], peak_threshold=30.0, ratio_threshold=2.0)
    assert above.variance_exceeded


def test_evaluate_variance_reports_stats():
    stats = oic.evaluate_variance(
        [4.0, 5.0, 6.0, 50.0],
        peak_threshold=30.0,
        ratio_threshold=2.0,
    )
    assert stats.points == 4
    assert stats.peak == pytest.approx(50.0)
    # Median of an even-count list: mean of the middle two.
    assert stats.baseline == pytest.approx(5.5)
    assert stats.ratio == pytest.approx(50.0 / 5.5)
    assert stats.variance_exceeded
    assert stats.peak_exceeded


def test_evaluate_variance_zero_baseline_has_no_floor():
    # A zero baseline demands only a positive peak: 22% of DCGM-style noise
    # over a zero median fires, all-zero data does not.
    stats = oic.evaluate_variance(
        [0.0, 0.0, 22.0],
        peak_threshold=40.0,
        ratio_threshold=2.0,
    )
    assert stats.ratio is None
    assert stats.required_ratio is None
    assert stats.required_peak == pytest.approx(0.0)
    assert stats.variance_exceeded
    zeros = oic.evaluate_variance(
        [0.0, 0.0, 0.0],
        peak_threshold=40.0,
        ratio_threshold=2.0,
    )
    assert not zeros.variance_exceeded


def test_api_variance_zero_baseline_has_no_floor():
    # A zero median demands only a positive request rate: 40 req/s over a
    # zero median fires.
    stats = oic.evaluate_variance([0.0, 0.0, 40.0], ratio_threshold=2.0)
    assert stats.peak_exceeded is False  # the API rule has no peak branch
    assert stats.variance_exceeded


def test_api_variance_uses_the_shared_curve():
    # The API rule shares the CPU/GPU law in req/s: 2x at a 50 req/s
    # median, ~2.66x at 25 req/s - a required peak of 66.5 req/s.
    below = oic.evaluate_variance([25.0] * 6 + [66.0], ratio_threshold=2.0)
    assert below.peak_exceeded is False  # the API rule has no peak branch
    assert below.required_peak == pytest.approx(66.50, abs=0.01)
    assert below.required_ratio == pytest.approx(2.66, abs=0.01)
    assert not below.variance_exceeded
    above = oic.evaluate_variance([25.0] * 6 + [70.0], ratio_threshold=2.0)
    assert above.variance_exceeded


def test_evaluate_variance_by_node_judges_each_node_alone():
    """The mmacik-sno pattern: a steady busy node pooled with a steady quiet
    sibling.  Pooled, the median lands between the modes and the busy node's
    peak over it reads as a 2x+ burst; per node, both are steady."""
    series = [
        make_range_series("busy-node", ["17"] * 10 + ["19.5"]),
        make_range_series("quiet-node", ["2"] * 10 + ["2.4"]),
    ]
    per_node = oic.evaluate_variance_by_node(series, peak_threshold=30.0, ratio_threshold=2.0)
    assert [s.node for s in per_node] == ["busy-node", "quiet-node"]
    assert all(not s.variance_exceeded for s in per_node)

    summary = oic.summarize_variance(per_node)
    assert not summary.active
    assert summary.points == 22
    assert summary.peak == pytest.approx(19.5)  # pooled max
    # The busy node comes closest to its own requirement (19.5 of 53.0)
    # vs the quiet one (2.4 of 15.0), so it drives the summary.
    assert summary.node == "busy-node"
    assert summary.ratio == pytest.approx(19.5 / 17.0)


def test_evaluate_variance_by_node_fires_on_one_bursty_node():
    series = [
        make_range_series("steady", ["5"] * 10),
        make_range_series("bursty", ["5"] * 10 + ["28"]),
        make_range_series("busy", ["8"] * 11),
    ]
    per_node = oic.evaluate_variance_by_node(series, peak_threshold=30.0, ratio_threshold=2.0)
    summary = oic.summarize_variance(per_node)
    assert summary.variance_exceeded
    assert summary.node == "bursty"  # the driver, not the steady siblings
    assert summary.ratio == pytest.approx(5.6)
    assert summary.required_peak == pytest.approx(25.79, abs=0.01)
    assert [s.node for s in per_node] == ["bursty", "busy", "steady"]  # sorted by name


def test_evaluate_variance_by_node_pools_cards_on_one_node():
    # DCGM: one series per card.  Cards on the same node pool into that
    # node's windows; the per-node grouping keys on `instance`.
    series = [
        {"metric": {"gpu": "0", "instance": "gpu-a:9400"}, "values": [[0, "5"], [1, "6"]]},
        {"metric": {"gpu": "1", "instance": "gpu-a:9400"}, "values": [[0, "5"], [1, "6"]]},
    ]
    per_node = oic.evaluate_variance_by_node(series, peak_threshold=40.0, ratio_threshold=2.0)
    assert len(per_node) == 1
    assert per_node[0].node == "gpu-a"
    assert per_node[0].points == 4


def test_evaluate_variance_by_node_filters_dead_nodes():
    series = [
        make_range_series("dead-node", ["50"] * 5),
        make_range_series("live-node", ["2"] * 5),
    ]
    per_node = oic.evaluate_variance_by_node(
        series, peak_threshold=30.0, ratio_threshold=2.0, live_nodes={"live-node"}
    )
    assert [s.node for s in per_node] == ["live-node"]


def test_evaluate_variance_by_node_zero_baseline_fires_on_noise():
    # The DCGM noise tail under the no-floor law: a zero-median exporter
    # peaking at 3% now fires - the mmacik-sno GPU flip - alongside a
    # genuinely busy sibling.  A positive peak over a zero baseline is an
    # unbounded multiple, so the noise node drives the summary.
    series = [
        make_range_series("noise", ["0"] * 10 + ["3"]),
        make_range_series("bursty", ["5"] * 10 + ["45"]),
    ]
    per_node = oic.evaluate_variance_by_node(series, peak_threshold=40.0, ratio_threshold=2.0)
    by_node = {s.node: s for s in per_node}
    assert by_node["noise"].variance_exceeded  # 3% over a zero median: no floor
    assert by_node["bursty"].variance_exceeded
    summary = oic.summarize_variance(per_node)
    assert summary.variance_exceeded
    assert summary.node == "noise"  # an unbounded multiple outranks a finite one
    assert summary.peak == pytest.approx(45.0)  # pooled max


def test_summarize_variance_empty_is_zeroed():
    stats = oic.summarize_variance([])
    assert stats.points == 0
    assert not stats.active
    assert stats.ratio is None
    assert stats.required_ratio is None
    assert stats.required_peak is None


def test_defaults_catch_the_false_idle_cores():
    """The patterns that motivated the rule: a hot node diluted by a quiet
    cluster, a hibernated cluster with a genuinely quiet uptime, and a
    workload that idles at 1% and loads past 10%."""
    cfg = base_config()
    # sridhartest pattern: median 5%, peak 96.9% - caught by the peak branch.
    hot = oic.evaluate_variance(
        [5.0] * 20 + [96.9],
        peak_threshold=cfg.cpu_peak_threshold,
        ratio_threshold=cfg.cpu_variance_ratio,
    )
    assert hot.active
    # mmacik-sno pattern: steady ~20% single-node load - stays idle.
    steady = oic.evaluate_variance(
        [19.5, 19.5, 19.5, 19.5],
        peak_threshold=cfg.cpu_peak_threshold,
        ratio_threshold=cfg.cpu_variance_ratio,
    )
    assert not steady.active
    # The motivating burst: 1% idle, 11% under load - clears the required
    # 10x; a peak of exactly 10% would sit on the requirement.
    low_idle = oic.evaluate_variance(
        [1.0] * 20 + [11.0],
        peak_threshold=cfg.cpu_peak_threshold,
        ratio_threshold=cfg.cpu_variance_ratio,
    )
    assert low_idle.active
