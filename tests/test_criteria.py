"""Unit tests for the five voting criteria (CPU, memory, API, GPU, operators).

Prometheus access is stubbed with FakeProm keyed on the exact query strings,
so a changed query fails the test instead of silently returning no data.  The
oc CLI is stubbed with FakeOc for the operators criterion.
"""

from __future__ import annotations

import ocp_idle_check as oic
from helpers import (
    FakeOc,
    FakeProm,
    api_avg_query,
    base_config,
    gpu_avg_query,
    gpu_max_query,
    legacy_cpu_query,
    make_dcgm_series,
    make_range_series,
    memory_window_query,
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
    # peak is 6x the baseline and above the floor.
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["5", "5", "30"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["peak_exceeded"] is False
    assert outcome.entry["shape_exceeded"] is True
    assert outcome.entry["ratio"] == 6.0


def test_cpu_quiet_cluster_is_idle():
    prom = FakeProm(
        ranges={
            oic.CPU_RANGE_QUERY: [
                make_range_series("node-1", ["2", "2", "10"]),
                make_range_series("node-2", ["2", "2", "8"]),
            ]
        }
    )
    outcome = oic.check_cpu(base_config(), prom, {"node-1", "node-2"}, 2.0)
    assert outcome.result == "IDLE"
    assert outcome.entry["instant_override"] is False
    assert outcome.entry["value"] == "10.00"


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
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["2", "2", "10"])]})
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
    assert outcome.entry["shape_exceeded"] is True  # zero baseline, peak above floor
    assert outcome.entry["ratio"] is None


def test_gpu_quiet_is_idle():
    prom = FakeProm(ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["2", "2", "10"])]})
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


OPERATOR_POD_OLD = "odh-operator-controller-manager-abc 1/1 Running 0 12d"
OPERATOR_POD_RESTARTED = "odh-operator-controller-manager-abc 1/1 Running 5 (3d ago) 12d"
QUIET_EVENT = "5m Normal Pulled pod/odh-xyz Pulled image quay.io/example"
MATCHING_EVENT = "5m Normal Scheduled pod/odh-xyz reconciled successfully"


def test_operators_old_and_quiet_is_idle():
    oc = FakeOc(
        existing={"opendatahub"},
        pods={"opendatahub": [OPERATOR_POD_OLD]},
        events={"opendatahub": [MATCHING_EVENT, MATCHING_EVENT]},
    )
    outcome = oic.check_operators(base_config(), oc)
    assert outcome.result == "IDLE"
    assert outcome.counted
    assert outcome.entry["age_days"] == 12
    assert outcome.entry["events"] == 2  # below the threshold of 5
    assert outcome.entry["namespaces"] == ["opendatahub"]


def test_operators_age_reads_last_field_despite_restart_annotation():
    # oc renders RESTARTS as "5 (3d ago)" on restarted pods, shifting AGE
    # out of the fixed column the bash script read.  The age must come from
    # the LAST field: 12d, not "ago" or "3d".
    oc = FakeOc(
        existing={"opendatahub"},
        pods={"opendatahub": [OPERATOR_POD_RESTARTED]},
        events={"opendatahub": []},
    )
    outcome = oic.check_operators(base_config(), oc)
    assert outcome.entry["age_days"] == 12


def test_operators_recent_events_mark_active():
    oc = FakeOc(
        existing={"opendatahub"},
        pods={"opendatahub": [OPERATOR_POD_OLD]},
        events={"opendatahub": [MATCHING_EVENT] * 5},
    )
    outcome = oic.check_operators(base_config(), oc)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["events"] == 5


def test_operators_young_pods_mark_active():
    oc = FakeOc(
        existing={"opendatahub"},
        pods={"opendatahub": ["odh-operator-controller-manager-abc 1/1 Running 0 2d"]},
        events={"opendatahub": []},
    )
    outcome = oic.check_operators(base_config(), oc)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["age_days"] == 2


def test_operators_na_when_no_namespaces_hold_pods():
    oc = FakeOc(existing=set(), pods={}, events={})
    outcome = oic.check_operators(base_config(), oc)
    assert outcome.result == "N/A"
    assert not outcome.counted
    # Only namespace lookups happened; no pod or event listings.
    assert [kind for kind, _ in oc.calls] == ["namespace"] * 3


def test_operators_events_counted_from_podless_namespaces():
    # Events are summed over every EXISTING namespace, including ones with
    # no matching operator pods (bash parity).
    oc = FakeOc(
        existing={"opendatahub", "redhat-ods-applications"},
        pods={"opendatahub": [OPERATOR_POD_OLD]},
        events={
            "opendatahub": [MATCHING_EVENT] * 2,
            "redhat-ods-applications": [MATCHING_EVENT] * 3,
        },
    )
    outcome = oic.check_operators(base_config(), oc)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["events"] == 5
    assert ("events", "redhat-ods-applications") in oc.calls


def test_operators_only_first_five_pods_per_namespace():
    # Bash parity: only the first five matching pods per namespace count.
    pods = [
        f"odh-operator-{i}-controller-manager 1/1 Running 0 {age}"
        for i, age in enumerate(["1d", "2d", "3d", "4d", "5d", "30d"])
    ]
    oc = FakeOc(existing={"opendatahub"}, pods={"opendatahub": pods}, events={"opendatahub": []})
    outcome = oic.check_operators(base_config(), oc)
    assert outcome.entry["age_days"] == 5  # the 30d pod is sixth and ignored
    assert outcome.result == "ACTIVE"


def test_operators_only_last_twenty_events_count():
    # oc sorts by lastTimestamp; only the most recent 20 events are read.
    # The matching lines sit oldest (first), beyond the 20-line window.
    events = [MATCHING_EVENT] * 5 + [QUIET_EVENT] * 20
    oc = FakeOc(
        existing={"opendatahub"},
        pods={"opendatahub": [OPERATOR_POD_OLD]},
        events={"opendatahub": events},
    )
    outcome = oic.check_operators(base_config(), oc)
    assert outcome.entry["events"] == 0
    assert outcome.result == "IDLE"


def test_convert_age_to_days():
    assert oic.convert_age_to_days("12d") == 12
    assert oic.convert_age_to_days("2d7h") == 2
    assert oic.convert_age_to_days("5h") == 0
    assert oic.convert_age_to_days("30m") == 0
    assert oic.convert_age_to_days("10s") == 0
