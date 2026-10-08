#!/bin/bash
#
# OpenShift Cluster Idle Detection Script
# Checks if an OCP cluster is idle based on CPU, memory, pod activity, and events
#
# Exit codes:
#   0 = Cluster is IDLE
#   1 = Cluster is ACTIVE
#   2 = Error (cannot determine state)
#

set -uo pipefail

# === CONFIGURATION ===
CPU_IDLE_THRESHOLD=15          # CPU usage below this % is considered idle (raised for system overhead)
MEMORY_IDLE_THRESHOLD=35       # Memory usage below this % is considered idle
APISERVER_IDLE_THRESHOLD=100    # API server requests/sec below this is considered idle
OPERATOR_IDLE_AGE_DAYS=7       # If operator pods are older than this, cluster is likely idle
OPERATOR_NAMESPACES="opendatahub,redhat-ods-operator,redhat-ods-applications"  # Comma-separated list of operator namespaces to check
EVENT_TIME_MINUTES=60          # Check events in last N minutes (informational only)
TIME_WINDOW_MINUTES=10         # Time window for CPU/Memory average calculations (0 = instant only)
CHECK_ML_NODES=true            # Set to false to disable ML node specific checks
ML_NODE_PATTERN="p5|p4d|g5"    # Instance types to consider as ML nodes
DEBUG_PROBE=true              # Collect fleet-debug data (DCGM availability, per-node, buckets, timings)
DEBUG_BUCKET_THRESHOLD=20     # Hypothetical 15-minute bucket rule threshold, % CPU (debug only)
VERBOSE=true                   # Set to false for minimal output
EXPORT_CSV=""                  # Path to export CSV file (empty = no export)
EXPORT_JSON=""                 # Path to export JSON file (empty = no export)
PROMETHEUS_TOKEN_ARG=""        # Pre-generated Prometheus token (empty = auto-generate)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# === COMMAND LINE ARGUMENTS ===
show_usage() {
    cat << EOF
Usage: $0 [OPTIONS]

OpenShift Cluster Idle Detection Script

Options:
  -w, --window MINUTES       Time window for CPU/Memory averages (default: $TIME_WINDOW_MINUTES)
                            Set to 0 for instant metrics only
                            Note: Very large windows (>24h) may be rejected by Prometheus
  -c, --cpu-threshold N      CPU idle threshold percentage (default: $CPU_IDLE_THRESHOLD)
  -m, --mem-threshold N      Memory idle threshold percentage (default: $MEMORY_IDLE_THRESHOLD)
  -a, --api-threshold N      API server requests/sec threshold (default: $APISERVER_IDLE_THRESHOLD)
  -e, --events MINUTES       Event history window (default: $EVENT_TIME_MINUTES)
  -o, --operator-age N       Operator pod age threshold in days (default: $OPERATOR_IDLE_AGE_DAYS)
  --operator-namespaces NS   Comma-separated operator namespaces to check
                            (default: $OPERATOR_NAMESPACES)
  --csv FILE                Export results to CSV file
  --json FILE               Export results to JSON file
  --token TOKEN             Use pre-generated Prometheus token (skip token minting)
  --debug-probe             Collect fleet-debug data: DCGM GPU metrics availability,
                            per-node CPU/memory breakdown, 15-minute bucket analysis,
                            api-server verb mix, operator pod inventory, query timings,
                            and namespace scrape labels. Adds a "debug" section to
                            --json output; never affects the verdict.
  -q, --quiet               Quiet mode - show only criteria results and status
  --no-ml-check             Skip ML node specific checks
  -h, --help                Show this help message

Examples:
  $0 -w 30                                    # Check average over last 30 minutes
  $0 -q                                       # Quiet mode - minimal output
  $0 -w 10 -c 15 -m 35                        # 10 min window, 15% CPU, 35% mem thresholds
  $0 -a 20                                    # Consider idle if API requests < 20/sec
  $0 -o 30                                    # Consider idle if operators unchanged for 30+ days
  $0 --operator-namespaces "openshift-operators"  # Check custom operator namespace
  $0 -q --csv results.csv --json results.json     # Export to CSV and JSON files

  # Use pre-generated token (useful for automation)
  TOKEN=\$(oc create token prometheus-k8s -n openshift-monitoring --duration=10m)
  $0 --token "\$TOKEN" -w 10

EOF
    exit 0
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -w|--window)
            TIME_WINDOW_MINUTES="$2"
            shift 2
            ;;
        -c|--cpu-threshold)
            CPU_IDLE_THRESHOLD="$2"
            shift 2
            ;;
        -m|--mem-threshold)
            MEMORY_IDLE_THRESHOLD="$2"
            shift 2
            ;;
        -a|--api-threshold)
            APISERVER_IDLE_THRESHOLD="$2"
            shift 2
            ;;
        -e|--events)
            EVENT_TIME_MINUTES="$2"
            shift 2
            ;;
        -o|--operator-age)
            OPERATOR_IDLE_AGE_DAYS="$2"
            shift 2
            ;;
        --operator-namespaces)
            OPERATOR_NAMESPACES="$2"
            shift 2
            ;;
        --csv)
            EXPORT_CSV="$2"
            shift 2
            ;;
        --json)
            EXPORT_JSON="$2"
            shift 2
            ;;
        --token)
            PROMETHEUS_TOKEN_ARG="$2"
            shift 2
            ;;
        --debug-probe)
            DEBUG_PROBE=true
            shift
            ;;
        -q|--quiet)
            VERBOSE=false
            shift
            ;;
        --no-ml-check)
            CHECK_ML_NODES=false
            shift
            ;;
        -h|--help)
            show_usage
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use -h or --help for usage information"
            exit 2
            ;;
    esac
done

# === FUNCTIONS ===

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1" >&2
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1" >&2
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1" >&2
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

check_oc_command() {
    if ! command -v oc &> /dev/null; then
        log_error "oc command not found. Please install OpenShift CLI."
        exit 2
    fi

    if ! oc whoami &> /dev/null; then
        log_error "Not logged into OpenShift cluster. Run 'oc login' first."
        exit 2
    fi
}

check_dependencies() {
    # Check for required tools for Prometheus queries
    if ! command -v jq &> /dev/null; then
        log_error "jq not found. This tool is required for Prometheus queries."
        log_error "Install jq: dnf install jq / apt install jq"
        exit 2
    fi

    if ! command -v curl &> /dev/null; then
        log_error "curl not found. This tool is required for Prometheus queries."
        log_error "Install curl: dnf install curl / apt install curl"
        exit 2
    fi
}

# Global token variable - minted once per session
PROMETHEUS_TOKEN=""

get_prometheus_token() {
    # Check if token was provided via command-line argument
    if [[ -n "$PROMETHEUS_TOKEN" ]]; then
        log_info "Using cached Prometheus token"
        return 0
    fi

    if [[ -n "$PROMETHEUS_TOKEN_ARG" ]]; then
        log_info "Using provided Prometheus token from command-line"
        PROMETHEUS_TOKEN="$PROMETHEUS_TOKEN_ARG"
        return 0
    fi

    # Try bearer token first (works for regular oc login sessions)
    local token
    token=$(oc whoami -t 2>/dev/null || echo "")

    if [[ -z "$token" ]]; then
        log_info "No OAuth token, minting prometheus-k8s SA token (once per session)"
        token=$(oc create token prometheus-k8s -n openshift-monitoring --duration=10m 2>/dev/null)
        rc=$?
        if [[ $rc -ne 0 || -z "$token" ]]; then
            log_error "Failed to create SA token (exit $rc)"
            return 1
        fi
    else
        [[ "$VERBOSE" == "true" ]] && log_info "Using OAuth token from oc whoami -t"
    fi
    PROMETHEUS_TOKEN="$token"
}

query_prometheus() {
    # Query Prometheus/Thanos for metrics
    # Args: $1 = PromQL query
    local query="$1"
    local result
    local response
    local status
    local error_msg

    # Try to get thanos-querier route
    local thanos_host
    thanos_host=$(timeout 5 oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}' 2>&1)
    local rc=$?

    if [[ $rc -ne 0 || -z "$thanos_host" ]]; then
        [[ "$VERBOSE" == "true" ]] && log_error "Failed to get thanos-querier route: $thanos_host"
        echo "N/A"
        return 1
    fi

    # Query Prometheus - capture response and HTTP status separately
    local http_code
    local curl_output

    curl_output=$(timeout 60 curl -sk -w "\n%{http_code}" -H "Authorization: Bearer $PROMETHEUS_TOKEN" \
        "https://$thanos_host/api/v1/query?query=$(echo "$query" | jq -sRr @uri)" 2>&1)
    rc=$?

    # Split response and HTTP code (last line)
    http_code=$(echo "$curl_output" | tail -n1)
    response=$(echo "$curl_output" | head -n-1)

    if [[ $rc -ne 0 ]]; then
        [[ "$VERBOSE" == "true" ]] && log_error "Prometheus query failed (curl exit $rc)"
        [[ "$VERBOSE" == "true" ]] && log_error "Output: ${curl_output:0:300}"
        echo "N/A"
        return 1
    fi

    # Check HTTP status code
    if [[ "$http_code" != "200" ]]; then
        [[ "$VERBOSE" == "true" ]] && log_error "Prometheus returned HTTP $http_code"
        [[ "$VERBOSE" == "true" ]] && log_error "Query was: $query"
        [[ "$VERBOSE" == "true" ]] && log_error "Response: ${response:0:500}"
        echo "N/A"
        return 1
    fi

    # Check if response is valid JSON
    if ! echo "$response" | jq empty 2>/dev/null; then
        [[ "$VERBOSE" == "true" ]] && log_error "Prometheus returned non-JSON response"
        [[ "$VERBOSE" == "true" ]] && log_error "Query was: $query"
        [[ "$VERBOSE" == "true" ]] && log_error "Response (first 500 chars): ${response:0:500}"
        echo "N/A"
        return 1
    fi

    # Extract result value
    result=$(echo "$response" | jq -r '.data.result[0].value[1] // "N/A"' 2>/dev/null)

    if [[ "$result" == "N/A" ]]; then
        [[ "$VERBOSE" == "true" ]] && log_warning "Query returned no results: $query"
        local result_data=$(echo "$response" | jq -c '.data.result' 2>/dev/null)
        [[ "$VERBOSE" == "true" ]] && log_warning "Result data: $result_data"
    fi

    echo "$result"
}

# Debug wrapper around query_prometheus: records wall-clock duration and result
# per query into QUERY_TIMINGS_LOG. Active only when DEBUG_PROBE=true.
query_prometheus_timed() {
    if [[ "$DEBUG_PROBE" != "true" ]]; then
        query_prometheus "$1"
        return
    fi
    local label="$2"
    local start_ms=$(debug_now_ms)
    local result=$(query_prometheus "$1")
    local end_ms=$(debug_now_ms)
    if [[ "$label" == "" ]]; then
        label=$(echo "$1" | head -c 120)
    fi
    debug_record_timing "$label" "$result" $((end_ms - start_ms))
    echo "$result"
}

get_node_cpu_usage_windowed() {
    # Get average CPU usage over time window using Prometheus
    local window="${TIME_WINDOW_MINUTES}m"
    local query="(1 - avg(rate(node_cpu_seconds_total{mode=\"idle\"}[${window}]))) * 100"
    local result

    result=$(query_prometheus "$query")

    if [[ "$result" == "N/A" ]]; then
        echo "N/A"
    else
        # Round to 2 decimal places
        awk -v val="$result" 'BEGIN {printf "%.2f", val}'
    fi
}

get_node_memory_usage_windowed() {
    # Get average memory usage over time window using Prometheus
    local window="${TIME_WINDOW_MINUTES}m"
    local query="(1 - avg_over_time((sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes))[${window}:])) * 100"
    local result

    result=$(query_prometheus "$query")

    if [[ "$result" == "N/A" ]]; then
        echo "N/A"
    else
        # Round to 2 decimal places
        awk -v val="$result" 'BEGIN {printf "%.2f", val}'
    fi
}

