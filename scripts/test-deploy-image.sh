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
readonly run_dir="${work_dir}/run"
mkdir -p "${bin_dir}" "${app_dir}/scripts" "${lib_dir}/scripts" "${run_dir}"

# A digest-pinned reference under a test prefix. The script accepts only its own
# composition by default, so the prefix is overridden rather than the guard
# loosened -- the guard is one of the things under test.
readonly test_prefix="registry.example/hq/composition@sha256:"
readonly good_image="${test_prefix}0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

# The compose file each `compose` call ran under is logged by content rather than
# by path: the staged copies are gone by the time the assertions run, and the
# content is the thing that has to be right.
cat >"${bin_dir}/docker" <<'EOF'
#!/bin/sh
if [ "$1" = "inspect" ]; then
    case "$2" in
        --format)
            case "$3" in
                *Config.Image*) echo "registry.example/hq/composition@sha256:previous" ;;
                *Health.Status*) echo "${TEST_HEALTH:-healthy}" ;;
            esac
            ;;
    esac
    exit 0
fi
if [ "$1" = "create" ]; then
    echo "docker $*" >>"${TEST_LOG}"
    echo "test-compose-container"
    exit 0
fi
if [ "$1" = "cp" ]; then
    echo "docker $*" >>"${TEST_LOG}"
    [ "$2" = "test-compose-container:/app/docker-compose.yml" ] || exit 1
    cat "${TEST_IMAGE_COMPOSE}" >"$3"
    exit 0
fi
if [ "$1" = "compose" ]; then
    file=""
    previous=""
    for argument do
        [ "${previous}" = "-f" ] && file="${argument}"
        previous="${argument}"
    done
    echo "docker $* image=${SEVERINO_IMAGE:-} DOCKER_CONFIG=${DOCKER_CONFIG:-} compose=$(cat "${file}")" >>"${TEST_LOG}"
    for argument do
        if [ "${argument}" = pull ] && [ "${TEST_PULL_FAIL:-0}" -eq 1 ]; then
            exit 1
        fi
    done
    exit 0
fi
echo "docker $* DOCKER_CONFIG=${DOCKER_CONFIG:-}" >>"${TEST_LOG}"
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

# Root's own verifier, at the absolute path the script insists on. Stubbed so
# both answers are reachable: a test that can only ever observe "verified" would
# prove nothing about the refusal.
readonly verifier_dir="${work_dir}/verifier"
mkdir -p "${verifier_dir}"
cat >"${verifier_dir}/cosign" <<'EOF'
#!/bin/sh
echo "cosign $*" >>"${TEST_LOG}"
exit "${TEST_VERIFY_FAIL:-0}"
EOF
chmod +x "${verifier_dir}/cosign"

cat >"${bin_dir}/df" <<'EOF'
#!/bin/sh
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
printf '/dev/test 1000000 1 999999 1%% /\n'
EOF

cat >"${bin_dir}/sleep" <<'EOF'
#!/bin/sh
exit 0
EOF

# Controller activation fails unless a case says otherwise, which is what drives
# the rollback path. Like the real one, it refreshes the lib tree -- compose file
# included -- from the image now running.
cat >"${lib_dir}/scripts/install-controller.sh" <<'EOF'
#!/bin/sh
printf 'updated scripts\n' >"${SEVERINO_HQ_LIB_DIR}/version"
cp "${TEST_IMAGE_COMPOSE}" "${SEVERINO_HQ_LIB_DIR}/docker-compose.yml"
exit "${TEST_CONTROLLER_FAIL:-1}"
EOF
printf 'previous scripts\n' >"${lib_dir}/version"

# The running release's compose file, and the one the new image carries.
printf 'previous compose\n' >"${lib_dir}/docker-compose.yml"
readonly image_compose="${work_dir}/image-compose.yml"
printf 'next compose\n' >"${image_compose}"
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
        SEVERINO_HQ_RUN_DIR="${run_dir}" \
        SEVERINO_HQ_VERIFIER_DIR="${verifier_dir}" \
        SEVERINO_HQ_IMAGE_PREFIX="${test_prefix}" \
        TEST_VERIFY_FAIL="${TEST_VERIFY_FAIL:-0}" \
        TEST_IMAGE_COMPOSE="${image_compose}" \
        "${repo_dir}/scripts/deploy-image.sh" "${2:-${good_image}}" </dev/null
}

# Staged compose files and the container they were copied out of are gone,
# whichever way the run ended.
assert_staging_cleaned() {
    if find "${run_dir}" -name 'severino-hq-compose.*' | grep -q .; then
        echo "Staged compose files outlived the deploy." >&2
        exit 1
    fi
    if grep -q "docker create" "${log_file}" \
        && ! grep -q "docker rm -f test-compose-container" "${log_file}"; then
        echo "The container the compose file was copied from was left behind." >&2
        exit 1
    fi
}

# The new release is started under the compose file its own image carries, not
# the previous release's copy in the lib tree -- which is only refreshed after
# the health check, so using it made every compose change land one deploy late.
assert_new_compose_applied() {
    if ! grep " up -d " "${log_file}" \
        | grep "image=${good_image} " | grep -q "compose=next compose$"; then
        echo "The new image was not started under its own compose file." >&2
        exit 1
    fi
    if ! grep -q "docker create --pull never ${good_image} true" "${log_file}"; then
        echo "The compose file was not read from the verified image by digest." >&2
        exit 1
    fi
}

