#!/usr/bin/env bash
# Opens the logs a failed run sealed instead of publishing.
#   scripts/failure-logs.sh RUN_ID [DIR]
# Downloads the run's failure-logs artifact, decrypts it with the identity named
# by SEVERINO_FAILURE_LOG_IDENTITY (an op:// reference or a file; mise.local.toml sets
# it) and unpacks it into DIR, default a private temporary directory.
set -euo pipefail
cd "$(dirname "$0")/.."

run="${1:?usage: failure-logs.sh RUN_ID [DIR]}"
identity="${SEVERINO_FAILURE_LOG_IDENTITY:?set SEVERINO_FAILURE_LOG_IDENTITY (see scripts/mise.local.example.toml)}"

umask 077
dir="${2:-$(mktemp -d)}"
download="$(mktemp -d)"
trap 'rm -rf "$download"' EXIT
gh run download "$run" --name failure-logs --dir "$download"

# Read first, so a 1Password unlock that times out is reported as one.
case "$identity" in
  op://*) key="$(op read "$identity")" || { echo "1Password did not release the identity; approve the unlock and run again." >&2; exit 1; } ;;
  *) key="$(cat "$identity")" ;;
esac
mkdir -p "$dir"
age --decrypt --identity <(printf '%s\n' "$key") "$download/failure-logs.tar.age" | tar -xzf - -C "$dir"
unset key
echo "$dir"
find "$dir" -type f | sed "s|^$dir/|  |"