get_apiserver_request_rate() {
    # Get API server request rate over time window using Prometheus
    # Returns requests per second
    local window="${TIME_WINDOW_MINUTES}m"
    local query

    if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
        # Get rate of API requests over the time window
        query="sum(rate(apiserver_request_total[${window}]))"
    else
        # Instant rate (5m default for rate function)
        query="sum(rate(apiserver_request_total[5m]))"
    fi

    local result
    result=$(query_prometheus "$query")

    if [[ "$result" == "N/A" ]]; then
        echo "N/A"
    else
        # Round to 2 decimal places
        awk -v val="$result" 'BEGIN {printf "%.2f", val}'
    fi
}

get_apiserver_request_rate_breakdown() {
    # Get breakdown of API requests by verb (GET, POST, etc.) for verbose output
    local window="${TIME_WINDOW_MINUTES}m"
    local query
    local response
    local thanos_host
    local token
    local status
    local error_msg

    if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
        query="sum by (verb) (rate(apiserver_request_total[${window}]))"
    else
        query="sum by (verb) (rate(apiserver_request_total[5m]))"
    fi

    # Get thanos-querier route
    thanos_host=$(timeout 5 oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}' 2>&1)
    if [[ $? -ne 0 || -z "$thanos_host" ]]; then
        echo "  Breakdown not available (could not get thanos-querier route)"
        return 1
    fi
    # Query Prometheus for full breakdown
    response=$(timeout 60 curl -sk -H "Authorization: Bearer $PROMETHEUS_TOKEN" \
        "https://$thanos_host/api/v1/query?query=$(echo "$query" | jq -sRr @uri)" 2>&1)

    if [[ $? -ne 0 ]]; then
        echo "  Breakdown not available (query failed)"
        [[ "$VERBOSE" == "true" ]] && log_error "Breakdown query curl failed: ${response:0:200}"
        return 1
    fi

    # Check if response is valid
    status=$(echo "$response" | jq -r '.status // "error"' 2>&1)
    if [[ "$status" != "success" ]]; then
        error_msg=$(echo "$response" | jq -r '.error // .errorType // "Unknown error"' 2>&1)
        echo "  Breakdown not available (API error: $error_msg)"
        return 1
    fi

    # Parse and display breakdown
    local breakdown
    breakdown=$(echo "$response" | jq -r '.data.result[] | "\(.metric.verb): \(.value[1])"' 2>&1)

    if [[ -z "$breakdown" ]]; then
        echo "  No breakdown data available"
        return 1
    fi

    echo "$breakdown" | awk '{printf "  %s (%.2f req/s)\n", $1, $2}'
}

get_node_cpu_usage() {
    # Returns average CPU usage across all nodes
    local cpu_data
    cpu_data=$(timeout 10 oc adm top nodes --no-headers 2>/dev/null | awk '{gsub(/%/,"",$3); sum+=$3; count++} END {if(count>0) print sum/count; else print "N/A"}' || echo "N/A")
    echo "$cpu_data"
}

get_node_memory_usage() {
    # Returns average memory usage across all nodes
    local mem_data
    mem_data=$(timeout 10 oc adm top nodes --no-headers 2>/dev/null | awk '{gsub(/%/,"",$5); sum+=$5; count++} END {if(count>0) print sum/count; else print "N/A"}' || echo "N/A")
    echo "$mem_data"
}

get_ml_node_usage() {
    # Check ML nodes specifically if they exist (instant metrics)
    local ml_nodes
    ml_nodes=$(timeout 10 oc get nodes -o json 2>/dev/null | jq -r ".items[] | select(.metadata.labels.\"node.kubernetes.io/instance-type\" | test(\"$ML_NODE_PATTERN\")) | .metadata.name" 2>/dev/null || echo "")

    if [[ -z "$ml_nodes" ]]; then
        echo "N/A"
        return
    fi

    local ml_cpu_sum=0
    local ml_mem_sum=0
    local count=0

    while IFS= read -r node; do
        if [[ -n "$node" ]]; then
            local node_stats
            node_stats=$(timeout 10 oc adm top node "$node" --no-headers 2>/dev/null || echo "")
            if [[ -n "$node_stats" ]]; then
                local cpu=$(echo "$node_stats" | awk '{gsub(/%/,"",$3); print $3}')
                local mem=$(echo "$node_stats" | awk '{gsub(/%/,"",$5); print $5}')
                ml_cpu_sum=$(awk -v sum="$ml_cpu_sum" -v val="$cpu" 'BEGIN {print sum + val}')
                ml_mem_sum=$(awk -v sum="$ml_mem_sum" -v val="$mem" 'BEGIN {print sum + val}')
                ((count++))
            fi
        fi
    done <<< "$ml_nodes"

    if [[ $count -gt 0 ]]; then
        local ml_cpu_avg=$(awk -v sum="$ml_cpu_sum" -v cnt="$count" 'BEGIN {printf "%.2f", sum / cnt}')
        local ml_mem_avg=$(awk -v sum="$ml_mem_sum" -v cnt="$count" 'BEGIN {printf "%.2f", sum / cnt}')
        echo "${ml_cpu_avg}%CPU,${ml_mem_avg}%MEM"
    else
        echo "N/A"
    fi
}

get_ml_node_usage_windowed() {
    # Check ML nodes CPU usage over time window using Prometheus
    local window="${TIME_WINDOW_MINUTES}m"

    # Get ML node names
    local ml_nodes
    ml_nodes=$(timeout 10 oc get nodes -o json 2>/dev/null | jq -r ".items[] | select(.metadata.labels.\"node.kubernetes.io/instance-type\" | test(\"$ML_NODE_PATTERN\")) | .metadata.name" 2>/dev/null || echo "")

    if [[ -z "$ml_nodes" ]]; then
        echo "N/A"
        return
    fi

    # Build node filter regex
    local node_filter=$(echo "$ml_nodes" | tr '\n' '|' | sed 's/|$//')

    # Query Prometheus for ML node CPU
    local query="(1 - avg(rate(node_cpu_seconds_total{mode=\"idle\",instance=~\"${node_filter}.*\"}[${window}]))) * 100"
    local cpu_result=$(query_prometheus "$query")

    if [[ "$cpu_result" == "N/A" ]]; then
        echo "N/A"
    else
        cpu_result=$(awk -v val="$cpu_result" 'BEGIN {printf "%.2f", val}')
        echo "${cpu_result}%CPU"
    fi
}

get_all_gpu_node_data() {
    # Get all GPU node information in one API call
    # Returns: one line per GPU node in format: name|vendor|gpu_count|instance_type
    # Returns "N/A" if no GPU nodes found

    local nodes_json
    nodes_json=$(timeout 10 oc get nodes -o json 2>/dev/null)

    if [[ -z "$nodes_json" ]]; then
        echo "N/A"
        return 1
    fi

    # Extract GPU nodes with all their info in one pass
    local gpu_data
    gpu_data=$(echo "$nodes_json" | jq -r '.items[] |
        select(.status.capacity["nvidia.com/gpu"] != null or .status.capacity["amd.com/gpu"] != null) |
        .metadata.name + "|" +
        (if .status.capacity["nvidia.com/gpu"] != null then "NVIDIA"
         elif .status.capacity["amd.com/gpu"] != null then "AMD"
         else "Unknown" end) + "|" +
        (.status.capacity["nvidia.com/gpu"] // .status.capacity["amd.com/gpu"] // "0") + "|" +
        (.metadata.labels["node.kubernetes.io/instance-type"] // "unknown")
    ' 2>/dev/null)

    if [[ -z "$gpu_data" ]]; then
        echo "N/A"
        return 1
    fi

    echo "$gpu_data"
}

get_gpu_nodes() {
    # Legacy function - returns node names only for backward compatibility
    # Format: nodenames:count:vendor
    local gpu_data=$(get_all_gpu_node_data)

    if [[ "$gpu_data" == "N/A" ]]; then
        echo "N/A:0:N/A"
        return
    fi

    local node_names=$(echo "$gpu_data" | cut -d'|' -f1 | tr '\n' ' ')
    local node_count=$(echo "$gpu_data" | wc -l)
    local first_vendor=$(echo "$gpu_data" | head -1 | cut -d'|' -f2)

    echo "$node_names:$node_count:$first_vendor"
}

get_gpu_machines() {
    # Check for GPU machines by pattern <clustername>-*-gpu-*
    local cluster_name
    cluster_name=$(oc whoami --show-server 2>/dev/null | sed 's/.*api\.\(.*\):.*/\1/' | cut -d'.' -f1 || echo "")

    if [[ -z "$cluster_name" ]]; then
        echo "N/A:0"
        return
    fi

    # Look for machines with gpu pattern
    local gpu_machines=$(timeout 10 oc get machines -n openshift-machine-api -o json 2>/dev/null | \
        jq -r ".items[] | select(.metadata.name | test(\"${cluster_name}-.*-gpu-\")) | .metadata.name" 2>/dev/null || echo "")

    if [[ -z "$gpu_machines" ]]; then
        echo "N/A:0"
    else
        local machine_count=$(echo "$gpu_machines" | wc -l)
        echo "$gpu_machines:$machine_count"
    fi
}

get_gpu_info() {
    # Get comprehensive GPU information
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)
    local node_count=$(echo "$gpu_node_info" | cut -d':' -f2)
    local gpu_vendor=$(echo "$gpu_node_info" | cut -d':' -f3)

    if [[ "$nodes" == "N/A" ]]; then
        # No GPU nodes found, check for GPU machines
        local gpu_machine_info=$(get_gpu_machines)
        local machines=$(echo "$gpu_machine_info" | cut -d':' -f1)
        local machine_count=$(echo "$gpu_machine_info" | cut -d':' -f2)

        if [[ "$machines" == "N/A" ]]; then
            echo "N/A:0:N/A:0"
        else
            echo "N/A:0:$machines:$machine_count"
        fi
    else
        # GPU nodes found
        echo "$nodes:$node_count:N/A:0"
    fi
}

get_gpu_node_age() {
    # Get the age of GPU nodes (oldest GPU node)
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)

    if [[ "$nodes" == "N/A" ]]; then
        echo "N/A"
        return
    fi

    local oldest_age=""
    while IFS= read -r node; do
        if [[ -n "$node" ]]; then
            local node_age=$(oc get node "$node" -o jsonpath='{.metadata.creationTimestamp}' 2>/dev/null)
            if [[ -n "$node_age" ]] && [[ -z "$oldest_age" || "$node_age" < "$oldest_age" ]]; then
                oldest_age="$node_age"
            fi
        fi
    done <<< "$nodes"

    if [[ -n "$oldest_age" ]]; then
        echo "$oldest_age"
    else
        echo "N/A"
    fi
}

get_gpu_flavors() {
    # Get GPU flavors/types from all GPU nodes
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)

    if [[ "$nodes" == "N/A" ]]; then
        echo "N/A"
        return
    fi

    local flavors=""
    while IFS= read -r node; do
        if [[ -n "$node" ]]; then
            local gpu_vendor=$(oc get node "$node" -o json 2>/dev/null | \
                jq -r 'if .status.capacity["nvidia.com/gpu"] != null then "NVIDIA" elif .status.capacity["amd.com/gpu"] != null then "AMD" else "Unknown" end' 2>/dev/null)
            local instance_type=$(oc get node "$node" -o jsonpath='{.metadata.labels.node\.kubernetes\.io/instance-type}' 2>/dev/null)

            local gpu_info="${gpu_vendor}"
            if [[ -n "$instance_type" ]]; then
                gpu_info="${gpu_info}(${instance_type})"
            fi

            if [[ -z "$flavors" ]]; then
                flavors="$gpu_info"
            elif [[ "$flavors" != *"$flavor"* ]]; then
                flavors="${flavors},${gpu_info}"
            fi
        fi
    done <<< "$nodes"

    if [[ -n "$flavors" ]]; then
        echo "$flavors"
    else
        echo "N/A"
    fi
}

