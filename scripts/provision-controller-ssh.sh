#!/bin/sh
# Provision stable, controller-owned SSH identities and pinned host keys.
# This never copies or reads a personal SSH identity.

set -eu

readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
readonly registry="${app_dir}/config/controller-connections.json"
readonly ssh_dir="${app_dir}/secrets/ssh"
readonly env_file="${app_dir}/secrets/severino_controller_env"

if [ "$(id -u)" -ne 0 ]; then
    echo "provision-controller-ssh.sh must run as root." >&2
    exit 1
fi

# The registry declares the shape a connection can take. Which connections
# exist is the vault's to say, and they arrive already resolved in the rendered
# environment read below -- so there is nothing to cross-check here beyond the
# projection this script depends on being defined at all.
jq -e '
    .schema_version == 1
    and (.projections.ssh_transport | type == "object")
    and (.projections.ssh_transport | has("HOST") and has("PORT") and has("USER"))
' "${registry}" >/dev/null

# Endpoints come from 1Password through the rendered controller environment.
if [ ! -s "${env_file}" ]; then
    echo "Controller environment is missing." >&2
    exit 1
fi
# shellcheck source=/dev/null  # rendered at deploy time, not in the repo
. "${env_file}"

umask 077
install -d -o root -g root -m 0700 "${ssh_dir}"
temporary="$(mktemp "${ssh_dir}/.known_hosts.XXXXXX")"
trap 'rm -f "${temporary}"' EXIT HUP INT TERM

# Which connections open a shell is read off the rendered environment, the same
# way the controller reads it: a connection carrying a host and a user is one
# this can open. The registry says what an ssh_transport looks like; it does not
# know which of them exist, because the vault does.
while IFS= read -r prefix; do
    [ -n "${prefix}" ] || continue
    host=""
    eval "host=\${${prefix}_HOST:-}"
    [ -n "${host}" ] || continue
    connection_ref=""
    eval "connection_ref=\${${prefix}_CONNECTION_REF:-}"
    [ -n "${connection_ref}" ] || continue

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

    port=""
    host_key=""
    eval "port=\${${prefix}_PORT:-}"
    eval "host_key=\${${prefix}_HOST_KEY:-}"
    if [ -z "${host}" ] || [ -z "${port}" ] || [ -z "${host_key}" ]; then
        echo "${prefix}_HOST, ${prefix}_PORT and ${prefix}_HOST_KEY are required." >&2
        exit 1
    fi
    case "${host_key}" in
        "ssh-ed25519 "*) ;;
        *) echo "${prefix}_HOST_KEY must be an ssh-ed25519 key." >&2; exit 1 ;;
    esac
    printf '[%s]:%s %s\n' "${host}" "${port}" "${host_key}" >>"${temporary}"
done <<EOF
$(sed -nE 's/^([A-Z][A-Z0-9_]*)_CONNECTION_REF=.*/\1/p' "${env_file}")
EOF

sort -u "${temporary}" -o "${temporary}"
install -o root -g root -m 0600 "${temporary}" "${ssh_dir}/known_hosts"
rm -f "${temporary}"
trap - EXIT HUP INT TERM

echo "Controller SSH identities and pinned hosts are provisioned."
