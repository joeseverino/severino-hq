#!/bin/sh
# No known vulnerability or adverse project status in a project's locked
# dependencies, every group included: the host's own (`mise run lint:audit`)
# or an extension package's, by path where there is no mise (the admission
# action). Both run the host's pinned uv.
#   tools/pipeline/audit.sh [PROJECT_DIR]
set -eu
unset CDPATH

host="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
exec uv audit --project "${1:-$host}" --locked --preview-features audit-command
