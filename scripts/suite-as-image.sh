#!/bin/sh
# The composed suite as the image runs it, from a checkout.
#
#   mise run suite:as-image [manage.py test arguments]
#
# The suite runs in a copy of only what the image is built from (the paths
# Dockerfile.dockerignore admits, tracked or new), so a test that reads docs/,
# .git or anything else the image leaves out fails here rather than in Compose.
# It runs as the image configures it: the task's composed environment (DEBUG
# off, the extension set mise.local.toml names), with SEVERINO_HQ_SOURCE and
# SEVERINO_HQ_REVISION stamped. Without an extension set it runs the host alone
# and says so; REQUIRE_COMPOSED=1 fails instead.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
cd "${repo}"

# shellcheck source=scripts/lib/composed.sh
. ./scripts/lib/composed.sh
# shellcheck source=scripts/lib/export-tree.sh
. ./scripts/lib/export-tree.sh
# shellcheck source=scripts/lib/source-url.sh
. ./scripts/lib/source-url.sh
composed_set || echo "No HQ_LOCAL_PLUGINS supplied: the host alone, as the image runs it (scripts/mise.local.example.toml)."

source_url="$(source_url)"
tree="$(mktemp -d)"
trap 'rm -rf "${tree}"' EXIT HUP INT TERM

admitted="$(sed -n 's|^!\([^*]*[^/]\)/*$|\1|p' Dockerfile.dockerignore)"
[ -n "${admitted}" ] || { echo "Dockerfile.dockerignore admits nothing." >&2; exit 1; }
# shellcheck disable=SC2086  # one pathspec per admitted path
export_tree "${tree}" ${admitted}

# The checkout's environment, not a new one in the copy.
environment="${UV_PROJECT_ENVIRONMENT:-.venv}"
case "${environment}" in /*) ;; *) environment="${repo}/${environment}" ;; esac

cd "${tree}"
exec env \
    UV_PROJECT_ENVIRONMENT="${environment}" \
    SEVERINO_HQ_SOURCE="${source_url}" \
    SEVERINO_HQ_REVISION="$(git -C "${repo}" rev-parse HEAD)" \
    uv run --locked python manage.py test --noinput --parallel auto "$@"
