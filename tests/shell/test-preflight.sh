#!/bin/sh
# tools/gate/preflight.py, what `mise run preflight` runs, against a fixture repository, a fake GitHub and a fake
# deploy host reached through a fake ssh: exit 0 only when every gate ran and
# passed. What the host half checks is hq-host check's, held by
# controller/host/check_test.go; here, that it is built from HEAD for the
# host's platform, handed the units, and its exit read.

set -eu
# Hermetic: none of what the gates are asked for comes from the caller.
unset REQUIRE_COMPOSED SEVERINO_HQ_DEPLOY_HOST PREFLIGHT_BASE_REF \
    TEST_FAIL_CI TEST_FAIL_COMPOSED TEST_HOST_EXIT TEST_UNAME TEST_HOST_REFUSES TEST_PROGRAM_PAD

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT HUP INT TERM
failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

# The fixture repository: the real task and its verdict helper, CI's
# workflow, the units, and hq-host's source.
repo="${work}/repo"
mkdir -p "${repo}/tools/gate" "${repo}/tools/pipeline" "${repo}/controller/cmd/hq-host" "${repo}/.github/workflows"
cd "${repo_dir}"
cp -R deploy .gitignore "${repo}/"
cp .github/workflows/ci.yml "${repo}/.github/workflows/"
cp controller/cmd/hq-host/main.go "${repo}/controller/cmd/hq-host/"
cp tools/__init__.py "${repo}/tools/"
cp tools/gate/__init__.py tools/gate/preflight.py tools/gate/verdict.py "${repo}/tools/gate/"
cp tools/pipeline/__init__.py tools/pipeline/ci_report.py "${repo}/tools/pipeline/"
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
printf '// the release\n' >>"${repo}/controller/cmd/hq-host/main.go"
git_ add -A
git_ commit -q -m release

bin="${work}/bin"
mkdir -p "${bin}"
# The deploy host: it names its platform, then unpacks what it is handed,
# lists it, and exits as TEST_HOST_EXIT says hq-host check did.
cat >"${bin}/ssh" <<'EOF'
#!/bin/sh
echo "ssh $*" >>"${TEST_LOG}"
case "$*" in
    *" uname -s -m") echo "${TEST_UNAME-Linux x86_64}"; exit 0 ;;
esac
if [ -n "${TEST_HOST_REFUSES-}" ]; then
    echo "mktemp: ${TEST_HOST_REFUSES}" >&2
    exit 1
fi
dir="$(mktemp -d)"
tar -x -C "${dir}" || exit 70
(cd "${dir}" && find . -type f | LC_ALL=C sort) >"${TEST_PAYLOAD}"
cp "${dir}/hq-host" "${TEST_PAYLOAD}.program"
rm -rf "${dir}"
echo "ok      the fake host"
exit "${TEST_HOST_EXIT:-0}"
EOF
# go build writes, as the program, the hq-host source it was given.
cat >"${bin}/go" <<'EOF'
#!/bin/sh
echo "go $* GOOS=${GOOS-} GOARCH=${GOARCH-} CGO_ENABLED=${CGO_ENABLED-}" >>"${TEST_LOG}"
while [ "$#" -gt 0 ] && [ "$1" != -o ]; do shift; done
cp cmd/hq-host/main.go "$2"
# Padded past any pipe buffer, so a host that stops reading breaks the pipe.
if [ -n "${TEST_PROGRAM_PAD-}" ]; then dd if=/dev/zero bs=1024 count=4096 2>/dev/null >>"$2"; fi
EOF
cat >"${bin}/mise" <<'EOF'
#!/bin/sh
echo "mise $* REQUIRE_COMPOSED=${REQUIRE_COMPOSED:-}" >>"${TEST_LOG}"
case "$*" in
    *suite:as-image*) exit "${TEST_FAIL_COMPOSED:-0}" ;;
    *) exit "${TEST_FAIL_CI:-0}" ;;
esac
EOF
chmod 0755 "${bin}/ssh" "${bin}/go" "${bin}/mise"
TEST_LOG="${work}/log"
TEST_PAYLOAD="${work}/payload"
export TEST_LOG TEST_PAYLOAD

