"""Tests for the 80% verdict rule and the CSV/JSON exports."""

from __future__ import annotations

import json
import math

import pytest

import ocp_idle_check as oic
from helpers import base_config


def outcome(result: str, counted: bool = True, heat: float | None = None) -> oic.CheckOutcome:
    return oic.CheckOutcome(result, {"result": result, "threshold_ratio": heat}, counted)


# === VERDICT ================================================================


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        # All five voting IDLE.
        (
            [outcome("IDLE")] * 5,
            (5, 5, 4, "IDLE", 1),
        ),
        # Four criteria (operators N/A), all IDLE.
        (
            [outcome("IDLE")] * 4 + [outcome("N/A", counted=False)],
            (4, 4, 3, "IDLE", 1),
        ),
        # Boundary: 3 of 4 met is exactly 80% - still IDLE.
        (
            [outcome("IDLE")] * 3 + [outcome("ACTIVE")] + [outcome("N/A", counted=False)],
            (4, 3, 3, "IDLE", 1),
        ),
        # 2 of 4 met is below the threshold.
        (
            [outcome("IDLE")] * 2 + [outcome("ACTIVE")] * 2 + [outcome("N/A", counted=False)],
            (4, 2, 3, "ACTIVE", 0),
        ),
        # A single ACTIVE criterion among four still yields IDLE - the bash
        # script had the same property (int(4 * 0.80) = 3).
        (
            [outcome("ACTIVE")] + [outcome("IDLE")] * 3,
            (4, 3, 3, "IDLE", 1),
        ),
        # CPU UNKNOWN still counts in the denominator but does not vote
        # IDLE; 4 of 5 is exactly 80%.
        (
            [outcome("UNKNOWN")] + [outcome("IDLE")] * 4,
            (5, 4, 4, "IDLE", 1),
        ),
        # Everything unknown or N/A: total 0, threshold 0, 0 >= 0 - IDLE.
        # Unreachable in practice (cpu/memory UNKNOWN still count), but it
        # is what the bash formula did, so it is what this does.
        (
            [outcome("UNKNOWN", counted=False)] * 5,
            (0, 0, 0, "IDLE", 1),
        ),
        (
            [outcome("ACTIVE")] * 5,
            (5, 0, 4, "ACTIVE", 0),
        ),
    ],
)
def test_compute_verdict(outcomes, expected):
    assert oic.compute_verdict(outcomes) == expected


# === THRESHOLD FOCUS ========================================================


def test_threshold_focus_idle_picks_the_hottest_idle_vote():
    outcomes = [
        outcome("IDLE", heat=0.33),
        outcome("IDLE", heat=0.9),
        outcome("IDLE", heat=0.19),
        outcome("N/A", counted=False),
        outcome("ACTIVE", heat=2.5),
    ]
    assert oic.threshold_focus(outcomes, "IDLE") == {
        "criterion": "memory",
        "label": "Memory",
        "threshold_ratio": 0.9,
        "quorum_driven": False,
    }


def test_threshold_focus_active_picks_the_hottest_overall():
    outcomes = [
        outcome("IDLE", heat=0.5),
        outcome("IDLE", heat=0.6),
        outcome("IDLE", heat=0.7),
        outcome("N/A", counted=False),
        outcome("ACTIVE", heat=3.5),
    ]
    assert oic.threshold_focus(outcomes, "ACTIVE") == {
        "criterion": "operators",
        "label": "Operators",
        "threshold_ratio": 3.5,
        "quorum_driven": False,
    }


def test_threshold_focus_marks_quorum_driven_actives():
    outcomes = [
        outcome("UNKNOWN"),
        outcome("UNKNOWN"),
        outcome("IDLE", heat=0.85),
        outcome("N/A", counted=False),
        outcome("IDLE", heat=0.4),
    ]
    assert oic.threshold_focus(outcomes, "ACTIVE") == {
        "criterion": "api_server",
        "label": "API Server",
        "threshold_ratio": 0.85,
        "quorum_driven": True,
    }


def test_threshold_focus_without_any_heat_is_none():
    outcomes = [outcome("UNKNOWN") for _ in range(5)]
    assert oic.threshold_focus(outcomes, "IDLE") is None
    assert oic.threshold_focus(outcomes, "ACTIVE") is None


def test_format_threshold_ratio():
    assert oic.format_threshold_ratio(0.58) == "58%"
    assert oic.format_threshold_ratio(1.94) == "194%"
    assert oic.format_threshold_ratio(3.5) == "350%"
    assert oic.format_threshold_ratio(math.inf) == "999%+"


# === OVERSIZED ==============================================================


def variance_node(node: str, peak: float, peak_exceeded: bool = False) -> dict:
    """A per-node entry as _variance_entry renders it."""
    return {"node": node, "peak": peak, "peak_exceeded": peak_exceeded}


