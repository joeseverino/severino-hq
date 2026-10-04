#!/bin/sh
# Execute an allowlisted operation through a declared controller transport.

set -eu

readonly connection_ref="${1:?usage: controller-ssh.sh CONNECTION_REF OPERATION}"
readonly operation="${2:?usage: controller-ssh.sh CONNECTION_REF OPERATION}"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/lib/controller-env.sh
. "${script_dir}/lib/controller-env.sh"
# Rendered by hq-secrets from each connection's identity item.
readonly ssh_dir="${controller_runtime_dir}/ssh"

if [ "$(id -u)" -ne 0 ]; then
    echo "controller-ssh.sh must run as root." >&2
    exit 1
fi
# Held while the connection is read and its identity copied, so both are of
# one generation.
controller_ssh_lock shared
controller_require_connections

# One setting of the named connection, read as data: the reference is an
# argument to jq, never part of its program or of a shell word.
connection_value() {
    jq -er --arg ref "${connection_ref}" --arg name "$1" '
        [.connections[] | select(.ref == $ref)]
        | if length == 1 then .[0].values[$name] // "" else error("unknown connection") end
    ' "${controller_connections}" 2>/dev/null
}
if ! host="$(connection_value HOST)"; then
    echo "Unknown SSH connection_ref=${connection_ref}." >&2
    exit 1
fi
port="$(connection_value PORT)"
user="$(connection_value USER)"
if [ -z "${host}" ] || [ -z "${port}" ] || [ -z "${user}" ]; then
    echo "Connection ${connection_ref} has no SSH host, port and user." >&2
    exit 1
fi

# The destination argument: a leading dash would be an ssh option, an @ or a
# space would move where the user ends and the host begins.
case "${user}" in
    -* | *[!A-Za-z0-9_.-]*) echo "The connection's USER is not a login name." >&2; exit 1 ;;
esac
case "${host}" in
    -* | *[!A-Za-z0-9.:-]*) echo "The connection's HOST is not a host name or address." >&2; exit 1 ;;
esac
case "${port}" in
    '' | *[!0-9]*) echo "The connection's PORT is not a port." >&2; exit 1 ;;
esac

# Allowlisted by operation, not by host. What this constrains is which command
# may be run on the far end; which hosts exist is decided by which credentials
# the controller was given.
case "${operation}" in
    preflight)
        remote_command='preflight'
        ;;
    *)
        echo "SSH operation ${operation} is not allowed." >&2
        exit 1
        ;;
esac

# The identity and the pinned hosts of this generation, copied under the lock
# and used from the copy, so the lock is let go before ssh runs: a remote that
# hangs must not hold up every render for the length of its session. The copy
# sits on the same root-only tmpfs and goes when this script does; the
# launcher clears any `run.*` a killed one left.
run_dir="$(mktemp -d "${controller_runtime_dir}/run.ssh.XXXXXX")"
trap 'rm -rf "${run_dir}"' EXIT
trap 'exit 1' HUP INT TERM
cp -p "${ssh_dir}/${connection_ref}" "${run_dir}/identity"
cp -p "${ssh_dir}/known_hosts" "${run_dir}/known_hosts"
exec 8>&-

ssh \
    -F /dev/null \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="${run_dir}/known_hosts" \
    -o GlobalKnownHostsFile=/dev/null \
    -o ConnectTimeout=10 \
    -i "${run_dir}/identity" \
    -p "${port}" \
    -- \
    "${user}@${host}" \
    "${remote_command}"