preflight() {
    : >"${TEST_LOG}"
    rm -f "${TEST_PAYLOAD}"
    status=0
    # From the fixture, so `-m` imports its copy of the gate.
    (cd "${repo}" && PATH="${bin}:${PATH}" SEVERINO_HQ_DEPLOY_HOST="${SEVERINO_HQ_DEPLOY_HOST-deploy.example.test}" \
        env "$@" python3 -m tools.gate.preflight) >"${work}/out" 2>&1 || status=$?
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
says() { grep -qF -- "$1" "${work}/out" || fail "output lacks '$1'"; }
logged() { grep -qF -- "$1" "${TEST_LOG}" || fail "$2"; }

# 1. A current host and passing gates: ready, every gate required, the host
#    checked by the release's own hq-host.
expect 0 "a ready release"
says "READY"
grep -qx "mise run -c ci REQUIRE_COMPOSED=" "${TEST_LOG}" || fail "the pipeline's gates did not run"
grep -qx "mise run suite:as-image REQUIRE_COMPOSED=1" "${TEST_LOG}" || fail "the composed suite ran without being required"
logged "ssh -o BatchMode=yes deploy.example.test uname -s -m" "the host was not asked its platform"
logged "GOOS=linux GOARCH=amd64 CGO_ENABLED=0" "the check was not built for the host's platform"
logged "check --release" "the host did not run hq-host check"
grep -qx "./hq-host" "${TEST_PAYLOAD}" || fail "the host was not handed hq-host"
grep -qx "./release/deploy/systemd/severino-hq-example.timer" "${TEST_PAYLOAD}" ||
    fail "the host was not handed the release's units"
grep -qx "./main/deploy/systemd/severino-hq-web.service" "${TEST_PAYLOAD}" ||
    fail "the host was not handed main's units"
if grep -q "./main/deploy/systemd/severino-hq-example.timer" "${TEST_PAYLOAD}"; then
    fail "main's units include the release's new one"
fi
git_ show HEAD:controller/cmd/hq-host/main.go | cmp -s - "${TEST_PAYLOAD}.program" ||
    fail "hq-host was not built from HEAD"

# Built from HEAD, never from the working tree.
printf '// not committed\n' >>"${repo}/controller/cmd/hq-host/main.go"
expect 1 "an uncommitted change"
says "FAILED   committed tree"
git_ show HEAD:controller/cmd/hq-host/main.go | cmp -s - "${TEST_PAYLOAD}.program" ||
    fail "hq-host was built from the working tree"
git_ checkout -q -- controller/cmd/hq-host/main.go

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

# 3. The host's verdict is the check's exit; a platform it cannot build for
#    fails rather than guessing.
export TEST_HOST_EXIT=1
expect 1 "a host fault"
says "FAILED   deploy host deploy.example.test"
export TEST_HOST_EXIT=3
expect 2 "a release the host cannot verify"
says "UNKNOWN  deploy host deploy.example.test, read-only: the release it runs is unverified"
says "HOST NOT VERIFIED"
unset TEST_HOST_EXIT
export TEST_HOST_REFUSES="example.test: no space left" TEST_PROGRAM_PAD=1
expect 1 "a host that stops reading the check"
says "FAILED   deploy host deploy.example.test"
says "no space left"
if grep -q "Traceback" "${work}/out"; then fail "a host that stops reading crashed preflight"; fi
unset TEST_HOST_REFUSES TEST_PROGRAM_PAD
export TEST_UNAME="Linux aarch64"
expect 0 "an arm64 host"
logged "GOOS=linux GOARCH=arm64" "the check was not built for arm64"
export TEST_UNAME="Darwin arm64"
expect 1 "a host that is not Linux"
says "runs Darwin arm64"
unset TEST_UNAME

# 4. A failing local gate is not ready, and the verdict names it.
export TEST_FAIL_CI=1
expect 1 "a failing gate"
says "[preflight] FAILED in"
says "every gate the pipeline runs"
unset TEST_FAIL_CI
export TEST_FAIL_COMPOSED=1
expect 1 "a failing composed suite"
unset TEST_FAIL_COMPOSED

# 5. --remote reads what GitHub ran on HEAD instead of running `mise run ci`.
#    The fake gh answers from JSON pages, <API path with / as _>.<page>.json in
#    TEST_GH; every page, as one JSON array, only with --paginate --slurp. Watching a run
#    replaces the pages with those in TEST_GH/finished.
cat >"${bin}/gh" <<'EOF'
#!/bin/sh
echo "gh $*" >>"${TEST_LOG}"
case "$1" in
    repo) echo example/host ;;
    run) if [ -d "${TEST_GH}/finished" ]; then cp "${TEST_GH}/finished/"* "${TEST_GH}/"; fi ;;
    api)
        shift
        pages=1
        if [ "$1" = --paginate ] && [ "$2" = --slurp ]; then pages=99; shift 2; fi
        [ "$#" -eq 1 ] || exit 64
        key="$(printf '%s' "${1%%\?*}" | tr / _)"
        [ "${pages}" -eq 1 ] || printf '['
        page=1
        while [ "${page}" -le "${pages}" ] && [ -f "${TEST_GH}/${key}.${page}.json" ]; do
            [ "${page}" -eq 1 ] || printf ','
            cat "${TEST_GH}/${key}.${page}.json"
            page=$((page + 1))
        done
        [ "${pages}" -eq 1 ] || printf ']' ;;
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
unset TEST_GH PREFLIGHT_REMOTE_POLL PREFLIGHT_REMOTE_TIMEOUT

if [ "${failures}" -ne 0 ]; then
    echo "Preflight contracts failed (${failures})." >&2
    exit 1
fi
echo "Preflight contracts hold."
