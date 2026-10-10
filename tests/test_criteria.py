"""Unit tests for the five voting criteria (CPU, memory, API, GPU, operators).

Prometheus access is stubbed with FakeProm keyed on the exact query strings,
so a changed query fails the test instead of silently returning no data.  The
oc CLI is stubbed with FakeOc for the operators criterion.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import ocp_idle_check as oic
from helpers import (
    FakeOc,
    FakeProm,
    api_avg_query,
    base_config,
    event_row,
    gpu_avg_query,
    gpu_max_query,
    legacy_cpu_query,
    make_dcgm_series,
    make_range_series,
    memory_window_query,
    pod_row,
    rs_row,
    ts_before,
)

WINDOW = 10080


def gpu_node(name: str = "gpu-1") -> oic.GpuNode:
    return oic.GpuNode(name=name, vendor="NVIDIA", gpu_count=4, instance_type="g5.xlarge")


# === CPU ====================================================================


def test_cpu_active_on_peak():
    # Peak 45% crosses the threshold while the ratio stays low, isolating
    # the peak branch of the rule.
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["35", "35", "45"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"
    assert outcome.counted
    assert prom.range_calls == [oic.CPU_RANGE_QUERY]
    entry = outcome.entry
    assert entry["peak"] == 45.0
    assert entry["peak_exceeded"] is True
    assert entry["shape_exceeded"] is False
    assert entry["value"] == "45.00"
    assert entry["source"] == "spike/shape over 15m windows"


def test_cpu_active_on_shape_ratio():
    # Median 5%, peak 30%: no window crossed the peak threshold, but the
    # peak is 6x the median.
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["5", "5", "30"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["peak_exceeded"] is False
    assert outcome.entry["shape_exceeded"] is True
    assert outcome.entry["ratio"] == 6.0


def test_cpu_quiet_cluster_is_idle():
    # Steady quiet load: the peak barely rises above the median, so neither
    # branch fires.
    prom = FakeProm(
        ranges={
            oic.CPU_RANGE_QUERY: [
                make_range_series("node-1", ["8", "8", "10"]),
                make_range_series("node-2", ["8", "8", "9"]),
            ]
        }
    )
    outcome = oic.check_cpu(base_config(), prom, {"node-1", "node-2"}, 2.0)
    assert outcome.result == "IDLE"
    assert outcome.entry["instant_override"] is False
    assert outcome.entry["value"] == "10.00"


def test_cpu_bursty_quiet_cluster_is_active():
    # Median 2%, peak 10%: nothing crosses the peak threshold, but the 5x
    # ratio is a real burst - with the shape floor removed, this counts.
    prom = FakeProm(
        ranges={
            oic.CPU_RANGE_QUERY: [
                make_range_series("node-1", ["2", "2", "10"]),
                make_range_series("node-2", ["2", "2", "8"]),
            ]
        }
    )
    outcome = oic.check_cpu(base_config(), prom, {"node-1", "node-2"}, 2.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["peak_exceeded"] is False
    assert outcome.entry["shape_exceeded"] is True
    assert outcome.entry["ratio"] == 5.0


def test_cpu_dead_node_series_are_ignored():
    # A deleted node's spiky history must not vote; only live nodes count.
    prom = FakeProm(
        ranges={
            oic.CPU_RANGE_QUERY: [
                make_range_series("dead-node", ["50", "50", "50"]),
                make_range_series("node-1", ["2", "2", "2"]),
            ]
        }
    )
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "IDLE"
    assert outcome.entry["peak"] == 2.0


def test_cpu_series_without_node_label_uses_instance():
    # OCP node exporter series carry only `instance`; _series_node strips
    # the port so the live-node filter still matches.
    series = {
        "metric": {"instance": "node-1:9100"},
        "values": [[1700000000, "50"]],
    }
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [series]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"


def test_cpu_legacy_fallback_active():
    prom = FakeProm(instant={legacy_cpu_query(WINDOW): 25.0})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["source"] == "legacy window average"
    assert outcome.entry["window_average"] == 25.0
    assert outcome.entry["value"] == "25.00"


def test_cpu_legacy_fallback_idle():
    prom = FakeProm(instant={legacy_cpu_query(WINDOW): 5.0})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, 5.0)
    assert outcome.result == "IDLE"


def test_cpu_instant_override_beats_idle_shape():
    # Steady quiet windows, so the ACTIVE verdict comes from the instant
    # reading alone.
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["8", "8", "10"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, 50.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["instant_override"] is True
    assert outcome.entry["instant"] == 50.0


def test_cpu_instant_override_does_not_beat_active():
    # One-directional: a low instant reading must not downgrade an ACTIVE.
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["5", "5", "50"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, 1.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["instant_override"] is False


def test_cpu_no_data_is_unknown_but_counted():
    outcome = oic.check_cpu(base_config(), FakeProm(), {"node-1"}, None)
    assert outcome.result == "UNKNOWN"
    assert outcome.counted  # bash parity: cpu UNKNOWN still counts in the denominator


def test_cpu_window_zero_uses_instant_only():
    prom = FakeProm()
    outcome = oic.check_cpu(base_config(time_window_minutes=0), prom, {"node-1"}, 5.0)
    assert prom.range_calls == []  # no range query with no window
    assert outcome.result == "IDLE"
    assert outcome.entry["source"] == "instant"
    assert outcome.entry["value"] == "5.00"


def test_cpu_window_zero_instant_above_threshold():
    outcome = oic.check_cpu(base_config(time_window_minutes=0), FakeProm(), {"node-1"}, 50.0)
    assert outcome.result == "ACTIVE"


# === MEMORY =================================================================


def test_memory_windowed_active():
    prom = FakeProm(instant={memory_window_query(WINDOW): 40.0})
    outcome = oic.check_memory(base_config(), prom, 10.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["instant_override"] is False


def test_memory_windowed_idle():
    prom = FakeProm(instant={memory_window_query(WINDOW): 20.0})
    outcome = oic.check_memory(base_config(), prom, 30.0)
    assert outcome.result == "IDLE"
    assert outcome.entry["value"] == "20.00"


def test_memory_instant_override():
    prom = FakeProm(instant={memory_window_query(WINDOW): 20.0})
    outcome = oic.check_memory(base_config(), prom, 50.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["instant_override"] is True


def test_memory_instant_only_when_window_zero():
    outcome = oic.check_memory(base_config(time_window_minutes=0), FakeProm(), 40.0)
    assert outcome.result == "ACTIVE"
    outcome = oic.check_memory(base_config(time_window_minutes=0), FakeProm(), 20.0)
    assert outcome.result == "IDLE"


def test_memory_no_data_is_unknown_but_counted():
    outcome = oic.check_memory(base_config(), FakeProm(), None)
    assert outcome.result == "UNKNOWN"
    assert outcome.counted


# === API SERVER =============================================================


def test_api_average_above_threshold():
    prom = FakeProm(
        instant={api_avg_query(WINDOW): 150.0},
        ranges={
            oic.API_RANGE_QUERY: [{"metric": {}, "values": [[i * 900, "10"] for i in range(4)]}]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["value"] == "150.00"


def test_api_quiet_is_idle():
    prom = FakeProm(
        instant={api_avg_query(WINDOW): 5.0},
        ranges={
            oic.API_RANGE_QUERY: [{"metric": {}, "values": [[i * 900, "10"] for i in range(4)]}]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "IDLE"
    assert outcome.entry["spike_active"] is False


def test_api_spike_detector_marks_active():
    # Average 5 req/s is far below 100, but a 15-minute window hit 100 req/s:
    # 10x the median and above the spike floor.
    prom = FakeProm(
        instant={api_avg_query(WINDOW): 5.0},
        ranges={
            oic.API_RANGE_QUERY: [
                {"metric": {}, "values": [[i * 900, v] for i, v in enumerate(["10", "10", "100"])]}
            ]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["spike_active"] is True
    assert outcome.entry["spike_peak"] == 100.0
    assert outcome.entry["spike_ratio"] == 10.0


def test_api_steady_load_above_floor_is_not_a_spike():
    # Peak 80 is above the floor of 50 but only 1x the median.
    prom = FakeProm(
        instant={api_avg_query(WINDOW): 80.0},
        ranges={
            oic.API_RANGE_QUERY: [{"metric": {}, "values": [[i * 900, "80"] for i in range(4)]}]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "IDLE"  # 80 < 100 average threshold


def test_api_spike_active_without_average():
    # The average query failing does not sink the spike detector.
    prom = FakeProm(
        ranges={
            oic.API_RANGE_QUERY: [
                {"metric": {}, "values": [[i * 900, v] for i, v in enumerate(["10", "10", "100"])]}
            ]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["value"] is None


def test_api_no_data_is_unknown_and_not_counted():
    outcome = oic.check_api(base_config(), FakeProm())
    assert outcome.result == "UNKNOWN"
    assert not outcome.counted


def test_api_window_zero_uses_5m_average():
    prom = FakeProm(instant={api_avg_query(5): 150.0})
    outcome = oic.check_api(base_config(time_window_minutes=0), prom)
    assert prom.range_calls == []
    assert outcome.result == "ACTIVE"


# === GPU ====================================================================


def test_gpu_na_without_gpu_nodes():
    prom = FakeProm()
    outcome = oic.check_gpu(base_config(), prom, [])
    assert outcome.result == "N/A"
    assert not outcome.counted
    assert outcome.entry["reason"] == "no GPU nodes"
    assert prom.range_calls == []


def test_gpu_na_without_time_window():
    outcome = oic.check_gpu(base_config(time_window_minutes=0), FakeProm(), [gpu_node()])
    assert outcome.result == "N/A"
    assert not outcome.counted
    assert outcome.entry["reason"] == "no time window"


def test_gpu_active_on_peak():
    prom = FakeProm(ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["5", "5", "50"])]})
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "ACTIVE"
    assert outcome.counted
    assert outcome.entry["value"] == "50.00"
    assert outcome.entry["source"] == "spike/shape over 15m windows"


def test_gpu_active_on_shape_ratio():
    prom = FakeProm(ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["0", "0", "30"])]})
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "ACTIVE"
    assert outcome.entry["peak_exceeded"] is False
    assert outcome.entry["shape_exceeded"] is True  # zero baseline, unbounded ratio
    assert outcome.entry["ratio"] is None


def test_gpu_quiet_is_idle():
    # Steady quiet utilization: ratio 1.25, well below both branches.
    prom = FakeProm(ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["8", "8", "10"])]})
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "IDLE"


def test_gpu_pools_all_dcgm_series():
    # Every exporter series votes, not just per-node medians: one card's
    # spike among several quiet cards still marks the criterion ACTIVE.
    prom = FakeProm(
        ranges={
            oic.GPU_RANGE_QUERY: [
                make_dcgm_series(["0", "0", "0"], gpu="0"),
                make_dcgm_series(["0", "0", "0"], gpu="1"),
                make_dcgm_series(["0", "0", "45"], gpu="2"),
            ]
        }
    )
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "ACTIVE"


def test_gpu_aggregate_fallback():
    # No query_range matrix: whole-window max/avg aggregates decide.
    prom = FakeProm(
        instant_all={
            gpu_max_query(WINDOW): [
                {"metric": {"gpu": "0"}, "value": [1700000000, "30"]},
                {"metric": {"gpu": "1"}, "value": [1700000000, "10"]},
            ],
            gpu_avg_query(WINDOW): [
                {"metric": {"gpu": "0"}, "value": [1700000000, "10"]},
                {"metric": {"gpu": "1"}, "value": [1700000000, "4"]},
            ],
        }
    )
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "ACTIVE"
    assert outcome.entry["source"] == "window aggregates fallback"
    assert outcome.entry["peak"] == 30.0
    assert outcome.entry["baseline"] == 7.0  # mean of the per-series averages
    assert outcome.entry["ratio"] == 4.29  # 30/7, rounded as exported


def test_gpu_aggregate_fallback_quiet():
    prom = FakeProm(
        instant_all={
            gpu_max_query(WINDOW): [{"metric": {"gpu": "0"}, "value": [1700000000, "5"]}],
            gpu_avg_query(WINDOW): [{"metric": {"gpu": "0"}, "value": [1700000000, "3"]}],
        }
    )
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "IDLE"


def test_gpu_na_without_dcgm_metrics():
    outcome = oic.check_gpu(base_config(), FakeProm(), [gpu_node()])
    assert outcome.result == "N/A"
    assert not outcome.counted
    assert outcome.entry["reason"] == "no DCGM metrics"


# === OPERATORS ==============================================================

# Fixed clock for the operator tests: ages are exact and the event window
# boundaries are testable at 47/49 hours.
NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)

RS_KEY = ("opendatahub", "odh-operator-controller-manager-abc", "odh-operator-5d4c3b")


def old_operator_pod(namespace: str = "opendatahub") -> str:
    return pod_row(*RS_KEY[:2], "ReplicaSet", RS_KEY[2], ts_before(NOW, days=12))


def old_operator_rs() -> str:
    return rs_row(RS_KEY[0], RS_KEY[2], ts_before(NOW, days=12))


def reconciled_event(
    namespace: str = "opendatahub", hours: float = 1.0, timestamp: str | None = None
) -> str:
    return event_row(
        namespace,
        now=NOW,
        hours=hours,
        timestamp=timestamp,
        reason="Reconciled",
        kind="DataScienceCluster",
        involved="dsc-sample",
        message="reconciliation complete",
    )


def test_operators_old_and_quiet_is_idle():
    oc = FakeOc(pods=[old_operator_pod()], replicasets=[old_operator_rs()], events=[])
    outcome = oic.check_operators(base_config(), oc, now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.counted
    assert outcome.entry["age_days"] == 12.0
    assert outcome.entry["events"] == 0  # below the threshold of 5
    assert outcome.entry["namespaces"] == ["opendatahub"]
    assert outcome.entry["event_window_hours"] == 48
    assert outcome.entry["units"] == [
        {
            "namespace": "opendatahub",
            "kind": "replicaset",
            "name": "odh-operator-5d4c3b",
            "age_days": 12.0,
        }
    ]
    assert oc.calls == ["pods", "replicasets", "events"]


def test_operators_young_controllers_mark_active():
    pod = pod_row(*RS_KEY[:2], "ReplicaSet", RS_KEY[2], ts_before(NOW, days=2))
    rs = rs_row(RS_KEY[0], RS_KEY[2], ts_before(NOW, days=2))
    outcome = oic.check_operators(base_config(), FakeOc(pods=[pod], replicasets=[rs]), now=NOW)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["age_days"] == 2.0


def test_operators_median_young_majority_is_active():
    # Two of three controllers redeployed two days ago; one stale survivor
    # is 40 days old.  The old oldest-pod rule called this IDLE.
    pods = [
        pod_row(
            "ns-a",
            f"{name}-controller-manager",
            "ReplicaSet",
            f"{name}-rs",
            ts_before(NOW, days=age),
        )
        for name, age in (("a", 2), ("b", 2), ("c", 40))
    ]
    outcome = oic.check_operators(base_config(), FakeOc(pods=pods), now=NOW)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["age_days"] == 2.0


def test_operators_median_old_majority_stays_idle():
    # One fresh unit among old ones: a single recent restart must not mark
    # the cluster active.  Ages 30, 30, 1 -> median 30.
    pods = [
        pod_row(
            "ns-a",
            f"{name}-controller-manager",
            "ReplicaSet",
            f"{name}-rs",
            ts_before(NOW, days=age),
        )
        for name, age in (("a", 30), ("b", 30), ("c", 1))
    ]
    outcome = oic.check_operators(base_config(), FakeOc(pods=pods), now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.entry["age_days"] == 30.0


def test_operators_even_sample_averages_the_middle_ages():
    # Ages 2, 2, 30, 30: the median is 16, above the threshold even though
    # half the stack is fresh.
    pods = [
        pod_row(
            "ns-a",
            f"{name}-controller-manager",
            "ReplicaSet",
            f"{name}-rs",
            ts_before(NOW, days=age),
        )
        for name, age in (("a", 2), ("b", 2), ("c", 30), ("d", 30))
    ]
    outcome = oic.check_operators(base_config(), FakeOc(pods=pods), now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.entry["age_days"] == 16.0


def test_operators_replicaset_age_survives_drain():
    # The ReplicaSet is 30 days old but its pod was recreated yesterday by
    # a node drain: the unit's age is the ReplicaSet's, not the pod's.
    pod = pod_row(*RS_KEY[:2], "ReplicaSet", RS_KEY[2], ts_before(NOW, days=1))
    rs = rs_row(RS_KEY[0], RS_KEY[2], ts_before(NOW, days=30))
    outcome = oic.check_operators(base_config(), FakeOc(pods=[pod], replicasets=[rs]), now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.entry["age_days"] == 30.0


def test_operators_bare_pod_falls_back_to_pod_age():
    # A controller pod with no owner references is aged by its own creation
    # time; no ReplicaSet listing is needed for it.
    oc = FakeOc(
        pods=[pod_row("team-a", "standalone-operator", "", "", ts_before(NOW, days=2))], events=[]
    )
    outcome = oic.check_operators(base_config(), oc, now=NOW)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["age_days"] == 2.0
    assert oc.calls == ["pods", "events"]


def test_operators_missing_replicaset_falls_back_to_pod_age():
    # A ReplicaSet deleted mid-rollout leaves its pods behind; the oldest
    # replica's age stands in, and unrelated ReplicaSets (here: an old
    # scaled-to-zero revision) are not read.
    pods = [
        pod_row(
            RS_KEY[0],
            f"odh-operator-controller-manager-{sfx}",
            "ReplicaSet",
            "odh-operator-gone",
            ts_before(NOW, days=age),
        )
        for sfx, age in (("abc", 30), ("def", 1))
    ]
    rs = rs_row(RS_KEY[0], "unrelated-old-revision", ts_before(NOW, days=5))
    outcome = oic.check_operators(base_config(), FakeOc(pods=pods, replicasets=[rs]), now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.entry["age_days"] == 30.0


def test_operators_dedupes_replicas_of_one_replicaset():
    # Three replicas of one ReplicaSet are a single unit; the median must
    # not be skewed by replica count.
    pods = [
        pod_row(
            RS_KEY[0],
            f"odh-operator-controller-manager-{sfx}",
            "ReplicaSet",
            RS_KEY[2],
            ts_before(NOW, days=12),
        )
        for sfx in ("abc", "def", "ghi")
    ]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=pods, replicasets=[old_operator_rs()]), now=NOW
    )
    assert len(outcome.entry["units"]) == 1
    assert outcome.entry["age_days"] == 12.0


def test_operators_event_window_is_48_hours():
    # 47 hours old counts, 49 does not, and an event with no timestamps at
    # all is treated as outside the window.
    events = [
        reconciled_event(hours=47),
        reconciled_event(hours=49),
        reconciled_event(timestamp=""),
    ]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], events=events), now=NOW
    )
    assert outcome.entry["events"] == 1
    assert outcome.result == "IDLE"


def test_operators_recent_events_mark_active():
    events = [reconciled_event(hours=1) for _ in range(5)]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], events=events), now=NOW
    )
    assert outcome.result == "ACTIVE"
    assert outcome.entry["events"] == 5


def test_operators_workload_lifecycle_events_are_excluded():
    # Events about pods, replica sets, and deployments are the noise a node
    # drain manufactures; they match the regex but must not count toward
    # the threshold.  The excluded tally is exported for calibration.
    events = [
        event_row(
            "opendatahub", now=NOW, reason="Created", kind="Pod", message="Created pod odh-xyz"
        ),
        event_row(
            "opendatahub",
            now=NOW,
            reason="SuccessfulCreate",
            kind="ReplicaSet",
            message="created pod odh-xyz-abc",
        ),
        event_row(
            "opendatahub",
            now=NOW,
            reason="ScalingReplicaSet",
            kind="Deployment",
            message="scaled up replica set odh-xyz",
        ),
        reconciled_event(),
    ]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], events=events), now=NOW
    )
    assert outcome.result == "IDLE"
    assert outcome.entry["events"] == 1
    assert outcome.entry["events_excluded_workload"] == 3


def test_operators_platform_namespaces_are_ignored():
    # openshift-* and kube-* hold the platform's own controllers, whose
    # pods and events would swamp the criterion.
    oc = FakeOc(
        pods=[
            pod_row(
                "openshift-operators",
                "cluster-automation-operator-abc",
                "ReplicaSet",
                "cao-rs",
                ts_before(NOW, days=1),
            ),
            pod_row("kube-system", "some-operator-xyz", "", "", ts_before(NOW, days=1)),
        ],
        events=[reconciled_event("openshift-ingress-operator")],
    )
    outcome = oic.check_operators(base_config(), oc, now=NOW)
    assert outcome.result == "N/A"
    assert not outcome.counted
    assert outcome.entry["reason"] == "no controller pods in any scanned namespace"


def test_operators_configured_namespaces_override_the_exclusions():
    # A namespace on the always-include list counts even when it matches an
    # excluded prefix (an ODH install inside openshift-*, say).
    cfg = base_config(operator_namespaces="openshift-odh-custom")
    pod = pod_row(
        "openshift-odh-custom", "my-operator-1", "ReplicaSet", "my-rs", ts_before(NOW, days=12)
    )
    outcome = oic.check_operators(cfg, FakeOc(pods=[pod]), now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.entry["namespaces"] == ["openshift-odh-custom"]


def test_operators_na_makes_no_further_oc_calls():
    oc = FakeOc(pods=[], events=[])
    outcome = oic.check_operators(base_config(), oc, now=NOW)
    assert outcome.result == "N/A"
    assert not outcome.counted
    # The pod listing alone rules out the criterion; no ReplicaSet or event
    # listings follow.
    assert oc.calls == ["pods"]


def test_operators_scans_beyond_the_configured_namespaces():
    # Controllers running in user namespaces - strimzi, prometheus-operator,
    # istio - are operator activity too; the scan covers them.
    pod = pod_row(
        "team-a", "strimzi-cluster-operator-abc", "ReplicaSet", "strimzi-rs", ts_before(NOW, days=1)
    )
    outcome = oic.check_operators(base_config(), FakeOc(pods=[pod]), now=NOW)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["namespaces"] == ["team-a"]


def test_operators_events_sum_across_scanned_namespaces():
    # Events count from every scanned namespace, not just the ones holding
    # matching pods; strimzi reconciles Kafka custom resources, not pods.
    events = [reconciled_event() for _ in range(2)]
    events += [
        event_row(
            "team-a",
            now=NOW,
            reason="Reconciled",
            kind="Kafka",
            involved="my-cluster",
            message="reconciliation complete",
        )
        for _ in range(3)
    ]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], events=events), now=NOW
    )
    assert outcome.result == "ACTIVE"
    assert outcome.entry["events"] == 5


def test_parse_k8s_timestamp():
    assert oic.parse_k8s_timestamp("2026-10-01T12:00:00Z") == datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert oic.parse_k8s_timestamp("") is None
    assert oic.parse_k8s_timestamp("not-a-timestamp") is None
    # Naive values are read as UTC, never left to poison the arithmetic.
    assert oic.parse_k8s_timestamp("2026-10-01T12:00:00") == datetime(2026, 10, 1, 12, tzinfo=UTC)


def test_node_top_percentages_maps_and_skips_malformed():
    lines = [
        "node-1 250m 2% 3212Mi 5%",
        "gpu-node-1 500m 3% 8000Mi 6%",
        "short line",
        "node-2 100m junk% 2000Mi 7%",
    ]
    # Malformed rows are dropped whole; later duplicates win, like a dict build.
    assert oic.node_top_percentages(lines) == {
        "node-1": (2.0, 5.0),
        "gpu-node-1": (3.0, 6.0),
    }


def test_instant_by_instance_and_average_matching():
    series = [
        {"metric": {"instance": "gpu-node-1:9100"}, "value": [1700000000, "7"]},
        {"metric": {"instance": "gpu-node-10:9100"}, "value": [1700000000, "9"]},
        {"metric": {"instance": "other:9100"}, "value": [1700000000, "50"]},
        {"metric": {}, "value": [1700000000, "junk"]},
    ]
    by_instance = oic.instant_by_instance(series)
    assert by_instance == {"gpu-node-1:9100": 7.0, "gpu-node-10:9100": 9.0, "other:9100": 50.0}
    # The prefix quirk is inherited from the original per-node queries:
    # `instance=~"gpu-node-1.*"` also matches gpu-node-10's series, so the
    # mean pools both. An unmatched node has no value.
    gpu_node_1 = re.compile(r"gpu-node-1.*")
    assert oic.average_matching(by_instance, gpu_node_1) == 8.0
    assert oic.average_matching(by_instance, re.compile(r"missing.*")) is None
    assert oic.instant_by_instance(None) == {}
