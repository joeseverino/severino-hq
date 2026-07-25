#!/bin/sh
# Execute the controller inside the exact running HQ image. Provider secrets
# enter only this short-lived exec process, never the web process environment.

set -eu

readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
readonly env_file="${app_dir}/secrets/severino_controller_env"
readonly registry="${app_dir}/config/controller-connections.json"
readonly mode="${1:-}"
readonly container="${HQ_CONTAINER:-severino-hq}"

if [ "$(id -u)" -ne 0 ]; then
    echo "run-controller.sh must run as root." >&2
    exit 1
fi
if [ ! -s "${env_file}" ]; then
    echo "Controller environment is missing." >&2
    exit 1
fi

set -a
# Values are shell-quoted by render-controller-env.sh.
# shellcheck disable=SC1090
. "${env_file}"
set +a

set -- exec --env HQ_IN_PROCESS=1
for connection_ref in $(jq -r '.connections | keys[]' "${registry}"); do
    prefix="$(
        jq -r --arg ref "${connection_ref}" \
            '.connections[$ref].env_prefix' "${registry}"
    )"
    projection="$(
        jq -r --arg ref "${connection_ref}" \
            '.connections[$ref].projection' "${registry}"
    )"
    for env_name in $(
        jq -r --arg projection "${projection}" \
            '.projections[$projection] | keys[]' "${registry}"
    ); do
        set -- "$@" --env "${prefix}_${env_name}"
    done
done
set -- "$@" "${container}" python -m controller_runtime.worker
if [ "${mode}" = "--apply" ]; then
    set -- "$@" --apply
fi

exec docker "$@"
