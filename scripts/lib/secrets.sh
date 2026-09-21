# shellcheck shell=sh
# Shared secret-rendering engine. Source this; do not execute it.
#
# Contract:
#   secrets_backend_select <vault> <script_dir>   authenticate op, or exit
#   secrets_lock <dir>                            refuse a concurrent run
#   secrets_stage <dir>                           sets `staging`, auto-removed
#   secrets_install_if_changed <src> <dst> <uid> <gid> [mode]
#   secrets_validate_render <file>               refuse a render that resolved badly
#
# secrets_install_if_changed sets `installed_change` to 1 when it wrote, and
# `any_changed` to 1 if anything has been written this run. Callers read those
# to decide what to restart.

# Clear the other backend's authentication: Connect takes precedence in `op`.
secrets_backend_select() {
    _vault="$1"
    _script_dir="$2"

    case "${SEVERINO_SECRETS_BACKEND:?Secrets backend must be explicitly configured}" in
        connect)
            unset OP_SERVICE_ACCOUNT_TOKEN
            : "${OP_CONNECT_HOST:?Connect endpoint is required}"
            # Each consumer has a distinct credential name.
            _name="${SEVERINO_CONNECT_CREDENTIAL:?Connect credential name is required}"
            OP_CONNECT_TOKEN="$(cat "${CREDENTIALS_DIRECTORY:?}/${_name}")"
            [ -n "${OP_CONNECT_TOKEN}" ] || {
                echo "Empty Connect token." >&2; exit 1; }
            export OP_CONNECT_TOKEN
            # Prove the endpoint and the token before `op` can send anything to
            # whatever is actually listening on that port.
            sh "${_script_dir}/list-secret-items.sh" "${_vault}" >/dev/null
            ;;
        service-account)
            unset OP_CONNECT_HOST OP_CONNECT_TOKEN
            _name="${SEVERINO_SERVICE_ACCOUNT_CREDENTIAL:-op_service_account_token}"
            OP_SERVICE_ACCOUNT_TOKEN="$(cat "${CREDENTIALS_DIRECTORY:?}/${_name}")"
            [ -n "${OP_SERVICE_ACCOUNT_TOKEN}" ] || {
                echo "Empty reader credential." >&2; exit 1; }
            export OP_SERVICE_ACCOUNT_TOKEN
            ;;
        *)
            echo "Unknown secrets backend." >&2
            exit 1
            ;;
    esac
}

# Prevent concurrent installations.
secrets_lock() {
    exec 9>"$1/.refresh.lock"
    flock -n 9 || { echo "Secret refresh is already running." >&2; exit 1; }
}

# Call directly, not in command substitution: the cleanup trap owns `staging`.
secrets_stage() {
    staging="$(mktemp -d "$1/.refresh.XXXXXX")"
    # Only this invocation's directory, so a concurrent or crashed run's files
    # are never collected by someone else's cleanup.
    trap 'rm -rf "${staging}"' EXIT
    trap 'exit 1' HUP INT TERM
}

# Preserve existing inodes for Docker file bind mounts. Copying is not atomic.
# shellcheck disable=SC2034
secrets_install_if_changed() {
    installed_change=0
    _src="$1"; _dst="$2"; _uid="$3"; _gid="$4"; _mode="${5:-400}"

    chown "${_uid}:${_gid}" "${_src}"
    chmod "${_mode}" "${_src}"

    if [ -f "${_dst}" ] && cmp -s "${_src}" "${_dst}"; then
        rm -f "${_src}"
        return 0
    fi
    if [ -f "${_dst}" ]; then
        cat "${_src}" >"${_dst}"
        chown "${_uid}:${_gid}" "${_dst}"
        chmod "${_mode}" "${_dst}"
        rm -f "${_src}"
    else
        mv "${_src}" "${_dst}"
    fi
    installed_change=1
    any_changed=1
}

# Reject empty values and unresolved references.
secrets_validate_render() {
    _file="$1"
    [ -s "${_file}" ] || { echo "Refusing an empty render." >&2; exit 1; }
    if grep -q 'op://' "${_file}"; then
        echo "Refusing: an unresolved reference survived injection." >&2
        exit 1
    fi
    if grep -qE '^[A-Za-z_][A-Za-z0-9_]*=$' "${_file}"; then
        echo "Refusing: a variable rendered empty." >&2
        exit 1
    fi
}
