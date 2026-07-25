#!/bin/sh
# Execute an allowlisted operation through a declared controller transport.

set -eu

readonly connection_ref="${1:?usage: controller-ssh.sh CONNECTION_REF OPERATION}"
readonly operation="${2:?usage: controller-ssh.sh CONNECTION_REF OPERATION}"
readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
readonly registry="${app_dir}/config/controller-connections.json"
readonly ssh_dir="${app_dir}/secrets/ssh"

if [ "$(id -u)" -ne 0 ]; then
    echo "controller-ssh.sh must run as root." >&2
    exit 1
fi
if ! jq -e --arg ref "${connection_ref}" '.ssh_transports[$ref]' "${registry}" >/dev/null; then
    echo "Unknown SSH connection_ref=${connection_ref}." >&2
    exit 1
fi

host="$(jq -r --arg ref "${connection_ref}" '.ssh_transports[$ref].host' "${registry}")"
port="$(jq -r --arg ref "${connection_ref}" '.ssh_transports[$ref].port' "${registry}")"
user="$(jq -r --arg ref "${connection_ref}" '.ssh_transports[$ref].user' "${registry}")"

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
