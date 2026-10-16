#!/bin/sh
# scripts/preflight.sh against a fixture repository and a fake deploy host
# reached through a fake ssh: exit 0 only when every gate ran and passed.

set -eu
# Hermetic: none of what the gates are asked for comes from the caller.
unset REQUIRE_COMPOSED SEVERINO_HQ_DEPLOY_HOST \
    TEST_FAIL_CI TEST_FAIL_COMPOSED TEST_FREE_KB TEST_DRIFT TEST_SUDO_PASSWORD

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT HUP INT TERM
failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

# The fixture repository: the real preflight, host half and unit library,
# with mise stubbed to report how the gates were called.
repo="${work}/repo"
mkdir -p "${repo}/scripts/lib"
cd "${repo_dir}"
cp -R deploy "${repo}/"
mkdir -p "${repo}/controller/cmd/hq-host"
cp controller/cmd/hq-host/main.go "${repo}/controller/cmd/hq-host/"
for f in preflight.sh preflight-host.sh deploy-image.sh lib/systemd-units.sh upgrade-container.sh; do
    cp "scripts/${f}" "${repo}/scripts/${f}"
done
# Hermetic: the fixture is a throwaway repository, so no signing and no hooks.
git_() {
    git -C "${repo}" -c user.name=test -c user.email=test@example.com \
        -c commit.gpgsign=false -c core.hooksPath=/dev/null "$@"
}
git_ init -q
git_ add -A
git_ commit -q -m main
git_ update-ref refs/remotes/origin/main HEAD
# The release adds a unit main does not ship: the release installs it, so the
# host is not expected to have it yet.
printf '[Timer]\nOnCalendar=daily\n' >"${repo}/deploy/systemd/severino-hq-example.timer"
git_ add -A
git_ commit -q -m release

# The deploy host as the previous release left it.
host="${work}/host"
lib="${host}/lib"
mkdir -p "${lib}" "${host}/etc" "${host}/bin"
git_ archive HEAD~1 scripts deploy | tar -x -C "${lib}"
(cd "${lib}/deploy/systemd" && find . -type f ! -name '*.example') | while IFS= read -r f; do
    mkdir -p "${host}/etc/$(dirname "${f}")"
    cp "${lib}/deploy/systemd/${f}" "${host}/etc/${f}"
done

bin="${work}/bin"
mkdir -p "${bin}"
cat >"${bin}/ssh" <<EOF
#!/bin/sh
echo "ssh \$*" >>"\${TEST_LOG}"
PATH="${host}/bin:\${PATH}" PREFLIGHT_LIB="${lib}" \\
    PREFLIGHT_SYSTEMD="${host}/etc" PREFLIGHT_ROOT_UID="$(id -u)" exec sh -s
EOF
# sudo refuses to run unless its stdin is empty: stdin is the host script.
cat >"${host}/bin/sudo" <<'EOF'
#!/bin/sh
if IFS= read -r _; then echo "sudo read the host script from stdin" >&2; exit 99; fi
case "$1 ${2##*/} $3" in
    "-l -U "*) printf '%s\n' "${TEST_SUDO_RULES}" ;;
    "-n hq-host verify")
        if [ -n "${TEST_SUDO_PASSWORD:-}" ]; then echo "sudo: a password is required" >&2; exit 1; fi
        if [ -n "${TEST_DRIFT:-}" ]; then echo "drift: ${TEST_DRIFT}" >&2; exit 1; fi
        echo "The host is its release." ;;
    *) exit 98 ;;
esac
EOF
cp "${repo_dir}/scripts/fixtures/systemctl" "${host}/bin/systemctl"
TEST_ACCOUNT="$(id -un)"
export TEST_ACCOUNT
cat >"${host}/bin/df" <<'EOF'
#!/bin/sh
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
printf '/dev/test 99999999 1 %s 1%% /\n' "${TEST_FREE_KB:-9999999}"
EOF
cat >"${bin}/mise" <<'STUB'
#!/bin/sh
echo "mise $* REQUIRE_COMPOSED=${REQUIRE_COMPOSED:-}" >>"${TEST_LOG}"
case "$*" in
    *suite:composed*) exit "${TEST_FAIL_COMPOSED:-0}" ;;
    *) exit "${TEST_FAIL_CI:-0}" ;;
esac
STUB
chmod 0755 "${bin}/ssh" "${bin}/mise" "${host}/bin/sudo" "${host}/bin/df"

readonly good_rules="Matching Defaults entries:
    env_reset, secret-marker-example
User runner may run the following commands:
    (root) NOPASSWD: ${lib}/scripts/deploy-image.sh"
TEST_SUDO_RULES="${good_rules}"
TEST_LOG="${work}/log"
export TEST_SUDO_RULES TEST_LOG

preflight() {
    : >"${TEST_LOG}"
    status=0
    PATH="${bin}:${PATH}" SEVERINO_HQ_DEPLOY_HOST="${SEVERINO_HQ_DEPLOY_HOST-deploy.example.test}" \
        sh "${repo}/scripts/preflight.sh" "$@" >"${work}/out" 2>&1 || status=$?
    return "${status}"
}
expect() { # expect <status> <why> [args]
    want="$1"
    why="$2"
    shift 2
    status=0
    preflight "$@" || status=$?
    [ "${status}" -eq "${want}" ] || fail "${why}: exit ${status}, expected ${want}
$(cat "${work}/out")"
}
says() { grep -qF "$1" "${work}/out" || fail "output lacks '$1'"; }

