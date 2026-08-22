#!/bin/sh
# Execute the controller in a short-lived, isolated container made from the
# exact running HQ image. The web container never receives provider secrets,
# deployment identities, ACME state, or certificate private keys.

set -eu

readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
readonly env_file="${app_dir}/secrets/severino_controller_env"
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

# Labelled so the sweep this container is about to run can tell that one of the
# containers it finds is itself. Unlabelled, Docker invents a name for it and
# the machine grows a row called something different every minute.
set -- run --rm --network host --user 10001:10001 --cap-drop ALL \
    --label severino-hq.role=controller \
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
# Forward what the renderer produced, rather than recomputing the same names
# from a registry. The registry holds the shape a connection can take; which
# connections exist is the vault's to say, so a list rebuilt here is a second
# answer to a question this file cannot see.
#
# The names only: `--env NAME` passes the value already sourced above, so no
# secret reaches the process table.
while IFS= read -r env_name; do
    [ -n "${env_name}" ] || continue
    set -- "$@" --env "${env_name}"
done <<EOF
$(sed -nE 's/^([A-Z][A-Z0-9_]*)=.*/\1/p' "${env_file}")
EOF
set -- "$@" "${image}" -m controller_runtime.worker
if [ "${mode}" = "--apply" ]; then
    set -- "$@" --apply
fi

docker "$@"
