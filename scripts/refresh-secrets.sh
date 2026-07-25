#!/bin/sh
# Refresh Severino HQ secrets from 1Password without exposing them:
#   - the MCP validator token   -> secrets/severino_mcp_token
#   - the full app environment  -> secrets/severino_hq_env
#   - controller providers      -> secrets/severino_controller_env
# All are root-rendered. Web secrets become container-owned read-only mounts;
# controller secrets remain root-owned and never enter the web container.

set -eu

readonly vault="Severino HQ Production"
readonly mcp_ref="op://Severino HQ Production/Severino HQ MCP/credential"
readonly env_item="severino-hq env"
readonly credential_file="${CREDENTIALS_DIRECTORY:?}/op_service_account_token"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
readonly secret_dir="/opt/apps/severino-hq/secrets"
readonly mcp_target="${secret_dir}/severino_mcp_token"
readonly env_target="${secret_dir}/severino_hq_env"
readonly controller_target="${secret_dir}/severino_controller_env"

umask 077
install -d -m 700 -o root -g root "${secret_dir}"
trap 'rm -f "${secret_dir}"/.tmp.*' EXIT HUP INT TERM

OP_SERVICE_ACCOUNT_TOKEN="$(cat "${credential_file}")"
export OP_SERVICE_ACCOUNT_TOKEN

any_changed=0
web_changed=0
installed_change=0

# install_if_changed <tmp> <target> <uid> <gid> — atomic-ish install that
# preserves a bind-mounted inode when the web container is already running.
install_if_changed() {
    installed_change=0
    chown "$3:$4" "$1"
    chmod 400 "$1"
    if [ -f "$2" ] && cmp -s "$1" "$2"; then
        rm -f "$1"
        return 0
    fi
    if [ -f "$2" ]; then
        cat "$1" >"$2"
        chown "$3:$4" "$2"
        chmod 400 "$2"
        rm -f "$1"
    else
        mv "$1" "$2"
    fi
    installed_change=1
    any_changed=1
}

# MCP validator token
temporary="$(mktemp "${secret_dir}/.tmp.XXXXXX")"
token="$(op read "${mcp_ref}")"
if [ "${#token}" -lt 32 ]; then
    echo "Refusing weak or empty MCP token from 1Password." >&2
    exit 1
fi
printf %s "${token}" >"${temporary}"
install_if_changed "${temporary}" "${mcp_target}" 10001 10001
if [ "${installed_change}" -eq 1 ]; then
    web_changed=1
fi

# App environment — every UPPER_SNAKE field on the env item
temporary="$(mktemp "${secret_dir}/.tmp.XXXXXX")"
op item get "${env_item}" --vault "${vault}" --format json \
    | jq -r -f "${script_dir}/render-env.jq" >"${temporary}"
count="$(grep -c . "${temporary}" || true)"
if [ "${count}" -lt 15 ]; then
    echo "Refusing suspiciously small app env (${count} vars) from 1Password." >&2
    exit 1
fi
install_if_changed "${temporary}" "${env_target}" 10001 10001
if [ "${installed_change}" -eq 1 ]; then
    web_changed=1
fi

# Controller-only provider environment. This file is intentionally not mounted
# into the web container; run-controller.sh forwards it only to controller exec.
temporary="$(mktemp "${secret_dir}/.tmp.XXXXXX")"
"${script_dir}/render-controller-env.sh" "${vault}" >"${temporary}"
count="$(grep -c . "${temporary}" || true)"
expected_count="$(
    jq '
        [.connections[].projection] as $selected
        | [$selected[] as $projection
          | (.projections[$projection] | length)] | add
    ' \
        "${script_dir}/../config/controller-connections.json"
)"
if [ "${count}" -ne "${expected_count}" ]; then
    echo "Refusing incomplete controller env (${count}/${expected_count} vars)." >&2
    exit 1
fi
install_if_changed "${temporary}" "${controller_target}" 0 0

if [ "${any_changed}" -eq 0 ]; then
    echo "Severino HQ secrets are current."
    exit 0
fi

if [ "${web_changed}" -eq 1 ] && docker inspect severino-hq >/dev/null 2>&1; then
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

if [ "${web_changed}" -eq 1 ]; then
    echo "Refreshed secrets; Severino HQ is not currently installed."
else
    echo "Refreshed controller-only secrets without restarting Severino HQ."
fi