def oversized_kwargs(**overrides) -> dict:
    """evaluate_oversized inputs for an ACTIVE cluster with one quiet worker."""
    kwargs = dict(
        status="ACTIVE",
        quorum_driven=False,
        cpu_entry={"per_node": [variance_node("worker-1", 12.5)]},
        gpu_entry={"result": "N/A", "reason": "no GPU nodes"},
        gpu_node_names=set(),
        worker_nodes={"worker-1"},
    )
    kwargs.update(overrides)
    return kwargs


def test_oversized_false_when_cluster_not_active():
    out = oic.evaluate_oversized(**oversized_kwargs(status="IDLE"))
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_false_when_no_criterion_fired():
    # A quorum-driven ACTIVE has no evidence of use, so no right-sizing
    # claim either.
    out = oic.evaluate_oversized(**oversized_kwargs(quorum_driven=True))
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_false_when_node_listing_failed():
    out = oic.evaluate_oversized(**oversized_kwargs(worker_nodes=None, gpu_node_names=None))
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_false_without_per_node_cpu_data():
    # Legacy fallback or instant-only CPU: no per-node peaks to judge.
    cpu_entry = {"result": "IDLE", "window_average": 8.0}
    out = oic.evaluate_oversized(**oversized_kwargs(cpu_entry=cpu_entry))
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_true_lists_level_idle_workers():
    out = oic.evaluate_oversized(**oversized_kwargs())
    assert out == {
        "result": True,
        "gpu_peak": None,
        "nodes": [{"node": "worker-1", "cpu_peak": 12.5, "gpu_node": False}],
    }


def test_oversized_ignores_variance_firings():
    # A node that only ever low-level-bursted (variance exceeded, peak under
    # the threshold) is still level-idle - that is the whole point.
    cpu_entry = {"per_node": [{**variance_node("worker-1", 11.0), "variance_exceeded": True}]}
    out = oic.evaluate_oversized(**oversized_kwargs(cpu_entry=cpu_entry))
    assert out["result"] is True


def test_oversized_skips_nodes_that_fired_the_peak_branch():
    cpu_entry = {
        "per_node": [variance_node("worker-1", 12.5), variance_node("worker-2", 44.0, True)]
    }
    out = oic.evaluate_oversized(
        **oversized_kwargs(cpu_entry=cpu_entry, worker_nodes={"worker-1", "worker-2"})
    )
    assert out["result"] is True
    assert [e["node"] for e in out["nodes"]] == ["worker-1"]


def test_oversized_excludes_control_plane_and_infra_nodes():
    # A quiet master or infra node is expected, not right-sizing evidence;
    # a SNO (its only node control-plane) can therefore never be oversized.
    cpu_entry = {"per_node": [variance_node("master-0", 12.5), variance_node("infra-0", 9.0)]}
    out = oic.evaluate_oversized(**oversized_kwargs(cpu_entry=cpu_entry, worker_nodes=set()))
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_gpu_node_needs_untouched_gpus():
    # DCGM series cannot be attributed to node names, so a GPU node is only
    # acquirable when no GPU series anywhere ever met the variance floor.
    cpu_entry = {"per_node": [variance_node("gpu-0", 5.0)]}
    busy_gpu = {"per_node": [variance_node("10.130.2.24", 55.0, True)]}
    out = oic.evaluate_oversized(
        **oversized_kwargs(
            cpu_entry=cpu_entry,
            gpu_entry=busy_gpu,
            gpu_node_names={"gpu-0"},
            worker_nodes={"gpu-0"},
        )
    )
    assert out == {"result": False, "gpu_peak": None, "nodes": []}

    idle_gpu = {"peak": 0.0, "per_node": [variance_node("10.130.2.24", 0.0)]}
    out = oic.evaluate_oversized(
        **oversized_kwargs(
            cpu_entry=cpu_entry,
            gpu_entry=idle_gpu,
            gpu_node_names={"gpu-0"},
            worker_nodes={"gpu-0"},
        )
    )
    assert out["nodes"] == [{"node": "gpu-0", "cpu_peak": 5.0, "gpu_node": True}]
    # The acquittal's receipt: the hottest series cluster-wide, not the
    # node's own peak (which series cannot be attributed to).
    assert out["gpu_peak"] == 0.0


