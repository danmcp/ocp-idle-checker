#!/usr/bin/env python3
"""OpenShift cluster idle detection.

Python port of ocp-idle-check.sh (stdlib only).  ocp-idle-check.sh is a thin
bash shim that execs this file, so existing callers - the Jenkins job
("bash ocp-idle-check.sh ..."), cluster-monitor's vendored copy, and the
README examples - keep working unchanged.

A cluster is IDLE when at least 80% of the applicable criteria vote IDLE:

  cpu        variance rule on per-node 15-minute CPU averages taken from
             the Prometheus query_range matrix, evaluated per node - each
             node's windows against that node's own median, so a steady busy
             node pooled with a quiet sibling is two steady nodes, not a
             burst: ACTIVE when any node's window averaged above the peak
             threshold, or when a node's window peak clears a
             baseline-scaled required peak anchored at the peak threshold.
             Falls back to the legacy window-average rule when the
             matrix is unavailable.  An instant reading above the idle
             threshold can still override an IDLE result (one-directional,
             as in the bash version).
  memory     legacy window-average rule with the same one-directional
             instant override.
  api_server legacy window-average request rate against the threshold, plus
             a variance detector: any 15-minute window clearing a
             baseline-scaled required peak (anchored at the request
             threshold, floored at an absolute request rate) also counts
             as ACTIVE.
  gpu        the same per-node variance rule as cpu, on DCGM GPU
             utilization (DCGM_FI_DEV_GPU_UTIL).  N/A (not counted) on
             clusters without GPU nodes or without DCGM metrics.
  operators  controller age and reconciliation events across all non-platform
             namespaces: ACTIVE when the median controller is younger than
             the age threshold (controllers are aged by their ReplicaSet,
             so a node drain does not reset the clock) or when
             reconciliation events inside the event window reach the
             threshold.  N/A (not counted) when no controller pods exist
             anywhere on the cluster.

The variance parameters (peak threshold, ratio; the API rule also has an
absolute floor) are fleet calibration knobs exposed as command-line
flags; the defaults are provisional.  All three variance rules share one
law: the peak must clear ratio_threshold times the baseline when the
baseline sits at half the criterion's normal threshold (peak threshold
for CPU/GPU, request threshold for API) - where the required peak equals
that threshold - and the multiple grows with the cube root of the
shortfall below it, so quiet baselines need proportionally bigger bursts
before they count as bursty.  A floor (percent for CPU/GPU, req/s for
API) is the whole requirement on a zero baseline.  "Baseline" is the
median of the 15-minute windows on the query_range path and the window
mean on the aggregate fallback path.

The five criteria run concurrently in a thread pool (they are independent
I/O-bound checks); the log output is consumed in a fixed order so it reads
exactly like the old sequential run.  The informational sections avoid
per-node fan-out: per-node instant usage comes from the single cached
`oc adm top nodes` snapshot, and windowed per-node usage from one batched
Prometheus query per metric.

Exit codes:
  0 = cluster is ACTIVE
  1 = cluster is IDLE
  2 = error (cannot determine state)

OCP_IDLE_PROMETHEUS_URL overrides the thanos-querier route lookup; it exists
for hermetic tests and manual debugging.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# === CONFIGURATION DEFAULTS ================================================

# Matches the effective value every real caller passes (-w 10080); the old
# bash default of 10 minutes made the windowed checks near-instantaneous and
# would leave the variance rule with a single data point.
DEFAULT_TIME_WINDOW_MINUTES = 10080  # 7 days

CPU_IDLE_THRESHOLD = 15.0  # percent; instant override + legacy fallback
MEMORY_IDLE_THRESHOLD = 35.0  # percent
APISERVER_IDLE_THRESHOLD = 100.0  # requests/sec
OPERATOR_IDLE_AGE_DAYS = 7
OPERATOR_NAMESPACES = "opendatahub,redhat-ods-operator,redhat-ods-applications"
OPERATOR_EXCLUDE_PREFIXES = "openshift-,kube-,open-cluster-management-"
OPERATOR_EVENT_THRESHOLD = 5  # reconciliation events within the window
OPERATOR_EVENT_WINDOW_HOURS = 48
EVENT_TIME_MINUTES = 60
ML_NODE_PATTERN = "p5|p4d|g5"

# Variance rule defaults - fleet-calibration knobs, ideal values TBD.
CPU_PEAK_THRESHOLD = 30.0  # percent; any 15-min window above this = ACTIVE
CPU_VARIANCE_RATIO = 2.0  # burst multiple required at half the peak threshold
CPU_VARIANCE_FLOOR = 1.0  # percent; minimum peak the burst rule demands on a zero baseline
GPU_PEAK_THRESHOLD = 40.0
GPU_VARIANCE_RATIO = 2.0  # burst multiple required at half the peak threshold
GPU_VARIANCE_FLOOR = 5.0  # percent; DCGM's idle noise tail reads 1-4% on a GPU doing nothing
API_VARIANCE_RATIO = 2.0  # burst multiple required at half the request threshold
API_VARIANCE_FLOOR = 50.0  # requests/sec; minimum peak the rule demands on a quiet baseline

VARIANCE_WINDOW_MINUTES = 15  # the "15 min period" of the variance rule
STEP_SECONDS = 900  # query_range step: one point per 15-min window

# Timeouts (seconds), matching the bash original.
OC_TIMEOUT = 10
ROUTE_TIMEOUT = 5
QUERY_TIMEOUT = 60
RANGE_TIMEOUT = 180

PROM_URL_OVERRIDE = "OCP_IDLE_PROMETHEUS_URL"

# PromQL templates.  The `by (node, instance)` on the CPU range query is
# required: OCP node series carry no `node` label, only `instance`.
CPU_RANGE_QUERY = (
    '(1 - avg by (node, instance) (rate(node_cpu_seconds_total{mode="idle"}'
    f"[{VARIANCE_WINDOW_MINUTES}m]))) * 100"
)
GPU_RANGE_QUERY = f"avg_over_time(DCGM_FI_DEV_GPU_UTIL[{VARIANCE_WINDOW_MINUTES}m])"
API_RANGE_QUERY = f"sum(rate(apiserver_request_total[{VARIANCE_WINDOW_MINUTES}m]))"

POD_RE = re.compile(r"controller-manager|operator|dashboard")
OPERATOR_EVENT_RE = re.compile(r"reconcil|created|updated|scaled", re.IGNORECASE)
RECENT_ACTIVITY_RE = re.compile(r"Pod|Deployment|ReplicaSet|Job")

# Events about workload machinery (a pod being scheduled, a replica set
# scaling) are the noise a node drain manufactures by the dozen; the age
# half of the operators rule already treats drains as non-events, so the
# event half must not count them either.
OPERATOR_WORKLOAD_KINDS = frozenset(
    {"Pod", "ReplicaSet", "Deployment", "DaemonSet", "StatefulSet", "Job", "CronJob", "Node"}
)

# Cluster-wide listings for the operators criterion, fetched as jsonpath
# with explicit tab separators: custom-columns collapses empty fields into
# space runs, which would shift the positional fields of a bare-pod row.
PODS_ALL_JSONPATH = (
    'jsonpath={range .items[*]}{.metadata.namespace}{"\\t"}{.metadata.name}{"\\t"}'
    '{.metadata.ownerReferences[0].kind}{"\\t"}{.metadata.ownerReferences[0].name}{"\\t"}'
    '{.metadata.creationTimestamp}{"\\n"}{end}'
)
REPLICASETS_ALL_JSONPATH = (
    'jsonpath={range .items[*]}{.metadata.namespace}{"\\t"}{.metadata.name}{"\\t"}'
    '{.metadata.creationTimestamp}{"\\n"}{end}'
)
EVENTS_ALL_JSONPATH = (
    'jsonpath={range .items[*]}{.metadata.namespace}{"\\t"}{.type}{"\\t"}{.reason}{"\\t"}'
    '{.involvedObject.kind}{"\\t"}{.involvedObject.name}{"\\t"}{.message}{"\\t"}'
    '{.lastTimestamp}{"\\t"}{.firstTimestamp}{"\\n"}{end}'
)

_UNVERIFIED_CTX = ssl.create_default_context()
_UNVERIFIED_CTX.check_hostname = False
_UNVERIFIED_CTX.verify_mode = ssl.CERT_NONE

_VERBOSE = True


# === LOGGING ===============================================================

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
BLUE = "\033[0;34m"
NC = "\033[0m"


def set_verbose(verbose: bool) -> None:
    global _VERBOSE
    _VERBOSE = verbose


def _log(color: str, tag: str, message: str) -> None:
    if not _VERBOSE:
        return
    print(f"{color}[{tag}]{NC} {message}", file=sys.stderr)


def log_info(message: str) -> None:
    _log(BLUE, "INFO", message)


def log_ok(message: str) -> None:
    _log(GREEN, "OK", message)


def log_warn(message: str) -> None:
    _log(YELLOW, "WARN", message)


def log_error(message: str) -> None:
    # Errors are printed even in quiet mode; they explain a non-zero exit.
    print(f"{RED}[ERROR]{NC} {message}", file=sys.stderr)


# === CONFIGURATION =========================================================


@dataclass(frozen=True)
class Config:
    """All knobs, as parsed from the command line."""

    time_window_minutes: int
    cpu_idle_threshold: float
    memory_idle_threshold: float
    api_threshold: float
    operator_age_days: int
    operator_namespaces: str
    operator_exclude_prefixes: str
    operator_event_hours: int
    event_history_minutes: int
    check_ml_nodes: bool
    ml_node_pattern: str
    cpu_peak_threshold: float
    cpu_variance_ratio: float
    gpu_peak_threshold: float
    gpu_variance_ratio: float
    api_variance_ratio: float
    api_variance_floor: float
    verbose: bool
    debug_probe: bool
    token: str
    csv_path: Path | None
    json_path: Path | None


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        prog="ocp-idle-check.sh",
        description="Detect whether an OpenShift cluster is idle.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "-w",
        "--window",
        type=int,
        default=DEFAULT_TIME_WINDOW_MINUTES,
        metavar="MINUTES",
        help="time window for metric averages (default: %(default)s = 7 days)",
    )
    parser.add_argument(
        "-c",
        "--cpu-threshold",
        type=float,
        default=CPU_IDLE_THRESHOLD,
        metavar="N",
        help="CPU idle threshold in percent (default: %(default)s)",
    )
    parser.add_argument(
        "-m",
        "--mem-threshold",
        type=float,
        default=MEMORY_IDLE_THRESHOLD,
        metavar="N",
        help="memory idle threshold in percent (default: %(default)s)",
    )
    parser.add_argument(
        "-a",
        "--api-threshold",
        type=float,
        default=APISERVER_IDLE_THRESHOLD,
        metavar="N",
        help="API server idle threshold in requests/sec (default: %(default)s)",
    )
    parser.add_argument(
        "-e",
        "--events",
        type=int,
        default=EVENT_TIME_MINUTES,
        metavar="MINUTES",
        help="event history window for informational output (default: %(default)s)",
    )
    parser.add_argument(
        "-o",
        "--operator-age",
        type=int,
        default=OPERATOR_IDLE_AGE_DAYS,
        metavar="DAYS",
        help="operator idle age threshold in days (default: %(default)s)",
    )
    parser.add_argument(
        "--operator-namespaces",
        default=OPERATOR_NAMESPACES,
        metavar="NS",
        help="comma-separated namespaces always included in the operator scan "
        "(the scan covers every namespace except the excluded prefixes)",
    )
    parser.add_argument(
        "--operator-exclude-prefixes",
        default=OPERATOR_EXCLUDE_PREFIXES,
        metavar="PREFIX,...",
        help="namespace prefixes skipped by the operator scan - the platform's own "
        "controllers (default: %(default)s)",
    )
    parser.add_argument(
        "--operator-event-hours",
        type=int,
        default=OPERATOR_EVENT_WINDOW_HOURS,
        metavar="HOURS",
        help="operator reconciliation events must be newer than this to count (default: %(default)s)",
    )
    parser.add_argument(
        "--csv", type=Path, metavar="FILE", help="append a result row to a CSV file"
    )
    parser.add_argument("--json", type=Path, metavar="FILE", help="write a JSON report")
    parser.add_argument("--token", default="", metavar="TOKEN", help="Prometheus bearer token")
    parser.add_argument(
        "--cpu-peak-threshold",
        type=float,
        default=CPU_PEAK_THRESHOLD,
        metavar="N",
        help="CPU variance rule: any 15-min window above this percent = ACTIVE (default: %(default)s)",
    )
    parser.add_argument(
        "--cpu-variance-ratio",
        type=float,
        default=CPU_VARIANCE_RATIO,
        metavar="N",
        help="CPU variance rule: burst multiple the peak must clear when a node median is at half the peak threshold; quieter medians require more (default: %(default)s)",
    )
    parser.add_argument(
        "--gpu-peak-threshold",
        type=float,
        default=GPU_PEAK_THRESHOLD,
        metavar="N",
        help="GPU variance rule: any 15-min window above this percent = ACTIVE (default: %(default)s)",
    )
    parser.add_argument(
        "--gpu-variance-ratio",
        type=float,
        default=GPU_VARIANCE_RATIO,
        metavar="N",
        help="GPU variance rule: burst multiple the peak must clear when a node median is at half the peak threshold; quieter medians require more (default: %(default)s)",
    )
    parser.add_argument(
        "--api-variance-ratio",
        type=float,
        default=API_VARIANCE_RATIO,
        metavar="N",
        help="API variance rule: burst multiple the peak must clear when the median window is at half the request threshold; quieter medians require more (default: %(default)s)",
    )
    parser.add_argument(
        "--api-variance-floor",
        type=float,
        default=API_VARIANCE_FLOOR,
        metavar="N",
        help="API variance rule: minimum peak in req/s the rule demands on a quiet median (default: %(default)s)",
    )
    parser.add_argument(
        "--debug-probe",
        action="store_true",
        help="accepted for compatibility; criteria detail is always exported now",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="minimal output")
    parser.add_argument("--no-ml-check", action="store_true", help="skip the ML node check")
    args = parser.parse_args(argv)

    return Config(
        time_window_minutes=args.window,
        cpu_idle_threshold=args.cpu_threshold,
        memory_idle_threshold=args.mem_threshold,
        api_threshold=args.api_threshold,
        operator_age_days=args.operator_age,
        operator_namespaces=args.operator_namespaces,
        operator_exclude_prefixes=args.operator_exclude_prefixes,
        operator_event_hours=args.operator_event_hours,
        event_history_minutes=args.events,
        check_ml_nodes=not args.no_ml_check,
        ml_node_pattern=ML_NODE_PATTERN,
        cpu_peak_threshold=args.cpu_peak_threshold,
        cpu_variance_ratio=args.cpu_variance_ratio,
        gpu_peak_threshold=args.gpu_peak_threshold,
        gpu_variance_ratio=args.gpu_variance_ratio,
        api_variance_ratio=args.api_variance_ratio,
        api_variance_floor=args.api_variance_floor,
        verbose=not args.quiet,
        debug_probe=args.debug_probe,
        token=args.token,
        csv_path=args.csv,
        json_path=args.json,
    )


# === OC CLI WRAPPER ========================================================


class Oc:
    """Thin wrapper around the oc CLI with per-command result caching."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._nodes: list[dict[str, Any]] | None = None
        self._top_lines: list[str] | None = None
        self._top_map: dict[str, tuple[float, float]] | None = None
        self._event_rows: list[str] | None = None

    def run(self, args: list[str], timeout: float = OC_TIMEOUT) -> str | None:
        """Run oc; return stdout on success, None on failure or timeout."""
        try:
            proc = subprocess.run(["oc", *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if self.verbose:
                log_warn(f"oc {' '.join(args)} failed: {exc}")
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def logged_in(self) -> bool:
        return self.run(["whoami"]) is not None

    def whoami_server(self) -> str:
        out = self.run(["whoami", "--show-server"])
        return (out or "").strip() or "Unknown"

    def whoami_token(self) -> str | None:
        out = self.run(["whoami", "-t"])
        return out.strip() or None if out else None

    def create_prometheus_token(self) -> str | None:
        out = self.run(
            ["create", "token", "prometheus-k8s", "-n", "openshift-monitoring", "--duration=10m"]
        )
        return out.strip() or None if out else None

    def route_host(self) -> str | None:
        out = self.run(
            [
                "get",
                "route",
                "thanos-querier",
                "-n",
                "openshift-monitoring",
                "-o",
                "jsonpath={.spec.host}",
            ],
            timeout=ROUTE_TIMEOUT,
        )
        return out.strip() or None if out else None

    def top_nodes(self) -> list[str]:
        """Cached `oc adm top nodes --no-headers` lines."""
        if self._top_lines is None:
            out = self.run(["adm", "top", "nodes", "--no-headers"])
            self._top_lines = [ln for ln in (out or "").splitlines() if ln.strip()]
        return self._top_lines

    def top_node_map(self) -> dict[str, tuple[float, float]]:
        """Per-node (CPU%, MEM%) parsed from the cached cluster-wide snapshot.

        Replaces per-node `oc adm top node <name>` spawns — one snapshot
        serves every consumer.
        """
        if self._top_map is None:
            self._top_map = node_top_percentages(self.top_nodes())
        return self._top_map

    def nodes(self) -> list[dict[str, Any]]:
        """Cached `oc get nodes -o json` items."""
        if self._nodes is None:
            out = self.run(["get", "nodes", "-o", "json"])
            try:
                self._nodes = json.loads(out or "{}").get("items", [])
            except json.JSONDecodeError:
                self._nodes = []
        return self._nodes

    def pods_all(self) -> list[str]:
        """Cluster-wide pod rows: namespace, name, owner kind, owner name, created."""
        out = self.run(["get", "pods", "-A", "-o", PODS_ALL_JSONPATH])
        return [ln for ln in (out or "").splitlines() if ln.strip()]

    def replicasets_all(self) -> list[str]:
        """Cluster-wide ReplicaSet rows: namespace, name, created."""
        out = self.run(["get", "rs", "-A", "-o", REPLICASETS_ALL_JSONPATH])
        return [ln for ln in (out or "").splitlines() if ln.strip()]

    def events_all_rows(self) -> list[str]:
        """Cached cluster-wide event rows: namespace, type, reason, object
        kind, object name, message, lastTimestamp, firstTimestamp."""
        if self._event_rows is None:
            out = self.run(["get", "events", "-A", "-o", EVENTS_ALL_JSONPATH])
            self._event_rows = [ln for ln in (out or "").splitlines() if ln.strip()]
        return self._event_rows

    def machines(self) -> list[dict[str, Any]]:
        out = self.run(["get", "machines", "-n", "openshift-machine-api", "-o", "json"])
        try:
            return json.loads(out or "{}").get("items", [])
        except json.JSONDecodeError:
            return []


# === PROMETHEUS CLIENT =====================================================


class PrometheusClient:
    """Minimal Prometheus/Thanos client over urllib (unverified TLS, like
    the bash script's `curl -sk`)."""

    def __init__(self, oc: Oc, token: str = "", verbose: bool = False) -> None:
        self._oc = oc
        self.verbose = verbose
        self._base: str | None = None
        self._base_resolved = False
        self._token: str | None = token or None
        self._token_resolved = bool(token)
        # The five criteria run in a thread pool; the lazy endpoint/token
        # resolution below must happen exactly once for all of them.
        self._init_lock = threading.Lock()

    def _base_url(self) -> str | None:
        with self._init_lock:
            if not self._base_resolved:
                self._base_resolved = True
                override = os.environ.get(PROM_URL_OVERRIDE, "")
                if override:
                    self._base = override.rstrip("/")
                else:
                    host = self._oc.route_host()
                    self._base = f"https://{host}" if host else None
            return self._base

    def _bearer(self) -> str:
        with self._init_lock:
            if not self._token_resolved:
                self._token_resolved = True
                self._token = self._oc.whoami_token() or self._oc.create_prometheus_token()
            return self._token or ""

    def _get_json(self, path: str, params: dict[str, str], timeout: float) -> dict[str, Any] | None:
        base = self._base_url()
        if base is None:
            return None
        url = f"{base}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._bearer()}"})
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=_UNVERIFIED_CTX
            ) as response:
                data = json.loads(response.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if self.verbose:
                log_warn(f"Prometheus request failed: {exc}")
            return None
        if not isinstance(data, dict) or data.get("status") != "success":
            if self.verbose:
                log_warn(f"Prometheus query was not successful: {path}")
            return None
        return data

    def query(self, promql: str) -> float | None:
        """Instant query; returns the first series' value or None."""
        data = self._get_json("/api/v1/query", {"query": promql}, QUERY_TIMEOUT)
        if data is None:
            return None
        result = data.get("data", {}).get("result", [])
        if not result:
            return None
        try:
            value = float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def query_all(self, promql: str) -> list[dict[str, Any]] | None:
        """Instant query; returns every result series (or None on failure)."""
        data = self._get_json("/api/v1/query", {"query": promql}, QUERY_TIMEOUT)
        if data is None:
            return None
        return data.get("data", {}).get("result", [])

    def query_range(
        self, promql: str, start_s: int, end_s: int, step_s: int = STEP_SECONDS
    ) -> list[dict[str, Any]] | None:
        """Range query; returns every result series with its `values` matrix."""
        data = self._get_json(
            "/api/v1/query_range",
            {"query": promql, "start": str(start_s), "end": str(end_s), "step": str(step_s)},
            RANGE_TIMEOUT,
        )
        if data is None:
            return None
        return data.get("data", {}).get("result", [])


def _to_floats(raw_values: list[Any]) -> list[float]:
    """Convert Prometheus sample values, dropping non-finite garbage."""
    out: list[float] = []
    for raw in raw_values:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out.append(value)
    return out


def _series_points(series: list[dict[str, Any]] | None) -> list[float]:
    """Flatten a query_range matrix into a pooled list of sample values."""
    if not series:
        return []
    points: list[float] = []
    for entry in series:
        points.extend(_to_floats([v for _, v in entry.get("values", [])]))
    return points


def _series_node(metric: dict[str, Any]) -> str:
    """Node name for a series: the `node` label, else `instance` sans port."""
    node = str(metric.get("node") or metric.get("instance") or "")
    return re.sub(r":[0-9]+$", "", node)


# === NODE DATA =============================================================


@dataclass(frozen=True)
class GpuNode:
    name: str
    vendor: str
    gpu_count: int
    instance_type: str


def gpu_nodes_from_items(items: list[dict[str, Any]]) -> list[GpuNode]:
    """Nodes with nvidia.com/gpu or amd.com/gpu capacity (as in the bash
    script's get_all_gpu_node_data)."""
    gpu_nodes: list[GpuNode] = []
    for item in items:
        meta = item.get("metadata", {})
        capacity = item.get("status", {}).get("capacity", {})
        labels = meta.get("labels", {})
        nvidia = capacity.get("nvidia.com/gpu")
        amd = capacity.get("amd.com/gpu")
        if nvidia is None and amd is None:
            continue
        if nvidia is not None:
            vendor, count = "NVIDIA", nvidia
        else:
            vendor, count = "AMD", amd
        try:
            gpu_count = int(count)
        except (TypeError, ValueError):
            gpu_count = 0
        gpu_nodes.append(
            GpuNode(
                name=str(meta.get("name", "")),
                vendor=vendor,
                gpu_count=gpu_count,
                instance_type=str(labels.get("node.kubernetes.io/instance-type", "")),
            )
        )
    return gpu_nodes


def node_instance_type(item: dict[str, Any]) -> str:
    labels = item.get("metadata", {}).get("labels", {})
    return str(labels.get("node.kubernetes.io/instance-type", ""))


def cluster_short_name(server: str) -> str:
    """First DNS label after "api." in the server URL (for machine naming)."""
    match = re.search(r"api\.([^:]+)", server)
    if not match:
        return ""
    return match.group(1).split(".")[0]


def instant_averages(top_lines: list[str]) -> tuple[float | None, float | None]:
    """Average CPU% (column 3) and MEM% (column 5) from `oc adm top nodes`."""
    cpu_values: list[float] = []
    mem_values: list[float] = []
    for line in top_lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        try:
            cpu_values.append(float(fields[2].rstrip("%")))
            mem_values.append(float(fields[4].rstrip("%")))
        except ValueError:
            continue
    cpu_avg = sum(cpu_values) / len(cpu_values) if cpu_values else None
    mem_avg = sum(mem_values) / len(mem_values) if mem_values else None
    return cpu_avg, mem_avg


def node_top_percentages(top_lines: list[str]) -> dict[str, tuple[float, float]]:
    """Per-node (CPU%, MEM%) from `oc adm top nodes` lines, keyed by node name.

    Parses the same columns as `instant_averages` (CPU% = column 3,
    MEM% = column 5); malformed rows are skipped.
    """
    stats: dict[str, tuple[float, float]] = {}
    for line in top_lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        try:
            stats[fields[0]] = (float(fields[2].rstrip("%")), float(fields[4].rstrip("%")))
        except ValueError:
            continue
    return stats


def fmt2(value: float | None) -> str | None:
    """Two-decimal string, matching the bash script's printf %.2f output."""
    return f"{value:.2f}" if value is not None else None


# === VARIANCE RULE ==========================================================


@dataclass(frozen=True)
class VarianceStats:
    """Result of the peak + variance evaluation."""

    points: int
    peak: float
    baseline: float  # median of windows (query_range) or window mean (fallback)
    ratio: float | None  # peak / baseline; None when the baseline is zero
    required_ratio: (
        float | None
    )  # scaled multiple the peak must clear; None when the baseline is zero
    required_peak: (
        float | None
    )  # absolute peak the variance rule demands; None only when there is no data
    peak_exceeded: bool
    variance_exceeded: bool
    node: str = ""  # set by per-node evaluation; empty when not node-scoped

    @property
    def active(self) -> bool:
        return self.peak_exceeded or self.variance_exceeded


def variance_required_peak(
    baseline: float, ratio_threshold: float, ratio_scale: float, floor: float
) -> float:
    """Absolute window peak the variance rule demands at a baseline.

    `ratio_scale` is the criterion's normal threshold - the peak threshold
    for CPU/GPU, the request threshold for the API server.  The multiple the
    peak must clear is ratio_threshold when the baseline sits at half the
    scale - where the required peak equals the scale, so for CPU/GPU the
    two branches of the rule meet - and grows with the cube root of the
    shortfall below that: roughly 5x at a 1% median against a 30% scale, so
    a workload that idles at 1% and loads to 10% is caught.  Above half the
    scale the multiple is pinned at ratio_threshold, where for CPU/GPU the
    peak rule subsumes this one.  `floor` is the minimum required peak, and
    stands in for the whole curve on a zero baseline: the unbounded ratio
    the rule once applied there fired on DCGM's 1-4% idle noise tail.
    """
    if baseline <= 0:
        return floor
    half = ratio_scale / 2.0
    multiple = ratio_threshold * max(1.0, (half / baseline) ** (1.0 / 3.0))
    return max(floor, multiple * baseline)


def evaluate_variance(
    points: list[float],
    *,
    peak_threshold: float | None = None,
    ratio_threshold: float,
    floor: float,
    ratio_scale: float | None = None,
    node: str = "",
) -> VarianceStats:
    """The variance rule: ACTIVE when any window exceeded `peak_threshold`
    (when given), or when the peak clears the baseline-scaled required peak.

    `ratio_scale` anchors the required peak (see variance_required_peak) and
    defaults to `peak_threshold`; one of the two must be given.  `floor` is
    the minimum required peak and the whole requirement on a zero baseline.
    """
    scale = peak_threshold if ratio_scale is None else ratio_scale
    if scale is None:
        raise ValueError("ratio_scale is required when peak_threshold is omitted")
    if not points:
        return VarianceStats(0, 0.0, 0.0, None, None, None, False, False)
    peak = max(points)
    baseline = statistics.median(points)
    required_peak = variance_required_peak(baseline, ratio_threshold, scale, floor)
    ratio = peak / baseline if baseline > 0 else None
    required_ratio = required_peak / baseline if baseline > 0 else None
    peak_exceeded = peak_threshold is not None and peak > peak_threshold
    variance_exceeded = peak > required_peak
    return VarianceStats(
        len(points),
        peak,
        baseline,
        ratio,
        required_ratio,
        required_peak,
        peak_exceeded,
        variance_exceeded,
        node,
    )


def evaluate_variance_by_node(
    series: list[dict[str, Any]] | None,
    *,
    peak_threshold: float,
    ratio_threshold: float,
    floor: float,
    ratio_scale: float | None = None,
    live_nodes: set[str] | None = None,
) -> list[VarianceStats]:
    """Evaluate the variance rule once per node, each against its own median.

    Grouping matters because a pooled median is dragged by cross-node
    heterogeneity: a steady busy node pooled with a steady quiet sibling
    lands the median in the valley between the two modes, and the busy
    node's peak over that valley reads as a burst.  Judged per node, both
    are steady.  `live_nodes`, when given, drops series from nodes that no
    longer exist (their history is real but says nothing about the cluster
    as it stands); the GPU criterion deliberately omits it, since a deleted
    node's samples are still activity.  Series with neither a `node` nor an
    `instance` label share the empty node name and pool as one group.
    Returns stats sorted by node name for deterministic output.
    """
    if not series:
        return []
    grouped: dict[str, list[float]] = {}
    for s in series:
        node = _series_node(s.get("metric", {}))
        if live_nodes is not None and node not in live_nodes:
            continue
        grouped.setdefault(node, []).extend(_to_floats([v for _, v in s.get("values", [])]))
    return [
        evaluate_variance(
            points,
            peak_threshold=peak_threshold,
            ratio_threshold=ratio_threshold,
            floor=floor,
            ratio_scale=ratio_scale,
            node=node,
        )
        for node, points in sorted(grouped.items())
        if points
    ]


def _variance_severity(stats: VarianceStats) -> tuple[float, float]:
    """Ranking key for which node drives a summarized evaluation: how far
    the peak clears that node's required peak (above 1 means the rule
    fired), then the raw peak."""
    required = stats.required_peak or 0.0
    if required <= 0:
        return (0.0, stats.peak)
    return (stats.peak / required, stats.peak)


def summarize_variance(per_node: list[VarianceStats]) -> VarianceStats:
    """Cluster summary over per-node evaluations.

    peak and peak_exceeded are pooled (any node can trip the peak rule);
    baseline, ratio, required_ratio, required_peak, and node come from the
    node that comes closest to (or furthest past) its own required peak,
    so the summary numbers describe the node that drove the variance verdict.
    """
    if not per_node:
        return VarianceStats(0, 0.0, 0.0, None, None, None, False, False)
    driver = max(per_node, key=_variance_severity)
    return VarianceStats(
        points=sum(s.points for s in per_node),
        peak=max(s.peak for s in per_node),
        baseline=driver.baseline,
        ratio=driver.ratio,
        required_ratio=driver.required_ratio,
        required_peak=driver.required_peak,
        peak_exceeded=any(s.peak_exceeded for s in per_node),
        variance_exceeded=any(s.variance_exceeded for s in per_node),
        node=driver.node,
    )


@dataclass(frozen=True)
class CheckOutcome:
    """One criterion's verdict plus its criteria JSON entry."""

    result: str  # IDLE | ACTIVE | UNKNOWN | N/A
    entry: dict[str, Any]
    counted: bool  # counts toward the 80% denominator


def _variance_entry(
    stats: VarianceStats, per_node: list[VarianceStats] | None = None
) -> dict[str, Any]:
    """JSON detail for a variance evaluation (additive to result/value)."""
    entry: dict[str, Any] = {
        "peak": round(stats.peak, 2),
        "baseline": round(stats.baseline, 2),
        "ratio": round(stats.ratio, 2) if stats.ratio is not None else None,
        "required_ratio": round(stats.required_ratio, 2)
        if stats.required_ratio is not None
        else None,
        "required_peak": round(stats.required_peak, 2) if stats.required_peak is not None else None,
        "points": stats.points,
        "peak_exceeded": stats.peak_exceeded,
        "variance_exceeded": stats.variance_exceeded,
    }
    if stats.node:
        entry["node"] = stats.node
    if per_node:
        entry["per_node"] = [_variance_entry(s) for s in per_node]
    return entry


# === CRITERIA ==============================================================


def check_cpu(
    cfg: Config,
    prom: PrometheusClient,
    live_nodes: set[str],
    instant_cpu: float | None,
) -> CheckOutcome:
    """CPU criterion: variance rule on 15-minute windows, with the legacy
    window-average as fallback and the one-directional instant override."""
    entry: dict[str, Any] = {"result": "UNKNOWN", "value": None}
    stats: VarianceStats | None = None
    windowed: float | None = None
    per_node: list[VarianceStats] = []

    if cfg.time_window_minutes > 0:
        end_s = int(time.time())
        start_s = end_s - cfg.time_window_minutes * 60
        series = prom.query_range(CPU_RANGE_QUERY, start_s, end_s)
        # Each node's windows are judged against that node's own median,
        # with series from deleted nodes dropped: their history is real but
        # says nothing about the cluster as it stands, and a steady busy
        # node pooled with a quiet sibling is two steady nodes, not a burst.
        per_node = evaluate_variance_by_node(
            series,
            peak_threshold=cfg.cpu_peak_threshold,
            ratio_threshold=cfg.cpu_variance_ratio,
            floor=CPU_VARIANCE_FLOOR,
            live_nodes=live_nodes,
        )
        if per_node:
            stats = summarize_variance(per_node)
        else:
            log_info("CPU query_range matrix unavailable; using legacy window average")
            windowed = prom.query(
                f'(1 - avg(rate(node_cpu_seconds_total{{mode="idle"}}'
                f"[{cfg.time_window_minutes}m]))) * 100"
            )

    active = False
    source = None
    if stats is not None:
        active = stats.active
        source = f"variance over {VARIANCE_WINDOW_MINUTES}m windows"
        entry.update(_variance_entry(stats, per_node))
    elif windowed is not None:
        active = windowed >= cfg.cpu_idle_threshold
        source = "legacy window average"
        entry["window_average"] = round(windowed, 2)
    elif instant_cpu is not None:
        active = instant_cpu >= cfg.cpu_idle_threshold
        source = "instant"
    else:
        # No data at all: UNKNOWN, but still counted (bash parity).
        entry["value"] = fmt2(instant_cpu)
        entry["instant"] = instant_cpu
        return CheckOutcome("UNKNOWN", entry, counted=True)

    # One-directional instant override: a live spike beats an idle verdict.
    instant_override = False
    if not active and instant_cpu is not None and instant_cpu >= cfg.cpu_idle_threshold:
        active = True
        instant_override = True

    value = stats.peak if stats is not None else (windowed if windowed is not None else instant_cpu)
    entry["result"] = "ACTIVE" if active else "IDLE"
    entry["value"] = fmt2(value)
    entry["instant"] = instant_cpu
    entry["instant_override"] = instant_override
    entry["source"] = source
    return CheckOutcome(entry["result"], entry, counted=True)


def check_memory(cfg: Config, prom: PrometheusClient, instant_mem: float | None) -> CheckOutcome:
    """Memory criterion: legacy window average with instant override."""
    entry: dict[str, Any] = {"result": "UNKNOWN", "value": None}
    windowed: float | None = None
    if cfg.time_window_minutes > 0:
        windowed = prom.query(
            "(1 - avg_over_time((sum(node_memory_MemAvailable_bytes) / "
            f"sum(node_memory_MemTotal_bytes))[{cfg.time_window_minutes}m:])) * 100"
        )

    if windowed is None and instant_mem is None:
        entry["value"] = None
        return CheckOutcome("UNKNOWN", entry, counted=True)

    active = False
    if windowed is not None:
        active = windowed >= cfg.memory_idle_threshold
    instant_override = False
    if not active and instant_mem is not None and instant_mem >= cfg.memory_idle_threshold:
        active = True
        instant_override = True

    value = windowed if windowed is not None else instant_mem
    entry["result"] = "ACTIVE" if active else "IDLE"
    entry["value"] = fmt2(value)
    entry["instant"] = instant_mem
    entry["instant_override"] = instant_override
    return CheckOutcome(entry["result"], entry, counted=True)


def check_api(cfg: Config, prom: PrometheusClient) -> CheckOutcome:
    """API criterion: window-average rate against the threshold, plus a
    variance detector (peak vs median of 15-minute windows)."""
    entry: dict[str, Any] = {"result": "UNKNOWN", "value": None}
    if cfg.time_window_minutes > 0:
        avg = prom.query(f"sum(rate(apiserver_request_total[{cfg.time_window_minutes}m]))")
        end_s = int(time.time())
        start_s = end_s - cfg.time_window_minutes * 60
        series = prom.query_range(API_RANGE_QUERY, start_s, end_s)
        variance_points = _series_points(series)
        stats: VarianceStats | None = None
        if variance_points:
            # Only the variance branch applies here; the absolute-threshold
            # branch is the windowed average checked above.
            stats = evaluate_variance(
                variance_points,
                ratio_threshold=cfg.api_variance_ratio,
                floor=cfg.api_variance_floor,
                ratio_scale=cfg.api_threshold,
            )
    else:
        avg = prom.query("sum(rate(apiserver_request_total[5m]))")
        stats = None

    if avg is None and stats is None:
        return CheckOutcome("UNKNOWN", entry, counted=False)

    active = (avg is not None and avg >= cfg.api_threshold) or (
        stats is not None and stats.variance_exceeded
    )
    entry["result"] = "ACTIVE" if active else "IDLE"
    entry["value"] = fmt2(avg)
    if stats is not None:
        entry["variance_peak"] = round(stats.peak, 2)
        entry["variance_baseline"] = round(stats.baseline, 2)
        entry["variance_ratio"] = round(stats.ratio, 2) if stats.ratio is not None else None
        entry["variance_required_ratio"] = (
            round(stats.required_ratio, 2) if stats.required_ratio is not None else None
        )
        entry["variance_required_peak"] = (
            round(stats.required_peak, 2) if stats.required_peak is not None else None
        )
        entry["variance_active"] = stats.variance_exceeded
    return CheckOutcome(entry["result"], entry, counted=True)


def check_gpu(cfg: Config, prom: PrometheusClient, gpu_nodes: list[GpuNode]) -> CheckOutcome:
    """GPU criterion: variance rule on DCGM GPU utilization.  N/A (not
    counted) without GPU nodes or DCGM metrics."""
    entry: dict[str, Any] = {"result": "N/A", "value": None}
    if not gpu_nodes:
        entry["reason"] = "no GPU nodes"
        return CheckOutcome("N/A", entry, counted=False)
    if cfg.time_window_minutes <= 0:
        entry["reason"] = "no time window"
        return CheckOutcome("N/A", entry, counted=False)

    end_s = int(time.time())
    start_s = end_s - cfg.time_window_minutes * 60
    series = prom.query_range(GPU_RANGE_QUERY, start_s, end_s)
    # Grouped per node (per exporter) but with no live filter, unlike cpu:
    # a deleted node's DCGM samples are still activity.
    per_node = evaluate_variance_by_node(
        series,
        peak_threshold=cfg.gpu_peak_threshold,
        ratio_threshold=cfg.gpu_variance_ratio,
        floor=GPU_VARIANCE_FLOOR,
    )

    stats: VarianceStats | None = None
    source = f"variance over {VARIANCE_WINDOW_MINUTES}m windows"
    if per_node:
        stats = summarize_variance(per_node)
    else:
        # Aggregate fallback: whole-window max/avg per series.  All exporter
        # series are pooled - a deleted node's samples are still activity.
        peaks = _to_floats(
            [
                s.get("value", [None, None])[1]
                for s in prom.query_all(
                    f"max_over_time(DCGM_FI_DEV_GPU_UTIL[{cfg.time_window_minutes}m])"
                )
                or []
            ]
        )
        avgs = _to_floats(
            [
                s.get("value", [None, None])[1]
                for s in prom.query_all(
                    f"avg_over_time(DCGM_FI_DEV_GPU_UTIL[{cfg.time_window_minutes}m])"
                )
                or []
            ]
        )
        if peaks:
            peak = max(peaks)
            baseline = sum(avgs) / len(avgs) if avgs else 0.0
            ratio = peak / baseline if baseline > 0 else None
            required_peak = variance_required_peak(
                baseline, cfg.gpu_variance_ratio, cfg.gpu_peak_threshold, GPU_VARIANCE_FLOOR
            )
            stats = VarianceStats(
                points=len(peaks) + len(avgs),
                peak=peak,
                baseline=baseline,
                ratio=ratio,
                required_ratio=required_peak / baseline if baseline > 0 else None,
                required_peak=required_peak,
                peak_exceeded=peak > cfg.gpu_peak_threshold,
                variance_exceeded=peak > required_peak,
            )
            source = "window aggregates fallback"
        else:
            entry["reason"] = "no DCGM metrics"
            return CheckOutcome("N/A", entry, counted=False)

    entry["result"] = "ACTIVE" if stats.active else "IDLE"
    entry["value"] = fmt2(stats.peak)
    entry.update(_variance_entry(stats, per_node))
    entry["source"] = source
    return CheckOutcome(entry["result"], entry, counted=True)


def parse_k8s_timestamp(ts: str) -> datetime | None:
    """Parse a Kubernetes timestamp ("2026-10-01T12:34:56Z"); None when
    absent or malformed.  Naive values (oc never emits them, but they are
    never trusted either) are read as UTC so callers' arithmetic stays
    well-defined."""
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)  # 3.11+: "Z" is accepted directly
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def check_operators(cfg: Config, oc: Oc, now: datetime | None = None) -> CheckOutcome:
    """Operators criterion: controller age and recent reconciliation events.

    Scope is every namespace on the cluster except the excluded platform
    prefixes, plus the configured namespaces (an always-include override, so
    an ODH namespace that happens to match a prefix still counts).  A unit
    is a pod matching POD_RE, aged by its ReplicaSet's creation time when it
    has one - a rollout resets both, a drain only the pod, and the
    ReplicaSet age is the "last deliberate change" signal - and by the pod's
    own creation time otherwise; replicas of one ReplicaSet are a single
    unit.  The criterion votes ACTIVE when the MEDIAN unit age is below the
    threshold (a majority redeployed recently; the old oldest-pod rule was
    blind to a partial rollout masked by one stale survivor) or when
    reconciliation events inside the event window reach
    OPERATOR_EVENT_THRESHOLD.  Events about workload machinery (pods,
    deployments, ...) are excluded from that count: node drains manufacture
    them by the dozen, and the age half already treats drains as noise.
    """
    now = now or datetime.now(UTC)
    entry: dict[str, Any] = {"result": "N/A", "age_days": 0}

    always = {ns.strip() for ns in cfg.operator_namespaces.split(",") if ns.strip()}
    excludes = [p.strip() for p in cfg.operator_exclude_prefixes.split(",") if p.strip()]

    def in_scope(namespace: str) -> bool:
        return namespace in always or not namespace.startswith(tuple(excludes))

    # Unit key: (namespace, kind, name).  A "replicaset" key collapses the
    # replicas of one ReplicaSet into a single unit.
    units: dict[tuple[str, str, str], datetime | None] = {}
    rs_wanted = False
    for line in oc.pods_all():
        fields = line.split("\t")
        if len(fields) != 5:
            continue
        namespace, pod, owner_kind, owner_name, created_raw = fields
        if not in_scope(namespace) or not POD_RE.search(pod):
            continue
        pod_created = parse_k8s_timestamp(created_raw)
        if owner_kind == "ReplicaSet" and owner_name:
            rs_wanted = True
            key = (namespace, "replicaset", owner_name)
            # Provisional pod age: it stands until the ReplicaSet's own
            # creation time is looked up, and remains the fallback for a
            # ReplicaSet deleted mid-rollout whose pods outlive it - the
            # oldest replica is the closest proxy for the missing age.
            if pod_created is None:
                units.setdefault(key, None)
            elif units.get(key) is None or pod_created < units[key]:
                units[key] = pod_created
        elif pod_created is not None:
            units[(namespace, "pod", pod)] = pod_created

    if not units:
        entry["reason"] = "no controller pods in any scanned namespace"
        return CheckOutcome("N/A", entry, counted=False)

    if rs_wanted:
        # Only ReplicaSets owning a live in-scope pod are read: the cluster
        # also holds old scaled-to-zero revisions whose ages would read
        # "install date", not "last change".
        for line in oc.replicasets_all():
            fields = line.split("\t")
            if len(fields) != 3:
                continue
            rs_created = parse_k8s_timestamp(fields[2])
            if rs_created is None:
                continue
            key = (fields[0], "replicaset", fields[1])
            if key in units:
                units[key] = rs_created

    units = {key: created for key, created in units.items() if created is not None}
    if not units:
        entry["reason"] = "no controller pods with a readable creation time"
        return CheckOutcome("N/A", entry, counted=False)

    events = 0
    excluded_workload = 0
    window_start = now - timedelta(hours=cfg.operator_event_hours)
    for line in oc.events_all_rows():
        fields = line.split("\t")
        # namespace, type, reason, object kind, object name, message (which
        # may itself contain tabs), lastTimestamp, firstTimestamp.
        if len(fields) < 8:
            continue
        if not in_scope(fields[0]):
            continue
        timestamp = parse_k8s_timestamp(fields[-2]) or parse_k8s_timestamp(fields[-1])
        if timestamp is None or timestamp < window_start:
            continue
        if not OPERATOR_EVENT_RE.search(" ".join(fields[1:-2])):
            continue
        if fields[3] in OPERATOR_WORKLOAD_KINDS:
            excluded_workload += 1
        else:
            events += 1

    ages_days = {key: (now - created).total_seconds() / 86400 for key, created in units.items()}
    median_age = statistics.median(ages_days.values())
    idle = median_age >= cfg.operator_age_days and events < OPERATOR_EVENT_THRESHOLD
    entry["result"] = "IDLE" if idle else "ACTIVE"
    entry["age_days"] = round(median_age, 2)
    entry["units"] = [
        {"namespace": ns, "kind": kind, "name": name, "age_days": round(age, 2)}
        for (ns, kind, name), age in sorted(ages_days.items(), key=lambda kv: kv[1], reverse=True)
    ]
    entry["namespaces"] = sorted({ns for ns, _, _ in units})
    entry["events"] = events
    entry["events_excluded_workload"] = excluded_workload
    entry["event_window_hours"] = cfg.operator_event_hours
    return CheckOutcome(entry["result"], entry, counted=True)


