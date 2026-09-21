#!/bin/sh
# Execute an allowlisted operation through a declared controller transport.

set -eu

readonly connection_ref="${1:?usage: controller-ssh.sh CONNECTION_REF OPERATION}"
readonly operation="${2:?usage: controller-ssh.sh CONNECTION_REF OPERATION}"
readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
readonly ssh_dir="${app_dir}/secrets/ssh"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/lib/controller-env.sh
. "${script_dir}/lib/controller-env.sh"
readonly env_file="${controller_env}"

if [ "$(id -u)" -ne 0 ]; then
    echo "controller-ssh.sh must run as root." >&2
    exit 1
fi
controller_require_environment
# shellcheck source=/dev/null
. "${env_file}"
prefix=""
reference=""
# Only validated variable-name tokens are emitted, not arbitrary lines.
# shellcheck disable=SC2013
for candidate in $(sed -n 's/^\([A-Z][A-Z0-9_]*\)_CONNECTION_REF=.*/\1/p' "${env_file}"); do
    eval "reference=\${${candidate}_CONNECTION_REF}"
    if [ "${reference}" = "${connection_ref}" ]; then
        prefix="${candidate}"
        break
    fi
done
if [ -z "${prefix}" ]; then
    echo "Unknown SSH connection_ref=${connection_ref}." >&2
    exit 1
fi

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
