#!/bin/sh
# scripts/preflight.sh against a fixture repository and a fake deploy host
# reached through a fake ssh: exit 0 only when every gate ran and passed.

set -eu
# Hermetic: none of what the gates are asked for comes from the caller.
unset REQUIRE_COMPOSED SEVERINO_HQ_DEPLOY_HOST \
    TEST_FAIL_CI TEST_FAIL_COMPOSED TEST_FREE_KB TEST_DRIFT TEST_SUDO_PASSWORD \
    TEST_UNIT_STATE TEST_INSTALLED_AT

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
mkdir -p "${repo}/controller/cmd/hq-host" "${repo}/.github/workflows"
cp .github/workflows/ci.yml "${repo}/.github/workflows/"
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
: >"${lib}/.source"
cat >"${bin}/ssh" <<EOF
#!/bin/sh
echo "ssh \$*" >>"\${TEST_LOG}"
PATH="${host}/bin:\${PATH}" PREFLIGHT_LIB="${lib}" \\
    PREFLIGHT_SYSTEMD="${host}/etc" PREFLIGHT_ROOT_UID="$(id -u)" exec sh -s
EOF
# GNU stat's modification time, which the host has: the release was installed
# at TEST_INSTALLED_AT, in Unix seconds.
cat >"${host}/bin/stat" <<'EOF'
#!/bin/sh
[ "$1 $2" = "-c %Y" ] && [ -e "$3" ] || exit 1
echo "${TEST_INSTALLED_AT:-1000}"
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
    *suite:as-image*) exit "${TEST_FAIL_COMPOSED:-0}" ;;
    *) exit "${TEST_FAIL_CI:-0}" ;;
esac
STUB
chmod 0755 "${bin}/ssh" "${bin}/mise" "${host}/bin/sudo" "${host}/bin/df" "${host}/bin/stat"

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
        env "$@" sh "${repo}/scripts/preflight.sh" >"${work}/out" 2>&1 || status=$?
    return "${status}"
}
expect() { # expect <status> <why> [the task's usage_* flags]
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
grep -qx "mise run suite:as-image REQUIRE_COMPOSED=1" "${TEST_LOG}" ||
    fail "the composed suite ran without being required"
grep -qx "ssh -o BatchMode=yes deploy.example.test sh -s" "${TEST_LOG}" ||
    fail "the host was not reached through ssh as expected"
says "ok      the host is the release it runs"
says "upgrade-container.sh is root's alone"
if grep -q "secret-marker-example" "${work}/out"; then
    fail "the host check printed the sudo rules"
fi

# 2. Skipping the host is loud and never a pass.
expect 2 "a skipped host" usage_skip_host=true
says "SKIPPED  deploy host (--skip-host)"
says "HOST NOT VERIFIED"
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

# 4. --remote reads what GitHub ran on HEAD instead of running `mise run ci`.
#    The fake gh answers from JSON pages, <API path with / as _>.<page>.json in
#    TEST_GH, through the real jq; past the first page only with --paginate.
#    Watching a run replaces the pages with those in TEST_GH/finished.
cat >"${bin}/gh" <<'EOF'
#!/bin/sh
echo "gh $*" >>"${TEST_LOG}"
case "$1" in
    repo) echo example/host ;;
    run) if [ -d "${TEST_GH}/finished" ]; then cp "${TEST_GH}/finished/"* "${TEST_GH}/"; fi ;;
    api)
        shift
        pages=1
        if [ "$1" = --paginate ]; then pages=99; shift; fi
        [ "$2" = --jq ] || exit 64
        key="$(printf '%s' "${1%%\?*}" | tr / _)"
        page=1
        while [ "${page}" -le "${pages}" ] && [ -f "${TEST_GH}/${key}.${page}.json" ]; do
            jq -r "$3" "${TEST_GH}/${key}.${page}.json" || exit 1
            page=$((page + 1))
        done ;;
    *) exit 64 ;;
esac
EOF
chmod 0755 "${bin}/gh"
git_ update-ref refs/remotes/origin/feature HEAD
sha="$(git_ rev-parse HEAD)"
TEST_GH="${work}/gh"
export TEST_GH PREFLIGHT_REMOTE_POLL=0 PREFLIGHT_REMOTE_TIMEOUT=0