get_gpu_node_usage() {
    # Get instant CPU and memory usage for GPU nodes
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)

    if [[ "$nodes" == "N/A" ]]; then
        echo "N/A"
        return
    fi

    local total_cpu=0
    local total_mem=0
    local count=0

    while IFS= read -r node; do
        if [[ -n "$node" ]]; then
            local node_stats
            node_stats=$(timeout 10 oc adm top node "$node" --no-headers 2>/dev/null || echo "")
            if [[ -n "$node_stats" ]]; then
                local cpu=$(echo "$node_stats" | awk '{gsub(/%/,"",$3); print $3}')
                local mem=$(echo "$node_stats" | awk '{gsub(/%/,"",$5); print $5}')
                total_cpu=$(awk -v sum="$total_cpu" -v val="$cpu" 'BEGIN {print sum + val}')
                total_mem=$(awk -v sum="$total_mem" -v val="$mem" 'BEGIN {print sum + val}')
                ((count++))
            fi
        fi
    done <<< "$nodes"

    if [[ $count -gt 0 ]]; then
        local instant_cpu=$(awk -v sum="$total_cpu" -v cnt="$count" 'BEGIN {printf "%.2f", sum / cnt}')
        local instant_mem=$(awk -v sum="$total_mem" -v cnt="$count" 'BEGIN {printf "%.2f", sum / cnt}')
        echo "${instant_cpu}%CPU,${instant_mem}%MEM"
    else
        echo "N/A"
    fi
}

get_gpu_node_cpu_usage_windowed() {
    # Get GPU nodes CPU usage over time window using Prometheus
    local window="${TIME_WINDOW_MINUTES}m"

    # Get GPU node names
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)

    if [[ "$nodes" == "N/A" ]]; then
        echo "N/A"
        return
    fi

    # Build node filter regex
    local node_filter=$(echo "$nodes" | tr '\n' '|' | sed 's/|$//')

    # Query Prometheus for GPU node CPU
    local query="(1 - avg(rate(node_cpu_seconds_total{mode=\"idle\",instance=~\"${node_filter}.*\"}[${window}]))) * 100"
    local cpu_result=$(query_prometheus "$query")

    if [[ "$cpu_result" == "N/A" ]]; then
        echo "N/A"
    else
        awk -v val="$cpu_result" 'BEGIN {printf "%.2f", val}'
    fi
}

get_gpu_node_memory_usage_windowed() {
    # Get GPU nodes memory usage over time window using Prometheus
    local window="${TIME_WINDOW_MINUTES}m"

    # Get GPU node names
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)

    if [[ "$nodes" == "N/A" ]]; then
        echo "N/A"
        return
    fi

    # Build node filter regex for memory query
    local node_filter=$(echo "$nodes" | tr '\n' '|' | sed 's/|$//')

    # Query Prometheus for GPU node memory usage
    # Calculate: (1 - (available / total)) * 100
    local query="(1 - avg_over_time((avg(node_memory_MemAvailable_bytes{instance=~\"${node_filter}.*\"}) / avg(node_memory_MemTotal_bytes{instance=~\"${node_filter}.*\"}))[${window}:])) * 100"
    local mem_result=$(query_prometheus "$query")

    if [[ "$mem_result" == "N/A" ]]; then
        echo "N/A"
    else
        awk -v val="$mem_result" 'BEGIN {printf "%.2f", val}'
    fi
}

get_pod_counts() {
    local total_pods
    local running_pods

    total_pods=$(timeout 10 oc get pods -A --no-headers 2>/dev/null | wc -l || echo 0)
    running_pods=$(timeout 10 oc get pods -A --no-headers 2>/dev/null | grep -c "Running" || echo 0)

    echo "${total_pods}:${running_pods}"
}

convert_age_to_days() {
    # Convert Kubernetes age format to days
    local age="$1"

    if [[ "$age" =~ ^([0-9]+)d ]]; then
        echo "${BASH_REMATCH[1]}"
    elif [[ "$age" =~ ^([0-9]+)h ]]; then
        echo "0"
    elif [[ "$age" =~ ^([0-9]+)m ]]; then
        echo "0"
    elif [[ "$age" =~ ^([0-9]+)s ]]; then
        echo "0"
    else
        echo "0"
    fi
}

get_operator_age() {
    # Check operator pods in specified namespaces and return their age
    local operator_info=""
    local oldest_age_days=0
    local found_operators=""

    # Split comma-separated namespaces
    IFS=',' read -ra NAMESPACES <<< "$OPERATOR_NAMESPACES"

    for ns in "${NAMESPACES[@]}"; do
        # Trim whitespace
        ns=$(echo "$ns" | xargs)

        # Check if namespace exists
        if ! timeout 5 oc get namespace "$ns" &>/dev/null; then
            continue
        fi

        # Get operator pods from this namespace
        local ns_pods=$(timeout 10 oc get pods -n "$ns" --no-headers 2>/dev/null | grep -E "controller-manager|operator|dashboard" | head -5)

        if [[ -n "$ns_pods" ]]; then
            while IFS= read -r pod; do
                local pod_name=$(echo "$pod" | awk '{print $1}')
                local pod_age=$(echo "$pod" | awk '{print $5}')
                local age_days=$(convert_age_to_days "$pod_age")

                if [[ $age_days -gt $oldest_age_days ]]; then
                    oldest_age_days=$age_days
                fi
            done <<< "$ns_pods"

            # Track which namespaces had operators
            if [[ -z "$found_operators" ]]; then
                found_operators="$ns"
            else
                found_operators="${found_operators},$ns"
            fi
        fi
    done

    if [[ -z "$found_operators" ]]; then
        echo "N/A:0"
    else
        operator_info="${found_operators}:${oldest_age_days}d"
        echo "$operator_info"
    fi
}

check_operator_events() {
    # Check for recent operator reconciliation or significant events
    local total_events=0

    # Split comma-separated namespaces
    IFS=',' read -ra NAMESPACES <<< "$OPERATOR_NAMESPACES"

    for ns in "${NAMESPACES[@]}"; do
        # Trim whitespace
        ns=$(echo "$ns" | xargs)

        # Check if namespace exists
        if ! timeout 5 oc get namespace "$ns" &>/dev/null; then
            continue
        fi

        # Check operator events in this namespace
        local ns_events=$(timeout 10 oc get events -n "$ns" --sort-by='.lastTimestamp' 2>/dev/null | \
            tail -20 | \
            grep -Eic "reconcil|created|updated|scaled" 2>/dev/null || echo 0)

        # Sanitize values (remove whitespace, ensure numeric)
        ns_events=$(echo "$ns_events" | tr -d '[:space:]' | grep -o '[0-9]*' || echo 0)
        ns_events=${ns_events:-0}

        total_events=$((total_events + ns_events))
    done

    echo "$total_events"
}

get_recent_pod_activity() {
    # Count pod-related events in the last N minutes (simplified)
    local pod_events
    local event_output

    # Just count recent events, simplified approach
    event_output=$(timeout 10 oc get events -A --sort-by='.lastTimestamp' 2>/dev/null | tail -20)

    if [[ -n "$event_output" ]]; then
        pod_events=$(echo "$event_output" | grep -Ec "Pod|Deployment|ReplicaSet|Job" 2>/dev/null || echo 0)
    else
        pod_events=0
    fi

    # Sanitize: remove whitespace and ensure numeric, take first number only
    pod_events=$(echo "$pod_events" | head -1 | tr -d '[:space:]' | grep -o '^[0-9]*' || echo 0)
    pod_events=${pod_events:-0}

    echo "$pod_events"
}

# === DEBUG PROBE (experimental, never affects the verdict) ===
# Collects the evidence needed to evaluate fixes for the known false-idle
# problems, plus the raw material (api-server verb mix, operator pod
# inventory) for judging whether the existing criteria thresholds are
# meaningful. All output goes to a "debug" section in the JSON export and to
# stderr logs; the standard criteria/verdict logic is untouched.

QUERY_TIMINGS_LOG=$(mktemp /tmp/ocp-idle-query-timings.XXXXXX)

# Append one "label|result|duration" line to the timings log, flattened back
# into JSON by debug_collect_all. Active only when DEBUG_PROBE=true.
debug_record_timing() {
    [[ "$DEBUG_PROBE" == "true" ]] && echo "$1|$2|${3}ms" >> "$QUERY_TIMINGS_LOG"
}

# Run a raw query and return the FULL JSON response (not just first value).
# Used by the debug probes where we need result counts and label sets.
# $2 (optional) labels the entry recorded in the query-timings log; without
# it the probes are invisible to query_timings, which is why the first fleet
# run recorded a single timing next to a dozen queries.
debug_query_raw() {
    local query="$1"
    local label="${2:-unlabeled}"
    local thanos_host
    thanos_host=$(timeout 5 oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}' 2>/dev/null)
    if [[ -z "$thanos_host" ]]; then
        debug_record_timing "$label" "route-unavailable" 0
        echo '{"status":"error","errorType":"route-unavailable"}'
        return 1
    fi
    local start_ms response end_ms status
    start_ms=$(debug_now_ms)
    response=$(timeout 120 curl -sk -H "Authorization: Bearer $PROMETHEUS_TOKEN" \
        "https://$thanos_host/api/v1/query?query=$(echo "$query" | jq -sRr @uri)" 2>/dev/null)
    end_ms=$(debug_now_ms)
    status=$(echo "$response" | jq -r '.status // "non-json"' 2>/dev/null)
    debug_record_timing "$label" "${status:-non-json}" $(( end_ms - start_ms ))
    echo "$response"
}

# Range query against the same thanos-querier route: the expression is
# evaluated server-side at each step between start and end (epoch seconds),
# and the caller aggregates the returned points client-side. This is the
# standard API for "what happened over this period" and avoids subquery
# evaluation, whose window coverage the first fleet run found unreliable.
# $5 (optional) labels the timings-log entry.
debug_query_range_raw() {
    local query="$1" start_s="$2" end_s="$3" step_s="$4"
    local label="${5:-unlabeled-range}"
    local thanos_host
    thanos_host=$(timeout 5 oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}' 2>/dev/null)
    if [[ -z "$thanos_host" ]]; then
        debug_record_timing "$label" "route-unavailable" 0
        echo '{"status":"error","errorType":"route-unavailable"}'
        return 1
    fi
    local start_ms response end_ms status
    start_ms=$(debug_now_ms)
    response=$(timeout 180 curl -sk -G -H "Authorization: Bearer $PROMETHEUS_TOKEN" \
        --data-urlencode "query=$query" \
        --data-urlencode "start=$start_s" --data-urlencode "end=$end_s" \
        --data-urlencode "step=${step_s}s" \
        "https://$thanos_host/api/v1/query_range" 2>/dev/null)
    end_ms=$(debug_now_ms)
    status=$(echo "$response" | jq -r '.status // "non-json"' 2>/dev/null)
    debug_record_timing "$label" "${status:-non-json}" $(( end_ms - start_ms ))
    echo "$response"
}

# JSON-escape a raw string for embedding in the debug JSON output.
# Note: no -r on jq - we want the quoted, escaped JSON string form.
debug_json_escape() {
    # printf, not echo: jq -sR slurps the whole input, and echo's trailing
    # newline would land inside the string as a literal \n.
    printf '%s' "$1" | jq -sR .
}

