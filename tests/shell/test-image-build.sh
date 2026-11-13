#!/bin/sh
# Prove image:build builds the automation worker's image beside the host's only
# when asked, labeled as the host image is, with a layer cache of its own that
# reads the host's and is written only when the host's is; that build hands the checks each image it built, or each one
# handed to it; and that the worker's check refuses a host image handed over
# without its worker image.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
# Run under lint:shell, this suite is inside that task's turn; a task run by
# its path outside one goes ahead as it is.
unset MISE_TASK_LOCKS HQ_MEASURED IMAGE RUNNER_IMAGE

fixture="${work}/checkout"
mkdir -p "${work}/bin"
for task in mise-tasks/image/build mise-tasks/build mise-tasks/image/verify/runner; do
    mkdir -p "${fixture}/$(dirname "${task}")"
    cp "${repo}/${task}" "${fixture}/${task}"
done
# shellcheck source=tests/shell/task-libraries.sh
. "${repo}/tests/shell/task-libraries.sh"
for task in mise-tasks/image/build mise-tasks/build mise-tasks/image/verify/runner; do
    copy_libraries "${repo}" "${fixture}" "${task}"
done
printf 'FROM python:3-slim-trixie@sha256:%s AS runtime\n' "$(printf '0%.0s' $(seq 64))" >"${fixture}/Dockerfile"
git -C "${fixture}" init -q
git -C "${fixture}" remote add origin git@example.com:example/hq.git
git -C "${fixture}" add -A
git -C "${fixture}" -c user.name=test -c user.email=test@example.com -c commit.gpgsign=false commit -q --no-verify -m fixture

# docker: each build's arguments, one a line, after a line naming the call.
cat >"${work}/bin/docker" <<'STUB'
#!/bin/sh
echo "call $1" >>"${TEST_CALLS}"
printf '%s\n' "$@" >>"${TEST_CALLS}"
STUB
# mise: each run's words, and for the checks the images they were handed.
cat >"${work}/bin/mise" <<'STUB'
#!/bin/sh
echo "mise $*" >>"${TEST_CALLS}"
case "$*" in
    *"image:verify:*"*) echo "checks IMAGE=${IMAGE:-} RUNNER_IMAGE=${RUNNER_IMAGE:-}" >>"${TEST_CALLS}" ;;
esac
STUB
cat >"${work}/bin/curl" <<'STUB'
#!/bin/sh
echo "Date: Mon, 02 Nov 2026 00:00:00 UTC"
STUB
chmod +x "${work}/bin/docker" "${work}/bin/mise" "${work}/bin/curl"

failures=0
fail() {
    echo "FAIL $*" >&2
    failures=$((failures + 1))
}

run() { # run <task> [NAME=value...]; the calls land in ${work}/calls, the status in ${work}/status
    task="$1"
    shift
    : >"${work}/calls"
    status=0
    (cd "${fixture}" && env PATH="${work}/bin:${PATH}" TEST_CALLS="${work}/calls" "$@" "./${task}") \
        >"${work}/out" 2>&1 || status=$?
    echo "${status}" >"${work}/status"
}

# The arguments of the nth docker buildx build.
build_call() {
    awk -v n="$1" '/^call /{ seen += ($2 == "buildx"); next } seen == n' "${work}/calls"
}
builds() { grep -c '^call buildx$' "${work}/calls" || true; }
labels() { build_call "$1" | grep '^org.opencontainers\|^dev.severino'; }

# 1. Without --runner-tag, the host image alone.
run mise-tasks/image/build usage_tag=example-host:test
[ "$(cat "${work}/status")" = 0 ] || fail "image:build failed: $(cat "${work}/out")"
[ "$(builds)" = 1 ] || fail "image:build without --runner-tag ran $(builds) builds, not one"
build_call 1 | grep -qx -- '--target' && fail "image:build without --runner-tag built a target"

# 2. With it, the runner stage after the host image, labeled the same. It
#    reads the host's cache and its own, and writes only its own, so neither
#    export replaces the other.
run mise-tasks/image/build usage_tag=example-host:test usage_runner_tag=example-runner:test \
    usage_cache=registry.example.com/cache:main usage_cache_write=true
[ "$(cat "${work}/status")" = 0 ] || fail "image:build --runner-tag failed: $(cat "${work}/out")"
[ "$(builds)" = 2 ] || fail "image:build --runner-tag ran $(builds) builds, not two"
build_call 1 >"${work}/host"
build_call 2 >"${work}/runner"
grep -A1 -x -- '--target' "${work}/runner" | grep -qx runner ||
    fail "the second build is not the runner stage"