list() { printf '%s,' "$@" | sed 's/,$//'; } # JSON values, comma-joined
page() { # page <API path> <page> <key> <JSON values>
    printf '{"%s":[%s]}\n' "$3" "$4" >"${TEST_GH}/$(printf '%s' "repos/example/host/$1" | tr / _).$2.json"
}
run() { # run <id> <workflow file> <event> <status> <conclusion|null> <created>
    printf '{"id":%s,"path":".github/workflows/%s","event":"%s","status":"%s","conclusion":%s,"created_at":"%s"}' \
        "$1" "$2" "$3" "$4" "$5" "$6"
}
runs() { page actions/runs 1 workflow_runs "$(list "$@")"; }
job() { # job <conclusion> <name> [the promotion step's conclusion]
    printf '{"name":"%s","status":"completed","conclusion":"%s","steps":[{"name":"Promote the image its pull request proved","conclusion":"%s"}]}' \
        "$2" "$1" "${3:-skipped}"
}
jobs() { # jobs <run id> <job...>
    id="$1"
    shift
    page "actions/runs/${id}/jobs" 1 jobs "$(list "$@")"
}
check() { printf '{"name":"%s","status":"%s","conclusion":%s}' "$3" "$1" "$2"; }
checks() { # checks <page> <check...>
    n="$1"
    shift
    page "commits/${sha}/check-runs" "${n}" check_runs "$(list "$@")"
}
# The gate jobs a push skips when its pull request proved the tree.
gates() { list "$(job "$1" Checks)" "$(gates_but_checks "$1")"; } # gates <conclusion>
gates_but_checks() {
    list "$(job "$1" "Tests (3.15)")" "$(job "$1" "Browser (layout)")" \
        "$(job "$1" "Browser (navigation)")" "$(job "$1" "Browser (demo)")" "$(job "$1" Controller)"
}
passed='"success"'
fresh() { rm -rf "${TEST_GH}" && mkdir -p "${TEST_GH}"; }
# A pull request whose run of CI passed whole, beside an earlier release that
# waits for approval and the Production check that reports it.
pull_request() {
    fresh
    runs "$(run 11 ci.yml pull_request completed "${passed}" 2026-10-19T10:00:00Z)" \
        "$(run 12 deploy.yml workflow_dispatch waiting null 2026-10-19T10:30:00Z)"
    pr_jobs "$(gates success)"
    checks 1 "$(check completed "${passed}" Checks)" "$(check completed "${passed}" "Severino HQ · Review")" \
        "$(check in_progress null "Severino HQ · Production")"
}
pr_jobs() { # pr_jobs <the gate jobs>
    jobs 11 "$(job skipped "Proven on its pull request")" "$1" "$(job success CodeQL)" "$(job success Image)" \
        "$(job success "Image checks")" "$(job success "Composition / Build HQ")" "$(job success Ready)" \
        "$(job skipped Release)"
}
# A push to main: CI, the Compose its Release started, and the Demo build.
main_push() { # main_push <the gate jobs> <the promotion step's conclusion>
    fresh
    runs "$(run 21 ci.yml push completed "${passed}" 2026-10-19T11:00:00Z)" \
        "$(run 22 compose.yml workflow_dispatch completed "${passed}" 2026-10-19T11:20:00Z)" \
        "$(run 23 demo.yml push completed "${passed}" 2026-10-19T11:00:01Z)"
    jobs 21 "$(job success "Proven on its pull request")" "$1" "$(job success CodeQL)" \
        "$(job success Image "$2")" "$(job skipped Ready)" "$(job success Release)"
    checks 1 "$(check completed "${passed}" Image)"
}

pull_request
expect 0 "a pull request whose CI passed" usage_remote=true
says "success  .github/workflows/ci.yml (pull_request, run 11)"
if grep -q '^mise run -c ci ' "${TEST_LOG}"; then fail "--remote still ran the local gates"; fi
remote_refuses() { # remote_refuses <why> [expected output]
    expect 1 "$1" usage_remote=true
    [ -z "${2:-}" ] || says "$2"
}

pr_jobs "$(list "$(job skipped Checks)" "$(gates_but_checks success)")"
remote_refuses "a required job that was skipped" "required job Checks: skipped"
pr_jobs "$(gates_but_checks success)"
remote_refuses "a required job that never ran" "required job Checks: did not run"

pull_request
checks 2 "$(check completed '"failure"' "Structural bar")"
remote_refuses "a failed check past the first page" "a check did not pass"

fresh
runs "$(run 31 demo.yml push completed "${passed}" 2026-10-19T10:00:00Z)"
checks 1 "$(check completed "${passed}" "Unrelated check")"
remote_refuses "an unrelated workflow and check, and no CI" "no CI run on this commit"
fresh
checks 1 "$(check completed "${passed}" "Unrelated check")"
remote_refuses "an unrelated check alone" "no CI run on this commit"

