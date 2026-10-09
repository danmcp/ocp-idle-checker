# OpenShift Cluster Idle Detection Script

Detects if your OpenShift cluster is idle based on CPU, memory, API server activity, GPU utilization, and operator age. Supports time-windowed metrics and GPU node detection.

`ocp-idle-check.sh` is a thin bash shim that execs the Python implementation (`ocp_idle_check.py`, stdlib only), so existing callers — the Jenkins job, cluster-monitor's vendored copy — keep working unchanged.

## Quick Start

```bash
# Default check (7-day window)
./ocp-idle-check.sh

# Quiet mode with export
./ocp-idle-check.sh -q --csv results.csv --json results.json

# Custom thresholds
./ocp-idle-check.sh -w 1440 -c 15 -m 35 -a 50
```

## Exit Codes

- **0** = Cluster is ACTIVE (success - resources are being used)
- **1** = Cluster is IDLE (warning - resources may be wasted)
- **2** = Error

## Idle Criteria (80% must pass)

1. **CPU** — spike/shape rule over 15-minute windows (see below)
2. **Memory** < 35% (time-windowed average)
3. **API Server** < 100 req/sec, plus a spike detector
4. **GPU** — spike/shape rule on DCGM GPU utilization (only on clusters with GPU nodes)
5. **Operators** ≥ 7 days old with low activity

### Decision Logic

Each criterion is evaluated independently and can be `IDLE`, `ACTIVE`, `UNKNOWN`, or `N/A`. The cluster is declared **IDLE** when the number of `IDLE` results reaches `int(total_criteria × 0.80)`.

### Spike/Shape Detection (CPU & GPU)

The CPU and GPU criteria pool per-node 15-minute window averages from the Prometheus `query_range` matrix and evaluate:

- **Peak rule**: any 15-minute window averaged above the peak threshold (default 40%), **or**
- **Shape rule**: the window peak is more than shape-ratio times (default 2×) the median window while sitting above the shape floor (default 20%).

The floor keeps quiet baselines from tripping the ratio alone: a cluster whose median window is 2% and whose peak window is 10% is a 5× ratio but still quiet, while median 5% with peaks at 30% is genuinely bursty and counts as active. A zero baseline with a nonzero peak counts as an unbounded ratio, gated by the floor just the same.

CPU falls back to the legacy window-average rule when the range matrix is unavailable, and an instant `oc adm top nodes` reading above the idle threshold can still override an IDLE result (one-directional, as in the bash version).

### Spike Detection (API Server)

The API server criterion keeps the legacy window-average threshold (100 req/sec) and adds a second detector: any 15-minute window above 2× the median window (and above a floor of 50 req/s) also counts as ACTIVE.

### Instant Override (Memory)

Memory uses the windowed average with a one-directional instant override:

| Windowed average | Instant snapshot | Result |
|-----------------|-----------------|--------|
| IDLE            | IDLE            | **IDLE** ✓ |
| IDLE            | ACTIVE          | **ACTIVE** — spike detected |
| ACTIVE          | any             | **ACTIVE** |
| N/A             | IDLE            | **IDLE** ✓ (fallback to instant) |
| N/A             | ACTIVE          | **ACTIVE** |

### Conditional Criteria

Some criteria are **skipped** (not counted in the total) when the required data is unavailable, which affects the 80% threshold denominator:

- **API Server**: skipped if both the windowed average and the spike detector have no data
- **GPU**: skipped on clusters without GPU nodes or without DCGM metrics
- **Operators**: skipped if none of the configured namespaces exist on the cluster

CPU and memory `UNKNOWN` still count toward the denominator (they never help the IDLE count, only the total).

## Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `-w, --window MINUTES` | Time window for averages | 10080 (7 days) |
| `-c, --cpu-threshold N` | CPU idle threshold (%) — instant override and legacy fallback | 15 |
| `-m, --mem-threshold N` | Memory idle threshold (%) | 35 |
| `-a, --api-threshold N` | API requests/sec threshold | 100 |
| `-o, --operator-age N` | Operator age threshold (days) | 7 |
| `--operator-namespaces NS` | Operator namespaces to check | opendatahub,redhat-ods-operator,redhat-ods-applications |
| `--cpu-peak-threshold N` | CPU: any 15-min window above this % = ACTIVE | 40 |
| `--cpu-shape-ratio N` | CPU: peak/median above this = ACTIVE | 2 |
| `--cpu-shape-floor N` | CPU: peak must reach this % for the ratio to count | 20 |
| `--gpu-peak-threshold N` | GPU: any 15-min window above this % = ACTIVE | 40 |
| `--gpu-shape-ratio N` | GPU: peak/median above this = ACTIVE | 2 |
| `--gpu-shape-floor N` | GPU: peak must reach this % for the ratio to count | 20 |
| `--api-spike-ratio N` | API: peak/median above this = ACTIVE | 2 |
| `--api-spike-floor N` | API: peak must reach this many req/s for the ratio to count | 50 |
| `--csv FILE` | Export to CSV | - |
| `--json FILE` | Export to JSON | - |
| `--token TOKEN` | Prometheus bearer token (resolved via `oc` when omitted) | - |
| `-e, --events MINUTES` | Event history window for informational output | 60 |
| `-q, --quiet` | Minimal output | false |
| `--no-ml-check` | Skip the ML/GPU node check | false |
| `--debug-probe` | Accepted for compatibility; criteria detail is always exported now | false |