def test_oversized_gpu_series_meeting_the_floor_blocks_acquittal():
    # The VRAM case: utilization is a weak removal signal for GPU nodes -
    # VRAM capacity is often why the GPU was picked, and a memory-resident
    # workload computes little - so a series sitting well under the peak
    # threshold still blocks acquittal once it meets the variance floor.
    # At the floor exactly it has met it: the noise band is strictly below.
    cpu_entry = {"per_node": [variance_node("gpu-0", 5.0)]}
    for series_peak in (12.0, oic.GPU_VARIANCE_FLOOR):
        touched_gpu = {"peak": series_peak, "per_node": [variance_node("10.130.2.24", series_peak)]}
        out = oic.evaluate_oversized(
            **oversized_kwargs(
                cpu_entry=cpu_entry,
                gpu_entry=touched_gpu,
                gpu_node_names={"gpu-0"},
                worker_nodes={"gpu-0"},
            )
        )
        assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_gpu_node_without_gpu_data_is_not_acquitted():
    # GPU nodes present but no DCGM metrics: the GPU leg cannot be judged,
    # so the node is not named.
    cpu_entry = {"per_node": [variance_node("gpu-0", 5.0)]}
    out = oic.evaluate_oversized(
        **oversized_kwargs(
            cpu_entry=cpu_entry,
            gpu_entry={"result": "N/A", "reason": "no DCGM metrics"},
            gpu_node_names={"gpu-0"},
            worker_nodes={"gpu-0"},
        )
    )
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_gpu_pooled_fallback_entry_counts():
    # The aggregate fallback carries the pooled peak at the top level; the
    # floor check reads it the same way as the per-node peaks.
    cpu_entry = {"per_node": [variance_node("gpu-0", 5.0)]}
    out = oic.evaluate_oversized(
        **oversized_kwargs(
            cpu_entry=cpu_entry,
            gpu_entry={"peak": 3.0, "peak_exceeded": False},
            gpu_node_names={"gpu-0"},
            worker_nodes={"gpu-0"},
        )
    )
    assert out["nodes"] == [{"node": "gpu-0", "cpu_peak": 5.0, "gpu_node": True}]
    assert out["gpu_peak"] == 3.0


def test_worker_nodes_from_items_drops_role_labeled_nodes():
    items = [
        {"metadata": {"name": "master-0", "labels": {"node-role.kubernetes.io/master": ""}}},
        {"metadata": {"name": "cp-0", "labels": {"node-role.kubernetes.io/control-plane": ""}}},
        {"metadata": {"name": "infra-0", "labels": {"node-role.kubernetes.io/infra": ""}}},
        {"metadata": {"name": "worker-0", "labels": {"node-role.kubernetes.io/worker": ""}}},
        {"metadata": {"name": "bare-0", "labels": {}}},
    ]
    assert oic.worker_nodes_from_items(items) == {"worker-0", "bare-0"}


# === CRITERIA SUMMARY =======================================================


def test_criteria_summary_keeps_headline_fields_only():
    # result/value/threshold_ratio stay in `criteria`; the variance
    # diagnostics behind them are not exported.
    entry = {
        "result": "IDLE",
        "value": 12.48,
        "threshold_ratio": 0.46,
        "peak": 12.48,
        "baseline": 5.41,
        "source": "variance over 15m windows",
        "per_node": [{"node": "node-1", "peak": 12.48}],
    }
    assert oic.criteria_summary("cpu", entry) == {
        "result": "IDLE",
        "value": 12.48,
        "threshold_ratio": 0.46,
    }


def test_criteria_summary_operators_keeps_age_and_events():
    entry = {
        "result": "IDLE",
        "age_days": 8.1,
        "events": 0,
        "threshold_ratio": 0.86,
        "units": [{"name": "odh-operator"}],
        "namespaces": ["opendatahub"],
        "event_window_hours": 48,
    }
    assert oic.criteria_summary("operators", entry) == {
        "result": "IDLE",
        "age_days": 8.1,
        "events": 0,
        "threshold_ratio": 0.86,
    }


def test_criteria_summary_drops_failure_reasons():
    # A failure's reason is diagnostic detail, not a headline field.
    entry = {
        "result": "UNKNOWN",
        "age_days": 0,
        "reason": "pod listing failed (oc error or timeout)",
        "threshold_ratio": None,
    }
    assert oic.criteria_summary("operators", entry) == {
        "result": "UNKNOWN",
        "age_days": 0,
        "threshold_ratio": None,
    }


def test_criteria_summary_fields_cover_every_criterion():
    # A criterion added without summary fields would leak its diagnostics
    # into every verbatim `criteria` renderer.
    assert set(oic.CRITERIA_SUMMARY_FIELDS) == set(oic.CRITERIA_KEYS)
    for fields in oic.CRITERIA_SUMMARY_FIELDS.values():
        assert {"result", "value", "threshold_ratio"} <= fields


# === EXPORTS ================================================================


