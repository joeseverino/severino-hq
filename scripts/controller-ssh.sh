#!/bin/sh
# Execute an allowlisted operation through a declared controller transport.

set -eu

readonly connection_ref="${1:?usage: controller-ssh.sh CONNECTION_REF OPERATION}"
readonly operation="${2:?usage: controller-ssh.sh CONNECTION_REF OPERATION}"
readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
readonly registry="${app_dir}/config/controller-connections.json"
readonly ssh_dir="${app_dir}/secrets/ssh"
readonly env_file="${app_dir}/secrets/severino_controller_env"

if [ "$(id -u)" -ne 0 ]; then
    echo "controller-ssh.sh must run as root." >&2
    exit 1
fi
prefix="$(
    jq -r --arg ref "${connection_ref}" \
        '.connections[$ref] | select(.projection == "ssh_transport") | .env_prefix // empty' \
        "${registry}"
)"
if [ -z "${prefix}" ]; then
    echo "Unknown SSH connection_ref=${connection_ref}." >&2
    exit 1
fi

# Endpoints come from 1Password through the rendered controller environment,
# the same path every other connection's values take.
if [ ! -s "${env_file}" ]; then
    echo "Controller environment is missing." >&2
    exit 1
fi
# shellcheck source=/dev/null  # rendered at deploy time, not in the repo
. "${env_file}"

host=""
port=""
user=""
eval "host=\${${prefix}_HOST:-}"
eval "port=\${${prefix}_PORT:-}"
eval "user=\${${prefix}_USER:-}"
for required in host port user; do
    eval "value=\${${required}}"
    if [ -z "${value}" ]; then
        echo "${prefix}_$(printf %s "${required}" | tr '[:lower:]' '[:upper:]') is required." >&2
        exit 1
    fi
done

case "${connection_ref}:${operation}" in
    edge:preflight)
        remote_command='preflight'
        ;;
    namecheap-cpanel:preflight)
        remote_command='preflight'
        ;;
    *)
        echo "SSH operation ${operation} is not allowed for ${connection_ref}." >&2
        exit 1
        ;;
esac

exec ssh \
    -F /dev/null \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="${ssh_dir}/known_hosts" \
    -o GlobalKnownHostsFile=/dev/null \
    -o ConnectTimeout=10 \
    -i "${ssh_dir}/${connection_ref}" \
    -p "${port}" \
    "${user}@${host}" \
    "${remote_command}"
