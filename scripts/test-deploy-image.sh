#!/bin/sh
# Prove deployments hold and restore the units that start release work, and
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
readonly web_secret_dir="${work_dir}/tmpfs/web"
readonly lib_dir="${work_dir}/lib"
readonly log_file="${work_dir}/calls.log"
readonly run_dir="${work_dir}/run"
readonly sbin_dir="${work_dir}/sbin"
mkdir -p "${bin_dir}" "${app_dir}/scripts" "${lib_dir}/scripts" "${run_dir}" "${sbin_dir}"

# A digest-pinned reference under a test prefix. The script accepts only its own
# composition by default, so the prefix is overridden rather than the guard
# loosened: the guard is one of the things under test.
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
    case "$2" in
        test-compose-container:/app/docker-compose.yml) cat "${TEST_IMAGE_COMPOSE}" >"$3" ;;
        test-compose-container:/app/scripts/severino-hq-sync-scripts) cat "${TEST_IMAGE_SYNC}" >"$3" ;;
        *) exit 1 ;;
    esac
    exit 0
fi
if [ "$1" = "compose" ]; then
    file=""
    previous=""
    for argument do
        [ "${previous}" = "-f" ] && file="${argument}"
        previous="${argument}"
    done
    echo "docker $* image=${SEVERINO_IMAGE:-} DOCKER_CONFIG=${DOCKER_CONFIG:-} app_env=${SEVERINO_APP_ENV_FILE_HOST:-} ca=${SEVERINO_CONTROLLER_CA_FILE_HOST:-} compose=$(cat "${file}")" >>"${TEST_LOG}"
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

# The script runs as root and calls these directly rather than through sudo.
# `is-active` answers from the test: every held unit is active unless named in
# TEST_INACTIVE_UNITS, and a service is running only for the first
# TEST_RUNS_IN_FLIGHT times it is asked about.
cat >"${bin_dir}/systemctl" <<'EOF'
#!/bin/sh
echo "systemctl $*" >>"${TEST_LOG}"
if [ "$1" = "is-active" ]; then
    for unit do :; done
    case " ${TEST_INACTIVE_UNITS:-} " in *" ${unit} "*) exit 3 ;; esac
    case "${unit}" in
        *.service)
            asked="$(grep -c "^systemctl is-active --quiet ${unit}\$" "${TEST_LOG}")"
            [ "${asked}" -le "${TEST_RUNS_IN_FLIGHT:-0}" ] || exit 3 ;;
    esac
fi
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
echo "install $*" >>"${TEST_LOG}"
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

# The running release's installer. The deploy never runs it: the new image's
# sync replaces it before anything is installed.
previous_installer() {
    cat >"${lib_dir}/scripts/install-controller.sh" <<'EOF'
#!/bin/sh
echo "previous installer" >>"${TEST_LOG}"
exit 1
EOF
}
previous_installer
printf 'previous scripts\n' >"${lib_dir}/version"
printf 'previous sync\n' >"${sbin_dir}/severino-hq-sync-scripts"

# The sync program the new image ships. Like the real one, it refreshes the lib
# tree (compose file included) from the image now running, which brings the
# release's own installer. Controller activation fails unless a case says
# otherwise, which is what drives the rollback path.
readonly image_sync="${work_dir}/image-sync"
cat >"${image_sync}" <<'EOF'
#!/bin/sh
echo "release sync" >>"${TEST_LOG}"
printf 'updated scripts\n' >"${SEVERINO_HQ_LIB_DIR}/version"
: >"${SEVERINO_HQ_LIB_DIR}/only-in-the-release"
cp "${TEST_IMAGE_COMPOSE}" "${SEVERINO_HQ_LIB_DIR}/docker-compose.yml"
printf '#!/bin/sh\necho "release installer SYNCED=${SEVERINO_HQ_INSTALLER_SYNCED:-}" >>"${TEST_LOG}"\nexit "${TEST_CONTROLLER_FAIL:-1}"\n' \
    >"${SEVERINO_HQ_LIB_DIR}/scripts/install-controller.sh"
EOF

