#!/usr/bin/env bash
# Hermetic test for the debug-probe section of ocp-idle-check.sh.
#
# Extracts the debug functions from the script, runs them against fake
# oc/curl/timeout binaries (fixtures/bin), and asserts on the assembled
# debug JSON: DCGM samples, per-node breakdown, spike detection via
# query_range with recording-rule fallback, criteria detail (api verb mix,
# operator pod inventory), and the query-timings log. Then feeds the result
# through export_json to prove the debug section survives embedding.
#
# The fake curl decodes each PromQL query and answers it exactly, so a query
# whose shape changes in the script fails this test loudly instead of
# silently degrading on a real cluster.
. "$(dirname "$0")/common.sh"

export PATH="$FIXTURES_BIN:$PATH"

extract_debug_region "$WORKDIR/debug-funcs.sh"
extract_export_json "$WORKDIR/export-json-func.sh"

# Environment the debug functions expect (mirrors a Jenkins run: 7-day
# window, debug probe on, criteria already evaluated).
TIME_WINDOW_MINUTES=10080
DEBUG_BUCKET_THRESHOLD=20
DEBUG_PROBE=true
VERBOSE=true
PROMETHEUS_TOKEN=fake-token
OPERATOR_IDLE_AGE_DAYS=7
OPERATOR_NAMESPACES="opendatahub,redhat-ods-operator,redhat-ods-applications"
cpu_result="ACTIVE"; mem_result="IDLE"; api_result="ACTIVE"; operator_result="IDLE"

# query_prometheus lives outside the extracted region; stub it.
query_prometheus() { echo "21.535558887409834"; }

source "$WORKDIR/debug-funcs.sh"

JSON=$(debug_collect_all)

echo "=== raw debug JSON ==="
echo "$JSON" | jq . || fail "invalid JSON from debug_collect_all"

echo "=== assertions: debug section content ==="
echo "$JSON" | jq -e '
  .dcgm.dcgm_gpu_util_series == 1 and
  .dcgm.dcgm_gpu_util_samples[0].node == "10.129.0.42" and
  .dcgm.dcgm_fb_used_samples[0].node == "10.129.0.42" and
  .dcgm.dcgm_gpu_util_window.max[0].max_util_pct == "17" and
  .dcgm.dcgm_gpu_util_window.avg[0].avg_util_pct == "0.7" and
  .dcgm.dcgm_gpu_util_window.samples[0].samples == "20160" and
  .dcgm.dcgm_fb_used_window.max[0].max_fb_used_mib == "42572" and
  .dcgm.dcgm_fb_used_window.avg[0].avg_fb_used_mib == "41000" and
  .dcgm.dcgm_exporter_pods == 1 and
  .dcgm.dcgm_exporter_pod_names == ["nvidia-gpu-operator/dcgm-exporter-abc123"] and
  .dcgm.sanity_node_series_count == "384" and
  (.dcgm.gpu_namespace_labels | contains("nvidia-gpu-operator")) and
  .per_node.per_node_cpu_windowed[0].node == "ip-10-0-5-50.us-east-2.compute.internal" and
  .per_node.per_node_cpu_windowed[0].cpu_pct == "15.2" and
  .per_node.per_node_cpu_windowed[1].cpu_pct == "2.1" and
  .per_node.cluster_avg_cpu_windowed == "21.535558887409834" and
  .per_node.per_node_mem_windowed[0].mem_pct == "36.1" and
  (.per_node.per_node_instant | length) == 2 and
  .per_node.per_node_instant[0].cpu_pct == "15" and
  .spikes.bucket_threshold_pct == 20 and
  .spikes.query_range.status == "ok" and
  .spikes.query_range.step_seconds == 900 and
  .spikes.query_range.expected_points == 673 and
  .spikes.query_range.per_node[0].points == 6 and
  .spikes.query_range.per_node[0].coverage_pct == 0.89 and
  .spikes.query_range.per_node[0].max_15m_avg_cpu_pct == 41.7 and
  .spikes.query_range.per_node[0].max_at != null and
  .spikes.query_range.per_node[0].windows_above_threshold == 1 and
  .spikes.query_range.per_node[0].pct_windows_above_threshold == 16.66 and
  .spikes.query_range.per_node[0].would_flag_active == true and
  .spikes.query_range.per_node[1].would_flag_active == false and
  .spikes.query_range.verdict == "ACTIVE" and
  .spikes.recording_rules.available == true and
  .spikes.recording_rules.rule == "node:node_cpu_utilisation:ratio_5m" and
  .spikes.recording_rules.per_node[0].max_util_pct == "39" and
  .spikes.recording_rules.sample_counts[0].samples == "2016" and
  .spikes.recording_rules.verdict == "ACTIVE" and
  .spikes.hypothetical_rule_verdict == "ACTIVE" and
  (.spikes.verdict_source | contains("query_range")) and
  .criteria_detail.api_server.window == "10080m" and
  (.criteria_detail.api_server.by_verb | length) == 7 and
  .criteria_detail.api_server.by_verb[0].verb == "GET" and
  .criteria_detail.api_server.by_verb[0].req_per_sec == "230" and
  .criteria_detail.api_server.read_req_per_sec == "263" and
  .criteria_detail.api_server.write_req_per_sec == "38" and
  .criteria_detail.operators.age_threshold_days == 7 and
  .criteria_detail.operators.event_threshold == 5 and
  (.criteria_detail.operators.namespaces | length) == 3 and
  .criteria_detail.operators.namespaces[0].namespace == "opendatahub" and
  (.criteria_detail.operators.namespaces[0].pods | length) == 3 and
  .criteria_detail.operators.namespaces[0].pods[0].pod == "rhods-operator-controller-manager-abc" and
  .criteria_detail.operators.namespaces[0].pods[0].restarts == "3" and
  .criteria_detail.operators.namespaces[0].pods[0].age == "14d" and
  .criteria_detail.operators.namespaces[0].pods[1].age == "13d" and
  .criteria_detail.operators.namespaces[0].pods[2].pod == "odh-model-controller-manager-qqq" and
  .criteria_detail.operators.namespaces[0].pods[2].age == "3d" and
  .criteria_detail.operators.namespaces[0].events_in_tail == 20 and
  .criteria_detail.operators.namespaces[0].matching_events == 2 and
  .na_census.cpu == "ACTIVE" and
  .na_census.memory == "IDLE" and
  # 16 timings in the fixture scenario (5m recording rule present, so
  # the probe runs spikes_rule_max and spikes_rule_coverage). Clusters
  # without the rule run the ratio_1h fallback instead and log 15.
  (.query_timings | length) == 16 and
  ([.query_timings[].label] | index("spikes_query_range")) != null and
  ([.query_timings[].label] | index("spikes_rule_count_ratio_5m")) != null and
  ([.query_timings[].label] | index("dcgm_gpu_util_max_window")) != null and
  ([.query_timings[].label] | index("criteria_api_by_verb")) != null
