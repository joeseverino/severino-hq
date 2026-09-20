#!/bin/sh
# Refresh Severino HQ secrets from 1Password without exposing them:
#   - the MCP validator token   -> secrets/severino_mcp_token
#   - the full app environment  -> secrets/severino_hq_env
#   - controller providers      -> secrets/severino_controller_env
# All are root-rendered. Web secrets become container-owned read-only mounts;
# controller secrets remain root-owned and never enter the web container.

set -eu

# Supplied by the host, because it differs per machine. Required rather than
# defaulted: a default is a wrong value waiting to be used silently on a host
# that was never configured.
readonly vault="${SEVERINO_SECRETS_VAULT:?vault is required}"
readonly mcp_ref="${SEVERINO_MCP_SECRET_REF:?MCP secret reference is required}"
readonly env_item="${SEVERINO_ENV_ITEM:?environment item is required}"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
readonly secret_dir="${SEVERINO_HQ_SECRET_DIR:-/opt/apps/severino-hq/secrets}"
readonly mcp_target="${secret_dir}/severino_mcp_token"
readonly env_target="${secret_dir}/severino_hq_env"
# Rendered into tmpfs, not onto the disk. This file carries every provider
# credential the controller uses, and a copy under /opt/apps would sit in every
# disk image and backup of this host for as long as the host exists. /run is
# cleared on boot, and the renderer puts it back before the controller starts.
#
# Overridable so a host that has not migrated yet keeps working.
readonly controller_target="${SEVERINO_CONTROLLER_ENV:-/run/severino-hq/severino_controller_env}"

# shellcheck source=scripts/lib/secrets.sh
. "${script_dir}/lib/secrets.sh"

umask 077
install -d -m 700 -o root -g root "${secret_dir}"
# /run is empty after a boot, so the directory has to exist before the render.
install -d -m 700 -o root -g root "$(dirname "${controller_target}")"
secrets_lock "${secret_dir}"
secrets_stage "${secret_dir}"

secrets_backend_select "${vault}" "${script_dir}"

any_changed=0
web_changed=0
installed_change=0

install_if_changed() { secrets_install_if_changed "$@"; }

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

# Controller-only provider environment. This file is intentionally not mounted
# into the web container; run-controller.sh forwards it only to controller exec.
temporary="${staging}/controller"
"${script_dir}/render-controller-env.sh" "${vault}" >"${temporary}"
# Completeness is the renderer's to guarantee, not this script's to recount.
# The registry no longer lists the connections -- the vault does -- so there is
# no number here to compare against, and the renderer exits non-zero on any
# value a projection requires and an item does not carry. What is left to check
# is that it produced an environment at all: a run that resolved nothing exits
# zero with an empty file, and installing that would take every provider away.
connections="$(grep -c '_CONNECTION_REF=' "${temporary}" || true)"
if [ "${connections}" -eq 0 ]; then
    echo "Refusing a controller env that resolved no connections." >&2
    exit 1
fi
# Retrieval and validation complete before any live file is modified. Existing
# bind mounts require in-place updates; this is not a multi-file transaction.
install_if_changed "${staging}/mcp" "${mcp_target}" 10001 10001
web_changed="${installed_change}"
install_if_changed "${staging}/app" "${env_target}" 10001 10001
if [ "${installed_change}" -eq 1 ]; then web_changed=1; fi
install_if_changed "${staging}/controller" "${controller_target}" 0 0

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