# The running release's compose file, and the one the new image carries.
printf 'previous compose\n' >"${lib_dir}/docker-compose.yml"
readonly image_compose="${work_dir}/image-compose.yml"
printf 'next compose\n' >"${image_compose}"
cat >"${lib_dir}/scripts/run-private.sh" <<'EOF'
#!/bin/sh
echo "private $1" >>"${TEST_LOG}"
shift 2
exec "$@"
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
        SEVERINO_HQ_SBIN_DIR="${sbin_dir}" \
        SEVERINO_HQ_RUN_DIR="${run_dir}" \
        SEVERINO_HQ_LOG_DIR="${work_dir}/log" \
        SEVERINO_HQ_VERIFIER_DIR="${verifier_dir}" \
        SEVERINO_HQ_IMAGE_PREFIX="${test_prefix}" \
        SEVERINO_HQ_ROOT_UID="${TEST_OWNER_UID:-0}" \
        SEVERINO_HQ_WEB_UID="${TEST_OWNER_UID:-10001}" \
        SEVERINO_HQ_WEB_SECRET_DIR="${web_secret_dir}" \
        TEST_VERIFY_FAIL="${TEST_VERIFY_FAIL:-0}" \
        TEST_IMAGE_COMPOSE="${image_compose}" \
        TEST_IMAGE_SYNC="${image_sync}" \
        TEST_INACTIVE_UNITS="${TEST_INACTIVE_UNITS:-}" \
        TEST_RUNS_IN_FLIGHT="${TEST_RUNS_IN_FLIGHT:-0}" \
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
# the previous release's copy in the lib tree, which is refreshed only after
# the health check.
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
    assert_held_and_restored
}

# The units that start release work on their own, the controller's path unit
# and the secret refresh among them, are stopped before anything changes and
# each one that was active is started again.
readonly held="severino-hq-controller.timer severino-hq-controller.path severino-hq-audit-prune.timer severino-hq-contacts-inbox.timer severino-hq-content-sync.timer severino-hq-public-registry.timer severino-hq-secrets.timer severino-hq-sessions-clear.timer severino-hq-script-drift.timer"
line_of() { grep -n -- "$1" "${log_file}" | head -n 1 | cut -d: -f1; }
# Each is a unit the repository ships, and the controller's every trigger is held.
for unit in ${held}; do
    [ -f "${repo_dir}/deploy/systemd/${unit}" ] || { echo "${unit} is held and not shipped." >&2; exit 1; }
done
for trigger in "${repo_dir}"/deploy/systemd/*.timer "${repo_dir}"/deploy/systemd/*.path; do
    if grep -qx -e 'Unit=severino-hq-controller.service' -e 'Unit=severino-hq-secrets.service' \
        -e 'Unit=severino-hq-script-drift.service' -e 'Unit=severino-hq-job@.*\.service' "${trigger}"; then
        case " ${held} " in
            *" $(basename "${trigger}") "*) ;;
            *) echo "$(basename "${trigger}") starts release work and is not held by the deploy." >&2; exit 1 ;;
        esac
    fi
done
assert_held_and_restored() {
    grep -qx "systemctl stop ${held}" "${log_file}" || {
        echo "The deploy did not hold every unit that starts release work." >&2; exit 1; }
    for unit in ${held}; do
        grep -qx "systemctl start ${unit}" "${log_file}" || {
            echo "${unit} was held and not restored." >&2; exit 1; }
    done
}
# Held before the image is pulled or replaced, and started again only after
# the last thing the deploy does to the container or the root tree.
assert_held_for_the_whole_window() {
    stopped="$(line_of "^systemctl stop ${held}\$")"
    first_change="$(line_of '^docker compose ')"
    first_start="$(line_of '^systemctl start ')"
    last_change="$(grep -n -e '^docker compose ' -e '^release installer' -e '^install ' "${log_file}" | tail -n 1 | cut -d: -f1)"
    if [ -z "${stopped}" ] || [ "${stopped}" -ge "${first_change}" ] || [ "${first_start}" -le "${last_change}" ]; then
        echo "A held unit could fire between the image swap and the release's install." >&2
        exit 1
    fi
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

# The release installs itself: the image's sync runs, is installed as the
# host's, and the installer that runs is the synced one. The previous release's
# installer never runs.
assert_release_installed_itself() {
    grep -q "^release sync$" "${log_file}" || {
        echo "The new image's sync program did not run." >&2; exit 1; }
    grep -q "^install -o root -g root -m 0755 ${run_dir}/severino-hq-compose\.[^ ]*/sync ${sbin_dir}/severino-hq-sync-scripts$" \
        "${log_file}" || { echo "The image's sync program was not installed." >&2; exit 1; }
    grep -q "^release installer SYNCED=1$" "${log_file}" || {
        echo "The synced installer did not run." >&2; exit 1; }
    if grep -q "^previous installer$" "${log_file}"; then
        echo "The previous release's installer ran after the sync." >&2
        exit 1
    fi
}

