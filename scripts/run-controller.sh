#!/bin/sh
# Execute the controller in a short-lived, isolated container made from the
# exact running HQ image. The web container never receives provider secrets,
# deployment identities, ACME state, or certificate private keys.

set -eu

readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
readonly env_file="${app_dir}/secrets/severino_controller_env"
readonly registry="${app_dir}/config/controller-connections.json"
readonly mode="${1:-}"
readonly container="${HQ_CONTAINER:-severino-hq}"
readonly ssh_dir="${app_dir}/secrets/ssh"
readonly acme_dir="${app_dir}/secrets/acme"
readonly app_env="${app_dir}/secrets/severino_hq_env"
readonly ca_file="/usr/local/share/ca-certificates/severino-labs-root-ca.crt"

if [ "$(id -u)" -ne 0 ]; then
    echo "run-controller.sh must run as root." >&2
    exit 1
fi
if [ ! -s "${env_file}" ]; then
    echo "Controller environment is missing." >&2
    exit 1
fi
if [ ! -s "${app_env}" ] || [ ! -s "${ca_file}" ]; then
    echo "Controller application environment or CA bundle is missing." >&2
    exit 1
fi

install -d -o root -g root -m 0700 "${acme_dir}"
chown 10001:10001 "${acme_dir}"
runtime_app_env="$(mktemp /run/severino-hq-controller-env.XXXXXX)"
runtime_ssh_dir="$(mktemp -d /run/severino-hq-controller-ssh.XXXXXX)"
trap 'rm -f "${runtime_app_env}"; rm -rf "${runtime_ssh_dir}"' EXIT HUP INT TERM
install -o root -g root -m 0400 "${app_env}" "${runtime_app_env}"
chown 10001:10001 "${runtime_app_env}"
cp -a "${ssh_dir}/." "${runtime_ssh_dir}/"
chown -R 10001:10001 "${runtime_ssh_dir}"
image="$(docker inspect --format '{{.Config.Image}}' "${container}")"
data_volume="$(
    docker inspect --format \
        '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' \
        "${container}"
)"
if [ -z "${image}" ] || [ -z "${data_volume}" ]; then
    echo "Could not resolve the deployed image or HQ data volume." >&2
    exit 1
fi

set -a
# Values are shell-quoted by render-controller-env.sh.
# shellcheck disable=SC1090
. "${env_file}"
set +a

set -- run --rm --network host --user 10001:10001 --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --entrypoint python \
    --mount "type=volume,source=${data_volume},target=/data" \
    --mount "type=bind,source=${runtime_app_env},target=/run/secrets/severino_hq_env,readonly" \
    --mount "type=bind,source=${ca_file},target=/run/secrets/severino_controller_ca.pem,readonly" \
    --mount "type=bind,source=${runtime_ssh_dir},target=/run/secrets/controller-ssh,readonly" \
    --mount "type=bind,source=${acme_dir},target=/var/lib/severino-hq/acme" \
    --env HQ_IN_PROCESS=1 \
    --env HQ_CONTROLLER_SSH_DIR=/run/secrets/controller-ssh \
    --env HQ_ACME_DIR=/var/lib/severino-hq/acme \
    --env HQ_CONTROLLER_CA_FILE=/run/secrets/severino_controller_ca.pem
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
set -- "$@" "${image}" -m controller_runtime.worker
if [ "${mode}" = "--apply" ]; then
    set -- "$@" --apply
fi

docker "$@"