The spike/shape parameters are fleet-calibration knobs; the defaults are provisional.

## Features

### Time-Windowed Metrics
Queries Prometheus to get average CPU/Memory over the last N minutes instead of just current instant values. The default window of 7 days matches what every real caller passes; a short window leaves the spike/shape rule with too few 15-minute windows to see a shape.

```bash
# Check if idle over the last day
./ocp-idle-check.sh -w 1440
```

### GPU Node Detection
Automatically detects GPU nodes (NVIDIA/AMD) and reports:
- GPU count and type
- Instance flavor (e.g., g4dn.xlarge, p5.48xlarge)
- CPU and memory usage (current + windowed)
- DCGM GPU utilization as a voting criterion (see above)

### Export Results
Export to CSV (append mode) or JSON for automation and historical tracking.

**CSV Format:**
```csv
timestamp,cluster,status,cpu_result,cpu_value,memory_result,...,gpu_result,gpu_value,operators_result,...,has_gpu_nodes,gpu_node_count,gpu_flavors,gpu_node_age,...
```

**JSON Format:**
```json
{
  "status": "IDLE",
  "criteria": {
    "cpu": { "result": "IDLE", "peak": 12.0, "baseline": 3.0, "ratio": 4.0 },
    "gpu": { "result": "ACTIVE", "peak": 80.0 },
    ...
  },
  "gpu": {
    "has_gpu_nodes": true,
    "node_count": 1,
    "flavors": "NVIDIA(g4dn.xlarge)",
    "usage": { "cpu_current": "7.00", "cpu_windowed": "6.58", ... }
  }
}
```

## Use Cases

### AWS Capacity Block Monitoring
```bash
# Check if expensive ML hardware is idle
./ocp-idle-check.sh -w 1440 -c 5

if [ $? -eq 1 ]; then
    echo "Cluster idle, consider releasing reservation"
fi
```

### Scheduled Monitoring
```bash
# Cron: check hourly, log to CSV
0 * * * * /path/to/ocp-idle-check.sh -q --csv /var/log/ocp-idle-history.csv
```

## Dependencies

**Required:**
- `oc` (OpenShift CLI) - logged in to cluster
- `python3` (3.12+; standard library only, no pip installs)
- Prometheus/Thanos - for time-windowed metrics

For development (tests, linting) use [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Output Modes

### Verbose (default)
Shows full details: node lists, metrics, operator pods, GPU information, recent events.

### Quiet (`-q`)
Minimal output:
```
CPU: IDLE
Memory: ACTIVE
API Server: IDLE
GPU: N/A
Operators: IDLE
STATUS: IDLE
```

## Configuration

Defaults live at the top of `ocp_idle_check.py`; prefer overriding them with the command-line options above.

## Troubleshooting

**"oc command not found"**
- Install the OpenShift CLI and log in: `oc login`

**"Not logged in to an OpenShift cluster"**
- Run `oc whoami` to check your session

**Prometheus queries return no data**
- Check Prometheus/Thanos is available: `oc get pods -n openshift-monitoring`
- `OCP_IDLE_PROMETHEUS_URL` overrides the thanos-querier route lookup (for debugging)

**Script hangs**
- Built-in timeouts on all `oc` commands (10s) and Prometheus queries (60s instant / 180s range)
- If persistent, check cluster API responsiveness

## Development

```bash
uv sync                # create the venv (Python 3.12+)
uv run pytest          # hermetic test suite
uv run ruff check      # lint
uv run ruff format --check
```

The tests in `tests/` are hermetic — no cluster, no `oc` login, no network. The oc CLI is stubbed with strict argv matching (an undeclared `oc` invocation fails the test instead of silently returning empty output) and Prometheus is a local HTTP server keyed on the exact PromQL strings, so a changed query fails the tests instead of silently degrading on a real cluster.