pull_request
runs "$(run 11 ci.yml pull_request completed '"failure"' 2026-10-19T10:00:00Z)"
remote_refuses "a CI run whose latest attempt failed" "a workflow run did not pass"
runs "$(run 11 ci.yml pull_request completed '"cancelled"' 2026-10-19T10:00:00Z)"
remote_refuses "a cancelled CI run"
runs "$(run 13 ci.yml pull_request completed '"failure"' 2026-10-19T12:00:00Z)" \
    "$(run 11 ci.yml pull_request completed "${passed}" 2026-10-19T10:00:00Z)"
remote_refuses "an earlier run that passed, after a newer one failed" "run 13"
runs "$(run 11 ci.yml pull_request in_progress null 2026-10-19T10:00:00Z)"
remote_refuses "a rerun still going when time runs out" "ci.yml is in_progress"

# A run still going is watched until it finishes.
pull_request
mkdir -p "${TEST_GH}/finished"
cp "${TEST_GH}/repos_example_host_actions_runs.1.json" "${TEST_GH}/finished/"
runs "$(run 11 ci.yml pull_request in_progress null 2026-10-19T10:00:00Z)"
PREFLIGHT_REMOTE_TIMEOUT=600
expect 0 "a run that finishes while watched" usage_remote=true
PREFLIGHT_REMOTE_TIMEOUT=0
grep -q '^gh run watch 11 ' "${TEST_LOG}" || fail "the running CI was not watched"

main_push "$(gates success)" skipped
expect 0 "a push to main whose gates and Compose passed" usage_remote=true
main_push "$(gates skipped)" success
expect 0 "a push that promoted the image its pull request proved" usage_remote=true
main_push "$(gates skipped)" skipped
remote_refuses "a push that skipped its gates and promoted nothing" "required job Checks: skipped"
main_push "$(gates success)" skipped
runs "$(run 21 ci.yml push completed "${passed}" 2026-10-19T11:00:00Z)" \
    "$(run 22 compose.yml workflow_dispatch in_progress null 2026-10-19T11:20:00Z)"
remote_refuses "a Compose run still going" "compose.yml is in_progress"
runs "$(run 21 ci.yml push completed "${passed}" 2026-10-19T11:00:00Z)"
remote_refuses "a push whose Release has not started Compose" "has not started Compose"
git_ update-ref -d refs/remotes/origin/feature
git_ update-ref -d refs/remotes/origin/main
expect 1 "an unpushed HEAD" usage_remote=true
says "HEAD is not pushed"
git_ update-ref refs/remotes/origin/main HEAD~1
git_ update-ref refs/remotes/origin/feature HEAD
unset TEST_GH PREFLIGHT_REMOTE_POLL PREFLIGHT_REMOTE_TIMEOUT

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

# Where sudo needs a password, the drift unit's run since the release was
# installed is the evidence; without one there is no verdict on the host.
export TEST_SUDO_PASSWORD=1
expect 2 "a verify that cannot run, and no run of the drift unit"
says "UNKNOWN hq-host verify needs a password here"
says "HOST NOT VERIFIED"
drift_unit() { # drift_unit <started> <result> <status>
    printf 'ActiveState=inactive\nResult=%s\nExecMainStatus=%s\nExecMainStartTimestamp=@%s\nExecMainExitTimestamp=@%s\n' \
        "$2" "$3" "$1" "$(($1 + 60))" >"${TEST_UNIT_STATE}/severino-hq-script-drift.service"
}
TEST_UNIT_STATE="${work}/units"
mkdir -p "${TEST_UNIT_STATE}"
export TEST_UNIT_STATE TEST_INSTALLED_AT=1000
drift_unit 2000 success 0
expect 0 "a drift unit that verified the installed release"
says "ok      the host is the release it runs (severino-hq-script-drift.service"
drift_unit 500 success 0
expect 2 "a drift unit that last verified an earlier release"
drift_unit 2000 exit-code 1
host_fault "a drift unit that failed on the installed release" "severino-hq-script-drift.service failed since this release was installed"
drift_unit 2000 success 0
mv "${lib}/.source" "${lib}/.source.held"
expect 2 "a release whose install time cannot be read"
mv "${lib}/.source.held" "${lib}/.source"
unset TEST_SUDO_PASSWORD TEST_UNIT_STATE TEST_INSTALLED_AT

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
