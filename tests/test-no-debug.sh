#!/usr/bin/env bash
# Regression test for export_json's empty-debug path: with the debug probe
# off (DEBUG_JSON empty), the JSON export must stay valid and must not grow
# a "debug" key or a dangling comma where the debug tail would sit.
. "$(dirname "$0")/common.sh"

extract_export_json "$WORKDIR/export-json-func.sh"

# A complete main-run environment with the debug probe off. api_result is
# UNKNOWN with an empty value on purpose: that exercises the null path for
# criterion values alongside the quoted-string path.
VERBOSE=true
TIME_WINDOW_MINUTES=10080
CPU_IDLE_THRESHOLD=15
MEMORY_IDLE_THRESHOLD=35
APISERVER_IDLE_THRESHOLD=100
OPERATOR_IDLE_AGE_DAYS=7
TIMESTAMP="2026-10-08T00:00:00Z"
TIMESTAMP_HUMAN="test"
CLUSTER_NAME="test-cluster"
FINAL_STATUS="ACTIVE"
EXIT_CODE=0
total_criteria=4
idle_criteria_met=2
idle_threshold=3
cpu_result="ACTIVE"; cpu_to_check="23.4"
mem_result="IDLE"; mem_to_check="8.1"
api_result="UNKNOWN"; api_rate=""
operator_result="ACTIVE"; operator_age_days="12"
HAS_GPU_NODES=false
GPU_NODE_COUNT=0
GPU_CPU_CURRENT="N/A"; GPU_MEM_CURRENT="N/A"
GPU_CPU_WINDOWED="N/A"; GPU_MEM_WINDOWED="N/A"
GPU_FLAVORS="N/A"; GPU_NODE_AGE="N/A"
DEBUG_JSON=""

source "$WORKDIR/export-json-func.sh"

export_json "$WORKDIR/no-debug-output.json"

jq -e . "$WORKDIR/no-debug-output.json" > /dev/null || fail "invalid JSON without debug tail"
jq -e 'has("debug") | not' "$WORKDIR/no-debug-output.json" > /dev/null || fail "unexpected debug key"
echo "PASS: no-debug export is valid JSON with no debug section"