# Current time in ms. GNU date supports %3N; BSD date does not and prints
# garbage - fall back to whole seconds in that case.
debug_now_ms() {
    local t
    t=$(date +%s%3N 2>/dev/null)
    if [[ "$t" =~ ^[0-9]{13}$ ]]; then
        echo "$t"
    else
        echo $(( $(date +%s) * 1000 ))
    fi
}

# DCGM availability probe: settles whether GPU utilization series exist in
# the platform metrics store, and why not if they don't. When they do exist,
# also records GPU activity over the full window (peak and average
# utilization, peak/average VRAM, sample coverage): a GPU that ran anything
# at all during the week shows up here even when the instant sample reads
# 0%. Plain range selectors throughout - no subqueries - so these do not
# depend on the evaluation path the spike probe found unreliable.
debug_probe_dcgm() {
    local window="${TIME_WINDOW_MINUTES}m"
    local probe='{'
    local util_raw fb_raw series_count dcgm_pods ns_labels thanos_route

    if [[ "$VERBOSE" == "true" ]]; then
        echo "--- DEBUG: DCGM GPU metrics availability probe ---" >&2
    fi

    # 1. DCGM_FI_DEV_GPU_UTIL - the headline question
    util_raw=$(debug_query_raw 'DCGM_FI_DEV_GPU_UTIL' "dcgm_gpu_util_instant")
    series_count=$(echo "$util_raw" | jq -r '.data.result | length' 2>/dev/null || echo "0")
    probe+="\"dcgm_gpu_util_series\": ${series_count:-0},"
    if [[ "${series_count:-0}" -gt 0 ]] 2>/dev/null; then
        # Sample values to show what utilization actually looks like. The
        # host label varies by dcgm-exporter version and deployment
        # (Hostname, node, kubernetes_node, or only the target's instance) -
        # coalesce them all; the first run printed null by assuming one.
        local util_samples util_win_max util_win_avg util_win_cnt
        util_samples=$(echo "$util_raw" | jq -c '[.data.result[] | {node: ((.metric.node // .metric.Hostname // .metric.kubernetes_node // .metric.instance // .metric.pod) | sub(":[0-9]+$"; "")), gpu: .metric.gpu, util: .value[1]}] | .[0:8]' 2>/dev/null || echo "[]")
        probe+="\"dcgm_gpu_util_samples\": ${util_samples},"
        util_win_max=$(debug_query_raw "max by (gpu) (max_over_time(DCGM_FI_DEV_GPU_UTIL[${window}]))" "dcgm_gpu_util_max_window")
        util_win_max=$(echo "$util_win_max" | jq -c '[.data.result[] | {gpu: .metric.gpu, max_util_pct: .value[1]}]' 2>/dev/null || echo "[]")
        util_win_avg=$(debug_query_raw "avg by (gpu) (avg_over_time(DCGM_FI_DEV_GPU_UTIL[${window}]))" "dcgm_gpu_util_avg_window")
        util_win_avg=$(echo "$util_win_avg" | jq -c '[.data.result[] | {gpu: .metric.gpu, avg_util_pct: .value[1]}]' 2>/dev/null || echo "[]")
        util_win_cnt=$(debug_query_raw "count by (gpu) (count_over_time(DCGM_FI_DEV_GPU_UTIL[${window}]))" "dcgm_gpu_util_samples_window")
        util_win_cnt=$(echo "$util_win_cnt" | jq -c '[.data.result[] | {gpu: .metric.gpu, samples: .value[1]}]' 2>/dev/null || echo "[]")
        probe+="\"dcgm_gpu_util_window\": {\"max\": ${util_win_max}, \"avg\": ${util_win_avg}, \"samples\": ${util_win_cnt}},"
    fi

    # 2. DCGM_FI_DEV_FB_USED - VRAM usage (frame buffer memory used in MiB).
    # Average vs peak separates "a model was resident essentially all week"
    # (avg ~= max) from "something loaded briefly and freed" (avg << max).
    fb_raw=$(debug_query_raw 'DCGM_FI_DEV_FB_USED' "dcgm_fb_used_instant")
    series_count=$(echo "$fb_raw" | jq -r '.data.result | length' 2>/dev/null || echo "0")
    probe+="\"dcgm_fb_used_series\": ${series_count:-0},"
    if [[ "${series_count:-0}" -gt 0 ]] 2>/dev/null; then
        local fb_samples fb_win_max fb_win_avg
        fb_samples=$(echo "$fb_raw" | jq -c '[.data.result[] | {node: ((.metric.node // .metric.Hostname // .metric.kubernetes_node // .metric.instance // .metric.pod) | sub(":[0-9]+$"; "")), gpu: .metric.gpu, fb_used_mib: .value[1]}] | .[0:8]' 2>/dev/null || echo "[]")
        probe+="\"dcgm_fb_used_samples\": ${fb_samples},"
        fb_win_max=$(debug_query_raw "max by (gpu) (max_over_time(DCGM_FI_DEV_FB_USED[${window}]))" "dcgm_fb_used_max_window")
        fb_win_max=$(echo "$fb_win_max" | jq -c '[.data.result[] | {gpu: .metric.gpu, max_fb_used_mib: .value[1]}]' 2>/dev/null || echo "[]")
        fb_win_avg=$(debug_query_raw "avg by (gpu) (avg_over_time(DCGM_FI_DEV_FB_USED[${window}]))" "dcgm_fb_used_avg_window")
        fb_win_avg=$(echo "$fb_win_avg" | jq -c '[.data.result[] | {gpu: .metric.gpu, avg_fb_used_mib: .value[1]}]' 2>/dev/null || echo "[]")
        probe+="\"dcgm_fb_used_window\": {\"max\": ${fb_win_max}, \"avg\": ${fb_win_avg}},"
    fi

    # 3. Is dcgm-exporter deployed anywhere? (pod presence) - match by pod
    # NAME, not label: the GPU operator's pods don't carry
    # app=dcgm-exporter, which is why the first run reported 0 pods next to
    # live DCGM series.
    local dcgm_pod_list
    # Match by name: GPU operator pods carry no app= selector, and a bare
    # /dcgm/ also catches the operator's non-exporter dcgm service pods.
    dcgm_pod_list=$(timeout 10 oc get pods -A --no-headers 2>/dev/null | awk '$2 ~ /dcgm-exporter/ {print $1 "/" $2}')
    dcgm_pods=$(printf '%s' "$dcgm_pod_list" | awk 'END {print NR}')
    probe+="\"dcgm_exporter_pods\": ${dcgm_pods:-0},"
    probe+="\"dcgm_exporter_pod_names\": $(printf '%s' "$dcgm_pod_list" | jq -Rsc 'split("\n") | map(select(length > 0)) | .[0:8]'),"

    # 4. Namespace scrape-enabling labels (platform Prometheus only scrapes
    # ServiceMonitors in namespaces labeled openshift.io/cluster-monitoring=true)
    ns_labels=$(timeout 10 oc get ns -L openshift.io/cluster-monitoring 2>/dev/null | grep -Ei 'gpu|dcgm|nvidia' || echo "(none found)")
    probe+="\"gpu_namespace_labels\": $(debug_json_escape "$ns_labels"),"

    # 5. Sanity check that the query path itself works (node metric should always return)
    local sanity
    sanity=$(debug_query_raw 'count(node_cpu_seconds_total)' "sanity_node_series")
    local sanity_count
    sanity_count=$(echo "$sanity" | jq -r '.data.result[0].value[1] // "N/A"' 2>/dev/null)
    probe+="\"sanity_node_series_count\": \"${sanity_count:-N/A}\","

    # 6. Can thanos-querier be reached at all
    thanos_route=$(timeout 5 oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}' 2>/dev/null)
    probe+="\"thanos_route_reachable\": $( [[ -n "$thanos_route" ]] && echo true || echo false ),"

    probe="${probe%,}}"
    echo "$probe"
}

# Per-node breakdown: shows how much node dilution is happening on this cluster.
debug_probe_per_node() {
    local window="${TIME_WINDOW_MINUTES}m"
    local probe='{'
    local raw

    if [[ "$VERBOSE" == "true" ]]; then
        echo "--- DEBUG: per-node CPU/memory breakdown ---" >&2
    fi

    # Per-node windowed CPU (what the main check averages into one number).
    # Group by (node, instance): node_cpu_seconds_total on OCP carries no
    # "node" label, so grouping by node alone collapses the whole cluster
    # into a single unlabeled series (that is why the first run printed
    # node: null and one "per-node" number). Grouping by both yields one
    # series per node everywhere.
    raw=$(debug_query_raw "(1 - avg by (node, instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[${window}]))) * 100" "per_node_cpu_windowed")
    local per_node_cpu
    per_node_cpu=$(echo "$raw" | jq -c '[.data.result[] | {node: ((.metric.node // .metric.instance // "?") | sub(":[0-9]+$"; "")), cpu_pct: .value[1]}]' 2>/dev/null || echo "[]")
    probe+="\"per_node_cpu_windowed\": ${per_node_cpu},"

    # Cluster average as the main check computes it, for side-by-side comparison
    local cluster_avg
    cluster_avg=$(query_prometheus_timed "(1 - avg(rate(node_cpu_seconds_total{mode=\"idle\"}[${window}]))) * 100" "debug_cluster_avg_cpu")
    probe+="\"cluster_avg_cpu_windowed\": \"${cluster_avg}\","

    # Per-node windowed memory (same node/instance grouping fix)
    raw=$(debug_query_raw "100 * (1 - avg_over_time((avg by (node, instance) (node_memory_MemAvailable_bytes) / avg by (node, instance) (node_memory_MemTotal_bytes))[${window}:]))" "per_node_mem_windowed")
    local per_node_mem
    per_node_mem=$(echo "$raw" | jq -c '[.data.result[] | {node: ((.metric.node // .metric.instance // "?") | sub(":[0-9]+$"; "")), mem_pct: .value[1]}]' 2>/dev/null || echo "[]")
    probe+="\"per_node_mem_windowed\": ${per_node_mem},"

    # Raw instant per-node (no averaging) for spike visibility
    local top_raw
    top_raw=$(timeout 10 oc adm top nodes --no-headers 2>/dev/null | awk '{print $1 "|" $3 "|" $5}')
    if [[ -n "$top_raw" ]]; then
        local instant_json
        instant_json=$(echo "$top_raw" | awk -F'|' '{printf "%s{\"node\": \"%s\", \"cpu_pct\": \"%s\", \"mem_pct\": \"%s\"}", (NR>1 ? "," : ""), $1, $2, $3}')
        probe+="\"per_node_instant\": [${instant_json}],"
    fi

    probe="${probe%,}}"
    echo "$probe"
}

