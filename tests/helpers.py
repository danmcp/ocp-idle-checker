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


def make_dcgm_series(values: list[str], gpu: str = "0") -> dict:
    """One query_range result series for a DCGM utilization gauge."""
    return {
        "metric": {"gpu": gpu, "Hostname": "gpu-node"},
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
        event_history_minutes=60,
        check_ml_nodes=True,
        ml_node_pattern="p5|p4d|g5",
        cpu_peak_threshold=40.0,
        cpu_shape_ratio=2.0,
        cpu_shape_floor=20.0,
        gpu_peak_threshold=40.0,
        gpu_shape_ratio=2.0,
        gpu_shape_floor=20.0,
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
        existing: set[str] | None = None,
        pods: dict[str, list[str]] | None = None,
        events: dict[str, list[str]] | None = None,
    ) -> None:
        self.existing = existing or set()
        self.pods = pods or {}
        self.events = events or {}
        self.calls: list[tuple[str, str]] = []

    def namespace_exists(self, namespace: str) -> bool:
        self.calls.append(("namespace", namespace))
        return namespace in self.existing

    def pods_in(self, namespace: str) -> list[str]:
        self.calls.append(("pods", namespace))
        return self.pods.get(namespace, [])

    def events_in(self, namespace: str) -> list[str]:
        self.calls.append(("events", namespace))
        return self.events.get(namespace, [])


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


def node_cpu_window_query(node: str, minutes: int) -> str:
    pattern = f"{re.escape(node)}.*"
    return (
        '(1 - avg(rate(node_cpu_seconds_total{mode="idle",'
        f'instance=~"{pattern}"}}[{minutes}m]))) * 100'
    )


def node_mem_window_query(node: str, minutes: int) -> str:
    pattern = f"{re.escape(node)}.*"
    return (
        "(1 - avg_over_time((avg(node_memory_MemAvailable_bytes"
        f'{{instance=~"{pattern}"}}) / avg(node_memory_MemTotal_bytes'
        f'{{instance=~"{pattern}"}}))[{minutes}m:])) * 100'
    )


def prom_instant_json(value: str) -> str:
    """A successful instant-query response body with a single series."""
    return (
        '{"status":"success","data":{"resultType":"vector","result":'
        f'[{{"metric":{{}},"value":[1700000000,"{value}"]}}]}}}}'
    )


def prom_range_json(series: list[dict]) -> str:
    """A successful query_range response body."""
    return json.dumps({"status": "success", "data": {"resultType": "matrix", "result": series}})
