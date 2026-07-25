#!/bin/sh
# Provision stable, controller-owned SSH identities and pinned host keys.
# This never copies or reads a personal SSH identity.

set -eu

readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
readonly registry="${app_dir}/config/controller-connections.json"
readonly ssh_dir="${app_dir}/secrets/ssh"

if [ "$(id -u)" -ne 0 ]; then
    echo "provision-controller-ssh.sh must run as root." >&2
    exit 1
fi

jq -e '
    .schema_version == 1
    and (.ssh_transports | type == "object")
    and all(
        .ssh_transports | to_entries[];
        (.key | test("^[a-z0-9][a-z0-9-]*$"))
        and (.value.host | type == "string" and length > 0)
        and (.value.port | type == "number" and . >= 1 and . <= 65535)
        and (.value.user | test("^[a-z_][a-z0-9_-]*$"))
        and (.value.host_key | test("^ssh-ed25519 [A-Za-z0-9+/]+={0,2}$"))
    )
' "${registry}" >/dev/null

umask 077
install -d -o root -g root -m 0700 "${ssh_dir}"
temporary="$(mktemp "${ssh_dir}/.known_hosts.XXXXXX")"
trap 'rm -f "${temporary}"' EXIT HUP INT TERM

for connection_ref in $(jq -r '.ssh_transports | keys[]' "${registry}"); do
    identity="${ssh_dir}/${connection_ref}"
    if [ ! -f "${identity}" ]; then
        ssh-keygen -q -t ed25519 -N '' \
            -C "severino-hq:${connection_ref}" -f "${identity}"
    fi
    if [ ! -s "${identity}.pub" ]; then
        ssh-keygen -y -f "${identity}" >"${identity}.pub"
    fi
    chmod 0600 "${identity}"
    chmod 0644 "${identity}.pub"

    host="$(jq -r --arg ref "${connection_ref}" '.ssh_transports[$ref].host' "${registry}")"
    port="$(jq -r --arg ref "${connection_ref}" '.ssh_transports[$ref].port' "${registry}")"
    host_key="$(jq -r --arg ref "${connection_ref}" '.ssh_transports[$ref].host_key' "${registry}")"
    printf '[%s]:%s %s\n' "${host}" "${port}" "${host_key}" >>"${temporary}"
done

sort -u "${temporary}" -o "${temporary}"
install -o root -g root -m 0600 "${temporary}" "${ssh_dir}/known_hosts"
rm -f "${temporary}"
trap - EXIT HUP INT TERM

echo "Controller SSH identities and pinned hosts are provisioned."
