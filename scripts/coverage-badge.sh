#!/bin/sh
# The README coverage badge states the coverage this run measured.
#
# One definition for CI and scripts/ci-local.sh. Whole percent, rounded down:
# the default total is rounded, and a figure on a .5 boundary rounds
# differently across versions.
#
# Usage: scripts/coverage-badge.sh [python]   (after `coverage combine`)
set -eu

python_bin="${1:-python}"
measured="$("$python_bin" -m coverage report --format=total --precision=2)"
measured="${measured%.*}"
# One capture group: the URL-encoded percent sign in `coverage-86%25-` is
# itself digits, and a looser match reads the badge as both 86 and 25.
claimed="$(sed -nE 's/.*coverage-([0-9]+)%25-.*/\1/p' README.md | head -1)"
if [ "$measured" != "$claimed" ]; then
    echo "README claims ${claimed}% coverage; this run measured ${measured}%. Update the badge to ${measured}%." >&2
    exit 1
fi
echo "README badge agrees with the measured ${measured}%."
