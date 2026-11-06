#!/bin/sh
# Prove each suite that runs a file task in a copy of the checkout copies the
# libraries the task sources, read from the task's own lines: a task that
# begins to source one more still passes its suite, with no list to update.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

failures=0
for case in test-suite-coverage.sh:mise-tasks/test/suite test-composed.sh:mise-tasks/test/composed; do
    suite="${case%%:*}"
    task="${case#*:}"
    # A checkout holding what the suite copies, its task sourcing one library
    # more, ahead of the ones it sources now.
    copy="${work}/${suite}"
    mkdir -p "${copy}/tests" "${copy}/tools" "${copy}/$(dirname "${task}")"
    cp -R "${repo}/tests/shell" "${copy}/tests/"
    cp -R "${repo}/tools/lib" "${repo}/tools/gate" "${repo}/tools/__init__.py" "${copy}/tools/"
    : >"${copy}/tools/lib/example.sh"
    cp "${repo}/${task}" "${copy}/${task}"
    awk '!planted && /^\. \.\/tools\/lib\// { print ". ./tools/lib/example.sh"; planted = 1 } { print }' \
        "${repo}/${task}" >"${copy}/${task}"
    status=0
    "${SHELL_UNDER_TEST:-/bin/sh}" "${copy}/tests/shell/${suite}" >"${work}/out" 2>&1 || status=$?
    if [ "${status}" -ne 0 ]; then
        echo "FAIL ${suite}: with ${task} sourcing tools/lib/example.sh, it exited ${status}" >&2
        cat "${work}/out" >&2
        failures=$((failures + 1))
    fi
done

if [ "${failures}" -ne 0 ]; then
    echo "task library contracts failed (${failures})." >&2
    exit 1
fi
echo "task library contracts hold."
