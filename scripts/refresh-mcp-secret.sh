#!/bin/sh
# Refresh the MCP validator token from 1Password without exposing it.

set -eu

readonly secret_ref="op://Severino HQ Production/Severino HQ MCP/credential"
readonly credential_file="${CREDENTIALS_DIRECTORY:?}/op_service_account_token"
readonly secret_dir="/opt/apps/severino-hq/secrets"
readonly target="${secret_dir}/severino_mcp_token"

umask 077
install -d -m 700 -o root -g root "${secret_dir}"
temporary="$(mktemp "${secret_dir}/.severino_mcp_token.XXXXXX")"
trap 'rm -f "${temporary}"' EXIT HUP INT TERM

token="$(
    OP_SERVICE_ACCOUNT_TOKEN="$(cat "${credential_file}")" \
        op read "${secret_ref}"
)"

if [ "${#token}" -lt 32 ]; then
    echo "Refusing weak or empty MCP token from 1Password." >&2
    exit 1
fi

printf %s "${token}" >"${temporary}"
chown 10001:10001 "${temporary}"
chmod 400 "${temporary}"

if [ -f "${target}" ] && cmp -s "${temporary}" "${target}"; then
    echo "Severino HQ MCP token is current."
    exit 0
fi

# Preserve the bind-mounted inode when the container is already running.
if [ -f "${target}" ]; then
    cat "${temporary}" >"${target}"
    chown 10001:10001 "${target}"
    chmod 400 "${target}"
else
    mv "${temporary}" "${target}"
fi

if docker inspect severino-hq >/dev/null 2>&1; then
    docker restart severino-hq >/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
        status="$(
            docker inspect --format '{{.State.Health.Status}}' severino-hq \
                2>/dev/null || true
        )"
        if [ "${status}" = "healthy" ]; then
            echo "Refreshed MCP token and restarted healthy Severino HQ."
            exit 0
        fi
        sleep 5
    done
    echo "Severino HQ did not become healthy after secret rotation." >&2
    exit 1
fi

echo "Refreshed MCP token; Severino HQ is not currently installed."
