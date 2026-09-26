# shellcheck shell=sh
# Private controller files must never share the web container's writable mount.
readonly controller_runtime_dir="${SEVERINO_CONTROLLER_SECRET_DIR:-/run/severino-hq-secrets}"
readonly controller_env="${controller_runtime_dir}/severino_controller_env"

case "${controller_runtime_dir}" in
    /run/severino-hq|/run/severino-hq/*|*//*|*/../*|*/./*|*/..|*/.|*/)
        echo "Unsafe controller secret directory." >&2; exit 1 ;;
    /*) ;;
    *) echo "Controller secret directory must be absolute." >&2; exit 1 ;;
esac
if [ "${SEVERINO_CONTROLLER_ENV:-${controller_env}}" != "${controller_env}" ]; then
    echo "Remove SEVERINO_CONTROLLER_ENV; configure SEVERINO_CONTROLLER_SECRET_DIR consistently instead." >&2
    exit 1
fi

controller_require_directory() {
    if [ -L "${controller_runtime_dir}" ] ||
        [ "$(stat -c '%u:%a' "${controller_runtime_dir}")" != '0:700' ]; then
        echo "Controller secret directory must be root-owned with mode 0700." >&2
        exit 1
    fi
    # Every mount in the chain, not a string comparison against the whole
    # output. `findmnt --target` prints one line per mount at or above the path,
    # so a directory given a mount of its own reads as "tmpfs tmpfs", and
    # comparing that to the literal "tmpfs" refuses a path that is strictly
    # better protected than a plain directory under /run.
    #
    # Empty output fails too. findmnt writes its errors to stderr and prints
    # nothing on stdout, so treating empty as acceptable would let a probe that
    # could not answer stand in for an answer of yes.
    fstypes="$(findmnt -n -o FSTYPE --target "${controller_runtime_dir}" 2>/dev/null || true)"
    if [ -z "${fstypes}" ] || printf '%s\n' "${fstypes}" | grep -qv '^tmpfs$'; then
        echo "Controller secret directory must be on tmpfs." >&2
        exit 1
    fi
    # The innermost mount must carry `noswap`: a plain tmpfs pages its
    # contents out to swap.
    options="$(findmnt -n -r -o TARGET,OPTIONS --target "${controller_runtime_dir}" 2>/dev/null |
        awk 'length($1) > longest { longest = length($1); options = $2 } END { print options }' || true)"
    case ",${options}," in
        *,noswap,*) ;;
        *) echo "Controller secret directory must be a tmpfs mounted with noswap." >&2
           exit 1 ;;
    esac
}

# Serialize access to the rendered SSH identities, so a reader never sees a
# mix of two generations. $1 is `shared` for readers, `exclusive` for the
# renderer. The lock is held on fd 8 until the process exits.
controller_ssh_lock() {
    case "$1" in
        shared) _flag=-s ;;
        exclusive) _flag=-x ;;
        *) echo "Unknown lock mode." >&2; exit 1 ;;
    esac
    exec 8>>"${controller_runtime_dir}/ssh.lock"
    flock "${_flag}" -w 60 8 || { echo "Timed out waiting for the SSH identity lock." >&2; exit 1; }
}

controller_require_environment() {
    controller_require_directory
    if [ -L "${controller_env}" ] || [ ! -f "${controller_env}" ] || [ ! -s "${controller_env}" ] ||
        [ "$(stat -c '%u:%a' "${controller_env}")" != '0:400' ]; then
        echo "Controller environment must be a nonempty root-owned file with mode 0400." >&2
        exit 1
    fi
}

# The env prefix of every connection in a rendered controller environment
# ($1), one per line: only validated variable-name tokens, never a value.
controller_connection_prefixes() {
    sed -nE 's/^([A-Z][A-Z0-9_]*)_CONNECTION_REF=.*/\1/p' "$1"
}