' >/dev/null && echo "PASS: all assertions hold" || fail "assertion mismatch (see JSON above)"

# --- integration: the debug JSON must survive embedding by export_json ---
# export_json interpolates ${DEBUG_JSON} into its heredoc behind a
# conditional comma+key (debug_tail); this is where an unbalanced brace in
# any probe would corrupt the entire export.
TIMESTAMP="2026-10-08T00:00:00Z"
TIMESTAMP_HUMAN="test"
CLUSTER_NAME="test-cluster"
FINAL_STATUS="ACTIVE"
EXIT_CODE=0
total_criteria=4
idle_criteria_met=2
idle_threshold=3
CPU_IDLE_THRESHOLD=15
MEMORY_IDLE_THRESHOLD=35
APISERVER_IDLE_THRESHOLD=100
cpu_result="ACTIVE"; cpu_to_check="21.5"
mem_result="IDLE"; mem_to_check="20.0"
api_result="ACTIVE"; api_rate="269.4"
operator_result="IDLE"; operator_age_days="14"
HAS_GPU_NODES=false
GPU_NODE_COUNT=0
GPU_FLAVORS="N/A"; GPU_NODE_AGE="N/A"
GPU_CPU_CURRENT="N/A"; GPU_MEM_CURRENT="N/A"
GPU_CPU_WINDOWED="N/A"; GPU_MEM_WINDOWED="N/A"
DEBUG_JSON="$JSON"

source "$WORKDIR/export-json-func.sh"
export_json "$WORKDIR/full-export.json"

echo "=== assertions: export_json integration ==="
jq -e '
  .status == "ACTIVE" and
  .criteria.api_server.value == "269.4" and
  .debug.spikes.query_range.verdict == "ACTIVE" and
  .debug.criteria_detail.api_server.write_req_per_sec == "38" and
  (.debug.query_timings | length) == 16
' "$WORKDIR/full-export.json" >/dev/null \
  && echo "PASS: export_json embeds the debug section" \
  || fail "debug section corrupted by export_json embedding"
