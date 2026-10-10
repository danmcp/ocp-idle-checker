"""Shared helpers for the ocp_idle_check test suite.

Importable as a top-level module because conftest.py puts this directory
on sys.path.  Unit tests stub the Prometheus transport (FakeProm) and the
oc CLI's operator calls (FakeOc) at their Python seams; the end-to-end
test stubs Oc.run with strict argv matching and drives the real HTTP
client against a local server.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import ocp_idle_check as oic


class FakeProm:
    """In-memory stand-in for PrometheusClient; queries match exactly."""

    def __init__(
        self,
        instant: dict[str, float | None] | None = None,
        instant_all: dict[str, list[dict]] | None = None,
        ranges: dict[str, list[dict]] | None = None,
    ) -> None:
        self.instant = instant or {}
        self.instant_all = instant_all or {}
        self.ranges = ranges or {}
        self.range_calls: list[str] = []

    def query(self, promql: str) -> float | None:
        return self.instant.get(promql)

    def query_all(self, promql: str) -> list[dict] | None:
        return self.instant_all.get(promql)

    def query_range(self, promql: str, start_s: int, end_s: int, step_s: int = 900):
        self.range_calls.append(promql)
        return self.ranges.get(promql)


def make_range_series(node: str, values: list[str]) -> dict:
    """One query_range result series for a node label and sample values."""
    return {
        "metric": {"node": node, "instance": f"{node}:9100"},
        "values": [[1700000000 + i * 900, v] for i, v in enumerate(values)],
    }


def make_dcgm_series(values: list[str], gpu: str = "0", instance: str = "gpu-node:9400") -> dict:
    """One query_range result series for a DCGM utilization gauge.

    Real DCGM series carry an exporter `instance` label (host:port) and no
    `node` label, so per-node grouping keys on it; cards on the same node
    share the instance and pool into one node's windows.
    """
    return {
        "metric": {"gpu": gpu, "Hostname": "gpu-node", "instance": instance},
        "values": [[1700000000 + i * 900, v] for i, v in enumerate(values)],
    }


def base_config(**overrides) -> oic.Config:
    """A Config with the shipped defaults, quiet, plus test overrides."""
    values = dict(
        time_window_minutes=10080,
        cpu_idle_threshold=15.0,
        memory_idle_threshold=35.0,
        api_threshold=100.0,
        operator_age_days=7,
        operator_namespaces="opendatahub,redhat-ods-operator,redhat-ods-applications",
        operator_exclude_prefixes="openshift-,kube-,open-cluster-management-",
        operator_event_hours=48,
        event_history_minutes=60,
        check_ml_nodes=True,
        ml_node_pattern="p5|p4d|g5",
        cpu_peak_threshold=40.0,
        cpu_shape_ratio=2.0,
        gpu_peak_threshold=40.0,
        gpu_shape_ratio=2.0,
        api_spike_ratio=2.0,
        api_spike_floor=50.0,
        verbose=False,
        debug_probe=False,
        token="",
        csv_path=None,
        json_path=None,
    )
    values.update(overrides)
    return oic.Config(**values)


class FakeOc:
    """In-memory stand-in for the oc CLI's operator-criterion calls."""

    def __init__(
        self,
        pods: list[str] | None = None,
        replicasets: list[str] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.pod_rows = pods or []
        self.rs_rows = replicasets or []
        self.event_rows = events or []
        self.calls: list[str] = []

    def pods_all(self) -> list[str]:
        self.calls.append("pods")
        return self.pod_rows

    def replicasets_all(self) -> list[str]:
        self.calls.append("replicasets")
        return self.rs_rows

    def events_all_rows(self) -> list[str]:
        self.calls.append("events")
        return self.event_rows


# Row builders mirroring the module's jsonpath shapes (PODS_ALL_JSONPATH,
# REPLICASETS_ALL_JSONPATH, EVENTS_ALL_JSONPATH); tests key their FakeOc
# data on these so a changed column order fails loudly instead of silently
# producing garbage ages.


def ts_before(now: datetime, **delta) -> str:
    """A Kubernetes timestamp `delta` (days=..., hours=...) before `now`."""
    return (now - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pod_row(
    namespace: str, pod: str, owner_kind: str = "", owner_name: str = "", created: str = ""
) -> str:
    """One `oc get pods -A` row: namespace, pod, owner kind, owner name, created."""
    return f"{namespace}\t{pod}\t{owner_kind}\t{owner_name}\t{created}"


def rs_row(namespace: str, name: str, created: str) -> str:
    """One `oc get rs -A` row: namespace, name, created."""
    return f"{namespace}\t{name}\t{created}"


def event_row(
    namespace: str,
    *,
    now: datetime,
    hours: float = 1.0,
    type_: str = "Normal",
    reason: str = "Pulled",
    kind: str = "Pod",
    involved: str = "odh-xyz",
    message: str = "quiet",
    timestamp: str | None = None,
) -> str:
    """One `oc get events -A` row; `timestamp=""` yields no timestamps."""
    ts = timestamp if timestamp is not None else ts_before(now, hours=hours)
    return f"{namespace}\t{type_}\t{reason}\t{kind}\t{involved}\t{message}\t{ts}\t{ts}"


# Expected query strings, mirroring the f-strings the module builds.  Tests
# key their FakeProm / prom_server data on these so any change to a query
# fails loudly instead of silently returning no data.


def legacy_cpu_query(minutes: int) -> str:
    return f'(1 - avg(rate(node_cpu_seconds_total{{mode="idle"}}[{minutes}m]))) * 100'


def memory_window_query(minutes: int) -> str:
    return (
        "(1 - avg_over_time((sum(node_memory_MemAvailable_bytes) / "
        f"sum(node_memory_MemTotal_bytes))[{minutes}m:])) * 100"
    )


def api_avg_query(minutes: int) -> str:
    return f"sum(rate(apiserver_request_total[{minutes}m]))"


def gpu_max_query(minutes: int) -> str:
    return f"max_over_time(DCGM_FI_DEV_GPU_UTIL[{minutes}m])"


def gpu_avg_query(minutes: int) -> str:
    return f"avg_over_time(DCGM_FI_DEV_GPU_UTIL[{minutes}m])"


def node_cpu_window_query(nodes: list[str], minutes: int) -> str:
    """The batched per-node windowed CPU query (one round trip for all nodes)."""
    pattern = "|".join(f"{re.escape(node)}.*" for node in nodes)
    return (
        '(1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle",'
        f'instance=~"{pattern}"}}[{minutes}m]))) * 100'
    )


def node_mem_window_query(nodes: list[str], minutes: int) -> str:
    """The batched per-node windowed memory query (one round trip for all nodes)."""
    pattern = "|".join(f"{re.escape(node)}.*" for node in nodes)
    return (
        "(1 - avg_over_time((avg by (instance) (node_memory_MemAvailable_bytes"
        f'{{instance=~"{pattern}"}}) / avg by (instance) (node_memory_MemTotal_bytes'
        f'{{instance=~"{pattern}"}}))[{minutes}m:])) * 100'
    )


def prom_instant_json(value: str) -> str:
    """A successful instant-query response body with a single series."""
    return (
        '{"status":"success","data":{"resultType":"vector","result":'
        f'[{{"metric":{{}},"value":[1700000000,"{value}"]}}]}}}}'
    )


def prom_instant_series_json(series: dict[str, str]) -> str:
    """A successful instant-query response with one series per instance label."""
    result = [
        {"metric": {"instance": instance}, "value": [1700000000, value]}
        for instance, value in series.items()
    ]
    return json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}})


def prom_range_json(series: list[dict]) -> str:
    """A successful query_range response body."""
    return json.dumps({"status": "success", "data": {"resultType": "matrix", "result": series}})
