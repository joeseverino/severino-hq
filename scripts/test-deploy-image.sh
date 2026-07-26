#!/bin/sh
# Prove failed deployments restore the previously active controller timers.

set -eu

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
readonly repo_dir
work_dir="$(mktemp -d)"
readonly work_dir
trap 'rm -rf "${work_dir}"' EXIT HUP INT TERM
readonly bin_dir="${work_dir}/bin"
readonly app_dir="${work_dir}/app"
readonly log_file="${work_dir}/calls.log"
mkdir -p "${bin_dir}" "${app_dir}/scripts"

cat >"${bin_dir}/docker" <<'EOF'
#!/bin/sh
if [ "$1" = "inspect" ]; then
    case "$2" in
        --format)
            case "$3" in
                *Config.Image*) echo "registry.example/hq:previous" ;;
                *Health.Status*) echo "healthy" ;;
            esac
            ;;
    esac
    exit 0
fi
if [ "$1" = "compose" ]; then
    echo "docker $* image=${SEVERINO_IMAGE:-}" >>"${TEST_LOG}"
    if [ "$2" = "pull" ] && [ "${TEST_PULL_FAIL:-0}" -eq 1 ]; then
        exit 1
    fi
    exit 0
fi
exit 0
EOF

cat >"${bin_dir}/sudo" <<'EOF'
#!/bin/sh
echo "sudo $*" >>"${TEST_LOG}"
if [ "$1" = "systemctl" ]; then
    exit 0
fi
case "$1" in
    */install-controller.sh) exit 1 ;;
esac
exit 0
EOF

cat >"${bin_dir}/df" <<'EOF'
#!/bin/sh
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
printf '/dev/test 1000000 1 999999 1%% /\n'
EOF

cat >"${bin_dir}/sleep" <<'EOF'
#!/bin/sh
exit 0
EOF

chmod +x "${bin_dir}/docker" "${bin_dir}/sudo" "${bin_dir}/df" "${bin_dir}/sleep"

run_failure() {
    : >"${log_file}"
    if PATH="${bin_dir}:${PATH}" \
        TEST_LOG="${log_file}" \
        TEST_PULL_FAIL="${1}" \
        SEVERINO_HQ_APP_DIR="${app_dir}" \
        "${repo_dir}/scripts/deploy-image.sh" registry.example/hq:new; then
        echo "Expected deployment to fail." >&2
        exit 1
    fi
    grep -q "sudo systemctl start severino-hq-controller.timer" "${log_file}"
    grep -q "sudo systemctl start severino-hq-content-sync.timer" "${log_file}"
}

# A failed image pull leaves the old app running and restores timer state.
run_failure 1

# A failed controller activation rolls back the image and restores timer state.
run_failure 0
grep -q "image=registry.example/hq:previous" "${log_file}"

echo "deploy-image rollback tests passed"
