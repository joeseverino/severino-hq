# shellcheck shell=sh
# Shared secret-rendering engine. Source this; do not execute it.
#
# Rendering secrets is the same shape everywhere: choose a backend, load one
# credential, take a lock, resolve into a staging directory, validate, install.
# Implementing that once means a fix reaches every consumer, rather than the
# copy whose author remembered it.
#
# A consumer supplies only what is its own: which vault, which items, and where
# the results go.
#
# POSIX sh on purpose. These run under dash on the hosts, and a bashism here
# fails in production rather than in a test.
#
# Contract:
#   secrets_backend_select <vault> <script_dir>   authenticate op, or exit
#   secrets_lock <dir>                            refuse a concurrent run
#   secrets_stage <dir>                           sets `staging`, auto-removed
#   secrets_install_if_changed <src> <dst> <uid> <gid> [mode]
#
# secrets_install_if_changed sets `installed_change` to 1 when it wrote, and
# `any_changed` to 1 if anything has been written this run. Callers read those
# to decide what to restart.

# Authenticate `op` for exactly one backend, and make the other impossible.
#
# The two are mutually exclusive by more than convention: OP_CONNECT_HOST and
# OP_CONNECT_TOKEN take precedence over OP_SERVICE_ACCOUNT_TOKEN inside `op`
# wherever both are visible. A process that inherited Connect variables and was
# handed a service-account token would quietly use Connect -- and Connect is
# read-only, so a writer would fail in a way that reads like a permissions
# problem. Unsetting the other side is the fix, and it belongs here rather than
# in each caller's memory.
secrets_backend_select() {
    _vault="$1"
    _script_dir="$2"

    case "${SEVERINO_SECRETS_BACKEND:-service-account}" in
        connect)
            unset OP_SERVICE_ACCOUNT_TOKEN
            : "${OP_CONNECT_HOST:?Connect endpoint is required}"
            # Named for the consumer holding it, never for the protocol: a host
            # may run several renderers, and a shared credential name clobbers.
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

# Refuse to run twice at once. Two renderers interleaving their installs is how
# a host ends up with files from two different reads of the vault.
secrets_lock() {
    exec 9>"$1/.refresh.lock"
    flock -n 9 || { echo "Secret refresh is already running." >&2; exit 1; }
}

# A private directory for this run, left in `staging`. Everything is resolved
# and validated there before an installed file is touched, so a retrieval or
# validation failure leaves every previous file exactly as it was.
#
# Sets the variable rather than printing it: a caller using `$( )` would run
# this in a subshell, and the cleanup trap would be armed in a shell that exits
# immediately -- leaving the staging directory behind on every run.
secrets_stage() {
    staging="$(mktemp -d "$1/.refresh.XXXXXX")"
    # Only this invocation's directory, so a concurrent or crashed run's files
    # are never collected by someone else's cleanup.
    trap 'rm -rf "${staging}"' EXIT
    trap 'exit 1' HUP INT TERM
}

# Install a staged file, preserving the inode when one already exists.
#
# That matters for single-file bind mounts: replacing the inode leaves a running
# container holding the old file forever. It also means this is *not* an atomic
# swap -- interruption during the copy can leave a partial file. Say so rather
# than calling it atomic rotation; a generation-directory layout is what would
# make it truly transactional, and that requires moving the mounts first.
# installed_change and any_changed are the documented return channel, read by
# callers to decide what to restart.
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

# Reject a render that resolved to nothing useful. Each of these has happened
# somewhere: a renamed item resolving to an empty value, a template whose
# references silently survived, a variable that rendered blank and would have
# disabled the authentication it was meant to configure.
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
