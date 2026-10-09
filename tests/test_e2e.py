"""End-to-end tests: the full run() orchestration against a local
Prometheus HTTP server and a stubbed oc CLI, plus subprocess smoke tests
for the bash shim that Jenkins and cluster-monitor invoke.

oc is stubbed at the Oc.run seam with strict argv matching: every command
the module issues must be declared by the test, and an undeclared command
fails the test loudly.  That keeps the oc command construction under test
without spawning oc or keeping a fake binary on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import ocp_idle_check as oic
from helpers import (
    api_avg_query,
    make_dcgm_series,
    make_range_series,
    memory_window_query,
    node_cpu_window_query,
    node_mem_window_query,
    prom_instant_json,
    prom_range_json,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "ocp-idle-check.sh"
# Resolved at import time: one test narrows PATH to a directory without bash.
BASH = shutil.which("bash") or "/bin/bash"
SERVER = "https://api.test-cluster.example.com:6443"
WINDOW = 10080

OPERATOR_POD_OLD = "odh-operator-controller-manager-abc 1/1 Running 5 (3d ago) 12d"
OPERATOR_POD_YOUNG = "odh-operator-controller-manager-abc 1/1 Running 0 2d"
QUIET_EVENT = "5m Normal Pulled pod/odh-xyz Pulled image quay.io/example"
CREATE_TOKEN_ARGS = (
    "create",
    "token",
    "prometheus-k8s",
    "-n",
    "openshift-monitoring",
    "--duration=10m",
)


def install_fake_oc(
    monkeypatch, responses: dict[tuple[str, ...], str | None]
) -> list[tuple[str, ...]]:
    """Replace Oc.run with a strict argv-keyed stub; returns the call log.

    A value of None means the command failed (non-zero exit), matching how
    Oc.run reports failures.
    """
    calls: list[tuple[str, ...]] = []

    def fake_run(self, args, timeout=oic.OC_TIMEOUT):
        key = tuple(args)
        calls.append(key)
        if key not in responses:
            raise AssertionError(f"unexpected oc invocation: {' '.join(key)}")
        return responses[key]

    monkeypatch.setattr(oic.Oc, "run", fake_run)
    # The oc presence probe must pass regardless of the host PATH.
    monkeypatch.setattr(oic.shutil, "which", lambda name: "/fakebin/oc" if name == "oc" else None)
    return calls


def node_item(name: str, instance_type: str = "m5.xlarge", gpus: str | None = None) -> dict:
    capacity = {"nvidia.com/gpu": gpus} if gpus is not None else {}
    return {
        "metadata": {
            "name": name,
            "labels": {"node.kubernetes.io/instance-type": instance_type},
        },
        "status": {"capacity": capacity},
    }


def base_oc_responses(nodes: list[dict]) -> dict[tuple[str, ...], str | None]:
    names = [n["metadata"]["name"] for n in nodes]
    top_lines = "\n".join(f"{name} 250m 2% 3212Mi 5%" for name in names) + "\n"
    return {
        ("whoami",): "test-user",
        ("whoami", "--show-server"): SERVER,
        ("whoami", "-t"): "fake-token",
        ("get", "nodes", "-o", "json"): json.dumps({"items": nodes}),
        ("adm", "top", "nodes", "--no-headers"): top_lines,
        ("get", "namespace", "opendatahub"): "",
        ("get", "namespace", "redhat-ods-operator"): None,
        ("get", "namespace", "redhat-ods-applications"): None,
        ("get", "pods", "-n", "opendatahub", "--no-headers"): OPERATOR_POD_OLD + "\n",
        (
            "get",
            "events",
            "-n",
            "opendatahub",
            "--sort-by=.lastTimestamp",
        ): QUIET_EVENT + "\n",
    }


def quiet_cpu(responses: dict, nodes: list[str]) -> None:
    responses[("/api/v1/query_range", oic.CPU_RANGE_QUERY)] = prom_range_json(
        [make_range_series(n, ["2"] * 8) for n in nodes]
    )


def quiet_memory_and_api(responses: dict) -> None:
    responses[("/api/v1/query", memory_window_query(WINDOW))] = prom_instant_json("10")
    responses[("/api/v1/query", api_avg_query(WINDOW))] = prom_instant_json("5")
    responses[("/api/v1/query_range", oic.API_RANGE_QUERY)] = prom_range_json(
        [{"metric": {}, "values": [[i * 900, "5"] for i in range(8)]}]
    )


# === FULL RUN, IN PROCESS ===================================================


def test_idle_cluster_verbose(monkeypatch, prom_server, tmp_path, capsys):
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    oc_responses = base_oc_responses(
        [node_item("node-1"), node_item("node-2"), node_item("node-3")]
    )
    # Verbose mode also pulls the cluster-wide event list and machine list.
    oc_responses[("get", "events", "-A", "--sort-by=.lastTimestamp")] = ""
    oc_responses[("get", "machines", "-n", "openshift-machine-api", "-o", "json")] = '{"items": []}'
    calls = install_fake_oc(monkeypatch, oc_responses)
    quiet_cpu(responses, ["node-1", "node-2", "node-3"])
    quiet_memory_and_api(responses)

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 1

    # The quiet result lines are stdout; everything else goes to stderr.
    assert capsys.readouterr().out.splitlines() == [
        "CPU: IDLE",
        "Memory: IDLE",
        "API Server: IDLE",
        "GPU: N/A",
        "Operators: IDLE",
        "STATUS: IDLE",
    ]

    report = json.loads(json_path.read_text())
    assert report["status"] == "IDLE"
    assert report["exit_code"] == 1
    assert report["cluster"] == SERVER
    assert list(report) == [
        "timestamp",
        "timestamp_human",
        "cluster",
        "status",
        "exit_code",
        "configuration",
        "criteria",
        "gpu",
    ]
    assert list(report["configuration"]) == [
        "time_window_minutes",
        "cpu_threshold",
        "memory_threshold",
        "api_threshold",
        "operator_age_threshold_days",
        "cpu_peak_threshold",
        "cpu_shape_ratio",
        "gpu_peak_threshold",
        "gpu_shape_ratio",
        "api_spike_ratio",
        "api_spike_floor",
    ]
    criteria = report["criteria"]
    assert list(criteria) == [
        "total",
        "met",
        "threshold",
        "cpu",
        "memory",
        "api_server",
        "gpu",
        "operators",
    ]
    assert (criteria["total"], criteria["met"], criteria["threshold"]) == (4, 4, 3)
    assert criteria["cpu"]["result"] == "IDLE"
    assert criteria["cpu"]["source"] == "spike/shape over 15m windows"
    assert criteria["cpu"]["peak"] == 2.0
    assert criteria["operators"]["age_days"] == 12
    assert criteria["operators"]["events"] == 0
    assert criteria["gpu"]["result"] == "N/A"
    # The token came from `oc whoami -t`; no service-account token was created.
    assert CREATE_TOKEN_ARGS not in calls


def test_active_cluster_quiet(monkeypatch, prom_server, tmp_path, capsys):
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    oc_responses = base_oc_responses(
        [node_item("node-1"), node_item("node-2"), node_item("node-3")]
    )
    oc_responses[("get", "pods", "-n", "opendatahub", "--no-headers")] = OPERATOR_POD_YOUNG + "\n"
    install_fake_oc(monkeypatch, oc_responses)
    # One node spends a single 15-minute window at 50% CPU.
    responses[("/api/v1/query_range", oic.CPU_RANGE_QUERY)] = prom_range_json(
        [
            make_range_series("node-1", ["5"] * 8),
            make_range_series("node-2", ["5", "5", "50"] + ["5"] * 5),
            make_range_series("node-3", ["5"] * 8),
        ]
    )
    quiet_memory_and_api(responses)

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 0
    assert capsys.readouterr().out.splitlines()[-1] == "STATUS: ACTIVE"

    criteria = json.loads(json_path.read_text())["criteria"]
    assert (criteria["total"], criteria["met"]) == (4, 2)
    assert criteria["cpu"]["result"] == "ACTIVE"
    assert criteria["cpu"]["peak"] == 50.0
    assert criteria["memory"]["result"] == "IDLE"
    assert criteria["api_server"]["result"] == "IDLE"
    assert criteria["operators"]["result"] == "ACTIVE"


def test_gpu_cluster_adds_fifth_criterion(monkeypatch, prom_server, tmp_path):
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    nodes = [node_item("node-1"), node_item("gpu-node-1", instance_type="g5.xlarge", gpus="4")]
    oc_responses = base_oc_responses(nodes)
    oc_responses[("get", "pods", "-n", "opendatahub", "--no-headers")] = OPERATOR_POD_YOUNG + "\n"
    oc_responses[("adm", "top", "node", "gpu-node-1", "--no-headers")] = (
        "gpu-node-1 500m 3% 8000Mi 6%\n"
    )
    install_fake_oc(monkeypatch, oc_responses)

    quiet_cpu(responses, ["node-1"])
    quiet_memory_and_api(responses)
    # One 15-minute window at 80% GPU utilization.
    responses[("/api/v1/query_range", oic.GPU_RANGE_QUERY)] = prom_range_json(
        [make_dcgm_series(["0", "0", "0", "80"])]
    )
    # Per-node windowed usage queries from the gpu report and ML node check.
    responses[("/api/v1/query", node_cpu_window_query("gpu-node-1", WINDOW))] = prom_instant_json(
        "7"
    )
    responses[("/api/v1/query", node_mem_window_query("gpu-node-1", WINDOW))] = prom_instant_json(
        "11"
    )

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 0

    report = json.loads(json_path.read_text())
    criteria = report["criteria"]
    # Five criteria now; cpu, memory, and api vote IDLE - 3 of 5 is below 80%.
    assert (criteria["total"], criteria["met"], criteria["threshold"]) == (5, 3, 4)
    assert criteria["gpu"]["result"] == "ACTIVE"
    assert criteria["gpu"]["peak"] == 80.0
    gpu = report["gpu"]
    assert gpu["has_gpu_nodes"] is True
    assert gpu["node_count"] == 1
    assert gpu["flavors"] == "NVIDIA(g5.xlarge)"
    assert gpu["usage"] == {
        "cpu_current": "3.00",
        "memory_current": "6.00",
        "cpu_windowed": "7.00",
        "memory_windowed": "11.00",
    }


def test_csv_export_appends_with_header(monkeypatch, prom_server, tmp_path):
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    install_fake_oc(monkeypatch, base_oc_responses([node_item("node-1")]))
    quiet_cpu(responses, ["node-1"])
    quiet_memory_and_api(responses)

    csv_path = tmp_path / "results.csv"
    argv = ["-q", "--csv", str(csv_path), "-w", str(WINDOW)]
    assert oic.run(argv) == 1
    assert oic.run(argv) == 1

    lines = csv_path.read_text().splitlines()
    assert len(lines) == 3
    assert lines[0] == oic.CSV_HEADER
    assert lines[1] == lines[2]
    for row in lines[1:]:
        assert len(row.split(",")) == 24


# === BASH SHIM SMOKE TESTS ==================================================


def run_shim(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, str(SHIM), *args], capture_output=True, text=True, timeout=timeout)


def test_shim_help_exits_0():
    proc = run_shim(["-h"])
    assert proc.returncode == 0
    assert "ocp-idle-check.sh" in proc.stdout


def test_shim_unknown_flag_exits_2():
    proc = run_shim(["--nope"])
    assert proc.returncode == 2


def test_shim_missing_oc_exits_2(monkeypatch):
    # A PATH with python3 but no oc: the shim must fail cleanly with 2.
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    proc = run_shim(["-q"])
    assert proc.returncode == 2
    assert "oc command not found" in proc.stderr


def test_shim_without_python3_exits_2(monkeypatch, tmp_path):
    # No python3 anywhere on PATH: exit 2, not exec's 127.  The guards run
    # before the SCRIPT_DIR resolution on purpose - dirname is an external
    # binary and would also be missing from a stripped PATH.
    monkeypatch.setenv("PATH", str(tmp_path))
    proc = run_shim(["-q"])
    assert proc.returncode == 2
    assert "python3 not found" in proc.stderr


def test_shim_old_python3_exits_2(monkeypatch, tmp_path):
    # A too-old python3 must fail with 2; a Python traceback would exit 1,
    # which callers would misread as IDLE.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/bash\necho 'Python 3.6.9'\nexit 1\n")
    fake_python.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    proc = run_shim(["-q"])
    assert proc.returncode == 2
    assert "python3 >= 3.12 required" in proc.stderr
    assert "Python 3.6.9" in proc.stderr


def test_shim_prefers_python312_over_old_bare_python3(monkeypatch, tmp_path):
    # The Jenkins agent image ships a bare python3 of 3.9 alongside a full
    # python3.12; the shim must run the module with the versioned name.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    old = fake_bin / "python3"
    old.write_text("#!/bin/bash\necho 'Python 3.9.25'\nexit 1\n")
    old.chmod(0o755)
    fake_312 = fake_bin / "python3.12"
    fake_312.write_text(
        "#!/bin/bash\n"
        "# The version probe passes; any other invocation reports its argv.\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        'echo "ran $*"\n'
    )
    fake_312.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    proc = run_shim(["-q"])
    assert proc.returncode == 0
    # dirname is absent from the stripped PATH, so the shim falls back to the
    # working directory for SCRIPT_DIR - the same fallback the oc test above
    # relies on.
    assert proc.stdout.strip() == f"ran {Path.cwd() / 'ocp_idle_check.py'} -q"
