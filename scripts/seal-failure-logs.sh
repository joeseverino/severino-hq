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

if ! command -v age >/dev/null; then
  tools="${RUNNER_TEMP:-$(mktemp -d)}/age-${AGE_VERSION}"
  archive="$tools.tar.gz"
  curl -fsSL --retry 3 -o "$archive" \
    "https://github.com/FiloSottile/age/releases/download/v${AGE_VERSION}/age-v${AGE_VERSION}-linux-amd64.tar.gz"
  echo "${AGE_SHA256}  ${archive}" | sha256sum --check --quiet
  mkdir -p "$tools"
  tar -xzf "$archive" -C "$tools" --strip-components 1 age/age
  PATH="$tools:$PATH"
fi

(cd "$dir" && find . -type f -size +0 -print0) \
  | tar -czf - -C "$dir" --null -T - \
  | age --encrypt --recipient "$FAILURE_LOG_RECIPIENT" --output "$out"
echo "sealed $(find "$dir" -type f -size +0 | wc -l | tr -d ' ') withheld log(s)"
