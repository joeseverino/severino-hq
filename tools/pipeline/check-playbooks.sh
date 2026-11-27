#!/bin/sh
# Every reason HQ would refuse to adopt a repository of playbooks, read from
# its checkout by the scan and judgment adoption runs, in the automation
# worker's environment (tools/lib/runner-environment.sh). Run by path from
# that repository's CI, against a checkout of HQ's main:
#   tools/pipeline/check-playbooks.sh <checkout>
set -eu
unset CDPATH

[ "$#" -eq 1 ] || { echo "usage: $0 <checkout>" >&2; exit 2; }
checkout=$(cd -- "$1" && pwd)
cd -- "$(dirname -- "$0")/../.."
# shellcheck source=tools/lib/runner-environment.sh
. ./tools/lib/runner-environment.sh
UV_PROJECT_ENVIRONMENT=$runner_venv exec uv run --locked --group runner python -m tools.pipeline.check_playbooks "$checkout"
