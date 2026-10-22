#!/bin/sh
# No known vulnerability in a project's locked dependencies, audited by the
# host's pinned pip-audit: the host's own (`mise run checks:audit`) or an
# extension package's, by path where there is no mise (the admission action).
#   tools/pipeline/audit.sh [PROJECT_DIR]
set -eu
unset CDPATH

host="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
project="${1:-$host}"

export="$(mktemp)"
trap 'rm -f "$export"' EXIT
uv export --project "$project" --all-groups --no-emit-project --output-file "$export" >/dev/null
# The export is exact and hashed, so nothing is resolved or installed to audit it.
uv run --project "$host" --only-group audit pip-audit --disable-pip --require-hashes -r "$export"