# A failed controller activation rolls back the image and restores timer state,
# the root tree exactly as it was, and the previous sync program.
run_failure 0
assert_release_installed_itself
grep -q "image=registry.example/hq/composition@sha256:previous" "${log_file}"
grep -qx 'previous scripts' "${lib_dir}/version"
grep -qx 'previous compose' "${lib_dir}/docker-compose.yml"
if [ -e "${lib_dir}/only-in-the-release" ]; then
    echo "Rollback left the new release's files in the root tree." >&2
    exit 1
fi
grep -q '"previous installer"' "${lib_dir}/scripts/install-controller.sh" || {
    echo "Rollback did not restore the previous installer." >&2; exit 1; }
grep -q "^install -o root -g root -m 0755 ${run_dir}/severino-hq-compose\.[^ ]*/sync.previous ${sbin_dir}/severino-hq-sync-scripts$" \
    "${log_file}" || { echo "Rollback did not restore the previous sync program." >&2; exit 1; }
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
assert_release_installed_itself
assert_staging_cleaned
# Nothing of the previous release runs against the new image: the path unit and
# the timers are held from before the pull until the release installed itself.
assert_held_and_restored
assert_held_for_the_whole_window
if [ "$(grep -c " up -d " "${log_file}")" -ne 1 ]; then
    echo "A healthy deploy recreated the container more than once." >&2
    exit 1
fi
grep -qx 'next compose' "${lib_dir}/docker-compose.yml"
# Compose, the sync and the installer write to the host's private log, never to
# the public Actions log.
for step in "Image pull" "Replace" "Release install"; do
    grep -qx "private ${step}" "${log_file}" || {
        echo "${step} did not go through the private log." >&2; exit 1; }
done
printf 'previous compose\n' >"${lib_dir}/docker-compose.yml"
printf 'previous scripts\n' >"${lib_dir}/version"
previous_installer
rm -f "${lib_dir}/only-in-the-release"

# A failed activation holds the units again before rolling back, and they are
# started only once the previous image and tree are back.
run_failure 0
assert_held_for_the_whole_window
[ "$(grep -c "^systemctl stop ${held}\$" "${log_file}")" -eq 2 ] || {
    echo "The units were not held again for the rollback." >&2; exit 1; }

# A unit that was not active before the deploy is not started by it.
TEST_INACTIVE_UNITS="severino-hq-controller.path severino-hq-secrets.timer"
export TEST_INACTIVE_UNITS
: >"${log_file}"
if deploy 1; then
    echo "Expected deployment to fail." >&2
    exit 1
fi
unset TEST_INACTIVE_UNITS
for unit in severino-hq-controller.path severino-hq-secrets.timer; do
    if grep -qx "systemctl start ${unit}" "${log_file}"; then
        echo "${unit} was inactive before the deploy and was started by it." >&2
        exit 1
    fi
done
grep -qx "systemctl start severino-hq-controller.timer" "${log_file}"
grep -qx "systemctl start severino-hq-content-sync.timer" "${log_file}"

# A run already in flight is waited for before the image is replaced, and a run
# that never ends does not hold the deploy for ever.
TEST_RUNS_IN_FLIGHT=2
export TEST_RUNS_IN_FLIGHT
: >"${log_file}"
deploy 1 2>/dev/null && { echo "Expected deployment to fail." >&2; exit 1; }
[ "$(grep -c '^systemctl is-active --quiet severino-hq-controller.service$' "${log_file}")" -eq 3 ] || {
    echo "The deploy did not wait for the controller run in flight." >&2; exit 1; }
drained="$(grep -n '^systemctl is-active --quiet severino-hq-controller.service$' "${log_file}" | tail -n 1 | cut -d: -f1)"
[ "${drained}" -lt "$(line_of '^docker compose ')" ] || {
    echo "The image was replaced under a run in flight." >&2; exit 1; }
TEST_RUNS_IN_FLIGHT=1000000
: >"${log_file}"
if deploy 1 2>"${work_dir}/drain.err"; then echo "Expected deployment to fail." >&2; exit 1; fi
unset TEST_RUNS_IN_FLIGHT
grep -q "still going" "${work_dir}/drain.err" || {
    echo "A run that outlasted the wait was not reported." >&2; exit 1; }
[ "$(grep -c '^systemctl is-active --quiet severino-hq-controller.service$' "${log_file}")" -eq 36 ] || {
    echo "The wait for a run in flight is not bounded at three minutes." >&2; exit 1; }

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
# call reads it, and where Docker warns, every single deploy, that it is
# sitting there in plaintext.
: >"${log_file}"
printf 'x-access-token\nsecret-token-value\n' | PATH="${bin_dir}:${PATH}" \
    TEST_LOG="${log_file}" \
    TEST_UID=0 \
    TEST_PULL_FAIL=1 \
    SEVERINO_HQ_APP_DIR="${app_dir}" \
    SEVERINO_HQ_LIB_DIR="${lib_dir}" \
    SEVERINO_HQ_SBIN_DIR="${sbin_dir}" \
    SEVERINO_HQ_RUN_DIR="${run_dir}" \
    SEVERINO_HQ_VERIFIER_DIR="${verifier_dir}" \
    SEVERINO_HQ_IMAGE_PREFIX="${test_prefix}" \
    TEST_IMAGE_COMPOSE="${image_compose}" \
    TEST_IMAGE_SYNC="${image_sync}" \
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