# Spike/activity probe: was there ANY CPU activity worth noticing during the
# window, not just the week-long average the main check uses? A cluster can
# average 21% CPU while sitting at 2% nearly all week - the average alone
# can't tell those two stories apart.
#
# The detector answers one question: did any 15-minute-average CPU value in
# the window exceed the threshold? Two data sources for that one question,
# because the first fleet run showed a single-source version lying: a
# subquery max reported 14.7% as the highest 15-minute average of the week
# while the plain week-long average was 21.5% - impossible on the same data,
# so the subquery evidently evaluated only part of the window (Thanos partial
# responses are the prime suspect), and it cannot prove its own coverage.
#   query_range    - the 15-minute-average expression evaluated server-side
#                    at every step via /api/v1/query_range; max,
#                    when-it-happened, and time-above-threshold computed
#                    client-side from the returned points. The primary
#                    source: it reports exactly how much of the window it
#                    saw, so a partial answer is labeled, not silent.
#   recording_rule - OCP's own node:node_cpu_utilisation:ratio_5m/1h series
#                    when present: max_over_time over a plain range
#                    selector, no subquery and no long rate window. The
#                    fallback if query_range comes back with holes.
debug_probe_spikes() {
    local window="${TIME_WINDOW_MINUTES}m"
    local step_s=900
    local expected_points=$(( TIME_WINDOW_MINUTES * 60 / step_s + 1 ))
    local probe='{'
    probe+="\"bucket_threshold_pct\": ${DEBUG_BUCKET_THRESHOLD},"

    if [[ "$VERBOSE" == "true" ]]; then
        echo "--- DEBUG: spike/activity analysis (hypothetical rule, verdict not affected) ---" >&2
    fi

    # --- Primary source: query_range, one server-side evaluation per step ---
    local range_json range_summary="error" range_min_cov="n/a" raw_range range_nodes range_active
    local end_s start_s
    end_s=$(date +%s)
    start_s=$(( end_s - TIME_WINDOW_MINUTES * 60 ))
    raw_range=$(debug_query_range_raw \
        "(1 - avg by (node, instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[15m]))) * 100" \
        "$start_s" "$end_s" "$step_s" "spikes_query_range")
    if [[ "$(echo "$raw_range" | jq -r '.status' 2>/dev/null)" == "success" ]]; then
        range_nodes=$(echo "$raw_range" | jq -c --argjson thr "$DEBUG_BUCKET_THRESHOLD" --argjson expected "$expected_points" \
            '[.data.result[] | {
                node: ((.metric.node // .metric.instance // "?") | sub(":[0-9]+$"; "")),
                points: (.values | length),
                coverage_pct: (((.values | length) * 10000 / $expected | floor) / 100),
                coverage_start: (if (.values | length) > 0 then (.values[0][0] | todate) else null end),
                coverage_end: (if (.values | length) > 0 then (.values[-1][0] | todate) else null end),
                max_15m_avg_cpu_pct: ([.values[][1] | tonumber] | max),
                max_at: (if (.values | length) > 0 then (.values | max_by(.[1] | tonumber) | .[0] | todate) else null end),
                windows_above_threshold: ([.values[][1] | tonumber | select(. > $thr)] | length),
                pct_windows_above_threshold: ((([.values[][1] | tonumber | select(. > $thr)] | length) * 10000 / (.values | length) | floor) / 100),
                would_flag_active: (([.values[][1] | tonumber] | max) > $thr)
            }]' 2>/dev/null || echo "[]")
        range_active=$(echo "$range_nodes" | jq '[.[] | select(.would_flag_active)] | length' 2>/dev/null || echo "0")
        range_min_cov=$(echo "$range_nodes" | jq -r 'if length > 0 then ([.[].coverage_pct] | min | tostring) else "0" end')
        range_json="{\"status\": \"ok\", \"step_seconds\": ${step_s}, \"expected_points\": ${expected_points}, \"per_node\": ${range_nodes}, \"verdict\": $( [[ "$range_active" -gt 0 ]] && echo '"ACTIVE"' || echo '"IDLE"' )}"
        range_summary="max $(echo "$range_nodes" | jq -r 'if length > 0 then ([.[].max_15m_avg_cpu_pct] | max | floor | tostring) + "%" else "no data" end'), $(echo "$range_nodes" | jq -r '[.[].points] | add // 0') points"
    else
        local err
        err=$(echo "$raw_range" | jq -r '.error // .errorType // "unknown"' 2>/dev/null)
        range_json="{\"status\": \"error\", \"error\": $(debug_json_escape "${err}")}"
    fi
    probe+="\"query_range\": ${range_json},"

    # --- Fallback: platform recording rules, if OCP ships them ---
    local rule_json rule_summary="n/a" rule_name="" candidate raw_cnt cnt raw_rule rule_nodes rule_counts rule_active
    for candidate in node:node_cpu_utilisation:ratio_5m node:node_cpu_utilisation:ratio_1h; do
        raw_cnt=$(debug_query_raw "count(${candidate})" "spikes_rule_count_${candidate##*:}")
        cnt=$(echo "$raw_cnt" | jq -r '.data.result[0].value[1] // "0"' 2>/dev/null)
        if [[ "$cnt" =~ ^[0-9]+$ ]] && [[ "$cnt" -gt 0 ]]; then
            rule_name="$candidate"
            break
        fi
    done
    if [[ -n "$rule_name" ]]; then
        raw_rule=$(debug_query_raw "max by (node, instance) (max_over_time(${rule_name}[${window}])) * 100" "spikes_rule_max")
        rule_nodes=$(echo "$raw_rule" | jq -c --argjson thr "$DEBUG_BUCKET_THRESHOLD" \
            '[.data.result[] | {node: ((.metric.node // .metric.instance // "?") | sub(":[0-9]+$"; "")), max_util_pct: .value[1], would_flag_active: ((.value[1] | tonumber) > $thr)}]' 2>/dev/null || echo "[]")
        raw_rule=$(debug_query_raw "count by (node, instance) (count_over_time(${rule_name}[${window}]))" "spikes_rule_coverage")
        rule_counts=$(echo "$raw_rule" | jq -c '[.data.result[] | {node: ((.metric.node // .metric.instance // "?") | sub(":[0-9]+$"; "")), samples: .value[1]}]' 2>/dev/null || echo "[]")
        rule_active=$(echo "$rule_nodes" | jq '[.[] | select(.would_flag_active)] | length' 2>/dev/null || echo "0")
        rule_json="{\"available\": true, \"rule\": \"${rule_name}\", \"per_node\": ${rule_nodes}, \"sample_counts\": ${rule_counts}, \"verdict\": $( [[ "$rule_active" -gt 0 ]] && echo '"ACTIVE"' || echo '"IDLE"' )}"
        rule_summary="max $(echo "$rule_nodes" | jq -r 'if length > 0 then ([.[].max_util_pct | tonumber] | max | floor | tostring) + "%" else "no data" end') via ${rule_name}"
    else
        rule_json="{\"available\": false, \"candidates_checked\": [\"node:node_cpu_utilisation:ratio_5m\", \"node:node_cpu_utilisation:ratio_1h\"]}"
        rule_summary="no utilization recording rules found"
    fi
    probe+="\"recording_rules\": ${rule_json},"

    # Headline verdict: prefer query_range (it proves its coverage), then the
    # recording rule.
    local verdict="UNKNOWN" verdict_source="no method succeeded"
    if [[ "$(echo "$range_json" | jq -r '.status // empty' 2>/dev/null)" == "ok" ]]; then
        verdict=$(echo "$range_json" | jq -r '.verdict')
        verdict_source="query_range (min node coverage ${range_min_cov}%)"
    elif [[ -n "$rule_name" ]]; then
        verdict=$(echo "$rule_json" | jq -r '.verdict')
        verdict_source="recording_rule ${rule_name}"
    fi
    probe+="\"hypothetical_rule_verdict\": \"${verdict}\","
    probe+="\"verdict_source\": $(debug_json_escape "$verdict_source"),"

    if [[ "$VERBOSE" == "true" ]]; then
        echo "    query_range:    ${range_summary} [min node coverage ${range_min_cov}%]" >&2
        echo "    recording rule: ${rule_summary}" >&2
        echo "    hypothetical rule verdict: ${verdict} (from ${verdict_source})" >&2
    fi

    probe="${probe%,}}"
    echo "$probe"
}

# Raw material behind the api_server and operators criteria, so fleet runs
# can show what those thresholds actually measure. The api_server criterion
# sees a single number (total req/sec over the window); the validation
# question is its composition — platform read churn (GET/LIST/WATCH from
# controllers) vs mutating traffic (CREATE/UPDATE/PATCH/DELETE). The
# operators criterion also sees a single number (oldest matching pod age);
# the question is which pods and how much event activity sit behind it.
# Mirrors get_operator_age()/check_operator_events() (same pod regex, same
# event grep) but exports every matching pod instead of head -5, and exports
# the event count the criterion compares against 5.
debug_probe_criteria() {
    local probe='{'

    # --- api_server: request rate by verb over the criterion's own window ---
    local window="5m"
    [[ $TIME_WINDOW_MINUTES -gt 0 ]] && window="${TIME_WINDOW_MINUTES}m"
    local verb_raw by_verb read_rate write_rate
    verb_raw=$(debug_query_raw "sum by (verb) (rate(apiserver_request_total[${window}]))" "criteria_api_by_verb")
    by_verb=$(echo "$verb_raw" | jq -c '[.data.result[] | {verb: (.metric.verb // "?"), req_per_sec: .value[1]}] | sort_by(-(.req_per_sec | tonumber))' 2>/dev/null || echo "[]")
    read_rate=$(echo "$by_verb" | jq -r '[.[] | select((.verb // "") | ascii_downcase | test("^(get|list|watch)$")) | .req_per_sec | tonumber] | add // 0' 2>/dev/null)
    write_rate=$(echo "$by_verb" | jq -r '[.[] | select((.verb // "") | ascii_downcase | test("^(create|update|patch|delete|deletecollection|post|put)$")) | .req_per_sec | tonumber] | add // 0' 2>/dev/null)
    read_rate=${read_rate:-0}
    write_rate=${write_rate:-0}
    probe+="\"api_server\": {\"window\": \"${window}\", \"by_verb\": ${by_verb:-[]}, \"read_req_per_sec\": \"${read_rate}\", \"write_req_per_sec\": \"${write_rate}\"},"

    # --- operators: per-namespace pod inventory and event activity ---
    # Same namespace-existence skip as the criterion: missing namespaces are
    # what make the criterion N/A on non-ODH clusters.
    local ns_entries="" ns_count=0
    local ns
    IFS=',' read -ra CRIT_NAMESPACES <<< "$OPERATOR_NAMESPACES"
    for ns in "${CRIT_NAMESPACES[@]}"; do
        ns=$(echo "$ns" | xargs)
        timeout 5 oc get namespace "$ns" &>/dev/null || continue

        local pod_lines pods_json ev_tail ev_total ev_match
        pod_lines=$(timeout 10 oc get pods -n "$ns" --no-headers 2>/dev/null | grep -E "controller-manager|operator|dashboard" | head -20)
        pods_json=$(echo "$pod_lines" | jq -Rsc 'split("\n") | map(select(length > 0)) | map([splits("\\s+")] | {pod: .[0], ready: .[1], status: .[2], restarts: .[3], age: .[4]})' 2>/dev/null || echo "[]")

        ev_tail=$(timeout 10 oc get events -n "$ns" --sort-by='.lastTimestamp' 2>/dev/null | tail -20)
        ev_total=$(printf '%s\n' "$ev_tail" | grep -c '.' 2>/dev/null)
        ev_match=$(printf '%s\n' "$ev_tail" | grep -Eic "reconcil|created|updated|scaled" 2>/dev/null)
        ev_total=${ev_total:-0}
        ev_match=${ev_match:-0}

        [[ -n "$ns_entries" ]] && ns_entries+=","
        ns_entries+="{\"namespace\": \"${ns}\", \"pods\": ${pods_json:-[]}, \"events_in_tail\": ${ev_total}, \"matching_events\": ${ev_match}}"
        ((ns_count++))
    done

    local pod_count=0
    [[ -n "$ns_entries" ]] && pod_count=$(echo "[$ns_entries]" | jq '[.[].pods[]] | length' 2>/dev/null)
    probe+="\"operators\": {\"age_threshold_days\": ${OPERATOR_IDLE_AGE_DAYS}, \"event_threshold\": 5, \"namespaces\": [${ns_entries}]}"

    if [[ "$VERBOSE" == "true" ]]; then
        echo "    api verbs:      read ${read_rate}/s, write ${write_rate}/s (window ${window})" >&2
        echo "    operators:      ${ns_count} namespace(s), ${pod_count} matching pod(s)" >&2
    fi

    echo "${probe}}"
}

# Assemble the full debug section as a JSON object string.
debug_collect_all() {
    local probe='{'
    probe+="\"dcgm\": $(debug_probe_dcgm),"
    probe+="\"per_node\": $(debug_probe_per_node),"
    probe+="\"spikes\": $(debug_probe_spikes),"
    probe+="\"criteria_detail\": $(debug_probe_criteria),"

    # Query timings from QUERY_TIMINGS_LOG
    local timings_json
    timings_json=$(awk -F'|' '{printf "%s{\"label\": \"%s\", \"result\": \"%s\", \"duration\": \"%s\"}", (NR>1 ? "," : ""), $1, $2, $3}' "$QUERY_TIMINGS_LOG" 2>/dev/null)
    probe+="\"query_timings\": [${timings_json}],"

    # N/A census: which criteria reported UNKNOWN
    probe+="\"na_census\": {\"cpu\": \"${cpu_result:-unset}\", \"memory\": \"${mem_result:-unset}\", \"api_server\": \"${api_result:-unset}\", \"operators\": \"${operator_result:-unset}\"},"

    probe="${probe%,}}"
    echo "$probe"
}

# === MAIN SCRIPT ===

if [[ "$VERBOSE" == "true" ]]; then
    echo ""
    echo "========================================"
    echo "  OpenShift Cluster Idle Detection"
    echo "========================================"
    echo ""
fi

# Check prerequisites
check_oc_command
check_dependencies

get_prometheus_token

if [[ "$VERBOSE" == "true" ]]; then
    # Get cluster name
    CLUSTER_NAME=$(oc whoami --show-server 2>/dev/null || echo "Unknown")
    log_info "Cluster: $CLUSTER_NAME"
    log_info "Timestamp: $(date)"
    echo ""

    # Display configuration
    echo "Configuration:"
    echo "  CPU Idle Threshold: < ${CPU_IDLE_THRESHOLD}%"
    echo "  Memory Idle Threshold: < ${MEMORY_IDLE_THRESHOLD}%"
    echo "  API Server Threshold: < ${APISERVER_IDLE_THRESHOLD} req/sec"
    echo "  Operator Idle Age: >= ${OPERATOR_IDLE_AGE_DAYS} days"
    if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
        echo "  Time Window: Last ${TIME_WINDOW_MINUTES} minutes"
    else
        echo "  Time Window: Instant metrics only"
    fi
    echo "  Event History: Last ${EVENT_TIME_MINUTES} minutes"
    echo ""
fi

# Initialize idle criteria counters
idle_criteria_met=0
total_criteria=0

# === CHECK 1: Node CPU Usage ===
((total_criteria++))
cpu_result="UNKNOWN"

if [[ "$VERBOSE" == "true" ]]; then
    echo "--- Node CPU Usage ---"
fi

# Get instant CPU usage
instant_cpu=$(get_node_cpu_usage)

# Get time-windowed CPU usage if time window is enabled
cpu_windowed="N/A"
if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
    cpu_windowed=$(get_node_cpu_usage_windowed)
fi

if [[ "$instant_cpu" == "N/A" ]] && [[ "$cpu_windowed" == "N/A" ]]; then
    if [[ "$VERBOSE" == "true" ]]; then
        log_warning "Cannot retrieve CPU metrics. Metrics server may not be available."
    fi
    cpu_result="UNKNOWN"
else
    if [[ "$VERBOSE" == "true" ]]; then
        # Display instant metrics
        if [[ "$instant_cpu" != "N/A" ]]; then
            echo "Current CPU Usage: ${instant_cpu}%"
            timeout 10 oc adm top nodes 2>/dev/null | head -n 10 || log_warning "Could not display node details"
        fi

        # Display windowed metrics
        if [[ "$cpu_windowed" != "N/A" ]]; then
            echo "Average CPU (last ${TIME_WINDOW_MINUTES} min): ${cpu_windowed}%"
        fi
    fi

    # Determine idle status based on both instant and windowed metrics
    cpu_to_check="$instant_cpu"
    instant_idle=false
    windowed_idle=false

    # Check instant CPU
    if [[ "$instant_cpu" != "N/A" ]]; then
        if awk -v cpu="$instant_cpu" -v threshold="$CPU_IDLE_THRESHOLD" 'BEGIN {exit !(cpu < threshold)}'; then
            instant_idle=true
        fi
    fi

    # Check windowed CPU
    if [[ "$cpu_windowed" != "N/A" ]]; then
        cpu_to_check="$cpu_windowed"
        if awk -v cpu="$cpu_windowed" -v threshold="$CPU_IDLE_THRESHOLD" 'BEGIN {exit !(cpu < threshold)}'; then
            windowed_idle=true
        fi
    fi

    # Decision logic:
    # - If we have windowed data, use it for primary decision
    # - But if windowed is IDLE and current is ACTIVE, flag as currently active
    if [[ "$cpu_windowed" != "N/A" ]]; then
        if [[ "$windowed_idle" == "true" ]]; then
            if [[ "$instant_idle" == "false" && "$instant_cpu" != "N/A" ]]; then
                # Average is idle but currently active
                if [[ "$VERBOSE" == "true" ]]; then
                    log_warning "CPU average is IDLE (${cpu_windowed}%) but currently ACTIVE (${instant_cpu}%)"
                fi
                cpu_result="ACTIVE"
            else
                # Both average and current are idle
                if [[ "$VERBOSE" == "true" ]]; then
                    log_info "CPU usage is IDLE (avg: ${cpu_windowed}%, current: ${instant_cpu}%)"
                fi
                cpu_result="IDLE"
                ((idle_criteria_met++))
            fi
        else
            # Windowed average is active
            if [[ "$VERBOSE" == "true" ]]; then
                log_warning "CPU usage is ACTIVE (avg: ${cpu_windowed}%)"
            fi
            cpu_result="ACTIVE"
        fi
    else
        # No windowed data, use instant only
        if [[ "$instant_idle" == "true" ]]; then
            if [[ "$VERBOSE" == "true" ]]; then
                log_info "CPU usage is IDLE (${instant_cpu}%)"
            fi
            cpu_result="IDLE"
            ((idle_criteria_met++))
        else
            if [[ "$VERBOSE" == "true" ]]; then
                log_warning "CPU usage is ACTIVE (${instant_cpu}%)"
            fi
            cpu_result="ACTIVE"
        fi
    fi
fi

if [[ "$VERBOSE" == "false" ]]; then
    echo "CPU: $cpu_result"
fi

if [[ "$VERBOSE" == "true" ]]; then
    echo ""
fi

# === CHECK 2: Node Memory Usage ===
((total_criteria++))
mem_result="UNKNOWN"

if [[ "$VERBOSE" == "true" ]]; then
    echo "--- Node Memory Usage ---"
fi

# Get instant memory usage
instant_mem=$(get_node_memory_usage)

# Get time-windowed memory usage if time window is enabled
mem_windowed="N/A"
if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
    mem_windowed=$(get_node_memory_usage_windowed)
fi

if [[ "$instant_mem" == "N/A" ]] && [[ "$mem_windowed" == "N/A" ]]; then
    if [[ "$VERBOSE" == "true" ]]; then
        log_warning "Cannot retrieve memory metrics."
    fi
    mem_result="UNKNOWN"
else
    if [[ "$VERBOSE" == "true" ]]; then
        # Display instant metrics
        if [[ "$instant_mem" != "N/A" ]]; then
            echo "Current Memory Usage: ${instant_mem}%"
        fi

        # Display windowed metrics
        if [[ "$mem_windowed" != "N/A" ]]; then
            echo "Average Memory (last ${TIME_WINDOW_MINUTES} min): ${mem_windowed}%"
        fi
    fi

    # Determine idle status based on both instant and windowed metrics
    mem_to_check="$instant_mem"
    instant_idle=false
    windowed_idle=false

    # Check instant memory
    if [[ "$instant_mem" != "N/A" ]]; then
        if awk -v mem="$instant_mem" -v threshold="$MEMORY_IDLE_THRESHOLD" 'BEGIN {exit !(mem < threshold)}'; then
            instant_idle=true
        fi
    fi

    # Check windowed memory
    if [[ "$mem_windowed" != "N/A" ]]; then
        mem_to_check="$mem_windowed"
        if awk -v mem="$mem_windowed" -v threshold="$MEMORY_IDLE_THRESHOLD" 'BEGIN {exit !(mem < threshold)}'; then
            windowed_idle=true
        fi
    fi

    # Decision logic:
    # - If we have windowed data, use it for primary decision
    # - But if windowed is IDLE and current is ACTIVE, flag as currently active
    if [[ "$mem_windowed" != "N/A" ]]; then
        if [[ "$windowed_idle" == "true" ]]; then
            if [[ "$instant_idle" == "false" && "$instant_mem" != "N/A" ]]; then
                # Average is idle but currently active
                if [[ "$VERBOSE" == "true" ]]; then
                    log_warning "Memory average is IDLE (${mem_windowed}%) but currently ACTIVE (${instant_mem}%)"
                fi
                mem_result="ACTIVE"
            else
                # Both average and current are idle
                if [[ "$VERBOSE" == "true" ]]; then
                    log_info "Memory usage is IDLE (avg: ${mem_windowed}%, current: ${instant_mem}%)"
                fi
                mem_result="IDLE"
                ((idle_criteria_met++))
            fi
        else
            # Windowed average is active
            if [[ "$VERBOSE" == "true" ]]; then
                log_warning "Memory usage is ACTIVE (avg: ${mem_windowed}%)"
            fi
            mem_result="ACTIVE"
        fi
    else
        # No windowed data, use instant only
        if [[ "$instant_idle" == "true" ]]; then
            if [[ "$VERBOSE" == "true" ]]; then
                log_info "Memory usage is IDLE (${instant_mem}%)"
            fi
            mem_result="IDLE"
            ((idle_criteria_met++))
        else
            if [[ "$VERBOSE" == "true" ]]; then
                log_warning "Memory usage is ACTIVE (${instant_mem}%)"
            fi
            mem_result="ACTIVE"
        fi
    fi
fi

if [[ "$VERBOSE" == "false" ]]; then
    echo "Memory: $mem_result"
fi

if [[ "$VERBOSE" == "true" ]]; then
    echo ""
fi

# === CHECK 3: API Server Request Rate ===
((total_criteria++))
api_result="UNKNOWN"

if [[ "$VERBOSE" == "true" ]]; then
    echo "--- API Server Request Rate ---"
fi

api_rate=$(get_apiserver_request_rate)

if [[ "$api_rate" == "N/A" ]]; then
    if [[ "$VERBOSE" == "true" ]]; then
        log_warning "Cannot retrieve API server metrics from Prometheus"
    fi
    api_result="UNKNOWN"
    # Don't count this criteria if we can't get metrics
    ((total_criteria--))
else
    if [[ "$VERBOSE" == "true" ]]; then
        echo "API Server Request Rate: ${api_rate} req/sec"

        # Show breakdown by verb if available
        if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
            echo "Request breakdown (last ${TIME_WINDOW_MINUTES} min):"
        else
            echo "Request breakdown (last 5 min):"
        fi
        get_apiserver_request_rate_breakdown 2>/dev/null || echo "  Breakdown not available"
        echo ""
    fi

    # Compare against threshold
    if awk -v rate="$api_rate" -v threshold="$APISERVER_IDLE_THRESHOLD" 'BEGIN {exit !(rate < threshold)}'; then
        if [[ "$VERBOSE" == "true" ]]; then
            log_info "API server request rate is IDLE (< ${APISERVER_IDLE_THRESHOLD} req/sec)"
        fi
        api_result="IDLE"
        ((idle_criteria_met++))
    else
        if [[ "$VERBOSE" == "true" ]]; then
            log_warning "API server request rate is ACTIVE (>= ${APISERVER_IDLE_THRESHOLD} req/sec)"
        fi
        api_result="ACTIVE"
    fi
fi

if [[ "$VERBOSE" == "false" ]] && [[ "$api_result" != "UNKNOWN" ]]; then
    echo "API Server: $api_result"
fi

if [[ "$VERBOSE" == "true" ]]; then
    echo ""
fi

# === CHECK 4: GPU/ML Node Detection (informational only) ===
# Get all GPU node data once for both display and export
gpu_node_data=$(get_all_gpu_node_data)

if [[ "$CHECK_ML_NODES" == "true" ]] && [[ "$VERBOSE" == "true" ]]; then
    echo "--- GPU/ML Node Detection ---"

    # Display GPU detection results
    if [[ "$gpu_node_data" != "N/A" ]]; then
        # Count nodes and build flavor summary
        gpu_node_count=$(echo "$gpu_node_data" | wc -l)
        gpu_flavors=$(echo "$gpu_node_data" | awk -F'|' '{print $2"("$4")"}' | sort -u | tr '\n' ',' | sed 's/,$//')

        echo "GPU Nodes Found: $gpu_node_count ($gpu_flavors)"
        echo ""
        echo "GPU Node Resource Usage:"

        # Display per-node usage
        while IFS='|' read -r node_name gpu_vendor gpu_count instance_type; do
            # Trim leading/trailing whitespace from variables
            node_name=$(echo "$node_name" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            gpu_vendor=$(echo "$gpu_vendor" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            gpu_count=$(echo "$gpu_count" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            instance_type=$(echo "$instance_type" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

            if [[ -n "$node_name" ]]; then
                # Get current usage for this node
                node_stats=$(timeout 10 oc adm top node "$node_name" --no-headers 2>/dev/null || echo "")
                if [[ -n "$node_stats" ]]; then
                    node_cpu=$(echo "$node_stats" | awk '{gsub(/%/,"",$3); print $3}')
                    node_mem=$(echo "$node_stats" | awk '{gsub(/%/,"",$5); print $5}')

                    echo "  $node_name ($gpu_vendor x${gpu_count}, $instance_type):"
                    echo "    Current: CPU ${node_cpu}%, Memory ${node_mem}%"

                    # Get time-windowed usage for this specific node if enabled
                    if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
                        # Temporarily disable verbose logging for per-node queries
                        saved_verbose="$VERBOSE"
                        VERBOSE=false

                        # CPU windowed for this node
                        window="${TIME_WINDOW_MINUTES}m"
                        cpu_query="(1 - avg(rate(node_cpu_seconds_total{mode=\"idle\",instance=~\"${node_name}.*\"}[${window}]))) * 100"
                        cpu_windowed=$(query_prometheus "$cpu_query")

                        # Memory windowed for this node
                        mem_query="(1 - avg_over_time((avg(node_memory_MemAvailable_bytes{instance=~\"${node_name}.*\"}) / avg(node_memory_MemTotal_bytes{instance=~\"${node_name}.*\"}))[${window}:])) * 100"
                        mem_windowed=$(query_prometheus "$mem_query")

                        # TODO: get GPU memory and GPU usage using vendor-specific queries
                        # Restore verbose setting
                        VERBOSE="$saved_verbose"

                        if [[ "$cpu_windowed" != "N/A" ]] && [[ "$mem_windowed" != "N/A" ]]; then
                            cpu_windowed=$(awk -v val="$cpu_windowed" 'BEGIN {printf "%.2f", val}')
                            mem_windowed=$(awk -v val="$mem_windowed" 'BEGIN {printf "%.2f", val}')
                            echo "    Average (last ${TIME_WINDOW_MINUTES} min): CPU ${cpu_windowed}%, Memory ${mem_windowed}%"
                        fi
                    fi
                else
                    echo "  $node_name ($gpu_vendor x${gpu_count}, $instance_type): Unable to get metrics"
                fi
            fi
        done <<< "$gpu_node_data"

    else
        # Check for GPU machines if no GPU nodes found
        gpu_machine_info=$(get_gpu_machines)
        gpu_machines=$(echo "$gpu_machine_info" | cut -d':' -f1)
        gpu_machine_count=$(echo "$gpu_machine_info" | cut -d':' -f2)

        if [[ "$gpu_machines" != "N/A" ]]; then
            echo "GPU Machines Found (by pattern): $gpu_machine_count"
            while IFS= read -r machine; do
                if [[ -n "$machine" ]]; then
                    echo "  $machine"
                fi
            done <<< "$gpu_machines"
            log_info "GPU machines exist but nodes may not be ready/running"
        else
            log_info "No GPU nodes or machines found"
        fi
    fi

    echo ""

    # Check ML nodes by instance type pattern
    echo "ML Node Check (by instance type):"
    ml_usage=$(get_ml_node_usage)

    # Get windowed ML node usage if time window is enabled
    ml_usage_windowed="N/A"
    if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
        ml_usage_windowed=$(get_ml_node_usage_windowed)
    fi

    if [[ "$ml_usage" == "N/A" ]] && [[ "$ml_usage_windowed" == "N/A" ]]; then
        log_info "No ML nodes found (patterns: $ML_NODE_PATTERN)"
    else
        # Display instant metrics
        if [[ "$ml_usage" != "N/A" ]]; then
            echo "Current ML Node Average: $ml_usage"
        fi

        # Display windowed metrics
        if [[ "$ml_usage_windowed" != "N/A" ]]; then
            echo "ML Node Average (last ${TIME_WINDOW_MINUTES} min): $ml_usage_windowed"
        fi

        # Determine which metric to use for comparison
        ml_cpu_to_check=""
        if [[ "$ml_usage_windowed" != "N/A" ]]; then
            ml_cpu_to_check=$(echo "$ml_usage_windowed" | sed 's/%CPU//')
        elif [[ "$ml_usage" != "N/A" ]]; then
            ml_cpu_to_check=$(echo "$ml_usage" | cut -d',' -f1 | sed 's/%CPU//')
        fi

        # Check if idle
        if [[ -n "$ml_cpu_to_check" ]]; then
            if awk -v cpu="$ml_cpu_to_check" -v threshold="$CPU_IDLE_THRESHOLD" 'BEGIN {exit !(cpu < threshold)}'; then
                log_info "ML nodes are IDLE"
            else
                log_warning "ML nodes are ACTIVE (expensive resources in use!)"
            fi
        fi
    fi
    echo ""
fi

# === CHECK 5: Operator Age (RHODS/OpenDataHub) ===
((total_criteria++))
operator_result="UNKNOWN"

if [[ "$VERBOSE" == "true" ]]; then
    echo "--- Operator Age & Activity ---"
fi

operator_info=$(get_operator_age)
operator_name=$(echo "$operator_info" | cut -d':' -f1)
operator_age_days=$(echo "$operator_info" | cut -d':' -f2 | sed 's/d//')

if [[ "$VERBOSE" == "true" ]]; then
    echo "Detected Operators: $operator_name"
fi

if [[ "$operator_name" == "N/A" ]]; then
    if [[ "$VERBOSE" == "true" ]]; then
        log_info "No operators found in configured namespaces: $OPERATOR_NAMESPACES"
        # If no operators, check general pod info
        pod_data=$(get_pod_counts)
        IFS=':' read -r total_pods running_pods <<< "$pod_data"
        echo "Total Pods: $total_pods (Running: $running_pods)"
        log_info "Skipping operator age check (criteria not counted)"
    fi
    ((total_criteria--))
    operator_result="N/A"
else
    if [[ "$VERBOSE" == "true" ]]; then
        echo "Oldest Operator Pod Age: ${operator_age_days} days"
    fi

    # Check for recent operator events
    operator_events=$(check_operator_events)

    if [[ "$VERBOSE" == "true" ]]; then
        echo "Recent Operator Events (last 20): $operator_events"

        # Display some operator pods from configured namespaces
        echo ""
        echo "Sample Operator Pods:"

        IFS=',' read -ra NAMESPACES <<< "$OPERATOR_NAMESPACES"
        for ns in "${NAMESPACES[@]}"; do
            ns=$(echo "$ns" | xargs)
            if timeout 5 oc get namespace "$ns" &>/dev/null; then
                timeout 10 oc get pods -n "$ns" --no-headers 2>/dev/null | \
                    grep -E "controller-manager|operator|dashboard" | \
                    head -3 | \
                    awk -v ns_name="$ns" '{printf "  [%s] %-50s  Age: %s\n", ns_name, $1, $5}'
            fi
        done
        echo ""
    fi

    # Determine if operators indicate idle state
    if [[ $operator_age_days -ge $OPERATOR_IDLE_AGE_DAYS ]] && [[ $operator_events -lt 5 ]]; then
        if [[ "$VERBOSE" == "true" ]]; then
            log_info "Operators are IDLE (age: ${operator_age_days}d, low activity)"
        fi
        operator_result="IDLE"
        ((idle_criteria_met++))
    else
        if [[ "$VERBOSE" == "true" ]]; then
            log_warning "Operators are ACTIVE (age: ${operator_age_days}d, events: $operator_events)"
        fi
        operator_result="ACTIVE"
    fi
fi

if [[ "$VERBOSE" == "false" ]]; then
    echo "Operators: $operator_result"
fi

if [[ "$VERBOSE" == "true" ]]; then
    echo ""
fi

# === INFORMATIONAL: Recent Pod Activity (not counted in criteria) ===
if [[ "$VERBOSE" == "true" ]]; then
    echo "--- Recent Activity (last ${EVENT_TIME_MINUTES} minutes) ---"
    recent_events=$(get_recent_pod_activity)

    echo "Pod-related events: $recent_events"
    timeout 10 oc get events -A --sort-by='.lastTimestamp' 2>/dev/null | tail -5 || echo "Could not retrieve events"

    if [[ $recent_events -eq 0 ]]; then
        log_info "No recent pod activity"
    else
        log_warning "Recent pod activity detected - it does not imply the cluster is being actively used"
    fi
    echo ""
fi

# === COLLECT ALL RESULTS FOR EXPORT ===
# Store all results in variables for export
CLUSTER_NAME=$(oc whoami --show-server 2>/dev/null || echo "Unknown")
TIMESTAMP=$(date -Iseconds)
TIMESTAMP_HUMAN=$(date)

# Collect GPU information for export (reuse cached data from CHECK 4)
# gpu_node_data was already collected above

# Determine if cluster has GPU nodes
if [[ "$gpu_node_data" != "N/A" ]]; then
    HAS_GPU_NODES="true"
    GPU_NODE_COUNT=$(echo "$gpu_node_data" | wc -l)
    GPU_FLAVORS=$(echo "$gpu_node_data" | awk -F'|' '{print $2"("$4")"}' | sort -u | tr '\n' ',' | sed 's/,$//')

    # Get average GPU usage across all GPU nodes (for backward compatibility with CSV/JSON export)
    gpu_cpu_sum=0
    gpu_mem_sum=0
    gpu_count_nodes=0

    while IFS='|' read -r node_name gpu_vendor gpu_count instance_type; do
        node_name=$(echo "$node_name" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        if [[ -n "$node_name" ]]; then
            node_stats=$(timeout 10 oc adm top node "$node_name" --no-headers 2>/dev/null || echo "")
            if [[ -n "$node_stats" ]]; then
                node_cpu=$(echo "$node_stats" | awk '{gsub(/%/,"",$3); print $3}')
                node_mem=$(echo "$node_stats" | awk '{gsub(/%/,"",$5); print $5}')
                gpu_cpu_sum=$(awk -v sum="$gpu_cpu_sum" -v val="$node_cpu" 'BEGIN {print sum + val}')
                gpu_mem_sum=$(awk -v sum="$gpu_mem_sum" -v val="$node_mem" 'BEGIN {print sum + val}')
                ((gpu_count_nodes++))
            fi
        fi
    done <<< "$gpu_node_data"

    if [[ $gpu_count_nodes -gt 0 ]]; then
        GPU_CPU_CURRENT=$(awk -v sum="$gpu_cpu_sum" -v cnt="$gpu_count_nodes" 'BEGIN {printf "%.2f", sum / cnt}')
        GPU_MEM_CURRENT=$(awk -v sum="$gpu_mem_sum" -v cnt="$gpu_count_nodes" 'BEGIN {printf "%.2f", sum / cnt}')
    else
        GPU_CPU_CURRENT="N/A"
        GPU_MEM_CURRENT="N/A"
    fi

    # Get windowed GPU usage if time window is enabled (average across all GPU nodes)
    if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
        gpu_cpu_windowed_sum=0
        gpu_mem_windowed_sum=0
        gpu_windowed_count=0

        # Temporarily disable verbose for export queries
        saved_verbose_export="$VERBOSE"
        VERBOSE=false

        while IFS='|' read -r node_name gpu_vendor gpu_count instance_type; do
            node_name=$(echo "$node_name" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            if [[ -n "$node_name" ]]; then
                window="${TIME_WINDOW_MINUTES}m"
                cpu_query="(1 - avg(rate(node_cpu_seconds_total{mode=\"idle\",instance=~\"${node_name}.*\"}[${window}]))) * 100"
                cpu_windowed=$(query_prometheus "$cpu_query")

                mem_query="(1 - avg_over_time((avg(node_memory_MemAvailable_bytes{instance=~\"${node_name}.*\"}) / avg(node_memory_MemTotal_bytes{instance=~\"${node_name}.*\"}))[${window}:])) * 100"
                mem_windowed=$(query_prometheus "$mem_query")

                if [[ "$cpu_windowed" != "N/A" ]] && [[ "$mem_windowed" != "N/A" ]]; then
                    gpu_cpu_windowed_sum=$(awk -v sum="$gpu_cpu_windowed_sum" -v val="$cpu_windowed" 'BEGIN {print sum + val}')
                    gpu_mem_windowed_sum=$(awk -v sum="$gpu_mem_windowed_sum" -v val="$mem_windowed" 'BEGIN {print sum + val}')
                    ((gpu_windowed_count++))
                fi
            fi
        done <<< "$gpu_node_data"

        # Restore verbose setting
        VERBOSE="$saved_verbose_export"

        if [[ $gpu_windowed_count -gt 0 ]]; then
            GPU_CPU_WINDOWED=$(awk -v sum="$gpu_cpu_windowed_sum" -v cnt="$gpu_windowed_count" 'BEGIN {printf "%.2f", sum / cnt}')
            GPU_MEM_WINDOWED=$(awk -v sum="$gpu_mem_windowed_sum" -v cnt="$gpu_windowed_count" 'BEGIN {printf "%.2f", sum / cnt}')
        else
            GPU_CPU_WINDOWED="N/A"
            GPU_MEM_WINDOWED="N/A"
        fi
    else
        GPU_CPU_WINDOWED="N/A"
        GPU_MEM_WINDOWED="N/A"
    fi

    GPU_NODE_AGE="N/A"  # We don't track this anymore
else
    HAS_GPU_NODES="false"
    GPU_NODE_COUNT=0
    GPU_FLAVORS="N/A"
    GPU_NODE_AGE="N/A"
    GPU_CPU_CURRENT="N/A"
    GPU_MEM_CURRENT="N/A"
    GPU_CPU_WINDOWED="N/A"
    GPU_MEM_WINDOWED="N/A"
fi

# === FINAL DETERMINATION ===
# Determine if cluster is idle (need at least 75% criteria met)
idle_threshold=$(awk -v total="$total_criteria" 'BEGIN {print int(total * 0.80)}')

if [[ $idle_criteria_met -ge $idle_threshold ]]; then
    FINAL_STATUS="IDLE"
    EXIT_CODE=1  # Exit 1 for IDLE (warning - wasting resources)
else
    FINAL_STATUS="ACTIVE"
    EXIT_CODE=0  # Exit 0 for ACTIVE (success - resources being used)
fi

# === DEBUG PROBE COLLECTION ===
# Runs after the verdict so per-node/bucket data can be compared against it.
# Never modifies FINAL_STATUS or EXIT_CODE.
DEBUG_JSON=""
if [[ "$DEBUG_PROBE" == "true" ]]; then
    DEBUG_JSON=$(debug_collect_all)
fi

if [[ "$VERBOSE" == "true" ]]; then
    echo "========================================"
    echo "         IDLE DETECTION SUMMARY"
    echo "========================================"
    echo ""
    echo "Idle Criteria Met: $idle_criteria_met / $total_criteria"
    echo ""

    if [[ "$FINAL_STATUS" == "IDLE" ]]; then
        echo -e "${RED}╔═══════════════════════════════╗${NC}"
        echo -e "${RED}║   CLUSTER STATUS: IDLE ✗      ║${NC}"
        echo -e "${RED}╚═══════════════════════════════╝${NC}"
        echo ""
        log_warning "Cluster is considered IDLE - resources may be wasted!"

        if [[ "$ml_usage" != "N/A" ]] || [[ "$HAS_GPU_NODES" == "true" ]]; then
            echo ""
            log_warning "Expensive GPU/ML nodes are idle - incurring unnecessary costs!"
        fi
    else
        echo -e "${GREEN}╔═══════════════════════════════╗${NC}"
        echo -e "${GREEN}║   CLUSTER STATUS: ACTIVE ✓    ║${NC}"
        echo -e "${GREEN}╚═══════════════════════════════╝${NC}"
        echo ""
        log_success "Cluster is considered ACTIVE - resources are being utilized"
    fi
else
    # Quiet mode - just print final status
    echo "STATUS: $FINAL_STATUS"
fi

# === EXPORT RESULTS ===
export_csv() {
    local csv_file="$1"

    # Prepare values (remove N/A and handle empty values)
    local cpu_val="${cpu_to_check:-N/A}"
    local mem_val="${mem_to_check:-N/A}"
    local api_val="${api_rate:-N/A}"
    local operator_age="${operator_age_days:-N/A}"

    # Write CSV header if file doesn't exist
    if [[ ! -f "$csv_file" ]]; then
        echo "timestamp,cluster,status,cpu_result,cpu_value,memory_result,memory_value,api_server_result,api_server_value,operators_result,operator_age_days,criteria_met,total_criteria,time_window_minutes,has_gpu_nodes,gpu_node_count,gpu_flavors,gpu_node_age,gpu_cpu_current,gpu_mem_current,gpu_cpu_windowed,gpu_mem_windowed" > "$csv_file"
    fi

    # Append data
    echo "${TIMESTAMP},${CLUSTER_NAME},${FINAL_STATUS},${cpu_result},${cpu_val},${mem_result},${mem_val},${api_result},${api_val},${operator_result},${operator_age},${idle_criteria_met},${total_criteria},${TIME_WINDOW_MINUTES},${HAS_GPU_NODES},${GPU_NODE_COUNT},${GPU_FLAVORS},${GPU_NODE_AGE},${GPU_CPU_CURRENT},${GPU_MEM_CURRENT},${GPU_CPU_WINDOWED},${GPU_MEM_WINDOWED}" >> "$csv_file"
}

export_json() {
    local json_file="$1"

    # Prepare values
    local cpu_val="${cpu_to_check:-null}"
    local mem_val="${mem_to_check:-null}"
    local api_val="${api_rate:-null}"
    local operator_age="${operator_age_days:-null}"

    # Quote string values, leave null as-is
    [[ "$cpu_val" != "null" ]] && cpu_val="\"$cpu_val\""
    [[ "$mem_val" != "null" ]] && mem_val="\"$mem_val\""
    [[ "$api_val" != "null" ]] && api_val="\"$api_val\""
    [[ "$operator_age" != "null" ]] && operator_age="$operator_age"

    # Prepare GPU values
    local gpu_cpu_current_val="${GPU_CPU_CURRENT}"
    local gpu_mem_current_val="${GPU_MEM_CURRENT}"
    local gpu_cpu_windowed_val="${GPU_CPU_WINDOWED}"
    local gpu_mem_windowed_val="${GPU_MEM_WINDOWED}"
    local gpu_flavors_val="${GPU_FLAVORS}"
    local gpu_age_val="${GPU_NODE_AGE}"

    # Convert N/A to null
    [[ "$gpu_cpu_current_val" == "N/A" ]] && gpu_cpu_current_val="null" || gpu_cpu_current_val="\"$gpu_cpu_current_val\""
    [[ "$gpu_mem_current_val" == "N/A" ]] && gpu_mem_current_val="null" || gpu_mem_current_val="\"$gpu_mem_current_val\""
    [[ "$gpu_cpu_windowed_val" == "N/A" ]] && gpu_cpu_windowed_val="null" || gpu_cpu_windowed_val="\"$gpu_cpu_windowed_val\""
    [[ "$gpu_mem_windowed_val" == "N/A" ]] && gpu_mem_windowed_val="null" || gpu_mem_windowed_val="\"$gpu_mem_windowed_val\""
    [[ "$gpu_flavors_val" == "N/A" ]] && gpu_flavors_val="null" || gpu_flavors_val="\"$gpu_flavors_val\""
    [[ "$gpu_age_val" == "N/A" ]] && gpu_age_val="null" || gpu_age_val="\"$gpu_age_val\""

    # Assemble the optional debug tail before the heredoc: literal quotes
    # inside a ${VAR:+...} expansion are stripped by quote removal, and
    # backslash-escapes in a heredoc body are never unescaped, so the only
    # clean way to emit a quoted key conditionally is a separate variable.
    local debug_tail=""
    [[ -n "$DEBUG_JSON" ]] && debug_tail=",
  \"debug\": ${DEBUG_JSON}"

    # Create JSON
    cat > "$json_file" << EOF
{
  "timestamp": "${TIMESTAMP}",
  "timestamp_human": "${TIMESTAMP_HUMAN}",
  "cluster": "${CLUSTER_NAME}",
  "status": "${FINAL_STATUS}",
  "exit_code": ${EXIT_CODE},
  "configuration": {
    "time_window_minutes": ${TIME_WINDOW_MINUTES},
    "cpu_threshold": ${CPU_IDLE_THRESHOLD},
    "memory_threshold": ${MEMORY_IDLE_THRESHOLD},
    "api_threshold": ${APISERVER_IDLE_THRESHOLD},
    "operator_age_threshold_days": ${OPERATOR_IDLE_AGE_DAYS}
  },
  "criteria": {
    "total": ${total_criteria},
    "met": ${idle_criteria_met},
    "threshold": ${idle_threshold},
    "cpu": {
      "result": "${cpu_result}",
      "value": ${cpu_val}
    },
    "memory": {
      "result": "${mem_result}",
      "value": ${mem_val}
    },
    "api_server": {
      "result": "${api_result}",
      "value": ${api_val}
    },
    "operators": {
      "result": "${operator_result}",
      "age_days": ${operator_age}
    }
  },
  "gpu": {
    "has_gpu_nodes": ${HAS_GPU_NODES},
    "node_count": ${GPU_NODE_COUNT},
    "flavors": ${gpu_flavors_val},
    "node_age": ${gpu_age_val},
    "usage": {
      "cpu_current": ${gpu_cpu_current_val},
      "memory_current": ${gpu_mem_current_val},
      "cpu_windowed": ${gpu_cpu_windowed_val},
      "memory_windowed": ${gpu_mem_windowed_val}
    }
  }${debug_tail}
}
EOF
}

# Export if requested
if [[ -n "$EXPORT_CSV" ]]; then
    export_csv "$EXPORT_CSV"
    if [[ "$VERBOSE" == "true" ]]; then
        log_info "Results exported to CSV: $EXPORT_CSV"
    fi
fi

if [[ -n "$EXPORT_JSON" ]]; then
    export_json "$EXPORT_JSON"
    if [[ "$VERBOSE" == "true" ]]; then
        log_info "Results exported to JSON: $EXPORT_JSON"
    fi
fi

exit $EXIT_CODE