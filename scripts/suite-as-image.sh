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

if [ -z "${HQ_LOCAL_PLUGINS:-}" ]; then
    if [ "${REQUIRE_COMPOSED:-0}" = 1 ]; then
        echo "The composed suite is required and HQ_LOCAL_PLUGINS is unset (scripts/mise.local.example.toml)." >&2
        exit 1
    fi
    echo "No HQ_LOCAL_PLUGINS supplied: the host alone, as the image runs it (scripts/mise.local.example.toml)."
fi

# shellcheck source=scripts/lib/source-url.sh
. ./scripts/lib/source-url.sh
source_url="$(source_url)"
tree="$(mktemp -d)"
trap 'rm -rf "${tree}"' EXIT HUP INT TERM

admitted="$(sed -n 's|^!\([^*]*[^/]\)/*$|\1|p' Dockerfile.dockerignore)"
[ -n "${admitted}" ] || { echo "Dockerfile.dockerignore admits nothing." >&2; exit 1; }
# shellcheck disable=SC2086  # one pathspec per admitted path
git -c core.quotepath=off ls-files --cached --others --exclude-standard -- ${admitted} |
    while IFS= read -r file; do
        [ -f "${file}" ] && printf '%s\n' "${file}"
    done | tar -cf - -T - | tar -xf - -C "${tree}"

# The checkout's environment, not a new one in the copy.
environment="${UV_PROJECT_ENVIRONMENT:-.venv}"
case "${environment}" in /*) ;; *) environment="${repo}/${environment}" ;; esac

cd "${tree}"
exec env \
    UV_PROJECT_ENVIRONMENT="${environment}" \
    SEVERINO_HQ_SOURCE="${source_url}" \
    SEVERINO_HQ_REVISION="$(git -C "${repo}" rev-parse HEAD)" \
    uv run --locked python manage.py test --noinput --parallel auto "$@"