# The checkout's .env cannot choose what the web container binds: a run
# directory of its own, or a certificate authority from anywhere.
refuses_env() {
    printf '%s\n' "$1" >"${app_dir}/.env"
    : >"${log_file}"
    if TEST_OWNER_UID="$(id -u)" deploy 0 2>/dev/null; then
        echo "Expected the .env line '$1' to be refused." >&2
        exit 1
    fi
    if [ -s "${log_file}" ]; then
        echo "A refused .env reached docker: $1" >&2
        exit 1
    fi
}
refuses_env "SEVERINO_CONTROLLER_RUN_DIR=/home"
refuses_env "SEVERINO_CONTROLLER_CA_FILE_HOST=${work_dir}/ca.pem"
refuses_env "SEVERINO_CONTROLLER_CA_FILE_HOST=/usr/local/share/ca-certificates/../../../../etc/shadow"

# What compose bound as the web environment, on the last deploy.
bound_env() { grep " up -d " "${log_file}" | tail -n 1 | sed -n 's/.*app_env=\([^ ]*\) .*/\1/p'; }
deploy_bound() { # deploy_bound <expected app_env>
    : >"${log_file}"
    TEST_OWNER_UID="$(id -u)" deploy 0 >/dev/null 2>&1 || true
    [ "$(bound_env)" = "$1" ] || { echo "compose bound '$(bound_env)', not '$1'." >&2; exit 1; }
}

# The secrets environment is never named by .env: with nothing rendered,
# compose binds nothing, whatever the file says.
printf 'SEVERINO_APP_ENV_FILE_HOST=/etc/shadow\n' >"${app_dir}/.env"
deploy_bound /dev/null
rm -f "${app_dir}/.env"

# The rendered file on the tmpfs, in a directory only root enters, is bound.
mkdir -p "${web_secret_dir}"
chmod 700 "${web_secret_dir}"
printf 'SECRET=rendered\n' >"${web_secret_dir}/severino_hq_env"
deploy_bound "${web_secret_dir}/severino_hq_env"
# In a directory others can enter, or as a link, it is refused before docker.
chmod 755 "${web_secret_dir}"
: >"${log_file}"
if TEST_OWNER_UID="$(id -u)" deploy 0 2>/dev/null || [ -s "${log_file}" ]; then
    echo "An enterable secrets directory was bound." >&2
    exit 1
fi
chmod 700 "${web_secret_dir}"
mv "${web_secret_dir}/severino_hq_env" "${work_dir}/planted.env"
ln -s "${work_dir}/planted.env" "${web_secret_dir}/severino_hq_env"
: >"${log_file}"
if TEST_OWNER_UID="$(id -u)" deploy 0 2>/dev/null || [ -s "${log_file}" ]; then
    echo "A linked secrets file was bound." >&2
    exit 1
fi
rm "${web_secret_dir}/severino_hq_env"

# Before the tmpfs copy exists, the checkout's is bound, with the same checks.
mkdir -p "${app_dir}/secrets"
chmod 700 "${app_dir}/secrets"
printf 'SECRET=rendered\n' >"${app_dir}/secrets/severino_hq_env"
deploy_bound "${app_dir}/secrets/severino_hq_env"
# Once the tmpfs copy exists, a healthy deploy binds it and removes the
# checkout's, which no container reads.
mv "${work_dir}/planted.env" "${web_secret_dir}/severino_hq_env"
: >"${log_file}"
TEST_CONTROLLER_FAIL=0
export TEST_CONTROLLER_FAIL
if ! TEST_OWNER_UID="$(id -u)" deploy 0 >/dev/null 2>&1; then
    echo "Expected the deploy that moves the environment to succeed." >&2
    exit 1
fi
unset TEST_CONTROLLER_FAIL
[ "$(bound_env)" = "${web_secret_dir}/severino_hq_env" ] \
    || { echo "The moved environment was not bound." >&2; exit 1; }
[ ! -e "${app_dir}/secrets/severino_hq_env" ] \
    || { echo "The checkout's environment outlived the move." >&2; exit 1; }

echo "deploy-image rollback, compose and input-guard tests passed"