# 1. A current host and passing gates: ready, every gate required, host reached.
expect 0 "a ready release"
says "[preflight] READY"
grep -qx "mise run -c ci REQUIRE_COMPOSED=" "${TEST_LOG}" ||
    fail "the pipeline's gates did not run"
grep -qx "mise run suite:composed REQUIRE_COMPOSED=1" "${TEST_LOG}" ||
    fail "the composed suite ran without being required"
grep -qx "ssh -o BatchMode=yes deploy.example.test sh -s" "${TEST_LOG}" ||
    fail "the host was not reached through ssh as expected"
says "ok      the host is the release it runs"
says "upgrade-container.sh is root's alone"
if grep -q "secret-marker-example" "${work}/out"; then
    fail "the host check printed the sudo rules"
fi

# 2. Skipping the host is loud and never a pass.
expect 2 "a skipped host" --skip-host
says "SKIPPED  deploy host (--skip-host)"
says "HOST NOT CHECKED"
if grep -q '^ssh' "${TEST_LOG}"; then fail "--skip-host still reached the host"; fi
# Assigned and reset around each call: an assignment prefixed to a shell
# function persists after it returns.
SEVERINO_HQ_DEPLOY_HOST=""
expect 1 "an unset deploy host"
says "SEVERINO_HQ_DEPLOY_HOST is not set"
unset SEVERINO_HQ_DEPLOY_HOST

# 3. A failing local gate, or an uncommitted change, is not ready.
export TEST_FAIL_CI=1
expect 1 "a failing gate"
unset TEST_FAIL_CI
export TEST_FAIL_COMPOSED=1
expect 1 "a failing composed suite"
unset TEST_FAIL_COMPOSED
: >"${repo}/uncommitted"
expect 1 "an uncommitted change"
says "FAILED   committed tree"
rm "${repo}/uncommitted"

# 4. --remote reads GitHub's checks on HEAD instead of running `mise run ci`.
cat >"${bin}/gh" <<'EOF'
#!/bin/sh
echo "gh $*" >>"${TEST_LOG}"
case "$1" in
    repo) echo example/host ;;
    api) printf '%s\n' "${TEST_CHECKS}" ;;
esac
EOF
chmod 0755 "${bin}/gh"
git_ update-ref refs/remotes/origin/feature HEAD
export PREFLIGHT_REMOTE_POLL=0 PREFLIGHT_REMOTE_TIMEOUT=0
export TEST_CHECKS="completed success test
completed success code scanning"
expect 0 "every GitHub check passed" --remote
says "code scanning"
if grep -q '^mise run -c ci ' "${TEST_LOG}"; then fail "--remote still ran the local gates"; fi
TEST_CHECKS="completed success test
completed failure structural bar"
expect 1 "a failed GitHub check" --remote
TEST_CHECKS="in_progress - test"
expect 1 "a GitHub check still running when time runs out" --remote
TEST_CHECKS=""
expect 1 "no GitHub check at all" --remote
git_ update-ref -d refs/remotes/origin/feature
git_ update-ref -d refs/remotes/origin/main
TEST_CHECKS="completed success test"
expect 1 "an unpushed HEAD" --remote
says "HEAD is not pushed"
git_ update-ref refs/remotes/origin/main HEAD~1
unset TEST_CHECKS PREFLIGHT_REMOTE_POLL PREFLIGHT_REMOTE_TIMEOUT

# 5. Each host fault fails the preflight and says what it is.
host_fault() { # host_fault <why> <expected output>
    expect 1 "$1"
    says "$2"
}
TEST_SUDO_RULES="    (ALL : ALL) ALL"
host_fault "a runner with unrestricted sudo" "may run any command"
TEST_SUDO_RULES="    (root) NOPASSWD: /usr/bin/true"
host_fault "a runner without the rule" "has no NOPASSWD sudo rule"
TEST_SUDO_RULES="${good_rules}"
export TEST_FREE_KB=1024
host_fault "a full disk" "the deploy needs"
unset TEST_FREE_KB

rm "${host}/etc/severino-hq-controller.timer"
host_fault "an established unit missing" "severino-hq-controller.timer is not installed"
cp "${lib}/deploy/systemd/severino-hq-controller.timer" "${host}/etc/"

export TEST_DRIFT="scripts/backup.sh differs from the image's"
host_fault "a host that is not its release" "FAIL    scripts/backup.sh differs from the image's"
unset TEST_DRIFT

export TEST_SUDO_PASSWORD=1
expect 0 "a verify that needs a password is a warning, not a pass of it"
says "warn    could not run hq-host verify without a password"
unset TEST_SUDO_PASSWORD

chmod g+w "${lib}/scripts/upgrade-container.sh"
host_fault "a helper sudo runs as root that others can write" "upgrade-container.sh is writable by its group or others"
chmod g-w "${lib}/scripts/upgrade-container.sh"

TEST_ACCOUNT=root
host_fault "a runner that runs as root" "the Actions runner runs as 'root'"
TEST_ACCOUNT="$(id -un)"

expect 0 "the repaired host"

# 6. Every sudo in the host half reads /dev/null: stdin is the script itself.
if grep -nE '(^[[:space:]]*|\$\(|[;&|][[:space:]]*)sudo[[:space:]]' \
    "${repo_dir}/scripts/preflight-host.sh" | grep -v '</dev/null'; then
    fail "a sudo in preflight-host.sh does not read /dev/null"
fi

if [ "${failures}" -ne 0 ]; then
    echo "Preflight contracts failed (${failures})." >&2
    exit 1
fi
echo "Preflight contracts hold."
