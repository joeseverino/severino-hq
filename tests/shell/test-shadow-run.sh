#!/bin/sh
# tools/pipeline/shadow-run.sh against a fake docker and systemctl: a rehearsal
# passes only when every check passed and the live web was not restarted, it
# never renders secrets or restarts anything, and it leaves nothing behind.
set -eu
unset TEST_RESTART TEST_READY TEST_RUNNING TEST_PLAN_EXIT TEST_UID

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT HUP INT TERM
failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

bin="${work}/bin"
secrets="${work}/secrets"
mkdir -p "${bin}" "${secrets}/web" "${secrets}/ssh" "${work}/acme"
echo "DJANGO_SECRET_KEY=example" >"${secrets}/web/severino_hq_env"
echo '{"connections":[]}' >"${secrets}/controller-connections.json"
echo "example identity" >"${secrets}/ssh/id_example"
echo "example account" >"${work}/acme/account.json"

cat >"${bin}/docker" <<'STUB'
#!/bin/sh
echo "docker $*" >>"${TEST_LOG}"
case "$1" in
    inspect)
        case "$*" in
            *Mounts*) echo hq-data ;;
            *StartedAt*) echo "2026-10-18T09:00:00Z" ;;
            *Running*) echo "${TEST_RUNNING:-true}" ;;
            *OOMKilled*) echo false ;;
        esac ;;
    image) echo "amd64 0123456" ;;
    run)
        case "$*" in
            *" -d "*) echo container-id ;;
            *hq-controller*)
                [ -z "${TEST_RESTART:-}" ] || : >"${TEST_RESTARTED}"
                echo '{"ok":true,"mode":"plan"}'
                exit "${TEST_PLAN_EXIT:-0}" ;;
        esac ;;
    exec)
        case "$*" in
            *" ready "*) exit "${TEST_READY:-0}" ;;
            *integrity_check*) echo ok ;;
            *"job no.such.job"*) exit 1 ;;
            *" job "*) echo '{"ok":true}' ;;
        esac ;;
    logs) echo "Applying example.0002_example... OK" ;;
    stats) echo "example 150MiB / 1GiB" ;;
esac
exit 0
STUB
cat >"${bin}/systemctl" <<'STUB'
#!/bin/sh
echo "systemctl $*" >>"${TEST_LOG}"
if [ -e "${TEST_RESTARTED}" ]; then echo "Sun 2026-10-18 11:00:00 UTC"; else echo "Sun 2026-10-18 09:00:00 UTC"; fi
STUB
cat >"${bin}/id" <<'STUB'
#!/bin/sh
echo "${TEST_UID:-0}"
STUB
# Ownership is the host's business; the fake account cannot give files away.
cat >"${bin}/chown" <<'STUB'
#!/bin/sh
exit 0
STUB
chmod 0755 "${bin}/docker" "${bin}/systemctl" "${bin}/id" "${bin}/chown"

TEST_LOG="${work}/calls"
TEST_RESTARTED="${work}/restarted"
export TEST_LOG TEST_RESTARTED
scratch="${work}/scratch"

shadow() { # shadow <expected status> <why>
    : >"${TEST_LOG}"
    rm -f "${TEST_RESTARTED}"
    status=0
    PATH="${bin}:${PATH}" SHADOW_SECRET_DIR="${secrets}" SHADOW_ACME_DIR="${work}/acme" \
        SHADOW_DIR="${scratch}" SHADOW_READY_TRIES=2 \
        sh "${repo_dir}/tools/pipeline/shadow-run.sh" example.test/hq@sha256:0123 >"${work}/out" 2>&1 || status=$?
    [ "${status}" -eq "$1" ] || fail "$2: exit ${status}, expected $1
$(cat "${work}/out")"
}
verdict() { # verdict <expected last line> <why>
    [ "$(tail -n 1 "${work}/out")" = "$1" ] || fail "$2: verdict was '$(tail -n 1 "${work}/out")'"
}
untouched() { # untouched <why>: nothing live was restarted, nothing rendered, nothing left
    if grep -E 'hq-secrets|restart|stop ' "${TEST_LOG}" | grep -v '^docker rm -f hq-shadow$'; then
        fail "$1: touched something live"
    fi
    [ ! -e "${scratch}" ] || fail "$1: left the scratch directory"
    grep -q '^docker volume rm hq-shadow-data hq-shadow-bridge' "${TEST_LOG}" || fail "$1: left the volumes"
}

# 1. A healthy rehearsal passes, and the controller never sees the database.
shadow 0 "a healthy rehearsal"
verdict "shadow-run: result=pass ready=yes socket=ok integrity=ok jobs=ok memory=ok plan=ok live=untouched" \
    "a healthy rehearsal"
untouched "a healthy rehearsal"
plan_call="$(grep 'entrypoint /usr/local/bin/hq-controller' "${TEST_LOG}")"
case "${plan_call}" in
    *target=/data*) fail "the controller was given the database" ;;
esac
case "${plan_call}" in
    *"/plan/connections.json,target=/run/secrets/controller-connections.json,readonly"*) ;;
    *) fail "the controller did not plan on a copy of the connections: ${plan_call}" ;;
esac
grep -q 'type=volume,source=hq-data,target=/src' "${TEST_LOG}" || fail "the production database was not copied"
if grep 'docker run -d' "${TEST_LOG}" | grep -qv -- '--network none'; then
    fail "the shadow web process had a network"
fi

# 2. The live web restarting meanwhile fails the rehearsal, whatever else passed.
TEST_RESTART=1
export TEST_RESTART
shadow 1 "a live restart"
verdict "shadow-run: result=fail ready=yes socket=ok integrity=ok jobs=ok memory=ok plan=ok live=restarted" \
    "a live restart"
unset TEST_RESTART

# 3. A shadow that never becomes ready fails, plans nothing and still cleans up.
TEST_READY=1 TEST_RUNNING=false
export TEST_READY TEST_RUNNING
shadow 1 "a shadow that never became ready"
verdict "shadow-run: result=fail ready=no socket=- integrity=- jobs=- memory=- plan=- live=untouched" \
    "a shadow that never became ready"
if grep -q 'entrypoint /usr/local/bin/hq-controller' "${TEST_LOG}"; then fail "planned without a ready shadow"; fi
untouched "a shadow that never became ready"
unset TEST_READY TEST_RUNNING

# 4. A failed plan fails the rehearsal and says how.
TEST_PLAN_EXIT=3
export TEST_PLAN_EXIT
shadow 1 "a failed plan"
verdict "shadow-run: result=fail ready=yes socket=ok integrity=ok jobs=ok memory=ok plan=exit-3 live=untouched" \
    "a failed plan"
unset TEST_PLAN_EXIT

# 5. Only root runs it.
TEST_UID=1000
export TEST_UID
shadow 1 "an unprivileged run"
if [ -s "${TEST_LOG}" ]; then fail "an unprivileged run reached docker"; fi
unset TEST_UID

if [ "${failures}" -ne 0 ]; then
    echo "Shadow run contracts failed (${failures})." >&2
    exit 1
fi
echo "Shadow run contracts hold."
