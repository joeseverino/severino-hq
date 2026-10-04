#!/usr/bin/env bash
# Seals a failed run's private output for the operator alone.
#   scripts/seal-failure-logs.sh DIR OUT
# DIR holds what the public log withheld. OUT is DIR as a tarball encrypted to
# FAILURE_LOG_RECIPIENT: an artifact anyone can download and only the holder of
# the matching identity can open (scripts/failure-logs.sh). Prints nothing from DIR.
set -euo pipefail

dir="${1:?usage: seal-failure-logs.sh DIR OUT}"
out="${2:?usage: seal-failure-logs.sh DIR OUT}"
. ./scripts/toolchain.env

if [ -z "$(find "$dir" -type f -size +0 -print -quit 2>/dev/null)" ]; then
  echo "nothing withheld from this run's log"
  exit 0
fi

# age is one of the pinned tools (mise.toml); the workflow installs it.
command -v age >/dev/null || { echo "age is not installed (mise install age)." >&2; exit 1; }

(cd "$dir" && find . -type f -size +0 -print0) \
  | tar -czf - -C "$dir" --null -T - \
  | age --encrypt --recipient "$FAILURE_LOG_RECIPIENT" --output "$out"
echo "sealed $(find "$dir" -type f -size +0 | wc -l | tr -d ' ') withheld log(s)"
