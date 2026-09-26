#!/bin/sh
# Refresh Severino HQ secrets from 1Password without exposing them:
#   - the full app environment  -> secrets/severino_hq_env
#   - controller providers      -> private tmpfs directory
# All are root-rendered. Web secrets become container-owned read-only mounts;
# controller secrets remain root-owned and never enter the web container.

set -eu

# Vault and item names are host configuration.
readonly vault="${SEVERINO_SECRETS_VAULT:?vault is required}"
readonly env_item="${SEVERINO_ENV_ITEM:?environment item is required}"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
readonly secret_dir="${SEVERINO_HQ_SECRET_DIR:-/opt/apps/severino-hq/secrets}"
# Nothing accepts an MCP token file, so none is kept.
readonly mcp_token_file="${secret_dir}/severino_mcp_token"
readonly env_target="${secret_dir}/severino_hq_env"
# Controller identities live on the tmpfs mount, never on this disk.
readonly legacy_ssh_dir="${secret_dir}/ssh"
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
# Private keys on disk outlive every rotation. Removing them is the operator's
# decision; refusing keeps the refresh from reporting a clean state meanwhile.
if [ -d "${legacy_ssh_dir}" ] &&
    grep -rlqs -e '-----BEGIN [A-Z ]*PRIVATE KEY-----' "${legacy_ssh_dir}"; then
    echo "Refusing: private keys remain in ${legacy_ssh_dir}. Remove them by hand; identities now come from the vault." >&2
    exit 1
fi
secrets_stage "${controller_runtime_dir}"

secrets_backend_select "${vault}" "${script_dir}"

any_changed=0
web_changed=0
installed_change=0

# App environment: every UPPER_SNAKE field on the env item
temporary="${staging}/app"
op item get "${env_item}" --vault "${vault}" --format json >"${staging}/app.json"
jq -r -f "${script_dir}/render-env.jq" "${staging}/app.json" >"${temporary}"
secrets_validate_render "${temporary}"
count="$(grep -c . "${temporary}" || true)"
if [ "${count}" -lt 15 ]; then
    echo "Refusing suspiciously small app env (${count} vars) from 1Password." >&2
    exit 1
fi

# Controller-only provider environment.
temporary="${staging}/controller"
"${script_dir}/render-controller-env.sh" "${vault}" >"${temporary}"
# The renderer validates each connection; an empty inventory is also an error.
secrets_validate_render "${temporary}"
connections="$(grep -c '_CONNECTION_REF=' "${temporary}" || true)"
if [ "${connections}" -eq 0 ]; then
    echo "Refusing a controller env that resolved no connections." >&2
    exit 1
fi

# SSH identities for the connections that open a shell. Each connection names
# an SSH key item in the same vault (`identity`); its private half and public
# half are rendered here, beside the environment, with the host key pinned from
# the connection. Nothing is generated on the host and nothing reaches a disk.
identities="${staging}/ssh"
mkdir -m 700 "${identities}"
: >"${identities}/known_hosts"
# Read in a subshell: sourcing the rendered file there cannot change anything
# this script later relies on.
(
    set -a
    # shellcheck disable=SC1090  # rendered above, shell-quoted by the renderer
    . "${temporary}"
    controller_connection_prefixes "${temporary}" | while IFS= read -r prefix; do
        eval "identity=\${${prefix}_IDENTITY:-}"
        [ -n "${identity}" ] || continue
        eval "printf '%s\t%s\t%s\t%s\t%s\n' \
            \"\${${prefix}_CONNECTION_REF}\" \"\${identity}\" \
            \"\${${prefix}_HOST}\" \"\${${prefix}_PORT}\" \"\${${prefix}_HOST_KEY}\""
    done
) >"${staging}/identities.tsv"
tab="$(printf '\t')"
# Exactly five fields per line: a tab inside any value would shift the rest.
if awk -F '\t' 'NF != 5 { bad = 1 } END { exit !bad }' "${staging}/identities.tsv"; then
    echo "Refusing an SSH connection whose fields contain a tab." >&2
    exit 1
fi
while IFS="${tab}" read -r ref identity host port host_key; do
    case "${ref}" in
        known_hosts | *.pub)
            echo "Connection ${ref} has a name its identity files would collide with." >&2
            exit 1 ;;
    esac
    case "${ref}" in
        '' | .* | */*)
            echo "Connection ${ref} has an invalid name." >&2
            exit 1 ;;
    esac
    case "${identity}" in
        '' | */* | op:*)
            echo "Connection ${ref} names an invalid identity item." >&2
            exit 1 ;;
    esac
    case "${host}" in
        '' | *[!A-Za-z0-9.:-]*)
            echo "Connection ${ref} has an invalid host." >&2
            exit 1 ;;
    esac
    case "${port}" in
        '' | *[!0-9]*)
            echo "Connection ${ref} has an invalid port." >&2
            exit 1 ;;
    esac
    case "${host_key}" in
        "ssh-ed25519 "*) ;;
        *) echo "Connection ${ref} must pin an ssh-ed25519 host key." >&2; exit 1 ;;
    esac
    op read "op://${vault}/${identity}/private key?ssh-format=openssh" \
        </dev/null >"${identities}/${ref}"
    op read "op://${vault}/${identity}/public key" </dev/null >"${identities}/${ref}.pub"
    chmod 400 "${identities}/${ref}"
    chmod 444 "${identities}/${ref}.pub"
    # The two halves must be one key: a mismatched item would otherwise ship a
    # public key to the target that no private key here can use.
    if [ "$(ssh-keygen -y -f "${identities}/${ref}" </dev/null | cut -d' ' -f1,2)" != \
        "$(cut -d' ' -f1,2 "${identities}/${ref}.pub")" ]; then
        echo "Connection ${ref}: the identity's private and public halves do not match." >&2
        exit 1
    fi
    printf '[%s]:%s %s\n' "${host}" "${port}" "${host_key}" >>"${identities}/known_hosts"
done <"${staging}/identities.tsv"
chmod 444 "${identities}/known_hosts"
# Retrieval and validation complete before any live file is modified. Existing
# bind mounts require in-place updates; this is not a multi-file transaction.
rm -f "${mcp_token_file}"
secrets_install_if_changed "${staging}/app" "${env_target}" 10001 10001
web_changed="${installed_change}"
# The controller environment and its identities change as one generation:
# readers hold the shared lock while they read either.
controller_ssh_lock exclusive
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

# Install each identity file by renaming it over the old one on the same
# tmpfs, then drop the files of connections that no longer exist.
live_ssh="${controller_runtime_dir}/ssh"
[ -d "${live_ssh}" ] || mkdir -m 700 "${live_ssh}"
wanted="$(cd "${identities}" && ls -A)"
for name in ${wanted}; do
    if [ ! -f "${live_ssh}/${name}" ] || ! cmp -s "${identities}/${name}" "${live_ssh}/${name}"; then
        mv -f "${identities}/${name}" "${live_ssh}/${name}"
        any_changed=1
    fi
done
for installed in "${live_ssh}"/* "${live_ssh}"/.[!.]*; do
    [ -e "${installed}" ] || continue
    if ! printf '%s\n' "${wanted}" | grep -qxF "$(basename "${installed}")"; then
        rm -rf "${installed}"
        any_changed=1
    fi
done
exec 8>&-

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