# Rollback restores the previous compose file along with the previous image.
assert_previous_compose_restored() {
    if ! grep " up -d " "${log_file}" \
        | grep "image=registry.example/hq/composition@sha256:previous " \
        | grep -q "compose=previous compose$"; then
        echo "Rollback did not restore the previous compose file." >&2
        exit 1
    fi
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
assert_staging_cleaned
if grep -q "docker create" "${log_file}"; then
    echo "A compose file was read from an image that failed to pull." >&2
    exit 1
fi

# A failed controller activation rolls back the image and restores timer state.
run_failure 0
grep -q "image=registry.example/hq/composition@sha256:previous" "${log_file}"
grep -qx 'previous scripts' "${lib_dir}/version"
grep -qx 'previous compose' "${lib_dir}/docker-compose.yml"
if find "${run_dir}" -name 'severino-hq-scripts.*' | grep -q .; then
    echo "Controller rollback snapshot was not cleaned up." >&2
    exit 1
fi
assert_new_compose_applied
assert_previous_compose_restored
assert_staging_cleaned

# A compose change is live after one deploy: the new container is created under
# the new file, and the lib tree carries it afterwards for the next deploy.
: >"${log_file}"
TEST_CONTROLLER_FAIL=0
export TEST_CONTROLLER_FAIL
if ! deploy 0 >/dev/null; then
    echo "Expected a healthy deploy to succeed." >&2
    exit 1
fi
unset TEST_CONTROLLER_FAIL
assert_new_compose_applied
assert_staging_cleaned
if [ "$(grep -c " up -d " "${log_file}")" -ne 1 ]; then
    echo "A healthy deploy recreated the container more than once." >&2
    exit 1
fi
grep -qx 'next compose' "${lib_dir}/docker-compose.yml"
printf 'previous compose\n' >"${lib_dir}/docker-compose.yml"
printf 'previous scripts\n' >"${lib_dir}/version"

# A failed health check restores the prior compose file with the prior image,
# and never reaches controller activation, so the lib tree is untouched.
TEST_HEALTH=unhealthy
export TEST_HEALTH
run_failure 0 2>/dev/null >/dev/null
unset TEST_HEALTH
assert_new_compose_applied
assert_previous_compose_restored
assert_staging_cleaned
grep -qx 'previous compose' "${lib_dir}/docker-compose.yml"
grep -qx 'previous scripts' "${lib_dir}/version"

# The argument decides what runs as a container, and a sudoers rule lets the
# caller choose it, so each of these must stop before anything is pulled.
refuses "registry.example/hq/composition:latest"
refuses "docker.io/somebody/else@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
refuses "${test_prefix}short"
refuses "${test_prefix}zzzz456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

# Root is not optional: the script acts on root-owned paths and expects to be
# the one holding the privilege, not to acquire it partway through.
#
# Set and reset around the call rather than written as a `TEST_UID=1000 deploy`
# prefix: an assignment before a *function* persists after it returns, so the
# prefix form silently runs every later case as an unprivileged user, where they
# stop at this guard and prove nothing.
: >"${log_file}"
TEST_UID=1000
if deploy 0; then
    echo "Expected a non-root run to be refused." >&2
    exit 1
fi
TEST_UID=0

# The signature is what separates this repository's composition from anything
# else a stolen registry token could push under the same name, and root checks
# it for itself rather than inheriting the runner's word for it.
: >"${log_file}"
TEST_VERIFY_FAIL=1
export TEST_VERIFY_FAIL
if deploy 0 >/dev/null 2>&1; then
    echo "Expected an unsigned image to be refused." >&2
    exit 1
fi
TEST_VERIFY_FAIL=0
grep -q "cosign verify" "${log_file}"
if grep -q "docker compose pull" "${log_file}"; then
    echo "An unverified image was pulled anyway." >&2
    exit 1
fi

# An absent verifier is a refusal, not a silent downgrade to the shape guard.
mv "${verifier_dir}/cosign" "${verifier_dir}/cosign.hidden"
: >"${log_file}"
if deploy 0 2>/dev/null; then
    echo "Expected a missing verifier to be refused." >&2
    exit 1
fi
if [ -s "${log_file}" ]; then
    echo "A deploy without a verifier still reached docker." >&2
    exit 1
fi
mv "${verifier_dir}/cosign.hidden" "${verifier_dir}/cosign"

# A registry credential is written to a private config directory for the length
# of the run, never handed to `docker login`. Login stores it in root's own
# ~/.docker/config.json, where it outlives the deploy and every later root docker
# call reads it -- and where Docker warns, every single deploy, that it is
# sitting there in plaintext.
: >"${log_file}"
printf 'x-access-token\nsecret-token-value\n' | PATH="${bin_dir}:${PATH}" \
    TEST_LOG="${log_file}" \
    TEST_UID=0 \
    TEST_PULL_FAIL=1 \
    SEVERINO_HQ_APP_DIR="${app_dir}" \
    SEVERINO_HQ_LIB_DIR="${lib_dir}" \
    SEVERINO_HQ_RUN_DIR="${run_dir}" \
    SEVERINO_HQ_VERIFIER_DIR="${verifier_dir}" \
    SEVERINO_HQ_IMAGE_PREFIX="${test_prefix}" \
    TEST_IMAGE_COMPOSE="${image_compose}" \
    "${repo_dir}/scripts/deploy-image.sh" "${good_image}" >/dev/null 2>&1 || true
if grep -q "docker login" "${log_file}"; then
    echo "The deploy still logs in, which writes the token to root's home." >&2
    exit 1
fi
if ! grep -q "DOCKER_CONFIG=${run_dir}/" "${log_file}"; then
    echo "The pull did not read the ephemeral credential." >&2
    exit 1
fi
if grep -rl "secret-token-value" "${run_dir}" 2>/dev/null | grep -q .; then
    echo "The credential outlived the deploy." >&2
    exit 1
fi

echo "deploy-image rollback, compose and input-guard tests passed"
