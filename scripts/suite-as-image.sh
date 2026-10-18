#!/bin/sh
# The composed suite as the image runs it, from a checkout.
#
#   scripts/suite-as-image.sh [manage.py test arguments]
#
# The suite runs in a copy of only what the image is built from (the paths
# Dockerfile.dockerignore admits, tracked or new), so a test that reads docs/,
# .git or anything else the image leaves out fails here rather than in Compose.
# It runs as the image configures it: DEBUG off, SEVERINO_HQ_SOURCE and
# SEVERINO_HQ_REVISION stamped, and the extension set mise.local.toml names
# (HQ_LOCAL_PLUGINS, HQ_LOCAL_PYTHONPATH). Without one it runs the host alone
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

# The address the build stamps, as https whatever the remote's scheme.
source_url="$(git remote get-url origin | sed -E 's#^git@([^:]+):#https://\1/#; s#\.git$##')"
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
exec env -u DJANGO_DEBUG \
    UV_PROJECT_ENVIRONMENT="${environment}" \
    SEVERINO_HQ_SOURCE="${source_url}" \
    SEVERINO_HQ_REVISION="$(git -C "${repo}" rev-parse HEAD)" \
    SEVERINO_HQ_PLUGINS="${HQ_LOCAL_PLUGINS:-}" \
    PYTHONPATH="${HQ_LOCAL_PYTHONPATH:-}" \
    uv run --locked python manage.py test --noinput --parallel auto "$@"
