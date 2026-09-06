#!/bin/sh
# Prove failed deployments restore the previously active controller timers, and
# that the script refuses the inputs a sudoers rule would otherwise let a caller
# choose.

set -eu

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
readonly repo_dir
work_dir="$(mktemp -d)"
readonly work_dir
trap 'rm -rf "${work_dir}"' EXIT HUP INT TERM
readonly bin_dir="${work_dir}/bin"
readonly app_dir="${work_dir}/app"
readonly lib_dir="${work_dir}/lib"
readonly log_file="${work_dir}/calls.log"
mkdir -p "${bin_dir}" "${app_dir}/scripts" "${lib_dir}/scripts"

# A digest-pinned reference under a test prefix. The script accepts only its own
# composition by default, so the prefix is overridden rather than the guard
# loosened -- the guard is one of the things under test.
readonly test_prefix="registry.example/hq/composition@sha256:"
readonly good_image="${test_prefix}0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

cat >"${bin_dir}/docker" <<'EOF'
#!/bin/sh
if [ "$1" = "inspect" ]; then
    case "$2" in
        --format)
            case "$3" in
                *Config.Image*) echo "registry.example/hq/composition@sha256:previous" ;;
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
echo "docker $*" >>"${TEST_LOG}"
exit 0
EOF

# The script now runs as root and calls these directly rather than through sudo.
cat >"${bin_dir}/systemctl" <<'EOF'
#!/bin/sh
echo "systemctl $*" >>"${TEST_LOG}"
exit 0
EOF

# Root is asserted with `id -u`; stubbing it keeps the real guard in the script
# rather than adding an escape hatch that would also exist in production.
cat >"${bin_dir}/id" <<'EOF'
#!/bin/sh
if [ "$1" = "-u" ]; then echo "${TEST_UID:-0}"; exit 0; fi
exit 0
EOF

cat >"${bin_dir}/install" <<'EOF'
#!/bin/sh
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

# Controller activation fails, which is what drives the rollback path.
cat >"${lib_dir}/scripts/install-controller.sh" <<'EOF'
#!/bin/sh
exit 1
EOF
cat >"${lib_dir}/scripts/run-private.sh" <<'EOF'
#!/bin/sh
exit 0
EOF

chmod +x "${bin_dir}/docker" "${bin_dir}/systemctl" "${bin_dir}/id" \
    "${bin_dir}/install" "${bin_dir}/df" "${bin_dir}/sleep" \
    "${lib_dir}/scripts/install-controller.sh" "${lib_dir}/scripts/run-private.sh"

deploy() {
    PATH="${bin_dir}:${PATH}" \
        TEST_LOG="${log_file}" \
        TEST_UID="${TEST_UID:-0}" \
        TEST_PULL_FAIL="${1}" \
        SEVERINO_HQ_APP_DIR="${app_dir}" \
        SEVERINO_HQ_LIB_DIR="${lib_dir}" \
        SEVERINO_HQ_IMAGE_PREFIX="${test_prefix}" \
        "${repo_dir}/scripts/deploy-image.sh" "${2:-${good_image}}" </dev/null
}

run_failure() {
    : >"${log_file}"
    if deploy "${1}"; then
        echo "Expected deployment to fail." >&2
        exit 1
    fi
    grep -q "systemctl start severino-hq-controller.timer" "${log_file}"
    grep -q "systemctl start severino-hq-content-sync.timer" "${log_file}"
}

refuses() {
    : >"${log_file}"
    if deploy 0 "${1}" 2>/dev/null; then
        echo "Expected ${1} to be refused." >&2
        exit 1
    fi
    if [ -s "${log_file}" ]; then
        echo "Refused input reached docker: ${1}" >&2
        exit 1
    fi
}

# A failed image pull leaves the old app running and restores timer state.
run_failure 1

# A failed controller activation rolls back the image and restores timer state.
run_failure 0
grep -q "image=registry.example/hq/composition@sha256:previous" "${log_file}"

# The argument decides what runs as a container, and a sudoers rule lets the
# caller choose it, so each of these must stop before anything is pulled.
refuses "registry.example/hq/composition:latest"
refuses "docker.io/somebody/else@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
refuses "${test_prefix}short"
refuses "${test_prefix}zzzz456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

# Root is not optional: the script acts on root-owned paths and expects to be
# the one holding the privilege, not to acquire it partway through.
: >"${log_file}"
if TEST_UID=1000 deploy 0; then
    echo "Expected a non-root run to be refused." >&2
    exit 1
fi

echo "deploy-image rollback and input-guard tests passed"
