# shellcheck shell=sh
# Private controller files must never share the web container's writable mount.
readonly controller_runtime_dir="${SEVERINO_CONTROLLER_SECRET_DIR:-/run/severino-hq-secrets}"
# The connections document hq-secrets renders: every provider connection the
# controller may open, as one JSON file. It is mounted into the controller
# container read-only; nothing in it is ever passed as an environment variable.
readonly controller_connections="${controller_runtime_dir}/controller-connections.json"

case "${controller_runtime_dir}" in
    /run/severino-hq|/run/severino-hq/*|*//*|*/../*|*/./*|*/..|*/.|*/)
        echo "Unsafe controller secret directory." >&2; exit 1 ;;
    /*) ;;
    *) echo "Controller secret directory must be absolute." >&2; exit 1 ;;
esac
if [ -n "${SEVERINO_CONTROLLER_ENV:-}" ]; then
    echo "Remove SEVERINO_CONTROLLER_ENV; configure SEVERINO_CONTROLLER_SECRET_DIR consistently instead." >&2
    exit 1
fi

# The connections document, and the directory it is in, as the renderer leaves
# them: a root-only tmpfs that never pages to disk, holding a root-only file.
controller_require_connections() {
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
    if [ -L "${controller_connections}" ] || [ ! -f "${controller_connections}" ] ||
        [ ! -s "${controller_connections}" ] ||
        [ "$(stat -c '%u:%a' "${controller_connections}")" != '0:400' ]; then
        echo "Controller connections must be a nonempty root-owned file with mode 0400." >&2
        exit 1
    fi
}

# Serialize access to the connections document and the rendered SSH
# identities, so a reader never sees a mix of two generations. $1 is `shared`
# for readers; the renderer (hq-secrets) holds the same lock exclusively. The
# lock is held on fd 8 until the process exits.
controller_ssh_lock() {
    case "$1" in
        shared) _flag=-s ;;
        exclusive) _flag=-x ;;
        *) echo "Unknown lock mode." >&2; exit 1 ;;
    esac
    exec 8>>"${controller_runtime_dir}/ssh.lock"
    flock "${_flag}" -w 60 8 || { echo "Timed out waiting for the SSH identity lock." >&2; exit 1; }
}
