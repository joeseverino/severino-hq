#!/bin/sh
# Refresh Severino HQ secrets from 1Password without exposing them:
#   - the MCP validator token   -> secrets/severino_mcp_token
#   - the full app environment  -> secrets/severino_hq_env
# Both are root-rendered, owned by the container UID, and bind-mounted
# read-only. The container is restarted once, only when something changed.

set -eu

readonly vault="Severino HQ Production"
readonly mcp_ref="op://Severino HQ Production/Severino HQ MCP/credential"
readonly env_item="severino-hq env"
readonly credential_file="${CREDENTIALS_DIRECTORY:?}/op_service_account_token"
readonly script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
readonly secret_dir="/opt/apps/severino-hq/secrets"
readonly mcp_target="${secret_dir}/severino_mcp_token"
readonly env_target="${secret_dir}/severino_hq_env"

umask 077
install -d -m 700 -o root -g root "${secret_dir}"
trap 'rm -f "${secret_dir}"/.tmp.*' EXIT HUP INT TERM

OP_SERVICE_ACCOUNT_TOKEN="$(cat "${credential_file}")"
export OP_SERVICE_ACCOUNT_TOKEN

changed=0

# install_if_changed <tmp> <target> — atomic-ish install that preserves the
# bind-mounted inode when the container is already running.
install_if_changed() {
    chown 10001:10001 "$1"
    chmod 400 "$1"
    if [ -f "$2" ] && cmp -s "$1" "$2"; then
        rm -f "$1"
        return 0
    fi
    if [ -f "$2" ]; then
        cat "$1" >"$2"
        chown 10001:10001 "$2"
        chmod 400 "$2"
        rm -f "$1"
    else
        mv "$1" "$2"
    fi
    changed=1
}

# MCP validator token
temporary="$(mktemp "${secret_dir}/.tmp.XXXXXX")"
token="$(op read "${mcp_ref}")"
if [ "${#token}" -lt 32 ]; then
    echo "Refusing weak or empty MCP token from 1Password." >&2
    exit 1
fi
printf %s "${token}" >"${temporary}"
install_if_changed "${temporary}" "${mcp_target}"

# App environment — every UPPER_SNAKE field on the env item
temporary="$(mktemp "${secret_dir}/.tmp.XXXXXX")"
op item get "${env_item}" --vault "${vault}" --format json \
    | jq -r -f "${script_dir}/render-env.jq" >"${temporary}"
count="$(grep -c . "${temporary}" || true)"
if [ "${count}" -lt 15 ]; then
    echo "Refusing suspiciously small app env (${count} vars) from 1Password." >&2
    exit 1
fi
install_if_changed "${temporary}" "${env_target}"

if [ "${changed}" -eq 0 ]; then
    echo "Severino HQ secrets are current."
    exit 0
fi

if docker inspect severino-hq >/dev/null 2>&1; then
    docker restart severino-hq >/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
        status="$(
            docker inspect --format '{{.State.Health.Status}}' severino-hq \
                2>/dev/null || true
        )"
        if [ "${status}" = "healthy" ]; then
            echo "Refreshed secrets and restarted healthy Severino HQ."
            exit 0
        fi
        sleep 5
    done
    echo "Severino HQ did not become healthy after secret rotation." >&2
    exit 1
fi

echo "Refreshed secrets; Severino HQ is not currently installed."
