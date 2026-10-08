#!/usr/bin/env bash
# Run every hermetic test in this directory (test-*.sh). Each test is
# self-contained: no cluster, no network — fake oc/curl/timeout binaries
# come from fixtures/bin. Prints one PASS/FAIL line per test; the captured
# output of a failing test follows its FAIL line.
cd "$(dirname "$0")" || exit 1

logs="$(mktemp -d)"
trap 'rm -rf "$logs"' EXIT

rc=0
for t in test-*.sh; do
    if bash "$t" > "$logs/$t.out" 2>&1; then
        echo "PASS  $t"
    else
        echo "FAIL  $t (output below)"
        cat "$logs/$t.out"
        rc=1
    fi
done
exit $rc