def sample_report() -> dict:
    """A report shaped exactly like run()'s, with quiet-IDLE values."""
    return {
        "timestamp": "2026-10-09T00:00:00+00:00",
        "timestamp_human": "Thu Oct 09 00:00:00 UTC 2026",
        "cluster": "https://api.test-cluster.example.com:6443",
        "status": "IDLE",
        "exit_code": 1,
        "configuration": {
            "time_window_minutes": 10080,
            "cpu_threshold": 15.0,
        },
        "criteria": {
            "total": 4,
            "met": 4,
            "threshold": 3,
            "cpu": {"result": "IDLE", "value": "2.00", "threshold_ratio": 0.13},
            "memory": {"result": "IDLE", "value": "10.00", "threshold_ratio": 0.29},
            "api_server": {"result": "IDLE", "value": "5.00", "threshold_ratio": 0.05},
            "gpu": {"result": "N/A", "value": None},
            "operators": {
                "result": "IDLE",
                "age_days": 12.0,
                "events": 0,
                "threshold_ratio": 0.58,
            },
            "driver": {
                "criterion": "operators",
                "label": "Operators",
                "threshold_ratio": 0.58,
                "quorum_driven": False,
            },
        },
        "gpu": {
            "has_gpu_nodes": False,
            "node_count": 0,
            "flavors": None,
            "node_age": None,
            "usage": {
                "cpu_current": None,
                "memory_current": None,
                "cpu_windowed": None,
                "memory_windowed": None,
            },
        },
        "oversized": {"result": False, "gpu_peak": None, "nodes": []},
    }


def test_export_csv_writes_header_once(tmp_path):
    path = tmp_path / "results.csv"
    oic.export_csv(sample_report(), path)
    oic.export_csv(sample_report(), path)
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert lines[0] == oic.CSV_HEADER
    assert lines[1] == lines[2]
    for row in lines[1:]:
        assert len(row.split(",")) == 24


def test_export_csv_renders_missing_values_as_na(tmp_path):
    path = tmp_path / "results.csv"
    oic.export_csv(sample_report(), path)
    row = path.read_text().splitlines()[1].split(",")
    assert row[2] == "IDLE"  # status
    assert row[9] == "N/A"  # gpu_result
    assert row[10] == "N/A"  # gpu_value (absent -> N/A, not empty)
    assert row[13] == "4"  # criteria_met
    assert row[14] == "4"  # total_criteria
    assert row[19] == "N/A"  # gpu_node_age
    assert row[20:24] == ["N/A"] * 4  # gpu usage columns


def test_export_csv_renders_unknown_gpu_presence(tmp_path):
    # A failed node listing leaves has_gpu_nodes/node_count as None; the CSV
    # row must say "unknown", not "false" - that cluster is not known to be
    # free of GPU nodes.
    report = sample_report()
    report["gpu"] = {
        "has_gpu_nodes": None,
        "node_count": None,
        "flavors": None,
        "node_age": None,
        "usage": {
            "cpu_current": None,
            "memory_current": None,
            "cpu_windowed": None,
            "memory_windowed": None,
        },
    }
    path = tmp_path / "results.csv"
    oic.export_csv(report, path)
    row = path.read_text().splitlines()[1].split(",")
    assert row[16] == "unknown"  # has_gpu_nodes
    assert row[17] == "N/A"  # gpu_node_count


def test_export_json_round_trips(tmp_path):
    path = tmp_path / "report.json"
    oic.export_json(sample_report(), path)
    data = json.loads(path.read_text())
    assert data["status"] == "IDLE"
    assert data["criteria"]["gpu"]["value"] is None
    # The sample's ratios are all finite; the infinite case has its own test.
    assert "Infinity" not in path.read_text()


def test_export_json_serializes_an_infinite_ratio(tmp_path):
    # The floorless zero-baseline fire reads as inf; Python's json emits and
    # reads back the non-standard Infinity token for it.
    report = sample_report()
    report["criteria"]["api_server"]["threshold_ratio"] = math.inf
    path = tmp_path / "report.json"
    oic.export_json(report, path)
    assert "Infinity" in path.read_text()
    assert json.loads(path.read_text())["criteria"]["api_server"]["threshold_ratio"] == math.inf


def test_base_config_matches_shipped_defaults():
    # Guards the test helpers against drifting away from the real defaults.
    cfg = base_config()
    assert cfg.time_window_minutes == oic.DEFAULT_TIME_WINDOW_MINUTES
    assert cfg.cpu_peak_threshold == oic.CPU_PEAK_THRESHOLD
    assert cfg.api_variance_ratio == oic.API_VARIANCE_RATIO
    assert cfg.operator_namespaces == oic.OPERATOR_NAMESPACES
    assert cfg.operator_exclude_prefixes == oic.OPERATOR_EXCLUDE_PREFIXES
    assert cfg.operator_event_hours == oic.OPERATOR_EVENT_WINDOW_HOURS
