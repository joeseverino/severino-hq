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
    if [ "$(findmnt -n -o FSTYPE --target "${controller_runtime_dir}")" != tmpfs ]; then
        echo "Controller secret directory must be on tmpfs." >&2
        exit 1
    fi
}

controller_require_environment() {
    controller_require_directory
    if [ -L "${controller_env}" ] || [ ! -f "${controller_env}" ] || [ ! -s "${controller_env}" ] ||
        [ "$(stat -c '%u:%a' "${controller_env}")" != '0:400' ]; then
        echo "Controller environment must be a nonempty root-owned file with mode 0400." >&2
        exit 1
    fi
}
