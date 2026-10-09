#!/bin/bash
#
# OpenShift Cluster Idle Detection Script - entrypoint shim.
#
# The implementation lives in ocp_idle_check.py (Python 3, stdlib only).
# This shim exists so every current caller keeps working unchanged:
#   - the Jenkins job ("bash ocp-idle-check.sh ...")
#   - cluster-monitor's vendored copy (subprocess.run(["bash", ...]))
#   - the README examples
#
# Exit codes (set by ocp_idle_check.py):
#   0 = Cluster is ACTIVE
#   1 = Cluster is IDLE
#   2 = Error (cannot determine state)
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/ocp_idle_check.py" "$@"
