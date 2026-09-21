#!/bin/sh
# Refresh Severino HQ secrets from 1Password without exposing them:
#   - the MCP validator token   -> secrets/severino_mcp_token
#   - the full app environment  -> secrets/severino_hq_env
#   - controller providers      -> private tmpfs directory
# All are root-rendered. Web secrets become container-owned read-only mounts;
# controller secrets remain root-owned and never enter the web container.

set -eu

# Vault and item names are host configuration.
readonly vault="${SEVERINO_SECRETS_VAULT:?vault is required}"
readonly mcp_ref="${SEVERINO_MCP_SECRET_REF:?MCP secret reference is required}"
readonly env_item="${SEVERINO_ENV_ITEM:?environment item is required}"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
readonly secret_dir="${SEVERINO_HQ_SECRET_DIR:-/opt/apps/severino-hq/secrets}"
readonly mcp_target="${secret_dir}/severino_mcp_token"
readonly env_target="${secret_dir}/severino_hq_env"
# shellcheck source=scripts/lib/controller-env.sh
. "${script_dir}/lib/controller-env.sh"

# shellcheck source=scripts/lib/secrets.sh
. "${script_dir}/lib/secrets.sh"

umask 077
install -d -m 700 -o root -g root "${secret_dir}"
[ ! -L "${controller_runtime_dir}" ] || { echo "Refusing a symlinked secret directory." >&2; exit 1; }
install -d -m 700 -o root -g root "${controller_runtime_dir}"
controller_require_directory
secrets_lock "${secret_dir}"
secrets_stage "${controller_runtime_dir}"

secrets_backend_select "${vault}" "${script_dir}"

any_changed=0
web_changed=0
installed_change=0

# MCP validator token
temporary="${staging}/mcp"
token="$(op read "${mcp_ref}")"
if [ "${#token}" -lt 32 ]; then
    echo "Refusing weak or empty MCP token from 1Password." >&2
    exit 1
fi
printf %s "${token}" >"${temporary}"

# App environment — every UPPER_SNAKE field on the env item
temporary="${staging}/app"
op item get "${env_item}" --vault "${vault}" --format json >"${staging}/app.json"
jq -r -f "${script_dir}/render-env.jq" "${staging}/app.json" >"${temporary}"
count="$(grep -c . "${temporary}" || true)"
if [ "${count}" -lt 15 ]; then
    echo "Refusing suspiciously small app env (${count} vars) from 1Password." >&2
    exit 1
fi

# Controller-only provider environment.
temporary="${staging}/controller"
"${script_dir}/render-controller-env.sh" "${vault}" >"${temporary}"
# The renderer validates each connection; an empty inventory is also an error.
connections="$(grep -c '_CONNECTION_REF=' "${temporary}" || true)"
if [ "${connections}" -eq 0 ]; then
    echo "Refusing a controller env that resolved no connections." >&2
    exit 1
fi
# Retrieval and validation complete before any live file is modified. Existing
# bind mounts require in-place updates; this is not a multi-file transaction.
secrets_install_if_changed "${staging}/mcp" "${mcp_target}" 10001 10001
web_changed="${installed_change}"
secrets_install_if_changed "${staging}/app" "${env_target}" 10001 10001
if [ "${installed_change}" -eq 1 ]; then web_changed=1; fi
# This file has no persistent bind mount: rename on the same tmpfs gives each
# reader a complete old or new environment.
if [ -L "${controller_env}" ] || [ ! -f "${controller_env}" ] ||
    ! cmp -s "${staging}/controller" "${controller_env}"; then
    chown 0:0 "${staging}/controller"
    chmod 400 "${staging}/controller"
    mv -f "${staging}/controller" "${controller_env}"
    any_changed=1
fi
chown 0:0 "${controller_env}"
chmod 400 "${controller_env}"
controller_require_environment

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