grep -A1 -x -- '-t' "${work}/runner" | grep -qx example-runner:test || fail "the runner stage is not tagged as --runner-tag"
grep -A1 -x -- '--cache-from' "${work}/runner" | grep -qx 'type=registry,ref=registry.example.com/cache:main' ||
    fail "the runner stage does not read the host image's cache"
grep -A1 -x -- '--cache-from' "${work}/runner" | grep -qx 'type=registry,ref=registry.example.com/cache:main-runner' ||
    fail "the runner stage does not read its own cache"
grep -A1 -x -- '--cache-to' "${work}/runner" | grep -vx -- '--cache-to' >"${work}/runner-writes" || true
if [ "$(grep -c . "${work}/runner-writes")" != 1 ] ||
    ! grep -qx 'type=registry,ref=registry.example.com/cache:main-runner,mode=max' "${work}/runner-writes"; then
    fail "the runner stage does not write every layer to its own cache alone: $(cat "${work}/runner-writes")"
fi
grep -A1 -x -- '--cache-to' "${work}/host" | grep -vx -- '--cache-to' >"${work}/host-writes" || true
if [ "$(grep -c . "${work}/host-writes")" != 1 ] ||
    ! grep -qx 'type=registry,ref=registry.example.com/cache:main,mode=max' "${work}/host-writes"; then
    fail "the host image does not write its own cache alone: $(cat "${work}/host-writes")"
fi
[ -n "$(labels 1)" ] && [ "$(labels 1)" = "$(labels 2)" ] || fail "the runner image's labels differ from the host image's"
labels 2 | grep -qx 'org.opencontainers.image.source=https://example.com/example/hq' ||
    fail "the runner image does not name its source as the host image does"

# 2b. Reading without --cache-write, neither build writes a cache.
run mise-tasks/image/build usage_tag=example-host:test usage_runner_tag=example-runner:test \
    usage_cache=registry.example.com/cache:main
[ "$(cat "${work}/status")" = 0 ] || fail "image:build --cache failed: $(cat "${work}/out")"
grep -qx -- '--cache-to' "${work}/calls" && fail "a build without --cache-write writes a cache"
build_call 2 | grep -qx 'type=registry,ref=registry.example.com/cache:main-runner' ||
    fail "the runner stage does not read its own cache without --cache-write"

# 3. A host image handed over: nothing is built.
run mise-tasks/image/build IMAGE=example-host:given usage_tag=example-host:test usage_runner_tag=example-runner:test
[ "$(builds)" = 0 ] || fail "image:build with IMAGE handed over built $(builds) images"

# 4. build builds both and hands the checks each image it built.
run mise-tasks/build usage_tag=example-host:test usage_runner_tag=example-runner:test
[ "$(cat "${work}/status")" = 0 ] || fail "build failed: $(cat "${work}/out")"
grep '^mise run image:build ' "${work}/calls" | grep -q -- '--runner-tag example-runner:test' ||
    fail "build does not ask image:build for the worker's image"
grep -qx 'checks IMAGE=example-host:test RUNNER_IMAGE=example-runner:test' "${work}/calls" ||
    fail "build does not hand the checks the images it built: $(grep '^checks' "${work}/calls")"

# 5. Handed both, it hands the checks those, never the local worker tag.
run mise-tasks/build IMAGE=example-host:given RUNNER_IMAGE=example-runner:given usage_runner_tag=example-runner:test
grep -qx 'checks IMAGE=example-host:given RUNNER_IMAGE=example-runner:given' "${work}/calls" ||
    fail "build does not hand the checks the images handed to it: $(grep '^checks' "${work}/calls")"

# 6. A host image handed over without its worker's: the worker's check refuses
#    before it reads any image.
run mise-tasks/image/verify/runner IMAGE=example-host:given
[ "$(cat "${work}/status")" = 2 ] || fail "image:verify:runner with IMAGE alone exited $(cat "${work}/status"), not 2"
grep -q '^call' "${work}/calls" && fail "image:verify:runner with IMAGE alone ran docker"
grep -q RUNNER_IMAGE "${work}/out" || fail "image:verify:runner with IMAGE alone does not name RUNNER_IMAGE"

if [ "${failures}" -ne 0 ]; then
    echo "image build contracts failed (${failures})." >&2
    exit 1
fi
echo "image build contracts hold."