def compute_verdict(outcomes: list[CheckOutcome]) -> tuple[int, int, int, str, int]:
    """The 80% rule: IDLE when the IDLE votes reach int(total * 0.80).

    Uncounted criteria (N/A / UNKNOWN-with-decrement) shrink the
    denominator; cpu and memory UNKNOWN still count, matching the bash
    script's bookkeeping.
    """
    total = sum(1 for outcome in outcomes if outcome.counted)
    met = sum(1 for outcome in outcomes if outcome.result == "IDLE")
    threshold = int(total * 0.80)
    status = "IDLE" if met >= threshold else "ACTIVE"
    exit_code = 1 if status == "IDLE" else 0
    return total, met, threshold, status, exit_code


# === INFORMATIONAL (non-voting) SECTIONS ===================================


def instant_by_instance(series: list[dict[str, Any]] | None) -> dict[str, float]:
    """Reduce instant-query series to {instance label: value}; junk skipped."""
    out: dict[str, float] = {}
    for entry in series or []:
        instance = str(entry.get("metric", {}).get("instance", ""))
        try:
            value = float(entry["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            out[instance] = value
    return out


def average_matching(by_instance: dict[str, float], pattern: re.Pattern[str]) -> float | None:
    """Mean of the values whose instance label matches `pattern`.

    Prometheus `=~` matchers are fully anchored, so the Python equivalent
    of a node's `instance=~"<name>.*"` selector is `pattern.fullmatch`.
    """
    values = [value for instance, value in by_instance.items() if pattern.fullmatch(instance)]
    return sum(values) / len(values) if values else None


def gpu_usage_section(
    cfg: Config, oc: Oc, prom: PrometheusClient, gpu_nodes: list[GpuNode]
) -> dict[str, Any]:
    """GPU node CPU/memory usage for the report (node resources, not GPU
    utilization - that now lives in the gpu criterion).

    Per-node instant numbers come from the single cached `adm top nodes`
    snapshot, and the windowed numbers from one batched query per metric
    over all GPU nodes (`avg by (instance)` keeps the series separable so
    they can be mapped back per node) instead of two round trips per node.
    """
    top = oc.top_node_map()
    cpu_current: list[float] = []
    mem_current: list[float] = []
    for node in gpu_nodes:
        stats = top.get(node.name)
        if stats is not None:
            cpu_current.append(stats[0])
            mem_current.append(stats[1])

    cpu_windowed: list[float] = []
    mem_windowed: list[float] = []
    if cfg.time_window_minutes > 0 and gpu_nodes:
        pattern = "|".join(f"{re.escape(node.name)}.*" for node in gpu_nodes)
        cpu_by_instance = instant_by_instance(
            prom.query_all(
                '(1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle",'
                f'instance=~"{pattern}"}}[{cfg.time_window_minutes}m]))) * 100'
            )
        )
        mem_by_instance = instant_by_instance(
            prom.query_all(
                "(1 - avg_over_time((avg by (instance) (node_memory_MemAvailable_bytes"
                f'{{instance=~"{pattern}"}}) / avg by (instance) (node_memory_MemTotal_bytes'
                f'{{instance=~"{pattern}"}}))[{cfg.time_window_minutes}m:])) * 100'
            )
        )
        for node in gpu_nodes:
            node_pattern = re.compile(f"{re.escape(node.name)}.*")
            cpu = average_matching(cpu_by_instance, node_pattern)
            if cpu is not None:
                cpu_windowed.append(cpu)
            mem = average_matching(mem_by_instance, node_pattern)
            if mem is not None:
                mem_windowed.append(mem)

    def _avg(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    return {
        "cpu_current": fmt2(_avg(cpu_current)),
        "memory_current": fmt2(_avg(mem_current)),
        "cpu_windowed": fmt2(_avg(cpu_windowed)),
        "memory_windowed": fmt2(_avg(mem_windowed)),
    }


def ml_node_report(
    cfg: Config, oc: Oc, prom: PrometheusClient, items: list[dict[str, Any]]
) -> bool:
    """ML node usage display (informational, never votes).

    The instance-type pattern match runs in every mode because the return
    value feeds the "expensive nodes are idle" warning. The per-node usage
    numbers - one cached `adm top nodes` snapshot plus one batched windowed
    query over all matched nodes - are only gathered in verbose mode, the
    only mode that displays them.
    """
    pattern = re.compile(cfg.ml_node_pattern)
    ml_nodes = [item for item in items if pattern.search(node_instance_type(item))]
    if not ml_nodes:
        log_info("No ML/GPU instance-type nodes found")
        return False

    log_info(f"Found {len(ml_nodes)} ML/GPU instance-type nodes:")
    if not cfg.verbose:
        return True

    top = oc.top_node_map()
    windowed_cpu: dict[str, float] = {}
    if cfg.time_window_minutes > 0:
        names = [str(item.get("metadata", {}).get("name", "")) for item in ml_nodes]
        instance_pattern = "|".join(f"{re.escape(name)}.*" for name in names)
        windowed_cpu = instant_by_instance(
            prom.query_all(
                f'(1 - avg by (instance) (rate(node_cpu_seconds_total{{mode="idle",'
                f'instance=~"{instance_pattern}"}}'
                f"[{cfg.time_window_minutes}m]))) * 100"
            )
        )
    for item in ml_nodes:
        name = str(item.get("metadata", {}).get("name", ""))
        instance = node_instance_type(item)
        cpu, mem = top.get(name, (None, None))
        windowed = None
        if windowed_cpu:
            windowed = average_matching(windowed_cpu, re.compile(f"{re.escape(name)}.*"))
        log_info(
            f"  {name} ({instance}): instant cpu={fmt2(cpu)}% "
            f"mem={fmt2(mem)}%, windowed cpu={fmt2(windowed)}%"
        )
    return True


def recent_activity_report(oc: Oc, event_minutes: int, now: datetime | None = None) -> None:
    """Verbose-only recent pod activity display."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=event_minutes)
    recent: list[str] = []
    for row in oc.events_all_rows():
        fields = row.split("\t")
        if len(fields) < 8:
            continue
        timestamp = parse_k8s_timestamp(fields[-2]) or parse_k8s_timestamp(fields[-1])
        if timestamp is None or timestamp < cutoff:
            continue
        line = " ".join(fields[1:-2])
        if RECENT_ACTIVITY_RE.search(line):
            recent.append(line)
    log_info(f"Pod-related events in the last {event_minutes} minutes: {len(recent)}")
    for line in recent[-5:]:
        log_info(f"  {line}")


def gpu_machines_report(oc: Oc, server: str) -> None:
    """Verbose-only GPU machine display for clusters without GPU nodes."""
    short = cluster_short_name(server)
    if not short:
        return
    pattern = re.compile(f"{re.escape(short)}-.*-gpu-")
    machines = [
        m for m in oc.machines() if pattern.search(str(m.get("metadata", {}).get("name", "")))
    ]
    if machines:
        log_info(f"GPU machines exist in openshift-machine-api: {len(machines)}")
    else:
        log_info("No GPU machines found in openshift-machine-api")


# === REPORT / EXPORT =======================================================

CSV_HEADER = (
    "timestamp,cluster,status,cpu_result,cpu_value,memory_result,memory_value,"
    "api_server_result,api_server_value,gpu_result,gpu_value,operators_result,"
    "operator_age_days,criteria_met,total_criteria,time_window_minutes,"
    "has_gpu_nodes,gpu_node_count,gpu_flavors,gpu_node_age,gpu_cpu_current,"
    "gpu_mem_current,gpu_cpu_windowed,gpu_mem_windowed"
)


def export_csv(report: dict[str, Any], path: Path) -> None:
    """Append one result row; write the header only for a new file."""
    gpu = report["gpu"]
    criteria = report["criteria"]
    row = [
        report["timestamp"],
        report["cluster"],
        report["status"],
        criteria["cpu"]["result"],
        criteria["cpu"]["value"] or "N/A",
        criteria["memory"]["result"],
        criteria["memory"]["value"] or "N/A",
        criteria["api_server"]["result"],
        criteria["api_server"]["value"] or "N/A",
        criteria["gpu"]["result"],
        criteria["gpu"]["value"] or "N/A",
        criteria["operators"]["result"],
        criteria["operators"]["age_days"],
        criteria["met"],
        criteria["total"],
        report["configuration"]["time_window_minutes"],
        "true" if gpu["has_gpu_nodes"] else "false",
        gpu["node_count"],
        gpu["flavors"] or "N/A",
        gpu["node_age"] or "N/A",
        gpu["usage"]["cpu_current"] or "N/A",
        gpu["usage"]["memory_current"] or "N/A",
        gpu["usage"]["cpu_windowed"] or "N/A",
        gpu["usage"]["memory_windowed"] or "N/A",
    ]
    is_new = not path.exists()
    with path.open("a", newline="") as fh:
        if is_new:
            fh.write(CSV_HEADER + "\n")
        fh.write(",".join(str(field) for field in row) + "\n")


def export_json(report: dict[str, Any], path: Path) -> None:
    with path.open("w") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")


# === MAIN ==================================================================


def run(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    set_verbose(cfg.verbose)

    if cfg.verbose:
        print("=" * 40, file=sys.stderr)
        print("  OpenShift Cluster Idle Detection", file=sys.stderr)
        print("=" * 40, file=sys.stderr)
        log_info(f"Time window: {cfg.time_window_minutes} minutes")
        log_info(f"Event history: {cfg.event_history_minutes} minutes")

    if cfg.debug_probe:
        log_info(
            "--debug-probe is accepted for compatibility; the criteria "
            "detail is always included in the JSON export now"
        )

    if shutil.which("oc") is None:
        log_error("oc command not found")
        return 2
    oc = Oc(verbose=cfg.verbose)
    if not oc.logged_in():
        log_error("Not logged in to an OpenShift cluster (oc whoami failed)")
        return 2

    prom = PrometheusClient(oc, token=cfg.token, verbose=cfg.verbose)
    server = oc.whoami_server()
    log_info(f"Cluster: {server}")
    log_info(f"Prometheus token: {'provided' if cfg.token else 'resolved via oc'}")

    node_items = oc.nodes()
    live_nodes = {str(item.get("metadata", {}).get("name", "")) for item in node_items}
    gpu_nodes = gpu_nodes_from_items(node_items)
    instant_cpu, instant_mem = instant_averages(oc.top_nodes())

    log_info(f"Nodes: {len(live_nodes)} ({len(gpu_nodes)} with GPUs)")
    log_info(f"Instant cluster CPU: {fmt2(instant_cpu)}%, memory: {fmt2(instant_mem)}%")

    # --- CHECK 1-5: the five criteria, run concurrently ---
    # They are independent I/O-bound checks; only the operators check spawns
    # oc commands, and the Prometheus client resolves its endpoint and token
    # exactly once under its init lock. Results are consumed in fixed order
    # so the log output matches the old sequential run line for line, and an
    # exception re-raises at the same check it would have in that run.
    now = datetime.now(UTC)
    checks: list[tuple[str, str, Callable[[], CheckOutcome]]] = [
        ("CPU Utilization", "CPU", lambda: check_cpu(cfg, prom, live_nodes, instant_cpu)),
        ("Memory Utilization", "Memory", lambda: check_memory(cfg, prom, instant_mem)),
        ("API Server Activity", "API Server", lambda: check_api(cfg, prom)),
        ("GPU Utilization", "GPU", lambda: check_gpu(cfg, prom, gpu_nodes)),
        ("Operators", "Operators", lambda: check_operators(cfg, oc, now)),
    ]
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        futures = [pool.submit(run_check) for _, _, run_check in checks]
        outcomes: list[CheckOutcome] = []
        for number, ((title, label, _), future) in enumerate(zip(checks, futures, strict=True), 1):
            log_info(f"--- CHECK {number}: {title} ---")
            outcome = future.result()
            log_info(f"{label} Result: {outcome.result}")
            outcomes.append(outcome)
    cpu_outcome, memory_outcome, api_outcome, gpu_outcome, operators_outcome = outcomes

    # --- Informational (never votes) ---
    ml_found = False
    if cfg.check_ml_nodes:
        ml_found = ml_node_report(cfg, oc, prom, node_items)
    if cfg.verbose:
        recent_activity_report(oc, cfg.event_history_minutes)
        if not gpu_nodes:
            gpu_machines_report(oc, server)

    # --- GPU report section (informational usage numbers) ---
    flavors = sorted({f"{n.vendor}({n.instance_type})" for n in gpu_nodes})
    gpu_section: dict[str, Any] = {
        "has_gpu_nodes": bool(gpu_nodes),
        "node_count": len(gpu_nodes),
        "flavors": ",".join(flavors) if flavors else None,
        "node_age": None,
        "usage": gpu_usage_section(cfg, oc, prom, gpu_nodes)
        if gpu_nodes
        else {
            "cpu_current": None,
            "memory_current": None,
            "cpu_windowed": None,
            "memory_windowed": None,
        },
    }

    # --- Verdict ---
    outcomes = [cpu_outcome, memory_outcome, api_outcome, gpu_outcome, operators_outcome]
    total, met, threshold, status, exit_code = compute_verdict(outcomes)

    report: dict[str, Any] = {
        "timestamp": now.isoformat(timespec="seconds"),
        "timestamp_human": now.strftime("%a %b %d %H:%M:%S UTC %Y"),
        "cluster": server,
        "status": status,
        "exit_code": exit_code,
        "configuration": {
            "time_window_minutes": cfg.time_window_minutes,
            "cpu_threshold": cfg.cpu_idle_threshold,
            "memory_threshold": cfg.memory_idle_threshold,
            "api_threshold": cfg.api_threshold,
            "operator_age_threshold_days": cfg.operator_age_days,
            "operator_event_window_hours": cfg.operator_event_hours,
            "operator_exclude_prefixes": cfg.operator_exclude_prefixes,
            "cpu_peak_threshold": cfg.cpu_peak_threshold,
            "cpu_variance_ratio": cfg.cpu_variance_ratio,
            "gpu_peak_threshold": cfg.gpu_peak_threshold,
            "gpu_variance_ratio": cfg.gpu_variance_ratio,
            "api_variance_ratio": cfg.api_variance_ratio,
            "api_variance_floor": cfg.api_variance_floor,
        },
        "criteria": {
            "total": total,
            "met": met,
            "threshold": threshold,
            "cpu": cpu_outcome.entry,
            "memory": memory_outcome.entry,
            "api_server": api_outcome.entry,
            "gpu": gpu_outcome.entry,
            "operators": operators_outcome.entry,
        },
        "gpu": gpu_section,
    }

    # --- Summary ---
    if cfg.verbose:
        print("-" * 40, file=sys.stderr)
        log_info(f"Idle criteria met: {met} / {total} (threshold: {threshold})")
        for name, outcome in zip(
            ("cpu", "memory", "api_server", "gpu", "operators"), outcomes, strict=True
        ):
            log_info(f"  {name}: {outcome.result}")
    if status == "IDLE" and (gpu_nodes or ml_found):
        log_warn("Expensive GPU/ML nodes are idle - review before cleanup")

    if cfg.verbose:
        if status == "IDLE":
            print(f"{GREEN}========================================{NC}", file=sys.stderr)
            print(f"{GREEN}  Cluster Status: IDLE{NC}", file=sys.stderr)
            print(f"{GREEN}========================================{NC}", file=sys.stderr)
        else:
            print(f"{YELLOW}========================================{NC}", file=sys.stderr)
            print(f"{YELLOW}  Cluster Status: ACTIVE{NC}", file=sys.stderr)
            print(f"{YELLOW}========================================{NC}", file=sys.stderr)

    # Quiet-mode result lines (stdout, matching the bash script's format).
    print(f"CPU: {cpu_outcome.result}")
    print(f"Memory: {memory_outcome.result}")
    if api_outcome.result != "UNKNOWN":
        print(f"API Server: {api_outcome.result}")
    print(f"GPU: {gpu_outcome.result}")
    print(f"Operators: {operators_outcome.result}")
    print(f"STATUS: {status}")

    if cfg.csv_path is not None:
        export_csv(report, cfg.csv_path)
        log_info(f"CSV exported to {cfg.csv_path}")
    if cfg.json_path is not None:
        export_json(report, cfg.json_path)
        log_info(f"JSON exported to {cfg.json_path}")

    return exit_code


if __name__ == "__main__":
    sys.exit(run())
