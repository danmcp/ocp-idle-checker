#!/usr/bin/env bash
# Shared helpers for the hermetic tests in this directory. Source, don't run.
#
# The script under test is one flat bash file, not a library, so the tests
# cut the functions they need out of it with awk ranges rather than sourcing
# the whole thing (which would immediately start talking to a cluster).

# Resolve the script under test relative to this file, whatever the cwd.
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$TESTS_DIR/../ocp-idle-check.sh"
FIXTURES_BIN="$TESTS_DIR/fixtures/bin"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# Scratch directory per test run, removed on exit.
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# The timed query wrapper plus the whole debug-probe region (globals, probe
# functions, debug_collect_all), down to — not including — the main-script
# marker.
extract_debug_region() {  # <outfile>
    awk '/^# Debug wrapper around query_prometheus/,/^}/' "$SCRIPT" > "$1"
    awk '/^QUERY_TIMINGS_LOG=/,0' "$SCRIPT" | awk '/^# === MAIN SCRIPT ===/ {exit} {print}' >> "$1"
}

# export_json's body ends in a heredoc whose column-0 "}" (the JSON close)
# comes before the function's own closing brace, so the range has to stop at
# the heredoc's EOF terminator; the brace is re-added by hand.
extract_export_json() {  # <outfile>
    awk '/^export_json\(\) \{/,/^EOF$/' "$SCRIPT" > "$1"
    echo '}' >> "$1"
}
